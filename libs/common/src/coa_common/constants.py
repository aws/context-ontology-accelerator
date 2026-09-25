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
    "preprocessingIssuesS3Key",
    "preprocessingIssuesTruncated",
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
    "preprocessingIssuesS3Key",
    "preprocessingIssuesTruncated",
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
# Infer the entity-class vocabulary from the corpus itself instead of inheriting
# graphrag-toolkit's hardcoded DEFAULT_ENTITY_CLASSIFICATIONS ('Company',
# 'Sports Team', 'Creative Work', …), which is a news/finance list that steers
# extraction to the wrong domain on anything else.
DEFAULT_INFER_ENTITY_CLASSIFICATIONS: str = "true"
# Explicit vocabulary is empty by default → falls back to infer (or, in a future
# change, resolves from the namespace's accepted ontology). Represented as an
# empty JSON array in env vars so the state-machine → ECS pipe can carry it
# without needing a new SFN field type.
DEFAULT_PREFERRED_ENTITY_CLASSIFICATIONS_JSON: str = "[]"
# Explicit TOPIC vocabulary, empty by default. Topics are the thematic groupings
# the extractor assigns chunks to (``__Topic__`` nodes, later induced as
# ``skos:Concept``); entity classifications are what a thing *is*
# (``__Entity__.class``, induced as ``owl:Class``). Two different axes.
#
# Note there is deliberately NO ``infer_topics`` flag: graphrag-toolkit has no
# topic analogue of ``InferClassificationsConfig``, so "let the model decide" is
# simply an empty list — the extractor then names topics freely from the chunk
# text. Adding a flag would imply a corpus-inference pass that does not exist.
DEFAULT_PREFERRED_TOPICS_JSON: str = "[]"

# Hard API cap on either vocabulary list. Not a modelling limit — a guard against
# a list so long it crowds the extraction prompt (every entry is injected into
# each chunk's prompt, so cost and adherence both degrade with length). 15-25
# entries is the practical sweet spot; 100 is the refusal threshold, well past
# any vocabulary that still steers the model usefully.
#
# NOTE: this check is load-bearing. The Smithy `@length` on EntityClassificationList
# / TopicList does NOT survive code generation (the generated Pydantic type is a
# plain `Optional[List[...]]` with no max_items), so the handler is the only place
# the list length is actually enforced. Per-ENTRY length does generate, as
# `max_length` on the member.
MAX_VOCABULARY_ENTRIES: int = 100
# Table extraction OFF by default. When ON, PDFs route through Textract's
# AnalyzeDocument(TABLES) instead of unstructured strategy="fast" — preserves
# row/column structure at materially higher per-page cost. Opt in per source.
DEFAULT_ENABLE_TABLE_EXTRACTION: str = "false"
# Chunk size / overlap — 0 means "use the toolkit default" (SentenceSplitter
# chunk_size=256, chunk_overlap=25). Setting a positive integer overrides. The
# graphrag benchmark harness pins 1024 for dense/tabular corpora.
DEFAULT_CHUNK_SIZE: int = 0
DEFAULT_CHUNK_OVERLAP: int = 0

