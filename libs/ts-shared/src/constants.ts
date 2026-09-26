// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Application display name shown in the UI header / browser title. */
export const UI_DISPLAY_TITLE = "Context Ontology Accelerator";

/** Default resource prefix used across CDK stacks and resource naming. */
export const DEFAULT_RESOURCE_PREFIX = "coa";

/** Default deployment environment name. */
export const DEFAULT_ENV = "dev";

// ---------------------------------------------------------------------------
// Derived brand tokens — all based on DEFAULT_RESOURCE_PREFIX so deployments
// with a custom prefix (e.g. --context resource_prefix=acme) get consistent
// naming across graph URIs, events, DataZone types, and URN schemes.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Brand token for DATA-PLANE identifiers.
//
// Deliberately STATIC and independent of `resource_prefix`. Graph IRIs,
// EventBridge sources and DataZone type names are data internal to the system,
// not control-plane resources: they are written into RDF stored in Neptune, into
// event contracts between services, and into DataZone metadata. Deriving them
// from a per-deployment prefix would mean a prefix change re-mints every IRI and
// orphans the data already in the graph, and would let a reader and a writer
// deployed with different prefixes silently stop matching.
//
// `resource_prefix` still governs physical resource names (S3, IAM, DynamoDB,
// SSM paths) via `prefixed()` — that is the knob meant to vary per deployment.
// ---------------------------------------------------------------------------

/** Static brand token for data-plane identifiers. NOT the resource prefix. */
export const BRAND = "coa";

/**
 * Base authority for knowledge-graph IRIs.
 * Used by: ontology-engine (IRI minting), context-manager (SPARQL), metric-service (Neptune writes).
 * Override at deploy time via CDK context `graph_base_uri`.
 */
export const DEFAULT_GRAPH_BASE_URI = `http://${BRAND}.amazon.com`;

/**
 * URN scheme prefix for internal identifiers (metrics, classes, graphs).
 * Pattern: `urn:{urnPrefix}:{namespace}:metric:{name}`
 */
export const DEFAULT_URN_PREFIX = BRAND;

/**
 * RDF vocabulary namespace for platform-defined predicates (isMapped, groundedTo, etc.).
 * Bound as `PREFIX {vocabPrefix}: <{graphBaseUri}/vocab/{vocabPrefix}#>` in SPARQL.
 */
export const DEFAULT_VOCAB_PREFIX = BRAND;

/**
 * EventBridge event source prefix.
 * Events are emitted as `{eventSourcePrefix}.metric-service`, `{eventSourcePrefix}.ontology`, etc.
 */
export const DEFAULT_EVENT_SOURCE_PREFIX = BRAND;

/**
 * Tag a custom connector's Lambda function must carry to be invocable.
 *
 * The Athena-mediated `lambda:InvokeFunction` grant must span every account, since the
 * connector lives in the customer's, so the ARN cannot be pinned. A resource tag is the
 * scoping attribute: `aws:ResourceTag` is evaluated natively by Lambda with no per-resource
 * opt-in, and unlike a name convention it cannot be matched by accident — an untagged
 * function is simply unreachable.
 *
 * `BRAND`, not the deployment's `resource_prefix`: the customer applies this in their own
 * account and cannot know which prefix a given deployment uses, so the token must be
 * identical everywhere.
 */
export const CONNECTOR_TAG_KEY = `${BRAND}:connector`;
export const CONNECTOR_TAG_VALUE = "true";

/**
 * Tag the KMS key encrypting a connector's spill bucket must carry.
 *
 * SSE-KMS on the spill bucket is REQUIRED, which is what makes this a real control rather
 * than a partial one: every spilled read must then pass `kms:Decrypt` against a key this tag
 * allowlists. Without the mandate the check is skipped for the common case (SSE-S3, or no
 * bucket encryption) and spill authorization rests on the key prefix alone.
 */
export const CONNECTOR_SPILL_KMS_TAG_KEY = `${BRAND}:connector-spill`;
export const CONNECTOR_SPILL_KMS_TAG_VALUE = "true";

/**
 * Key prefix a connector must spill under, as a glob for IAM resource patterns.
 *
 * Spill is read by Athena using the querying principal's forward-access-session credentials,
 * so the grant cannot be pinned to a bucket — the customer owns it. The key prefix bounds it
 * instead, which lets the grant span every account INCLUDING this one.
 *
 * Deliberately NOT a tag, unlike the Lambda and KMS grants above. S3 does support tag-based
 * authorization for general purpose buckets (ABAC), and it would work here, but it requires
 * ABAC to be enabled per bucket via a separate API call — and until it is, the condition is
 * not evaluated, so the grant silently fails to match and the customer sees a bare 403 on
 * their first spilled query. It would also make Orion's CDK template effectively mandatory,
 * since a connector deployed by any other means will not have ABAC on. See the LLD (§5.1)
 * for the full rationale and the conditions under which to revisit.
 *
 * Concrete form: `connectors/{connectorId}/spills/...`, e.g. `connectors/mock/spills/`.
 */
