# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session management via AgentCore Memory SDK.

Provides create/resume, history loading, and turn storage for
multi-turn conversations. Sessions use ULID IDs and are
scoped per namespace through DynamoDB metadata for discovery.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import structlog
from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.constants import BlobMessage, ConversationalMessage, MessageRole
from ulid import ULID

from .graph_context import (
    LEGACY_SUPPORTING_STATEMENTS_KEY,
    SUPPORTING_STATEMENTS_KEY,
    bound_supporting_statements,
    encode_supporting_statements,
    fair_bound_statement_groups,
    truncate_utf8,
)

logger = structlog.get_logger(__name__)

DEFAULT_HISTORY_WINDOW = 5
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="memory")

# Maximum size for the serialized JSON envelope stored as assistant message text.
_MAX_ENVELOPE_CHARS = 10_000
_MAX_ENVELOPE_BYTES = 10_000
_MAX_METADATA_CHARS = 8_000
_MAX_METADATA_BYTES = 8_000
_MAX_PERSISTED_VALUE_CHARS = 1_000
_MAX_PERSISTED_VALUE_BYTES = 2_000

# Up-front bounds on caller-supplied collections, applied BEFORE any size search.
# Compaction keeps far fewer items than these, so the caps change no successful
# outcome; they exist so an absurd input cannot drive the search loops at all.
# Tier-2 lets a caller request 10,000 result rows (``tier2/strategy.py``
# MAX_RESULT_ROWS) and graph context is unbounded upstream, and every search
# iteration costs one full envelope serialization.
_MAX_PERSISTED_RESULT_ROWS = 200
_MAX_PERSISTED_TRACE_STEPS = 100
_MAX_PERSISTED_ENTITIES = 200
_MAX_PERSISTED_RELATIONSHIPS = 200
_MAX_PERSISTED_STATEMENTS = 200

_CORE_ENVELOPE_FIELDS = frozenset({"text", "tier", "confidence", "partial", "guardrailBlocked"})
_OPTIONAL_FIELD_DROP_ORDER = (
    "query",
    "sparqlGenerated",
    "queryUsed",
    "dataSources",
    "ontologyVersion",
    "graphContext",
    "resultRows",
    "trace",
    "metadata",
    "requestId",
)
_DROP_JSON_VALUE = object()
_UNRESTORABLE_ASSISTANT_TEXT = "This response could not be restored."


def _json_default(obj: object) -> object:
    """json.dumps fallback for types the stdlib encoder can't handle.

    Query result rows can carry non-JSON-native types sourced from the
    underlying database (e.g. a ``numeric``/``DECIMAL`` column arrives as
    Python ``Decimal`` via asyncpg). Without this, envelope serialization
    raises ``TypeError: Object of type Decimal is not JSON serializable`` and
    the turn is never persisted, the response is never delivered, and the
    UI hangs on "Running query". Decimal → float (JSON number); date/datetime →
    ISO-8601 string.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def _sanitize_json_value(value: object, ancestors: frozenset[int] = frozenset()) -> object:
    """Return a JSON-safe copy, dropping only unsupported or cyclic branches.

    Result metadata can contain optional diagnostics supplied by downstream
    integrations. One malformed value must not prevent the serializable query
    contract from being persisted. Ancestors are tracked per recursion path so
    repeated shared values remain intact while actual cycles are omitted.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (Decimal, datetime.date, datetime.datetime, datetime.time)):
        return _json_default(value)

    if isinstance(value, dict):
        identity = id(value)
        if identity in ancestors:
            return _DROP_JSON_VALUE
        nested_ancestors = ancestors | {identity}
        sanitized_dict: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            sanitized_item = _sanitize_json_value(item, nested_ancestors)
            if sanitized_item is not _DROP_JSON_VALUE:
                sanitized_dict[key] = sanitized_item
        return sanitized_dict

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in ancestors:
            return _DROP_JSON_VALUE
        nested_ancestors = ancestors | {identity}
        sanitized_list: list[object] = []
        for item in value:
            sanitized_item = _sanitize_json_value(item, nested_ancestors)
            if sanitized_item is not _DROP_JSON_VALUE:
                sanitized_list.append(sanitized_item)
        return sanitized_list

    return _DROP_JSON_VALUE


