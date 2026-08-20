# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared constants for the Context Ontology Accelerator.

These values form the contract between infrastructure (CDK/Step Functions),
backend services (preprocessing Lambda, KG Build ECS), the API layer, and
the web application.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum

# ---------------------------------------------------------------------------
# Brand tokens — parameterized via environment variables so different
# deployments (CI/dev = "scl", greenfield = "coa", customer = custom) get
# consistent naming. Defaults match the public OSS brand.
# ---------------------------------------------------------------------------

RESOURCE_PREFIX: str = os.environ.get("RESOURCE_PREFIX", "coa").rstrip("-")
"""Deployment prefix for resource naming (S3, IAM, DynamoDB, SSM paths).

CDK injects this as ``{prefix}-{env}-`` (e.g. ``scl-dev-``) because its consumers
build resource names from it. Do NOT derive data-plane identifiers from it — use
``BRAND`` below.
"""

BRAND: str = "coa"
"""Static brand token for DATA-PLANE identifiers. NOT the resource prefix.

Graph IRIs, URN schemes, EventBridge sources and DataZone type names are data
internal to the system: they are written into RDF stored in Neptune, into event
contracts between services, and into DataZone metadata. They must stay stable
across deployments, so they are fixed here rather than derived from
``resource_prefix``.

The tokens below previously derived from ``RESOURCE_PREFIX``, which CDK sets to
``{prefix}-{env}-``. Under ``resource_prefix=scl`` that yielded
``http://scl-dev.amazon.com/vocab/scl-dev#`` for writers while readers used their
own compiled-in default — the two silently stopped matching. Mirrors the BRAND
block in libs/ts-shared/src/constants.ts.
"""

GRAPH_BASE_URI: str = os.environ.get("GRAPH_BASE_URI", f"http://{BRAND}.amazon.com")
"""Base authority for knowledge-graph IRIs (ontology classes, properties, instances)."""

URN_PREFIX: str = os.environ.get("URN_PREFIX", BRAND)
"""URN scheme prefix: urn:{URN_PREFIX}:{namespace}:metric:{name}."""

VOCAB_PREFIX: str = os.environ.get("VOCAB_PREFIX", BRAND)
"""Short SPARQL/Turtle prefix alias bound to VOCAB_URI (e.g. coa:isMapped)."""

VOCAB_URI: str = os.environ.get("VOCAB_URI", f"{GRAPH_BASE_URI}/vocab/{VOCAB_PREFIX}#")
"""Full RDF vocabulary namespace URI for platform-defined predicates."""

DZ_TYPE_PREFIX: str = os.environ.get("DZ_TYPE_PREFIX", BRAND[:1].upper() + BRAND[1:])
"""PascalCase DataZone type prefix: {DZ_TYPE_PREFIX}TableMetadata."""

EVENT_SOURCE_PREFIX: str = os.environ.get("EVENT_SOURCE_PREFIX", BRAND)
"""EventBridge source prefix: {EVENT_SOURCE_PREFIX}.metric-service, etc."""

# ---------------------------------------------------------------------------
# Ingestion pipeline status values
# ---------------------------------------------------------------------------
# Stored in the ``doc_sources`` DynamoDB table ``status`` attribute.
# Step Functions states reference these strings directly, so they MUST
# stay in sync with ``makeStatusUpdate()`` in unstructured-stack.ts.


class IngestionStatus(StrEnum):
    """Status of a doc-source ingestion pipeline run."""

    PENDING = "pending"
    INGESTING = "ingesting"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"


# Fields written by the ingestion pipeline that should be cleared when re-triggering.
# Keep in sync with the Step Functions state machine writes in unstructured-stack.ts.
PIPELINE_RUN_FIELDS: tuple[str, ...] = (
    "errorMessage",
    "filesTotal",
    "filesSkipped",
    "filesErrored",
    "preprocessingIssues",
)

# Statuses where a new ingestion request should be rejected (409).
ACTIVE_INGESTION_STATUSES: frozenset[str] = frozenset(
    {
        IngestionStatus.PENDING,
        IngestionStatus.INGESTING,
        IngestionStatus.DELETING,
    }
)

