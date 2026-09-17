// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Re-export generic defaults from shared package
export {
  DEFAULT_RESOURCE_PREFIX,
  DEFAULT_ENV,
  DEFAULT_EVENT_BUS_NAME,
  DEFAULT_BEDROCK_MODEL_ID,
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_GRAPH_BASE_URI,
  DEFAULT_URN_PREFIX,
  DEFAULT_VOCAB_PREFIX,
  DEFAULT_EVENT_SOURCE_PREFIX,
  DEFAULT_DZ_TYPE_PREFIX,
  BRAND,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
  CONNECTOR_SPILL_KMS_TAG_KEY,
  CONNECTOR_SPILL_KMS_TAG_VALUE,
  CONNECTOR_SPILL_KEY_GLOB,
} from "@coa/shared";

/** CDK context key names. */
export const CTX_RESOURCE_PREFIX = "resource_prefix";
export const CTX_ENV = "env";
export const CTX_PROJECT_TAG = "project_tag";
export const CTX_IDP_TYPE = "idpType";
export const CTX_CALLBACK_URLS = "callbackUrls";
export const CTX_LOGOUT_URLS = "logoutUrls";
export const CTX_GRAPH_BASE_URI = "graph_base_uri";
export const CTX_EVENT_SOURCE_PREFIX = "event_source_prefix";
export const CTX_LAMBDA_RESERVED_CONCURRENCY = "lambda_reserved_concurrency";

/** Default values when context keys are not provided. */
export const DEFAULT_PROJECT_TAG = "semantic-context";

/**
 * Default reserved concurrency for the two Lambdas that request it
 * (VKG reload, doc preprocessing). Bounds each function's blast radius; it is
 * NOT load-bearing for a single-tenant deployment. Set the
 * `lambda_reserved_concurrency` context to `0` to omit the reservation
 * entirely — required on accounts whose Lambda concurrent-executions quota
 * (`L-B99A9384`) is at the reduced default of 10, where reserving any
 * concurrency drops unreserved capacity below the account minimum and CDK
 * rolls back. See external-docs/content/deploying.md.
 */
export const DEFAULT_LAMBDA_RESERVED_CONCURRENCY = 5;
export const DEFAULT_CALLBACK_URLS = ["https://localhost/authenticate/"];
export const DEFAULT_LOGOUT_URLS = ["https://localhost/"];
export const DEFAULT_GROUP_CLAIM = "cognito:groups";

/** Default initial Cognito user attributes. */
export const DEFAULT_ADMIN_USERNAME = "nobody@amazon.com";
export const DEFAULT_ADMIN_GROUP = "Admin";

export const DEFAULT_ADMIN_EMAIL = "nobody@amazon.com";

/** Default admin role name for claim mappings. */
export const DEFAULT_ADMIN_ROLE = "platform-admin";

// ────────────────────────────────────────────────────────────────────────────
// Ingestion pipeline constants (subset used by CDK)
// IngestionStatus and DEFAULT_MAX_FILE_SIZE_MB are also in constants.py
// ────────────────────────────────────────────────────────────────────────────

/** Status values stored in the doc_sources DynamoDB table. */
export const IngestionStatus = {
  PENDING: "pending",
  INGESTING: "ingesting",
  COMPLETED: "completed",
  FAILED: "failed",
  DELETING: "deleting",
  DELETE_FAILED: "delete_failed",
} as const;

export type IngestionStatusType =
  (typeof IngestionStatus)[keyof typeof IngestionStatus];

/** Unified SourceStatus — used by SourcesStack pipeline and API. */
export const SourceStatus = {
  REGISTERED: "REGISTERED",
  SCANNING: "SCANNING",
  ENRICHING: "ENRICHING",
  PENDING_REVIEW: "PENDING_REVIEW",
  COMPLETED: "COMPLETED",
  APPROVED: "APPROVED",
  SCAN_FAILED: "SCAN_FAILED",
  DELETING: "DELETING",
  DELETED: "DELETED",
  DELETE_FAILED: "DELETE_FAILED",
} as const;

/** KG Build extraction modes. */
export const ExtractionMode = {
  CONTINUOUS: "continuous",
  SEPARATED: "separated",
} as const;

/** Default per-file size limit in MB for the preprocessing Lambda. */
export const DEFAULT_MAX_FILE_SIZE_MB = 200;

// ────────────────────────────────────────────────────────────────────────────
// API Gateway request throttling
// ────────────────────────────────────────────────────────────────────────────