def _build_message_envelope(
    answer: str,
    result_metadata: dict | None = None,
) -> str:
    """Build a JSON envelope containing answer text plus metadata.

    The envelope is stored as the assistant ConversationalMessage text.
    On read, the parser detects it (object with "text" key) and splits
    content from metadata.

    ``result_metadata`` is expected to come pre-serialized (camelCase keys,
    no None values) from QueryResult.model_dump(). This function applies
    deterministic UTF-8 size guards to trace details and graph context without
    mutating the caller's metadata.

    CPU cost is bounded: every caller-supplied collection is capped up front and
    each size-driven reduction binary-searches its keep count, so the number of
    envelope serializations is logarithmic in the input size, not linear. Callers
    on an event loop MUST still run this in an executor (see
    :meth:`SessionManager.append_turn`) because the work is synchronous.
    """
    envelope: dict = {"text": answer}
    if result_metadata:
        sanitized_metadata = _sanitize_json_value(result_metadata)
        if isinstance(sanitized_metadata, dict):
            envelope.update(sanitized_metadata)

    _cap_persisted_collections(envelope)
    if "graphContext" in envelope:
        _normalize_graph_context(envelope)

    # Keep metadata/evidence below a separate budget so a useful prefix of the
    # answer can still be persisted. Each stage is deterministic and only runs
    # when the metadata-only envelope remains over budget.
    if _metadata_too_large(envelope):
        _strip_trace_details(envelope)
    if _metadata_too_large(envelope):
        _compact_result_rows(envelope)
    if _metadata_too_large(envelope):
        _compact_nested_metadata(envelope)
    if _metadata_too_large(envelope):
        _compact_confidence(envelope)
    if _metadata_too_large(envelope):
        _trim_graph_context(envelope)
    if _metadata_too_large(envelope):
        _trim_trace_steps(envelope)
    if _metadata_too_large(envelope):
        _drop_optional_fields(envelope)
    if _metadata_too_large(envelope):
        envelope = _essential_envelope(envelope)

    serialized = _fit_answer(envelope, answer)
    if _envelope_too_large(serialized):
        serialized = _fit_answer(_essential_envelope(envelope), answer)
    if _envelope_too_large(serialized):
        # This literal is the final invariant backstop. It is valid restore JSON
        # and is far below both limits regardless of caller-controlled content.
        # Reaching it means the turn is persisted with NO answer text, so it is
        # reported rather than applied silently.
        logger.warning(
            "session_envelope_backstop_used",
            answer_chars=len(answer),
            answer_bytes=len(answer.encode("utf-8")),
            metadata_keys=sorted(key for key in envelope if key != "text"),
        )
        return '{"text":""}'

    return serialized


def _cap_persisted_collections(envelope: dict) -> None:
    """Bound caller-supplied list sizes before any size-driven compaction runs.

    Result rows and trace steps arrive unbounded from the caller's perspective,
    and each compaction iteration costs a full envelope serialization. Capping the
    input keeps the later searches cheap; the retained prefix is the same one
    compaction would have kept anyway. Graph context is capped in
    :func:`_normalize_graph_context`, where the statement budget is shared across
    exactly the entities that survive the cap.
    """
    for key, limit in (
        ("resultRows", _MAX_PERSISTED_RESULT_ROWS),
        ("trace", _MAX_PERSISTED_TRACE_STEPS),
    ):
        items = envelope.get(key)
        if isinstance(items, list) and len(items) > limit:
            logger.info(
                "session_envelope_input_capped",
                field=key,
                supplied=len(items),
                retained=limit,
            )
            envelope[key] = items[:limit]


def _serialize_envelope(envelope: dict) -> str:
    """Serialize a persistence envelope in its stable compact representation."""
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _serialized_too_large(serialized: str, *, max_chars: int, max_bytes: int) -> bool:
    """Return whether a serialized value exceeds either configured size bound."""
    return len(serialized) > max_chars or len(serialized.encode("utf-8")) > max_bytes


def _envelope_too_large(serialized: str) -> bool:
    """Return whether either hard session-envelope bound is exceeded."""
    return _serialized_too_large(
        serialized,
        max_chars=_MAX_ENVELOPE_CHARS,
        max_bytes=_MAX_ENVELOPE_BYTES,
    )


def _metadata_too_large(envelope: dict) -> bool:
    """Return whether persisted fields other than the answer exceed their budget."""
    metadata_envelope = {**envelope, "text": ""}
    return _serialized_too_large(
        _serialize_envelope(metadata_envelope),
        max_chars=_MAX_METADATA_CHARS,
        max_bytes=_MAX_METADATA_BYTES,
    )


