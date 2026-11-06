# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""I/O half of the document-to-table bridge: read, align, synthesise, write.

:func:`run_bridge` works against injected stores and clients (unit-testable);
:func:`run_bridge_for_namespace` builds the real ones from the app config, and
:func:`start_bridge_job` runs it as a polled induction job.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from coa_common.language_tags import DEFAULT_LANGUAGE
from rdflib import RDFS, Graph, URIRef

from coa_ontology.inducer.bridge.planner import (
    BridgeOptions,
    BridgePlan,
    BridgeSettings,
    DocClassInfo,
    DocEvidence,
    TableInfo,
    add_grounding_links,
    apply_answer,
    apply_to_graph,
    collect_doc_classes,
    collect_tables,
    column_text,
    dedupe_links,
    doc_class_text,
    excerpt,
    llm_max_tokens,
    mentioned_columns,
    parse_response,
    passage_query,
    phrase_variants,
    render_prompt,
    select_candidates,
    system_prompt,
    table_text,
    unit,
    words,
)

log = logging.getLogger(__name__)

SOURCE_TYPE = "BRIDGE"
# Predicates the bridge writes per redefined subject (replaced, not accumulated).
_MAX_REPORT_NAMES = 1000


@dataclass
class BridgeDeps:
    """Stores and clients one bridge run talks to."""

    graph_store: Any
    vector_store: Any
    embedder: Any  # .embed_texts(list[str]) -> list[list[float]], .model_id
    llm: Any  # .generate(prompt, system=, max_tokens=) -> str
    lexical_store: Any | None = None
    grounding_service: Any | None = None
    # search(body) -> raw OpenSearch response over the namespace's document-chunk index
    chunk_search: Callable[[dict], dict] | None = None


# Bedrock error codes that fail the same way on every call (unknown model, no
# access, bad credentials): the run stops at the first one instead of retrying.
_FATAL_LLM_ERROR_CODES = frozenset(
    {"AccessDeniedException", "ResourceNotFoundException", "UnrecognizedClientException", "ExpiredTokenException"}
)
# Definition calls that fail in a row, across all tables, before the run stops.
_MAX_CONSECUTIVE_LLM_FAILURES = 8
# Share of the namespace induction lock's stale age a job may run for, so it
# finishes (and stores its proposal) while it still holds the lock.
_LOCK_DEADLINE_FRACTION = 0.8


class BridgeLLMError(RuntimeError):
    """An LLM call that returned no usable text (stop reason and usage in the message)."""


class BridgeDeadlineError(RuntimeError):
    """The run reached its deadline (nothing is proposed or written)."""


