# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources API Lambda — unified source registry CRUD.

Single source of truth: all source data lives in sources-table.
  PK = NS#{namespaceId}, SK = SRC#{sourceId}

Routes:
  GET    /namespaces/{namespaceId}/sources                             — list
  POST   /namespaces/{namespaceId}/sources                             — create
  GET    /namespaces/{namespaceId}/sources/{sourceId}                  — get detail
  DELETE /namespaces/{namespaceId}/sources/{sourceId}                  — delete
  POST   /namespaces/{namespaceId}/sources/{sourceId}/rescan           — rescan
  POST   /namespaces/{namespaceId}/sources/upload-urls                 — pre-signed S3 URLs
  GET    /namespaces/{namespaceId}/sources/{sourceId}/tables           — list tables (DataZone)
  GET    /namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId} — get table (DataZone)
  POST   /namespaces/{namespaceId}/sources/{sourceId}/review           — review metadata (DataZone)
  GET    /namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}     — get scan job
  PUT    /namespaces/{namespaceId}/sources/{sourceId}/metadata         — update metadata
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import boto3
import structlog
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import resolve_region
from coa_common.constants import (
    PIPELINE_RUN_FIELDS,
    SOURCE_ACTIVE_STATUSES,
    to_graphrag_tenant_id,
    validate_namespace_id,
)
from coa_common.dao import DynamoDBDAO
from coa_common.dao.base import QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response, iso_to_epoch
from coa_control_plane_server.models.create_source_input import CreateSourceInput
from coa_control_plane_server.models.extraction_config import ExtractionConfig
from coa_control_plane_server.models.get_source_output import GetSourceOutput
from coa_control_plane_server.models.source_status import SourceStatus
from coa_control_plane_server.models.source_sub_type import SourceSubType
from coa_control_plane_server.models.source_summary import SourceSummary
from coa_control_plane_server.models.source_type import SourceType
from pydantic import ValidationError

from coa_sources.database.connectors.athena_catalog import (
    AthenaCatalogError,
    delete_lambda_catalog,
    derive_catalog_name,
)
from coa_sources.database.connectors.glue_connection_provisioner import (
    cleanup_federated_resources,
)
from coa_sources.database.glue_ownership import release_platform_catalog
from coa_sources.utils import merge_extraction_config

from .namespace_counters import adjust_namespace_source_count

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

_SOURCES_TABLE: str = os.environ.get("SOURCES_TABLE", "")
_SOURCE_SCAN_JOBS_TABLE: str = os.environ.get("SOURCE_SCAN_JOBS_TABLE", "")
_SCAN_QUEUE_URL: str = os.environ.get("SCAN_QUEUE_URL", "")
_INGESTION_QUEUE_URL: str = os.environ.get("INGESTION_QUEUE_URL", "")
_REVIEW_QUEUE_URL: str = os.environ.get("REVIEW_QUEUE_URL", "")
_BUCKET_NAME: str = os.environ.get("BUCKET_NAME", "")
_DELETION_STATE_MACHINE_ARN: str = os.environ.get("DELETION_STATE_MACHINE_ARN", "")
_NAMESPACES_TABLE: str = os.environ.get("NAMESPACES_TABLE", "")
_AWS_REGION: str = resolve_region()
_SMUS_DOMAIN_ID: str = os.environ.get("SMUS_DOMAIN_ID", "")
_PROJECT_ACCESS_ROLE_ARN: str = os.environ.get("PROJECT_ACCESS_ROLE_ARN", "")
_FEDERATION_PROVISIONER_ROLE_ARN: str = os.environ.get("FEDERATION_PROVISIONER_ROLE_ARN", "")
# Wall-clock budget for the synchronous DataZone asset cleanup that runs inline
# in the DELETE handler (see _delete_source_datazone_assets). A source with
# thousands of tables → thousands of serial delete_asset round-trips can exceed
# the Lambda timeout; cap the work so we exit cleanly and leave the rest to the
# namespace-deletion sweep, rather than letting the whole DELETE time out hard.
#
# Since GH-137 this is an **upper clamp** only: the effective deadline is
# min(env budget, remaining Lambda time − safety margin). A misconfigured or
# absent value cannot crash cold start; it falls back to 240s.
try:
    _DATAZONE_CLEANUP_BUDGET_S: int = int(os.environ.get("DATAZONE_CLEANUP_BUDGET_S", "240"))
except (ValueError, TypeError):
    _DATAZONE_CLEANUP_BUDGET_S = 240

_DEFAULT_MAX_RESULTS = 100
_MAX_RESULTS_LIMIT = 100
_MAX_UPLOAD_FILES = 100
_MAX_REVIEW_TABLES = 500
_UPLOAD_URL_EXPIRY_SECONDS = 900

_BY_NAMESPACE_GSI = "ByNamespace"
_BY_SOURCE_TYPE_GSI = "BySourceType"

# Which DatabaseSourceDetail member carries a sub-type's configuration.
#
# The sources-table stores the config blob in one untyped `configuration`
# column, so sourceSubType is the only thing that says which shape it holds —
# and the shapes are not interchangeable. GlueConfiguration requires catalogId
# (12-digit account form) and region, which a CUSTOM_CONNECTOR config has
# neither of, so reporting one as Glue fails GetSourceOutput validation and
# turns GET into a 500 rather than a cosmetic mislabel.
#
# Keep one entry per DATABASE sub-type: a sub-type added to the response shape
# but not to this map is caught by test_sources_handler.py rather than silently
# read as Glue.
_DETAIL_CONFIG_KEYS: dict[str, str] = {
    SourceSubType.GLUE_DATABASE.value: "glueConfiguration",
    SourceSubType.JDBC_DATABASE.value: "jdbcConfiguration",
    SourceSubType.CUSTOM_CONNECTOR.value: "customConnectorConfiguration",
}

# How a row this map does not cover is read: as Glue, which is how every
# non-JDBC row was read before the map existed.
#
# A row whose sourceSubType is absent or not a SourceSubType value never gets
# this far in a way the caller can see: sourceSubType is @required on both
# GetSourceOutput and SourceSummary, so such a row already fails response
# validation on that member alone, on GET and LIST alike, whichever member the
# blob lands under. What does reach here is a value the enum accepts but this
# map has no entry for — a DOCUMENTS sub-type on a row mis-stored as
# sourceType=DATABASE, or a DATABASE sub-type added to the enum ahead of its
# mapping. Both then read as Glue and 500 on GlueConfiguration's required
# members, so the fallback is logged rather than silent: the pydantic error
# names catalogId, which says nothing about the missing mapping that caused it.
# Raising here instead would buy nothing — the GET path catches neither.
_FALLBACK_CONFIG_KEY = "glueConfiguration"

# ---------------------------------------------------------------------------
# Lazy clients
# ---------------------------------------------------------------------------