def _mark_graph_relationship_degradation(envelope: dict, reason: str, dropped: int) -> None:
    """Persist truthful partial/degraded state when relationship evidence is lost."""
    if dropped <= 0:
        return
    envelope["partial"] = True
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        envelope["metadata"] = metadata
    degraded_sources = metadata.get("degradedSources")
    if not isinstance(degraded_sources, list):
        degraded_sources = []
        metadata["degradedSources"] = degraded_sources
    if any(
        isinstance(entry, dict) and entry.get("source") == "session_persistence" and entry.get("reason") == reason
        for entry in degraded_sources
    ):
        return
    degraded_sources.append(
        {
            "source": "session_persistence",
            "reason": reason,
            "detail": "graph relationship evidence was truncated while persisting or restoring the turn",
        }
    )


def _fit_list_prefix(envelope: dict, items: list) -> int:
    """Keep the longest prefix of ``items`` that fits the metadata budget.

    ``items`` MUST be the very list object reachable from ``envelope`` — it is
    truncated in place so the envelope stays consistent while the search runs.

    The keep count is binary-searched rather than decremented one element per full
    envelope re-serialization: serialized size is monotone in the prefix length, so
    ``O(log n)`` serializations find the same answer that ``O(n)`` pops would.

    Returns:
        How many trailing items were dropped.
    """
    if not items or not _metadata_too_large(envelope):
        return 0
    original = list(items)
    # The full list is already known not to fit, so search [0, len - 1].
    low, high, best = 0, len(original) - 1, 0
    while low <= high:
        midpoint = (low + high) // 2
        items[:] = original[:midpoint]
        if _metadata_too_large(envelope):
            high = midpoint - 1
        else:
            best = midpoint
            low = midpoint + 1
    items[:] = original[:best]
    return len(original) - best


def _graph_context_parts(graph_context: object) -> tuple[list[dict], list[dict]]:
    """Return mutable entity/relationship lists from canonical graph context."""
    if isinstance(graph_context, dict):
        raw_entities = graph_context.get("entities")
        raw_relationships = graph_context.get("relationships")
        entities = (
            [entity for entity in raw_entities if isinstance(entity, dict)] if isinstance(raw_entities, list) else []
        )
        relationships = (
            [relationship for relationship in raw_relationships if isinstance(relationship, dict)]
            if isinstance(raw_relationships, list)
            else []
        )
        graph_context["entities"] = entities
        graph_context["relationships"] = relationships
        return entities, relationships
    return [], []


def _statement_slots(entities: list[dict]) -> list[tuple[dict, str, list[str]]]:
    """Locate supporting-statement properties across canonical and legacy entities."""
    slots: list[tuple[dict, str, list[str]]] = []
    for entity in entities:
        properties = entity.get("properties")
        if not isinstance(properties, dict):
            continue
        key = SUPPORTING_STATEMENTS_KEY if SUPPORTING_STATEMENTS_KEY in properties else LEGACY_SUPPORTING_STATEMENTS_KEY
        if key not in properties:
            continue
        slots.append((properties, key, bound_supporting_statements(properties[key])))
    return slots


def _store_slot(properties: dict, key: str, statements: list[str]) -> None:
    """Write one compacted slot while preserving its canonical or legacy key."""
    if statements:
        properties[key] = encode_supporting_statements(statements)
    else:
        properties.pop(key, None)


def _cap_statement_total(groups: list[list[str]], limit: int) -> int:
    """Bound the TOTAL statement count across groups, shedding the largest first.

    ``fair_bound_statement_groups`` bounds statement bytes but not the statement
    count, so a few thousand one-character statements still cost one search
    iteration each in :func:`_trim_graph_context`. Dropping from the currently
    largest group (ties resolved by lowest index) keeps the reduction deterministic
    and fair, and costs no serialization.

    Returns:
        How many statements were dropped.
    """
    total = sum(len(group) for group in groups)
    dropped = 0
    while total > limit:
        index = max(range(len(groups)), key=lambda position: (len(groups[position]), -position))
        if not groups[index]:
            break
        groups[index].pop()
        total -= 1
        dropped += 1
    return dropped


