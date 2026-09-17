# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Enrichment step handler — ECS Fargate task entry point.

Reads discovered metadata from SageMaker Catalog (DataZone), runs
AI-powered enrichment via Amazon Bedrock, and writes enriched business
metadata back as asset revisions.

Environment variables (injected by Step Functions container overrides):
    DATASOURCE_ID: The data source being enriched
    SCAN_JOB_ID: The current scan job ID
    NAMESPACE_ID: The namespace owning this data source
    SCAN_TYPE: "full" or "incremental"
    SMUS_DOMAIN_ID: DataZone domain ID
    DATASOURCES_TABLE: DynamoDB table for data source configs
    SCAN_JOBS_TABLE: DynamoDB table for scan job tracking
"""

from __future__ import annotations

import logging
import os
import sys
import time

from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.database.pipeline.enrichment_metrics import (
    EnrichmentContext,
    EnrichmentMetricEmitter,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stdout)


def handler() -> None:
    """ECS task main entry point for metadata enrichment."""
    datasource_id = os.environ["DATASOURCE_ID"]  # "DS#<uuid>"
    scan_job_id = os.environ["SCAN_JOB_ID"]
    namespace_id = os.environ["NAMESPACE_ID"]
    scan_type = os.environ.get("SCAN_TYPE", "full")
    scan_job_sk = os.environ.get("SCAN_JOB_SK", scan_job_id)  # ISO timestamp SK
    domain_id = os.environ["SMUS_DOMAIN_ID"]
    # Set by the Step Functions enrichment step from the execution input; a
    # re-scan of an already-approved source ends in RESCAN_REVIEW (the steward
    # reviews the drift) instead of PENDING_REVIEW. Absent/"false" on a first scan.
    is_rescan = os.environ.get("IS_RESCAN", "").strip().lower() == "true"
    # Passed from discovery's reviewNeeded via the RESCAN_REVIEW_NEEDED container
    # override. A re-scan that found nothing for the steward to review (no drift
    # and no carried-forward orphaned tables) returns straight to APPROVED instead
    # of parking the source in RESCAN_REVIEW with an empty review. Default "true"
    # so an older infra revision without the override stays on the review side.
    rescan_review_needed = os.environ.get("RESCAN_REVIEW_NEEDED", "true").strip().lower() != "false"
    if not is_rescan:
        terminal_status = SourceStatus.PENDING_REVIEW
    elif rescan_review_needed:
        terminal_status = SourceStatus.RESCAN_REVIEW
    else:
        terminal_status = SourceStatus.APPROVED

    # Strip "DS#" prefix to get the bare source UUID
    source_id = datasource_id.removeprefix("DS#")

    logger.info(
        "Enrichment started: datasource=%s scan_job=%s namespace=%s type=%s",
        datasource_id,
        scan_job_id,
        namespace_id,
        scan_type,
    )

    # New sources-table key schema: PK=NS#{namespaceId}, SK=SRC#{sourceId}
    source_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}
    # source-scan-jobs key schema: PK=SRC#{sourceId}, SK=<ISO timestamp>
    scan_job_key = {"PK": f"SRC#{source_id}", "SK": scan_job_sk}

    # Resolve DataZone project ID from namespace
    from coa_common.dao import DynamoDBDAO

    region = os.environ.get("AWS_REGION", "us-east-1")
    namespaces_table = os.environ.get("NAMESPACES_TABLE")
    if not namespaces_table:
        raise ValueError("NAMESPACES_TABLE environment variable is required")
    ns_dao = DynamoDBDAO(namespaces_table, region=region)
    ns_item = ns_dao.get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not ns_item or "dataZoneProjectId" not in ns_item:
        raise ValueError(f"Namespace {namespace_id} not found or missing dataZoneProjectId")
    project_id = ns_item["dataZoneProjectId"]

    ds_dao = DynamoDBDAO(os.environ["SOURCES_TABLE"], region=region)

    # Read source record so we can honour the per-source metadata enrichment
    # toggle. The discovery step requires the source to exist before this
    # task runs; if it's missing here the source was concurrently deleted
    # (or never created), and continuing would either crash on a missing
    # asset or write a partial record back via the DDB update below. Match
    # the discovery handler's pattern and fail loudly so the SFN error
    # chain transitions the source/scan job to SCAN_FAILED.
    source_item = ds_dao.get(source_key)
    if source_item is None:
        raise ValueError(f"Source record not found: {source_key}")

    # Default-enabled when the field is absent so legacy records (created
    # before the toggle existed) keep their historical behaviour.
    enrichment_enabled = bool(source_item.get("metadataEnrichmentEnabled", True))

    if not enrichment_enabled:
        logger.info(
            "Enrichment skipped (metadataEnrichmentEnabled=false): datasource=%s",
            datasource_id,
        )
        ds_dao.update(
            key=source_key,
            update_fields={"status": terminal_status},
            condition="attribute_exists(PK)",
        )
        return

    # Mark source as ENRICHING. Conditional update guards against writing to
    # a record deleted concurrently (race vs. delete-source): if the row no
    # longer exists we surface a ConditionalCheckFailedException rather than
    # silently re-creating a tombstoned key. Matches discovery_handler.
    ds_dao.update(
        key=source_key,
        update_fields={"status": SourceStatus.ENRICHING},
        condition="attribute_exists(PK)",
    )

    ctx = EnrichmentContext(source_item=source_item, namespace_id=namespace_id)
    emitter = EnrichmentMetricEmitter(ctx)

    try:
        from coa_sources.database.enrichment.table_enricher import run as enrich_tables

        enrich_start = time.monotonic()
        result = enrich_tables(
            datasource_id=datasource_id,
            namespace_id=project_id,
            domain_id=domain_id,
            scan_type=scan_type,
            emitter=emitter,
        )
        enrich_duration_ms = (time.monotonic() - enrich_start) * 1000

        emitter.emit_metric("EnrichmentJobDurationMs", enrich_duration_ms, "Milliseconds")
        emitter.emit_metric("TablesEnriched", result["tables_enriched"], "Count")
        emitter.emit_metric("TablesFailed", result["tables_failed"], "Count")
        emitter.emit_metric("TablesSkippedUnchanged", result["tables_skipped_unchanged"], "Count")
        emitter.emit_enrichment_estimated_cost()

        if result["tables_enriched"] == 0 and result["tables_failed"] == 0:
            ds_item = ds_dao.get(source_key)
            discovered = (ds_item or {}).get("tablesDiscovered", 0)
            if discovered:
                logger.warning(
                    "Discovery found %d tables but enrichment found 0 assets in DataZone. "
                    "Assets may not have been indexed yet or search query mismatch.",
                    discovered,
                )

        # Per-table enrichment failures (guardrail block, truncated-JSON parse
        # error, per-table timeout) do NOT fail the job: the table is written
        # back without enrichment and the source still advances to review. The
        # aggregate TablesFailed metric alone hides WHICH tables regressed, so a
        # steward sees a blank table that looks identical to a legitimately-empty
        # one. Record the names on this scan-job row (surfaced by
        # GET .../scan/{jobId}) and log them loudly so the partial failure is
        # visible rather than silent.
        failed_table_ids = result.get("failed_table_ids", [])
        if result["tables_failed"]:
            logger.warning(
                "Enrichment partial failure: %d of %d table(s) were written back without "
                "enrichment (guardrail block or parse/timeout error): %s",
                result["tables_failed"],
                result["tables_enriched"] + result["tables_failed"],
                failed_table_ids,
            )
            scan_dao = DynamoDBDAO(os.environ["SOURCE_SCAN_JOBS_TABLE"], region=region)
            # Best-effort diagnostic write: a concurrently-deleted scan-job row
            # must not crash an otherwise-successful enrichment (that would flip
            # the source to SCAN_FAILED and mask the tables that DID enrich).
            scan_dao.update(
                key=scan_job_key,
                update_fields={
                    "enrichmentPartialFailure": True,
                    "enrichmentFailedTables": failed_table_ids,
                },
                condition="attribute_exists(PK)",
                raise_on_error=False,
            )

        # Mark source terminal after successful enrichment: PENDING_REVIEW for a
        # first scan, RESCAN_REVIEW for a re-scan of an already-approved source.
        ds_dao.update(
            key=source_key,
            update_fields={"status": terminal_status},
            condition="attribute_exists(PK)",
        )
    except Exception as exc:
        logger.exception("Enrichment failed for datasource %s", datasource_id)
        from datetime import UTC, datetime

        # Failure-path updates use condition + raise_on_error=False: if the
        # row was deleted concurrently we still want to surface the original
        # enrichment exception, not a ConditionalCheckFailedException from
        # cleanup. Matches discovery_handler.
        ds_dao.update(
            key=source_key,
            update_fields={
                "status": SourceStatus.SCAN_FAILED,
                "lastScanJobId": scan_job_sk,
                "lastScanAt": datetime.now(UTC).isoformat(),
            },
            condition="attribute_exists(PK)",
            raise_on_error=False,
        )
        scan_dao = DynamoDBDAO(os.environ["SOURCE_SCAN_JOBS_TABLE"], region=region)
        scan_dao.update(
            key=scan_job_key,
            update_fields={"errorMessage": str(exc)},
            condition="attribute_exists(PK)",
            raise_on_error=False,
        )
        raise RuntimeError(str(exc)) from exc

    logger.info(
        "Enrichment complete: datasource=%s enriched=%d failed=%d",
        datasource_id,
        result["tables_enriched"],
        result["tables_failed"],
    )


if __name__ == "__main__":
    handler()
