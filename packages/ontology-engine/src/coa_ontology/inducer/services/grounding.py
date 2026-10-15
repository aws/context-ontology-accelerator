# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Centralized grounding service.

Layered pipeline:
  1. Exact-name match (normalized label equality → score 1.0)
  2. Embedding recall (top-K via vector search)
  3. LLM rerank (discriminative: pick best match or abstain)
  4. Score-thresholded tier classification (exact / high_confidence / ambiguous / novel)

Modes (controlled by ``grounding_mode`` param):
  - NONE:     skip grounding entirely, all concepts → novel
  - STANDARD: layers 1 + 2 only (deterministic, no LLM, cheap)
  - ENHANCED: full pipeline including LLM rerank (default)
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from threading import Lock

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import async_boto_config
from coa_common.bedrock import extract_text_blocks
from coa_common.bedrock_metrics import CostTracker

from coa_ontology.inducer.schemas import ConceptMatch, MatchCandidate
from coa_ontology.inducer.services.unicode_normalization import (
    filter_letters_marks_numbers,
    simple_case_identity,
    strict_percent_decode,
    width_only_nfkc,
)

log = logging.getLogger(__name__)

GROUNDING_MODES = ("NONE", "STANDARD", "ENHANCED")


class GroundingRerankError(Exception):
    """An LLM rerank call failed for an infrastructure reason, not an abstention.

    Raised when the Bedrock ``converse`` call or its response parsing fails
    (``ClientError``/``BotoCoreError`` after botocore's bounded retries, a
    missing text block, unparseable non-JSON output, or a truncated/blocked
    ``stopReason``). This is deliberately distinct from a genuine abstention —
    a parseable ``{"choice": "NONE"}`` meaning the LLM *considered the
    candidates and declined* (a real "novel"). Conflating the two is the defect
    behind github.com/aws/context-ontology-accelerator issue 59: an
    infrastructure failure silently classified every table ``novel``. The
    strategy's batch loop re-raises this (rather than degrading to all-novel) so
    the induction job fails loud instead of returning a wrong-but-plausible
    all-novel ontology.
    """


class GroundingLookupError(RuntimeError):
    """The grounding candidate lookup is incomplete, so no decision can be made.

    Raised when the lexical metadata listing fails or a lookup exceeds a declared
    bound. Without the full candidate set, "no candidate matched" and "the
    candidates were never read" look the same, so a table or class that should
    ground to an existing class would come out novel (a duplicate class with no
    link to the existing one). Both induction callers re-raise this ahead of
    their per-batch / per-class fail-open handlers, the same way they treat
    ``GroundingRerankError``, so the job fails instead of persisting that
    decision.
    """


class GroundingRecallLimitError(GroundingLookupError):
    """Grounding metadata exceeded a declared bound.

    An incomplete lexical index can silently classify a real class as novel, so
    pool, row, and byte overflows fail the induction instead of returning a
    partial candidate set.
    """


# Connection-pool cap for the shared bedrock-runtime client. The rerank
# ThreadPool (pipeline.py, _grounding_workers default 24) has ALL threads share
# this one lazily-built client; botocore's default max_pool_connections=10
# throttles a multi-threaded pool ("Connection pool is full, discarding
# connection"). Sized above the worker pool (24) so no rerank thread starves on
# a connection.
_BEDROCK_MAX_POOL_CONNECTIONS = 32

# The reranker's prompt lists at most this many recall candidates. Was a
# hardcoded 8, too thin against a large grounding pool (e.g. 342 classes): a
# correct class recalled at rank 9+ never reached the LLM, so no amount of
# reranking could recover it (github.com/aws/context-ontology-accelerator
# issue 72). Bounded, not unbounded, because the reranker runs per-table across
# _grounding_workers (default 24) sharing one Bedrock client — a large window
# multiplies prompt tokens and latency for little precision gain past the top
# candidates. Lexically-recalled candidates are pinned in ADDITION to this
# window (see _llm_rerank), so raising recall depth without raising this cap
# still lets a token-overlap hit reach the LLM.
_RERANK_CANDIDATE_WINDOW = 20

# When the top recall candidates' embedding scores span less than this, the
# retriever did not discriminate — the ranking is near-arbitrary (issue 72
# observed a 0.04 spread across ten unrelated classes). A pick from such a
# distribution is capped at "ambiguous" so it routes to steward review instead
# of being auto-accepted on a similarity that carries no signal.
_FLAT_RECALL_SPREAD = 0.05

# A recalled class must share at least one token this long with the subject
# name to be a lexical candidate. Two-plus chars drops noise (single letters,
# stray digits) while keeping the real signal ("area", "tuf" in issue 72).
_MIN_TOKEN_LEN = 2

# Both dense and lexical recall loop once per selected ontology. The structured
# pipeline already documents a practical maximum of ten; enforce it here.
_LEXICAL_INDEX_MAX_ONTOLOGIES = 10

# The previous backends disagreed: Neptune returned 100 rows while OpenSearch
# returned up to 10,000 full embedding documents. Reserve one row within that
# backend window for overflow detection, so truncation fails loud instead of
# becoming a backend-dependent false-novel result.
_LEXICAL_INDEX_MAX_ENTITIES = 9_999
_LEXICAL_INDEX_MAX_TOTAL_UTF8_BYTES = 4 * 1024 * 1024
_LEXICAL_INDEX_MAX_ID_UTF8_BYTES = 2_048
_LEXICAL_INDEX_MAX_DEFINITION_UTF8_BYTES = 512

_RERANK_SYSTEM = (
    "You are an ontology grounding expert. Given a source database table "
    "(with its columns and description) and a list of candidate ontology "
    "classes (each with its formal definition), decide which candidate "
    "the table should be grounded to.\n\n"
    "Output EXACTLY one JSON object with these fields:\n"
    '  {"choice": "<localName or NONE>", '
    '"relationship": "<exactMatch|closeMatch|broadMatch|'
    'narrowMatch|relatedMatch|none>", '
    '"confidence": <0.0-1.0>, "reason": "<one sentence>"}\n\n'
    "Rules:\n"
    "- If a candidate's name matches or closely describes the table's "
    "purpose, PICK IT. Prefer the candidate with a definition that aligns "
    "with the table's columns and description.\n"
    "- When multiple candidates share the same name (e.g. from different "
    "ontologies), pick the one whose definition best matches the table's "
    "domain. If one has a definition and the other doesn't, prefer the "
    "one with a definition.\n"
    "- Pick NONE only if no candidate genuinely represents the same or "
    "closely related concept — i.e., the table is a truly novel domain "
    "concept not covered by any candidate.\n"
    "- exactMatch: table IS this concept (same meaning, same scope)\n"
    "- closeMatch: nearly interchangeable, minor scope difference\n"
    "- broadMatch: candidate is a broader/parent concept\n"
    "- narrowMatch: candidate is a narrower/child concept\n"
    "- relatedMatch: conceptually associated but distinct\n"
    "- Output valid JSON only. No markdown, no explanation outside the JSON."
)