def _normalize_graph_context(envelope: dict) -> None:
    """Canonicalize graph context, cap its size, and apply fair statement bounds."""
    graph_context = envelope.get("graphContext")
    if isinstance(graph_context, list):
        graph_context = {
            "entities": [entity for entity in graph_context if isinstance(entity, dict)],
            "relationships": [],
        }
        envelope["graphContext"] = graph_context
    entities, relationships = _graph_context_parts(graph_context)

    # Cap before the statement budget is shared, so the surviving entities get the
    # whole budget instead of a sliver each. `_graph_context_parts` installed these
    # exact list objects on `graph_context`, so truncating in place is enough.
    dropped_entities = max(0, len(entities) - _MAX_PERSISTED_ENTITIES)
    dropped_relationships = max(0, len(relationships) - _MAX_PERSISTED_RELATIONSHIPS)
    del entities[_MAX_PERSISTED_ENTITIES:]
    del relationships[_MAX_PERSISTED_RELATIONSHIPS:]
    _mark_graph_relationship_degradation(
        envelope,
        "graph_relationship_cap_reached",
        dropped_relationships,
    )

    slots = _statement_slots(entities)
    fair_groups = fair_bound_statement_groups([statements for _, _, statements in slots])
    dropped_statements = _cap_statement_total(fair_groups, _MAX_PERSISTED_STATEMENTS)
    for (properties, key, slot_statements), statements in zip(slots, fair_groups, strict=True):
        slot_statements[:] = statements
        _store_slot(properties, key, statements)

    if dropped_entities or dropped_relationships or dropped_statements:
        logger.info(
            "session_envelope_graph_context_capped",
            dropped_entities=dropped_entities,
            dropped_relationships=dropped_relationships,
            dropped_statements=dropped_statements,
        )

    if not entities and not relationships:
        envelope.pop("graphContext", None)


def _strip_trace_details(envelope: dict) -> None:
    """Drop diagnostic trace details, earliest steps first, while metadata is over budget.

    Step/status contract fields are retained. The number of leading steps to strip
    is binary-searched (serialized size is monotone in it), so this costs
    ``O(log steps)`` envelope serializations, not one per step, and keeps the same
    details a step-by-step strip would.
    """
    trace = envelope.get("trace")
    if not isinstance(trace, list):
        return
    positions = [index for index, step in enumerate(trace) if isinstance(step, dict) and "detail" in step]
    if not positions:
        return
    originals = {index: trace[index] for index in positions}
    stripped = {
        index: {key: value for key, value in step.items() if key != "detail"} for index, step in originals.items()
    }

    def _strip_leading(count: int) -> None:
        for rank, index in enumerate(positions):
            trace[index] = stripped[index] if rank < count else originals[index]

    # Stripping every detail is the most this stage can do; when even that does
    # not fit, the later stages take over.
    _strip_leading(len(positions))
    if _metadata_too_large(envelope):
        return
    low, high, best = 1, len(positions) - 1, len(positions)
    while low <= high:
        midpoint = (low + high) // 2
        _strip_leading(midpoint)
        if _metadata_too_large(envelope):
            low = midpoint + 1
        else:
            best = midpoint
            high = midpoint - 1
    _strip_leading(best)


def _compact_json_value(value: object) -> object:
    """Bound nested persisted values without changing their basic JSON shape."""
    if isinstance(value, str):
        return truncate_utf8(
            value,
            max_chars=_MAX_PERSISTED_VALUE_CHARS,
            max_bytes=_MAX_PERSISTED_VALUE_BYTES,
        )
    if isinstance(value, list):
        return [_compact_json_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _compact_json_value(item) for key, item in list(value.items())[:50]}
    return value


def _compact_result_rows(envelope: dict) -> None:
    """Retain the earliest bounded result rows while metadata remains over budget."""
    rows = envelope.get("resultRows")
    if not isinstance(rows, list):
        return
    compacted = [_compact_json_value(row) for row in rows]
    envelope["resultRows"] = compacted
    _fit_list_prefix(envelope, compacted)


def _compact_nested_metadata(envelope: dict) -> None:
    """Bound nested metadata values, then discard least-essential keys deterministically."""
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        envelope.pop("metadata", None)
        return

    compacted = {str(key): _compact_json_value(value) for key, value in metadata.items()}
    envelope["metadata"] = compacted
    preferred = {"degradedSources", "namespace", "modelId", "principal"}
    # ``degradedSources`` is not optional decoration: dropping it while
    # graphContext is still awaiting its own compaction would turn known evidence
    # loss back into an apparently complete restored response.
    removal_order = sorted(
        (key for key in compacted if key != "degradedSources"),
        key=lambda key: (key in preferred, key),
    )
    for key in removal_order:
        if not _metadata_too_large(envelope):
            break
        compacted.pop(key, None)
    if not compacted:
        envelope.pop("metadata", None)


