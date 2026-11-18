# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The OnboardingAgent's tools for the source lifecycle: register, scan, approve, rescan, delete.

Wraps COA's unified-sources API. One ``create_source`` covers every DATABASE sub-type
(``JDBC_DATABASE``, ``GLUE_DATABASE``, ``CUSTOM_CONNECTOR``, ``DATABRICKS_SQL_WAREHOUSE``,
``RDF_SPARQL``) because they differ only in the nested configuration; ``create_document_source``
covers DOCUMENTS sources. The SQL sub-types share one status machine; ``RDF_SPARQL`` only verifies
that its endpoint answers (``REGISTERED`` to ``APPROVED`` or ``SCAN_FAILED``).

A tool that starts asynchronous work waits for it (``tools/polling.py``), so the agent gets one
outcome per call instead of a status string to re-poll.

Request bodies are built from the generated models, which refuse a field they do not define.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Literal

import structlog
from coa_common.constants import GRAPH_BACKED_SUB_TYPES
from coa_common.sparql_config import GRAPH_SOURCES_DISABLED_MESSAGE
from coa_control_plane_server.models.create_database_source_input import CreateDatabaseSourceInput
from coa_control_plane_server.models.create_document_source_input import CreateDocumentSourceInput
from coa_control_plane_server.models.create_source_input import CreateSourceInput
from coa_control_plane_server.models.custom_connector_configuration import CustomConnectorConfiguration
from coa_control_plane_server.models.databricks_sql_warehouse_configuration import DatabricksSqlWarehouseConfiguration
from coa_control_plane_server.models.extraction_config import ExtractionConfig
from coa_control_plane_server.models.glue_configuration import GlueConfiguration
from coa_control_plane_server.models.glue_execution_engine import GlueExecutionEngine
from coa_control_plane_server.models.jdbc_configuration import JdbcConfiguration
from coa_control_plane_server.models.put_source_rescan_schedule_request_content import (
    PutSourceRescanScheduleRequestContent,
)
from coa_control_plane_server.models.source_status import SourceStatus
from coa_control_plane_server.models.source_type import SourceType
from coa_control_plane_server.models.sparql_configuration import SparqlConfiguration
from coa_control_plane_server.models.sparql_store import SparqlStore
from coa_control_plane_server.models.update_source_metadata_request_content import UpdateSourceMetadataRequestContent
from pydantic import BaseModel, ValidationError
from strands import tool
from strands.types.tools import AgentTool

from coa_workflow_agents.clients.coa_api_client import (
    LIST_SOURCE_SCAN_JOBS_MAX_RESULTS,
    LIST_SOURCES_MAX_RESULTS,
    CoaApiClient,
    CoaApiError,
    build_request,
    is_client_token_conflict,
)
from coa_workflow_agents.tools._shared import (
    as_list,
    clamp,
    collect_pages,
    read_local_file,
    safe_relative_path,
    tolerate_initial_not_found,
)
from coa_workflow_agents.tools.polling import ignore_stale_failure, poll_to_terminal

logger = structlog.get_logger(__name__)

_PENDING_REVIEW_STATE = SourceStatus.PENDING_REVIEW.value
_RESCAN_REVIEW_STATE = SourceStatus.RESCAN_REVIEW.value
_REVIEW_READY_STATES = {_PENDING_REVIEW_STATE, _RESCAN_REVIEW_STATE}
_APPROVED_STATE = SourceStatus.APPROVED.value
_APPROVAL_FAILED_STATE = SourceStatus.APPROVAL_FAILED.value
# States COA's ApproveSource accepts; RESCAN_REVIEW is approving a re-scan of an APPROVED source.
_APPROVAL_RETRYABLE_STATES = {_PENDING_REVIEW_STATE, _APPROVAL_FAILED_STATE, _RESCAN_REVIEW_STATE}
# A source already mid-approval (a prior ApproveSource still in flight) rejoins the poll; it is
# not "not approvable".
_APPROVAL_IN_FLIGHT_STATE = SourceStatus.APPROVING.value

# A reject ends REJECTED (a first-scan review) or APPROVED (rejecting a re-scan restores the
# previously approved scan), or REJECTION_FAILED.
_REJECTION_FAILED_STATE = SourceStatus.REJECTION_FAILED.value
_REJECT_RETRYABLE_STATES = {_PENDING_REVIEW_STATE, _REJECTION_FAILED_STATE, _RESCAN_REVIEW_STATE}
_REJECT_IN_FLIGHT_STATE = SourceStatus.REJECTING.value
_REJECTED_STATE = SourceStatus.REJECTED.value
_REJECT_END_STATES = {_REJECTED_STATE, _APPROVED_STATE}

_SCAN_FAILED_STATE = SourceStatus.SCAN_FAILED.value


def _last_scan_job_id(source: dict[str, Any]) -> Any:
    """``databaseDetails.lastScanJobId`` from a ``GetSource`` response, or None.

    COA models it only on the database-source detail (``DatabaseSourceDetail``),
    which covers every source type this package onboards.
    """
    details = source.get("databaseDetails")
    return details.get("lastScanJobId") if isinstance(details, dict) else None


_DEFAULT_APPROVE_TIMEOUT_S = 600

# COA refuses DeleteSource (409) while a scan, enrichment or delete is active, so a
# repeat delete mid-delete rejoins the poll. The worker removes the row last: the end
# state is GetSource 404, and DELETE_FAILED is retryable.
_DELETING_STATE = SourceStatus.DELETING.value
_DELETE_FAILED_STATE = SourceStatus.DELETE_FAILED.value
_DELETE_BLOCKED_STATES = {
    s.value
    for s in (
        SourceStatus.REGISTERED,
        SourceStatus.SCANNING,
        SourceStatus.SCANNING_ENTITY_EXTRACTION,
        SourceStatus.SCANNING_KG_BUILD,
        SourceStatus.ENRICHING,
    )
}
# Sentinel status the delete poll reports once GetSource answers 404.
_DELETED_SENTINEL = SourceStatus.DELETED.value
_DEFAULT_DELETE_TIMEOUT_S = 900


# COA's DisplayName for a source name: 1-128 characters.
_SOURCE_NAME_MAX_LEN = 128


def _name_held_by_deleting_source(exc: CoaApiError, replaced: list[str]) -> str | None:
    """The id of the source holding a document-source name, if it is being deleted (or its delete failed).

    COA answers a taken document-source name with a 409 naming the holder
    (``{error, sourceId, status}``). The holder counts when this call replayed it (its id is in
    ``replaced``) or its status is DELETING or DELETE_FAILED, whoever created it. Any other
    409, a clientToken conflict, or a live holder returns None.
    """
    if exc.status_code != 409 or is_client_token_conflict(exc):
        return None
    try:
        body = json.loads(exc.body)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    holder = body.get("sourceId")
    if holder in replaced or body.get("status") in (_DELETING_STATE, _DELETE_FAILED_STATE):
        return str(holder) if holder else (replaced[-1] if replaced else None)
    return None