# Class-flavored variant of _RERANK_SYSTEM used when grounding an induced
# ontology CLASS (e.g. from the unstructured / lexical-graph pipeline) rather
# than a database table. The recall/exact-match internals are identical; only
# the LLM framing changes so the reranker judges class-vs-class alignment
# instead of table-vs-class. Keeping the JSON contract byte-identical means the
# same parser handles both.
_RERANK_SYSTEM_CLASS = (
    "You are an ontology grounding expert. Given a source ontology class "
    "(with its label and definition) and a list of candidate ontology "
    "classes (each with its formal definition), decide which candidate "
    "the source class should be grounded to.\n\n"
    "Output EXACTLY one JSON object with these fields:\n"
    '  {"choice": "<localName or NONE>", '
    '"relationship": "<exactMatch|closeMatch|broadMatch|'
    'narrowMatch|relatedMatch|none>", '
    '"confidence": <0.0-1.0>, "reason": "<one sentence>"}\n\n'
    "Rules:\n"
    "- If a candidate's name matches or closely describes the source class's "
    "meaning, PICK IT. Prefer the candidate whose definition aligns with the "
    "source class's definition.\n"
    "- When multiple candidates share the same name (e.g. from different "
    "ontologies), pick the one whose definition best matches the source "
    "class's domain. If one has a definition and the other doesn't, prefer "
    "the one with a definition.\n"
    "- Pick NONE only if no candidate genuinely represents the same or "
    "closely related concept — i.e., the source class is a truly novel domain "
    "concept not covered by any candidate.\n"
    "- exactMatch: the source class IS this concept (same meaning, same scope)\n"
    "- closeMatch: nearly interchangeable, minor scope difference\n"
    "- broadMatch: candidate is a broader/parent concept\n"
    "- narrowMatch: candidate is a narrower/child concept\n"
    "- relatedMatch: conceptually associated but distinct\n"
    "- Output valid JSON only. No markdown, no explanation outside the JSON."
)


@dataclass
class GroundingCandidate:
    """A candidate foundational entity for grounding, with lexical and rerank scores."""

    entity_uri: str
    ontology_id: str | None
    label: str
    definition: str
    lexical_sim: float
    rerank_score: float | None = None
    rerank_relationship: str | None = None
    rerank_reason: str | None = None
    # True when this candidate came from the token-overlap retriever rather than
    # dense embedding recall. It carries no comparable cosine score (lexical_sim
    # stays 0.0), so it is excluded from the flat-spread measure and pinned into
    # the reranker window rather than competing on cosine rank.
    from_lexical: bool = False


@dataclass(frozen=True)
class _LexicalIndexEntry:
    """One immutable, metadata-only lexical index row."""

    entity_uri: str
    ontology_id: str
    label: str
    definition: str
    tokens: frozenset[str]


@dataclass
class GroundingResult:
    """Result of grounding one source column: the chosen match, tier, and rationale."""

    source_table: str
    source_column: str
    candidates: list[GroundingCandidate]
    chosen: GroundingCandidate | None
    match_type: str  # exact | high_confidence | ambiguous | novel
    relationship: str | None  # SKOS relationship from reranker
    confidence: float | None  # reranker confidence or cosine sim
    reason: str | None  # reranker's justification
    mode: str  # which mode produced this result


def _normalize(s: str) -> str:
    """Build a comparison key without merging distinct Unicode words.

    Separators and punctuation are removed, width forms are unified, and Latin
    diacritics are folded for the existing fuzzy-name behavior. Marks belonging
    to non-Latin scripts remain significant, and symbols remain literal rather
    than compatibility-expanding into ASCII text. Ordinary lowercase plus an
    explicit final-sigma mapping avoids compatibility case-fold collisions.
    """
    width_normalized = width_only_nfkc(s)
    decomposed = unicodedata.normalize("NFD", width_normalized)
    kept: list[str] = []
    has_base = False
    base_is_ascii = False
    for character in decomposed:
        category = unicodedata.category(character)[0]
        if category in "LNS":
            kept.append(character)
            has_base = True
            base_is_ascii = character.isascii() and category in "LN"
            continue
        if category == "M":
            # Generic Latin diacritics remain foldable after an ASCII base, but
            # script-specific marks are lexical identity. U+0345, for example,
            # is COMBINING GREEK YPOGEGRAMMENI and must not collapse ``aͅ`` to
            # plain ``a`` merely because its synthetic test/base is ASCII.
            is_script_specific = "GREEK" in unicodedata.name(character, "")
            if has_base and (not base_is_ascii or is_script_specific):
                kept.append(character)
            continue
        if unicodedata.category(character) == "Cf" and has_base:
            # Preserve attached format characters until the shared helper can
            # allow only ZWJ/ZWNJ and Unicode TAG sequences. Unsupported format
            # controls are still discarded there and reset attachment state.
            kept.append(character)
            continue
        has_base = False
        base_is_ascii = False
    folded = simple_case_identity("".join(kept))
    filtered = filter_letters_marks_numbers(folded, keep_symbols=True)
    return unicodedata.normalize("NFC", filtered)