def _compact_confidence(envelope: dict) -> None:
    """Keep confidence score while bounding or removing an oversized rationale."""
    confidence = envelope.get("confidence")
    if not isinstance(confidence, dict):
        return
    rationale = confidence.get("rationale")
    if isinstance(rationale, str):
        confidence["rationale"] = truncate_utf8(
            rationale,
            max_chars=_MAX_PERSISTED_VALUE_CHARS,
            max_bytes=_MAX_PERSISTED_VALUE_BYTES,
        )
    if _metadata_too_large(envelope):
        confidence.pop("rationale", None)


def _fit_connected_subgraph(
    envelope: dict,
    entities: list[dict],
    relationships: list[dict],
    *,
    include_dangling: bool = True,
) -> None:
    """Shrink graph context toward a smaller CONNECTED subgraph.

    Dropping relationships before entities leaves the user a list of entity names
    with no edges, which is strictly less useful than fewer entities that still
    show how they connect. So the entity count is binary-searched, and an edge is
    retained exactly when BOTH of its endpoints survive — the induced subgraph over
    the retained entities. Any budget still left over is then spent re-admitting
    edges whose endpoints were never returned as entities (a graph traversal can
    cite an edge to a node outside the returned entity set).

    ``entities`` / ``relationships`` MUST be the list objects reachable from
    ``envelope``; both are rewritten in place.
    """
    all_entities = list(entities)
    all_relationships = list(relationships)

    def apply(keep: int) -> None:
        retained = all_entities[:keep]
        retained_uris = {entity["uri"] for entity in retained if isinstance(entity.get("uri"), str) and entity["uri"]}
        entities[:] = retained
        relationships[:] = [
            relationship
            for relationship in all_relationships
            if relationship.get("sourceUri") in retained_uris and relationship.get("targetUri") in retained_uris
        ]

    low, high, best = 0, len(all_entities), 0
    while low <= high:
        midpoint = (low + high) // 2
        apply(midpoint)
        if _metadata_too_large(envelope):
            high = midpoint - 1
        else:
            best = midpoint
            low = midpoint + 1
    apply(best)

    if not include_dangling:
        return

    induced = {id(relationship) for relationship in relationships}
    dangling = [relationship for relationship in all_relationships if id(relationship) not in induced]
    if not dangling or _metadata_too_large(envelope):
        return
    connected = list(relationships)
    low, high, best_extra = 0, len(dangling), 0
    while low <= high:
        midpoint = (low + high) // 2
        relationships[:] = connected + dangling[:midpoint]
        if _metadata_too_large(envelope):
            high = midpoint - 1
        else:
            best_extra = midpoint
            low = midpoint + 1
    relationships[:] = connected + dangling[:best_extra]


def _trim_graph_context(envelope: dict) -> None:
    """Remove graph evidence fairly until the metadata-only envelope fits.

    Statement evidence goes first (it is the bulkiest per entity and is already
    duplicated in the answer text), then the entity/relationship sets are reduced
    together toward a connected subgraph.
    """
    graph_context = envelope.get("graphContext")
    entities, relationships = _graph_context_parts(graph_context)
    slots = _statement_slots(entities)

    # Bounded by `_MAX_PERSISTED_STATEMENTS` (enforced in `_normalize_graph_context`),
    # so this cannot become an unbounded serialize-per-element loop.
    while _metadata_too_large(envelope):
        populated = [(index, slot) for index, slot in enumerate(slots) if slot[2]]
        if not populated:
            break
        multi = [entry for entry in populated if len(entry[1][2]) > 1]
        candidates = multi or populated
        _, (properties, key, statements) = max(
            candidates,
            key=lambda entry: (
                len(entry[1][2]),
                sum(len(statement.encode("utf-8")) for statement in entry[1][2]),
                entry[0],
            ),
        )
        statements.pop()
        _store_slot(properties, key, statements)

    if _metadata_too_large(envelope):
        relationship_count = len(relationships)
        _fit_connected_subgraph(envelope, entities, relationships)
        dropped_relationships = relationship_count - len(relationships)
        _mark_graph_relationship_degradation(
            envelope,
            "graph_relationship_size_compaction",
            dropped_relationships,
        )
        # The degradation record is part of the persisted contract and therefore
        # part of the same metadata budget. Re-fit after adding it instead of
        # allowing the later optional-field stage to discard graphContext whole.
        if dropped_relationships and _metadata_too_large(envelope):
            _fit_connected_subgraph(envelope, entities, relationships, include_dangling=False)

    if not entities and not relationships:
        envelope.pop("graphContext", None)


