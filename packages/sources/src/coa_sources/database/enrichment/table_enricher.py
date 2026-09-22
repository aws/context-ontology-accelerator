# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-table LLM enrichment orchestrator."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from coa_common.bedrock import BedrockTruncationError, GuardrailBlockedError
from coa_common.config import resolve_region
from coa_common.datazone_forms import build_forms_input
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
)
from coa_common.metadata_store.reader import read_assets_for_datasource

from coa_sources.database.enrichment.bedrock_client import BedrockClient
from coa_sources.database.enrichment.prompts import SYSTEM_PROMPT, build_user_prompt
from coa_sources.database.pipeline.enrichment_metrics import (
    EnrichmentMetricEmitter,
)

logger = logging.getLogger(__name__)

AWS_REGION = resolve_region()
COLUMN_BATCH_SIZE = 50
COLUMN_BATCH_THRESHOLD = 80
MAX_WORKERS = 10
TABLE_TIMEOUT_SEC = int(os.getenv("ENRICHMENT_TABLE_TIMEOUT_SEC", "90"))
GUARDRAIL_SSM_PARAM = os.getenv("GUARDRAIL_SSM_PARAM", "")


def _resolve_guardrail_id() -> str | None:
    """Resolve Bedrock guardrail ID from SSM at runtime. Returns None if unavailable."""
    if not GUARDRAIL_SSM_PARAM:
        logger.info("GUARDRAIL_SSM_PARAM not set — proceeding without guardrails")
        return None
    try:
        ssm = boto3.client("ssm", region_name=AWS_REGION)
        value = ssm.get_parameter(Name=GUARDRAIL_SSM_PARAM)["Parameter"]["Value"]
        logger.info("Resolved guardrail ID from SSM: %s", value)
        return value
    except Exception:
        logger.warning(
            "Failed to resolve guardrail from SSM param %s — proceeding without guardrails",
            GUARDRAIL_SSM_PARAM,
            exc_info=True,
        )
        return None


# Description provenance values that AI enrichment must not overwrite. Mirrors
# the deterministic-PK/FK skip pattern (``_apply_primary_key`` / ``_apply_foreign_keys``):
#   * DETERMINISTIC      — pulled directly from the source system (Glue
#                          ``Description``/``Comment``, PostgreSQL
#                          ``pg_description``, Oracle ``ALL_*_COMMENTS``, …).
#   * CATALOG_EXISTING   — pulled from a 3rd-party catalog (Collibra, Atlan, …).
#   * STEWARD_EDITED /   — set by a human reviewer; the most authoritative.
#     STEWARD_SPECIFIED
_PROTECTED_DESCRIPTION_SOURCES: frozenset[str] = frozenset(
    {
        EnrichmentSource.DETERMINISTIC,
        EnrichmentSource.CATALOG_EXISTING,
        EnrichmentSource.STEWARD_EDITED,
        EnrichmentSource.STEWARD_SPECIFIED,
    }
)


def _prepare_table_context(table: Table) -> dict:
    """Extract existing metadata from a table for use as prompt context."""
    existing_description = table.business_metadata.description
    existing_pk = table.primary_key.columns if table.primary_key.columns else None
    existing_fks = [
        {"column": fk.column, "target_table": fk.target_table, "target_column": fk.target_column}
        for fk in table.foreign_keys
    ] or None
    extra_context = table.database_description or ""
    if not extra_context and table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC:
        extra_context = table.business_metadata.description or ""
    return {
        "existing_description": existing_description,
        "existing_pk": existing_pk,
        "existing_fks": existing_fks,
        "extra_context": extra_context,
        "extra_context_hint": "",
    }


