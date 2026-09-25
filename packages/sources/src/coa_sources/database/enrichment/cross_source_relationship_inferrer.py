# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-source foreign key inference (issue #1088).

The single-source Pass 2 (``relationship_inferrer.py``) only ever sees one
datasource's tables, so a relationship COA readily infers *within* a database is
missed when the same tables are split across separately-onboarded sources. This
pass runs over the tables of MULTIPLE datasources in a namespace at once and
infers the relationships that cross a source boundary.

Two differences from the within-source pass, both driven by #1088:

* **Descriptions are hints.** The prompt carries each table/column description,
  because a cross-source link is frequently spelled out in the metadata
  ("maps to a customer via account_xref") even when the column names alone are
  ambiguous.
* **Everything is written PENDING_REVIEW.** A cross-source relationship is never
  silently wired into the ontology — it is surfaced for a steward to approve
  (the induction gate in ``table_to_ontology.py`` withholds anything that is not
  approved/authoritative). Each carries its ``target_datasource_id`` (so the
  edge can resolve the parent class in the other source) and a ``provenance``
  note of what it was inferred from.
"""

from __future__ import annotations

import logging
from collections import Counter

from coa_common.bedrock import BedrockTruncationError
from coa_common.domain_models import EnrichmentSource, ForeignKey, ReviewStatus, Table

from coa_sources.database.enrichment.bedrock_client import BedrockClient
from coa_sources.database.enrichment.relationship_inferrer import (
    _has_protected_fk,
    _infer_batch_raw,
)
from coa_sources.database.pipeline.enrichment_metrics import EnrichmentMetricEmitter

logger = logging.getLogger(__name__)

CROSS_SOURCE_SYSTEM_PROMPT = """\
You are a database schema analyst. You are given tables from SEVERAL separate \
databases (sources), each tagged with a source alias (ds1, ds2, ...). Infer \
foreign key relationships that connect a column in one source to a table in a \
DIFFERENT source.

Respond ONLY with raw JSON — a JSON array of objects with this exact structure:
[
  {
    "source_table": "ds1.orders",
    "column": "customer_id",
    "target_table": "ds2.customers",
    "target_column": "id",
    "confidence": 0.9,
    "rationale": "orders.customer_id matches the customers primary key in ds2"
  }
]