# Complete extraction config defaults — single source of truth for all handlers.
EXTRACTION_DEFAULTS: dict[str, object] = {
    "extraction_mode": DEFAULT_EXTRACTION_MODE,
    "use_batch_inference": DEFAULT_USE_BATCH_INFERENCE.lower() == "true",
    "enable_versioning": DEFAULT_ENABLE_VERSIONING.lower() == "true",
    "enable_proposition_extraction": DEFAULT_ENABLE_PROPOSITION_EXTRACTION.lower() == "true",
    "delete_prev_versions": DEFAULT_DELETE_PREV_VERSIONS.lower() == "true",
    "infer_entity_classifications": DEFAULT_INFER_ENTITY_CLASSIFICATIONS.lower() == "true",
    "preferred_entity_classifications": [],
    "preferred_topics": [],
    "enable_table_extraction": DEFAULT_ENABLE_TABLE_EXTRACTION.lower() == "true",
    "chunk_size": DEFAULT_CHUNK_SIZE,
    "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
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

MAX_QUERY_CODEPOINTS: int = 4000
"""Maximum natural-language query length at every API and tokenizer boundary.

Code points, not bytes, so the allowance does not shrink for non-Latin scripts.

A caller cannot always reach this bound, and the reason is upstream of any code
here: the API's WAF WebACL runs ``AWSManagedRulesCommonRuleSet``, whose
``SizeRestrictions_BODY`` rule blocks any request body over 8,192 bytes. WAF sits
in front of the Lambda, so an oversized body is answered ``403 Forbidden`` with no
explanation and this validator never sees it. Measured against a deployed API: a
body of 8,188 bytes passes, 8,233 is blocked.

The bound the request body actually imposes therefore depends on encoding:

    ASCII (1 byte/char)      ~4000 code points — the full allowance
    CJK (3 bytes/char)       ~2725 code points
    astral (4 bytes/char)    ~2045 code points

Left as is deliberately. A natural-language question does not approach 2,725
characters, so raising the WAF limit would widen a security control for input
nobody sends. Recorded here instead so a generated client does not accept 4,000
characters and then surface an unexplained 403 — and so the decision can be
revisited with evidence if a real query is ever refused.
"""


# Tier-2 engine selection accepted on ``options.strategy``.
#
# Lives here because the data-layer Lambda validates it and depends only on
# ``coa-common`` — importing the generated ``coa_data_layer_server`` enum or
# context-manager's ``StrategyOption`` would add an undeclared runtime dependency
# to the Lambda bundle.
#
# This is the THIRD copy of the same set (Smithy ``QueryStrategy``, serve's
# ``StrategyOption``, here), so drift is guarded by a three-way parity test in
# ``packages/context-manager/tests/unit/test_strategy.py``. An unknown value must be
# rejected rather than forwarded: it would fall out of
# ``Orchestrator._EXPLICIT_STRATEGY_OPTIONS`` and silently run the default fallback
# chain while the caller believed it pinned an engine.
QUERY_STRATEGIES: frozenset[str] = frozenset(
    {
        "best",
        "ontop",
        "nl_to_sql",
        "ontop_first",
        "nl_to_sql_first",
        "deep-reasoning",
    }
)


def validate_query_text(value: object) -> str:
    """Return a stripped natural-language query or raise ``ValueError``.

    The raw code-point bound is checked before stripping so oversized input is
    rejected in constant time before any Unicode segmentation or downstream I/O.
    """
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    if len(value) > MAX_QUERY_CODEPOINTS:
        raise ValueError(f"query must be at most {MAX_QUERY_CODEPOINTS} characters")
    query = value.strip()
    if not query:
        raise ValueError("query must not be blank")
    return query


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


def sql_qualified_table(name: str, schema: str | None = None) -> str:
    """Return a (schema-)qualified SQL-delimited table identifier.

    ``sql_qualified_table("orders", "sales")`` -> ``'"sales"."orders"'`` and
    ``sql_qualified_table("orders")`` -> ``'"orders"'``.

    SUPERSEDED for the inducer path. It once UNCONDITIONALLY schema-qualified
    every ``rr:tableName`` / H2 table (cause A of the cross-catalog defect), but that forced every
    already-accepted single-source mapping to re-induce. The inducer now qualifies
    ONLY on a genuine collision via
    ``inducer/strategies/base.py:disambiguated_refs`` / ``logical_table_names`` (a
    unique name stays bare), so this helper has no remaining inducer caller. It is
    retained only as a generic identifier utility; do NOT reintroduce
    unconditional qualification from it. For any qualification that must AGREE with
    the mapping, go through ``logical_table_names`` instead.
    """
    if schema:
        return f"{sql_ident(schema)}.{sql_ident(name)}"
    return sql_ident(name)


def split_sql_ident_path(literal: str) -> list[str]:
    """Split a possibly-quoted, possibly-qualified SQL name into its bare segments.

    The inverse of :func:`sql_ident` applied to each dotted segment, so
    ``'"public"."customers"'`` yields ``["public", "customers"]`` and a
    round-trip through ``".".join(sql_ident(p) for p in ...)`` is identity.

    Splitting on ``.`` textually is NOT the inverse, and the difference is not
    cosmetic: a table legitimately named ``q1.results`` is emitted as the single
    identifier ``"q1.results"``, and a textual split turns it into the schema
    ``"q1`` and the table ``results"`` — unbalanced quotes that make the whole
    generated DDL a syntax error rather than one bad table. Only an unquoted dot
    separates segments here, and ``""`` inside a quoted segment is one literal
    quote character.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(literal):
        char = literal[i]
        if char == '"':
            if in_quotes and literal[i + 1 : i + 2] == '"':
                buf.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        i += 1
    parts.append("".join(buf))
    return parts


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


# ---------------------------------------------------------------------------
# Namespace resource-tag binding
# ---------------------------------------------------------------------------
#
# An AWS resource the platform reads on a namespace's behalf but does NOT own —
# today a JDBC source's credential secret — is bound to the namespaces entitled
# to it by a resource tag:
#
#     <prefix>:namespace = "<namespaceId> [<namespaceId> ...]"
#
# The KEY carries the deployment's resource prefix so two deployments co-located
# in one AWS account bind independently: a secret onboarded to `scl` is not
# readable by a `coa` deployment's roles, whose IAM conditions name their own
# key. (Same reasoning as `eventSourcePrefix` in infra/lib/context.ts — the
# resource being tagged is account-global and therefore shared.)
#
# The VALUE is a whitespace-separated list so one secret can serve several
# namespaces (a shared read-only reporting credential, say) without a per-
# namespace copy. It is validated strictly — every entry must be a namespace
# UUID, in canonical single-space form — because it is written by whoever owns
# the secret, and both the registration check and the IAM `StringLike`
# conditions derived from it depend on entries being whole, unambiguous tokens.
#
# Secrets Manager caps a tag value at 256 characters, so a single secret binds
# at most 6 namespaces (37 chars each). That ceiling is AWS-enforced on write;
# nothing here needs to police it.

NAMESPACE_TAG_SEPARATOR: str = " "
"""Canonical separator between namespace IDs in a ``<prefix>:namespace`` tag value."""


def namespace_tag_key(prefix: str | None = None) -> str:
    """Resource-tag key binding a resource to one or more namespaces.

    ``prefix`` defaults to the deployment's bare resource prefix from
    ``RESOURCE_TAG_PREFIX`` (CDK injects ``resolveContext().prefix``), falling
    back to :data:`BRAND` (``"coa"``).

    Deliberately NOT derived from ``RESOURCE_PREFIX``: that variable means
    different things in different runtimes — CDK injects ``{prefix}-{env}-``
    (``scl-dev-``) into compute, while the integ runner sets the bare prefix
    (``scl``) for SSM paths. A tag key must be one exact string shared by the
    registration check, the IAM conditions, and whoever tags the secret, so it
    gets its own unambiguous variable rather than a guess at which form arrived.
    """
    resolved = (prefix if prefix is not None else os.environ.get("RESOURCE_TAG_PREFIX", "")) or BRAND
    return f"{resolved.strip().rstrip('-')}:namespace"


def parse_namespace_tag(value: str) -> list[str]:
    """Parse a ``<prefix>:namespace`` tag value into the namespace IDs it binds.

    Accepts one or more namespace UUIDs separated by single spaces. Returns them
    in the order written; membership, not order, is what callers check.

    Raises ``ValueError`` when the value is empty, holds an entry that is not a
    namespace UUID, or is not in canonical form (leading/trailing whitespace,
    repeated or non-space separators). Canonical form is required, not merely
    preferred: the IAM conditions that enforce this same binding at the platform
    layer match entries by literal space boundary (see
    :func:`namespace_tag_condition_patterns`), so a value this function accepted
    but IAM could not match would pass registration and then fail every read.
    """
    ids = (value or "").split()
    if not ids:
        raise ValueError("Namespace tag value is empty. Expected one or more namespace UUIDs separated by a space.")
    for entry in ids:
        validate_namespace_id(entry, "namespace tag entry")
    canonical = NAMESPACE_TAG_SEPARATOR.join(ids)
    if value != canonical:
        raise ValueError(
            f"Namespace tag value is not in canonical form. Expected {canonical!r} "
            "(namespace UUIDs separated by exactly one space, no leading or trailing whitespace)."
        )
    return ids


def namespace_tag_condition_patterns(namespace_id: str) -> list[str]:
    """IAM ``StringLike`` patterns matching *namespace_id* as a whole tag entry.

    A tag value may list several namespaces, so ``StringEquals`` on the id alone
    would never match a shared secret. IAM has no word-boundary operator, so the
    four possible positions are enumerated: only entry, first, last, or middle.
    Anchoring each on a literal space keeps this an entry match rather than a
    substring one — ``<other><id>`` matches none of these patterns.
    """
    return [
        namespace_id,
        f"{namespace_id}{NAMESPACE_TAG_SEPARATOR}*",
        f"*{NAMESPACE_TAG_SEPARATOR}{namespace_id}",
        f"*{NAMESPACE_TAG_SEPARATOR}{namespace_id}{NAMESPACE_TAG_SEPARATOR}*",
    ]


# ---------------------------------------------------------------------------
# Cross-account datasource onboarding
# ---------------------------------------------------------------------------


def datasource_external_id(namespace_id: str) -> str:
    """ExternalId the platform presents when assuming a customer's datasource role.

    Derived from the namespace, never from the API request: the cross-account role
    ARN is caller-supplied, so this is what binds an assume to the namespace
    entitled to it. A caller with ``manageSource`` on one namespace therefore
    cannot point a source at a role onboarded for another (confused deputy).

    Single source of truth on purpose. The sources connector sends this value and
    the control plane shows it to the customer for their trust policy — if the two
    derivations drifted, every cross-account onboarding would fail ``AccessDenied``
    with nothing to point at.

    Reads ``RESOURCE_PREFIX`` per call (not the module-level constant above, which
    is stripped to the bare brand token) so the value matches the deployment's
    ``{prefix}-{env}-`` naming, the same form as ``athenaWorkgroupName``.
    """
    prefix = os.environ.get("RESOURCE_PREFIX", "coa-dev-")
    return f"{prefix}{namespace_id}"


# ---------------------------------------------------------------------------
# Document source bucket authorization
# ---------------------------------------------------------------------------


def bucket_namespace_tag_key() -> str:
    """Tag key a bucket owner sets to authorize namespaces to read that bucket.

    Only a principal holding ``s3:TagResource`` on the bucket can set this, so the
    tag is evidence that the bucket's owner authorized the read. That is the whole
    control: an S3 document source names a bucket, and creating a source in a
    namespace says nothing about whether the caller may read what it points at.

    Same key as :func:`namespace_tag_key`, which this delegates to — one tag
    contract, one implementation, one ``RESOURCE_TAG_PREFIX``. It exists as a named
    alias because the two uses read differently at the call site: that one binds a
    credential secret to the namespaces entitled to it, this one records that a
    bucket's owner authorized namespaces to read it.
    """
    return namespace_tag_key()


def bucket_grants_namespace(tags: dict[str, str], namespace_id: str) -> bool:
    """Whether *tags* authorize *namespace_id* to read the bucket.

    The value is a whitespace-separated list of namespace ids, so one bucket can
    serve several namespaces: ``coa:namespace = "<ns-a> <ns-b>"``. ``str.split()``
    absorbs repeated, leading and trailing whitespace, so a hand-edited tag with
    untidy spacing still resolves.

    Fails closed on anything unexpected — a missing tag, an empty value, or a
    namespace absent from the list all return ``False``. Matching is exact against
    whole entries, never a substring, so one namespace id cannot authorize another
    by sharing a prefix.
    """
    if not namespace_id:
        return False
    return namespace_id in tags.get(bucket_namespace_tag_key(), "").split()