# ---------------------------------------------------------------------------
# Unified source pipeline status values
# ---------------------------------------------------------------------------
# These mirror the Smithy SourceStatus enum in unified-sources.smithy.
# Kept here so pipeline Lambdas (discovery, enrichment, preprocessing) can
# reference them without importing the generated server models.

# Statuses where delete/rescan should be rejected (409) for unified sources.
SOURCE_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        "REGISTERED",
        "SCANNING",
        "SCANNING_ENTITY_EXTRACTION",
        "SCANNING_KG_BUILD",
        "ENRICHING",
        "DELETING",
    }
)

# Fields written by the unified pipeline that should be cleared on rescan.
SOURCE_PIPELINE_RUN_FIELDS: tuple[str, ...] = (
    "errorMessage",
    "filesTotal",
    "filesSkipped",
    "filesErrored",
    "preprocessingIssues",
)


class ExtractionMode(StrEnum):
    """KG Build extraction mode."""

    CONTINUOUS = "continuous"
    SEPARATED = "separated"


# ---------------------------------------------------------------------------
# Supported file extensions for unstructured document processing
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".docx", ".pdf"})

# MIME types accepted for direct file upload via pre-signed S3 URLs.
# Must stay in sync with SUPPORTED_EXTENSIONS above.
SUPPORTED_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "text/plain",  # .txt
        "text/markdown",  # .md
    }
)

# Extensions produced by preprocessing and consumed by KG Build.
# Preprocessing converts all SUPPORTED_EXTENSIONS into these text formats.
STAGED_TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md"})

# ---------------------------------------------------------------------------
# File size limit (per file) for the preprocessing Lambda
# ---------------------------------------------------------------------------

DEFAULT_MAX_FILE_SIZE_MB: int = 200

# ---------------------------------------------------------------------------
# GraphRAG extraction batch size
# ---------------------------------------------------------------------------
# Controls how many documents are chunked together per micro-batch.
# Batch inference requires ≥100 chunks per Bedrock batch job; setting this
# high enough ensures a single micro-batch produces enough chunks.

EXTRACTION_BATCH_SIZE: int = 1000

# ---------------------------------------------------------------------------
# KG Build configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_EXTRACTION_MODE: str = ExtractionMode.CONTINUOUS
DEFAULT_USE_BATCH_INFERENCE: str = "false"
DEFAULT_ENABLE_VERSIONING: str = "true"
DEFAULT_ENABLE_PROPOSITION_EXTRACTION: str = "true"
DEFAULT_DELETE_PREV_VERSIONS: str = "false"

# Complete extraction config defaults — single source of truth for all handlers.
EXTRACTION_DEFAULTS: dict[str, object] = {
    "extraction_mode": DEFAULT_EXTRACTION_MODE,
    "use_batch_inference": DEFAULT_USE_BATCH_INFERENCE.lower() == "true",
    "enable_versioning": DEFAULT_ENABLE_VERSIONING.lower() == "true",
    "enable_proposition_extraction": DEFAULT_ENABLE_PROPOSITION_EXTRACTION.lower() == "true",
    "delete_prev_versions": DEFAULT_DELETE_PREV_VERSIONS.lower() == "true",
}


# ---------------------------------------------------------------------------
# Embedding model — SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------
# Every embedding producer (ontology induction, doc-kg-build ingestion, metric
# onboarding) and consumer (serve Tier-1/2/3 retrieval) MUST use the SAME model,
# or vectors are cross-model incomparable and retrieval degrades silently to
# noise (cosine of orthogonal vectors ≈ 0). Cohere Embed v4 is the canonical
# choice, aligning serve with the graphrag-toolkit default lineage.
#
# NOTE: cohere.embed-v4:0 does NOT support on-demand throughput on Bedrock — it
# must be invoked via an inference profile (the ``us.`` prefix). Both the direct
# invoke path (BedrockEmbedder) and the LlamaIndex/graphrag path accept this
# 3-part "region.provider.model" id.
#
# Changing the model is also a DATA MIGRATION: all existing indexes were written
# with the prior model and must be re-embedded/re-ingested, else retrieval breaks.
DEFAULT_EMBED_MODEL_ID: str = "us.cohere.embed-v4:0"
DEFAULT_EMBED_DIMENSIONS: int = 1024