Rules:
- ONLY infer relationships BETWEEN DIFFERENT sources (the source_table alias and \
target_table alias MUST differ). Same-source relationships are handled elsewhere; \
do not return them.
- Use BOTH the column names/types AND the table/column descriptions. A description \
that names the link (e.g. "maps to a customer via account_xref") is strong evidence.
- Only reference tables and columns that appear in the schema below, using the \
EXACT "dsN.table" labels shown.
- confidence is 0.0-1.0 based on how strongly names/types/descriptions agree.
- Prefer the target's primary key / id column unless a description says otherwise.
- If no cross-source relationships can be inferred, return an empty array []."""


def _source_aliases(tables: list[Table]) -> dict[str, str]:
    """Assign a stable ``dsN`` alias to each distinct datasource id (sorted)."""
    ids = sorted({t.data_source_id for t in tables if t.data_source_id})
    return {ds_id: f"ds{i + 1}" for i, ds_id in enumerate(ids)}


def _labels(tables: list[Table], alias_by_ds: dict[str, str]) -> dict[int, str]:
    """Map each table (by ``id()``) to a label unique across the whole union.

    ``{alias}.{name}`` normally; falls back to ``{alias}.{table_id}`` when a bare
    name repeats within the same datasource (multi-schema source), so the LLM —
    and the reverse lookup in :func:`apply_cross_source_relationships` — can tell
    every table apart.
    """
    per_ds_name_counts: dict[tuple[str, str], int] = Counter((t.data_source_id, t.name) for t in tables)
    out: dict[int, str] = {}
    for t in tables:
        alias = alias_by_ds.get(t.data_source_id, "ds?")
        bare = t.name if per_ds_name_counts[(t.data_source_id, t.name)] == 1 else t.table_id
        out[id(t)] = f"{alias}.{bare}"
    return out


def build_cross_source_prompt(tables: list[Table]) -> str:
    """Build a datasource-aliased schema summary carrying descriptions as hints."""
    alias_by_ds = _source_aliases(tables)
    return _build_prompt_for(tables, alias_by_ds, _labels(tables, alias_by_ds))


def _build_prompt_for(tables: list[Table], alias_by_ds: dict[str, str], labels: dict[int, str]) -> str:
    """Render the prompt for ``tables`` using PRE-ASSIGNED aliases/labels.

    Aliases are assigned once over the whole namespace union (see
    :func:`infer_cross_source_relationships`) so a per-pair prompt names each
    source and table exactly as every other prompt — and as ``apply`` — does.
    Only the aliases of sources present in ``tables`` are listed.
    """
    present = {t.data_source_id for t in tables if t.data_source_id}
    source_lines = [f"  {alias} = source {ds_id}" for ds_id, alias in alias_by_ds.items() if ds_id in present]

    schema_lines: list[str] = []
    for table in tables:
        # A malformed/corrupt table record must not abort the whole prompt: skip
        # it (with a warning) so the remaining tables are still inferred over.
        try:
            desc = (table.business_metadata.description or table.database_description or "").strip()
            desc_part = f' — "{desc}"' if desc else ""
            col_parts = []
            for c in table.columns:
                cdesc = (c.business_metadata.description or "").strip()
                col_parts.append(f"{c.name} ({c.data_type})" + (f' "{cdesc}"' if cdesc else ""))
            schema_lines.append(f"  {labels[id(table)]}{desc_part}: " + ", ".join(col_parts))
        except (AttributeError, TypeError, KeyError):
            logger.warning(
                "cross_source_prompt_skipped_table: table=%r has missing/malformed attributes",
                getattr(table, "name", table),
                exc_info=True,
            )

    return "Sources:\n" + "\n".join(source_lines) + "\n\nSchema:\n" + "\n".join(schema_lines)


def infer_cross_source_relationships(
    tables: list[Table],
    client: BedrockClient,
    emitter: EnrichmentMetricEmitter,
    *,
    focus_datasource_id: str | None = None,
) -> list[dict]:
    """Invoke Bedrock to infer cross-source FK candidates, one bounded prompt per source pair.

    A foreign key's two ends are always in DIFFERENT sources, so cross-source
    inference never needs the whole namespace in one prompt: it is exactly the
    set of source PAIRS. Prompting per pair bounds each call to two sources'
    tables (no unbounded union prompt that can overflow the context window or the
    output budget and silently return ``[]``), and lets the per-pair results be
    merged and de-duplicated.

    ``focus_datasource_id`` bounds the WORK: when given (the source that just
    finished enriching), only pairs involving that source are inferred —
    ``O(new x existing)`` per pass rather than re-inferring every pair in the
    namespace on every enrichment. When ``None``, all pairs are inferred (a full
    sweep).

    Returns a list of dicts: {source_table, column, target_table, target_column,
    confidence, rationale} where source_table/target_table are ``dsN.table``
    labels. The aliases are assigned once over the FULL union so they are
    identical in every pair prompt and in :func:`apply_cross_source_relationships`.
    Empty when there are fewer than two sources.
    """
    by_ds: dict[str, list[Table]] = {}
    for t in tables:
        if t.data_source_id:
            by_ds.setdefault(t.data_source_id, []).append(t)
    if len(by_ds) < 2:
        return []  # cross-source inference is meaningless with a single source

    # Aliases/labels over the whole union, so "ds3.orders" means the same table
    # in every pair prompt and when the candidates are resolved back.
    alias_by_ds = _source_aliases(tables)
    labels = _labels(tables, alias_by_ds)

    ds_ids = sorted(by_ds)
    if focus_datasource_id is not None:
        if focus_datasource_id not in by_ds:
            logger.info("cross-source inference: focus source %s has no tables — nothing to pair", focus_datasource_id)
            return []
        pairs = [(focus_datasource_id, other) for other in ds_ids if other != focus_datasource_id]
    else:
        pairs = [(a, b) for i, a in enumerate(ds_ids) for b in ds_ids[i + 1 :]]

    merged: list[dict] = []
    for a, b in pairs:
        pair_tables = by_ds[a] + by_ds[b]
        merged.extend(_infer_pair(pair_tables, alias_by_ds, labels, client, emitter))

    candidates = _deduplicate_candidates(merged)
    logger.info(
        "Cross-source inference produced %d candidates over %d source pair(s) (%d tables)",
        len(candidates),
        len(pairs),
        len(tables),
    )
    return candidates


# Output budget for one pair prompt. Larger than Pass 2's default 8192 because a
# pair of description-rich sources can legitimately yield many candidates; a
# truncated response is caught and reported rather than returned partially.
_PAIR_MAX_TOKENS = 16384


def _infer_pair(
    pair_tables: list[Table],
    alias_by_ds: dict[str, str],
    labels: dict[int, str],
    client: BedrockClient,
    emitter: EnrichmentMetricEmitter,
) -> list[dict]:
    """One bounded Bedrock call over the tables of exactly two sources."""
    prompt = _build_prompt_for(pair_tables, alias_by_ds, labels)
    try:
        result = _infer_batch_raw(
            CROSS_SOURCE_SYSTEM_PROMPT, prompt, client, emitter, stage="CrossSource", max_tokens=_PAIR_MAX_TOKENS
        )
    except BedrockTruncationError as exc:
        logger.warning("Cross-source pair inference truncated (%d tables) — skipping pair", len(pair_tables))
        emitter.emit_bedrock_invocation_error(stage="CrossSource", exc=exc)
        return []
    except Exception as exc:
        logger.warning("Cross-source pair inference failed (%d tables)", len(pair_tables), exc_info=True)
        emitter.emit_bedrock_invocation_error(stage="CrossSource", exc=exc)
        return []
    if not isinstance(result, list):
        logger.warning("Cross-source inference response is not a list: %s", type(result).__name__)
        return []
    valid = [
        item
        for item in result
        if isinstance(item, dict) and item.get("source_table") and item.get("column") and item.get("target_table")
    ]
    dropped = len(result) - len(valid)
    if dropped:
        # Visibility into model output quality: malformed candidates are dropped,
        # not silently, so a degrading prompt/model shows up in the logs.
        logger.warning("Cross-source inference dropped %d malformed candidate(s) of %d", dropped, len(result))
    return valid


def _deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """Merge per-pair results: one entry per (source, column, target, target_column); highest confidence wins."""
    best: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["source_table"], c["column"], c["target_table"], c.get("target_column", ""))
        cur = best.get(key)
        if cur is None or float(c.get("confidence", 0) or 0) > float(cur.get("confidence", 0) or 0):
            best[key] = c
    return list(best.values())


def _has_equivalent_cross_source_fk(table: Table, candidate: dict, target_name: str, target_ds: str) -> bool:
    """True if a cross-source FK for the same column+target already exists (idempotency)."""
    target_col = candidate.get("target_column", "")
    return any(
        fk.source == EnrichmentSource.AI_INFERRED
        and fk.column == candidate["column"]
        and fk.target_table == target_name
        and fk.target_column == target_col
        and fk.target_datasource_id == target_ds
        for fk in table.foreign_keys
    )


def apply_cross_source_relationships(tables: list[Table], candidates: list[dict]) -> int:
    """Write cross-source FK candidates onto the child tables as PENDING_REVIEW.

    A candidate is applied only when it is genuinely cross-source (source and
    target resolve to tables in DIFFERENT datasources), the source column exists,
    no higher-priority (deterministic/steward) FK already claims the column, and
    an equivalent cross-source FK is not already present (idempotent re-runs).

    Returns the number of relationships written.
    """
    if not candidates:
        return 0
    alias_by_ds = _source_aliases(tables)
    labels = _labels(tables, alias_by_ds)
    table_by_label = {labels[id(t)]: t for t in tables}

    applied = 0
    skipped = 0
    for cand in candidates:
        src = table_by_label.get(cand["source_table"])
        tgt = table_by_label.get(cand["target_table"])
        if src is None or tgt is None:
            skipped += 1
            continue
        if src.data_source_id == tgt.data_source_id:
            skipped += 1  # not cross-source — handled by the within-source pass
            continue
        if not any(c.name == cand["column"] for c in src.columns):
            skipped += 1
            continue
        if _has_protected_fk(src, cand["column"]):
            skipped += 1
            continue
        if _has_equivalent_cross_source_fk(src, cand, tgt.name, tgt.data_source_id):
            skipped += 1
            continue
        rationale = (cand.get("rationale") or "").strip()
        provenance = rationale or (
            f"cross-source inference: {cand['source_table']}.{cand['column']} -> {cand['target_table']}"
        )
        src.foreign_keys.append(
            ForeignKey(
                column=cand["column"],
                target_table=tgt.name,
                target_column=cand.get("target_column", ""),
                source=EnrichmentSource.AI_INFERRED,
                confidence=float(cand.get("confidence", 0.0)),
                review_status=ReviewStatus.PENDING_REVIEW,
                target_datasource_id=tgt.data_source_id,
                provenance=provenance,
            )
        )
        applied += 1

    if skipped:
        logger.info("Cross-source pass skipped %d candidates (same-source/guarded/duplicate)", skipped)
    logger.info("Cross-source pass wrote %d PENDING relationships", applied)
    return applied
