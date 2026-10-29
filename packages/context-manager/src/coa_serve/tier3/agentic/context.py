# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Accumulated-context store for one Agentic Retriever reasoning session.

A single :class:`AccumulatedContext` is created per request and shared across
every Sub_Question and Exploration_Branch. It is the agent's working memory: it
holds the native ``ChunkResult`` / ``GraphEntity`` objects gathered so far (fed
verbatim to the final ``Synthesizer``), the per-request cached ontology
(Req 3.8), the set of de-duplication keys that powers the "no new information"
stop (Req 6.3), and the running list of degraded sources (Req 9).

Each :class:`~coa_serve.tier3.agentic.tools.base.ToolResult` is
incorporated via :meth:`AccumulatedContext.add` *before* the controller selects
the next Reasoning_Step (Req 4.2). ``add`` de-duplicates by a stable per-item key
and returns the count of genuinely NEW retrieval results, so a step that adds
nothing already-unseen returns ``0`` and lets the controller stop with
``no_new_information`` (Req 6.3).

Import discipline (Requirement 12.3): this module is dependency-free at load
time. ``ChunkResult``, ``GraphEntity``, and ``Ontology`` are referenced only as
string / ``TYPE_CHECKING`` annotations and handled structurally (via ``getattr``)
at runtime, so importing this module never pulls ``graphrag_toolkit`` — nor even
the sibling retriever modules — into ``sys.modules``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import structlog

from ...graph_context import (
    LEGACY_SUPPORTING_STATEMENTS_KEY,
    SUPPORTING_STATEMENTS_KEY,
    bound_supporting_statements,
    encode_supporting_statements,
)

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from ..graph_traverser import GraphEntity
    from ..vector_retriever import ChunkResult
    from .ontology.source import Ontology
    from .tools.base import ExecutedQuery, ToolResult


def _chunk_key(chunk: Any) -> str:
    """Derive a stable de-dupe key for a retrieved chunk (Req 6.3).

    Prefers the chunk's own identifier, then its source URI, and finally a hash
    of its text so that two chunks carrying the same content de-duplicate even
    when neither id nor uri is populated. Handled structurally (``getattr``) so
    the function never needs to import ``ChunkResult``.
    """
    chunk_id = getattr(chunk, "chunk_id", "") or ""
    if chunk_id:
        return f"chunk:{chunk_id}"
    uri = getattr(chunk, "uri", "") or ""
    if uri:
        return f"chunk:{uri}"
    text = getattr(chunk, "text", "") or ""
    return f"chunk:#{sha256(text.encode('utf-8')).hexdigest()}"


def _entity_key(entity: Any) -> str:
    """Derive a stable de-dupe key for a retrieved entity (Req 6.3).

    Prefers the entity URI (the graph's stable identifier), falling back to a
    ``label|type`` composite when no URI is present.
    """
    uri = getattr(entity, "uri", "") or ""
    if uri:
        return f"entity:{uri}"
    label = getattr(entity, "label", "") or ""
    etype = getattr(entity, "type", "") or ""
    return f"entity:{label}|{etype}"


_MAX_ENTITY_PROPERTIES = 50
_MAX_RELATIONSHIPS_PER_ENTITY = 10

# Upper bound on distinct entities one reasoning session accumulates. Nothing
# upstream bounded this: a graph traversal returning thousands of connected nodes
# put every one of them into the planner summary, the synthesis prompt, the wire
# response, and the persisted session envelope. Generous enough that no realistic
# session reaches it; hitting it is recorded as a degraded source rather than
# silently truncating the evidence set.
MAX_CONTEXT_ENTITIES = 500
_RELATIONSHIP_ALIASES = {
    "source_uri": ("source_uri", "sourceUri"),
    "source_label": ("source_label", "sourceLabel"),
    "predicate_uri": ("predicate_uri", "predicateUri"),
    "predicate_label": ("predicate_label", "predicateLabel"),
    "target_uri": ("target_uri", "targetUri"),
    "target_label": ("target_label", "targetLabel"),
}


def _statement_values(value: Any) -> list[str]:
    """Decode one bounded, ordered ``supportingStatements`` property value."""
    return bound_supporting_statements(value)