class BridgeLLM:
    """Bedrock ``converse`` for the definition calls.

    Separate from the shared ``LLMClient`` for two reasons:
    a reasoning model can spend the whole output budget thinking and return no text
    block, and a long answer outlives a 120 s read timeout. This client allows a large
    output budget and a long read, and reports ``stopReason`` + usage when no text came back.
    """

    def __init__(self, model_id: str, region: str, read_timeout: int = 900, max_pool_connections: int = 10):
        """Build the Bedrock runtime client with a long read timeout and adaptive retries.

        ``max_pool_connections`` must be at least the number of threads calling
        :meth:`generate` at once, or they queue for a connection.
        """
        import boto3
        from botocore.config import Config

        self.model_id = model_id
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                read_timeout=read_timeout,
                connect_timeout=10,
                retries={"max_attempts": 4, "mode": "adaptive"},
                max_pool_connections=max_pool_connections,
            ),
        )

    def generate(self, prompt: str, system: str = "", max_tokens: int = 4096) -> str:
        """Return the concatenated text blocks; raise :class:`BridgeLLMError` when there are none."""
        resp = self._client.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system}] if system else [],
            inferenceConfig={"maxTokens": max_tokens},
        )
        blocks = ((resp.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and "text" in b)
        stop = resp.get("stopReason")
        if not text.strip() or stop == "max_tokens":
            raise BridgeLLMError(
                f"no complete answer: stopReason={stop} usage={resp.get('usage')} text_chars={len(text)}"
            )
        return text


@dataclass
class _Counters:
    lock: Lock = field(default_factory=Lock, repr=False)
    grounding_errors: int = 0
    grounding_no_table_candidates: int = 0
    fact_lookup_errors: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    first_llm_error: str = ""
    consecutive_llm_failures: int = 0
    # Set once the run must stop; every later LLM call raises it at once.
    stop_reason: str = ""
    # time.monotonic() value after which no more calls start (None = no deadline).
    deadline: float | None = None
    notes: list[str] = field(default_factory=list)

    def check_deadline(self) -> None:
        """Raise :class:`BridgeDeadlineError` once the run is past its deadline."""
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise BridgeDeadlineError("the bridge run reached its deadline; lower maxTables")


def _llm_error_code(e: Exception) -> str:
    response = getattr(e, "response", None)
    return str(((response or {}).get("Error") or {}).get("Code") or "") if isinstance(response, dict) else ""


def _render_fact(f) -> str:
    tail = f.object_value if f.object_value is not None else (f.complement or "")
    return " ".join(f"{f.subject_value} {f.predicate} {tail}".split())[:300]


def _pick_facts(facts, limit: int) -> list[str]:
    """Distinct rendered facts, round-robin across predicates so one predicate cannot fill the budget."""
    by_pred: dict[str, list[str]] = {}
    seen: set[str] = set()
    for f in facts:
        text = _render_fact(f)
        if text and text not in seen:
            seen.add(text)
            by_pred.setdefault(f.predicate, []).append(text)
    out: list[str] = []
    while len(out) < limit and any(by_pred.values()):
        for pred in list(by_pred):
            if by_pred[pred] and len(out) < limit:
                out.append(by_pred[pred].pop(0))
    return out


def _doc_class_facts(lexical_store, doc: DocClassInfo, options: BridgeSettings) -> list[str]:
    if lexical_store is None or options.facts_per_class == 0:
        return []
    local = doc.iri.split("#")[-1].split("/")[-1]
    tried: list[str] = []
    for value in (doc.label, doc.label.replace("-", " "), local, local.replace("-", " ")):
        if not value or value in tried:
            continue
        tried.append(value)
        entities = lexical_store.get_entities_by_class(value, limit=options.entities_per_class)
        if entities:
            facts = lexical_store.get_facts_for_entities([e.entity_id for e in entities])
            return _pick_facts(facts, options.facts_per_class)
    return []


def _passages(chunk_search, table: TableInfo, options: BridgeSettings) -> list[str]:
    """Raw document passages that mention ``table`` or its columns, as prompt lines.

    Two bounded BM25 phrase queries per table over the namespace's chunk index: one
    for the table and all its columns, then one for the multi-word columns the first
    passages never named (wide tables otherwise spend the whole budget on a few
    columns). Each hit is cut to the text around its mentions and labelled with the
    columns it names.
    """
    if chunk_search is None or options.passages_per_table == 0:
        return []
    columns = table.columns[: options.max_columns_per_table]
    table_forms = [v for v in phrase_variants(table.name) if len(v) >= 3]
    out: list[str] = []
    seen: set[str] = set()
    covered: set[str] = set()
    used = 0

    def _take(resp: dict, budget: int) -> None:
        nonlocal used
        for hit in (resp.get("hits") or {}).get("hits") or []:
            text = str((hit.get("_source") or {}).get("value") or "")
            if not text.strip():
                continue
            cols = mentioned_columns(text, columns)
            names_table = any(
                re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", text, re.IGNORECASE) for v in table_forms
            )
            if not cols and not names_table:
                continue
            phrases = table_forms + [v for c in columns if c.name in cols for v in phrase_variants(c.name)]
            piece = excerpt(text, phrases, max_chars=options.passage_chars)
            if piece in seen or used + len(piece) > budget:
                continue
            seen.add(piece)
            used += len(piece)
            covered.update(cols)
            label = f"(mentions columns: {', '.join(cols[:20])})" if cols else "(mentions the table)"
            out.append(f"{label} {piece}")

    first = passage_query(table, max_columns=options.max_columns_per_table, size=options.passages_per_table)
    _take(chunk_search(first), int(options.passage_budget_chars * 0.6))
    missing = [c for c in columns if c.name not in covered and len(words(c.name)) > 1]
    if missing:
        follow = passage_query(
            table, max_columns=options.max_columns_per_table, size=options.passages_per_table, only_columns=missing
        )
        _take(chunk_search(follow), options.passage_budget_chars)
    return out


def _ground_tables(
    deps: BridgeDeps,
    ontology_id: str,
    docs: list[DocClassInfo],
    raw_vectors: dict[str, list[float]],
    table_iris: set[str],
    options: BridgeSettings,
    counters: _Counters,
) -> dict[str, list[tuple[str, str]]]:
    """Table-level alignment: ground each document class against the namespace's own table classes."""
    svc = deps.grounding_service
    if svc is None or not docs:
        return {}

    def _one(doc: DocClassInfo):
        counters.check_deadline()
        vec = raw_vectors.get(doc.iri)
        if not vec:
            return None
        return svc.ground_class(
            class_label=doc.label,
            class_description=doc.description,
            concept_vector=vec,
            model_id=deps.embedder.model_id,
            grounding_mode=options.grounding_mode,
            ontology_ids=[ontology_id],
            top_k=options.grounding_top_k,
            exclude_entity_uri=doc.iri,
            candidate_filter=lambda c: c.entity_uri in table_iris,
        )

    grounded: dict[str, list[tuple[str, str]]] = {}
    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        futures = [(doc, pool.submit(_one, doc)) for doc in docs]
        for doc, fut in futures:
            try:
                result = fut.result()
            except BridgeDeadlineError:
                raise
            except Exception as e:  # noqa: BLE001 — one class failing must not sink the run
                counters.grounding_errors += 1
                log.warning("bridge: grounding failed for %r: %s: %s", doc.label, type(e).__name__, e)
                continue
            if result is not None and result.chosen is None and not getattr(result, "candidates", None):
                counters.grounding_no_table_candidates += 1
            if (
                result is None
                or result.chosen is None
                or result.match_type not in ("exact", "high_confidence", "ambiguous")
            ):
                continue
            target = result.chosen.entity_uri
            if target not in table_iris:
                continue
            close = result.match_type != "ambiguous" and result.relationship in ("exactMatch", "closeMatch", None)
            grounded.setdefault(target, []).append(("closeMatch" if close else "relatedMatch", doc.iri))
    return grounded


def _synthesise_table(
    deps: BridgeDeps,
    table: TableInfo,
    evidence: list[DocEvidence],
    mentions: list[str],
    options: BridgeSettings,
    counters: _Counters,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> BridgePlan:
    plan = BridgePlan()
    system = system_prompt(namespace_language)
    refs = {e.ref: e.iri for e in evidence}
    columns = table.columns[: options.max_columns_per_table]
    step = options.columns_per_llm_call
    chunks = [columns[i : i + step] for i in range(0, len(columns), step)] or [[]]

    def _call(chunk, include_table: bool) -> tuple[dict | None, bool]:
        """Return ``(answer, splittable)`` for one call; ``answer`` is ``None`` on failure.

        ``splittable`` says whether a smaller chunk may succeed (an unparseable or
        cut-off answer). Raises when the run must stop: an error that every call
        would hit, too many failures in a row, or the deadline.
        """
        with counters.lock:
            if counters.stop_reason:
                raise BridgeLLMError(counters.stop_reason)
        counters.check_deadline()
        prompt = render_prompt(
            table,
            chunk,
            evidence,
            mentions,
            include_table=include_table,
            table_chars=options.table_definition_chars,
            column_chars=options.column_definition_chars,
        )
        max_tokens = llm_max_tokens(len(chunk))
        with counters.lock:
            counters.llm_calls += 1
        try:
            answer = parse_response(deps.llm.generate(prompt, system=system, max_tokens=max_tokens))
        except Exception as e:  # noqa: BLE001 — handled by the split/retry below
            error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning(
                "bridge: LLM call failed for table %s (%d columns, table=%s): %s: %s",
                table.name,
                len(chunk),
                include_table,
                type(e).__name__,
                str(e)[:500],
            )
            with counters.lock:
                counters.first_llm_error = counters.first_llm_error or error
                counters.consecutive_llm_failures += 1
                if _llm_error_code(e) in _FATAL_LLM_ERROR_CODES:
                    counters.stop_reason = f"definition call failed with an error every call would hit: {error}"
                elif counters.consecutive_llm_failures >= _MAX_CONSECUTIVE_LLM_FAILURES:
                    counters.stop_reason = (
                        f"{counters.consecutive_llm_failures} definition calls failed in a row; "
                        f"first error: {counters.first_llm_error}"
                    )
                if counters.stop_reason:
                    raise BridgeLLMError(counters.stop_reason) from e
            # Parse errors and cut-off answers (BridgeLLMError) may succeed on fewer columns.
            return None, isinstance(e, (ValueError, BridgeLLMError))
        with counters.lock:
            counters.consecutive_llm_failures = 0
        return answer, False

    def _run(chunk, include_table: bool, depth: int) -> None:
        answer, splittable = _call(chunk, include_table)
        if answer is None and splittable and depth < 2 and len(chunk) >= 4:
            # A too-long answer is the usual cause: halve the columns and try each half.
            half = len(chunk) // 2
            _run(chunk[:half], include_table, depth + 1)
            _run(chunk[half:], False, depth + 1)
            return
        if answer is None:
            answer, _ = _call(chunk, include_table)  # one plain retry
        if answer is None:
            with counters.lock:
                counters.llm_failures += 1
            plan.failed_tables.append(table.name)
            return
        apply_answer(
            plan,
            table,
            chunk,
            answer,
            refs,
            include_table=include_table,
            table_chars=options.table_definition_chars,
            column_chars=options.column_definition_chars,
        )

    for n, chunk in enumerate(chunks):
        _run(chunk, n == 0, 0)
    return plan


def build_plan(
    *,
    g: Graph,
    ontology_id: str,
    mapped_class_iris: set[str],
    options: BridgeSettings,
    deps: BridgeDeps,
    on_progress: Callable[[str], None] | None = None,
    namespace_language: str = DEFAULT_LANGUAGE,
    deadline: float | None = None,
) -> tuple[BridgePlan, dict[str, Any], list[TableInfo]]:
    """Align and synthesise; returns the plan, run statistics and the tables considered.

    ``deadline`` (a ``time.monotonic()`` value) stops the run with
    :class:`BridgeDeadlineError` once passed.
    """
    progress = on_progress or (lambda _msg: None)
    counters = _Counters(deadline=deadline)
    all_tables = collect_tables(g, mapped_class_iris)
    tables = all_tables[: options.max_tables]
    if len(all_tables) > len(tables):
        # A cap that binds is said in the report, never left silent.
        counters.notes.append(
            f"maxTables {options.max_tables}: {len(all_tables) - len(tables)} of {len(all_tables)} mapped tables "
            "(the last in name order) were not considered; raise maxTables (up to 200) to include them"
        )
    docs = collect_doc_classes(g, mapped_class_iris)
    stats: dict[str, Any] = {"tables_considered": len(tables), "doc_classes_in_ontology": len(docs)}
    if not docs:
        # The same loud precondition as the table side (see run_bridge).
        raise ValueError("no document classes in the ontology: accept a document induction before bridging")
    if not tables:
        stats["note"] = "no table classes"
        return BridgePlan(), stats, tables

    # 1. Embed tables, columns and document classes once.
    progress("embedding")
    keyed: list[tuple[str, str]] = []
    for t in tables:
        keyed.append((t.iri, table_text(t)))
        keyed.extend((c.iri, column_text(t, c)) for c in t.columns[: options.max_columns_per_table])
    keyed.extend((d.iri, doc_class_text(d)) for d in docs)
    raw = deps.embedder.embed_texts([text for _, text in keyed])
    raw_vectors = {k: v for (k, _), v in zip(keyed, raw, strict=True) if v}
    vectors = {k: unit(v) for k, v in raw_vectors.items()}
    stats["embedded_texts"] = len(raw_vectors)

    # 2. Candidates, then table-level grounding for the best-scoring doc classes.
    cand = select_candidates(tables, docs, vectors, column_min_similarity=options.column_min_similarity)
    doc_by_iri = {d.iri: d for d in docs}
    ranked = sorted(cand.doc_best, key=lambda i: -cand.doc_best[i])[: options.max_doc_classes]
    if len(cand.doc_best) > len(ranked):
        counters.notes.append(
            f"document classes: the {len(ranked)} best-matching of {len(cand.doc_best)} candidates were linked to "
            f"tables (cap {options.max_doc_classes}); the rest can still reach a table as similarity evidence"
        )
    progress(f"grounding {len(ranked)} document classes")
    table_iris = {t.iri for t in tables}
    grounded = _ground_tables(
        deps, ontology_id, [doc_by_iri[i] for i in ranked], raw_vectors, table_iris, options, counters
    )

    # 3. Evidence per table: grounded doc classes first, then column-similarity candidates.
    per_table_docs: dict[str, list[tuple[str, str | None]]] = {}
    for t in tables:
        chosen: list[tuple[str, str | None]] = []
        for rel, doc in grounded.get(t.iri, []):
            if doc not in [c for c, _ in chosen]:
                chosen.append((doc, rel))
        for doc, _sim in cand.table_docs.get(t.iri, []):
            if doc not in [c for c, _ in chosen]:
                chosen.append((doc, None))
        per_table_docs[t.iri] = chosen[: options.max_doc_classes_per_table]
    needed = sorted({d for lst in per_table_docs.values() for d, _ in lst})
    progress(f"reading facts for {len(needed)} document classes")
    facts: dict[str, list[str]] = {}
    if deps.lexical_store is None:
        counters.notes.append("no lexical store: document facts not used")
    if deps.lexical_store is not None and needed:
        with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
            futs = {d: pool.submit(_doc_class_facts, deps.lexical_store, doc_by_iri[d], options) for d in needed}
            for d, fut in futs.items():
                try:
                    facts[d] = fut.result()
                except Exception as e:  # noqa: BLE001 — evidence is best-effort per class
                    log.warning("bridge: fact lookup failed for %s: %s: %s", d, type(e).__name__, e)
                    counters.fact_lookup_errors += 1
                    facts[d] = []
    mentions: dict[str, list[str]] = {}
    if deps.chunk_search is not None and options.passages_per_table:
        progress(f"searching document passages for {len(tables)} tables")
        with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
            pfuts = {t.iri: pool.submit(_passages, deps.chunk_search, t, options) for t in tables}
            for t_iri, fut in pfuts.items():
                try:
                    mentions[t_iri] = fut.result()
                except Exception as e:  # noqa: BLE001 — passages are evidence, one table failing is not fatal
                    log.warning("bridge: passage search failed for %s: %s: %s", t_iri, type(e).__name__, e)
                    with counters.lock:
                        counters.notes.append(f"passage search failed: {type(e).__name__}")
    else:
        counters.notes.append("no chunk index search configured")

    # 4. One LLM pass per table (chunked by columns) — only where there is evidence.
    work = []
    for t in tables:
        ev = [
            DocEvidence(
                ref=f"D{i + 1}",
                iri=d,
                label=doc_by_iri[d].label,
                description=doc_by_iri[d].description,
                facts=facts.get(d, []),
                linked_relationship=rel,
            )
            for i, (d, rel) in enumerate(per_table_docs.get(t.iri, []))
        ]
        if ev or mentions.get(t.iri):
            work.append((t, ev, mentions.get(t.iri, [])))
    counters.check_deadline()
    progress(f"writing definitions for {len(work)} tables")
    plan = BridgePlan()
    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        futs2 = [
            pool.submit(_synthesise_table, deps, t, ev, m, options, counters, namespace_language) for t, ev, m in work
        ]
        for plan_fut in futs2:
            part = plan_fut.result()
            plan.updates.update(part.updates)
            plan.links |= part.links
            plan.failed_tables.extend(part.failed_tables)
    counters.check_deadline()
    add_grounding_links(plan, grounded)
    plan.links = dedupe_links(plan.links)

    stats.update(
        {
            "doc_classes_sent_to_grounding": len(ranked),
            "doc_classes_grounded_to_tables": len({d for pairs in grounded.values() for _, d in pairs}),
            "grounding_errors": counters.grounding_errors,
            "grounding_no_table_candidates": counters.grounding_no_table_candidates,
            "fact_lookup_errors": counters.fact_lookup_errors,
            "doc_classes_with_facts": sum(1 for v in facts.values() if v),
            "tables_with_evidence": len(work),
            "tables_with_passages": sum(1 for v in mentions.values() if v),
            "passages_used": sum(len(v) for v in mentions.values()),
            "tables_without_evidence": sorted(t.name for t in tables if t.iri not in {w[0].iri for w in work}),
            "llm_calls": counters.llm_calls,
            "llm_failures": counters.llm_failures,
            "first_llm_error": counters.first_llm_error,
            "failed_tables": sorted(set(plan.failed_tables)),
            "columns_offered": sum(min(len(t.columns), options.max_columns_per_table) for t, _, _ in work),
            "notes": sorted(set(counters.notes)),
        }
    )
    return plan, stats, tables


def report_for(plan: BridgePlan, tables: list[TableInfo], g: Graph) -> dict[str, Any]:
    """DynamoDB-safe summary (ints, strings, lists) of what the plan writes."""
    name_of: dict[str, str] = {}
    for t in tables:
        name_of[t.iri] = t.name
        for c in t.columns:
            name_of[c.iri] = f"{t.name}.{c.name}"
    table_iris = {t.iri for t in tables}
    ups = plan.updates.values()
    links = sorted(plan.links)

    def _doc_label(iri: str) -> str:
        value = g.value(URIRef(iri), RDFS.label)
        return str(value) if value is not None else iri.split("/")[-1]

    return {
        "table_definitions": sum(1 for u in ups if u.kind == "table"),
        "column_definitions": sum(1 for u in ups if u.kind == "column"),
        "replaced_boilerplate": sum(1 for u in ups if u.replaced),
        # Which rule made each replace: the word-based placeholder check, or the
        # model judging the existing description a placeholder. The model-judged
        # ones are the real descriptions a reviewer should look at before accepting.
        "replaced_by_rule": sum(1 for u in ups if u.replaced_by == "rule"),
        "replaced_by_model": sum(1 for u in ups if u.replaced_by == "model"),
        "replaced_by_model_subjects": sorted(name_of.get(u.iri, u.iri) for u in ups if u.replaced_by == "model")[
            :_MAX_REPORT_NAMES
        ],
        "appended_to_existing": sum(1 for u in ups if not u.replaced),
        "table_links": sum(1 for s, _, _ in links if s in table_iris),
        "column_links": sum(1 for s, _, _ in links if s not in table_iris),
        "close_match_links": sum(1 for _, r, _ in links if r == "closeMatch"),
        "related_match_links": sum(1 for _, r, _ in links if r == "relatedMatch"),
        "defined_tables": sorted(name_of.get(u.iri, u.iri) for u in ups if u.kind == "table")[:_MAX_REPORT_NAMES],
        "defined_columns": sorted(name_of.get(u.iri, u.iri) for u in ups if u.kind == "column")[:_MAX_REPORT_NAMES],
        "links": [f"{name_of.get(s, s)} {r} {_doc_label(d)}" for s, r, d in links][:_MAX_REPORT_NAMES],
    }


@dataclass
class BridgeProposal:
    """What the bridge proposes: an ontology slice and the R2RML of the tables it touches."""

    ontology_turtle: str
    r2rml_turtle: str
    datasource_ids: list[str]
    subjects: int
    tables: int


def _copy_subject(src: Graph, out: Graph, subject: Any, *, follow_named: bool = False) -> None:
    """Copy every triple of ``subject`` from ``src`` into ``out``, following blank nodes.

    With ``follow_named`` it also follows a named object that has triples of its own:
    table induction's R2RML names a TriplesMap's subject map, predicate-object maps and
    object maps (``<…/TriplesMap_X/SubjectMap>``), and without them the copy no longer
    says which class or columns the table maps. ``rr:parentTriplesMap`` is never
    followed, so a join stays a reference and another table's map is not copied.
    """
    from rdflib import BNode

    parent = URIRef("http://www.w3.org/ns/r2rml#parentTriplesMap")
    todo, seen = [subject], set()
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        for p, o in src.predicate_objects(node):
            out.add((node, p, o))
            if isinstance(o, BNode) or (
                follow_named and p != parent and isinstance(o, URIRef) and (o, None, None) in src
            ):
                todo.append(o)


def build_proposal(
    *,
    g: Graph,
    plan: BridgePlan,
    tables: list[TableInfo],
    r2rml_turtle: str | None,
    class_to_datasource: dict[str, str],
    namespace_language: str = DEFAULT_LANGUAGE,
) -> BridgeProposal | None:
    """The proposal for ``plan``: the touched tables as they read with the plan applied.

    The normal proposal accept writes it, so each touched table travels whole: the
    class with all its triples, every property whose domain it is (its columns and
    relationships), the labels of the classes those relationships point to, and its
    R2RML TriplesMaps. Accept then re-projects, re-maps and re-embeds those tables
    from the proposal alone exactly as a table induction does, replacing their
    descriptions and keeping the earlier ones in the description history. ``None``
    when the plan changes nothing.
    """
    from rdflib import OWL, RDF, Namespace

    if not plan.updates and not plan.links:
        return None
    after = Graph()
    for prefix, ns in g.namespaces():
        after.bind(prefix, ns, override=False)
    for triple in g:
        after.add(triple)
    apply_to_graph(after, plan, namespace_language)

    col_owner = {c.iri: t.iri for t in tables for c in t.columns}
    touched = {u.iri for u in plan.updates.values() if u.kind == "table"}
    touched |= {col_owner[u.iri] for u in plan.updates.values() if u.kind == "column" and u.iri in col_owner}
    touched |= {col_owner.get(s, s) for s, _, _ in plan.links}
    touched &= {t.iri for t in tables}

    out = Graph()
    for prefix, ns in g.namespaces():
        out.bind(prefix, ns, override=False)
    for iri in sorted(touched):
        cls = URIRef(iri)
        _copy_subject(after, out, cls)
        for prop in sorted(set(after.subjects(RDFS.domain, cls)), key=str):
            _copy_subject(after, out, prop)
            for target in after.objects(prop, RDFS.range):
                if isinstance(target, URIRef) and (target, RDF.type, OWL.Class) in after:
                    for label in after.objects(target, RDFS.label):
                        out.add((target, RDFS.label, label))

    rr = Namespace("http://www.w3.org/ns/r2rml#")
    mapping = Graph()
    if r2rml_turtle:
        source = Graph()
        source.parse(data=r2rml_turtle, format="turtle")
        for prefix, ns in source.namespaces():
            mapping.bind(prefix, ns, override=False)
        for tmap in source.subjects(RDF.type, rr.TriplesMap):
            classes = {str(c) for sm in source.objects(tmap, rr.subjectMap) for c in source.objects(sm, rr["class"])}
            if classes & touched:
                _copy_subject(source, mapping, tmap, follow_named=True)
    return BridgeProposal(
        ontology_turtle=out.serialize(format="turtle"),
        r2rml_turtle=mapping.serialize(format="turtle") if len(mapping) else "",
        datasource_ids=sorted({class_to_datasource[i] for i in touched if i in class_to_datasource}),
        subjects=len(set(out.subjects())),
        tables=len(touched),
    )


def run_bridge(
    *,
    namespace: str,
    ontology_id: str,
    r2rml_turtle: str | None,
    options: BridgeOptions | BridgeSettings,
    deps: BridgeDeps,
    on_progress: Callable[[str], None] | None = None,
    namespace_language: str = DEFAULT_LANGUAGE,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Plan the bridge on the accepted ontology ``ontology_id``; returns the report.

    Nothing is written. Unless ``dry_run``, the report's ``proposal`` holds the
    :class:`BridgeProposal` (``None`` when nothing would change); the job stores it
    as a pending proposal that the normal accept writes. ``namespace_language`` is
    the namespace's language tag: the definitions are written in it and tagged with
    it (see :func:`.planner.delta_graph`). ``max_seconds`` bounds the reading and
    LLM work (``None`` = no bound).
    """
    from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

    started = time.monotonic()
    options = BridgeSettings.of(options)
    progress = on_progress or (lambda _msg: None)
    progress("reading ontology")
    turtle = deps.graph_store.get_graph_turtle(ontology_id)
    if not turtle:
        raise ValueError(f"ontology {ontology_id!r} has no graph in namespace {namespace!r}")
    g = Graph()
    g.parse(data=turtle, format="turtle")
    ds_map = _class_datasource_map_from_r2rml(r2rml_turtle)
    if not ds_map:
        raise ValueError("no R2RML-mapped table classes: accept a table ontology before bridging")

    plan, stats, tables = build_plan(
        g=g,
        ontology_id=ontology_id,
        mapped_class_iris=set(ds_map),
        options=options,
        deps=deps,
        on_progress=progress,
        namespace_language=namespace_language,
        deadline=None if max_seconds is None else started + max_seconds,
    )
    attempted = stats.get("tables_with_evidence", 0)
    if attempted and not plan.updates and len(set(plan.failed_tables)) >= attempted:
        # Every definition call failed (wrong model id, no Bedrock access, ...):
        # fail the run instead of reporting an empty success.
        raise BridgeLLMError(
            f"every definition call failed for all {attempted} tables with evidence; "
            f"first error: {stats.get('first_llm_error') or 'unknown'}"
        )
    report: dict[str, Any] = {"ontology_id": ontology_id, "dry_run": options.dry_run, **stats}
    report.update(report_for(plan, tables, g))
    if not options.dry_run:
        report["proposal"] = build_proposal(
            g=g,
            plan=plan,
            tables=tables,
            r2rml_turtle=r2rml_turtle,
            class_to_datasource=ds_map,
            namespace_language=namespace_language,
        )
    report["elapsed_seconds"] = int(time.monotonic() - started)
    return report


# ── Wiring to the deployed stores ──────────────────────────────────────


def app_config() -> dict:
    """The running app's config (lazy import: ``main`` imports the routers that import this)."""
    from coa_ontology.main import app

    return app.state.config


def run_bridge_for_namespace(
    namespace: str,
    ontology_id: str,
    options: BridgeOptions,
    config: dict | None = None,
    on_progress: Callable[[str], None] | None = None,
    namespace_language: str = DEFAULT_LANGUAGE,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Build the deployed stores/clients and plan the bridge (see :func:`run_bridge`)."""
    from coa_common import DEFAULT_EMBED_MODEL_ID
    from coa_common.bedrock import FALLBACK_CHAT_MODEL_ID

    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient
    from coa_ontology.inducer.routers.induce_unstructured import _build_lexical_store
    from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
    from coa_ontology.inducer.services.grounding import GroundingService
    from coa_ontology.proposals import _accumulated_r2rml
    from coa_ontology.stores import build_stores
    from coa_ontology.stores.adapters import StoreOntologyCatalogAdapter

    cfg = config if config is not None else app_config()
    graph_store, vector_store = build_stores(namespace=namespace)
    lexical_store = None
    lexical_note = ""
    if cfg.get("neptune_endpoint"):
        try:
            lexical_store = _build_lexical_store(
                "neptune-db", cfg.get("bedrock_region", "us-east-1"), cfg, namespace=namespace
            )
        except Exception as e:  # noqa: BLE001 — facts are evidence, not a precondition
            log.warning("bridge: lexical store unavailable: %s: %s", type(e).__name__, e)
            lexical_note = f"lexical store unavailable: {type(e).__name__}"
    deps = BridgeDeps(
        graph_store=graph_store,
        vector_store=vector_store,
        embedder=BedrockEmbeddingClient(
            region=cfg.get("bedrock_region", "us-east-1"),
            model_id=cfg.get("bedrock_embed_model_id", DEFAULT_EMBED_MODEL_ID),
            dimensions=cfg.get("bedrock_embed_dimensions", 1024),
        ),
        llm=BridgeLLM(
            model_id=cfg.get("description_llm_model_id", "us.anthropic.claude-sonnet-4-6"),
            region=cfg.get("llm_region", "us-east-1"),
            max_pool_connections=max(10, options.concurrency),
        ),
        chunk_search=_chunk_searcher(vector_store, namespace),
        lexical_store=lexical_store,
        grounding_service=GroundingService(
            ontology_catalog=StoreOntologyCatalogAdapter(graph_store, vector_store, namespace=namespace),
            embedding_generator=EmbeddingGeneratorClient(cfg.get("embedding_generator_url", "")),
            llm_region=cfg.get("llm_region", "us-east-1"),
            llm_model_id=cfg.get("grounding_llm_model_id") or FALLBACK_CHAT_MODEL_ID,
            namespace=namespace,
        ),
    )
    report = run_bridge(
        namespace=namespace,
        ontology_id=ontology_id,
        r2rml_turtle=_accumulated_r2rml(ontology_id, namespace),
        options=options,
        deps=deps,
        on_progress=on_progress,
        namespace_language=namespace_language,
        max_seconds=max_seconds,
    )
    if lexical_note:
        report["notes"] = sorted({*report.get("notes", []), lexical_note})
    return report


def _chunk_searcher(vector_store, namespace: str) -> Callable[[dict], dict] | None:
    """Search over the namespace's document-chunk index, through the ontology vector store's AOSS client.

    The chunk index lives in the same collection as the ontology index; ``None``
    when the store is not AOSS-backed.
    """
    from coa_common import graphrag_chunk_index_name

    aoss = getattr(vector_store, "_aoss", None)
    if aoss is None:
        return None
    index = graphrag_chunk_index_name(namespace)
    return lambda body: aoss().search_raw(index, body)


def _best_effort(what: str, fn: Callable[[], Any]) -> None:
    """Run ``fn``; log a failure at WARNING instead of raising (job bookkeeping must not mask the run)."""
    try:
        fn()
    except Exception:  # noqa: BLE001 — logged, never raised
        log.warning("bridge: %s failed", what, exc_info=True)


def _run_bridge_job(
    job_id: str,
    namespace: str,
    ontology_id: str,
    options: BridgeOptions,
    config: dict,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> None:
    from coa_ontology import dynamo_store
    from coa_ontology.induce_catalog import _join_watchdog, _start_watchdog

    stop = Event()
    watchdog = None
    try:
        dynamo_store.update_job_status(job_id, "running", namespace=namespace, source_type=SOURCE_TYPE)
        watchdog = _start_watchdog(job_id, namespace, stop, region=config.get("llm_region"))

        def _progress(msg: str) -> None:
            log.info("bridge[%s]: %s", job_id, msg)
            _best_effort(
                "progress update",
                lambda: dynamo_store.update_job_status(job_id, "running", namespace=namespace, progress=msg),
            )

        report = run_bridge_for_namespace(
            namespace,
            ontology_id,
            options,
            config,
            on_progress=_progress,
            namespace_language=namespace_language,
            # Stop well before the lock turns stale and another induction may take it.
            max_seconds=dynamo_store._STALE_INDUCTION_LOCK_SECONDS * _LOCK_DEADLINE_FRACTION,
        )
        proposal = report.pop("proposal", None)
        if proposal is not None:
            # 1:1 proposal_id <-> job_id, as for the other inductions. A STRUCTURED
            # proposal, so the accept carries its R2RML (the touched tables' mapping)
            # and refreshes the VKG copy like a table induction.
            dynamo_store.create_proposal(
                proposal_id=job_id,
                job_id=job_id,
                namespace=namespace,
                ontology_id=ontology_id,
                proposal_type="bridge",
                ontology_turtle=proposal.ontology_turtle,
                r2rml_turtle=proposal.r2rml_turtle,
                source_type="STRUCTURED",
                metadata={
                    "label": "Document-to-table bridge",
                    "source_type": "STRUCTURED",
                    "strategy": "bridge_to_tables",
                    "datasource_ids": proposal.datasource_ids,
                    "subject_count": proposal.subjects,
                    # The proposals list shows a structured proposal's size from this key.
                    "tables_processed": proposal.tables,
                    "namespaceLanguage": namespace_language,
                },
            )
            report["proposal_id"] = job_id
        dynamo_store.update_job_status(job_id, "completed", namespace=namespace, bridge_report=report)
        log.info("bridge[%s]: completed %s", job_id, {k: v for k, v in report.items() if isinstance(v, int)})
    except Exception as e:
        log.exception("bridge job %s failed", job_id)
        error = f"{type(e).__name__}: {str(e)[:1024]}"
        _best_effort(
            "writing the failed status",
            lambda: dynamo_store.update_job_status(job_id, "failed", namespace=namespace, error=error),
        )
    finally:
        _join_watchdog(watchdog, stop, job_id, log)
        _best_effort("releasing the induction lock", lambda: dynamo_store.release_induction_lock(namespace, job_id))


def start_bridge_job(
    namespace: str,
    ontology_id: str,
    options: BridgeOptions,
    config: dict,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> str:
    """Start a bridge job (holds the namespace induction lock while it runs); returns the job id."""
    from fastapi import HTTPException

    from coa_ontology import dynamo_store
    from coa_ontology.proposals import PROPOSAL_STATUS_EMBEDDINGS_SYNC, PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    if not dynamo_store.get_ontology_registry(namespace, ontology_id):
        raise HTTPException(404, f"Ontology '{ontology_id}' not found in namespace '{namespace}'")
    job_id = str(uuid.uuid4())
    try:
        dynamo_store.acquire_induction_lock(namespace, job_id)
    except dynamo_store.InductionLockHeldError as e:
        raise HTTPException(
            409, {"message": "An induction is already in progress for this namespace.", "job_id": e.job_id}
        ) from e
    try:
        for status in ("accepting", PROPOSAL_STATUS_EMBEDDINGS_SYNC):
            busy = dynamo_store.list_proposals(namespace=namespace, status=status)
            if busy:
                raise HTTPException(
                    409, {"message": "A proposal accept is in progress.", "proposal_id": busy[0].get("proposal_id")}
                )
        # Like a table induction, wait for an unreviewed structured proposal (a table
        # re-induction, or an earlier bridge proposal): a bridge planned from the state
        # before it would overwrite its changes when both are accepted. Checked after the
        # lock is held, as the table induction does, so the read is authoritative.
        for status in PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION:
            pending = dynamo_store.list_proposals(namespace=namespace, status=status, source_type="STRUCTURED")
            if pending:
                raise HTTPException(
                    409,
                    {
                        "message": "A proposal is awaiting review. Accept or reject it before bridging.",
                        "proposal_id": pending[0].get("proposal_id"),
                    },
                )
        dynamo_store.put_job(
            job_id=job_id,
            status="pending",
            request_body={"ontology_uri_prefix": ontology_id, "label": "bridge_to_tables"},
            datasource_details=[],
            namespace=namespace,
            source_type=SOURCE_TYPE,
        )
        dynamo_store.update_job_status(
            job_id,
            "pending",
            namespace=namespace,
            bridge_options=json.dumps(options.model_dump()),
            started_at=datetime.now(UTC).isoformat(),
        )
        Thread(
            target=_run_bridge_job,
            args=(job_id, namespace, ontology_id, options, config, namespace_language),
            daemon=True,
        ).start()
    except Exception:
        _best_effort("releasing the induction lock", lambda: dynamo_store.release_induction_lock(namespace, job_id))
        raise
    return job_id
