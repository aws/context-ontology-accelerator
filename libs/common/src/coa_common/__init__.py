# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Semantic Context Common - shared config, logging, exceptions, constants, S3 utilities, auth, and DAO."""

__version__ = "0.3.3"

from coa_common.authnz_types import (
    PrincipalType,
    ResourceRoleMapping,
    ResourceType,
)
from coa_common.aws_config import (
    RetryPolicy,
    async_boto_config,
    opensearch_client_kwargs,
    sync_boto_config,
)
from coa_common.aws_user_agent import USER_AGENT_EXTRA, install_user_agent
from coa_common.bedrock import (
    BedrockClient,
    BedrockInvocationResult,
    BedrockTruncationError,
    GuardrailBlockedError,
    InputTooLargeError,
)
from coa_common.config import SCLConfig, resolve_region
from coa_common.constants import (
    ACTIVE_INGESTION_STATUSES,
    DEFAULT_DELETE_PREV_VERSIONS,
    DEFAULT_EMBED_DIMENSIONS,
    DEFAULT_EMBED_MODEL_ID,
    DEFAULT_ENABLE_PROPOSITION_EXTRACTION,
    DEFAULT_ENABLE_VERSIONING,
    DEFAULT_EXTRACTION_MODE,
    DEFAULT_MAX_FILE_SIZE_MB,
    DEFAULT_PAGE_SIZE,
    DEFAULT_USE_BATCH_INFERENCE,
    EXTRACTION_BATCH_SIZE,
    EXTRACTION_DEFAULTS,
    GOVERNED_METRICS_ONTOLOGY_ID,
    MAX_PAGE_SIZE,
    PIPELINE_RUN_FIELDS,
    STAGED_TEXT_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_UPLOAD_CONTENT_TYPES,
    ExtractionMode,
    IngestionStatus,
    NamespaceStatus,
    SqlDialect,
    canonical_col,
    graphrag_chunk_index_name,
    graphrag_index_names,
    ontology_vector_index_name,
    sql_ident,
    sql_qualified_table,
    to_graphrag_tenant_id,
    validate_id,
    validate_namespace_id,
    validate_namespace_name,
    validate_s3_prefix,
)
from coa_common.dao import DynamoDBDAO
from coa_common.datazone_forms import (
    ASSET_TYPE_NAME,
    FORM_TYPE_NAME,
    build_forms_input,
    deserialize_form,
    serialize_form,
)
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
    TechnicalMetadata,
    to_dict,
)
from coa_common.embeddings import BedrockEmbedder, make_llama_index_embedding
from coa_common.exceptions import SCLError
from coa_common.guardrail_screener import (
    GuardrailScreener,
    GuardrailScreenerError,
    ScreeningResult,
    assessment_has_block,
)
from coa_common.logging import setup_logging
from coa_common.metadata_store import (
    AssetResult,
    MetadataStoreClient,
    MetadataStoreError,
    ProjectResult,
    SMUSClient,
)
from coa_common.namespace_resolver import resolve_namespace_id
from coa_common.principal_keys import build_principal_keys, sanitize_principal_key
from coa_common.response import (
    CORS_HEADERS,
    api_response,
    get_caller_identity,
    iso_to_epoch,
)
from coa_common.s3 import (
    get_s3_client,
    list_objects,
    parse_bucket_from_arn,
    read_file_bytes,
    upload_file,
    upload_json,
)

__all__ = [
    "ACTIVE_INGESTION_STATUSES",
    "AossVectorClient",
    "build_index_mapping",
    "bulk_with_retry",
    "is_transient",
    "oss_retry",
    "ASSET_TYPE_NAME",
    "AssetResult",
    "BedrockClient",
    "BedrockEmbedder",
    "make_llama_index_embedding",
    "BedrockInvocationResult",
    "BedrockTruncationError",
    "GuardrailBlockedError",
    "GuardrailScreener",
    "GuardrailScreenerError",
    "InputTooLargeError",
    "BusinessMetadata",
    "CORS_HEADERS",
    "Column",
    "DEFAULT_DELETE_PREV_VERSIONS",
    "DEFAULT_EMBED_DIMENSIONS",
    "DEFAULT_EMBED_MODEL_ID",
    "DEFAULT_ENABLE_PROPOSITION_EXTRACTION",
    "DEFAULT_ENABLE_VERSIONING",
    "DEFAULT_EXTRACTION_MODE",
    "DEFAULT_MAX_FILE_SIZE_MB",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_USE_BATCH_INFERENCE",
    "DiscoveredMetadata",
    "DynamoDBDAO",
    "EnrichmentSource",
    "EXTRACTION_BATCH_SIZE",
    "EXTRACTION_DEFAULTS",
    "ExtractionMode",
    "FORM_TYPE_NAME",
    "ForeignKey",
    "GOVERNED_METRICS_ONTOLOGY_ID",
    "IngestionStatus",
    "MAX_PAGE_SIZE",
    "MetadataStoreClient",
    "MetadataStoreError",
    "NamespaceStatus",
    "PrimaryKey",
    "PrincipalType",
    "ProjectResult",
    "ResourceRoleMapping",
    "ResourceType",
    "ReviewStatus",
    "SCLConfig",
    "SCLError",
    "SMUSClient",
    "ScreeningResult",
    "PIPELINE_RUN_FIELDS",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_UPLOAD_CONTENT_TYPES",
    "STAGED_TEXT_EXTENSIONS",
    "SqlDialect",
    "Table",
    "TechnicalMetadata",
    "api_response",
    "assessment_has_block",
    "build_forms_input",
    "deserialize_form",
    "serialize_form",
    "to_dict",
    "canonical_col",
    "sql_ident",
    "sql_qualified_table",
    "graphrag_chunk_index_name",
    "graphrag_index_names",
    "ontology_vector_index_name",
    "to_graphrag_tenant_id",
    "read_file_bytes",
    "get_caller_identity",
    "iso_to_epoch",
    "get_s3_client",
    "list_objects",
    "parse_bucket_from_arn",
    "resolve_region",
    "setup_logging",
    "upload_file",
    "upload_json",
    "validate_id",
    "validate_namespace_id",
    "validate_namespace_name",
    "validate_s3_prefix",
    "resolve_namespace_id",
    "sanitize_principal_key",
    "build_principal_keys",
    "USER_AGENT_EXTRA",
    "install_user_agent",
    "async_boto_config",
    "sync_boto_config",
    "opensearch_client_kwargs",
    "RetryPolicy",
]

# Tag every AWS SDK call's User-Agent for usage attribution. Runs on import of
# this shared package, which every service uses.
install_user_agent()


# Lazily expose the AOSS vector client. Importing ``coa_common``
# must NOT drag in ``opensearchpy`` — many consumers (e.g. the ontology
# api-proxy Lambda, which only needs ``resolve_region``) don't bundle it, and an
# eager import would crash their cold start with ModuleNotFoundError. The
# opensearch submodule (and its opensearchpy dependency) is imported only when
# one of these names is first accessed.
_LAZY_OPENSEARCH = {
    "AossVectorClient",
    "build_index_mapping",
    "bulk_with_retry",
    "is_transient",
    "oss_retry",
}


def __getattr__(name: str):
    if name in _LAZY_OPENSEARCH:
        import coa_common.opensearch as _osm

        return getattr(_osm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