# ---------------------------------------------------------------------------
# Input validation — reusable across Lambda handlers and API layer
# ---------------------------------------------------------------------------

# Namespace and doc-source IDs: alphanumeric, hyphens, underscores only
SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")

# UUID v4 — used for namespace IDs
NAMESPACE_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# Namespace name: alphanumeric start, up to 128 chars total (alphanumeric, underscore, hyphen).
# Used for SPARQL graph URIs, OpenSearch index names, and other label-based lookups.
NAMESPACE_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# S3 prefixes: relative paths only — no leading slash, no ".." traversal
SAFE_S3_PREFIX_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_./-]+$")


def validate_id(value: str, name: str) -> None:
    """Raise ``ValueError`` if *value* contains unsafe characters.

    Valid IDs are non-empty strings containing only alphanumeric characters,
    hyphens, or underscores.
    """
    if not value or not SAFE_ID_RE.match(value):
        raise ValueError(
            f"Invalid {name}: {value!r}. "
            "Must be non-empty and contain only alphanumeric characters, "
            "hyphens, or underscores."
        )


def validate_namespace_id(value: str, name: str = "namespaceId") -> None:
    """Raise ``ValueError`` if *value* is not a valid UUID v4 namespace ID."""
    if not value or not NAMESPACE_ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}. Must be a UUID v4 (e.g. '550e8400-e29b-41d4-a716-446655440000').")


def validate_namespace_name(value: str) -> None:
    """Raise ``ValueError`` if *value* is not a valid namespace name.

    Valid namespace names start with an alphanumeric character and contain
    only alphanumeric characters, underscores, or hyphens (max 128 chars).
    Used for safe interpolation in SPARQL graph URIs and OpenSearch index names.
    """
    if not NAMESPACE_NAME_RE.match(value):
        raise ValueError(f"Invalid namespace {value!r}: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{{0,127}}$")


def validate_s3_prefix(value: str, name: str) -> None:
    """Raise ``ValueError`` if *value* is not a safe relative S3 prefix.

    Empty string is allowed (callers should default it). Non-empty values
    must be relative paths with no leading slash, no ``..`` components,
    and no ``s3://`` URI scheme.
    """
    if not value:
        return
    if value.startswith("/") or ".." in value or value.startswith("s3://") or not SAFE_S3_PREFIX_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}. Must be a relative path like 'healthcare/' or 'documents/2024/'.")


# ---------------------------------------------------------------------------
# GraphRAG Toolkit tenant ID conversion
# ---------------------------------------------------------------------------
# TenantId rules: 1–25 lowercase letters, numbers, and periods only.
# Periods cannot be at the start or end.


def to_graphrag_tenant_id(namespace_id: str, doc_source_id: str = "") -> str:
    """Derive a stable GraphRAG TenantId from the namespace ID.

    Strips hyphens from the UUID v4 namespace_id and truncates to 25 chars,
    satisfying the TenantId constraint (1–25 lowercase alphanumeric + periods).
    All doc sources within a namespace share the same tenant, giving 2 OSS
    indexes per namespace (chunk_{tenant_id}, statement_{tenant_id}) rather
    than 2 per doc source.

    The ``doc_source_id`` parameter is accepted but ignored — kept for
    backwards-compatible call sites during migration.

    Example:
        namespace_id = "550e8400-e29b-41d4-a716-446655440000"
        tenant_id    = "550e8400e29b41d4a71644665"
    """
    return namespace_id.replace("-", "")[:25]


def graphrag_chunk_index_name(namespace_id: str) -> str:
    """Return the OpenSearch index name for document chunks (GraphRAG).

    Pattern: ``chunk_{tenant_id}`` where tenant_id is the truncated namespace.
    """
    return f"chunk_{to_graphrag_tenant_id(namespace_id)}"