def _create_source_client_token(namespace_id: str, body: dict[str, Any]) -> str:
    """Deterministic ``clientToken`` for a CreateSource request.

    The sha256 hex (64 chars, COA's maximum token length) of the namespace and
    the canonical request body. The same source config therefore always sends
    the same token, so an agent that re-calls ``create_source`` after a lost
    response (or the client's own transport retry) gets the original source
    back from COA instead of registering a duplicate. A different config is a
    different token, so it creates normally.
    """
    canonical = json.dumps([namespace_id, body], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _next_create_source_token(token: str, deleted_source_id: str) -> str:
    """The token to resend with when ``token``'s replayed source has been deleted.

    Deterministic, so a repeat ``create_source`` call walks the same chain and
    lands on the same recovered source instead of registering another one.
    """
    return hashlib.sha256(f"{token}:{deleted_source_id}".encode()).hexdigest()


# How long a just-created source may still read 404 (GetSource is eventually
# consistent) before the 404 is believed. Not measured: chosen as a generous
# bound on GetSource read-after-write lag; raise it if real 404s are misread.
_NEW_SOURCE_NOT_FOUND_GRACE_S = 15.0

# COA replays a CreateSource with a seen clientToken for 24 h and marks the
# replay with this header (COA external-docs namespaces.md; coa_common.idempotency
# REPLAY_HEADER), even if the source it created was deleted since.
_REPLAY_HEADER = "x-idempotent-replay"
# A replayed source is only judged deleted after this many 404s, spaced by
# _SOURCE_GONE_RECHECK_S, which together span the grace a just-created source gets:
# judging it sooner would register a duplicate of a source that exists.
_SOURCE_GONE_RECHECK_S = 1.0
_SOURCE_GONE_CHECKS = int(_NEW_SOURCE_NOT_FOUND_GRACE_S / _SOURCE_GONE_RECHECK_S)
# Most resends create_source makes past deleted replays in one call: 3 resends,
# so at most 4 CreateSource calls; the 4th deleted replay raises.
_MAX_DELETED_REPLAY_RESENDS = 3


# The CreateDatabaseSourceInput config members, one per sub-type, and the ones UpdateSourceMetadata takes.
_DATABASE_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "jdbcConfiguration": JdbcConfiguration,
    "glueConfiguration": GlueConfiguration,
    "customConnectorConfiguration": CustomConnectorConfiguration,
    "databricksSqlWarehouseConfiguration": DatabricksSqlWarehouseConfiguration,
    "sparqlConfiguration": SparqlConfiguration,
}
_UPDATABLE_CONFIGS = ("glueConfiguration", "jdbcConfiguration", "customConnectorConfiguration")
# COA answers 400 to a configuration update on these sub-types.
_NON_UPDATABLE_CONFIGS = {"databricksSqlWarehouseConfiguration": "a Databricks", "sparqlConfiguration": "an RDF_SPARQL"}

# RDF_SPARQL: store and authMode default to neptune / sigv4, and COA refuses credentialSecretArn for neptune.
_SPARQL_DEFAULT_STORE = SparqlStore.NEPTUNE.value


def _build[M: BaseModel](model: type[M], label: str, **fields: Any) -> M:
    """``build_request`` with a pydantic failure re-raised as a short ``ValueError`` naming each field."""
    try:
        return build_request(model, **fields)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"invalid {label}: {problems}") from exc


def _display_name(name: Any) -> str:
    """``name`` trimmed, which must then be 1-128 characters (COA's DisplayName)."""
    display_name = name.strip() if isinstance(name, str) else ""
    if not display_name or len(display_name) > _SOURCE_NAME_MAX_LEN:
        raise ValueError(f"name must be 1-{_SOURCE_NAME_MAX_LEN} characters and not blank, got {name!r}")
    return display_name


def _database_config(database_source: Any, allowed: tuple[str, ...]) -> tuple[str, BaseModel]:
    """The one configuration in ``database_source`` as ``(key, generated model)``.

    ``database_source`` comes from the model, so it is checked here, with no API call: it must be an
    object holding exactly one of the ``allowed`` configuration keys, whose fields are the model's
    (an unknown field, a missing required one or a bad enum value is a ``ValueError``).
    """
    if not isinstance(database_source, dict):
        raise ValueError(f"database_source must be an object, got {type(database_source).__name__}")
    present = [key for key in allowed if key in database_source]
    if len(present) != 1:
        raise ValueError(
            f"database_source must contain exactly one config key ({', '.join(sorted(allowed))}); "
            f"found {sorted(present) if present else 'none'}"
        )
    key = present[0]
    config = database_source[key]
    if not isinstance(config, dict):
        raise ValueError(f"database_source.{key} must be an object, got {type(config).__name__}")
    model = _build(_DATABASE_CONFIG_MODELS[key], f"database_source.{key}", **config)
    if isinstance(model, SparqlConfiguration):
        _validate_sparql_configuration(model)
    if (
        isinstance(model, GlueConfiguration)
        and model.execution_engine == GlueExecutionEngine.REDSHIFT
        and not model.redshift_workgroup
    ):
        raise ValueError("database_source.glueConfiguration.executionEngine REDSHIFT requires redshiftWorkgroup")
    return key, model


def _validate_sparql_configuration(config: SparqlConfiguration) -> None:
    """The SparqlConfiguration rule COA enforces that the model cannot express."""
    store = config.store.value if config.store else _SPARQL_DEFAULT_STORE
    if store == _SPARQL_DEFAULT_STORE and config.credential_secret_arn:
        raise ValueError("sparqlConfiguration.credentialSecretArn is not accepted for a neptune store (it uses SigV4)")


def _raise_if_graph_sources_disabled(exc: CoaApiError) -> None:
    """Turn COA's graph-sources-off 400 into an actionable error; otherwise do nothing."""
    if exc.status_code == 400 and GRAPH_SOURCES_DISABLED_MESSAGE in exc.body:
        raise RuntimeError(
            "This COA deployment has graph (RDF_SPARQL) sources turned off, so the source was not registered. "
            "An operator must redeploy COA with graph sources on; nothing to retry until then."
        ) from exc


def _extraction_config(extraction_config: Any) -> ExtractionConfig:
    """``extraction_config`` as the generated ExtractionConfig, refusing an unknown or invalid field."""
    if not isinstance(extraction_config, dict):
        raise ValueError("extraction_config must be an object")
    return _build(ExtractionConfig, "extraction_config", **extraction_config)


# DOCUMENTS sources (CreateDocumentSourceInput / GetSourceUploadUrls).
_DOCUMENT_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_MAX_UPLOAD_FILES = 100  # GetSourceUploadUrls files @length(max: 100)
_DOCUMENTS_COMPLETED_STATE = SourceStatus.COMPLETED.value
_DEFAULT_DOCUMENT_TIMEOUT_S = 3600


def _document_upload_plan(local_files: list[str]) -> list[tuple[str, str, str]]:
    """Validate every local path before any API call: ``[(resolved path, filename, contentType)]``.

    Each path is confined to the working directory (``safe_relative_path``), must be an
    existing regular file of a supported type, and filenames must be unique (COA keys
    the upload by filename).
    """
    if not isinstance(local_files, list) or not local_files:
        raise ValueError("local_files must be a non-empty list of file paths")
    if len(local_files) > _MAX_UPLOAD_FILES:
        raise ValueError(f"at most {_MAX_UPLOAD_FILES} files per document source upload, got {len(local_files)}")
    plan: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for path in local_files:
        resolved = safe_relative_path(str(path), label="local_files entry")
        if not os.path.isfile(resolved):
            raise ValueError(f"local_files entry is not a file: {path!r}")
        filename = os.path.basename(resolved)
        content_type = _DOCUMENT_CONTENT_TYPES.get(os.path.splitext(filename)[1].lower())
        if content_type is None:
            raise ValueError(
                f"{path!r} is not a supported document type ({', '.join(sorted(_DOCUMENT_CONTENT_TYPES))})"
            )
        relative = os.path.relpath(resolved, os.path.realpath(os.getcwd()))
        if any(part.startswith(".") for part in relative.split(os.sep)) or filename in seen:
            raise ValueError(f"document filenames must be unique and not hidden: {path!r}")
        seen.add(filename)
        plan.append((resolved, filename, content_type))
    return plan


