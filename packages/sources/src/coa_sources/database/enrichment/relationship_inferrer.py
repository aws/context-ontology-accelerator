# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pass 2: Cross-table foreign key inference via compact schema summary."""

from __future__ import annotations

import logging
from collections import Counter

from coa_common.bedrock import BedrockTruncationError
from coa_common.domain_models import EnrichmentSource, ForeignKey, Table

from coa_sources.database.enrichment.bedrock_client import BedrockClient
from coa_sources.database.pipeline.enrichment_metrics import EnrichmentMetricEmitter

logger = logging.getLogger(__name__)

RELATIONSHIP_SYSTEM_PROMPT = """\
You are a database schema analyst. \
Given a compact schema summary of all tables in a database, \
infer foreign key relationships between tables.

Respond ONLY with raw JSON. Do not wrap in markdown code fences or any other formatting.
Output must be a JSON array of objects with this exact structure:
[
  {
    "source_table": "orders",
    "column": "customer_id",
    "target_table": "customers",
    "target_column": "id",
    "confidence": 0.95
  }
]

Rules:
- Infer foreign keys from column naming patterns (e.g., customer_id -> customers.id, \
category_id -> categories.id, parent_id -> same_table.id).
- Only infer relationships where the target table EXISTS in the schema.
- confidence should be 0.0-1.0 based on naming match strength and type compatibility.
- Do NOT re-infer relationships listed under "Known relationships" — they are already confirmed.
- If no foreign keys can be inferred, return an empty array [].
- Each relationship must reference actual tables and columns from the provided schema.
- Prefer id/primary key columns as targets unless naming clearly indicates otherwise."""


TABLES_PER_BATCH = 250


def _display_names(tables: list[Table]) -> dict[int, str]:
    """Map each table (by id()) to a name that is UNIQUE within this batch.

    A single JDBC source can scan multiple schemas (schemaFilter ``public|sales``),
    so two distinct tables may share the bare ``name`` (``public.orders`` and
    ``sales.orders``). Keying the prompt / candidate resolution by bare name would
    collapse them (last-wins) and mis-assign or drop inferred FKs. Use the bare
    name when it is unique in the batch, else fall back to the schema-qualified
    ``table_id`` so the LLM sees — and returns — distinguishable identifiers.
    """
    name_counts = Counter(t.name for t in tables)
    return {id(t): (t.name if name_counts[t.name] == 1 else t.table_id) for t in tables}


def build_relationship_prompt(tables: list[Table]) -> str:
    """Build a compact schema summary for cross-table FK inference.

    Each table is represented as: table_name: col1 (type), col2 (type), ...
    Targets ~50 tokens per table (column names + types only, no descriptions).

    Known deterministic/steward FKs are listed separately so the LLM skips them.
    """
    display = _display_names(tables)
    schema_lines: list[str] = []
    known_relationships: list[str] = []

    for table in tables:
        name = display[id(table)]
        cols = ", ".join(f"{c.name} ({c.data_type})" for c in table.columns)
        schema_lines.append(f"  {name}: {cols}")

        for fk in table.foreign_keys:
            if fk.source in (
                EnrichmentSource.DETERMINISTIC,
                EnrichmentSource.STEWARD_SPECIFIED,
            ):
                known_relationships.append(f"  {name}.{fk.column} -> {fk.target_table}.{fk.target_column}")

    prompt = "Schema:\n" + "\n".join(schema_lines)

    if known_relationships:
        prompt += "\n\nKnown relationships (DO NOT re-infer):\n" + "\n".join(known_relationships)

    return prompt


def infer_relationships(tables: list[Table], client: BedrockClient, emitter: EnrichmentMetricEmitter) -> list[dict]:
    """Invoke Bedrock to infer cross-table FK relationships.

    For sources with ≤TABLES_PER_BATCH tables, makes a single Bedrock call.
    For larger sources, splits into batches and merges/deduplicates results.

    Returns:
        List of dicts: {source_table, column, target_table, target_column, confidence}.
        Empty list if no tables provided or no FKs inferred.
    """
    if not tables:
        return []

    if len(tables) <= TABLES_PER_BATCH:
        return _infer_batch(tables, client, emitter)

    all_candidates: list[dict] = []
    for i in range(0, len(tables), TABLES_PER_BATCH):
        batch = tables[i : i + TABLES_PER_BATCH]
        candidates = _infer_batch(batch, client, emitter)
        all_candidates.extend(candidates)

    return _deduplicate(all_candidates)