# GraphRAG vector index prefixes that have EVER been created for a namespace.
#
# ``chunk`` + ``topic`` are the current set (``EMBEDDING_INDEXES`` in
# sources/documents/kg_build/graph_build.py). ``statement`` is legacy: the build
# path stopped embedding statements, but namespaces ingested before that change
# still have a ``statement_*`` index sitting in the collection.
#
# Teardown must cover the legacy name too. AOSS caps a collection at 1000
# indexes, so an index nobody writes any more still consumes the quota that
# eventually fails every embedding write with ``index_limit_breached``.
# Deleting an absent index is a no-op, so listing a name that was never created
# costs nothing.
GRAPHRAG_INDEX_PREFIXES = ("chunk", "topic", "statement")


def graphrag_index_names(namespace_id: str) -> list[str]:
    """Return every GraphRAG vector index name belonging to a namespace.

    These indexes are NAMESPACE-scoped, not source-scoped: all doc sources in a
    namespace share one tenant id (see :func:`to_graphrag_tenant_id`), so they
    become garbage only when the namespace itself goes away — which is why
    namespace teardown owns deleting them and source deletion does not.
    """
    tenant = to_graphrag_tenant_id(namespace_id)
    return [f"{prefix}_{tenant}" for prefix in GRAPHRAG_INDEX_PREFIXES]


def canonical_col(name: str) -> str:
    """Convert a column name to a valid SQL/H2 identifier for R2RML and schema.sql.

    Strips characters that are illegal in unquoted SQL identifiers (parens,
    percent signs, slashes, hyphens, etc.), lowercases, and prefixes
    digit-leading names with underscore.

    Must be used consistently across R2RML ``rr:column`` values, schema.sql
    ``CREATE TABLE`` DDL, and subject-map templates — Ontop requires all three
    to reference the same canonical identifier.

    .. deprecated::
        Prefer :func:`sql_ident` for SQL identifiers. Canonicalizing mangled
        the real source column name (``"aCL IgG"`` → ``acl_igg``) so the SQL
        Ontop emitted no longer matched the underlying datasource. SQL-delimited
        (double-quoted) identifiers preserve the original name and are accepted
        by H2/Ontop verbatim, removing the need for a reverse lookup map.
    """
    result = name.replace(" ", "_").lower()
    result = re.sub(r"[^a-z0-9_]", "", result)
    if result and result[0].isdigit():
        result = f"_{result}"
    return result or "col"


def sql_ident(name: str) -> str:
    """Return ``name`` as a SQL-delimited (double-quoted) identifier.

    Preserves the original column/table name verbatim — spaces, parens,
    percent signs, hyphens, mixed case, and SQL reserved words are all legal
    inside a double-quoted identifier. Embedded double quotes are escaped by
    doubling them, per the SQL standard.

    Used for R2RML ``rr:tableName`` / ``rr:column`` values and schema.sql DDL
    so that the SQL Ontop generates references the exact identifier present in
    the source datasource. To embed the result in an R2RML ``rr:template``
    placeholder, wrap it in braces: ``f"...{{{sql_ident(col)}}}"``.
    """
    escaped = (name or "").replace('"', '""')
    return f'"{escaped}"'


def ontology_vector_index_name(prefix: str, namespace_id: str, default_namespace: str = "default") -> str:
    """Return the OpenSearch index name for ontology embeddings.

    Pattern: ``{prefix}-{namespace_id}`` — no char limit.
    When ``namespace_id`` equals ``default_namespace``, returns the bare prefix
    (backwards-compatible with single-tenant deployments).

    The prefix is typically the ``OSS_INDEX`` / ``OSS_ONTOLOGY_INDEX`` env var
    value (e.g. ``coa-dev-vectors``).
    """
    if namespace_id == default_namespace:
        return prefix
    return f"{prefix}-{namespace_id}"


# ---------------------------------------------------------------------------
# Namespace constants — keep in sync with models/src/main/smithy/namespace.smithy
# ---------------------------------------------------------------------------