def _scan_job_warnings(job: dict[str, Any]) -> list[str]:
    """Plain-language warnings for a scan that succeeded but is incomplete or mis-filtered."""
    warnings: list[str] = []
    failed = job.get("tablesFailed")
    if isinstance(failed, int) and failed > 0:
        names = as_list(job.get("failedTables"))
        shown = f" (e.g. {', '.join(str(n) for n in names)})" if names else ""
        warnings.append(
            f"{failed} table(s) were listed but could not be read{shown}: they have no columns, comments or "
            "keys, and AI enrichment described them over the gap. Do not treat this scan as complete."
        )
    warnings.extend(str(w) for w in as_list(job.get("filterWarnings")))
    return warnings


def build_onboarding_tools(client: CoaApiClient, namespace_id: str) -> list[AgentTool]:
    """Build the OnboardingAgent's tool set, bound to one namespace and API client."""

    def replayed_source_is_deleted(source_id: str) -> bool:
        """True if a replayed CreateSource's source has been deleted (or is being).

        Deleted: GetSource answers 404 on every one of a few spaced reads, or
        the source is ``DELETING`` / ``DELETE_FAILED`` (COA keeps the row in
        those states until its teardown worker finishes, or for good if that
        fails). Any other status is a live source.
        """
        for attempt in range(_SOURCE_GONE_CHECKS):
            if attempt:
                time.sleep(_SOURCE_GONE_RECHECK_S)
            try:
                source = client.get_source(namespace_id, source_id)
            except CoaApiError as exc:
                if exc.status_code == 404:
                    continue
                raise
            return source.get("status") in (_DELETING_STATE, _DELETE_FAILED_STATE)
        return True

    def create_past_deleted_replays(request: CreateSourceInput, label: str) -> tuple[dict[str, Any], list[str]]:
        """CreateSource with the deterministic token; resend past replays of deleted sources.

        Returns COA's response and the ids of the deleted sources COA replayed
        (empty when the first create was fresh or replayed a live source). A name 409 held by a
        source that is being deleted raises RuntimeError, on the first create as well as a resend.
        """
        token = _create_source_client_token(
            namespace_id, request.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        replaced: list[str] = []
        while True:
            headers: dict[str, str] = {}
            try:
                result = client.create_source(namespace_id, request, client_token=token, headers_out=headers)
            except CoaApiError as exc:
                _raise_if_graph_sources_disabled(exc)
                holder = _name_held_by_deleting_source(exc, replaced)
                if holder is not None:
                    # Document sources only (a DATABASE create has no name 409): COA
                    # keeps a source's name reserved until its teardown removes the row.
                    raise RuntimeError(
                        f"{label}: the source {holder} that holds this name is still being deleted "
                        f"(or its delete failed), so COA refuses the name: wait for the teardown to finish, "
                        f"or use another name. COA said: {exc.body[:300]}"
                    ) from exc
                raise
            source_id = result.get("sourceId")
            if (
                headers.get(_REPLAY_HEADER, "").lower() != "true"
                or not source_id
                or not replayed_source_is_deleted(str(source_id))
            ):
                return result, replaced
            # COA replayed a create whose source has since been deleted.
            replaced.append(str(source_id))
            logger.warning("create_source_replay_resent", attempt=len(replaced))
            if len(replaced) > _MAX_DELETED_REPLAY_RESENDS:
                raise RuntimeError(
                    f"{label}: COA keeps replaying deleted sources {replaced} for this name and config; "
                    "use a different name, or retry after 24 hours"
                )
            token = _next_create_source_token(token, str(source_id))

    @tool
    def create_source(
        name: str,
        source_type: Literal["DATABASE"],
        database_source: dict[str, Any],
        metadata_enrichment_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Register a new data source with COA (``CreateSource``).

        ``database_source`` must contain EXACTLY ONE of these keys, matching
        the sub-type being onboarded:

        - ``jdbcConfiguration``: for PostgreSQL, MySQL, Redshift, Oracle,
          SQL Server, or Snowflake over JDBC. Required fields: ``engine``
          (one of POSTGRESQL|MYSQL|REDSHIFT|ORACLE|SQLSERVER|SNOWFLAKE),
          ``host``, ``port``, ``databaseName``. Usually also
          ``credentialSecretArn`` (Secrets Manager ARN for DB credentials).
          Optional: ``schemaFilter`` / ``schemaExcludeFilter`` (system
          schemas are always excluded), ``tableFilter`` /
          ``tableExcludeFilter``, ``crossAccountRoleArn`` (an IAM role COA
          assumes to read the secret in another account), and for SNOWFLAKE
          ``warehouse`` (the virtual warehouse discovery runs in — Snowflake
          needs one) and ``role`` (a Snowflake RBAC role, not an IAM role).
        - ``glueConfiguration``: for an existing Glue Data Catalog database.
          Required: ``catalogId`` (12-digit account id, or
          ``account:catalogName``), ``region``, ``databaseName``. Optional:
          ``tableFilter`` (Glue's own REGULAR EXPRESSION syntax — the one
          filter that is not a glob), ``tableExcludeFilter`` (a glob),
          ``crossAccountRoleArn``, ``athenaDataCatalogName`` (the nested
          catalog when the database is not in the account's root catalog),
          ``executionEngine`` (``ATHENA``, the default, or ``REDSHIFT`` to
          query through Redshift Serverless — then ``redshiftWorkgroup`` is
          required).
        - ``customConnectorConfiguration``: for a customer-authored Athena
          Query Federation Lambda connector. Required: ``connectorFunctionArn``
          (a full Lambda ARN in this deployment's own region),
          ``databaseName``. Optional: ``tableFilter`` / ``tableExcludeFilter``.
        - ``databricksSqlWarehouseConfiguration``: for a Databricks SQL
          Warehouse (COA-operated connector). Required: ``workspaceHostname``,
          ``httpPath``, ``databricksCatalog``, ``databaseName`` (the Unity
          Catalog schema), ``credentialSecretArn``, ``crossAccountRoleArn``.
          The role MUST be named ``<resource prefix>datasource-access-*``
          (COA rejects any other name) and its trust policy conditioned on
          the namespace's ``datasourceExternalId``. Optional:
          ``tableFilter`` / ``tableExcludeFilter``.
        - ``sparqlConfiguration``: for an RDF graph store queried over SPARQL
          (sub-type ``RDF_SPARQL``, e.g. Amazon Neptune). Required:
          ``dataEndpoint`` (a host, or ``https://host[:port][/path]``).
          Optional: ``store`` (``neptune``, the default), ``region`` (SigV4
          signing region; defaults to, and must equal, this deployment's
          region), ``port`` (default 8182), ``dataNamedGraph`` (an http(s)
          IRI confining queries to one named graph), ``authMode`` (``sigv4``,
          the default). ``credentialSecretArn`` is refused for ``neptune``.
          A graph source has a DIFFERENT lifecycle: there is no schema scan,
          no enrichment and no table review. COA registers it ``REGISTERED``,
          checks the endpoint is reachable, and moves it straight to
          ``APPROVED`` (queryable) or ``SCAN_FAILED`` — never call
          ``approve_source`` on it. Its classes come from
          an ontology imported separately (OntologyAgent). If the deployment
          has graph sources turned off, this raises with that explanation.

        Filters, except Glue's ``tableFilter``, are shell globs matched
        against the WHOLE name: ``*``, ``?``, ``[abc]``, several separated by
        ``|`` or ``,`` (``sales_*``, ``public|analytics``). They are NOT
        regular expressions — ``^``, ``$`` and ``.*`` match literally, and a
        regex-looking filter that matches nothing shows up as a
        ``filterWarnings`` entry on the scan job. Do not send ``externalId``:
        COA ignores it and derives the external id from the namespace.

        Retry-safe: the request carries a ``clientToken`` derived from the
        namespace, the ``name`` and the exact config, so calling this again
        with the SAME name and config within 24 hours returns the source the
        first call created rather than registering a duplicate. If that
        source was deleted in the meantime, COA still replays it; this tool
        detects that (the replayed source is gone, or ``DELETING`` /
        ``DELETE_FAILED``) and registers the source again, reporting the
        deleted id in ``replacedDeletedSourceIds``.

        Args:
            name: The source's display name in COA (``databaseSource.name``,
                required by COA; 1-128 characters after trimming
                leading/trailing spaces, punctuation allowed). COA still
                assigns the ``sourceId``.
            source_type: Always ``"DATABASE"`` — document sources are not
                supported by this tool.
            database_source: Exactly one of the config shapes above.
            metadata_enrichment_enabled: ``False`` skips AI enrichment
                (descriptions, synonyms, glossary terms, tags) so the source
                goes from scanning straight to ``PENDING_REVIEW`` with
                technical metadata only. Omit for COA's default (on).

        Returns:
            ``{sourceId, status, scanJobId, createdAt, name}``. ``status`` is
            freshly ``REGISTERED`` or already ``SCANNING`` — the scan
            pipeline (discovery -> enrichment) has started; call
            ``get_source_status`` to track it, or just call
            ``approve_source`` once it reaches ``PENDING_REVIEW``. (An
            ``RDF_SPARQL`` source instead ends ``APPROVED`` or
            ``SCAN_FAILED`` on its own after the reachability check.) Plus
            ``replacedDeletedSourceIds`` (a list) when the same name and
            config had been registered and then deleted within 24 hours —
            tell the caller a new source was registered in its place.
        """
        if source_type != SourceType.DATABASE.value:
            raise ValueError(f"source_type must be 'DATABASE', got {source_type!r}")
        display_name = _display_name(name)
        key, config = _database_config(database_source, tuple(_DATABASE_CONFIG_MODELS))
        db_source = _build(
            CreateDatabaseSourceInput,
            "database_source",
            name=display_name,
            metadata_enrichment_enabled=metadata_enrichment_enabled,
            **{key: config},
        )
        request = _build(CreateSourceInput, "create_source", source_type=SourceType.DATABASE, database_source=db_source)
        result, replaced = create_past_deleted_replays(request, "create_source")
        return {**result, "name": display_name, **({"replacedDeletedSourceIds": replaced} if replaced else {})}

    def upload_documents(plan: list[tuple[str, str, str]]) -> str:
        """Get upload URLs for ``plan`` and PUT each file; returns the ``uploadId``. URLs are never logged."""
        resp = client.get_source_upload_urls(namespace_id, [{"filename": f, "contentType": ct} for _, f, ct in plan])
        urls = {u.get("filename"): u.get("uploadUrl") for u in as_list(resp.get("uploadUrls")) if isinstance(u, dict)}
        upload_id = resp.get("uploadId")
        if not upload_id or any(not urls.get(f) for _, f, _ in plan):
            raise RuntimeError("COA did not return an uploadId and an upload URL for every file")
        for resolved, filename, content_type in plan:
            _, data = read_local_file(os.path.relpath(resolved), label=f"document {filename!r}")
            client.put_presigned(str(urls[filename]), data, content_type)
        return str(upload_id)

    @tool
    def create_document_source(
        name: str,
        local_files: list[str] | None = None,
        source_bucket_arn: str | None = None,
        s3_prefixes: list[str] | None = None,
        role_arn: str | None = None,
        extraction_config: dict[str, Any] | None = None,
        timeout_seconds: int = _DEFAULT_DOCUMENT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Register a DOCUMENTS source (PDF/DOCX/TXT/MD) and WAIT for ingestion to finish.

        Pass EXACTLY ONE of:

        - ``local_files``: paths to local documents (1-100; ``.pdf``,
          ``.docx``, ``.txt``, ``.md``; each up to 100 MiB), RELATIVE to the
          working directory (absolute paths and ``..`` escapes are rejected).
          The tool gets upload URLs (``GetSourceUploadUrls``), uploads each
          file, then creates the source from the upload (sub-type
          ``LOCAL_UPLOAD``). File names must be unique.
        - ``source_bucket_arn`` (``arn:aws:s3:::<bucket>``): ingest from an S3
          bucket (sub-type ``S3``), optionally limited to ``s3_prefixes``
          (up to 1000) and read through ``role_arn`` (a cross-account role).
          COA only accepts a bucket tagged to authorize this namespace.

        A documents source has its OWN lifecycle: ``SCANNING`` ->
        ``SCANNING_ENTITY_EXTRACTION`` -> ``SCANNING_KG_BUILD`` ->
        ``COMPLETED`` (or ``SCAN_FAILED``). There is no table review and no
        approve step — never call ``approve_source`` on it. Ingestion is LLM-backed and billable and can
        take a long time.

        Args:
            name: Display name (1-128 characters; unique among ALL the
                namespace's sources, database ones included — COA refuses a
                duplicate).
            local_files: Local documents to upload, or omit.
            source_bucket_arn: S3 bucket to ingest from, or omit.
            s3_prefixes: S3 key prefixes within the bucket (S3 only).
            role_arn: IAM role for cross-account S3 access (S3 only).
            extraction_config: Optional ExtractionConfig: ``extractionMode``
                (``continuous``|``separated``), ``useBatchInference``,
                ``enableVersioning``, ``enablePropositionExtraction``,
                ``preferredEntityClassifications`` (<=100 labels),
                ``inferEntityClassifications``, ``preferredTopics`` (<=100),
                ``enableTableExtraction`` (Textract for PDF tables — costs
                more), ``chunkSize`` (0-4096 tokens; 0 = default 256),
                ``chunkOverlap`` (0-512; 0 = default 25),
                ``deletePrevVersions``, ``bedrockModelArn``.
            timeout_seconds: Give up waiting after this long (default 3600s).
                The source keeps ingesting; follow it with
                ``get_source_status``.

        Returns:
            The terminal ``GetSource`` response (``status == "COMPLETED"``),
            whose ``documentDetails`` reports ``filesTotal``,
            ``filesSkipped``, ``filesErrored`` and ``preprocessingIssues`` —
            tell the caller about any skipped or errored file. Plus
            ``replacedDeletedSourceIds`` when the same S3 source had been
            registered and deleted within 24 hours (COA replays the earlier
            create; the tool detects the deleted source and registers anew).

        Raises:
            ValueError: invalid arguments (no API call made).
            RuntimeError: ingestion ended ``SCAN_FAILED`` (see
                ``documentDetails.errorMessage``), a source with this name
                already exists, a source holding this name is still being
                deleted or its delete failed (COA keeps the name until the
                teardown ends), or the wait timed out.
        """
        display_name = _display_name(name)
        if (local_files is None) == (source_bucket_arn is None):
            raise ValueError("pass exactly one of local_files or source_bucket_arn")
        fields: dict[str, Any] = {"name": display_name}
        if extraction_config is not None:
            fields["extraction_config"] = _extraction_config(extraction_config)
        if source_bucket_arn is not None:
            fields.update(
                source_bucket_arn=source_bucket_arn, s3_prefixes=s3_prefixes or None, role_arn=role_arn or None
            )
        else:
            if s3_prefixes or role_arn:
                raise ValueError("s3_prefixes and role_arn apply only to an S3 (source_bucket_arn) source")
            plan = _document_upload_plan(local_files or [])
            # COA refuses a duplicate source name (any source type: its ByName
            # index has no type key), but only after the upload; check first so
            # a retry does not upload every file again.
            existing = [
                src
                for src in collect_pages(
                    lambda tok: client.list_sources(namespace_id, max_results=LIST_SOURCES_MAX_RESULTS, next_token=tok),
                    "sources",
                )
                if src.get("name") == display_name
            ]
            if existing:
                raise RuntimeError(
                    f"a source named {display_name!r} already exists (sourceId "
                    f"{existing[0].get('sourceId')}, status {existing[0].get('status')}); "
                    "check it with get_source_status or use a different name"
                )
            fields["upload_id"] = upload_documents(plan)
        doc_source = _build(
            CreateDocumentSourceInput, "document source", **{k: v for k, v in fields.items() if v is not None}
        )
        request = _build(
            CreateSourceInput, "create_document_source", source_type=SourceType.DOCUMENTS, document_source=doc_source
        )
        created, replaced = create_past_deleted_replays(request, "create_document_source")
        source_id = str(created.get("sourceId") or "")
        if not source_id:
            raise RuntimeError("COA did not return a sourceId for the new document source")
        # GetSource is an eventually consistent read, so right after the
        # create it can still answer 404; read that as not yet visible.
        fetch = tolerate_initial_not_found(
            lambda: client.get_source(namespace_id, source_id),
            {"sourceId": source_id, "status": SourceStatus.REGISTERED.value},
            _NEW_SOURCE_NOT_FOUND_GRACE_S,
        )

        final = poll_to_terminal(
            f"document source {source_id} ingestion",
            fetch,
            operation="document_source_ingest",
            is_terminal=lambda src: src.get("status") == _DOCUMENTS_COMPLETED_STATE,
            is_failure=lambda src: src.get("status") == _SCAN_FAILED_STATE,
            timeout_s=float(timeout_seconds),
        )
        return {**final, **({"replacedDeletedSourceIds": replaced} if replaced else {})}

    @tool
    def get_source_status(source_id: str) -> dict[str, Any]:
        """Get a source's current status and detail (``GetSource``, read-only).

        Use this to check progress through discovery/enrichment, or to
        confirm a source reached ``APPROVED``. This does NOT poll — it
        returns the status at the instant of the call.

        Args:
            source_id: The ``sourceId`` returned by ``create_source``.

        Returns:
            The full ``GetSource`` response: ``sourceId``, ``sourceType``,
            ``sourceSubType``, ``status`` (REGISTERED, SCANNING, ENRICHING,
            SCAN_FAILED, PENDING_REVIEW, RESCAN_REVIEW, APPROVING,
            APPROVAL_FAILED, REJECTING, REJECTION_FAILED, APPROVED,
            REJECTED, DELETING or DELETE_FAILED; a DOCUMENTS source instead SCANNING,
            SCANNING_ENTITY_EXTRACTION, SCANNING_KG_BUILD, COMPLETED or
            SCAN_FAILED), and ``databaseDetails`` (DATABASE) or
            ``documentDetails`` (DOCUMENTS: ``filesTotal``, ``filesSkipped``,
            ``filesErrored``, ``preprocessingIssues`` [{filename, type
            skipped|error, reason}], ``errorMessage``, and after the
            knowledge-graph build ``documentsProcessed``, ``chunksLLM``,
            ``chunksEmbed``, ``chunksGraph``). On a DATABASE ``SCAN_FAILED``, call
            ``get_scan_job_status`` for the actual failure reason (most
            commonly a network-connectivity problem reaching the database)
            before deciding whether to ``rescan_source``.
        """
        return client.get_source(namespace_id, source_id)

    @tool
    def list_sources(
        max_results: int = 50, source_type: Literal["DATABASE", "DOCUMENTS"] | None = None
    ) -> dict[str, Any]:
        """List every data source in this namespace, with its status (``ListSources``, read-only).

        Use this to DISCOVER what sources exist and where each one is in its
        lifecycle, without already knowing any ``sourceId`` — the entry point
        for a status query like "what sources are in this namespace and what
        state are they in?". Pages through ALL sources (not capped at the
        first ``max_results``), so the result is complete.

        Args:
            max_results: Page size for the underlying paginated API (default
                50, clamped to COA's 1-100 range). All pages are followed
                regardless, so this only sets how many sources are fetched per
                request, not a total cap.
            source_type: Only list ``DATABASE`` or only ``DOCUMENTS`` sources;
                omit for both.

        Returns:
            ``{sources: [{sourceId, name, status, ...}, ...], count}`` — one
            entry per source with its ``sourceId``, ``name`` and lifecycle
            ``status`` (REGISTERED, SCANNING, ENRICHING, SCAN_FAILED,
            PENDING_REVIEW, RESCAN_REVIEW, APPROVING, APPROVAL_FAILED,
            APPROVED, REJECTED, DELETING, ...).
        """
        page_size = clamp(max_results, 1, LIST_SOURCES_MAX_RESULTS)
        type_filter = {"source_type": source_type} if source_type else {}
        sources = collect_pages(
            lambda tok: client.list_sources(namespace_id, max_results=page_size, next_token=tok, **type_filter),
            "sources",
        )
        return {"sources": sources, "count": len(sources)}

    @tool
    def get_scan_job_status(source_id: str, scan_job_id: str) -> dict[str, Any]:
        """Get detail for one scan job, including its failure reason (``GetSourceScanJob``, read-only).

        Use this when ``get_source_status`` reports ``SCAN_FAILED`` (or a
        scan otherwise seems stuck/wrong) — the source's own ``status``
        field only tells you THAT discovery failed, not WHY. This call
        returns the scan job's ``errorMessage``, which is where the actual
        cause shows up: most commonly a network-connectivity failure
        (e.g. ``"Network: cannot reach <host>:<port> — timed out"`` when
        COA's VPC has no route to the database's VPC, or the database's
        security group doesn't allow COA's connector security group), or a
        credentials/authentication failure.

        Args:
            source_id: The source the scan job belongs to.
            scan_job_id: The scan job identifier — the ``scanJobId`` from
                ``create_source``'s response, or from ``get_source_status``.

        Returns:
            ``{jobId, sourceId, status, startedAt, completedAt?,
            errorMessage?, tablesDiscovered?, tablesProcessed?, tablesTotal?,
            tablesAdded?, tablesRemoved?, tablesModified?, tablesFailed?,
            failedTables?, filterWarnings?, partialResultWarnings?}``.
            ``errorMessage`` is only present when ``status`` is a failure
            status — absent, not empty, on a successful or in-progress scan.

            A ``COMPLETED`` scan can still be INCOMPLETE: ``tablesFailed``
            counts tables it listed but could not read (``failedTables``
            names some of them, possibly fewer) — those have no columns or
            keys, so the review is not complete. ``filterWarnings`` flags a
            regex-looking filter that matched nothing (filters are globs, so
            the scan ran without the intended filtering). When either is
            present, ``partialResultWarnings`` restates them in plain
            language: tell the caller before they approve.
        """
        job = client.get_source_scan_job(namespace_id, source_id, scan_job_id)
        warnings = _scan_job_warnings(job)
        return {**job, "partialResultWarnings": warnings} if warnings else job

    @tool
    def approve_source(source_id: str, timeout_seconds: int = _DEFAULT_APPROVE_TIMEOUT_S) -> dict[str, Any]:
        """Bulk-approve every PENDING table/column on a source, and WAIT for it to finish.

        Calls ``ApproveSource`` then polls ``GetSource`` internally until the
        source reaches ``APPROVED`` or a failure state — you get one call,
        one deterministic outcome. The source must be in an approvable state:
        ``PENDING_REVIEW``, ``RESCAN_REVIEW`` (approving a re-scan of an
        already-approved source) or ``APPROVAL_FAILED`` (fires ApproveSource
        then polls), ``APPROVING`` (a prior call is already in flight — this
        rejoins the poll without re-firing), or already ``APPROVED`` (returns
        immediately, idempotent). Any other status is surfaced as an error
        without attempting the poll (COA would return 409). COA's approve
        handler accepts ``RESCAN_REVIEW``, although the ApproveSource API doc
        lists only ``PENDING_REVIEW`` and ``APPROVAL_FAILED``.

        Retrying from ``APPROVAL_FAILED`` after a failed RE-SCAN approve runs
        COA's ordinary approve, not its re-scan finalize (COA only knows it
        was a re-scan while the source is ``RESCAN_REVIEW``), so tables the
        re-scan found removed are not cleaned up.

        Already-explicitly-reviewed tables/columns are left untouched; this
        only resolves tables/columns still ``PENDING_REVIEW``.

        Args:
            source_id: The source to approve.
            timeout_seconds: Give up and raise after this long (default 600s
                / 10 minutes). The approve worker is usually fast; raise this
                only for a source with an unusually large number of tables.

        Returns:
            The terminal ``GetSource`` response, with ``status == "APPROVED"``.

        Raises:
            RuntimeError: the source reached ``APPROVAL_FAILED`` (retryable —
                call this tool again), or the poll timed out, or the source
                was not in an approvable state to begin with.
        """
        current = client.get_source(namespace_id, source_id)
        current_status = current.get("status")
        if current_status == _APPROVED_STATE:
            return current
        if current_status not in _APPROVAL_RETRYABLE_STATES and current_status != _APPROVAL_IN_FLIGHT_STATE:
            raise RuntimeError(
                f"source {source_id}: ApproveSource requires PENDING_REVIEW, RESCAN_REVIEW, "
                f"APPROVAL_FAILED, APPROVING, or APPROVED, got {current_status!r}"
            )
        if current_status != _APPROVAL_IN_FLIGHT_STATE:
            # Only fire the mutating call if one isn't already in flight —
            # APPROVING means a prior call already started it; skip straight
            # to polling instead of double-POSTing.
            client.approve_source(namespace_id, source_id)
        return poll_to_terminal(
            f"source {source_id} approve",
            lambda: client.get_source(namespace_id, source_id),
            operation="source_approve",
            is_terminal=lambda s: s.get("status") == _APPROVED_STATE,
            is_failure=ignore_stale_failure(
                lambda s: s.get("status") == _APPROVAL_FAILED_STATE,
                started_in_failure=current_status == _APPROVAL_FAILED_STATE,
            ),
            timeout_s=float(timeout_seconds),
        )

    @tool
    def reject_source(source_id: str, timeout_seconds: int = _DEFAULT_APPROVE_TIMEOUT_S) -> dict[str, Any]:
        """Bulk-reject every PENDING table/column on a source, and WAIT for it to finish.

        Calls ``RejectSource`` then polls ``GetSource`` internally until the
        reject completes — one call, one deterministic outcome. The source
        must be in a rejectable state: ``PENDING_REVIEW`` (a first-scan
        review — ends ``REJECTED``), ``RESCAN_REVIEW`` (rejecting a re-scan
        of an approved source — ends back at ``APPROVED``, keeping the
        previously approved scan) or ``REJECTION_FAILED`` (retry).
        ``REJECTING`` (a prior call still in flight) rejoins the poll without
        re-firing; an already-``REJECTED`` source returns immediately
        (idempotent). Any other status is surfaced as an error without
        calling COA (COA would return 409) — including ``APPROVED``, so a
        re-call after successfully rejecting a re-scan raises rather than
        returning; check ``get_source_status`` instead.

        DO NOT blindly retry a ``REJECTION_FAILED`` source that was being
        re-scanned (it was ``APPROVED``, then ``RESCAN_REVIEW``): COA only
        knows a reject is a re-scan restore while the source is
        ``RESCAN_REVIEW``, so a retry from ``REJECTION_FAILED`` runs a full
        first-scan reject and ends ``REJECTED`` — discarding the previously
        approved source, which then has to be onboarded again. COA leaves a
        failed re-scan reject for manual intervention; ask the caller first.

        Args:
            source_id: The source to reject.
            timeout_seconds: Give up and raise after this long (default 600s).

        Returns:
            The terminal ``GetSource`` response, with ``status`` ``REJECTED``
            or (after rejecting a re-scan) ``APPROVED``.

        Raises:
            RuntimeError: the source reached ``REJECTION_FAILED`` (retryable
                for a first-scan review; for a re-scan see the warning above),
                the poll timed out, or the source was not in a rejectable
                state to begin with.
        """
        current = client.get_source(namespace_id, source_id)
        current_status = current.get("status")
        if current_status == _REJECTED_STATE:
            return current
        if current_status not in _REJECT_RETRYABLE_STATES and current_status != _REJECT_IN_FLIGHT_STATE:
            raise RuntimeError(
                f"source {source_id}: RejectSource requires PENDING_REVIEW, RESCAN_REVIEW, "
                f"REJECTION_FAILED, REJECTING, or REJECTED, got {current_status!r}"
            )
        if current_status != _REJECT_IN_FLIGHT_STATE:
            # REJECTING means a prior call already started the reject; skip
            # straight to polling instead of double-POSTing.
            client.reject_source(namespace_id, source_id)
        return poll_to_terminal(
            f"source {source_id} reject",
            lambda: client.get_source(namespace_id, source_id),
            operation="source_reject",
            is_terminal=lambda s: s.get("status") in _REJECT_END_STATES,
            is_failure=ignore_stale_failure(
                lambda s: s.get("status") == _REJECTION_FAILED_STATE,
                started_in_failure=current_status == _REJECTION_FAILED_STATE,
            ),
            timeout_s=float(timeout_seconds),
        )

    @tool
    def rescan_source(
        source_id: str,
        confirm_discard_open_review: bool = False,
        timeout_seconds: int = _DEFAULT_APPROVE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Re-trigger discovery/enrichment for an existing source (``RescanSource``), and WAIT for it to finish.

        This is the retry path after a ``SCAN_FAILED`` source's underlying
        cause is fixed outside of COA — most commonly a network-
        connectivity problem between COA's VPC and the database's VPC
        (see ``get_scan_job_status`` to find the actual cause first). It
        re-runs the SAME source's scan; it does NOT create a new source,
        so prefer this over calling ``create_source`` again for the same
        database once connectivity/credentials are fixed.

        Also the correct way to pick up a schema change on an already
        ``APPROVED`` source (new/changed columns, etc.) — not just a
        failure-recovery tool.

        Each call sends a fresh ``clientToken``, so the client can safely
        retry a transient network failure without starting a second scan,
        while a deliberate second call (e.g. after fixing connectivity)
        still starts a new one.

        Args:
            source_id: The source to rescan.
            confirm_discard_open_review: Only meaningful when the source is
                currently ``RESCAN_REVIEW`` — an open re-scan review that a
                fresh rescan would discard. COA returns 409 if the source
                is in ``RESCAN_REVIEW`` and this is left ``False``; set it
                ``True`` only once the caller has confirmed they want to
                discard that open review (ignored, harmlessly, in every
                other source state).
            timeout_seconds: Give up and raise after this long (default
                600s). Discovery/enrichment duration scales with the
                source's table count, same as the first scan.

        Returns:
            The terminal ``GetSource`` response. ``status`` will be one of:

            - ``PENDING_REVIEW``: a first-scan-equivalent rescan, e.g.
              recovering from a failed first scan;
            - ``RESCAN_REVIEW``: a re-scan (of an ``APPROVED`` source, or
              retrying a failed re-scan) that found changes to review;
            - ``APPROVED``: such a re-scan that found no changes (COA returns
              it straight to ``APPROVED``), or the re-verify of an RDF_SPARQL
              source. This is detected by its ``lastScanJobId`` differing
              from the one before the rescan;
            - ``COMPLETED``: the re-ingest of a DOCUMENTS source.

        Raises:
            RuntimeError: the source reached ``SCAN_FAILED`` again (the
                underlying cause — check ``get_scan_job_status`` — is still
                not fixed), or the poll timed out.
        """
        # A rescan that finds no changes returns the source straight to
        # APPROVED, so APPROVED only counts as finished once the scan id has
        # moved past the one recorded here.
        before = client.get_source(namespace_id, source_id)
        scan_before = _last_scan_job_id(before)
        status_before = before.get("status")
        client.rescan_source(
            namespace_id,
            source_id,
            confirm_discard_open_review=confirm_discard_open_review or None,
            client_token=str(uuid.uuid4()),
        )
        # GetSource is eventually consistent, so the first polls can still
        # show the pre-rescan state (an open RESCAN_REVIEW being discarded, a
        # COMPLETED documents source, a SCAN_FAILED being retried). A read in
        # that same state counts only once the poll has seen another state,
        # the scan id has moved, or the stale-read grace has passed.
        echo_counts = ignore_stale_failure(lambda s: s.get("status") == status_before, started_in_failure=True)

        def is_fresh(s: dict[str, Any]) -> bool:
            # Call echo_counts on every read so it sees the state change.
            echo_ok = echo_counts(s)
            return echo_ok or s.get("status") != status_before or _last_scan_job_id(s) not in (None, scan_before)

        def is_terminal(s: dict[str, Any]) -> bool:
            if not is_fresh(s):
                return False
            status = s.get("status")
            # A DOCUMENTS source re-ingests and ends COMPLETED; it has no review.
            if status in _REVIEW_READY_STATES or status == _DOCUMENTS_COMPLETED_STATE:
                return True
            # A DATABASE re-scan with no changes, and an RDF_SPARQL re-verify
            # (REGISTERED -> APPROVED), both end APPROVED with a new scan id.
            return status == _APPROVED_STATE and _last_scan_job_id(s) not in (None, scan_before)

        return poll_to_terminal(
            f"source {source_id} rescan",
            lambda: client.get_source(namespace_id, source_id),
            operation="source_rescan",
            is_terminal=is_terminal,
            is_failure=lambda s: s.get("status") == _SCAN_FAILED_STATE and is_fresh(s),
            timeout_s=float(timeout_seconds),
        )

    @tool
    def update_source_metadata(
        source_id: str,
        name: str | None = None,
        metadata_enrichment_enabled: bool | None = None,
        database_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change an existing DATABASE source's name, enrichment flag or connection config (``UpdateSourceMetadata``).

        COA accepts this in any source state and keeps no history: read the
        current values with ``get_source_status`` first if the caller may want
        them back (``name`` at the top level; the config,
        ``metadataEnrichmentEnabled``, ``rescanSchedule`` and ``eventRescan``
        under ``databaseDetails``).
        Pass only what should change:

        - ``name``: the display name (1-128 characters after trimming).
        - ``metadata_enrichment_enabled``: turn AI metadata enrichment on/off
          for later scans.
        - ``database_source``: ONE full configuration object for the source's
          own sub-type (``glueConfiguration``, ``jdbcConfiguration`` or
          ``customConnectorConfiguration``; same shapes as ``create_source``).
          It replaces the whole stored config, so send every field (e.g.
          table filters), not just the changed one. COA refuses to change the
          fields that tie the source to its database — JDBC
          ``host``/``port``/``engine``/``credentialSecretArn``, Glue
          ``catalogId``/``databaseName``, connector
          ``connectorFunctionArn``/``databaseName`` — and a Databricks or
          ``RDF_SPARQL`` source's config cannot be changed (its name can);
          register a new source for those.

        The name changes at once; config and enrichment changes apply on the
        next scan (``rescan_source``).

        Args:
            source_id: The source to update.
            name: New display name, or omit.
            metadata_enrichment_enabled: New enrichment setting, or omit.
            database_source: A full config object as described above, or omit.

        Returns:
            ``{sourceId, status, updatedAt}``.
        """
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = _display_name(name)
        if metadata_enrichment_enabled is not None:
            fields["metadata_enrichment_enabled"] = metadata_enrichment_enabled
        if database_source is not None:
            if isinstance(database_source, dict):
                for unsupported, label in _NON_UPDATABLE_CONFIGS.items():
                    if unsupported in database_source:
                        raise ValueError(f"COA cannot update {label} source's configuration; register a new source")
            key, config = _database_config(database_source, _UPDATABLE_CONFIGS)
            fields[key] = config
        if not fields:
            raise ValueError("nothing to update: pass name, metadata_enrichment_enabled and/or database_source")
        request = _build(UpdateSourceMetadataRequestContent, "update_source_metadata", **fields)
        return client.update_source_metadata(namespace_id, source_id, request)

    @tool
    def list_source_scan_jobs(source_id: str) -> dict[str, Any]:
        """List a source's scan and review history, newest first (``ListSourceScanJobs``, read-only).

        Each entry is a scan (``eventType: SCAN``: ``status``, ``triggerType``
        INITIAL/MANUAL/SCHEDULED/EVENT, ``tablesDiscovered``, ``errorMessage``,
        ``isRescan``) or a steward decision (``eventType: REVIEW``:
        ``decision`` APPROVED/REJECTED). Use it to see why or when a source
        was last scanned; ``get_scan_job_status`` reads one scan in detail.

        Args:
            source_id: The source whose history to list.

        Returns:
            ``{items: [...], count}``, every page followed.
        """
        items = collect_pages(
            lambda tok: client.list_source_scan_jobs(
                namespace_id, source_id, next_token=tok, max_results=LIST_SOURCE_SCAN_JOBS_MAX_RESULTS
            ),
            "items",
        )
        return {"items": items, "count": len(items)}

    @tool
    def keep_rescan_removal(source_id: str, table_id: str, column_name: str | None = None) -> dict[str, Any]:
        """Veto a re-scan's removal of a table or column (``KeepRescanRemoval``).

        A re-scan that no longer finds a table or column marks it for deletion
        (``pendingDeletion``), applied when the re-scan is approved. "Not found
        on this scan" is not always "deleted" — a partial scan or a narrowed
        filter can drop something that should stay — so this keeps it. Only
        valid while the source is ``RESCAN_REVIEW`` (COA answers 409
        otherwise); keeping something not marked is a no-op. It cannot be
        undone except by re-scanning again.

        Args:
            source_id: The source under re-scan review.
            table_id: The flagged table's id, ``database.table``.
            column_name: Keep only this removed column; omit to keep the
                whole removed table.

        Returns:
            ``{tableId, columnName?, pendingDeletion: false}``.
        """
        if column_name is not None:
            column_name = column_name.strip()
            if not column_name:
                # A blank name would send {}, which keeps the WHOLE table — irreversibly.
                raise ValueError("column_name must not be blank; omit it to keep the whole table")
        return client.keep_rescan_removal(namespace_id, source_id, table_id, column_name=column_name)

    @tool
    def set_rescan_schedule(
        source_id: str,
        enabled: bool,
        schedule_expression: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Turn a source's scheduled re-scan on or off (``PutSourceRescanSchedule``).

        A schedule re-runs discovery and AI enrichment on a cadence (it costs
        each time). A run that finds drift moves an ``APPROVED`` source to
        ``RESCAN_REVIEW``; a scheduled run never discards an open review.

        Args:
            source_id: A DATABASE source.
            enabled: ``True`` to create or replace the schedule, ``False`` to
                delete it.
            schedule_expression: Required when enabling. ``rate(N hours)``/
                ``rate(N days)`` (at least 1 hour apart) or ``cron(...)`` with
                a single literal minute, e.g. ``cron(0 3 * * ? *)``.
            timezone: IANA time zone for a cron schedule (default UTC).

        Returns:
            ``{sourceId, rescanSchedule: {enabled, scheduleExpression?, timezone?}}``.
        """
        expression = schedule_expression.strip() if schedule_expression else None
        if enabled is True and not expression:
            raise ValueError("schedule_expression is required to enable a schedule")
        request = _build(
            PutSourceRescanScheduleRequestContent,
            "rescan schedule",
            enabled=enabled,
            schedule_expression=expression if enabled else None,
            timezone=(timezone.strip() or None) if enabled and timezone else None,
        )
        return client.put_source_rescan_schedule(namespace_id, source_id, request)

    @tool
    def set_event_rescan(source_id: str, enabled: bool) -> dict[str, Any]:
        """Turn event-driven re-scans on or off for a Glue source (``PutSourceEventRescan``).

        When on, Glue table/database changes (create, update, delete) start a
        re-scan automatically. Only for a Glue source in this deployment's own
        account and region with no cross-account role; COA answers 400
        otherwise.

        Args:
            source_id: A Glue DATABASE source.
            enabled: ``True`` to turn on, ``False`` to turn off.

        Returns:
            ``{sourceId, eventRescan: {enabled}}``.
        """
        return client.put_source_event_rescan(namespace_id, source_id, enabled=enabled)

    @tool
    def delete_source(source_id: str, timeout_seconds: int = _DEFAULT_DELETE_TIMEOUT_S) -> dict[str, Any]:
        """Delete a data source and its catalog data (``DeleteSource``), and WAIT until it is gone.

        PERMANENT: COA removes the source's discovered tables/columns from its
        catalog (DataZone assets, catalog/federation registrations) and then
        the source itself. It does not delete ontologies or proposals induced
        from the source — use the OntologyAgent's ``delete_ontology`` for
        those. Confirm with the caller before calling this.

        Calls ``DeleteSource`` then polls ``GetSource`` until COA no longer
        knows the source (404). A source that is already gone returns
        ``{deleted: True, alreadyGone: True}``; one already ``DELETING``
        rejoins the poll without re-sending; ``DELETE_FAILED`` retries the
        delete. COA refuses (409) while a scan or enrichment is running
        (``REGISTERED``, ``SCANNING``…, ``ENRICHING``), so this raises in those
        states before calling COA — wait for the scan to finish first. The
        exception is a ``REGISTERED`` RDF_SPARQL source on a deployment with
        graph sources turned off: it never moves on, and COA lets it be
        deleted, so the tool sends the delete and lets COA decide.

        Args:
            source_id: The source to delete.
            timeout_seconds: Give up and raise after this long (default 900s).
                Teardown time grows with the number of tables.

        Returns:
            ``{sourceId, deleted: True, alreadyGone}``.

        Raises:
            ValueError: ``source_id`` is blank or has surrounding spaces.
            RuntimeError: the source is mid-scan, reached ``DELETE_FAILED``
                (retryable — call this tool again), or the poll timed out.
        """
        # A padded or blank id would 404 on GetSource and be reported as
        # "already gone" while the real source still exists.
        if not isinstance(source_id, str) or not source_id.strip() or source_id != source_id.strip():
            raise ValueError(f"source_id must be a non-blank id without surrounding spaces, got {source_id!r}")
        try:
            current = client.get_source(namespace_id, source_id)
        except CoaApiError as exc:
            if exc.status_code == 404:
                return {"sourceId": source_id, "deleted": True, "alreadyGone": True}
            raise
        current_status = current.get("status")
        scan_active = RuntimeError(
            f"source {source_id}: cannot delete while a scan is active (status {current_status!r}); "
            "wait until it leaves the scan, then retry"
        )
        # A REGISTERED graph source is stranded, and deletable, when the
        # deployment has graph sources turned off (no verify will ever move it
        # on). Only COA knows that setting, so let it decide.
        maybe_stranded = (
            current_status == SourceStatus.REGISTERED.value and current.get("sourceSubType") in GRAPH_BACKED_SUB_TYPES
        )
        if current_status in _DELETE_BLOCKED_STATES and not maybe_stranded:
            raise scan_active
        if current_status != _DELETING_STATE:
            try:
                client.delete_source(namespace_id, source_id)
            except CoaApiError as exc:
                if exc.status_code == 409 and maybe_stranded:
                    raise scan_active from exc
                raise

        def fetch() -> dict[str, Any]:
            try:
                return client.get_source(namespace_id, source_id)
            except CoaApiError as exc:
                if exc.status_code == 404:
                    return {"sourceId": source_id, "status": _DELETED_SENTINEL}
                raise

        poll_to_terminal(
            f"source {source_id} delete",
            fetch,
            operation="source_delete",
            is_terminal=lambda s: s.get("status") == _DELETED_SENTINEL,
            is_failure=ignore_stale_failure(
                lambda s: s.get("status") == _DELETE_FAILED_STATE,
                started_in_failure=current_status == _DELETE_FAILED_STATE,
            ),
            timeout_s=float(timeout_seconds),
        )
        return {"sourceId": source_id, "deleted": True, "alreadyGone": False}

    return [
        create_source,
        create_document_source,
        get_source_status,
        list_sources,
        get_scan_job_status,
        approve_source,
        reject_source,
        rescan_source,
        update_source_metadata,
        list_source_scan_jobs,
        keep_rescan_removal,
        set_rescan_schedule,
        set_event_rescan,
        delete_source,
    ]


# Tools that change COA state: they need the caller's confirmation (see intervention.py).
MUTATING_TOOL_NAMES = frozenset(
    {
        "create_source",
        "create_document_source",
        "approve_source",
        "reject_source",
        "rescan_source",
        "update_source_metadata",
        "keep_rescan_removal",
        "set_rescan_schedule",
        "set_event_rescan",
        "delete_source",
    }
)