def _has_pending(table: Table) -> bool:
    """True if the table or any of its columns still needs review.

    On a re-scan, a fully-reviewed table (its table-level description and every
    column already APPROVED or REJECTED) has nothing to regenerate, so
    enrichment skips it entirely — preserving the steward's approvals and
    avoiding a needless Bedrock call. On a first scan the table is always
    PENDING_REVIEW, so nothing is skipped and behaviour is unchanged.
    """
    if table.business_metadata.review_status == ReviewStatus.PENDING_REVIEW:
        return True
    return any(c.business_metadata.review_status == ReviewStatus.PENDING_REVIEW for c in table.columns)


def run(
    datasource_id: str, namespace_id: str, domain_id: str, scan_type: str, emitter: EnrichmentMetricEmitter
) -> dict:
    """Enrich all tables for a data source via Bedrock LLM calls.

    All Bedrock calls (both single-table and column-batch) are submitted as
    peers to a shared thread pool capped at MAX_WORKERS. This avoids deadlocks
    and ensures column batches for large tables run in parallel.

    Returns:
        Dict with counts: tables_enriched, tables_failed, tables_skipped_unchanged.
    """
    tables = read_assets_for_datasource(domain_id, namespace_id, datasource_id)
    if not tables:
        logger.info("No tables found for datasource %s", datasource_id)
        return {"tables_enriched": 0, "tables_failed": 0, "tables_skipped_unchanged": 0, "failed_table_ids": []}

    guardrail_id = _resolve_guardrail_id()
    client = BedrockClient(read_timeout=TABLE_TIMEOUT_SEC, guardrail_id=guardrail_id)

    # PLAN: build all Bedrock call work items
    work_items: list[tuple[Table, list[Column], int, dict]] = []
    table_batch_counts: dict[str, int] = {}

    for table in tables:
        if not _has_pending(table):
            # Re-scan: nothing left to review on this table, so leave it exactly
            # as the steward approved it (no regeneration, no Bedrock call).
            continue
        ctx = _prepare_table_context(table)
        columns = table.columns
        if len(columns) <= COLUMN_BATCH_THRESHOLD:
            work_items.append((table, columns, 0, ctx))
            table_batch_counts[table.table_id] = 1
        else:
            batch_count = 0
            for start in range(0, len(columns), COLUMN_BATCH_SIZE):
                batch = columns[start : start + COLUMN_BATCH_SIZE]
                work_items.append((table, batch, batch_count, ctx))
                batch_count += 1
            table_batch_counts[table.table_id] = batch_count

    # EXECUTE: submit all Bedrock calls to shared pool
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _call_bedrock,
                table.name,
                table.database,
                cols,
                client,
                emitter,
                **ctx,
            ): (table, batch_idx)
            for table, cols, batch_idx, ctx in work_items
        }

        # COLLECT: gather results grouped by table
        table_results: dict[str, list[tuple[int, dict]]] = {}
        table_usage: dict[str, dict] = defaultdict(lambda: {"latency_ms": 0.0, "input_tokens": 0, "output_tokens": 0})
        failed_tables: set[str] = set()

        for future in as_completed(futures):
            table, batch_idx = futures[future]
            try:
                parsed, latency_ms, input_tokens, output_tokens = future.result()
                if table.table_id not in failed_tables:
                    table_results.setdefault(table.table_id, []).append((batch_idx, parsed))
                table_usage[table.table_id]["latency_ms"] += latency_ms
                table_usage[table.table_id]["input_tokens"] += input_tokens
                table_usage[table.table_id]["output_tokens"] += output_tokens
            except GuardrailBlockedError:
                failed_tables.add(table.table_id)
                logger.warning("Guardrail blocked table %s (batch %d)", table.table_id, batch_idx)
                emitter.emit_bedrock_invocation_error(stage="Pass1", exc=GuardrailBlockedError("blocked"))
            except BedrockTruncationError as exc:
                failed_tables.add(table.table_id)
                logger.warning(
                    "Bedrock truncated table %s (batch %d): model=%s output_tokens=%d requested_max_tokens=%d",
                    table.table_id,
                    batch_idx,
                    exc.model_id,
                    exc.output_tokens,
                    exc.max_tokens,
                )
                emitter.emit_bedrock_invocation_error(stage="Pass1", exc=exc)
            except Exception as exc:
                failed_tables.add(table.table_id)
                logger.exception("Failed to enrich table %s (batch %d)", table.table_id, batch_idx)
                emitter.emit_bedrock_invocation_error(stage="Pass1", exc=exc)

    # APPLY: process results for fully-succeeded tables
    succeeded_tables: list[Table] = []
    table_map = {t.table_id: t for t in tables}

    for table_id, results in table_results.items():
        if table_id in failed_tables:
            continue
        if len(results) < table_batch_counts[table_id]:
            failed_tables.add(table_id)
            continue
        table = table_map[table_id]
        results.sort(key=lambda x: x[0])
        _apply_table_metadata(table, results[0][1])
        _apply_primary_key(table, results[0][1])
        all_column_results: list[dict] = []
        for _, result in results:
            all_column_results.extend(result.get("columns", []))
        _apply_column_metadata(table, all_column_results)
        succeeded_tables.append(table)

    # Structured log per table for offline analysis
    for table_id, usage in table_usage.items():
        status = "failed" if table_id in failed_tables else "success"
        logger.info(
            "table_enrichment_complete: %s",
            {
                "table_id": table_id,
                "pass": "Pass1",
                "latency_ms": round(usage["latency_ms"], 1),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "status": status,
            },
        )

    # PASS 2: Cross-table FK inference on successfully enriched tables
    if succeeded_tables:
        from coa_sources.database.enrichment.relationship_inferrer import (
            apply_inferred_relationships,
            infer_relationships,
        )

        try:
            fk_candidates = infer_relationships(succeeded_tables, client, emitter=emitter)
            apply_inferred_relationships(succeeded_tables, fk_candidates)
        except Exception:
            logger.exception("Pass 2 FK inference failed; Pass 1 results preserved")

    enriched = len(succeeded_tables)
    failed = len(failed_tables)
    skipped_unchanged = len(tables) - enriched - failed

    # Alarm surface for the silent-failure mode this pass is prone to: the LLM
    # returns well-formed JSON with synonyms/tags but no table description, so
    # the table counts as "enriched" while landing in the catalog undescribed.
    missing_descriptions = sum(1 for t in succeeded_tables if not t.business_metadata.description)
    emitter.emit_metric("TablesMissingDescription", missing_descriptions, "Count")
    if missing_descriptions:
        logger.warning(
            "%d of %d enriched tables have no description — check the Pass 1 prompt contract",
            missing_descriptions,
            enriched,
        )

    # Write only successfully enriched tables back to DataZone
    _write_enriched_assets(succeeded_tables, domain_id, namespace_id)

    logger.info(
        "Enrichment complete: enriched=%d failed=%d skipped_unchanged=%d",
        enriched,
        failed,
        skipped_unchanged,
    )
    return {
        "tables_enriched": enriched,
        "tables_failed": failed,
        "tables_skipped_unchanged": skipped_unchanged,
        # The db.table ids that errored (guardrail block or parse/timeout) and
        # were written back WITHOUT enrichment. The aggregate `failed` count
        # alone hides which tables regressed; the caller records these names so
        # a steward can see them at review time.
        "failed_table_ids": sorted(failed_tables),
    }