def _trim_trace_steps(envelope: dict) -> None:
    """Retain the earliest trace steps while the metadata budget permits."""
    trace = envelope.get("trace")
    if not isinstance(trace, list):
        return
    _fit_list_prefix(envelope, trace)


def _drop_optional_fields(envelope: dict) -> None:
    """Drop known optional fields, then future unknown fields, in stable order."""
    for key in _OPTIONAL_FIELD_DROP_ORDER:
        if not _metadata_too_large(envelope):
            return
        envelope.pop(key, None)

    for key in sorted(set(envelope) - _CORE_ENVELOPE_FIELDS, reverse=True):
        if not _metadata_too_large(envelope):
            return
        envelope.pop(key, None)


def _essential_envelope(envelope: dict) -> dict:
    """Build the smallest useful contract envelope from caller-supplied fields."""
    essential: dict = {"text": ""}
    for key in ("tier", "confidence", "partial", "guardrailBlocked"):
        if key in envelope:
            essential[key] = _compact_json_value(envelope[key])
    if _metadata_too_large(essential):
        confidence = essential.get("confidence")
        if isinstance(confidence, dict):
            confidence.pop("rationale", None)
    for key in ("guardrailBlocked", "partial", "confidence", "tier"):
        if not _metadata_too_large(essential):
            break
        essential.pop(key, None)
    return essential


def _fit_answer(envelope: dict, answer: str) -> str:
    """Fit the longest deterministic answer prefix inside both hard bounds."""
    envelope["text"] = answer
    serialized = _serialize_envelope(envelope)
    if not _envelope_too_large(serialized):
        return serialized

    low = 0
    high = len(answer)
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = answer[:midpoint]
        if midpoint < len(answer) and candidate:
            candidate = f"{candidate}…"
        envelope["text"] = candidate
        candidate_serialized = _serialize_envelope(envelope)
        if _envelope_too_large(candidate_serialized):
            high = midpoint - 1
        else:
            best = candidate
            low = midpoint + 1

    envelope["text"] = best
    logger.info(
        "session_envelope_answer_truncated",
        original_chars=len(answer),
        retained_chars=len(best),
        original_bytes=len(answer.encode("utf-8")),
    )
    return _serialize_envelope(envelope)


def _normalize_restored_graph_context(metadata: dict) -> None:
    """Normalize legacy top-level entity arrays and re-bound restored statements.

    The documented statement bounds are a property of the CONTRACT, not only of the
    write path: a record persisted before the bounds existed can still carry a
    12,000-character statement, and returning it unbounded pushes an arbitrarily
    large value at the client. Statements are therefore re-bounded on read with the
    same helpers the write path uses, preserving each slot's canonical or legacy
    key so old-record compatibility is unchanged.
    """
    if "graphContext" in metadata:
        _normalize_graph_context(metadata)


def _looks_like_assistant_envelope(raw_text: str) -> bool:
    """Return whether ``raw_text`` starts like an envelope emitted by this module.

    :func:`_build_message_envelope` always serializes ``text`` as the first object
    member. Restricting the malformed-payload fallback to that prefix preserves
    legacy plaintext that merely starts with ``{`` and valid JSON answers that are
    not session envelopes.
    """
    stripped = raw_text.lstrip()
    if not stripped.startswith("{"):
        return False
    first_member = stripped[1:].lstrip()
    if not first_member.startswith('"text"'):
        return False
    return first_member[len('"text"') :].lstrip().startswith(":")