_dao: DynamoDBDAO | None = None
_scan_dao: DynamoDBDAO | None = None
_ns_dao: DynamoDBDAO | None = None
_sqs = None
_s3 = None
_sfn = None
_sts = None


def _get_dao() -> DynamoDBDAO:
    global _dao
    if _dao is None:
        if not _SOURCES_TABLE:
            raise RuntimeError("SOURCES_TABLE env var not set")
        _dao = DynamoDBDAO(_SOURCES_TABLE, region=_AWS_REGION)
    return _dao


def _get_scan_dao() -> DynamoDBDAO:
    global _scan_dao
    if _scan_dao is None:
        if not _SOURCE_SCAN_JOBS_TABLE:
            raise RuntimeError("SOURCE_SCAN_JOBS_TABLE env var not set")
        _scan_dao = DynamoDBDAO(_SOURCE_SCAN_JOBS_TABLE, region=_AWS_REGION)
    return _scan_dao


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao
    if _ns_dao is None:
        if not _NAMESPACES_TABLE:
            raise RuntimeError("NAMESPACES_TABLE env var not set")
        _ns_dao = DynamoDBDAO(_NAMESPACES_TABLE, region=_AWS_REGION)
    return _ns_dao


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=_AWS_REGION)
    return _sqs


def _get_s3():
    global _s3
    if _s3 is None:
        # Pin SigV4 for presigned document-upload URLs: a no-Config client
        # falls back to the deprecated SigV2 presigner in pre-2014 regions,
        # and SigV2-only regions can't presign at all. Only ContentType is
        # signed (see document_routes._handle_upload_urls), so browser PUTs
        # stay valid under SigV4.
        _s3 = boto3.client("s3", region_name=_AWS_REGION, config=Config(signature_version="s3v4"))
    return _s3


def _get_sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=_AWS_REGION)
    return _sfn


def _get_sts():
    global _sts
    if _sts is None:
        _sts = boto3.client("sts", region_name=_AWS_REGION)
    return _sts


# ---------------------------------------------------------------------------
# Deadline helper — GH-137
# ---------------------------------------------------------------------------


def _cleanup_deadline(context: Any, margin_s: float = 2.0) -> float:
    """Derive a monotonic deadline from the Lambda runtime's remaining time.

    The effective deadline is the **minimum** of:
      - ``time.monotonic() + remaining_ms/1000 − margin_s``  (derived)
      - ``time.monotonic() + _DATAZONE_CLEANUP_BUDGET_S``     (env clamp)

    so the env budget can shrink the window but never extend it past the real
    Lambda timeout. This self-corrects when the CDK timeout changes and can
    never again be a fiction (the old 240 s constant was 8× the real 30 s
    timeout, so the graceful early-exit could never fire).

    When *context* is ``None`` or lacks ``get_remaining_time_in_millis``
    (unit tests, non-Lambda runtimes) the env budget is used as-is — the
    function must never crash outside Lambda.
    """
    now = time.monotonic()
    env_deadline = now + _DATAZONE_CLEANUP_BUDGET_S

    remaining_ms: int | None = None
    if context is not None:
        getter = getattr(context, "get_remaining_time_in_millis", None)
        if callable(getter):
            try:
                remaining_ms = int(getter())
            except Exception:
                logger.warning("cleanup_deadline_remaining_time_failed", exc_info=True)

    if remaining_ms is not None:
        derived_deadline = now + (remaining_ms / 1000.0) - margin_s
        return min(derived_deadline, env_deadline)

    return env_deadline


# ---------------------------------------------------------------------------
# DDB item → Smithy model projections
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_id_from_item(item: dict[str, Any]) -> str:
    sk = item.get("SK", "")
    return sk.removeprefix("SRC#") if sk.startswith("SRC#") else sk


def _item_to_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Project a sources-table item to the SourceSummary response shape."""
    data = {
        "sourceId": _source_id_from_item(item),
        "namespaceId": item.get("namespaceId"),
        "name": item.get("name"),
        "sourceType": item.get("sourceType"),
        "sourceSubType": item.get("sourceSubType"),
        "status": item.get("status"),
        "createdAt": iso_to_epoch(item.get("createdAt", "")),
    }
    if "updatedAt" in item:
        data["updatedAt"] = iso_to_epoch(item["updatedAt"])
    if "tablesDiscovered" in item and item["tablesDiscovered"] is not None:
        data["tablesDiscovered"] = int(item["tablesDiscovered"])
    return SourceSummary.from_dict(data).to_dict()


def _build_database_detail(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a DatabaseSourceDetail dict from a sources-table item.

    The stored `configuration` blob is surfaced under the member the row's
    sourceSubType selects (see _DETAIL_CONFIG_KEYS).
    """
    db: dict[str, Any] = {}
    if item.get("configuration"):
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            parsed = json.loads(item["configuration"])
            # Type the blob from the sub-type. SourceSubType mixes in str, so it
            # hashes and compares as its value — the lookup accepts a raw DDB
            # string or a SourceSubType alike.
            sub_type: Any = item.get("sourceSubType") or ""
            config_key = _DETAIL_CONFIG_KEYS.get(sub_type)
            if config_key is None:
                logger.warning("source_sub_type_unmapped", sub_type=sub_type or None, read_as=_FALLBACK_CONFIG_KEY)
                config_key = _FALLBACK_CONFIG_KEY
            db[config_key] = parsed
    for field in _DETAIL_CONFIG_KEYS.values():
        if field in item and item[field] is not None:
            val = item[field]
            db[field] = json.loads(val) if isinstance(val, str) else val
    for field in (
        "tablesDiscovered",
        "tablesApproved",
        "lastScanJobId",
        "metadataEnrichmentEnabled",
        # Execution engine (ATHENA default, or REDSHIFT for Glue-via-Redshift).
        # Read-only, system-set at creation from the resolved queryEngine.
        "queryEngine",
        # Athena federation references — read-only, system-managed.
        # Populated only for JDBC sub-types after a successful first scan.
        "glueConnectionName",
        "athenaDataCatalogName",
    ):
        if field in item and item[field] is not None:
            db[field] = item[field]
    # redshiftWorkgroup is persisted as a top-level column (only when the Glue source
    # executes via Redshift). Surface it inside glueConfiguration so the source-detail
    # API + UI can show the chosen engine's workgroup.
    if item.get("redshiftWorkgroup") and isinstance(db.get("glueConfiguration"), dict):
        db["glueConfiguration"]["redshiftWorkgroup"] = item["redshiftWorkgroup"]
        db["glueConfiguration"].setdefault("executionEngine", "REDSHIFT")
    if item.get("lastScanAt"):
        db["lastScanAt"] = iso_to_epoch(item["lastScanAt"])
    return db or None