def _call_bedrock(
    table_name: str,
    database: str,
    columns: list[Column],
    client: BedrockClient,
    emitter: EnrichmentMetricEmitter,
    *,
    existing_description: str = "",
    existing_pk: list[str] | None = None,
    existing_fks: list[dict] | None = None,
    extra_context: str = "",
    extra_context_hint: str = "",
) -> tuple[dict, float, int, int]:
    """Build prompt and invoke Bedrock, returning (parsed, latency_ms, input_tokens, output_tokens).

    Emits per-call metrics (latency, tokens) via *emitter*.
    """
    user_prompt = build_user_prompt(
        table_name,
        database,
        columns,
        existing_description=existing_description,
        existing_pk=existing_pk,
        existing_fks=existing_fks,
        extra_context=extra_context,
        extra_context_hint=extra_context_hint,
    )
    max_tokens = 4096
    if extra_context:
        max_tokens = 16384 if extra_context_hint else 8192

    result = client.invoke(SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens)

    emitter.emit_bedrock_invocation_metrics(
        stage="Pass1",
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    parsed: dict = result.result  # type: ignore[assignment]
    return parsed, result.latency_ms, result.input_tokens, result.output_tokens


def _apply_result(table: Table, result: dict, columns: list[Column]) -> None:
    """Apply full LLM result to table."""
    _apply_table_metadata(table, result)
    _apply_primary_key(table, result)
    _apply_column_metadata(table, result.get("columns", []))


def _apply_table_metadata(table: Table, result: dict) -> None:
    """Apply table-level business metadata from LLM result.

    Descriptions whose provenance is in ``_PROTECTED_DESCRIPTION_SOURCES``
    (source system, 3rd-party catalog, or steward) are authoritative — the AI
    may still contribute synonyms/glossary/tags when the existing record left
    them empty, but the description itself is preserved.
    """
    table_meta = result.get("table", {})
    existing = table.business_metadata
    preserve_description = existing.enrichment_source in _PROTECTED_DESCRIPTION_SOURCES and bool(existing.description)

    if preserve_description:
        # Keep the authoritative description and source attribution; merge in
        # any AI-generated synonyms/glossary/tags only if they were missing.
        # NOTE: Do NOT auto-approve the table here. The table's review_status
        # must remain PENDING_REVIEW until all child columns reach terminal
        # states. The steward (or bulk approve) will flip it via review_logic.
        table.business_metadata = BusinessMetadata(
            description=existing.description,
            synonyms=existing.synonyms or table_meta.get("synonyms", []),
            glossary_terms=existing.glossary_terms or table_meta.get("glossary_terms", []),
            tags=existing.tags or table_meta.get("tags", []),
            enrichment_source=existing.enrichment_source,
            review_status=existing.review_status,
            confidence=existing.confidence or 1.0,
        )
        return

    # Already-reviewed AI table descriptions are preserved verbatim on a
    # re-scan: an APPROVED or REJECTED table is never regenerated. (Reached only
    # when the table is still in the work set because some column is PENDING.)
    if existing.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        return

    ai_description = table_meta.get("description") or ""
    if not ai_description:
        # Do NOT fabricate a description here. A synthesized string like
        # "Table orders in sales" carries no information the table name does not
        # already carry, but would be persisted as AI_GENERATED — passing it off
        # to stewards as real enrichment and diluting the description embeddings
        # used for semantic search / NL→SQL. Leave it empty, make it loud, and
        # let the prompt contract plus the TablesMissingDescription metric
        # surface regressions.
        logger.warning(
            "table_description_missing: LLM returned no table description for %s.%s "
            "(synonyms=%d tags=%d) — leaving description empty",
            table.database,
            table.name,
            len(table_meta.get("synonyms") or []),
            len(table_meta.get("tags") or []),
        )

    table.business_metadata = BusinessMetadata(
        description=ai_description,
        synonyms=table_meta.get("synonyms", []),
        glossary_terms=table_meta.get("glossary_terms", []),
        tags=table_meta.get("tags", []),
        enrichment_source=EnrichmentSource.AI_GENERATED,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _apply_primary_key(table: Table, result: dict) -> None:
    """Apply primary key inference from LLM result.

    Deterministic keys discovered from the source schema are authoritative and
    are never overwritten by AI inference.
    """
    if table.primary_key.source == EnrichmentSource.DETERMINISTIC:
        return
    pk = result.get("primary_key", {})
    if pk.get("columns"):
        table.primary_key = PrimaryKey(
            columns=pk["columns"],
            source=EnrichmentSource.AI_INFERRED,
            confidence=pk.get("confidence", 0.0),
        )


def _apply_foreign_keys(table: Table, result: dict) -> None:
    """Apply foreign key inferences from LLM result.

    Skipped entirely when deterministic foreign keys already exist — the
    source schema is authoritative.
    """
    if any(fk.source == EnrichmentSource.DETERMINISTIC for fk in table.foreign_keys):
        return
    fks = result.get("foreign_keys", [])
    for fk in fks:
        if fk.get("column") and fk.get("target_table"):
            table.foreign_keys.append(
                ForeignKey(
                    column=fk["column"],
                    target_table=fk["target_table"],
                    target_column=fk.get("target_column", ""),
                    source=EnrichmentSource.AI_INFERRED,
                    confidence=fk.get("confidence", 0.0),
                )
            )


def _apply_column_metadata(table: Table, column_results: list[dict]) -> None:
    """Apply column-level business metadata from LLM results.

    A column whose ``business_metadata.enrichment_source`` is in
    ``_PROTECTED_DESCRIPTION_SOURCES`` (source system, 3rd-party catalog, or
    steward) and has a non-empty description is preserved. The LLM result
    contributes only synonyms/glossary/tags when the existing record left
    them empty.
    """
    col_map = {c["name"]: c for c in column_results if "name" in c}
    for col in table.columns:
        if col.name not in col_map:
            continue
        meta = col_map[col.name]
        existing = col.business_metadata

        if existing.enrichment_source in _PROTECTED_DESCRIPTION_SOURCES and existing.description:
            col.business_metadata = BusinessMetadata(
                description=existing.description,
                synonyms=existing.synonyms or meta.get("synonyms", []),
                glossary_terms=existing.glossary_terms or meta.get("glossary_terms", []),
                tags=existing.tags or meta.get("tags", []),
                enrichment_source=existing.enrichment_source,
                review_status=existing.review_status,
                confidence=existing.confidence or 1.0,
            )
            continue

        # Already-reviewed columns (APPROVED/REJECTED) are preserved verbatim on
        # a re-scan — only PENDING columns get fresh AI.
        if existing.review_status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
            continue

        col.business_metadata = BusinessMetadata(
            description=meta.get("description", ""),
            synonyms=meta.get("synonyms", []),
            glossary_terms=meta.get("glossary_terms", []),
            tags=meta.get("tags", []),
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.PENDING_REVIEW,
            confidence=meta.get("confidence", 0.0),
        )


def _write_enriched_assets(tables: list[Table], domain_id: str, project_id: str) -> None:
    """Write enriched tables back to DataZone as asset revisions."""
    from coa_common.metadata_store import SMUSClient

    client = SMUSClient(
        domain_id=domain_id,
        region_name=AWS_REGION,
        assume_role_arn=os.environ.get("PROJECT_ACCESS_ROLE_ARN"),
        session_name="enrichment-task",
    )

    for table in tables:
        if not table.data_source_id:
            logger.warning("Skipping table %s with empty data_source_id", table.table_id)
            continue
        asset_name = f"{table.data_source_id}:{table.table_id}"
        try:
            # find_asset_by_name, not a top-1 search: a top-1 lookup on the full
            # asset name can rank a sibling table first and report the asset as
            # missing, and the miss branch below drops this table's enrichment
            # write entirely rather than erroring, so a ranking miss silently
            # loses generated metadata.
            asset = client.find_asset_by_name(project_id=project_id, name=asset_name)
            if asset is None:
                logger.warning(
                    "Asset not found for enriched table %s (searched '%s'). Skipping.", table.table_id, asset_name
                )
                continue

            client.create_asset_revision(
                asset_id=asset.asset_id,
                name=asset_name,
                description=table.business_metadata.description,
                forms_input=build_forms_input(table),
            )
        except Exception:
            logger.exception("Failed to write enriched asset %s to project %s", asset_name, project_id)