def _parse_assistant_content(raw_text: str) -> tuple[str, dict | None]:
    """Parse assistant message content, detecting JSON envelope.

    Returns (display_text, metadata_or_none).
    Legacy plain-string messages return (raw_text, None).

    A malformed payload that starts like an envelope emitted by this module is
    replaced with a neutral restoration message rather than rendering serialized
    JSON to the user. Plaintext — including text beginning with ``{`` and JSON that
    is not an assistant envelope — remains byte-for-byte compatible. Only
    structural facts are logged (types, lengths, keys); conversation content is
    never emitted.
    """
    stripped = raw_text.lstrip()
    if not stripped.startswith("{"):
        return raw_text, None
    looks_like_envelope = _looks_like_assistant_envelope(raw_text)
    try:
        obj = json.loads(raw_text)
    except Exception as exc:  # noqa: BLE001 - any decode failure must fall back, not raise
        if looks_like_envelope:
            logger.warning(
                "assistant_envelope_parse_failed",
                error_type=type(exc).__name__,
                text_chars=len(raw_text),
            )
            return _UNRESTORABLE_ASSISTANT_TEXT, None
        return raw_text, None

    if not isinstance(obj, dict) or "text" not in obj:
        return raw_text, None

    text = obj.pop("text")
    if not isinstance(text, str):
        logger.warning(
            "assistant_envelope_text_not_a_string",
            text_type=type(text).__name__,
            text_chars=len(raw_text),
        )
        return _UNRESTORABLE_ASSISTANT_TEXT, None

    _normalize_restored_graph_context(obj)
    return text, (obj or None)