export const CONNECTOR_SPILL_KEY_GLOB = "connectors/*/spills/*";

/**
 * DataZone form/asset type name prefix (PascalCase).
 * Form: `{dzTypePrefix}TableMetadata`, Asset: `{dzTypePrefix}RelationalTable`.
 */
export const DEFAULT_DZ_TYPE_PREFIX = BRAND.charAt(0).toUpperCase() + BRAND.slice(1);

/** Filename for the runtime config JSON served to the frontend. */
export const RUNTIME_CONFIG_FILENAME = "runtime-config.json";

// ---------------------------------------------------------------------------
// DynamoDB table logical names
// Actual table name = `{prefix}-{env}-{logicalName}`
// Used by CDK stacks (table creation) and cross-stack references (env vars).
// ---------------------------------------------------------------------------

export const TABLE_NAMES = {
  NAMESPACES: "namespaces",
  ROLES: "roles",
  RESOURCE_ROLE_MAPPINGS: "resource-role-mappings",
  CACHE_INVALIDATION: "cache-invalidation",
  SOURCES: "sources",
  SOURCE_SCAN_JOBS: "source-scan-jobs",
  ONTOLOGY_ENGINE: "ontology-engine",
  METRIC_IMPORT_JOBS: "metric-import-jobs",
} as const;

// ---------------------------------------------------------------------------
// Unstructured document upload constraints
// Must stay in sync with SUPPORTED_UPLOAD_CONTENT_TYPES in
// libs/common/src/coa_common/constants.py
// ---------------------------------------------------------------------------

/** MIME types accepted for direct file upload via pre-signed S3 URLs. */
export const SUPPORTED_UPLOAD_CONTENT_TYPES: ReadonlySet<string> = new Set([
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", // .docx
  "text/plain",
  "text/markdown",
]);

/** Maximum number of files per upload request. */
export const MAX_UPLOAD_FILES = 100;

// ---------------------------------------------------------------------------
// Metric service defaults
// ---------------------------------------------------------------------------

/** Default EventBridge bus name for metric lifecycle events. */
export const DEFAULT_EVENT_BUS_NAME = "default";

/**
 * Default Bedrock embedding model ID — MUST match the Python single source of
 * truth (coa_common.constants.DEFAULT_EMBED_MODEL_ID). Cohere
 * Embed v4 via an inference profile (on-demand unsupported). Producers
 * (induction, doc-kg-build, metrics) and consumers (serve retrieval) must all
 * use the same model, or vectors are cross-model incomparable.
 */
export const DEFAULT_BEDROCK_MODEL_ID = "us.cohere.embed-v4:0";

/**
 * Default Bedrock chat/completion model ID — MUST match the Python single
 * source of truth (coa_common.bedrock.DEFAULT_MODEL_ID). Claude Haiku 4.5 via
 * an inference profile. Distinct from DEFAULT_BEDROCK_MODEL_ID, which is the
 * embedding model; every LLM text path (source enrichment, constraint
 * inference, translation) uses this one. Consumers that only observe the model
 * (e.g. AWS/Bedrock ModelId dashboard dimensions) go dark if this drifts from
 * the Python default, so the two must move together.
 */
export const DEFAULT_BEDROCK_CHAT_MODEL_ID =
  "us.anthropic.claude-haiku-4-5-20251001-v1:0";

/**
 * Default Bedrock model ID for ontology induction, grounding rerank, and
 * description generation (ontology-engine LLM_MODEL_ID /
 * DESCRIPTION_LLM_MODEL_ID). Sonnet-class: induction quality gates on it, and
 * this tracks the same Sonnet generation the serve query LLM defaults to.
 * Overridable per deployment via the `bedrockInductionLlmModelId` SSM config
 * key (#94).
 */
export const DEFAULT_BEDROCK_INDUCTION_MODEL_ID =
  "us.anthropic.claude-sonnet-5";

/**
 * Default Bedrock model ID for the serve query LLM (Tier-2/Tier-3 reasoning,
 * NL-to-SQL/SPARQL, synthesis). Configured per deployment via the
 * `bedrockLlmModelId` SSM config key; when that key is absent ServeStack sets
 * `BEDROCK_MODEL_ID` to this value. It MUST match the runtime fallback literals
 * in `coa_serve/config.py` / `coa_serve/clients/bedrock.py`
 * (`infra/test/model-id-defaults.test.ts` pins it). It
 * exists so synth-time region validation can reason about the *effective*
 * model id for a deployment that has no config at all (#1020).
 */
export const DEFAULT_BEDROCK_LLM_MODEL_ID = "us.anthropic.claude-sonnet-5";