def _merge_entity_properties(existing: dict, incoming: dict) -> dict:
    """Merge graph properties in stable fork order without losing statement evidence.

    Scalar property conflicts retain the first non-empty value, matching the
    deterministic Sub_Question merge order. Distinct keys from later forks are
    appended until the property bound. ``supportingStatements`` is an ordered,
    de-duplicated union with its own prompt-safety bound.
    """
    statement_keys = {SUPPORTING_STATEMENTS_KEY, LEGACY_SUPPORTING_STATEMENTS_KEY}
    merged: dict = {}
    for properties in (existing, incoming):
        for key, value in properties.items():
            if key in statement_keys or key in merged or len(merged) >= _MAX_ENTITY_PROPERTIES:
                continue
            merged[key] = value

    statements = bound_supporting_statements(
        [
            *_statement_values(existing.get(SUPPORTING_STATEMENTS_KEY, existing.get(LEGACY_SUPPORTING_STATEMENTS_KEY))),
            *_statement_values(incoming.get(SUPPORTING_STATEMENTS_KEY, incoming.get(LEGACY_SUPPORTING_STATEMENTS_KEY))),
        ]
    )
    if statements:
        merged[SUPPORTING_STATEMENTS_KEY] = encode_supporting_statements(statements)
    return merged


def _relationship_value(relationship: dict, field: str) -> str:
    """Read one canonical relationship field through supported aliases."""
    for alias in _RELATIONSHIP_ALIASES[field]:
        value = relationship.get(alias)
        if isinstance(value, str) and value:
            return value
    return ""


def _canonical_relationship(relationship: dict) -> dict:
    """Normalize a directed relationship while preserving non-contract fields."""
    canonical = {field: value for field in _RELATIONSHIP_ALIASES if (value := _relationship_value(relationship, field))}
    alias_keys = {alias for aliases in _RELATIONSHIP_ALIASES.values() for alias in aliases}
    for key, value in relationship.items():
        if key not in alias_keys:
            canonical[key] = value
    return canonical


def _relationship_identity(relationship: dict) -> tuple[str, ...]:
    """Return stable directed-triple identity, falling back for legacy partial rows."""
    source_uri = _relationship_value(relationship, "source_uri")
    predicate_uri = _relationship_value(relationship, "predicate_uri")
    target_uri = _relationship_value(relationship, "target_uri")
    if source_uri and predicate_uri and target_uri:
        return ("triple", source_uri, predicate_uri, target_uri)
    return ("legacy", json.dumps(relationship, sort_keys=True, default=str))


def _merge_relationship_labels(existing: dict, incoming: dict) -> dict:
    """Enrich one triple with the first non-empty label seen in stable fork order."""
    merged = dict(existing)
    for label_field in ("source_label", "predicate_label", "target_label"):
        if not _relationship_value(merged, label_field):
            value = _relationship_value(incoming, label_field)
            if value:
                merged[label_field] = value
    return merged


def _merge_entity_relationships(existing: Any, incoming: Any) -> tuple[tuple[dict, ...], int]:
    """Return a bounded semantic union and the number of distinct rows discarded."""
    merged: list[dict] = []
    positions: dict[tuple[str, ...], int] = {}
    seen: set[tuple[str, ...]] = set()
    dropped = 0
    for relationship in [
        *(getattr(existing, "relationships", ()) or ()),
        *(getattr(incoming, "relationships", ()) or ()),
    ]:
        if not isinstance(relationship, dict):
            continue
        canonical = _canonical_relationship(relationship)
        identity = _relationship_identity(canonical)
        if identity in seen:
            index = positions.get(identity)
            if index is not None:
                merged[index] = _merge_relationship_labels(merged[index], canonical)
            continue
        seen.add(identity)
        if len(merged) >= _MAX_RELATIONSHIPS_PER_ENTITY:
            dropped += 1
            continue
        positions[identity] = len(merged)
        merged.append(canonical)
    return tuple(merged), dropped