def _build_document_detail(item: dict[str, Any], metrics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Build a DocumentSourceDetail dict from a sources-table item.

    ``metrics`` is the optional per-source KG-build metrics record
    (PK=METRICS#{ns}, SK=KGBUILD#{ds}), written by the graphrag ProgressMonitor
    as plain Number counters (atomic ADD, main-process/single-threaded — so the
    counts are exact without cross-process distinct sets). Stage progress:
      - ``documentsProcessed`` — docs that completed LLM extraction
      - ``chunksLLM``   — chunks that completed LLM extraction
      - ``chunksEmbed`` — chunk nodes written to the OSS vector store
      - ``chunksGraph`` — chunk nodes written to the Neptune graph store
    """
    doc: dict[str, Any] = {}
    for field in (
        "s3Prefixes",
        "sourceBucketArn",
        "roleArn",
        "extractionConfig",
        "filesTotal",
        "filesSkipped",
        "filesErrored",
    ):
        if field in item and item[field] is not None:
            doc[field] = item[field]

    # extractionConfig is stored in DDB, which marshals Integer fields as Number;
    # boto3 reads them back as Decimal -> they serialize to "0.0" in JSON, breaking
    # the Smithy Integer contract for the chunkSize/chunkOverlap fields.
    # Coerce them back to int on readback so the API honours its own model
    # (0 == "use the graphrag-toolkit default", not 0.0).
    ec = doc.get("extractionConfig")
    if isinstance(ec, dict):
        for int_field in ("chunkSize", "chunkOverlap"):
            v = ec.get(int_field)
            if v is not None:
                try:
                    ec[int_field] = int(v)
                except (TypeError, ValueError) as exc:
                    # Leave the raw value in place rather than dropping the field;
                    # log loudly so a contract-violating DDB value is diagnosable
                    # instead of silently emitting a non-integer to the client.
                    logger.warning(
                        "failed to coerce extractionConfig int field on readback",
                        field=int_field,
                        value=v,
                        error=str(exc),
                    )

    if metrics:
        # Stage metrics are plain Number counters written by the ProgressMonitor.
        def _num(field: str) -> int | None:
            v = metrics.get(field)
            return int(v) if v is not None and not isinstance(v, (set, frozenset)) else None

        for src_field, api_field in (
            ("documents_processed", "documentsProcessed"),
            ("chunks_llm", "chunksLLM"),
            ("chunks_embed", "chunksEmbed"),
            ("chunks_graph", "chunksGraph"),
        ):
            n = _num(src_field)
            if n is not None:
                doc[api_field] = n

    # Parse errorMessage — may be:
    #   1. A plain string (e.g. from sources_handler itself)
    #   2. A Lambda error envelope: {"errorMessage": "...", "errorType": "..."}
    #   3. A raw ECS task JSON blob with "StoppedReason" and "Containers[].Reason"
    raw_error = item.get("errorMessage")
    if raw_error:
        doc["errorMessage"] = _parse_error_message(raw_error)

    if item.get("preprocessingIssues"):
        val = item["preprocessingIssues"]
        if isinstance(val, str):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                doc["preprocessingIssues"] = json.loads(val)
        else:
            doc["preprocessingIssues"] = val
    return doc or None


def _parse_error_message(raw: Any) -> str:
    """Extract a human-readable error message from various error formats.

    Handles:
    - Plain strings — returned as-is
    - Lambda error envelopes: {"errorMessage": "...", "errorType": "..."}
    - ECS task JSON blobs: {"StoppedReason": "...", "Containers": [...]}
    """
    if not isinstance(raw, str):
        return str(raw)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw  # plain string

    if not isinstance(parsed, dict):
        return raw

    # Lambda error envelope
    if "errorMessage" in parsed:
        return str(parsed["errorMessage"])

    # ECS task JSON — extract StoppedReason + first container exit reason
    if "StoppedReason" in parsed or "Containers" in parsed:
        parts: list[str] = []
        stopped_reason = parsed.get("StoppedReason", "")
        if stopped_reason:
            parts.append(stopped_reason)
        containers = parsed.get("Containers", [])
        if containers and isinstance(containers, list):
            first = containers[0]
            if isinstance(first, dict):
                exit_code = first.get("ExitCode")
                reason = first.get("Reason", "")
                name = first.get("Name", "container")
                if reason:
                    parts.append(f"{name}: {reason}")
                elif exit_code is not None and exit_code != 0:
                    parts.append(f"{name} exited with code {exit_code}")
        return " — ".join(parts) if parts else "ECS task failed"

    return raw


def _item_to_detail(item: dict[str, Any], metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project a sources-table item to the GetSourceOutput response shape.

    Uses the generated GetSourceOutput Pydantic model for serialization,
    matching the pattern in doc_source_handler.py. ``metrics`` is the optional
    per-source KG-build metrics record used to enrich document-source details.
    """
    source_type = item.get("sourceType", "")
    data: dict[str, Any] = {
        "sourceId": _source_id_from_item(item),
        "namespaceId": item.get("namespaceId"),
        "name": item.get("name"),
        "sourceType": source_type,
        "sourceSubType": item.get("sourceSubType"),
        "status": item.get("status"),
        "createdAt": iso_to_epoch(item.get("createdAt", "")),
    }
    if "updatedAt" in item:
        data["updatedAt"] = iso_to_epoch(item["updatedAt"])

    if source_type == SourceType.DATABASE:
        db_detail = _build_database_detail(item)
        if db_detail:
            data["databaseDetails"] = db_detail
    else:
        doc_detail = _build_document_detail(item, metrics)
        if doc_detail:
            data["documentDetails"] = doc_detail

    return GetSourceOutput.from_dict(data).to_dict()


# ---------------------------------------------------------------------------
# Imports from split modules
# ---------------------------------------------------------------------------

from .database_routes import (  # noqa: E402
    _create_database_source,
    _handle_approve_source,
    _handle_get_scan_job,
    _handle_get_table,
    _handle_keep_rescan_removal,
    _handle_list_scan_jobs,
    _handle_list_tables,
    _handle_reject_source,
    _handle_review_column,
    _handle_review_table,
    _handle_update_column_metadata,
    _handle_update_metadata,
    _handle_update_table_keys,
    _handle_update_table_metadata,
)
from .document_routes import _create_document_source, _handle_upload_urls  # noqa: E402

# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


def _handle_list(event: dict[str, Any], namespace_id: str) -> dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    source_type: str | None = qs.get("sourceType") or None
    if source_type and source_type not in {t.value for t in SourceType}:
        valid = ", ".join(sorted(t.value for t in SourceType))
        return api_response(400, {"error": f"sourceType must be one of: {valid}"})

    try:
        max_results = int(qs.get("maxResults", str(_DEFAULT_MAX_RESULTS)))
        if not (1 <= max_results <= _MAX_RESULTS_LIMIT):
            return api_response(400, {"error": f"maxResults must be between 1 and {_MAX_RESULTS_LIMIT}"})
    except (ValueError, TypeError):
        return api_response(400, {"error": "maxResults must be a valid integer"})

    next_token: str | None = qs.get("nextToken")
    exclusive_start_key: dict[str, Any] | None = None
    if next_token:
        try:
            exclusive_start_key = json.loads(base64.b64decode(next_token).decode())
        except Exception:
            return api_response(400, {"error": "Invalid nextToken"})

    try:
        if source_type:
            result = _get_dao().query(
                QueryParams(
                    key_condition="#pk = :ns AND begins_with(#sk, :prefix)",
                    expression_values={":ns": namespace_id, ":prefix": f"{source_type}#"},
                    expression_names={"#pk": "namespaceId", "#sk": "sourceTypeCreatedAt"},
                    index_name=_BY_SOURCE_TYPE_GSI,
                    limit=max_results,
                    exclusive_start_key=exclusive_start_key,
                    scan_forward=False,
                )
            )
        else:
            result = _get_dao().query(
                QueryParams(
                    key_condition="#pk = :ns",
                    expression_values={":ns": namespace_id},
                    expression_names={"#pk": "namespaceId"},
                    index_name=_BY_NAMESPACE_GSI,
                    limit=max_results,
                    exclusive_start_key=exclusive_start_key,
                    scan_forward=False,
                )
            )
    except ClientError:
        logger.exception("ddb_query_failed", namespace_id=namespace_id)
        return api_response(500, {"error": "Internal server error"})

    body: dict[str, Any] = {"items": [_item_to_summary(i) for i in result.items]}
    if result.last_evaluated_key:
        body["nextToken"] = base64.b64encode(json.dumps(result.last_evaluated_key, default=str).encode()).decode()
    return api_response(200, body)


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


def _handle_create(event: dict[str, Any], namespace_id: str) -> dict[str, Any]:
    try:
        raw: dict[str, Any] = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"error": "Invalid JSON body"})

    # Parse and validate via generated Smithy model — same pattern as doc_source_handler.py
    try:
        req = CreateSourceInput.model_validate(raw)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"] if exc.errors() else str(exc)
        return api_response(400, {"error": msg})

    # Verify namespace exists
    ns_item = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not ns_item:
        return api_response(404, {"error": f"Namespace not found: {namespace_id}"})

    if req.source_type == SourceType.DATABASE:
        if not req.database_source:
            return api_response(400, {"error": "databaseSource is required when sourceType=DATABASE"})
        return _create_database_source(req.database_source, namespace_id)
    else:
        if not req.document_source:
            return api_response(400, {"error": "documentSource is required when sourceType=DOCUMENTS"})
        return _create_document_source(req.document_source, namespace_id, event)