class NamespaceStatus(StrEnum):
    """Lifecycle status of a namespace (Smithy NamespaceStatus enum)."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    DELETE_FAILED = "DELETE_FAILED"


# ---------------------------------------------------------------------------
# Ontology artifact S3 paths — shared between Model (publish) and Serve (read)
# ---------------------------------------------------------------------------
# Base: s3://{ONTOLOGY_BUCKET}/ontologies/{namespace}/{version}/
# See docs/contracts/few-shot-examples-contract.md for full specification.

ONTOLOGY_ARTIFACTS_PREFIX = "ontologies"
ONTOLOGY_ARTIFACT_EXAMPLES = "examples.json"
ONTOLOGY_ARTIFACT_MANIFEST = "manifest.json"


def ontology_artifact_s3_key(namespace: str, version: str, filename: str) -> str:
    """Build the S3 key for an ontology artifact.

    Example: ontology_artifact_s3_key("pc-insurance", "latest", "examples.json")
             → "ontologies/pc-insurance/latest/examples.json"

    Raises ValueError if any parameter contains path traversal characters.
    """
    validate_namespace_name(namespace)
    validate_id(version, "version")
    validate_id(filename.replace(".", ""), "filename")
    if ".." in filename or "/" in filename:
        raise ValueError(f"Invalid filename: {filename!r}. Must not contain path traversal characters.")
    return f"{ONTOLOGY_ARTIFACTS_PREFIX}/{namespace}/{version}/{filename}"


# ---------------------------------------------------------------------------
# Pagination defaults — shared across all list handlers
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE: int = 25
MAX_PAGE_SIZE: int = 100


# ---------------------------------------------------------------------------
# Direct-through discovery: env-var names and Smithy resource paths
# ---------------------------------------------------------------------------
# ListMetrics and DescribeSchema are pure catalog reads with no tier
# orchestration, so both surfaces that expose them (data-layer's ``handler.py``
# and MCP's ``coa_mcp.tools.discovery``) invoke the backend Lambdas DIRECTLY
# and skip the Context Manager. The env var names and resource paths below are
# the contract between those callers and the CDK stacks that wire the ARNs in.
#
# If any of these strings diverge between call sites, a caller falls back to
# an empty ARN (silent 501) or hits a URL the ontology-api-proxy's alias table
# does not know. Keep this the single source of truth.

# Env var names that the data-layer and MCP-server Lambdas read at cold start
# to locate the ontology-api-proxy and metric-service Lambdas. Must match what
# ``data-layer-stack.ts`` and ``mcp-stack.ts`` set in the target Lambda's env.
ONTOLOGY_PROXY_LAMBDA_ARN_ENV: str = "ONTOLOGY_PROXY_LAMBDA_ARN"
METRIC_SERVICE_LAMBDA_ARN_ENV: str = "METRIC_SERVICE_LAMBDA_ARN"

# Smithy operation resource paths. Both surfaces must send the identical URI
# to the backend Lambda so it can route it (via ``api_proxy_handler``'s per-path
# alias table) to the underlying FastAPI route. These come from
# ``models/src/main/smithy/serve.smithy`` — the canonical wire contract.
LIST_METRICS_RESOURCE: str = "/namespaces/{namespaceId}/metrics"
DESCRIBE_SCHEMA_RESOURCE: str = "/namespaces/{namespaceId}/schema"


# ---------------------------------------------------------------------------
# SQL Dialect — keep in sync with models/src/main/smithy/metric-service.smithy
# ---------------------------------------------------------------------------


class SqlDialect(StrEnum):
    """Supported SQL dialects for metric expressions (Smithy SqlDialect enum)."""

    POSTGRESQL = "POSTGRESQL"
    TRINO = "TRINO"
    REDSHIFT = "REDSHIFT"
    SNOWFLAKE = "SNOWFLAKE"
    DATABRICKS = "DATABRICKS"
    MYSQL = "MYSQL"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Governed Metrics — ontology integration constants
# ---------------------------------------------------------------------------

# The ontology_id under which all governed metrics are stored in the
# per-namespace named graph (same graph scheme as ontology-engine classes).
GOVERNED_METRICS_ONTOLOGY_ID: str = f"urn:{URN_PREFIX}:vocab#GovernedMetrics"