/**
 * Stage-wide default request throttle applied to every method via the
 * stage-level throttle. Deliberately far below the AWS account default (10,000 rps / 5,000
 * burst) — a conservative front-door ceiling. Soft: overridable per deploy via
 * `ApiStackProps.throttleRateLimit` / `throttleBurstLimit`.
 */
export const DEFAULT_THROTTLE_RATE_LIMIT = 50;
export const DEFAULT_THROTTLE_BURST_LIMIT = 100;

/**
 * Tighter per-operation throttle for "expensive" operations — those that launch
 * long-running async jobs (Fargate induction, source scans, LLM constraint
 * inference) or perform heavy graph writes. Applied per-method so a single
 * caller hammering one expensive route cannot consume the whole stage-wide
 * budget and starve cheap reads (best practice: per-operation throttling).
 * Soft: overridable via `ApiStackProps.expensiveThrottleRateLimit` / `...Burst`.
 */
export const DEFAULT_EXPENSIVE_THROTTLE_RATE_LIMIT = 5;
export const DEFAULT_EXPENSIVE_THROTTLE_BURST_LIMIT = 10;

/**
 * Expensive API operations that receive the tighter per-operation throttle.
 * `path` matches the Smithy/OpenAPI route; `method` is the HTTP verb. Only
 * routes actually present in the served OpenAPI spec are wired (see ApiStack),
 * so stale entries here are silently skipped rather than throttling nothing.
 */
export const EXPENSIVE_API_OPERATIONS: ReadonlyArray<{
  readonly method: string;
  readonly path: string;
}> = [
  // Ontology induction — launches a multi-minute Fargate + Bedrock job.
  { method: "POST", path: "/namespaces/{namespaceId}/induce" },
  // Source rescan — launches a Step Functions scan/enrichment pipeline.
  {
    method: "POST",
    path: "/namespaces/{namespaceId}/sources/{sourceId}/rescan",
  },
  // Metric OSI import — launches an import worker job.
  { method: "POST", path: "/namespaces/{namespaceId}/import-osi" },
  // Constraint inference — LLM-backed async job.
  {
    method: "POST",
    path: "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints",
  },
  // Proposal validation — LLM-backed async job.
  {
    method: "POST",
    path: "/namespaces/{namespaceId}/proposals/{proposalId}/validate",
  },
  // Constraint compilation — heavy synchronous compute.
  {
    method: "POST",
    path: "/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints",
  },
  // Proposal accept — heavy graph write (ingests the accepted ontology).
  {
    method: "POST",
    path: "/namespaces/{namespaceId}/proposals/{proposalId}/accept",
  },
];

// ────────────────────────────────────────────────────────────────────────────
// WAF rate-based (per-IP) throttling
// ────────────────────────────────────────────────────────────────────────────

/**
 * Default WAF rate-based rule limit: max requests per rolling 5-minute window
 * per source IP before that IP is blocked. Per-entity edge throttle that
 * protects the auth layer from anonymous floods before Cognito ever runs.
 * Generous for a legitimate single user, well under the stage-wide budget
 * (50 rps ≈ 15,000 / 5 min). Soft: overridable via `WafWebAclProps.rateLimit`.
 */
export const DEFAULT_WAF_RATE_LIMIT_PER_5MIN = 2000;

/** Base URL for per-namespace Neptune named graphs (ontology-engine convention). */
export const DEFAULT_GRAPH_URI_BASE = "https://ontology-workbench.local";

/**
 * AOSS vector collection — single source of truth for the collection name,
 * combined with the deployment prefix+env via `prefixed()` in the storage stack.
 * Embedding indices ({collection}-{namespace}) are scoped under this collection;
 * services derive their index prefix from the exported collection name so writers
 * and readers cannot drift.
 */
export const VECTOR_COLLECTION_SUFFIX = "vector-store";

/** Default extraction configuration — mirrors constants.py. */
export const DEFAULT_EXTRACTION_CONFIG = {
  extraction_mode: ExtractionMode.CONTINUOUS,
  use_batch_inference: "false",
  enable_versioning: "true",
  enable_proposition_extraction: "true",
  // Corpus-derived entity-class vocabulary; see graph_build.py.
  infer_entity_classifications: "true",
  // Explicit vocabulary (JSON-encoded list); "[]" means no explicit list, use
  // infer or the toolkit default depending on the flag above.
  preferred_entity_classifications: "[]",
  // Textract TABLES routing off by default.
  enable_table_extraction: "false",
  // "0" means "use the graphrag-toolkit default (256 / 25)".
  chunk_size: "0",
  chunk_overlap: "0",
  delete_prev_versions: "false",
} as const;