# ---------------------------------------------------------------------------
# GET DETAIL
# ---------------------------------------------------------------------------


def _handle_get(namespace_id: str, source_id: str) -> dict[str, Any]:
    try:
        item = _get_dao().get({"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"})
    except ClientError:
        logger.exception("ddb_get_failed", source_id=source_id)
        return api_response(500, {"error": "Internal server error"})

    if not item:
        return api_response(404, {"error": f"Source '{source_id}' not found"})

    # Best-effort: enrich document sources with KG-build metrics. Stored in a
    # dedicated partition (PK=METRICS#{ns}, SK=KGBUILD#{ds}) on the same table.
    # A missing/failed read must never break the detail view.
    metrics: dict[str, Any] | None = None
    if item.get("sourceType") != SourceType.DATABASE:
        try:
            metrics = _get_dao().get({"PK": f"METRICS#{namespace_id}", "SK": f"KGBUILD#{source_id}"})
        except (BotoCoreError, ClientError):
            logger.warning("kg_metrics_get_failed", source_id=source_id, exc_info=True)

    return api_response(200, _item_to_detail(item, metrics))


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def _delete_source_datazone_assets(
    namespace_id: str,
    source_id: str,
    context: Any = None,
) -> int:
    """Remove all DataZone (SageMaker Unified Studio Catalog) assets for a source.

    Assets created by the discovery/enrichment pipeline are named
    ``DS#{sourceId}:{tableId}`` in the namespace's DataZone project (see
    ``packages/sources/.../database/metadata_writer.py``).

    Two-phase to avoid pagination drift: first fully paginate ``search_assets``
    (50/page hard limit) and collect every matching asset id, THEN delete from
    the materialized list. DataZone search pages over a live result set, so
    deleting mid-pagination shrinks that set and the next-page token skips past
    unseen matches — leaving assets orphaned. Mirrors the read-only
    collect-then-act pattern in ``metadata_writer._build_existing_asset_map``.

    Since GH-137 a single derived deadline bounds **both** phases: if the
    Lambda has little time left, even the pagination must stop early so the
    caller can return a partial-completion response rather than timing out.

    Returns the number of assets deleted. Raises if the SMUS client cannot
    be built or if the underlying domain/project is missing.
    """
    # Imported lazily to avoid module-load-time SMUS client construction
    # (the SMUS client touches AWS config and slows cold starts otherwise).
    from .database_routes import _get_smus_client, _resolve_project_id

    if not _SMUS_DOMAIN_ID:
        logger.info("datazone_asset_cleanup_skipped_no_domain", source_id=source_id)
        return 0

    project_id = _resolve_project_id(namespace_id)
    if not project_id:
        logger.warning(
            "datazone_asset_cleanup_skipped_no_project",
            namespace_id=namespace_id,
            source_id=source_id,
        )
        return 0

    client = _get_smus_client()
    ds_key = f"DS#{source_id}"
    max_pages = 100

    deadline = _cleanup_deadline(context)

    # Phase 1: fully paginate and COLLECT matching asset ids — do NOT delete
    # while iterating. DataZone search pagination is over a live result set;
    # deleting assets mid-pagination shrinks that set, so the next-page token
    # skips past the remaining matches and they are never seen.
    #
    # The deadline is checked between pages so a near-timeout Lambda exits
    # cleanly instead of being killed mid-request (GH-137).
    asset_ids: list[str] = []
    search_stopped_early = False
    next_token: str | None = None
    for _ in range(max_pages):
        if time.monotonic() > deadline:
            search_stopped_early = True
            logger.warning(
                "datazone_asset_search_deadline_exceeded",
                source_id=source_id,
                collected=len(asset_ids),
            )
            break
        result = client.search_assets(
            project_id=project_id,
            search_text=ds_key,
            max_results=50,
            next_token=next_token,
        )
        for asset in result.items:
            # Search is fuzzy; only keep assets whose name actually
            # belongs to this source (matching the "DS#<id>:<table>" prefix).
            if asset.name.startswith(f"{ds_key}:"):
                asset_ids.append(asset.asset_id)
        next_token = result.next_token
        if not next_token:
            break
    else:
        logger.warning("datazone_asset_cleanup_pagination_limit", source_id=source_id, max_pages=max_pages)

    # Visibility into the collection phase — a low count here vs. expected
    # table count is the first signal when chasing orphaned assets.
    logger.info(
        "datazone_asset_cleanup_collected",
        source_id=source_id,
        asset_count=len(asset_ids),
        search_stopped_early=search_stopped_early,
    )

    # Phase 2: delete from the materialized list. The search result set is no
    # longer being iterated, so deletions can't perturb pagination. This loop
    # runs synchronously in the DELETE handler, so bound it by the same
    # derived deadline: if we're near timeout, stop early and let the
    # namespace-deletion sweep finish the rest.
    removed = 0
    for idx, asset_id in enumerate(asset_ids):
        if time.monotonic() > deadline:
            logger.warning(
                "datazone_asset_cleanup_budget_exceeded",
                source_id=source_id,
                deleted=removed,
                remaining=len(asset_ids) - idx,
                budget_s=_DATAZONE_CLEANUP_BUDGET_S,
            )
            break
        try:
            client.delete_asset(asset_id=asset_id)
            removed += 1
        except ClientError as exc:
            # Surface the DataZone error code to aid triage (throttling,
            # access-denied, already-deleted). Best-effort: keep deleting.
            logger.exception(
                "datazone_asset_delete_failed",
                asset_id=asset_id,
                source_id=source_id,
                error_code=exc.response.get("Error", {}).get("Code"),
            )
        except BotoCoreError:
            # Connection/timeout/credential-resolution failures from the SDK
            # layer (no response payload). Still best-effort: keep deleting.
            logger.exception(
                "datazone_asset_delete_failed",
                asset_id=asset_id,
                source_id=source_id,
            )
        except Exception:
            # Last-resort catch so one unexpected per-asset error can't strand
            # the rest of the cleanup. This loop is best-effort by design (the
            # caller in _handle_delete already treats the whole call as such);
            # narrowing further would abort cleanup of every remaining asset.
            logger.exception(
                "datazone_asset_delete_unexpected_error",
                asset_id=asset_id,
                source_id=source_id,
            )

    return removed