def _infer_batch(tables: list[Table], client: BedrockClient, emitter: EnrichmentMetricEmitter) -> list[dict]:
    """Single Bedrock call for one batch of tables."""
    user_prompt = build_relationship_prompt(tables)
    try:
        invocation = client.invoke(RELATIONSHIP_SYSTEM_PROMPT, user_prompt, max_tokens=8192)
        result = invocation.result
        emitter.emit_bedrock_invocation_metrics(
            stage="Pass2",
            latency_ms=invocation.latency_ms,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
        )
    except BedrockTruncationError as exc:
        logger.warning(
            "Pass 2 batch truncated (%d tables): model=%s output_tokens=%d requested_max_tokens=%d",
            len(tables),
            exc.model_id,
            exc.output_tokens,
            exc.max_tokens,
        )
        emitter.emit_bedrock_invocation_error(stage="Pass2", exc=exc)
        return []
    except Exception as exc:
        logger.warning("Pass 2 batch failed (%d tables)", len(tables), exc_info=True)
        emitter.emit_bedrock_invocation_error(stage="Pass2", exc=exc)
        return []

    if not isinstance(result, list):
        logger.warning("Pass 2 Bedrock response is not a list: %s", type(result))
        return []

    valid = []
    for item in result:
        if isinstance(item, dict) and item.get("source_table") and item.get("column") and item.get("target_table"):
            valid.append(item)

    logger.info("Pass 2 inferred %d FK candidates from %d tables", len(valid), len(tables))
    return valid


def _deduplicate(candidates: list[dict]) -> list[dict]:
    """Remove duplicate FK candidates, keeping the highest confidence."""
    best: dict[tuple, dict] = {}
    for candidate in candidates:
        key = (
            candidate["source_table"],
            candidate["column"],
            candidate["target_table"],
            candidate.get("target_column", ""),
        )
        existing = best.get(key)
        if not existing or candidate.get("confidence", 0) > existing.get("confidence", 0):
            best[key] = candidate
    return list(best.values())


def apply_inferred_relationships(tables: list[Table], candidates: list[dict]) -> int:
    """Write FK candidates to the corresponding Table objects.

    Each candidate is written as a ForeignKey with source=AI_INFERRED.
    Priority enforcement: if a DETERMINISTIC or STEWARD_SPECIFIED FK already
    exists for the same (table, column), the AI candidate is dropped.

    Returns the number of FKs applied.
    """
    if not candidates:
        return 0

    # Key by the same collision-safe display name used in the prompt, so the
    # LLM's source_table/target_table values resolve back to the exact Table
    # object (see _display_names — bare name when unique, schema-qualified when
    # two schemas share a table name).
    display = _display_names(tables)
    table_map = {display[id(t)]: t for t in tables}
    applied = 0
    skipped = 0

    for candidate in candidates:
        table = table_map.get(candidate["source_table"])
        if not table:
            continue
        if not any(c.name == candidate["column"] for c in table.columns):
            skipped += 1
            continue
        if candidate["target_table"] not in table_map:
            skipped += 1
            continue
        if _has_protected_fk(table, candidate["column"]):
            skipped += 1
            continue
        # Idempotency: on an incremental re-scan, a previously-inferred FK is
        # read back onto the Table, so skip if an AI_INFERRED FK already targets
        # the same column+target (else every re-scan appends a duplicate).
        if _has_equivalent_inferred_fk(table, candidate):
            skipped += 1
            continue
        table.foreign_keys.append(
            ForeignKey(
                column=candidate["column"],
                target_table=candidate["target_table"],
                target_column=candidate.get("target_column", ""),
                source=EnrichmentSource.AI_INFERRED,
                confidence=candidate.get("confidence", 0.0),
            )
        )
        applied += 1

    if skipped:
        logger.info("Pass 2 skipped %d candidates due to priority enforcement", skipped)
    logger.info("Pass 2 applied %d FK relationships", applied)
    return applied


_PROTECTED_FK_SOURCES: frozenset[str] = frozenset(
    {
        EnrichmentSource.DETERMINISTIC,
        EnrichmentSource.STEWARD_SPECIFIED,
    }
)


def _has_protected_fk(table: Table, column: str) -> bool:
    """Check if a higher-priority FK already exists for this table+column."""
    return any(fk.column == column and fk.source in _PROTECTED_FK_SOURCES for fk in table.foreign_keys)


def _has_equivalent_inferred_fk(table: Table, candidate: dict) -> bool:
    """True if an AI_INFERRED FK for the same column+target already exists.

    Prevents unbounded duplicate accumulation across incremental re-scans, where
    a prior run's inferred FK is read back onto the Table and would otherwise be
    re-appended (it is not in _PROTECTED_FK_SOURCES, so _has_protected_fk misses it).
    """
    target_col = candidate.get("target_column", "")
    return any(
        fk.source == EnrichmentSource.AI_INFERRED
        and fk.column == candidate["column"]
        and fk.target_table == candidate["target_table"]
        and fk.target_column == target_col
        for fk in table.foreign_keys
    )
