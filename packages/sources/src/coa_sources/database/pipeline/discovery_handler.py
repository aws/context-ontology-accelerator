# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery step handler — invoked by the scan pipeline state machine.

Reads the data source configuration from DynamoDB, dispatches to the
appropriate connector, discovers metadata, and persists to DataZone.

Input (from Step Functions):
    {
        "datasourceId": "DS#<id>",
        "scanJobId": "SCAN#<jobId>",
        "namespaceId": "<namespace-id>",
        "scanType": "full" | "incremental",
        "isRescan": true   # optional; set only for a re-scan of an already-
                           # approved source. When true, discovery MERGES onto
                           # the live assets (preserving curated metadata)
                           # instead of overwriting them. Absent/false on a
                           # first scan.
        "hadOpenRescan": true  # optional; true only when the source was already
                           # in RESCAN_REVIEW (a prior re-scan still open and
                           # un-approved). Only then is the S3 backup blob the
                           # approved pre-image to reconstruct the diff baseline
                           # from. When re-scanning from APPROVED (false/absent),
                           # the live assets ARE the approved baseline and any
                           # leftover backup blob is stale and must be ignored.
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC
from typing import Any

from coa_common.constants import datasource_external_id
from coa_common.dao import DynamoDBDAO
from coa_common.domain_models import DiscoveredMetadata, ReviewStatus
from coa_common.metadata_store.reader import read_assets_for_datasource
from coa_common.s3 import get_s3_client, read_file_bytes, upload_json
from coa_control_plane_server.models.source_status import SourceStatus
from coa_control_plane_server.models.source_sub_type import SourceSubType

from coa_sources.database.connectors import (
    MetadataConnector,
    get_connector,
)
from coa_sources.database.errors import PermanentScanError, TransientScanError, is_permanent_scan_error
from coa_sources.database.glue_ownership import (
    GlueOwnershipError,
    assert_namespace_may_catalog,
)
from coa_sources.database.metadata_writer import write_to_datazone
from coa_sources.database.metrics import emit_metric
from coa_sources.database.rescan import diff_tables, merged_write_set, reconstruct_approved_baseline
from coa_sources.database.rescan_backup import backup_s3_key, build_rescan_backup
from coa_sources.database.secret_binding import require_secret_namespace_binding

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

DATASOURCES_TABLE = os.environ["SOURCES_TABLE"]
SCAN_JOBS_TABLE = os.environ["SOURCE_SCAN_JOBS_TABLE"]
SMUS_DOMAIN_ID = os.environ["SMUS_DOMAIN_ID"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# S3 bucket for the re-scan backup blob (pre-rescan asset forms + change-set),
# read back by the approve/reject worker. Present on both first-scan and re-scan
# invocations of this Lambda; only the re-scan path uses it.
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")

# Guardrail: maximum number of tables a single source may register as DataZone
# assets in one discovery pass. Above this, the scan fails fast with an
# actionable message rather than silently hitting the Lambda timeout. 0 (or
# unset) disables the cap. Discovery itself is cheap; the cost is the per-table
# DataZone asset write, so the cap is sized to what the parallel writer clears
# well within the 15-minute Lambda timeout.
try:
    MAX_TABLES_PER_SOURCE = int(os.environ.get("MAX_TABLES_PER_SOURCE", "0"))
except ValueError:
    logger.warning(
        "Invalid MAX_TABLES_PER_SOURCE=%r; expected an integer. Disabling the table cap (0).",
        os.environ.get("MAX_TABLES_PER_SOURCE"),
    )
    MAX_TABLES_PER_SOURCE = 0

# Cap on the failed-table names stored on the scan-job record. A DynamoDB item is
# limited to 400 KB and this list is a signal, not an inventory — the count next
# to it is always exact.
_MAX_REPORTED_FAILED_TABLES = 50

_ds_dao: DynamoDBDAO | None = None
_scan_dao: DynamoDBDAO | None = None
_ns_dao: DynamoDBDAO | None = None


def _get_ds_dao() -> DynamoDBDAO:
    global _ds_dao
    if _ds_dao is None:
        _ds_dao = DynamoDBDAO(DATASOURCES_TABLE, region=AWS_REGION)
    return _ds_dao


def _get_scan_dao() -> DynamoDBDAO:
    global _scan_dao
    if _scan_dao is None:
        _scan_dao = DynamoDBDAO(SCAN_JOBS_TABLE, region=AWS_REGION)
    return _scan_dao


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao
    if _ns_dao is None:
        table_name = os.environ.get("NAMESPACES_TABLE", "")
        if not table_name:
            raise RuntimeError("NAMESPACES_TABLE environment variable not set")
        _ns_dao = DynamoDBDAO(table_name, region=AWS_REGION)
    return _ns_dao


def _read_existing_backup(source_id: str) -> dict[str, Any] | None:
    """Read the current re-scan backup blob (the last-approved pre-image), or None.

    Called ONLY when the source was in RESCAN_REVIEW at re-scan time (a prior
    re-scan is still open), so the live assets are an interim, un-approved state
    and this blob is the approved pre-image to diff against (see
    ``rescan.reconstruct_approved_baseline``). The caller gates on that condition,
    NOT on blob presence: a blob can also be left behind on an APPROVED source
    (re-scanned before delete-on-resolve shipped, or a paged-approve gap), and
    such a stale blob must never drive the baseline. A missing/unreadable blob or
    any read error returns None. Best-effort by design: it must not fail the scan.
    """
    if not BUCKET_NAME:
        return None
    try:
        raw = read_file_bytes(get_s3_client(), BUCKET_NAME, backup_s3_key(source_id))
        return json.loads(raw)
    except Exception:
        logger.info(
            "No readable re-scan backup for %s; treating live assets as the approved baseline",
            source_id,
        )
        return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for the Discovery step."""
    datasource_id = event["datasourceId"]  # "DS#<uuid>"
    scan_job_id = event["scanJobId"]
    namespace_id = event["namespaceId"]
    scan_type = event.get("scanType", "full")
    scan_job_sk = event.get("scanJobSK", scan_job_id)  # ISO timestamp SK for source-scan-jobs
    # Re-scan of an already-approved source (set by the rescan trigger; "false"
    # on a first scan). Drives the merge-onto-live-assets path below. The trigger
    # normalizes this to a string, so parse it as one (a bare bool works too).
    is_rescan = str(event.get("isRescan", "")).strip().lower() == "true"
    # True ONLY when the source was already in RESCAN_REVIEW when this re-scan was
    # triggered — i.e. a PRIOR re-scan is still open and un-approved, so the live
    # assets are that interim merge and the S3 backup blob holds the approved
    # pre-image. Gates the backup read below. Normalized as a string by the
    # trigger (a bare bool works too); defaults to false when absent.
    had_open_rescan = str(event.get("hadOpenRescan", "")).strip().lower() == "true"

    # Strip "DS#" prefix to get the bare source UUID
    source_id = datasource_id.removeprefix("DS#")

    logger.info(
        "Discovery started: datasource=%s scan_job=%s type=%s",
        datasource_id,
        scan_job_id,
        scan_type,
    )

    # New sources-table key schema: PK=NS#{namespaceId}, SK=SRC#{sourceId}
    source_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}
    # source-scan-jobs key schema: PK=SRC#{sourceId}, SK=<ISO timestamp>
    scan_job_key = {"PK": f"SRC#{source_id}", "SK": scan_job_sk}

    try:
        # Fetch source configuration from sources-table
        item = _get_ds_dao().get(source_key)
        if not item:
            raise ValueError(f"Data source not found: {datasource_id}")

        # Mark source as SCANNING. Conditional update guards against
        # writing to a record that has been deleted concurrently (race vs.
        # delete-source): if the row no longer exists we surface a
        # ConditionalCheckFailedException rather than silently re-creating
        # an attribute on a tombstoned key.
        _get_ds_dao().update(
            key=source_key,
            update_fields={"status": SourceStatus.SCANNING},
            condition="attribute_exists(PK)",
        )

        source_type = item["sourceSubType"]  # GLUE_DATABASE or JDBC_DATABASE
        raw_config = item.get("configuration", "{}")
        config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config

        # Re-verify the credential secret still lists this namespace BEFORE
        # anything reads it. Registration checks the ARN the caller supplies, but
        # this handler takes the ARN from the stored row — so registration alone
        # does not cover a row written before the binding rule existed, or a secret
        # whose tag list changed after the source was registered. Without this
        # re-check the discovery connector would fetch the secret and open a
        # connection to the host in the same row, which is where the credentials
        # leave the account.
        #
        # The IAM conditions on this role can only assert the tag EXISTS — a shared
        # execution role has no per-request namespace identity — so the membership
        # test has to happen here.
        #
        # Raises on refusal: the scan fails and the source goes SCAN_FAILED, which
        # is the signal the operator needs (add this namespace to the secret's tag,
        # then re-scan). Cross-account secrets are skipped — see secret_binding.
        require_secret_namespace_binding(config.get("credentialSecretArn"), namespace_id, datasource_id)

        # Athena federation for JDBC sources is provisioned by a separate,
        # dedicated Step Functions step (FederationProvisionerFn) — kept out of
        # discovery so its Lake Formation admin privilege stays isolated.

        # Dispatch to the appropriate connector. All JDBC engines (incl. Snowflake)
        # are discovered directly via their driver dialect (see connectors/dialects.py).
        connector = get_connector(source_type)
        scan_start = time.monotonic()
        metadata = _discover(connector, config, datasource_id, namespace_id, source_type, item)

        # Guardrail: fail fast on sources too large to register within the
        # Lambda timeout. Discovery is cheap; the per-table DataZone asset write
        # is the bottleneck. Surface an actionable message (scope with
        # schema/table filters) rather than letting the pipeline time out and
        # retry fruitlessly.
        if MAX_TABLES_PER_SOURCE and len(metadata.tables) > MAX_TABLES_PER_SOURCE:
            raise ValueError(
                f"Source has {len(metadata.tables)} tables, exceeding the limit of "
                f"{MAX_TABLES_PER_SOURCE}. Narrow the scope with schemaFilter / "
                f"schemaExcludeFilter / tableFilter on the source configuration "
                f"(e.g. exclude system schemas), then re-scan."
            )

        # Persist discovered metadata to DataZone.
        project_id = _get_project_id(namespace_id)
        write_metadata = metadata
        rescan_tables_approved = 0
        # Whether this re-scan leaves anything for the steward to review. A
        # re-scan that finds nothing (no drift and no carried-forward orphaned
        # tables) returns straight to APPROVED instead of parking in
        # RESCAN_REVIEW; enrichment reads this via the reviewNeeded return field.
        # Default True so a first scan (which ignores it) and any unset path stay
        # on the review side; set for real inside the re-scan branch below.
        rescan_review_needed = True
        if is_rescan:
            # Re-scan of an already-approved source: MERGE onto the live assets
            # instead of blindly overwriting them. Curated metadata (steward
            # edits, approved AI) is preserved; only changed items reset to
            # PENDING_REVIEW. Unchanged tables are skipped and removed tables
            # are left in place (deleted only when the steward approves).
            #
            # The baseline for the diff / merge / backup must be the last
            # APPROVED state, not the last scan. If a prior re-scan is still open
            # in RESCAN_REVIEW (had_open_rescan), the live assets are that interim,
            # un-approved merge; the existing backup blob holds the approved
            # pre-image, so we reconstruct the approved baseline from (live, backup)
            # and read the backup BEFORE the merge below overwrites it. Interim
            # un-approved edits made during an open review are intentionally NOT
            # preserved across a fresh re-scan.
            #
            # We read the backup ONLY when had_open_rescan is true. Blob PRESENCE
            # is NOT proof of an open review: a source re-scanned before the
            # delete-on-approve/reject change shipped (or hit by the paged-approve
            # gap) is APPROVED yet still has a leftover backup. Reconstructing from
            # that stale blob would build a WRONG baseline — dropping tables it
            # lists as added, restoring stale pre-images — and manufacture drift on
            # a clean re-scan. When re-scanning from APPROVED the live assets ARE
            # the approved baseline, so we pass None and any stale backup is ignored
            # (reconstruct_approved_baseline then returns live unchanged).
            existing_backup = _read_existing_backup(source_id) if had_open_rescan else None
            live = read_assets_for_datasource(SMUS_DOMAIN_ID, project_id, datasource_id)
            accepted = reconstruct_approved_baseline(live, existing_backup, source_id=source_id)
            diff = diff_tables(accepted, metadata.tables)
            logger.info(
                "Re-scan diff for %s: added=%d removed=%d modified=%d unchanged=%d",
                datasource_id,
                len(diff.added),
                len(diff.removed),
                len(diff.modified),
                len(diff.unchanged),
            )
            write_metadata = DiscoveredMetadata(tables=merged_write_set(diff, accepted, metadata.tables))

            # Recompute the approved count for the post-merge live state. The merge
            # resets every modified and added table to PENDING_REVIEW and leaves
            # unchanged tables' approval intact, so only unchanged-and-approved
            # tables still count. Added tables are new/pending; removed tables are
            # excluded to match the fresh `tablesDiscovered` denominator. Written to
            # the source row below — without it the row keeps the pre-rescan count
            # through the whole RESCAN_REVIEW window and per-table reviews then apply
            # their +/-1 adjustment on a stale base.
            unchanged_ids = set(diff.unchanged)
            rescan_tables_approved = sum(
                1
                for t in accepted
                if t.table_id in unchanged_ids and t.business_metadata.review_status == ReviewStatus.APPROVED
            )

            # Tables a prior (still-open) re-scan added that are gone from this
            # fresh scan. reconstruct_approved_baseline drops them from the
            # baseline and they are absent from the fresh scan, so diff_tables
            # files them under neither added nor removed and their live assets
            # would be stranded across this review. Carry them into the backup so
            # a review outcome reaps them (approve via removed_tables, reject via
            # added_tables). Empty unless an open prior re-scan's backup exists.
            fresh_table_ids = {t.table_id for t in metadata.tables}
            orphaned_added = [
                tid for tid in (existing_backup or {}).get("added_tables", []) if tid not in fresh_table_ids
            ]
            # Nothing for the steward to review iff the fresh diff is empty AND no
            # orphaned added tables were carried forward. Drives the no-drift
            # auto-return to APPROVED (read by enrichment via reviewNeeded).
            rescan_review_needed = diff.has_changes or bool(orphaned_added)

            # Back up the pre-rescan state to S3 BEFORE the merge overwrites any
            # live asset. This blob is the only durable record of what the re-scan
            # changed (the diff is otherwise discarded) and is what approve (delete
            # removed) and reject (restore modified, delete added) read back.
            # Needed when the diff has changes OR there are orphaned added tables
            # to reap; a no-drift re-scan with neither writes none.
            if diff.has_changes or orphaned_added:
                if not BUCKET_NAME:
                    raise RuntimeError(
                        "BUCKET_NAME is not set; cannot write the re-scan backup that "
                        "approve/reject rollback depends on. Refusing to overwrite live assets."
                    )
                prior_summary = {
                    k: item[k]
                    for k in ("tablesDiscovered", "discoveredSchemas", "lastScanAt", "lastScanJobId", "tablesApproved")
                    if k in item
                }
                backup = build_rescan_backup(
                    diff,
                    accepted,
                    source_id=source_id,
                    scan_job_sk=scan_job_sk,
                    prior_summary=prior_summary,
                    orphaned_added_tables=orphaned_added,
                )
                backup_key = backup_s3_key(source_id)
                upload_json(get_s3_client(), BUCKET_NAME, backup_key, backup)
                logger.info("Re-scan backup written to s3://%s/%s", BUCKET_NAME, backup_key)

        write_result = write_to_datazone(
            domain_id=SMUS_DOMAIN_ID,
            project_id=project_id,
            metadata=write_metadata,
            data_source_id=datasource_id,
        )

        # Update scan job with discovery counts (the full fresh scan).
        scan_job_update: dict[str, Any] = {
            "tablesDiscovered": len(metadata.tables),
            "columnsDiscovered": metadata.total_columns,
        }
        # A connector that reads each table separately can lose a table without
        # failing the scan, and that table reaches review with no columns, no
        # comments, and no keys while enrichment backfills AI descriptions over
        # the gap. Recording it on the scan job makes the degradation visible to
        # the steward reviewing the result rather than only to whoever reads the
        # logs. The names are capped because this is a signal, not an inventory —
        # the count is the part that must always be right.
        if metadata.failed_tables:
            scan_job_update["tablesFailed"] = len(metadata.failed_tables)
            scan_job_update["failedTables"] = metadata.failed_tables[:_MAX_REPORTED_FAILED_TABLES]
        _get_scan_dao().update(
            key=scan_job_key,
            update_fields=scan_job_update,
            condition="attribute_exists(PK)",
        )

        emit_metric("ScanDuration", (time.monotonic() - scan_start) * 1000, "Milliseconds", SourceType=source_type)
        emit_metric("TablesDiscovered", len(metadata.tables), "Count", SourceType=source_type)
        if metadata.failed_tables:
            emit_metric("TablesFailed", len(metadata.failed_tables), "Count", SourceType=source_type)
            logger.warning(
                "Discovery completed with %d unreadable table(s) for %s: %s",
                len(metadata.failed_tables),
                datasource_id,
                metadata.failed_tables[:_MAX_REPORTED_FAILED_TABLES],
            )

        # Update the source record's summary fields with THIS scan's results.
        # Written on a RE-SCAN too: showing the previous scan's counts after an
        # explicit re-scan is wrong, not merely stale. During review the source
        # is in RESCAN_REVIEW (out of the APPROVED gate), so consumers ignore it
        # meanwhile; a reject restores these pre-rescan counts from the S3 backup
        # (captured above), and an approve keeps them.
        from datetime import datetime

        source_update: dict[str, Any] = {
            "tablesDiscovered": len(metadata.tables),
            # Distinct schemas (Athena databases for federated JDBC catalogs);
            # used by the federation step to scope Lake Formation grants.
            "discoveredSchemas": sorted({t.database for t in metadata.tables if t.database}),
            "lastScanAt": datetime.now(UTC).isoformat(),
            "lastScanJobId": scan_job_sk,
        }
        # Native Glue sources are queryable via AwsDataCatalog as soon as
        # they're scanned. JDBC sources become queryable only after the
        # federation step provisions the catalog, so that handler sets
        # `queryable` for them.
        if source_type == SourceSubType.GLUE_DATABASE:
            source_update["queryable"] = True
        # On a re-scan, refresh the approved count to the post-merge live state
        # (computed above). Omitted on a first scan so the create-time 0 and any
        # per-table review increments are not clobbered.
        if is_rescan:
            source_update["tablesApproved"] = rescan_tables_approved
        _get_ds_dao().update(
            key=source_key,
            update_fields=source_update,
            condition="attribute_exists(PK)",
        )

    except Exception as exc:
        permanent = is_permanent_scan_error(exc)
        logger.exception("Discovery failed for datasource %s (permanent=%s)", datasource_id, permanent)
        # Failure-path updates use raise_on_error=False: if the underlying
        # row has been deleted we still want to surface the original
        # exception, not a ConditionalCheckFailedException from cleanup.
        from datetime import datetime

        _get_ds_dao().update(
            key=source_key,
            update_fields={
                "status": SourceStatus.SCAN_FAILED,
                "lastScanJobId": scan_job_sk,
                "lastScanAt": datetime.now(UTC).isoformat(),
            },
            condition="attribute_exists(PK)",
            raise_on_error=False,
        )
        _get_scan_dao().update(
            key=scan_job_key,
            update_fields={"errorMessage": str(exc)},
            condition="attribute_exists(PK)",
            raise_on_error=False,
        )
        raise (PermanentScanError if permanent else TransientScanError)(str(exc)) from exc

    return {
        "datasourceId": datasource_id,
        "scanJobId": scan_job_id,
        "namespaceId": namespace_id,
        "scanType": scan_type,
        "tablesDiscovered": len(metadata.tables),
        "columnsDiscovered": metadata.total_columns,
        "assetsCreated": write_result.get("assets_created", 0),
        # String "true"/"false" (matches isRescan) so the enrichment container
        # override can pass it as an env var. A re-scan reporting "false" returns
        # the source straight to APPROVED instead of RESCAN_REVIEW. Always True on
        # a first scan (enrichment ignores it unless isRescan).
        "reviewNeeded": "true" if rescan_review_needed else "false",
    }


def _external_id(namespace_id: str, config: dict) -> str:
    """ExternalId COA presents when assuming a customer datasource-access role.

    Derived from the requesting namespace, never read from the request. The role
    ARN is caller-supplied, so this is what stops a caller with ``manageSource``
    on one namespace from pointing a source at another tenant's
    ``*-datasource-access-*`` role and reading its data (confused deputy).

    The derivation itself lives in ``coa_common.constants.datasource_external_id``
    because the control plane shows the same value to the customer for their trust
    policy — the two must not drift.

    LEGACY BRIDGE: sources onboarded before this control carry an operator-chosen
    ``externalId`` in their stored configuration which their trust policy pins;
    those keep working until the trust policy is migrated to the derived value.
    New and updated sources never store the field (``database_routes`` strips it),
    so a stored value can only predate this change. Delete this branch once no
    stored configuration carries ``externalId``.
    """
    stored = str(config.get("externalId") or "").strip()
    if stored:
        logger.warning(
            "datasource_legacy_external_id namespace_id=%s — migrate the role trust policy to the derived ExternalId",
            namespace_id,
        )
        return stored
    return datasource_external_id(namespace_id)


def _discover(
    connector: MetadataConnector,
    config: dict,
    datasource_id: str,
    namespace_id: str,
    source_type: str = "",
    source_item: dict[str, Any] | None = None,
) -> DiscoveredMetadata:
    """Run discovery with the given connector and configuration.

    ``source_item`` is the sources-table record. It carries attributes that are
    not part of the stored ``configuration`` blob — notably the Athena data
    catalog name a custom-connector source is registered under, which the control
    plane derives at create time rather than accepting from the caller.
    """
    item = source_item or {}
    connector_config = {
        "database_name": config["databaseName"],
        # Custom-connector (CUSTOM_CONNECTOR) sources only. Read from the source
        # record, not from `config`: the name is derived by the control plane and
        # stored as a top-level attribute, so it is not in the configuration blob
        # the caller supplied. Other connectors ignore it.
        "athena_data_catalog_name": item.get("athenaDataCatalogName", ""),
        "catalog_id": config.get("catalogId", ""),
        "region": config.get("region", AWS_REGION),
        "cross_account_role_arn": config.get("crossAccountRoleArn"),
        # ExternalId presented on the cross-account assume (JDBC secret fetch +
        # Glue metadata read). Derived from the namespace, NOT from the request.
        "external_id": _external_id(namespace_id, config),
        # JDBC-only — passed through for schema-aware discovery (PG/Redshift).
        # Glue connector ignores these.
        "schema_filter": config.get("schemaFilter"),
        "schema_exclude_filter": config.get("schemaExcludeFilter"),
        "table_filter": config.get("tableFilter"),
        "table_exclude_filter": config.get("tableExcludeFilter"),
        # JDBC connection fields (no-op for Glue)
        "host": config.get("host"),
        "port": config.get("port"),
        "engine": config.get("engine"),
        "credential_secret_arn": config.get("credentialSecretArn"),
        # Snowflake-only: warehouse (required for INFORMATION_SCHEMA) + optional role.
        "warehouse": config.get("warehouse"),
        "role": config.get("role"),
        "data_source_id": datasource_id,
        "namespace_id": namespace_id,
    }

    # Re-check the Glue target's namespace ownership here, not only at
    # source-create. This is the last gate before the connector reads the Glue
    # catalog, samples rows through Athena and — in strict-LF accounts — grants
    # itself Lake Formation access, and it is reached from the stored
    # configuration blob rather than from the checked request, so it holds even if
    # a future write path stops going through the control-plane check.
    #
    # ``lf_self_grant_allowed`` carries the same verdict into the connector: the
    # self-grant assumes a Lake Formation admin role, so it must be positively
    # authorized rather than merely not-forbidden. See
    # ``coa_sources.database.glue_ownership``.
    if (source_item or {}).get("sourceSubType") == SourceSubType.GLUE_DATABASE:
        try:
            assert_namespace_may_catalog(
                _get_ds_dao(),
                namespace_id=namespace_id,
                catalog_id=connector_config["catalog_id"],
                database_name=connector_config["database_name"],
                region=connector_config["region"],
                cross_account_role_arn=connector_config["cross_account_role_arn"],
            )
        except GlueOwnershipError as exc:
            raise RuntimeError(str(exc)) from exc
        connector_config["lf_self_grant_allowed"] = True

    # Test connection first
    validate_start = time.monotonic()
    test_result = connector.test_connection(connector_config)
    emit_metric("ValidationLatency", (time.monotonic() - validate_start) * 1000, "Milliseconds", SourceType=source_type)
    emit_metric(
        "ConnectionValidation",
        1,
        "Count",
        SourceType=source_type,
        Result="Success" if test_result.success else "Failure",
    )
    if not test_result.success:
        raise RuntimeError(
            f"Connection test failed: {test_result.message} "
            f"checks={json.dumps([c.__dict__ for c in test_result.checks])}"
        )

    discover_start = time.monotonic()
    metadata = connector.discover_metadata(connector_config)
    emit_metric("DiscoveryDuration", (time.monotonic() - discover_start) * 1000, "Milliseconds", SourceType=source_type)
    return metadata


def _get_project_id(namespace_id: str) -> str:
    """Resolve the DataZone project ID for a namespace."""
    item = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item or "dataZoneProjectId" not in item:
        raise ValueError(f"Namespace {namespace_id} not found or missing dataZoneProjectId")
    return item["dataZoneProjectId"]