def _tokenize(name: str) -> set[str]:
    """Unicode token set from a name, splitting camelCase and separators.

    ``apaAreaGross`` → ``{"apa", "area", "gross"}``; ``tuf_petreg_licence`` →
    ``{"tuf", "petreg", "licence"}``; ``顧客`` → ``{"顧客"}``. Short ASCII
    tokens are dropped, while a one-character non-ASCII identifier remains
    eligible because it can be a complete word in scripts without Latin-style
    word boundaries. Used only by the lexical retriever — dense recall is
    unaffected.
    """
    characters = list(width_only_nfkc(name))
    with_camel_boundaries: list[str] = []
    for index, character in enumerate(characters):
        previous = characters[index - 1] if index > 0 else ""
        following = characters[index + 1] if index + 1 < len(characters) else ""
        if character.isupper() and (
            previous.islower() or previous.isdigit() or (previous.isupper() and following.islower())
        ):
            with_camel_boundaries.append(" ")
        with_camel_boundaries.append(character)

    tokens: set[str] = set()
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        token = unicodedata.normalize(
            "NFC",
            filter_letters_marks_numbers(simple_case_identity("".join(current))),
        )
        current.clear()
        if not token:
            return
        has_non_ascii_letter = any(
            not character.isascii() and unicodedata.category(character).startswith("L") for character in token
        )
        if len(token) >= _MIN_TOKEN_LEN or has_non_ascii_letter:
            tokens.add(token)

    for character in with_camel_boundaries:
        category = unicodedata.category(character)[0]
        if category in "LN" or (category == "M" and current):
            current.append(character)
        else:
            flush()
    flush()
    return tokens


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Return a valid UTF-8 prefix no larger than ``max_bytes``."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _local_name(uri: str) -> str:
    """Extract and strictly percent-decode the local name from an IRI."""
    local_name = uri.rsplit("#", 1)[1] if "#" in uri else uri.rsplit("/", 1)[-1]
    return strict_percent_decode(local_name)


def _recall_spread(candidates: list[GroundingCandidate]) -> float | None:
    """Spread (max − min) of the dense recall candidates' embedding scores.

    ``None`` when fewer than two dense candidates exist (no dispersion to
    measure). Lexically-recalled candidates carry no comparable cosine score
    (``from_lexical``) and are excluded, so their synthetic 0.0 cannot fake a
    wide spread.
    """
    sims = [c.lexical_sim for c in candidates if not c.from_lexical]
    if len(sims) < 2:
        return None
    return max(sims) - min(sims)


def classify_score_tier(
    score: float | None,
    *,
    has_rerank: bool = True,
    confidence_threshold: float = 0.80,
    recall_spread: float | None = None,
) -> str:
    """Classify a single match score into a grounding tier.

    Tiering is primarily score-thresholded: the reranked ladder
    (``has_rerank=True``, the default, matching how production induction scores
    LLM-reranked candidates) and the STANDARD embedding ladder each map an
    absolute score to ``exact``/``high_confidence``/``ambiguous``/``novel``.
    ``None`` (no candidate) is ``novel``.

    ``recall_spread``, when provided, adds a dispersion guard: if the dense
    recall did not discriminate (spread < ``_FLAT_RECALL_SPREAD``), a result
    that would otherwise be ``exact``/``high_confidence`` is capped at
    ``ambiguous`` and routed to review — a high score off an undiscriminating
    recall is not trustworthy (issue 72). It never rescues a ``novel`` and
    never blocks a genuine ``ambiguous``, so the existing calibration bands are
    unchanged for well-separated recalls.
    """
    if score is None:
        return "novel"
    if has_rerank:
        if score >= 0.85:
            tier = "exact"
        elif score >= 0.65:
            tier = "high_confidence"
        elif score >= 0.40:
            tier = "ambiguous"
        else:
            tier = "novel"
    # STANDARD mode: use embedding scores with original thresholds
    elif score >= 0.95:
        tier = "exact"
    elif score >= confidence_threshold:
        tier = "high_confidence"
    elif score >= 0.50:
        tier = "ambiguous"
    else:
        tier = "novel"
    if recall_spread is not None and recall_spread < _FLAT_RECALL_SPREAD and tier in ("exact", "high_confidence"):
        return "ambiguous"
    return tier