def _delete_source_scan_jobs(source_id: str) -> int:
    """Delete all scan-job rows for a source.

    Scan jobs live in ``source-scan-jobs`` with ``PK=SRC#{sourceId}`` and
    ``SK=<ISO timestamp>``. Pages through the primary index, batches the
    deletes, and returns the total removed.
    """
    from coa_common.dao.base import QueryParams

    dao = _get_scan_dao()
    keys: list[dict[str, str]] = []
    last_evaluated_key: dict[str, Any] | None = None
    max_pages = 100

    for _ in range(max_pages):
        result = dao.query(
            QueryParams(
                key_condition="PK = :pk",
                expression_values={":pk": f"SRC#{source_id}"},
                exclusive_start_key=last_evaluated_key,
            )
        )
        keys.extend({"PK": item["PK"], "SK": item["SK"]} for item in result.items)
        if not result.last_evaluated_key:
            break
        last_evaluated_key = result.last_evaluated_key
    else:
        logger.warning("scan_job_cleanup_pagination_limit", source_id=source_id, max_pages=max_pages)

    if keys:
        dao.batch_delete(keys)
    return len(keys)


def _handle_delete(namespace_id: str, source_id: str, context: Any = None) -> dict[str, Any]:
    try:
        item = _get_dao().get({"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"})
    except ClientError:
        logger.exception("ddb_get_failed", source_id=source_id)
        return api_response(500, {"error": "Internal server error"})

    if not item:
        return api_response(404, {"error": f"Source '{source_id}' not found"})

    source_type = item.get("sourceType", "")
    current_status = item.get("status", "")

    if source_type == SourceType.DOCUMENTS:
        if current_status == SourceStatus.DELETING:
            return api_response(202, {"sourceId": source_id, "status": current_status})
        if current_status in SOURCE_ACTIVE_STATUSES:
            return api_response(
                409,
                {
                    "error": (
                        f"Source '{source_id}' has active ingestion (status: '{current_status}'). Wait before deleting."
                    ),
                    "sourceId": source_id,
                    "status": current_status,
                },
            )
        now = _now_iso()
        try:
            _get_dao().update(
                {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
                {"status": SourceStatus.DELETING, "updatedAt": now},
                condition=Attr("status").ne(SourceStatus.DELETING),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return api_response(202, {"sourceId": source_id, "status": SourceStatus.DELETING})
            logger.exception("ddb_update_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        if _DELETION_STATE_MACHINE_ARN:
            sfn_input = json.dumps(
                {
                    "namespace_id": namespace_id,
                    "doc_source_id": source_id,
                    "tenant_id": item.get("tenantId", to_graphrag_tenant_id(namespace_id)),
                    "bucket_name": _BUCKET_NAME,
                    "source_type": item.get("docSourceType", "upload"),
                    "s3_prefixes": item.get("s3Prefixes", []),
                }
            )
            try:
                _get_sfn().start_execution(
                    stateMachineArn=_DELETION_STATE_MACHINE_ARN,
                    name=f"delete-{source_id}-{int(datetime.now(UTC).timestamp())}",
                    input=sfn_input,
                )
            except ClientError:
                logger.exception("sfn_start_failed", source_id=source_id)
                try:
                    _get_dao().update(
                        {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
                        {
                            "status": SourceStatus.DELETE_FAILED,
                            "errorMessage": "Failed to start deletion pipeline",
                        },
                    )
                except ClientError:
                    logger.exception("ddb_update_delete_failed_status_failed", source_id=source_id)
                return api_response(500, {"error": "Internal server error"})

        # Decrement the namespace sourceCount as soon as the source
        # transitions to DELETING. The actual S3 / KG cleanup is asynchronous,
        # but from the user's perspective the source is gone. The counter
        # decrement is best-effort — see ``adjust_namespace_source_count``.
        adjust_namespace_source_count(namespace_id, SourceType.DOCUMENTS, -1)

        return api_response(202, {"sourceId": source_id, "status": SourceStatus.DELETING})

    else:  # DATABASE
        if current_status in SOURCE_ACTIVE_STATUSES:
            return api_response(
                409,
                {
                    "error": (
                        f"Source '{source_id}' has an active scan (status: '{current_status}'). Wait before deleting."
                    ),
                    "sourceId": source_id,
                    "status": current_status,
                },
            )
        sub_type = item.get("sourceSubType", "")

        # The one name every teardown below works from, derived from the source id
        # rather than read off the row. `build_catalog_name` is what named the
        # resource in the first place — for the custom-connector Athena catalog and
        # for the federated-JDBC Glue catalog/connection alike — so the row has
        # nothing to contribute here, and the derivation being the sole input is
        # what makes it impossible for a stored value to redirect a delete.
        expected_name = derive_catalog_name(source_id)

        # A custom-connector source owns a top-level LAMBDA-type Athena data
        # catalog, which is a plain athena:DeleteDataCatalog on this role — no
        # Glue object, no Lake Formation grants, and so nothing to assume the
        # federation provisioner's admin role for.
        #
        # This must run BEFORE the federated-teardown block below, and that block
        # must exclude this sub-type: a Lambda catalog also populates
        # `athenaDataCatalogName`, so it would otherwise match, assume the
        # LF-admin role, and call glue.delete_catalog — a no-op for a Lambda
        # catalog — reporting success while leaking the registration.
        #
        # Fails the delete (HTTP 500) rather than proceeding, for the same reason
        # the federated teardown does: the source row is the only handle on the
        # catalog, so dropping the row after a failed teardown orphans it.
        if sub_type == SourceSubType.CUSTOM_CONNECTOR:
            try:
                delete_lambda_catalog(catalog_name=expected_name)
            except AthenaCatalogError:
                logger.exception(
                    "athena_data_catalog_delete_failed",
                    source_id=source_id,
                    catalog_name=expected_name,
                )
                return api_response(
                    500,
                    {"error": "Failed to remove the Athena data catalog; deletion not completed"},
                )

        # Teardown of any Glue federated catalog / connection provisioned for
        # this source. Dropping an LF-governed catalog requires the federation
        # provisioner's Lake Formation admin role, so we assume it and run the
        # teardown synchronously with those credentials. Because these are
        # billable resources with no automatic recovery path, a teardown failure
        # blocks the DDB delete (HTTP 500) so the source row remains and the
        # delete can be retried — rather than silently orphaning the resources.
        #
        # The two names are DERIVED from the source id rather than trusted from the
        # row, because the row is not a trustworthy record of what the provisioner
        # created. `POST /sources` used to copy the caller's
        # `glueConfiguration.athenaDataCatalogName` straight into this attribute; it
        # no longer does (see database_routes._create_database_source), but rows
        # written before that change still carry a caller-chosen value, and this is
        # the layer that has to be safe regardless of what reached the table.
        #
        # It matters because the role assumed below is a Lake Formation data-lake
        # admin whose IAM is scoped to the deployment-wide `{sanitizedPrefix}ds_*`
        # window — not to one namespace — so a seeded name would let a delete here
        # drop ANOTHER namespace's federated catalog, deregister its LF resource and
        # delete its Glue connection. Teardown is best-effort per resource, so that
        # would even report success.
        #
        # `build_catalog_name` makes the real name a pure function of
        # (RESOURCE_PREFIX, sourceId), and sourceId is a server-generated UUID
        # scoped to this namespace, so a cross-namespace name can never match
        # `expected_name`. A stored name that does not match is therefore either
        # seeded or from a deployment whose RESOURCE_PREFIX has since changed; both
        # are logged and skipped. Skipping can orphan a real resource in the
        # prefix-changed case, which is the right way round to fail — an orphan
        # costs money and is recoverable from the log line, dropping another
        # namespace's catalog is not.
        stored_conn = item.get("glueConnectionName") or ""
        stored_cat = item.get("athenaDataCatalogName") or ""
        unexpected = sorted({n for n in (stored_conn, stored_cat) if n and n != expected_name})
        if unexpected:
            logger.warning(
                "federated_teardown_skipped_name_not_derived",
                source_id=source_id,
                namespace_id=namespace_id,
                expected_name=expected_name,
                stored_names=unexpected,
            )
        glue_conn = stored_conn if stored_conn == expected_name else None
        athena_cat = stored_cat if stored_cat == expected_name else None
        if (
            sub_type != SourceSubType.CUSTOM_CONNECTOR
            and (glue_conn or athena_cat)
            and _FEDERATION_PROVISIONER_ROLE_ARN
        ):
            try:
                creds = (
                    _get_sts()
                    .assume_role(
                        RoleArn=_FEDERATION_PROVISIONER_ROLE_ARN,
                        RoleSessionName=f"delete-{source_id}"[:64],
                    )
                    .get("Credentials")
                )
                if not creds:
                    raise RuntimeError("AssumeRole returned no Credentials")
                admin_session = boto3.Session(
                    aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretAccessKey"],
                    aws_session_token=creds["SessionToken"],
                    region_name=_AWS_REGION,
                )
                cleanup_federated_resources(
                    glue_connection_name=glue_conn,
                    athena_catalog_name=athena_cat,
                    session=admin_session,
                )
            except Exception:
                logger.exception(
                    "federated_resources_cleanup_failed",
                    source_id=source_id,
                    glue_connection_name=glue_conn,
                    athena_catalog_name=athena_cat,
                )
                return api_response(
                    500,
                    {"error": "Failed to clean up federated query resources; deletion not completed"},
                )

        # Best-effort cleanup of DataZone (SageMaker Unified Studio Catalog)
        # assets created during the discovery/enrichment pipeline. Each
        # discovered table is registered as an asset named "DS#{sourceId}:..."
        # in the namespace's DataZone project (see metadata_writer.py).
        # Failures are logged but do not block the DDB delete; the assets
        # would otherwise also be cleaned up at namespace deletion time.
        try:
            removed = _delete_source_datazone_assets(namespace_id, source_id, context)
            logger.info("datazone_assets_deleted", source_id=source_id, count=removed)
        except Exception:
            logger.exception(
                "datazone_asset_cleanup_failed",
                source_id=source_id,
                namespace_id=namespace_id,
            )

        # Best-effort cleanup of all scan-job records for this source.
        # Schema: PK=SRC#{sourceId}, SK=<ISO timestamp>. These are otherwise
        # orphaned in source-scan-jobs until namespace deletion sweeps the
        # ByNamespace GSI.
        try:
            removed_jobs = _delete_source_scan_jobs(source_id)
            logger.info("scan_jobs_deleted", source_id=source_id, count=removed_jobs)
        except Exception:
            logger.exception("scan_job_cleanup_failed", source_id=source_id)

        try:
            _get_dao().delete({"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"})
        except ClientError:
            logger.exception("ddb_delete_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        # Release the namespace's claim on the catalog name this source was given,
        # now that both the catalog and the source row are gone. The name is
        # derived from the source id, so it can never be re-minted for a different
        # source — the release exists so the record does not outlive what it
        # describes, not to free the name. Best-effort: a surviving claim only
        # keeps a catalog that no longer exists attributed to this namespace.
        if sub_type in (SourceSubType.JDBC_DATABASE, SourceSubType.CUSTOM_CONNECTOR):
            with contextlib.suppress(ClientError):
                release_platform_catalog(_get_dao(), catalog_name=expected_name)

        # Decrement the namespace sourceCount after the row is gone.
        # Best-effort — see ``adjust_namespace_source_count``.
        adjust_namespace_source_count(namespace_id, SourceType.DATABASE, -1)

        return api_response(200, {"sourceId": source_id, "status": SourceStatus.DELETED})


# ---------------------------------------------------------------------------
# RESCAN
# ---------------------------------------------------------------------------


def _handle_rescan(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    from coa_control_plane_server.models.rescan_source_request_content import RescanSourceRequestContent

    # The body carries only the discard-open-review acknowledgement. It is
    # optional, so an absent body is valid and parses to all-defaults.
    try:
        raw: dict[str, Any] = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"error": "Invalid JSON body"})
    try:
        req = RescanSourceRequestContent.model_validate(raw)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"] if exc.errors() else str(exc)
        return api_response(400, {"error": msg})

    try:
        item = _get_dao().get({"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"})
    except ClientError:
        logger.exception("ddb_get_failed", source_id=source_id)
        return api_response(500, {"error": "Internal server error"})

    if not item:
        return api_response(404, {"error": f"Source '{source_id}' not found"})

    source_type = item.get("sourceType", "")
    current_status = item.get("status", "")
    now = _now_iso()

    # Validation differs by source type:
    # - DATABASE: re-scan is allowed from SCAN_FAILED (recovery), APPROVED
    #   (schema-drift re-scan of a live source — merges onto the accepted
    #   assets, preserves curated metadata, and moves the source to
    #   RESCAN_REVIEW for steward review), or RESCAN_REVIEW itself (retry a
    #   drift review). Any other status is rejected.
    # - DOCUMENTS: re-scan is allowed from COMPLETED (re-ingest) or SCAN_FAILED
    #   (retry after failure). Active ingestion statuses are still rejected.
    _DB_RESCAN_ALLOWED = (
        SourceStatus.SCAN_FAILED,
        SourceStatus.APPROVED,
        SourceStatus.RESCAN_REVIEW,
    )
    if source_type == SourceType.DATABASE and current_status not in _DB_RESCAN_ALLOWED:
        return api_response(
            409,
            {
                "error": (
                    f"Re-scan is only allowed when status is one of "
                    f"{', '.join(_DB_RESCAN_ALLOWED)} (current: '{current_status}')."
                ),
                "sourceId": source_id,
                "status": current_status,
            },
        )
    # Re-scanning a source that already has an OPEN re-scan review is the one
    # transition that destroys steward work, so it needs an explicit
    # acknowledgement. Discovery re-diffs against the last APPROVED state
    # (reconstructed from the live assets plus the backup blob) and then
    # overwrites that blob, so every review decision and edit made inside the
    # open window is dropped — see the merge path in
    # ``pipeline/discovery_handler.py``. Every other allowed status discards
    # nothing: from APPROVED the live assets already ARE the baseline, and
    # SCAN_FAILED has no review to lose. Checked before any write below, so a
    # refused call leaves the source exactly as it was.
    if (
        source_type == SourceType.DATABASE
        and current_status == SourceStatus.RESCAN_REVIEW
        and not req.confirm_discard_open_review
    ):
        return api_response(
            409,
            {
                "error": (
                    f"Source '{source_id}' has a re-scan review open. Starting a new re-scan discards "
                    f"the review decisions and edits already made in it. Retry with "
                    f"'confirmDiscardOpenReview': true to proceed, or resolve the open review first."
                ),
                "sourceId": source_id,
                "status": current_status,
                # Lets a caller tell this recoverable "confirm and retry" 409
                # apart from the wrong-status 409 above, which retrying cannot fix.
                "confirmationRequired": "confirmDiscardOpenReview",
            },
        )

    # A re-scan of an already-approved (or previously drift-reviewed) source is
    # non-destructive: discovery merges onto the live assets and enrichment
    # regenerates only PENDING items. The scan-failed recovery path is a first
    # scan, so isRescan stays false there.
    is_rescan = source_type == SourceType.DATABASE and current_status in (
        SourceStatus.APPROVED,
        SourceStatus.RESCAN_REVIEW,
    )

    if source_type == SourceType.DOCUMENTS:
        if current_status in SOURCE_ACTIVE_STATUSES:
            return api_response(
                409,
                {
                    "error": (f"Source '{source_id}' is already being ingested (status: '{current_status}')."),
                    "sourceId": source_id,
                    "status": current_status,
                },
            )
        updated_item = {**item, "status": SourceStatus.REGISTERED, "updatedAt": now}
        for stale_field in PIPELINE_RUN_FIELDS:
            updated_item.pop(stale_field, None)
        try:
            _get_dao().put(
                updated_item,
                condition=Attr("status").is_in([SourceStatus.COMPLETED, SourceStatus.SCAN_FAILED]),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return api_response(
                    409,
                    {
                        "error": f"Source '{source_id}' is already being ingested.",
                        "sourceId": source_id,
                    },
                )
            logger.exception("ddb_put_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        tenant_id = item.get("tenantId")
        if not tenant_id:
            return api_response(500, {"error": "Internal server error"})

        stored_config = item.get("extractionConfig", {})
        try:
            extraction_config = merge_extraction_config(
                ExtractionConfig.model_validate(stored_config) if stored_config else None
            )
        except (ValidationError, ValueError):
            logger.exception("invalid_stored_extraction_config", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        sqs_body: dict[str, Any] = {
            "namespace_id": namespace_id,
            "doc_source_id": source_id,
            "tenant_id": tenant_id,
            "source_type": item.get("docSourceType", "s3"),
            "s3_prefixes": item.get("s3Prefixes", []),
            "extraction_config": extraction_config,
        }
        if item.get("sourceBucketArn"):
            sqs_body["source_bucket_arn"] = item["sourceBucketArn"]
        if item.get("roleArn"):
            sqs_body["role_arn"] = item["roleArn"]
        try:
            _get_sqs().send_message(
                QueueUrl=_INGESTION_QUEUE_URL,
                MessageBody=json.dumps(sqs_body, default=str),
            )
        except ClientError:
            logger.exception("sqs_send_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        return api_response(200, _item_to_detail(updated_item))

    else:  # DATABASE
        if current_status in SOURCE_ACTIVE_STATUSES:
            return api_response(
                409,
                {
                    "error": (f"Source '{source_id}' has an active scan (status: '{current_status}')."),
                    "sourceId": source_id,
                    "status": current_status,
                },
            )
        scan_job_sk = now
        try:
            _get_scan_dao().put(
                {
                    "PK": f"SRC#{source_id}",
                    "SK": scan_job_sk,
                    "sourceId": source_id,
                    "namespaceId": namespace_id,
                    "status": "IN_PROGRESS",
                    "scanType": "full",
                    "startedAt": now,
                    "createdAt": now,
                }
            )
        except ClientError:
            logger.exception("ddb_put_scan_job_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        # Update source status to SCANNING so callers see the active state immediately
        try:
            _get_dao().update(
                {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
                {"status": SourceStatus.SCANNING, "updatedAt": now},
            )
        except ClientError:
            logger.exception("ddb_update_status_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        try:
            _get_sqs().send_message(
                QueueUrl=_SCAN_QUEUE_URL,
                MessageBody=json.dumps(
                    {
                        "datasourceId": f"DS#{source_id}",
                        "scanJobId": f"SCAN#{str(uuid.uuid4())}",
                        "scanJobPK": f"SRC#{source_id}",
                        "scanJobSK": scan_job_sk,
                        "namespaceId": namespace_id,
                        "scanType": "full",
                        # Set only when re-scanning an already-approved (or drift-
                        # review) source. Drives the merge-onto-live-assets path in
                        # discovery and RESCAN_REVIEW routing in enrichment.
                        "isRescan": is_rescan,
                        # True ONLY when the source was already in RESCAN_REVIEW,
                        # i.e. a prior re-scan is still open and un-approved. Only
                        # then are the live assets an interim merge and the S3
                        # backup blob the approved pre-image discovery must
                        # reconstruct from. When re-scanning from APPROVED the live
                        # assets ARE the approved baseline; a leftover backup blob
                        # (from before delete-on-resolve shipped, or a paged-approve
                        # gap) is stale and must be ignored. Blob presence alone is
                        # NOT proof of an open review — this flag is.
                        "hadOpenRescan": current_status == SourceStatus.RESCAN_REVIEW,
                    }
                ),
            )
        except ClientError:
            logger.exception("sqs_send_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

        return api_response(202, {"sourceId": source_id, "scanJobId": scan_job_sk, "status": "IN_PROGRESS"})


# ---------------------------------------------------------------------------
# Lambda entry-point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for the Sources API.

    Dispatches the API Gateway proxy request to the matching source-registry
    route and returns the proxy response. Any unhandled exception is logged and
    converted to a 500 so the API never leaks an internal error to the caller.

    Args:
        event: API Gateway proxy integration event (method, resource, path
            parameters, and body).
        context: Lambda runtime context — threaded to cleanup helpers so they
            can derive a wall-clock deadline from the real remaining time.

    Returns:
        API Gateway proxy response dict with status code and JSON body.
    """
    try:
        return _route(event, context)
    except Exception:
        logger.exception("unhandled_error")
        return api_response(500, {"error": "Internal server error"})


def _route(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    http_method: str = event.get("httpMethod", "")
    resource: str = event.get("resource", "")
    # REST API Gateway proxy integration passes pathParameters exactly as they
    # appear in the request URL — still percent-encoded. Smithy-generated
    # clients (and the web app through them) encode every httpLabel per
    # RFC 3986, so a non-ASCII tableId such as "db.商品マスタ" arrives as
    # "db.%E5%95%86%E5%93%81%E3%83%9E%E3%82%B9%E3%82%BF" and would never match
    # the stored asset name. Decode once here, at the single extraction point,
    # so every route below sees the literal identifier. unquote() leaves
    # strings without escape sequences untouched, so already-decoded values
    # (e.g. in unit-test events) pass through unchanged.
    path_params: dict[str, str] = {
        key: unquote(value) for key, value in (event.get("pathParameters") or {}).items() if value is not None
    }
    namespace_id: str = path_params.get("namespaceId", "")

    logger.info("request_received", method=http_method, resource=resource, namespace_id=namespace_id)

    if not namespace_id:
        return api_response(400, {"error": "namespaceId is required"})
    try:
        validate_namespace_id(namespace_id, "namespaceId")
    except ValueError as exc:
        return api_response(400, {"error": str(exc)})

    if resource == "/namespaces/{namespaceId}/sources":
        if http_method == "GET":
            return _handle_list(event, namespace_id)
        if http_method == "POST":
            return _handle_create(event, namespace_id)

    if resource == "/namespaces/{namespaceId}/sources/upload-urls" and http_method == "POST":
        return _handle_upload_urls(event, namespace_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}":
        source_id: str = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        if http_method == "GET":
            return _handle_get(namespace_id, source_id)
        if http_method == "DELETE":
            return _handle_delete(namespace_id, source_id, context)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/rescan" and http_method == "POST":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_rescan(event, namespace_id, source_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/approve" and http_method == "POST":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_approve_source(event, namespace_id, source_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/reject" and http_method == "POST":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_reject_source(event, namespace_id, source_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables" and http_method == "GET":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_list_tables(event, namespace_id, source_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}" and http_method == "GET":
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        if not source_id or not table_id:
            return api_response(400, {"error": "sourceId and tableId are required"})
        return _handle_get_table(namespace_id, source_id, table_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review" and http_method == "PUT":
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        if not source_id or not table_id:
            return api_response(400, {"error": "sourceId and tableId are required"})
        return _handle_review_table(event, namespace_id, source_id, table_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keep" and http_method == "PUT":
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        if not source_id or not table_id:
            return api_response(400, {"error": "sourceId and tableId are required"})
        return _handle_keep_rescan_removal(event, namespace_id, source_id, table_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata" and http_method == "PATCH":
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        if not source_id or not table_id:
            return api_response(400, {"error": "sourceId and tableId are required"})
        return _handle_update_table_metadata(event, namespace_id, source_id, table_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys" and http_method == "PATCH":
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        if not source_id or not table_id:
            return api_response(400, {"error": "sourceId and tableId are required"})
        return _handle_update_table_keys(event, namespace_id, source_id, table_id)

    if (
        resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review"
        and http_method == "PUT"
    ):
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        column_name = path_params.get("columnName", "")
        if not source_id or not table_id or not column_name:
            return api_response(400, {"error": "sourceId, tableId, and columnName are required"})
        return _handle_review_column(event, namespace_id, source_id, table_id, column_name)

    if (
        resource == "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata"
        and http_method == "PATCH"
    ):
        source_id = path_params.get("sourceId", "")
        table_id = path_params.get("tableId", "")
        column_name = path_params.get("columnName", "")
        if not source_id or not table_id or not column_name:
            return api_response(400, {"error": "sourceId, tableId, and columnName are required"})
        return _handle_update_column_metadata(event, namespace_id, source_id, table_id, column_name)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/scan" and http_method == "GET":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_list_scan_jobs(namespace_id, source_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}" and http_method == "GET":
        source_id = path_params.get("sourceId", "")
        job_id = path_params.get("jobId", "")
        if not source_id or not job_id:
            return api_response(400, {"error": "sourceId and jobId are required"})
        return _handle_get_scan_job(namespace_id, source_id, job_id)

    if resource == "/namespaces/{namespaceId}/sources/{sourceId}/metadata" and http_method == "PUT":
        source_id = path_params.get("sourceId", "")
        if not source_id:
            return api_response(400, {"error": "sourceId is required"})
        return _handle_update_metadata(event, namespace_id, source_id)

    return api_response(404, {"error": f"Unknown route: {http_method} {resource}"})