def _merge_entities(existing: Any, incoming: Any) -> tuple[Any, int]:
    """Merge duplicate entities and report distinct relationships over the cap."""
    updates: dict[str, Any] = {}

    relationships, dropped_relationships = _merge_entity_relationships(existing, incoming)
    if relationships != tuple(getattr(existing, "relationships", ()) or ()):
        updates["relationships"] = relationships

    if hasattr(existing, "properties"):
        existing_properties = dict(getattr(existing, "properties", {}) or {})
        properties = _merge_entity_properties(
            existing_properties,
            dict(getattr(incoming, "properties", {}) or {}),
        )
        if properties != existing_properties:
            updates["properties"] = properties

    for field_name in ("label", "type"):
        existing_value: Any = getattr(existing, field_name, "")
        incoming_value: Any = getattr(incoming, field_name, "")
        if not existing_value and incoming_value:
            updates[field_name] = incoming_value

    return (replace(existing, **updates) if updates else existing), dropped_relationships


def _normalize_entity(entity: Any) -> tuple[Any, int]:
    """Apply relationship/property bounds and report distinct relationships dropped."""
    updates: dict[str, Any] = {}
    relationships, dropped_relationships = _merge_entity_relationships(None, entity)
    if relationships != tuple(getattr(entity, "relationships", ()) or ()):
        updates["relationships"] = relationships
    if hasattr(entity, "properties"):
        existing_properties = dict(getattr(entity, "properties", {}) or {})
        properties = _merge_entity_properties({}, existing_properties)
        if properties != existing_properties:
            updates["properties"] = properties
    return (replace(entity, **updates) if updates else entity), dropped_relationships


def _row_key(row: dict) -> str:
    """Derive a stable de-dupe key for a structured result row (Req 6.3).

    A structured row is a plain ``dict`` (one NL→SQL / metric result record) with
    no natural id, so the key is a hash of its canonical (sorted-key) serialization
    — two identical rows de-duplicate, distinct rows do not. Non-JSON values fall
    back to ``repr`` via ``default=str`` so the key derivation never raises.
    """
    canonical = json.dumps(row, sort_keys=True, default=str)
    return f"row:#{sha256(canonical.encode('utf-8')).hexdigest()}"


# Caps so the carried reasoning state can never blow the planner prompt.
_MAX_ATTEMPT_INPUT_CHARS = 120
_MAX_WORKING_ANSWER_CHARS = 2000


def _short_input(value: Any) -> Any:
    """Summarize a tool-input value for the compact attempt-history record.

    Long strings are truncated and long lists are reduced to a count + head so a
    bulky input (e.g. a list of entity ids) cannot bloat the carried state that is
    echoed back into the planner prompt each step.
    """
    if isinstance(value, str):
        return value if len(value) <= _MAX_ATTEMPT_INPUT_CHARS else value[:_MAX_ATTEMPT_INPUT_CHARS] + "…"
    if isinstance(value, list | tuple):
        head = [_short_input(v) for v in list(value)[:3]]
        extra = len(value) - len(head)
        return head + [f"(+{extra} more)"] if extra > 0 else head
    return value