class GroundingService:
    """Shared grounding service used by all induction strategies."""

    def __init__(
        self,
        ontology_catalog,
        embedding_generator,
        llm_region: str = "us-east-1",
        llm_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        namespace: str | None = None,
        cost_tracker: CostTracker | None = None,
    ):
        """Configure the catalog, embedding, and LLM dependencies for grounding.

        Args:
            ontology_catalog: Client used to recall candidate foundational entities.
            embedding_generator: Client used to embed source concepts.
            llm_region: AWS region for the Bedrock rerank client.
            llm_model_id: Bedrock model id used to rerank candidates.
            namespace: Namespace whose per-namespace recall index to query; None uses
                the default-namespace index.
            cost_tracker: Optional per-job Bedrock usage tracker for rerank calls.
        """
        self.oc = ontology_catalog
        self.eg = embedding_generator
        self._llm_region = llm_region
        self._llm_model_id = llm_model_id
        self._bedrock = None
        # Optional per-job Bedrock usage tracker. When present, every rerank
        # converse call records its token usage under the "rerank" stage.
        self._cost_tracker = cost_tracker
        # Namespace whose per-namespace OpenSearch index the recall must
        # query. When None the catalog client omits ?namespace= and the
        # endpoint falls back to the default-namespace (bare) index — correct
        # ONLY for default-namespace deployments. Pass the real namespace on
        # multi-namespace deployments or recall hits the empty bare index.
        self._namespace = namespace
        # One service instance is shared by the structured worker pool and by
        # one unstructured induction. Build each pool's lexical index once.
        self._lexical_index_cache: dict[tuple[str, ...], tuple[_LexicalIndexEntry, ...]] = {}
        self._lexical_index_lock = Lock()

    @property
    def bedrock(self):
        """Lazily create and cache the Bedrock runtime client for rerank calls."""
        if self._bedrock is None:
            self._bedrock = boto3.client(
                "bedrock-runtime",
                region_name=self._llm_region,
                config=async_boto_config(read_timeout=120, max_pool_connections=_BEDROCK_MAX_POOL_CONNECTIONS),
            )
        return self._bedrock

    def ground_class(
        self,
        class_label: str,
        class_description: str,
        concept_vector: list[float],
        model_id: str,
        confidence_threshold: float = 0.80,
        grounding_mode: str = "ENHANCED",
        ontology_ids: list[str] | None = None,
        top_k: int = 30,
        exclude_entity_uri: str | None = None,
        rerank_max_tokens: int = 1000,
    ) -> GroundingResult:
        """Ground an induced ontology CLASS against loaded ontologies.

        Thin wrapper over :meth:`ground_table` that shares the entire
        recall → exact-name → LLM-rerank pipeline, but frames the LLM
        reranker as judging class-vs-class alignment (via
        :data:`_RERANK_SYSTEM_CLASS`) instead of table-vs-class. A class
        has no columns, so ``columns=[]`` and the rerank prompt omits the
        column section. Used by the unstructured (lexical-graph) induction
        path so document-derived classes ground to foundational ontologies
        exactly the way structured tables do.

        The returned :class:`GroundingResult` carries the class label in
        ``source_table`` (the field is strategy-agnostic despite its name);
        callers emit ``skos:*Match`` / ``rdfs:subClassOf`` from
        ``result.chosen`` + ``result.relationship``.
        """
        return self.ground_table(
            table_name=class_label,
            table_description=class_description,
            columns=[],
            concept_vector=concept_vector,
            model_id=model_id,
            confidence_threshold=confidence_threshold,
            grounding_mode=grounding_mode,
            ontology_ids=ontology_ids,
            top_k=top_k,
            exclude_entity_uri=exclude_entity_uri,
            rerank_max_tokens=rerank_max_tokens,
            _rerank_system=_RERANK_SYSTEM_CLASS,
            _subject_kind="class",
        )

    def ground_table(
        self,
        table_name: str,
        table_description: str,
        columns: list[dict],
        concept_vector: list[float],
        model_id: str,
        confidence_threshold: float = 0.80,
        grounding_mode: str = "ENHANCED",
        ontology_ids: list[str] | None = None,
        top_k: int = 30,
        exclude_entity_uri: str | None = None,
        rerank_max_tokens: int = 1000,
        *,
        _rerank_system: str = _RERANK_SYSTEM,
        _subject_kind: str = "table",
    ) -> GroundingResult:
        """Ground a source table against the provided ontology pool.

        Layered pipeline: embedding recall (top-K by cosine similarity) →
        exact-name shortcut → LLM rerank of the survivors.

        Args:
            table_name: Name of the source table (or class, for ``ground_class``).
            table_description: Free-text description used in the rerank prompt.
            columns: Column metadata dicts; included in the rerank context.
            concept_vector: Pre-computed embedding of the subject concept.
            model_id: Embedding model id used for recall.
            confidence_threshold: Minimum rerank confidence to accept a match.
            grounding_mode: ``ENHANCED``/``STANDARD`` run the pipeline;
                ``NONE`` short-circuits to a novel result (no grounding).
            ontology_ids: The resolved grounding pool — grounding is scoped to
                EXACTLY these ontologies. An empty pool (``[]`` or ``None``)
                means "no grounding scope": recall returns no candidates and the
                subject is classified novel. There is no unscoped/global recall —
                an empty pool never falls back to a namespace-wide search (which
                would match loaded-but-unselected ontologies; the grounding
                opt-out bug).
            top_k: Number of recall candidates to fetch per ontology.
            exclude_entity_uri: IRI to drop from recall so the subject can't
                ground to itself (incremental induction grounds against a pool
                that includes its own ontology).
            rerank_max_tokens: Output-token cap for the ENHANCED-mode LLM rerank
                call; reasoning models spend part of it on discarded thinking.

        Returns:
            A ``GroundingResult`` with the chosen match (or ``match_type
            == "novel"`` when nothing grounds).
        """
        log.info(
            "ground_%s: name=%s mode=%s ontology_ids=%s vector_len=%d model=%s",
            _subject_kind,
            table_name,
            grounding_mode,
            ontology_ids[:2] if ontology_ids else None,
            len(concept_vector),
            model_id,
        )
        if grounding_mode == "NONE":
            return GroundingResult(
                source_table=table_name,
                source_column="",
                candidates=[],
                chosen=None,
                match_type="novel",
                relationship=None,
                confidence=None,
                reason="Grounding disabled (mode=NONE)",
                mode="NONE",
            )

        # Layer 1: Embedding recall (top-K candidates). When grounding against a
        # pool that includes the subject's OWN ontology (e.g. incremental
        # induction grounds new classes against the namespace's already-accepted
        # ontologies), the subject can recall — and then "ground to" — ITSELF.
        # Exclude its own IRI from the candidate set. Fetch one extra so removing
        # self doesn't shrink the effective top-K.
        recall_k = top_k + 1 if exclude_entity_uri else top_k
        raw_candidates = self._recall(concept_vector, model_id, ontology_ids, recall_k, subject_name=table_name)
        if exclude_entity_uri:
            _self = exclude_entity_uri.rstrip("/")
            raw_candidates = [c for c in raw_candidates if c.entity_uri.rstrip("/") != _self][:top_k]
        if not raw_candidates:
            return GroundingResult(
                source_table=table_name,
                source_column="",
                candidates=[],
                chosen=None,
                match_type="novel",
                relationship=None,
                confidence=None,
                reason="No candidates returned from embedding search",
                mode=grounding_mode,
            )

        # Layer 2: Exact-name match check.
        # In STANDARD mode (no LLM): auto-ground a unique exact name. For
        # multiple exact names, embedding score may disambiguate only when the
        # maximum itself is unique; equal maxima must not pick recall order.
        # In ENHANCED mode: ALWAYS defer to LLM for domain verification — a name match
        # is NOT sufficient (e.g. Dublin Core "Policy" ≠ insurance "policy").
        exact_matches = self._exact_name_matches(table_name, raw_candidates)
        if exact_matches and grounding_mode == "STANDARD":
            if len(exact_matches) == 1:
                exact = exact_matches[0]
                return GroundingResult(
                    source_table=table_name,
                    source_column="",
                    candidates=raw_candidates,
                    chosen=exact,
                    match_type="exact",
                    relationship="exactMatch",
                    confidence=1.0,
                    reason=f"Exact name match: {table_name} == {exact.label}",
                    mode="STANDARD",
                )
            else:
                best_score = max(candidate.lexical_sim for candidate in exact_matches)
                best_matches = [candidate for candidate in exact_matches if candidate.lexical_sim == best_score]
                if len(best_matches) != 1:
                    return GroundingResult(
                        source_table=table_name,
                        source_column="",
                        candidates=raw_candidates,
                        chosen=None,
                        match_type="novel",
                        relationship=None,
                        confidence=None,
                        reason=(f"Multiple exact-name matches ({len(exact_matches)}) share the top embedding score"),
                        mode="STANDARD",
                    )
                best = best_matches[0]
                return GroundingResult(
                    source_table=table_name,
                    source_column="",
                    candidates=raw_candidates,
                    chosen=best,
                    match_type="high_confidence",
                    relationship="closeMatch",
                    confidence=best.lexical_sim,
                    reason=(
                        f"Multiple exact-name matches ({len(exact_matches)}); "
                        "picked unique best embedding score (no LLM in STANDARD mode)"
                    ),
                    mode="STANDARD",
                )
        if exact_matches and grounding_mode == "ENHANCED":
            log.info(
                "ground_table: %d exact-name match(es) for '%s' — deferring to LLM for domain verification",
                len(exact_matches),
                table_name,
            )
        # In ENHANCED mode: fall through to LLM rerank below (always)

        if grounding_mode == "STANDARD":
            # No LLM — classify purely on embedding score
            best = raw_candidates[0]
            match_type = classify_score_tier(
                best.lexical_sim,
                has_rerank=False,
                confidence_threshold=confidence_threshold,
                recall_spread=_recall_spread(raw_candidates),
            )
            return GroundingResult(
                source_table=table_name,
                source_column="",
                candidates=raw_candidates,
                chosen=best if match_type != "novel" else None,
                match_type=match_type,
                relationship="closeMatch"
                if match_type in ("exact", "high_confidence")
                else ("relatedMatch" if match_type == "ambiguous" else None),
                confidence=best.lexical_sim,
                reason=None,
                mode="STANDARD",
            )

        # Layer 3: LLM rerank (ENHANCED mode)
        rerank_result = self._llm_rerank(
            table_name,
            table_description,
            columns,
            raw_candidates,
            max_tokens=rerank_max_tokens,
            rerank_system=_rerank_system,
            subject_kind=_subject_kind,
        )

        if rerank_result.get("choice") == "NONE":
            # LLM genuinely abstained — novel. (A failed/unparseable rerank
            # raises GroundingRerankError in _llm_rerank; it never returns here.)
            return GroundingResult(
                source_table=table_name,
                source_column="",
                candidates=raw_candidates,
                chosen=None,
                match_type="novel",
                relationship=None,
                confidence=None,
                reason=rerank_result.get("reason", "LLM abstained"),
                mode="ENHANCED",
            )

        # Resolve the model's returned local name without letting a lossy
        # comparison key choose the wrong candidate. Prefer the exact label/local
        # spelling; use normalization only when it identifies one candidate.
        choice_name = rerank_result["choice"]
        exact_choices = [
            candidate
            for candidate in raw_candidates
            if choice_name in (candidate.label, _local_name(candidate.entity_uri))
        ]
        normalized_choices: list[GroundingCandidate] = []
        if len(exact_choices) == 1:
            chosen = exact_choices[0]
        elif exact_choices:
            # The reranker returns only a local name, so duplicate exact names
            # from different ontologies are not distinguishable. Never let
            # recall ordering silently choose one ontology's concept.
            chosen = None
        else:
            norm_choice = _normalize(choice_name)
            normalized_choices = (
                [
                    candidate
                    for candidate in raw_candidates
                    if _normalize(candidate.label) == norm_choice
                    or _normalize(_local_name(candidate.entity_uri)) == norm_choice
                ]
                if norm_choice
                else []
            )
            chosen = normalized_choices[0] if len(normalized_choices) == 1 else None
        if not chosen:
            # Preserve the established missing-choice diagnostic while failing
            # closed with a distinct message when the name is ambiguous.
            ambiguous = bool(exact_choices or normalized_choices)
            conflicting_candidates = sorted(
                {candidate.entity_uri for candidate in (exact_choices or normalized_choices)}
            )
            displayed_candidates = [uri if len(uri) <= 160 else f"{uri[:157]}..." for uri in conflicting_candidates[:5]]
            candidate_context = f"; conflicting candidate URIs: {displayed_candidates}" if ambiguous else ""
            reason = (
                f"LLM choice '{choice_name}' did not uniquely identify a candidate{candidate_context}"
                if ambiguous
                else f"LLM choice '{choice_name}' not in candidates"
            )
            log.warning("%s for table '%s'", reason, table_name)
            return GroundingResult(
                source_table=table_name,
                source_column="",
                candidates=raw_candidates,
                chosen=None,
                match_type="novel",
                relationship=None,
                confidence=None,
                reason=reason,
                mode="ENHANCED",
            )

        try:
            llm_confidence = float(rerank_result.get("confidence", 0.5))
        except (TypeError, ValueError):
            llm_confidence = 0.5
        relationship = rerank_result.get("relationship", "relatedMatch")
        match_type = classify_score_tier(
            llm_confidence,
            has_rerank=True,
            confidence_threshold=confidence_threshold,
            recall_spread=_recall_spread(raw_candidates),
        )

        chosen.rerank_score = llm_confidence
        chosen.rerank_relationship = relationship
        chosen.rerank_reason = rerank_result.get("reason")

        return GroundingResult(
            source_table=table_name,
            source_column="",
            candidates=raw_candidates,
            chosen=chosen,
            match_type=match_type,
            relationship=relationship,
            confidence=llm_confidence,
            reason=rerank_result.get("reason"),
            mode="ENHANCED",
        )

    def _recall(
        self,
        vector: list[float],
        model_id: str,
        ontology_ids: list[str] | None,
        top_k: int,
        subject_name: str | None = None,
    ) -> list[GroundingCandidate]:
        """Embedding-based recall: top-K candidates by cosine similarity.

        Scope semantics of ``ontology_ids`` (the resolved grounding pool):
          - a non-empty list → search each ontology separately and merge/dedup;
          - an empty pool (``[]`` or ``None``) → return no candidates. The
            caller resolved a grounding pool and it is empty (the user selected
            no ontologies and the namespace has no accepted induced ontologies).
            We do NOT fall back to a namespace-wide search — that fallback would
            match loaded-but-unselected ontologies, defeating the user's choice
            not to ground against them (the grounding opt-out bug). Grounding is
            always scoped to exactly the pool the caller provides; there is no
            unscoped/global recall.
        """
        if not ontology_ids:
            log.info("grounding_recall: empty grounding scope — returning no candidates (all-novel)")
            return []
        ids_to_search = list(dict.fromkeys(ontology_ids))
        if len(ids_to_search) > _LEXICAL_INDEX_MAX_ONTOLOGIES:
            raise GroundingRecallLimitError(
                f"grounding ontology pool exceeds {_LEXICAL_INDEX_MAX_ONTOLOGIES} distinct ontologies"
            )

        log.info(
            "grounding_recall: vector_len=%d model_id=%s ontology_ids=%s top_k=%d",
            len(vector),
            model_id,
            ids_to_search[:3],
            top_k,
        )

        all_results: list[dict] = []
        for oid in ids_to_search:
            try:
                hits = self.oc.search_embeddings(
                    vector=vector,
                    embedding_type="lexical",
                    model_id=model_id,
                    entity_type="class",
                    ontology_id=oid,
                    top_k=top_k,
                    namespace=self._namespace,
                )
                log.info(
                    "grounding_recall: ontology_id=%s returned %d hits, first=%s",
                    oid,
                    len(hits),
                    hits[0].get("entity_uri", "")[:60] if hits else "EMPTY",
                )
                if hits:
                    sample = hits[0]
                    log.info(
                        "grounding_recall: sample_hit keys=%s score=%s vector_len=%d",
                        list(sample.keys())[:8],
                        sample.get("score"),
                        len(sample.get("vector") or []),
                    )
                all_results.extend(hits)
            except Exception as e:
                # Re-raise so an incomplete recall fails the job rather than
                # dropping this ontology's candidates and misclassifying the
                # subject 'novel'.
                log.error("grounding_recall: FAILED ontology_id=%s error=%s", oid, e, exc_info=True)
                raise

        log.info("grounding_recall: total_results=%d across %d ontology searches", len(all_results), len(ids_to_search))

        if not all_results:
            return []

        import math

        def _cosine(a, b):
            n = min(len(a), len(b))
            if n == 0:
                return 0.0
            dot = sum(a[i] * b[i] for i in range(n))
            na = math.sqrt(sum(a[i] ** 2 for i in range(n)))
            nb = math.sqrt(sum(b[i] ** 2 for i in range(n)))
            return dot / (na * nb) if na and nb else 0.0

        # Deduplicate: first by normalized URI, then by (local_name, ontology_id)
        # to collapse duplicates from http/https variants or multiple embedding runs.
        seen_uri: dict[str, dict] = {}
        for r in all_results:
            uri = r.get("entity_uri", "").rstrip("/")
            score = r.get("score") or 0.0
            if not uri:
                continue
            if uri in seen_uri:
                if score > (seen_uri[uri].get("score") or 0):
                    seen_uri[uri] = r
            else:
                seen_uri[uri] = r
        # Secondary dedup: same local name + same ontology → keep highest score
        seen_name: dict[tuple[str, str], dict] = {}
        for r in seen_uri.values():
            uri = r.get("entity_uri", "")
            local = _local_name(uri)
            oid = r.get("ontology_id") or ""
            # Candidate identity must be lossless: dedup canonical/width/case
            # variants, but preserve punctuation, accents, script marks, and
            # join controls. Reusing fuzzy ``_normalize`` here erased distinct
            # candidates (for example ``Hohe``/``Höhe`` or ``A-B``/``AB``)
            # before the exact-spelling resolver had a chance to choose.
            candidate_identity = unicodedata.normalize(
                "NFC",
                simple_case_identity(width_only_nfkc(local)),
            )
            key = (candidate_identity or uri, oid)
            existing = seen_name.get(key)
            if existing is None or (r.get("score") or 0) > (existing.get("score") or 0):
                seen_name[key] = r
        deduped = list(seen_name.values())

        candidates = []
        for r in deduped:
            uri = r.get("entity_uri", "")
            if not uri:
                continue
            # Prefer the pre-computed score from the vector store (kNN);
            # fall back to local cosine if the raw vector is available.
            sim = r.get("score") or 0.0
            if not sim and r.get("vector"):
                sim = _cosine(vector, r["vector"])
            if sim <= 0:
                continue
            # Use the 'text' field from AOSS as the definition — it contains
            # the label + definition text that was embedded. This is more
            # reliable than the Neptune graph lookup (which fails for classes
            # ingested before the skos:definition fix).
            raw_text = r.get("text") or ""
            # Strip the label prefix from the text to get just the definition.
            # Handle both exact label ("Organization ...") and split CamelCase
            # ("Data Catalog ...") since _text_for uses _split_camel at embed time.
            label = _local_name(uri)
            split_label = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", label)
            defn = raw_text
            if raw_text.lower().startswith(label.lower()):
                defn = raw_text[len(label) :].strip()
            elif raw_text.lower().startswith(split_label.lower()):
                defn = raw_text[len(split_label) :].strip()

            candidates.append(
                GroundingCandidate(
                    entity_uri=uri,
                    ontology_id=r.get("ontology_id"),
                    label=label,
                    definition=defn,
                    lexical_sim=sim,
                )
            )
        candidates.sort(key=lambda c: -c.lexical_sim)
        top = candidates[:top_k]

        # Additive lexical recall: token-overlap over class local-names surfaces
        # classes sharing surface tokens with the subject that dense embedding
        # ranked out (issue 72 — MainArea↔apaAreaGross share "area";
        # TUFacility↔tuf_petreg_licence share "tuf"). Deduped against the dense
        # set; pinned through the reranker window in _llm_rerank so they reach
        # the LLM regardless of dense rank.
        lexical = self._lexical_recall(ids_to_search, subject_name, top_k)
        if lexical:
            have = {c.entity_uri.rstrip("/") for c in top}
            top = top + [c for c in lexical if c.entity_uri.rstrip("/") not in have]

        return top

    def _lexical_recall(
        self, ontology_ids: list[str], subject_name: str | None, top_k: int
    ) -> list[GroundingCandidate]:
        """Token-overlap retrieval over class local-names, additive to dense recall.

        Complements dense embedding recall for the case it is blind to: a class
        whose name shares surface tokens with the subject but sits in a flat,
        undiscriminating region of the embedding space (issue 72). Scores each
        class by Jaccard token overlap between its local-name and the subject
        name, returning the top ``top_k`` with a positive overlap.

        Portable across both vector backends: it builds one bounded,
        metadata-only index per ontology pool and scores that immutable index
        in-process. A catalog without the listing capability remains a no-op.
        A supported listing that fails or exceeds a declared bound fails loud;
        returning a partial index would create backend-dependent false-novel
        results.
        """
        if not subject_name:
            return []
        lister = getattr(self.oc, "list_embeddings_for_ontology", None)
        if lister is None:
            return []
        subj = _tokenize(subject_name)
        if not subj:
            return []
        entries = self._get_lexical_index(tuple(dict.fromkeys(ontology_ids)), lister)
        scored: list[tuple[float, _LexicalIndexEntry]] = []
        for entry in entries:
            overlap = subj & entry.tokens
            if not overlap:
                continue
            score = len(overlap) / len(subj | entry.tokens)
            scored.append((score, entry))
        scored.sort(key=lambda s: -s[0])
        out: list[GroundingCandidate] = []
        for _score, entry in scored[:top_k]:
            out.append(
                GroundingCandidate(
                    entity_uri=entry.entity_uri,
                    ontology_id=entry.ontology_id,
                    label=entry.label,
                    definition=entry.definition,
                    lexical_sim=0.0,
                    from_lexical=True,
                )
            )
        return out

    def _get_lexical_index(
        self,
        ontology_ids: tuple[str, ...],
        lister,
    ) -> tuple[_LexicalIndexEntry, ...]:
        """Return one bounded lexical index, building it once across workers."""
        if len(ontology_ids) > _LEXICAL_INDEX_MAX_ONTOLOGIES:
            raise GroundingRecallLimitError(
                f"grounding ontology pool exceeds {_LEXICAL_INDEX_MAX_ONTOLOGIES} distinct ontologies"
            )

        cached = self._lexical_index_cache.get(ontology_ids)
        if cached is not None:
            return cached

        with self._lexical_index_lock:
            cached = self._lexical_index_cache.get(ontology_ids)
            if cached is not None:
                return cached

            entries: list[_LexicalIndexEntry] = []
            seen: set[tuple[str, str]] = set()
            total_bytes = 0

            for requested_ontology_id in ontology_ids:
                remaining = _LEXICAL_INDEX_MAX_ENTITIES - len(entries)
                if remaining <= 0:
                    raise GroundingRecallLimitError(
                        f"lexical grounding index exceeds {_LEXICAL_INDEX_MAX_ENTITIES} entities"
                    )
                try:
                    docs = lister(
                        requested_ontology_id,
                        entity_type="class",
                        embedding_type="lexical",
                        namespace=self._namespace,
                        limit=remaining + 1,
                    )
                except Exception as exc:
                    log.error(
                        "lexical_recall: metadata listing failed ontology_id=%s",
                        requested_ontology_id,
                        exc_info=True,
                    )
                    raise GroundingLookupError(
                        f"lexical grounding metadata unavailable for ontology {requested_ontology_id!r}"
                    ) from exc

                if len(docs or []) > remaining:
                    raise GroundingRecallLimitError(
                        f"lexical grounding index exceeds {_LEXICAL_INDEX_MAX_ENTITIES} entities"
                    )

                for document in docs or []:
                    uri = str(document.get("entity_uri") or "").strip()
                    if not uri:
                        continue
                    ontology_id = str(document.get("ontology_id") or requested_ontology_id)
                    uri_bytes = len(uri.encode("utf-8"))
                    ontology_id_bytes = len(ontology_id.encode("utf-8"))
                    if (
                        uri_bytes > _LEXICAL_INDEX_MAX_ID_UTF8_BYTES
                        or ontology_id_bytes > _LEXICAL_INDEX_MAX_ID_UTF8_BYTES
                    ):
                        raise GroundingRecallLimitError(
                            f"lexical grounding identity exceeds {_LEXICAL_INDEX_MAX_ID_UTF8_BYTES} UTF-8 bytes"
                        )
                    identity = (uri.rstrip("/"), ontology_id)
                    if identity in seen:
                        continue

                    label = _local_name(uri)
                    tokens = frozenset(_tokenize(label))
                    if not tokens:
                        continue
                    definition = _utf8_prefix(
                        str(document.get("text") or ""),
                        _LEXICAL_INDEX_MAX_DEFINITION_UTF8_BYTES,
                    )
                    row_bytes = (
                        uri_bytes + ontology_id_bytes + len(label.encode("utf-8")) + len(definition.encode("utf-8"))
                    )
                    if total_bytes + row_bytes > _LEXICAL_INDEX_MAX_TOTAL_UTF8_BYTES:
                        raise GroundingRecallLimitError(
                            f"lexical grounding metadata exceeds {_LEXICAL_INDEX_MAX_TOTAL_UTF8_BYTES} UTF-8 bytes"
                        )

                    seen.add(identity)
                    total_bytes += row_bytes
                    entries.append(
                        _LexicalIndexEntry(
                            entity_uri=uri,
                            ontology_id=ontology_id,
                            label=label,
                            definition=definition,
                            tokens=tokens,
                        )
                    )

            built = tuple(entries)
            self._lexical_index_cache[ontology_ids] = built
            log.info(
                "lexical_recall: built bounded metadata index ontologies=%d entities=%d bytes=%d",
                len(ontology_ids),
                len(built),
                total_bytes,
            )
            return built

    def _exact_name_matches(self, table_name: str, candidates: list[GroundingCandidate]) -> list[GroundingCandidate]:
        """Return all candidates whose label is an exact normalized match to the table name.

        An empty normalized key is not a match. ``_normalize`` returns ``""`` for a
        name with no letters or digits at all (``"—"``, ``"(1)"``), and ``"" == ""``
        would make every such name match every other one — grounding distinct
        concepts onto each other. Bail rather than compare.
        """
        norm_table = _normalize(table_name)
        if not norm_table:
            return []
        return [c for c in candidates if _normalize(c.label) == norm_table]

    def _llm_rerank(
        self,
        table_name: str,
        description: str,
        columns: list[dict],
        candidates: list[GroundingCandidate],
        rerank_system: str = _RERANK_SYSTEM,
        subject_kind: str = "table",
        max_tokens: int = 1000,
        candidate_window: int = _RERANK_CANDIDATE_WINDOW,
    ) -> dict:
        """Ask the LLM to pick the best candidate or abstain.

        ``subject_kind`` ("table" or "class") + ``rerank_system`` let the
        shared reranker frame the source correctly: tables get a
        ``Columns:`` section; classes (no columns) omit it and are labeled
        SOURCE CLASS. The JSON contract and parsing are identical.

        ``max_tokens`` caps the rerank response; a reasoning model spends part
        of this budget on its (discarded) thinking, so it defaults well above
        the short JSON answer's needs.

        Returns the parsed rerank dict (``{"choice": "NONE", ...}`` is a
        genuine abstention the caller maps to novel). Raises
        :class:`GroundingRerankError` when the call itself failed
        (Bedrock error after retries, no text block, a truncating
        ``stopReason``, or unparseable non-JSON output) — an infrastructure
        or model-contract failure that must NOT be misread as an abstention
        and silently grounded novel.
        """
        is_class = subject_kind == "class"
        # Build the prompt with source context + candidate definitions.
        # A class has no columns, so the column section is omitted for it.
        col_lines = []
        for col in columns[:20]:
            parts = [f"  - {col.get('name', '')} : {col.get('dataType', '')}"]
            if col.get("description"):
                parts.append(f"-- {col['description'][:80]}")
            if col.get("constraint"):
                parts.append(f"[{col['constraint']}]")
            col_lines.append(" ".join(parts))

        # Show the top of the recall window, PLUS any token-overlap (lexical)
        # candidate that fell past it. The lexical retriever exists precisely to
        # surface classes dense recall ranked low, so window truncation must not
        # drop them before the LLM judges (issue 72). Dense candidates past the
        # window are dropped as before — that is the intended precision cut.
        window = list(candidates[:candidate_window])
        window.extend(c for c in candidates[candidate_window:] if c.from_lexical)
        cand_lines = []
        for c in window:
            defn = c.definition or "(no definition available)"
            source = _local_name(c.ontology_id) if c.ontology_id else "unknown"
            cand_lines.append(f"  - {c.label} [from {source}]: {defn[:200]}")

        header = f"SOURCE {'CLASS' if is_class else 'TABLE'}: {table_name}\n"
        columns_block = "" if is_class else ("Columns:\n" + "\n".join(col_lines) + "\n")
        prompt = (
            header + f"Description: {description or '(none)'}\n" + columns_block + "\n"
            "CANDIDATE ONTOLOGY CLASSES:\n" + "\n".join(cand_lines) + "\n\n"
            f"Which candidate should this {subject_kind} ground to (or NONE)?"
        )

        try:
            # Warm the lazy bedrock client BEFORE timing but INSIDE the try — first
            # access constructs the boto3 client + service model (~100-300ms), which
            # must NOT be counted as rerank latency (_t0 is captured after the client
            # is ready, so _t0→converse-return measures only the round-trip). Keeping
            # the warm-up inside the try means a construction failure (e.g. bad region)
            # is caught below and abstains, exactly like a converse() failure — never
            # escaping to fail the whole induction job.
            bedrock = self.bedrock
            _t0 = time.monotonic()
            # No temperature: it is deprecated on newer models (e.g. Claude Opus 5),
            # which reject the request with a ValidationException rather than
            # ignoring it. We only ever wanted 0 (the model default), so omit it.
            # botocore's bounded retries (async_boto_config, max_attempts=3) cover
            # transient throttling / 5xx; an error surfacing past them is real and
            # is raised below, not swallowed to a novel.
            resp = bedrock.converse(
                modelId=self._llm_model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                system=[{"text": rerank_system}],
                inferenceConfig={"maxTokens": max_tokens},
            )
            # Record token usage on the success path, right after converse returns
            # — usage is present even if the JSON body later fails to parse.
            if self._cost_tracker is not None:
                self._cost_tracker.record_from_converse(
                    "rerank",
                    self._llm_model_id,
                    resp,
                    latency_ms=(time.monotonic() - _t0) * 1000.0,
                )
            # A truncated / malformed / filtered response is an infrastructure
            # failure, not an abstention — do not try to parse a partial body
            # into a silent novel. max_tokens most commonly bites reasoning
            # models, whose thinking shares this budget: surface the knob.
            stop_reason = resp.get("stopReason")
            if stop_reason in ("max_tokens", "model_context_window_exceeded"):
                raise GroundingRerankError(
                    f"rerank response truncated (stopReason={stop_reason}) for '{table_name}' — "
                    f"raise rerank_max_tokens (currently {max_tokens})"
                )
            if stop_reason in ("malformed_model_output", "content_filtered"):
                raise GroundingRerankError(f"rerank response unusable (stopReason={stop_reason}) for '{table_name}'")
            # Select the answer by block type, not position: a reasoning model
            # returns a reasoningContent block (no "text" key) before the answer.
            raw = extract_text_blocks(resp["output"]["message"]["content"]).strip()
            # Parse JSON response; handle cases where the LLM wraps it in markdown.
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(raw)
            log.info(
                "grounding_rerank table=%s choice=%s confidence=%.2f reason=%s",
                table_name,
                result.get("choice"),
                result.get("confidence", 0),
                result.get("reason", ""),
            )
            return result
        except json.JSONDecodeError as e:
            # The call succeeded but the model did not return parseable JSON
            # (prose preamble, wrong quoting, trailing text, or a markdown
            # wrapper the fence-strip above did not fully normalize). A genuine
            # abstention has a parseable form ({"choice": "NONE"}); unparseable
            # output is the model failing the JSON contract, i.e. a malfunction.
            # Raise so the job fails loud instead of silently grounding novel.
            snippet = raw[:200].replace("\n", " ")
            log.error("LLM rerank returned non-JSON for table '%s': %s | raw=%r", table_name, e, snippet)
            raise GroundingRerankError(f"LLM rerank returned non-JSON for '{table_name}': {e} | raw={snippet!r}") from e
        except (ClientError, BotoCoreError, ValueError) as e:
            # Infrastructure failure: Bedrock error after retries (ClientError /
            # BotoCoreError — includes the temperature ValidationException and any
            # transient error that outlived the bounded retries) or no usable text
            # block (ValueError from extract_text_blocks). NOT an abstention —
            # raise so the job fails loud instead of silently grounding novel.
            # The GroundingRerankError raised above for a bad stopReason does not
            # subclass these, so it propagates untouched rather than re-wrapping.
            log.error("LLM rerank failed for table '%s': %s", table_name, e)
            raise GroundingRerankError(f"LLM rerank failed for '{table_name}': {e}") from e

    def to_concept_match(self, result: GroundingResult) -> ConceptMatch:
        """Convert a GroundingResult to the existing ConceptMatch schema."""
        return ConceptMatch(
            source_column=result.source_column,
            source_table=result.source_table,
            matched_class_uri=result.chosen.entity_uri if result.chosen else None,
            matched_ontology_id=result.chosen.ontology_id if result.chosen else None,
            similarity=result.confidence,
            match_type=result.match_type,
            relationship=result.relationship,
            scoring_strategy=f"grounding_{result.mode.lower()}",
            reason=result.reason,
            candidates=[
                MatchCandidate(
                    entity_uri=c.entity_uri,
                    ontology_id=c.ontology_id,
                    lexical_sim=c.lexical_sim,
                    structural_sim=None,
                    fused_score=c.rerank_score,
                    definition=c.definition or None,
                )
                for c in result.candidates[:10]
            ]
            if result.candidates
            else None,
        )