class SessionManager:
    """Manages conversation sessions backed by AgentCore Memory."""

    def __init__(self, memory_id: str, region: str | None = None):
        """Wire up the AgentCore Memory session manager.

        Args:
            memory_id: AgentCore Memory resource id backing conversation storage.
            region: AWS region for the Memory client; uses the default when None.
        """
        self._manager = MemorySessionManager(
            memory_id=memory_id,
            region_name=region,
        )

    @staticmethod
    def generate_session_id() -> str:
        """Generate a new unique session ID (ULID)."""
        return str(ULID())

    @staticmethod
    def _memory_actor(user_id: str, namespace_id: str) -> str:
        """Compose the AgentCore Memory actor id, scoped by namespace.

        Conversation turns are stored in AgentCore Memory keyed by
        (actor_id, session_id). Keying the actor on ``user_id`` alone lets a
        session id reused across namespaces (same user) share one history —
        cross-namespace context leakage. Qualifying the actor with the namespace
        isolates history per (user, namespace) so the same session id in a
        different namespace resolves to a distinct, empty history.

        The Memory API constrains actor_id to ``[a-zA-Z0-9][a-zA-Z0-9-_/]*`` —
        ``/`` is permitted (``#`` is not), so we join with ``/``. An empty
        ``namespace_id`` returns the bare ``user_id`` unchanged, preserving the
        prior key for any caller that cannot supply a namespace.
        """
        return f"{user_id}/{namespace_id}" if namespace_id else user_id

    async def create_or_resume(
        self,
        user_id: str,
        namespace_id: str,
        session_id: str | None = None,
        max_turns: int = DEFAULT_HISTORY_WINDOW,
    ) -> tuple[str, list[dict[str, str]]]:
        """Create or resume a session, returning ID and conversation history.

        For the orchestrator path: extracts only the text content from
        envelope messages, keeping the LLM context clean.

        Returns:
            Tuple of (session_id, conversation_history).
        """
        sid = session_id or SessionManager.generate_session_id()
        history = await self._load_history(user_id, sid, max_turns, include_metadata=False, namespace_id=namespace_id)

        logger.info(
            "session_ready",
            session_id=sid,
            user_id=user_id,
            namespace_id=namespace_id,
            resumed_turns=len(history),
        )
        return sid, history

    async def get_rich_history(
        self,
        user_id: str,
        session_id: str,
        max_turns: int = 25,
        *,
        namespace_id: str = "",
    ) -> list[dict]:
        """Retrieve session history with metadata for UI restore.

        Returns list of dicts: {role, content, metadata?}
        """
        history = await self._load_history(
            user_id, session_id, max_turns, include_metadata=True, namespace_id=namespace_id
        )

        logger.info(
            "rich_history_loaded",
            session_id=session_id,
            user_id=user_id,
            messages=len(history),
        )
        return history

    async def get_rich_history_page(
        self,
        user_id: str,
        session_id: str,
        limit: int = 5,
        offset: int = 0,
        max_turns: int = 25,
        *,
        namespace_id: str = "",
    ) -> tuple[list[dict], int, bool]:
        """Retrieve a page of session history (newest-first pagination).

        Loads history up to max_turns, then slices to return the requested page.
        Messages are ordered oldest-first in the returned list so the client
        can prepend them naturally.

        For initial restore, use a small max_turns (25) for fast response.
        For deep scroll-back, callers can pass a larger max_turns (500).

        Args:
            user_id: Identifier of the user whose history is being read.
            session_id: Conversation session to page through.
            limit: Number of messages per page.
            offset: How many messages from the end to skip (0 = most recent).
            max_turns: Upper bound on turns to fetch from AgentCore Memory.
            namespace_id: Namespace scoping the memory actor; isolates history
                per (user, namespace).

        Returns:
            Tuple of (messages_page, total_count, has_more).
        """
        history = await self._load_history(
            user_id, session_id, max_turns, include_metadata=True, namespace_id=namespace_id
        )
        total = len(history)

        # Slice from the end: offset=0 means the last `limit` messages
        end_idx = total - offset
        start_idx = max(0, end_idx - limit)
        page = history[start_idx:end_idx] if end_idx > 0 else []
        has_more = start_idx > 0

        logger.info(
            "rich_history_page_loaded",
            session_id=session_id,
            user_id=user_id,
            total=total,
            offset=offset,
            limit=limit,
            page_size=len(page),
            has_more=has_more,
        )
        return page, total, has_more

    async def _load_history(
        self,
        user_id: str,
        session_id: str,
        max_turns: int,
        *,
        include_metadata: bool,
        namespace_id: str = "",
    ) -> list[dict]:
        """Fetch turns from AgentCore Memory and parse messages."""
        session = self._manager.create_memory_session(
            actor_id=self._memory_actor(user_id, namespace_id),
            session_id=session_id,
        )
        loop = asyncio.get_running_loop()
        turns = await loop.run_in_executor(_EXECUTOR, session.get_last_k_turns, max_turns)

        history: list[dict] = []
        for event_messages in reversed(turns):
            for msg in event_messages:
                role = msg.get("role", "").lower()
                text = msg.get("content", {}).get("text", "")
                if role not in ("user", "assistant") or not text:
                    continue

                if role == "assistant":
                    display_text, metadata = _parse_assistant_content(text)
                    entry: dict = {"role": role, "content": display_text}
                    if include_metadata and metadata:
                        entry["metadata"] = metadata
                    history.append(entry)
                else:
                    history.append({"role": role, "content": text})

        return history

    async def append_turn(
        self,
        user_id: str,
        session_id: str,
        query: str,
        answer_summary: str,
        *,
        namespace_id: str = "",
        result_metadata: dict | None = None,
    ) -> None:
        """Store a conversation turn (user query + assistant response).

        Stores plain text first (guaranteed to work), then optionally
        attempts to store with JSON envelope for rich history.

        Envelope construction is CPU-bound synchronous work over caller-sized
        collections, so it runs on :data:`_EXECUTOR` rather than inline in this
        coroutine — inline it would block the whole event loop (every other
        in-flight request included), not just one worker.
        """
        # Build the text to store. Try envelope, fall back to plain text.
        loop = asyncio.get_running_loop()
        assistant_text = answer_summary
        if result_metadata:
            try:
                assistant_text = await loop.run_in_executor(
                    _EXECUTOR,
                    lambda: _build_message_envelope(answer=answer_summary, result_metadata=result_metadata),
                )
            except Exception:
                logger.warning("envelope_build_failed", session_id=session_id, exc_info=True)
                assistant_text = await loop.run_in_executor(
                    _EXECUTOR, lambda: _build_message_envelope(answer=answer_summary)
                )
        elif _serialized_too_large(
            answer_summary,
            max_chars=_MAX_ENVELOPE_CHARS,
            max_bytes=_MAX_ENVELOPE_BYTES,
        ):
            assistant_text = await loop.run_in_executor(
                _EXECUTOR, lambda: _build_message_envelope(answer=answer_summary)
            )

        session = self._manager.create_memory_session(
            actor_id=self._memory_actor(user_id, namespace_id),
            session_id=session_id,
        )
        messages: list[ConversationalMessage | BlobMessage] = [
            ConversationalMessage(query, MessageRole.USER),
            ConversationalMessage(assistant_text, MessageRole.ASSISTANT),
        ]

        # Store the turn. No metadata param; keep it simple and reliable.
        await loop.run_in_executor(_EXECUTOR, lambda: session.add_turns(messages))