@dataclass
class AccumulatedContext:
    """The agent's shared working memory for a single reasoning session.

    Attributes:
        chunks: Native chunk objects gathered so far, in arrival order, passed
            through to the final Synthesizer. De-duplicated by :meth:`add`.
        entities: Native graph-entity objects gathered so far, in arrival order,
            passed through to the final Synthesizer. De-duplicated by :meth:`add`.
        ontology: The ontology resolved once per request and reused on subsequent
            lookups (Req 3.8); ``None`` until the Ontology_Lookup_Tool populates
            it.
        seen_keys: Stable de-dupe keys for every chunk/entity already
            incorporated, so a repeated retrieval result is not double-counted
            and drives the ``no_new_information`` stop (Req 6.3).
        degraded_sources: Running list of ``{"source", "reason"}`` records for
            Tools that failed or timed out, surfaced in the Tier3_Result (Req 9).
    """

    chunks: list[ChunkResult] = field(default_factory=list)
    entities: list[GraphEntity] = field(default_factory=list)
    # Structured result rows (NL→SQL / metric tools), in arrival order, passed to
    # the final Synthesizer alongside chunks/entities. De-duplicated by :meth:`add`.
    rows: list[dict] = field(default_factory=list)
    ontology: Ontology | None = None
    seen_keys: set[str] = field(default_factory=set)
    degraded_sources: list[dict] = field(default_factory=list)
    # Query artifacts (SQL/SPARQL) that structured tools built and executed this
    # session, in arrival order — one entry per generated statement. Surfaced
    # (gated by includeDebugInfo) on the final QueryResult so Deep Reasoning
    # returns the queries it ran, mirroring Standard mode (#1166). Each item is
    # an ``ExecutedQuery`` (kind + query). Never de-duplicated: two distinct
    # sub-questions legitimately run distinct SQL, and even an identical repeat
    # is a real second execution worth showing.
    executed_queries: list[ExecutedQuery] = field(default_factory=list)
    # ── Carried reasoning state (improvement ideas 2 & 3) ──────────────
    # The planner is stateless across steps (a fresh Converse call each step), so
    # cross-step continuity lives here and is echoed back into the planner prompt.
    #
    # attempt_history: one record per Reasoning_Step the controller ran —
    #   {"tool", "inputs", "new_count", "status"} — so the planner can SEE what it
    #   already tried (and that an attempt added nothing) and deliberately escalate
    #   to a different tool / reframing instead of repeating it (idea 4).
    # working_answers: the decision agent's running, context-grounded best-guess
    #   answer per sub-question — {"question", "answer"} — updated as evidence
    #   arrives and handed to the synthesizer alongside the chunks (idea 2).
    attempt_history: list[dict] = field(default_factory=list)
    working_answers: list[dict] = field(default_factory=list)
    # Edge-type predicates discovered present on anchored entities via the graph
    # tool's ``edge_types`` mode (idea 5 soft-prior): raw ``r.value`` labels the
    # planner can propose for an ``edge_typed`` traversal even when the pruned
    # ontology omits them. Insertion order preserved (discovery is frequency-ranked).
    present_edge_types: list[str] = field(default_factory=list)
    # BCP 47 tag of the user's question, resolved ONCE at session start and read by
    # every step that writes prose (the planner's working answer and the final
    # synthesis), so no step has to guess the language again. Empty means
    # unresolved: those steps fall back to "match the question's language".
    answer_language: str = ""
    # Position of each accumulated entity by its stable dedupe key. Without it,
    # merging an already-seen entity rescanned `entities` linearly and recomputed a
    # key per element, making accumulation O(n^2) in the entity count.
    entity_positions: dict[str, int] = field(default_factory=dict)
    # Number of distinct entities refused because `MAX_CONTEXT_ENTITIES` was
    # reached. Non-zero means the evidence set is provably incomplete.
    dropped_entities: int = 0
    # Whether any entity supplied more distinct relationships than the bounded
    # agentic working set could retain. The exact overage can recur across
    # retrieval steps, so the contract records the degradation rather than
    # presenting a misleading cumulative count.
    relationship_cap_reached: bool = False

    def _record_relationship_cap(self, dropped_in_merge: int) -> None:
        """Record the first relationship-cap event for this request context."""
        if dropped_in_merge <= 0 or self.relationship_cap_reached:
            return
        self.relationship_cap_reached = True
        logger.warning(
            "agentic_context_relationship_cap_reached",
            cap_per_entity=_MAX_RELATIONSHIPS_PER_ENTITY,
            dropped_in_merge=dropped_in_merge,
        )
        self.record_degraded("accumulated_context", "relationship_cap_reached")

    def _add_or_merge_entity(self, entity: GraphEntity) -> bool:
        """Add one entity or merge new evidence into its existing stable slot."""
        key = _entity_key(entity)
        if key not in self.seen_keys:
            if len(self.entities) >= MAX_CONTEXT_ENTITIES:
                self.dropped_entities += 1
                if self.dropped_entities == 1:
                    logger.warning(
                        "agentic_context_entity_cap_reached",
                        cap=MAX_CONTEXT_ENTITIES,
                    )
                    self.record_degraded("accumulated_context", "entity_cap_reached")
                return False
            self.seen_keys.add(key)
            self.entity_positions[key] = len(self.entities)
            normalized, dropped_relationships = _normalize_entity(entity)
            self.entities.append(normalized)
            self._record_relationship_cap(dropped_relationships)
            return True

        index = self.entity_positions.get(key)
        if index is None:
            # A caller that appended to `entities` directly (tests, older code) has
            # no recorded position; rebuild the index once rather than rescanning
            # on every subsequent merge.
            self._reindex_entities()
            index = self.entity_positions.get(key)
        if index is None:
            return False

        existing = self.entities[index]
        merged, dropped_relationships = _merge_entities(existing, entity)
        self._record_relationship_cap(dropped_relationships)
        if merged == existing:
            return False
        self.entities[index] = merged
        return True

    def _reindex_entities(self) -> None:
        """Rebuild :attr:`entity_positions` from :attr:`entities` (first-seen wins)."""
        self.entity_positions = {}
        for index, existing in enumerate(self.entities):
            self.entity_positions.setdefault(_entity_key(existing), index)

    def add(self, result: ToolResult) -> int:
        """Incorporate a ToolResult's chunks/entities/rows, returning the NEW count.

        De-duplicates every chunk, entity, and structured row in ``result``
        against :attr:`seen_keys` using a stable per-item key. Only items whose
        key has not been seen before are appended to :attr:`chunks` /
        :attr:`entities` / :attr:`rows` and counted; previously seen items are
        skipped so they are never double-counted (Req 6.3). Arrival order is
        preserved.

        Rows are counted toward progress exactly like chunks/entities — this is
        why a successful structured tool (NL→SQL / metric) is not mis-scored as
        no-progress: its output arrives on ``result.rows``, not ``result.chunks``.

        Args:
            result: The Tool invocation output to incorporate (Req 4.2).

        Returns:
            The number of genuinely new (not-already-present) retrieval results
            added by this call. ``0`` means the step surfaced no new information
            and the controller may stop with ``no_new_information`` (Req 6.3).
        """
        new_count = 0

        for chunk in result.chunks:
            key = _chunk_key(chunk)
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)
            self.chunks.append(chunk)
            new_count += 1

        for entity in result.entities:
            if self._add_or_merge_entity(entity):
                new_count += 1

        for row in result.rows:
            key = _row_key(row)
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)
            self.rows.append(row)
            new_count += 1

        # Collect any query artifacts the tool executed (#1166). Not counted as
        # progress (that is what rows/chunks/entities are for) and not
        # de-duplicated — this is an observability record of what ran.
        self.executed_queries.extend(result.executed_queries)

        return new_count

    def fork(self) -> AccumulatedContext:
        """Return a private sub-context for one parallel sub-question (P2).

        The fork starts EMPTY (own ``chunks`` / ``entities`` / ``seen_keys`` /
        ``attempt_history`` / ``working_answers`` / ``degraded_sources``) but shares
        the already-resolved :attr:`ontology` by reference (read-only during the
        loop; pre-resolved once before fan-out — P3). Private ``seen_keys`` is the
        whole point: it prevents the false ``no_new_information`` cross-poisoning
        that a shared dedup set would cause when two sibling sub-questions retrieve
        an overlapping chunk (each must judge progress against *its own* prior
        retrievals, not a sibling's). Results are folded back with :meth:`merge`
        after the sibling loops join.
        """
        return AccumulatedContext(
            ontology=self.ontology,
            present_edge_types=list(self.present_edge_types),
            answer_language=self.answer_language,
        )

    def merge(self, other: AccumulatedContext) -> int:
        """Fold a forked sub-context back into this one, returning the NEW count (P2).

        Re-runs the same stable per-item dedup (:func:`_chunk_key` /
        :func:`_entity_key` / :func:`_row_key`) so a chunk/entity/row already
        present here — or already contributed by an earlier-merged sibling — is not
        double-counted, exactly as sequential accumulation would have produced.
        ``attempt_history`` and
        ``degraded_sources`` are concatenated (observability), and
        ``working_answers`` are merged by question (each sibling resolved a distinct
        sub-question, so entries append; a same-question entry is overwritten with
        the sibling's, matching :meth:`set_working_answer`'s last-writer rule). The
        shared ``ontology`` is unchanged (both already reference it).
        """
        new_count = 0
        for chunk in other.chunks:
            key = _chunk_key(chunk)
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)
            self.chunks.append(chunk)
            new_count += 1
        for entity in other.entities:
            if self._add_or_merge_entity(entity):
                new_count += 1
        for row in other.rows:
            key = _row_key(row)
            if key in self.seen_keys:
                continue
            self.seen_keys.add(key)
            self.rows.append(row)
            new_count += 1
        self.attempt_history.extend(other.attempt_history)
        self.degraded_sources.extend(other.degraded_sources)
        # Fold in the sibling's executed query artifacts (#1166), in order,
        # exactly as sequential accumulation would have — same append-only,
        # non-deduplicated treatment as degraded_sources.
        self.executed_queries.extend(other.executed_queries)
        # A sibling that hit the entity cap already recorded its own degraded source
        # (concatenated above); carry the count so the total stays accurate.
        self.dropped_entities += other.dropped_entities
        self.relationship_cap_reached = self.relationship_cap_reached or other.relationship_cap_reached
        for entry in other.working_answers:
            self.set_working_answer(entry.get("question", ""), entry.get("answer", ""))
        # Union discovered predicates (preserve order, drop dups a sibling already had).
        for edge in other.present_edge_types:
            if edge not in self.present_edge_types:
                self.present_edge_types.append(edge)
        return new_count

    def record_degraded(self, source: str, reason: str) -> None:
        """Record a degraded Tool source so it surfaces in the Tier3_Result.

        Appends a ``{"source", "reason"}`` record to :attr:`degraded_sources`
        (reason is typically ``"error"`` or ``"timeout"``, Req 9.1-9.2). Records
        accumulate — one per failure — and are never de-duplicated, so repeated
        failures of the same Tool are each represented.
        """
        self.degraded_sources.append({"source": source, "reason": reason})

    # ── carried reasoning state (improvement ideas 2 & 3) ──────────────

    def record_attempt(
        self, tool_name: str, inputs: dict | None, new_count: int, status: str, reasoning: str = ""
    ) -> None:
        """Append a compact record of one Reasoning_Step to :attr:`attempt_history`.

        Captures the tool, a summarized view of its inputs, how many genuinely new
        results it produced, its status (``"succeeded"``, ``"timeout"``, ``"error"``,
        ``"invalid_input"``, ``"unknown_tool"``), and the planner's own short
        ``reasoning`` for the step (E-cheap): carrying the *why* — not just the
        what+count — lets the next planner step build on its prior intent rather
        than re-deriving it from a lossy summary (idea 3/4). Empty reasoning is
        omitted so the record stays compact.
        """
        record: dict = {
            "tool": tool_name,
            "inputs": {k: _short_input(v) for k, v in (inputs or {}).items()},
            "new_count": new_count,
            "status": status,
        }
        if reasoning:
            record["reasoning"] = _short_input(reasoning)
        self.attempt_history.append(record)

    def set_working_answer(self, question: str, answer: str) -> None:
        """Store/refresh the decision agent's grounded best-guess for ``question`` (idea 2).

        Updates the existing entry for the sub-question when present, otherwise
        appends a new one. Empty/whitespace answers are ignored so a failed update
        never clobbers a prior good guess. The answer is truncated to a bound so it
        cannot blow the synthesis prompt.
        """
        ans = (answer or "").strip()
        if not ans:
            return
        ans = ans[:_MAX_WORKING_ANSWER_CHARS]
        for entry in self.working_answers:
            if entry.get("question") == question:
                entry["answer"] = ans
                return
        self.working_answers.append({"question": question, "answer": ans})

    @property
    def working_answer(self) -> str:
        """The combined grounded best-guess answer(s) for the synthesizer (idea 2).

        Empty when the agent produced no grounded guess. A single sub-question
        returns its answer directly; multiple sub-questions are labelled so the
        synthesizer can attribute each grounded finding.
        """
        answers = [e for e in self.working_answers if e.get("answer")]
        if not answers:
            return ""
        if len(answers) == 1:
            return answers[0]["answer"]
        return "\n\n".join(f"- For '{e['question']}': {e['answer']}" for e in answers)

    def recent_chunk_snippets(self, limit: int = 5, width: int = 280) -> list[str]:
        """Return short text snippets of the most-recently gathered chunks.

        Used to give the (otherwise count-only) planner summary some CONTENT to
        reason about — so the sufficiency/working-answer assessment and escalation
        decisions are based on what was actually retrieved, not just how much.
        """
        out: list[str] = []
        for chunk in self.chunks[-limit:]:
            text = getattr(chunk, "text", "") or ""
            if text:
                out.append(text[:width])
        return out

    def recent_chunk_refs(self, limit: int = 5, width: int = 200) -> list[tuple[str, str]]:
        """Return ``(chunk_id, snippet)`` pairs for the most-recently gathered chunks.

        Surfaces the chunk identifiers (not just text) so the planner can pass them
        to the chunk-window expansion tool to pull the physically adjacent chunks
        (improvement idea 6). Only chunks carrying a non-empty id are included.
        """
        out: list[tuple[str, str]] = []
        for chunk in self.chunks[-limit:]:
            cid = getattr(chunk, "chunk_id", "") or ""
            if not cid:
                continue
            text = (getattr(chunk, "text", "") or "")[:width]
            out.append((cid, text))
        return out

    def recent_row_snippets(self, limit: int = 5, width: int = 200) -> list[str]:
        """Return compact renderings of gathered structured rows (evenly sampled).

        Structured rows (NL->SQL / metric tools) were invisible to the planner's
        content view: the sufficiency/working-answer assessment saw only document
        chunks, so a sub-question fully answered by rows was assessed as "no data
        gathered" and the loop wandered or reported nothing. Surfacing a bounded
        row sample (same shape the synthesizer receives) closes that blind spot.
        """
        rows = self.rows
        if len(rows) > limit:
            # Evenly-spaced sample (first and last rows always included). A
            # tail-only sample misrepresents ordered results: on a date-ordered
            # multi-day table the last rows are all one date, so the assessor
            # concluded the other days were missing and re-ran the same tool.
            step = (len(rows) - 1) / (limit - 1) if limit > 1 else 0
            rows = [rows[round(i * step)] for i in range(limit)]
        out: list[str] = []
        for row in rows:
            items = getattr(row, "items", None)
            if not callable(items):
                continue
            rendered = str({k: str(v).replace("\n", " ")[:width] for k, v in row.items()})
            out.append(rendered[: width * 2])
        return out

    def _by_relevance(self, limit: int) -> list:
        """Return up to ``limit`` gathered chunks ranked by relevance (idea 8).

        Ranks all gathered chunks by ``relevance_score`` descending (stable, so
        ties keep arrival order), but GUARANTEES the single newest chunk is
        included — so the planner still sees the effect of its most recent action
        even when that chunk did not score highest. This replaces the pure-recency
        ``self.chunks[-limit:]`` view used by the recency helpers, so the capped
        context the planner/assessor sees is the most ON-TOPIC chunks rather than
        merely the most recent ones (the rest of the chunks remain in the context
        and still reach final synthesis — this only changes what is SURFACED).
        """
        chunks = list(self.chunks)
        if not chunks:
            return []
        ranked = sorted(chunks, key=lambda c: float(getattr(c, "relevance_score", 0.0) or 0.0), reverse=True)
        chosen: list = []
        seen: set[int] = set()
        # Newest first so the planner sees its last action, then fill by relevance.
        for chunk in [chunks[-1], *ranked]:
            marker = id(chunk)
            if marker in seen:
                continue
            seen.add(marker)
            chosen.append(chunk)
            if len(chosen) >= limit:
                break
        return chosen

    def relevant_chunk_refs(self, limit: int = 5, width: int = 200) -> list[tuple[str, str]]:
        """``(chunk_id, snippet)`` pairs for the most-RELEVANT gathered chunks (idea 8).

        Same shape as :meth:`recent_chunk_refs` but ranked by relevance score
        (with a guaranteed slot for the newest chunk) instead of recency.
        """
        out: list[tuple[str, str]] = []
        for chunk in self._by_relevance(limit):
            cid = getattr(chunk, "chunk_id", "") or ""
            if not cid:
                continue
            out.append((cid, (getattr(chunk, "text", "") or "")[:width]))
        return out

    def relevant_chunk_snippets(self, limit: int = 5, width: int = 280) -> list[str]:
        """Text snippets of the most-RELEVANT gathered chunks (idea 8).

        Relevance-ranked counterpart of :meth:`recent_chunk_snippets`.
        """
        out: list[str] = []
        for chunk in self._by_relevance(limit):
            text = getattr(chunk, "text", "") or ""
            if text:
                out.append(text[:width])
        return out
