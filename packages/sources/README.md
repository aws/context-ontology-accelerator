# Sources

Unified source registry for the Context Ontology Accelerator.

## Overview

The `sources` package provides the full unified source management API and pipelines for both DATABASE and DOCUMENTS sources:

1. **Full CRUD API** (`coa_sources.api`) — create, list, get, delete, rescan sources; manage database table metadata; generate upload URLs.
2. **Resource-oriented review API** — per-table/column `PUT review` and `PATCH metadata` endpoints (sync, ~400ms) for fine-grained review, plus `POST /approve` and `POST /reject` (async, 202) backed by an SQS-driven worker for bulk operations on large catalogs. See [Review API](#review-api-database-sources).
3. **Database source pipeline** (`coa_sources.database`) — Glue/JDBC connectors (PostgreSQL/Redshift schema discovery via `information_schema`), Bedrock-powered enrichment, DataZone asset creation, scan Step Functions state machine.
4. **Document source pipeline** (`coa_sources.documents`) — preprocessing Lambda, GraphRAG KG Build ECS task, deletion pipeline.

All pipelines are wired to `SourcesStack`.

## Getting Started

### Prerequisites

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (workspace-level package manager)
- Docker (for building ECS task images locally)
- AWS credentials configured (for integration tests)

### Install

From the repo root:

```bash
uv sync
```

This installs the `coa-sources` package and its workspace dependencies (`coa-common`, `coa-control-plane-server`).

### Run Tests

```bash
# Unit tests (80% coverage threshold enforced)
npx nx test sources

# Or directly:
uv run pytest packages/sources/tests/unit -v --tb=short \
  --cov=packages/sources/src --cov-fail-under=80
```

### Lint & Format

```bash
npx nx lint sources     # ruff check + ruff format --check + mypy
npx nx format sources   # auto-format with ruff
```

### Test Structure

Tests are organized by domain:

| Directory | Covers |
|-----------|--------|
| `tests/unit/api/` | Sources API Lambda (CRUD, pagination, upload URLs, review endpoints) |
| `tests/unit/database/` | Connectors (Glue, JDBC), enrichment, discovery, federation, bulk review |
| `tests/unit/documents/` | Preprocessing, KG build, trigger, deletion, namespace isolation |
| `tests/integ/` | End-to-end API tests against a deployed stack |

Tests use `pytest` markers: `@pytest.mark.unit` and `@pytest.mark.integ`. CI runs only `unit`.

#### Integration Tests

```bash
uv run pytest packages/sources -m integ -v
```

Requires a deployed stack. Test files:

| File | Coverage |
|------|----------|
| `test_sources_crud_integ.py` | Create/list/get/delete for DATABASE + DOCUMENTS sources |
| `test_sources_scan_pipeline_integ.py` | Scan to terminal status, scan job retrieval, re-scan (SCAN_FAILED recovery + 409 guard) |
| `test_sources_documents_integ.py` | Upload URL generation, LOCAL_UPLOAD + S3 doc source creation |
| `test_sources_tables_integ.py` | List/get tables, edit table metadata, edit column metadata |
| `test_sources_review_integ.py` | Per-table/column approve/reject, bulk approve→APPROVED, bulk reject→REJECTED |
| `test_sources_database_subtypes_integ.py` | Per sub-type onboarding: GLUE + JDBC (POSTGRESQL/MYSQL/SQLSERVER/REDSHIFT/ORACLE), plus the federated catalog being non-empty |
| `test_sources_snowflake_integ.py` | SNOWFLAKE onboarding: warehouse round-trip, scan to `PENDING_REVIEW`, non-empty catalog, and no-warehouse → `SCAN_FAILED` |
| `test_sources_isolation_integ.py` | Cross-namespace isolation (sources invisible across NS boundaries) |
| `test_sources_authentication_integ.py` | Token validation (missing/invalid/valid) |

Review and table tests create a real source over the seeded S3-backed Glue catalog and poll it to `PENDING_REVIEW` (no manual setup). The `test_databases` fixture reads the `coa-integ-test-databases` CloudFormation stack outputs for DB endpoints, ports, credential secret ARNs, and Glue catalog names.

Environment variables:

| Variable | Purpose |
|----------|---------|
| `INTEG_TEST_DATABASES_STACK` | Override the test-databases stack name (default `coa-integ-test-databases`). |
| `INTEG_ENRICHED_SOURCE_ID` / `INTEG_ENRICHED_NAMESPACE_ID` | Reuse a pre-existing reviewable source instead of creating one. |
| `INTEG_JDBC_CONNECTIVITY=0` | Opt out of the Tier-B JDBC onboarding tests (scan to terminal). They run by default; set to `0` when cross-VPC connectivity to the test-databases VPC (`tests/cdk/scripts/connect-cross-network.sh`) or the discovery role's access to the credential secrets is not in place, so the scan isn't expected to fail. |
| `SNOWFLAKE_HOST`, `SNOWFLAKE_SECRET_ARN`, `SNOWFLAKE_WAREHOUSE` | Required to run `test_sources_snowflake_integ.py` — Snowflake is an external SaaS account, so it cannot be stack-provisioned. All three unset ⇒ the suite skips with the missing names listed (never a silent pass). In CI they come from **project-level CI/CD variables** (Settings > CI/CD > Variables), not from `ci/mainline.yml`, so each account/fork points the suite at its own Snowflake without editing pipeline YAML. |
| `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA` | Optional Snowflake overrides (default `SNOWFLAKE_SAMPLE_DATA` / `TPCH_SF1`). |

The Snowflake account is a 30-day trial, so `test_sources_snowflake_integ.py`
**skips instead of failing** when a scan fails with an account-level error
(trial ended, account suspended/locked, credentials rejected) — that is an
environment problem no code change can fix. The tolerated signatures are listed
in `_ACCOUNT_UNAVAILABLE_SIGNATURES` and deliberately exclude anything a bad
request of ours could also produce (e.g. `does not exist or not authorized`,
which is what a wrong warehouse value looks like). The classifier has unit
coverage in `tests/unit/database/test_snowflake_integ_gating.py`, since getting
it wrong either swallows real defects or reports unfixable failures.

## Package Structure

```text
packages/sources/
├── src/
│   └── coa_sources/
│       ├── api/
│       │   └── sources_handler.py      # SourcesApiFn Lambda — GET /sources
│       ├── database/                   # Database source pipeline (Glue, JDBC)
│       │   ├── api/                    # CRUD handlers for data sources
│       │   ├── connectors/             # Glue and JDBC connectors
│       │   ├── enrichment/             # Bedrock-powered metadata enrichment
│       │   ├── pipeline/               # Discovery + enrichment Step Functions handlers
│       │   ├── bulk_review/            # Async bulk approve/reject worker logic
│       │   └── metadata_writer.py      # DataZone asset writer
│       └── documents/                  # Document source pipeline
│           ├── api/                    # CRUD handlers for doc sources
│           ├── deletion/               # S3 cleanup Lambda
│           ├── kg_build/               # GraphRAG KG build + cleanup ECS tasks
│           ├── preprocessing/          # PDF/DOCX preprocessing Lambda
│           └── trigger/                # SQS trigger Lambda
├── database/
│   ├── enrichment/Dockerfile           # ECS enrichment task image
│   ├── trigger/index.py                # SQS scan trigger Lambda
│   └── bulk_review_worker/index.py     # SQS bulk review worker Lambda (delegates to src/.../bulk_review/worker.py)
├── documents/
│   ├── preprocessing/Dockerfile        # Lambda preprocessing image
│   ├── kg-build/Dockerfile             # ECS KG build image
│   ├── deletion/requirements.txt
│   └── trigger/requirements.txt
├── tests/
│   ├── unit/
│   │   ├── api/                        # Tests for sources_handler.py
│   │   ├── database/                   # Tests for database pipeline
│   │   └── documents/                  # Tests for documents pipeline
│   └── integ/
│       ├── conftest.py                  # Fixtures: payloads, test_databases, pollers, reviewable source
│       └── test_sources_*_integ.py      # CRUD, scan, documents, tables, review, subtypes, isolation, auth
├── requirements.txt                    # Lambda runtime deps (SourcesApiFn only)
├── pyproject.toml
├── project.json
└── README.md
```

## DynamoDB Tables

### `sources-table`

Unified registry of all connected sources.

| Key | Value |
|-----|-------|
| PK  | `NS#{namespaceId}` |
| SK  | `SRC#{sourceId}` |

**GSIs:**

| Index | PK | SK | Purpose |
|-------|----|----|---------|
| `ByNamespace` | `namespaceId` | `createdAt` | List all sources for a namespace, newest first |
| `BySourceType` | `namespaceId` | `sourceTypeCreatedAt` (`{sourceType}#{createdAt}`) | Filter by type within a namespace |

### `source-scan-jobs`

Scan job history for sources.

| Key | Value |
|-----|-------|
| PK  | `SRC#{sourceId}` |
| SK  | `createdAt` (ISO timestamp) |

**GSIs:**

| Index | PK | Purpose |
|-------|----|---------|
| `ByNamespace` | `namespaceId` | Namespace-scoped cleanup on namespace deletion |

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/namespaces/{namespaceId}/sources` | List all sources (paginated, newest first) |
| `POST` | `/namespaces/{namespaceId}/sources` | Create a DATABASE or DOCUMENTS source |
| `GET` | `/namespaces/{namespaceId}/sources/{sourceId}` | Get source detail |
| `DELETE` | `/namespaces/{namespaceId}/sources/{sourceId}` | Delete a source |
| `POST` | `/namespaces/{namespaceId}/sources/{sourceId}/rescan` | Re-trigger scan or ingestion |
| `POST` | `/namespaces/{namespaceId}/sources/upload-urls` | Get pre-signed S3 upload URLs |
| `GET` | `/namespaces/{namespaceId}/sources/{sourceId}/tables` | List DataZone tables (DATABASE only) |
| `GET` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}` | Get table detail (DATABASE only) |
| `POST` | `/namespaces/{namespaceId}/sources/{sourceId}/approve` | Approve all PENDING tables/columns (async, 202) — DATABASE only |
| `POST` | `/namespaces/{namespaceId}/sources/{sourceId}/reject` | Reject all PENDING tables/columns (async, 202) — DATABASE only |
| `PUT` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review` | Approve/reject a single table (sync) — DATABASE only |
| `PATCH` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata` | Edit a table's business metadata — DATABASE only |
| `PATCH` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys` | Edit a table's primary key and foreign key relationships — DATABASE only |
| `PUT` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review` | Approve/reject a single column (sync) — DATABASE only |
| `PATCH` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata` | Edit a column's business metadata — DATABASE only |
| `PUT` | `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keep` | Decline a re-scan-flagged removal so approving the re-scan will not delete the table, or one column of it — DATABASE only |
| `GET` | `/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}` | Get scan job status (DATABASE only) |
| `GET` | `/namespaces/{namespaceId}/sources/{sourceId}/scan` | List scan and review history, newest first (DATABASE only) |
| `PUT` | `/namespaces/{namespaceId}/sources/{sourceId}/metadata` | Update source-level metadata (DATABASE only) |

### Authentication

All requests must include a valid Bearer token in the `Authorization` header. The API Gateway Lambda authorizer validates the token and enforces namespace-level access control — callers must have `viewNamespace` permission on the requested namespace.

```
Authorization: Bearer <id_token>
```

### Query Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `sourceType` | `DATABASE` \| `DOCUMENTS` | Filter by source type (uses `BySourceType` GSI) |
| `maxResults` | integer (1–100) | Page size (default: 100) |
| `nextToken` | string | Pagination cursor from previous response |

### Response Example

```json
{
  "items": [
    {
      "sourceId": "01KS35KF268AWM6FDPP6ZHXYJ1",
      "namespaceId": "550e8400-e29b-41d4-a716-446655440000",
      "name": "prod-glue-catalog",
      "sourceType": "DATABASE",
      "sourceSubType": "GLUE_DATABASE",
      "status": "APPROVED",
      "createdAt": 1779296746,
      "updatedAt": 1779301987
    }
  ],
  "nextToken": "eyJQSyI6..."
}
```

`createdAt` and `updatedAt` are Unix epoch seconds (integer).  
`nextToken` is omitted when there are no more pages.

### Error Responses

| Status | Condition |
|--------|-----------|
| `400` | Invalid `namespaceId` format, `maxResults` out of range (1–100), or malformed `nextToken` |
| `401` | Missing or invalid Bearer token |
| `403` | Caller lacks `viewNamespace` permission on the namespace |
| `409` | Conflict — applies to review/edit endpoints when the source is in a non-reviewable transient state (`SCANNING`, `ENRICHING`, `APPROVING`, `REJECTING`, `DELETING`) |
| `500` | Internal server error (DynamoDB query failure) |

## Review API (DATABASE sources)

DATABASE sources go through a metadata review workflow after scan + enrichment complete. The API is split into two complementary surfaces:

### Synchronous per-asset endpoints

Single-table and single-column operations operate on one DataZone asset (one search + one `get_asset_forms` ≈ 400ms regardless of source size). Each endpoint is **idempotent** — a DataZone revision is only written when the resulting state actually changes.

```
PUT    /sources/{id}/tables/{tid}/review                 — body: { "decision": "APPROVED" | "REJECTED" }
PATCH  /sources/{id}/tables/{tid}/metadata               — body: { "overrides": { description?, synonyms?, glossaryTerms?, tags? } }
PUT    /sources/{id}/tables/{tid}/columns/{c}/review     — body: { "decision": "APPROVED" | "REJECTED" }
PATCH  /sources/{id}/tables/{tid}/columns/{c}/metadata   — body: { "overrides": { description?, ... } }
PATCH  /sources/{id}/tables/{tid}/keys                    — body: { "primaryKey"?: { "columns": [...] }, "foreignKeys"?: [{ "column", "targetTable", "targetColumn"? }] }
```

**Editing keys (`PATCH .../keys`):** lets a steward correct a table's primary
key and foreign key relationships.

- Provide `primaryKey` and/or `foreignKeys`. An **omitted** field is left
  unchanged; an **empty list** clears it (`{ "columns": [] }` clears the PK).
- Key columns are validated against the table's own columns — an unknown
  `primaryKey.columns` entry or foreign-key `column` returns `400`; each
  foreign key requires `column` and `targetTable`. Duplicate primary-key
  columns are de-duplicated server-side (order preserved).
- Affected keys are stored with `enrichmentSource`/`source = STEWARD_SPECIFIED`,
  which **overrides** deterministic or AI-inferred keys.
- Like the metadata edit endpoint, it **never** changes `reviewStatus`.

**Cascade rules** (see `libs/common/src/coa_common/review_logic.py`):

The cascade rules differ between per-asset and bulk callers because they
have different semantics. Per-asset endpoints are *commands* — the user is
explicitly making a decision on this specific resource. Bulk endpoints are
*defaults* — the user clicked "Approve All" without inspecting every prior
explicit decision.

**Per-asset (`PUT`/`PATCH` on a single table or column):**

- `APPROVED` on a table cascades to its `PENDING_REVIEW` columns; **never** overrides `REJECTED` columns (those require explicit re-approval).
- `REJECTED` on a table cascades to every non-`REJECTED` column (clobbers `APPROVED`). The user is explicitly rejecting the table; their prior column-level approvals are not preserved by this command.
- The table's status is always flipped to match the decision (overrides any prior explicit decision on the table itself — the user is asking for the change).
- `PATCH .../metadata` sets `enrichmentSource = STEWARD_EDITED` but **never** changes `reviewStatus`. The "edit + approve" UX is two calls: `PATCH` then `PUT /review`.

**Bulk (`POST /approve` and `POST /reject`):**

Bulk operations preserve every prior explicit decision in both directions. This protects the steward from silently undoing earlier per-asset work when they hit "Approve All" at the end of a review session.

- A table with `reviewStatus = REJECTED` is **skipped entirely** — the table and its columns are untouched. A table with `reviewStatus = APPROVED` still cascades to any remaining `PENDING_REVIEW` columns (handles the case where a table was approved before all its columns reached terminal state). Only `PENDING_REVIEW` tables have their table-level status flipped.
- Within a `PENDING_REVIEW` table that bulk reaches, only `PENDING_REVIEW` columns flip. Both `APPROVED` and `REJECTED` columns are preserved.

| Scenario | Per-asset `PUT /review` | Bulk `POST /approve` or `/reject` |
|---|---|---|
| `APPROVE` on a `REJECTED` table | Flips to `APPROVED` | **Skipped** (table preserved) |
| `APPROVE` on an `APPROVED` table with `PENDING` cols | Cascades columns | Cascades `PENDING` columns |
| `REJECT` on an `APPROVED` table | Flips to `REJECTED` | Flips to `REJECTED` |
| `APPROVE` cascade with `REJECTED` columns | Columns preserved | Columns preserved |
| `REJECT` cascade with `APPROVED` columns | Columns flipped to `REJECTED` | Columns flipped to `REJECTED` |
| `APPROVE` cascade with `APPROVED` columns | No-op | No-op |
| `REJECT` cascade with `REJECTED` columns | No-op | No-op |

A `tablesApproved` counter on the source record is updated atomically (DynamoDB `ADD` expression) when a table transitions in or out of `APPROVED` via per-asset review. The bulk worker recomputes the counter from the actual final state when it persists the terminal status.

### Asynchronous bulk endpoints (202 + worker)

Bulk approve/reject for sources with many tables would exceed API Gateway's 29-second timeout if run inline. The API Lambda atomically transitions the source to a transient state and enqueues an SQS message; a dedicated worker Lambda (`sources-bulk-review-worker`) processes the message in the background.

```
POST /sources/{id}/approve   — 202 Accepted, source enters APPROVING
POST /sources/{id}/reject    — 202 Accepted, source enters REJECTING
```

**Lifecycle:**

```
APPROVE flow:  PENDING_REVIEW (or APPROVAL_FAILED) ──► APPROVING ──► APPROVED
                                                            │
                                                            └────► APPROVAL_FAILED  (worker error; client may retry)

REJECT flow:   PENDING_REVIEW (or REJECTION_FAILED) ──► REJECTING ──► PENDING_REVIEW
                                                            │
                                                            └────► REJECTION_FAILED (worker error; client may retry)
```

Reject completes back to `PENDING_REVIEW` (not `APPROVED`) — rejection sets every non-rejected table to `REJECTED` but leaves the source open for further review.

**Idempotency** is enforced at two layers:

1. The API handler uses a conditional DynamoDB update — only `PENDING_REVIEW` or the matching `*_FAILED` state can transition into the transient state. Duplicate API calls get `409 Conflict`.
2. The worker checks the source's status before any DataZone work; if it's no longer in the matching transient state (because an earlier worker invocation already finished), the duplicate exits silently.

**Race protection.** Synchronous per-asset operations are blocked while the source is in `APPROVING`, `REJECTING`, or any other non-reviewable transient state. The handler reads the source's status via projected DDB get and returns `409` if not allowed. This prevents a per-table edit from being silently overwritten by a bulk worker that loaded the asset before the edit.

### Polling for completion

Frontend polls `GET /sources/{sourceId}` and inspects the `status` field. Terminal states for the bulk flow are `APPROVED`, `PENDING_REVIEW` (after a successful reject), `APPROVAL_FAILED`, or `REJECTION_FAILED`.

## Data Model

### `sourceType` values

| Value | Description |
|-------|-------------|
| `DATABASE` | Structured data source (Glue or JDBC) |
| `DOCUMENTS` | Unstructured document source (S3 or file upload) |

### `sourceSubType` values

| Value | Parent type | Description |
|-------|-------------|-------------|
| `GLUE_DATABASE` | `DATABASE` | AWS Glue Data Catalog database |
| `JDBC_DATABASE` | `DATABASE` | JDBC-connected relational database |
| `CUSTOM_CONNECTOR` | `DATABASE` | Customer-authored Athena Query Federation SDK connector — a Lambda in the customer's account, registered here as a `LAMBDA`-type Athena data catalog and discovered through Athena SQL (`SHOW`/`DESCRIBE`) |
| `S3` | `DOCUMENTS` | S3 bucket prefix |
| `LOCAL_UPLOAD` | `DOCUMENTS` | Direct file upload |

### `status` values

| Value | Description |
|-------|-------------|
| `REGISTERED` | Source registered, processing not yet started |
| `SCANNING` | Processing pipeline running |
| `ENRICHING` | AI metadata enrichment in progress |
| `PENDING_REVIEW` | Enrichment complete, awaiting review |
| `APPROVING` | Bulk approve in flight (async worker) — DATABASE only |
| `APPROVAL_FAILED` | Bulk approve worker failed; client may retry — DATABASE only |
| `REJECTING` | Bulk reject in flight (async worker) — DATABASE only |
| `REJECTION_FAILED` | Bulk reject worker failed; client may retry — DATABASE only |
| `COMPLETED` | Processing completed (document sources) |
| `APPROVED` | All tables reviewed and approved (database sources) |
| `SCAN_FAILED` | Processing failed |
| `DELETING` | Deletion in progress |
| `DELETED` | Fully deleted |
| `DELETE_FAILED` | Deletion failed |

## Environment Variables (SourcesApiFn Lambda)

| Variable | Required | Description |
|----------|----------|-------------|
| `SOURCES_TABLE` | Yes | DynamoDB sources table name |
| `SOURCE_SCAN_JOBS_TABLE` | Yes | DynamoDB scan jobs table name |
| `NAMESPACES_TABLE` | Yes | DynamoDB namespaces table (read-only — used by the reviewable-state guard and review handlers) |
| `SCAN_QUEUE_URL` | Yes | SQS URL of the database scan queue |
| `INGESTION_QUEUE_URL` | Yes | SQS URL of the document ingestion queue |
| `REVIEW_QUEUE_URL` | Yes | SQS URL of the bulk review queue (consumed by `sources-bulk-review-worker`) |
| `BUCKET_NAME` | Yes | S3 bucket for document uploads |
| `DELETION_STATE_MACHINE_ARN` | Yes | Step Functions ARN for the document deletion pipeline |
| `SMUS_DOMAIN_ID` | Yes | DataZone domain ID |
| `PROJECT_ACCESS_ROLE_ARN` | Yes | IAM role ARN for DataZone project access |
| `FEDERATION_PROVISIONER_ROLE_ARN` | No | ARN of the federation provisioner role holding Lake Formation data-lake-admin privileges. The handler assumes it to tear down a JDBC source's Glue federated catalog on deletion. When unset, federated-resource cleanup is skipped. |
| `RESOURCE_PREFIX` | Yes, for `CUSTOM_CONNECTOR` | Deployment resource prefix (e.g. `scl-dev-`), used to derive the per-source Athena data-catalog name. **Load-bearing:** unset, `derive_catalog_name` falls back to a hard-coded `coa-dev-`, and every `CreateDataCatalog` then fails `AccessDenied` against the prefix-scoped IAM policy — which reads as a policy defect rather than a naming one. |
| `ALLOWED_ORIGIN` | No | CORS allowed origin (default: `*`) |

## Environment Variables (Database Pipeline)

### Discovery Lambda (`sources-db-connector`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SOURCES_TABLE` | Yes | DynamoDB data source connectors table |
| `SOURCE_SCAN_JOBS_TABLE` | Yes | DynamoDB scan jobs table |
| `NAMESPACES_TABLE` | Yes | DynamoDB namespaces table |
| `SMUS_DOMAIN_ID` | Yes | DataZone domain ID |
| `PROJECT_ACCESS_ROLE_ARN` | Yes | IAM role ARN for DataZone project access |
| `DATAZONE_WRITE_PARALLELISM` | No | Number of parallel DataZone asset writes per discovery invocation (default `10`, must be `>= 1`). Increase cautiously for large sources; higher values speed up the per-table asset writes but raise DataZone API throttling risk. Invalid values fall back to the default. |
| `MAX_TABLES_PER_SOURCE` | No | Fail-fast guardrail for the table count a single source may register in one discovery pass (default `10000`; `0` disables the cap). Above this, discovery fails fast with an actionable error instead of silently hitting the 15-minute Lambda timeout — see the [structured-data troubleshooting guide](../../docs/guides/structured-data/index.md#troubleshooting) for the "exceeding the limit" error. Invalid values fall back to `0` (cap disabled). |

JDBC-only step that provisions the managed Glue federated catalog (see [Athena Queryability](#athena-queryability)). Isolated from discovery so the Lake Formation data-lake-admin privilege stays on this single-purpose role.

| Variable | Required | Description |
|----------|----------|-------------|
| `SOURCES_TABLE` | Yes | DynamoDB data source connectors table (reads source config, persists `glueConnectionName` / `athenaDataCatalogName`) |
| `CONNECTOR_SECURITY_GROUP_ID` | No | Security group attached to Glue Connections. Source-DB SGs must allow inbound from this SG. Skipping it disables federated catalog provisioning. |
| `CONNECTOR_SUBNET_ID` | No | Subnet ID where Glue Connections live. Single AZ — see [Athena Queryability](#athena-queryability) caveat. |
| `RESOURCE_PREFIX` | No | Resource-naming prefix (default `coa-dev-`); used to build the `{prefix}ds_{hash}` Glue Connection / catalog names. |
| `FEDERATED_CATALOG_ROLE_ARN` | Yes (for federation) | Glue Data Catalog / Lake Formation role set as the federated connection's `ROLE_ARN`; Lake Formation assumes it to vend credentials for the managed (no-Lambda) connector. |
| `ATHENA_SPILL_BUCKET` | Yes (for federation) | S3 bucket name for Athena query overflow data, set on the Glue Connection's `AthenaProperties.spill_bucket`. |

### Enrichment ECS Task (`sources-db-enrichment-agent`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SOURCES_TABLE` | Yes | DynamoDB data source connectors table |
| `SOURCE_SCAN_JOBS_TABLE` | Yes | DynamoDB scan jobs table |
| `NAMESPACES_TABLE` | Yes | DynamoDB namespaces table |
| `SMUS_DOMAIN_ID` | Yes | DataZone domain ID |
| `PROJECT_ACCESS_ROLE_ARN` | Yes | IAM role ARN for DataZone project access |
| `DATASOURCE_ID` | Yes | Injected by Step Functions container override |
| `SCAN_JOB_ID` | Yes | Injected by Step Functions container override |
| `NAMESPACE_ID` | Yes | Injected by Step Functions container override |
| `SCAN_TYPE` | No | `full` or `incremental` (default: `full`) |

### Scan Trigger Lambda (`sources-db-scan-trigger`)

| Variable | Required | Description |
|----------|----------|-------------|
| `STATE_MACHINE_ARN` | Yes | ARN of the database scan Step Functions state machine |

### Bulk Review Worker Lambda (`sources-bulk-review-worker`)

Consumes SQS messages from the bulk review queue and applies the review decision to every PENDING table/column on a source.

| Variable | Required | Description |
|----------|----------|-------------|
| `SOURCES_TABLE` | Yes | DynamoDB sources table (status transitions + tablesApproved counter) |
| `NAMESPACES_TABLE` | Yes | DynamoDB namespaces table (resolves `dataZoneProjectId`) |
| `SMUS_DOMAIN_ID` | Yes | DataZone domain ID |
| `PROJECT_ACCESS_ROLE_ARN` | Yes | IAM role ARN for DataZone project access |
| `BULK_REVIEW_PARALLELISM` | No | Number of parallel `create_asset_revision` calls (default `10`) |

## Environment Variables (Documents Pipeline)

### Preprocessing Lambda (`sources-doc-preprocessing`)

| Variable | Required | Description |
|----------|----------|-------------|
| `BUCKET_NAME` | Yes | S3 bucket for raw uploads and staging |
| `DOC_SOURCES_TABLE` | Yes | DynamoDB doc sources table |
| `MAX_FILE_SIZE_MB` | No | Maximum file size in MB (default: 100) |

### KG Build ECS Task (`sources-doc-kg-build`)

| Variable | Required | Description |
|----------|----------|-------------|
| `BUCKET_NAME` | Yes | S3 bucket for staging data |
| `DOC_SOURCES_TABLE` | Yes | DynamoDB doc sources table |
| `NEPTUNE_ENDPOINT` | Yes | Neptune cluster endpoint |
| `OPENSEARCH_ENDPOINT` | Yes | OpenSearch Serverless endpoint |
| `BATCH_INFERENCE_ROLE_ARN` | Yes | IAM role for Bedrock batch inference |
| `DOC_SOURCE_ID` | Yes | Injected by Step Functions container override |
| `NAMESPACE_ID` | Yes | Injected by Step Functions container override |
| `TENANT_ID` | Yes | Injected by Step Functions container override |
| `STAGING_PREFIX` | Yes | Injected by Step Functions container override |
| `EXTRACTION_MODE` | Yes | Injected by Step Functions container override |

### Ingestion Trigger Lambda (`sources-doc-ingestion-trigger`)

| Variable | Required | Description |
|----------|----------|-------------|
| `STATE_MACHINE_ARN` | Yes | ARN of the document ingestion Step Functions state machine |
| `BEDROCK_MODEL_ARN` | Yes | Bedrock inference profile ARN for KG build |

### Deletion Cleanup Lambda (`sources-doc-deletion-cleanup`)

| Variable | Required | Description |
|----------|----------|-------------|
| `DOC_SOURCES_TABLE` | Yes | DynamoDB doc sources table |

## Athena Queryability

JDBC database sub-types (PostgreSQL, MySQL, Redshift, SQL Server) become directly queryable from Amazon Athena via federated query. The scan pipeline transparently provisions a **managed Glue federated catalog** (no connector Lambda, no CloudFormation stack) so users do not have to wire any of this up themselves.

### When provisioning happens

Federation provisioning runs as its own dedicated pipeline step — **`DbFederation`** — that executes _after_ Discovery and _before_ Enrichment (`Discovery → Federation → Enrichment`). It is isolated in the `sources-federation-provisioner` Lambda, which holds the Lake Formation data-lake-admin privilege required to create federated catalogs; keeping it out of the broad discovery connector limits the blast radius of that privilege. Provisioning is **idempotent** — re-scans are no-ops if the resources already exist. The handler self-gates: **S3/Iceberg sources** (`GLUE_DATABASE` without a `host`) and JDBC sources with incomplete config return success and are skipped — they are already queryable through Athena's native `AwsDataCatalog`.

**`CUSTOM_CONNECTOR` sources take a third branch.** There is nothing to provision: their Lambda-backed Athena data catalog is registered by the sources API at **source-create**, not here, because this sub-type's discovery queries that catalog and Discovery runs first. The branch only marks the source `queryable` — discovery having succeeded is the evidence, since the `SHOW`/`DESCRIBE` statements it ran prove the catalog resolves and the connector answers. No Glue object and no Lake Formation grant exist for this sub-type, so nothing else gates it. The write raises on failure for the same reason it does for JDBC: a source that scanned cleanly and then answers nothing is worse than a failed scan.

A sub-type the `SourceSubType` enum recognises but this handler has no branch for now **raises** rather than silently skipping, so a new `DATABASE` sub-type cannot ship leaving every source of that type stuck at `queryable=False`. An absent or unrecognised value keeps the historical no-op, since a source deleted concurrently with its scan reads back as an empty record.

Federation failure is **FATAL for JDBC sources**. If provisioning or its persistence raises, the `DbFederation` step fails and the pipeline's error catch marks the scan `FAILED` and the source `SCAN_FAILED` — a source that isn't queryable is not a successful scan. (This is a behavioral change from the previous non-fatal approach where discovery completed even if federation failed.)

### Prerequisite — Lake Formation data-lake admin

Federation provisioning **and** teardown require the federation provisioner role (`{prefix}federated-catalog-role`, ARN published at SSM `/{prefix}/sources/federation-provisioner-role-arn`) to be registered as a Lake Formation **data-lake admin**. Creating a federated catalog needs `Create Catalog` / `DATA_LOCATION_ACCESS` that only an LF admin can grant, and source deletion assumes this role (via `FEDERATION_PROVISIONER_ROLE_ARN`) to drop the LF-governed catalog. This is an account-global setting **not** managed by this service stack — it must be applied during foundation/LF bootstrap. Without it, every JDBC scan fails with `Insufficient Lake Formation permission(s): Required Create Catalog on Catalog` (an LF authorization error, not IAM). See [`infra/README.md`](../../infra/README.md#athena-federation-architecture) for the bootstrap command.

### Resources created per source

Provisioning mirrors the Athena "create PostgreSQL data source" console flow and creates **no Lambda and no CloudFormation stack**:

| Resource | Naming | Purpose |
|---|---|---|
| Glue Connection | `{resource-prefix}ds_{hash}` (lowercased prefix, 16-char source-id hash) | Engine-typed JDBC connection with `AthenaProperties.MANAGED_CONNECTION=true` (AWS runs the connector as a managed service), `ConnectionProperties.ROLE_ARN` set to the federated-catalog role, host/port/database + Secrets Manager auth |
| Lake Formation registration | registers the connection ARN | `register_resource(WithFederation=True)` so the catalog is LF-governed |
| Glue federated catalog | same name as the Glue Connection | `create_catalog` with a `FederatedCatalog` block pointing at the connection |

Both the connection and catalog names are returned in the `GetSource` response under `databaseDetails.glueConnectionName` and `databaseDetails.athenaDataCatalogName`. They are torn down by `cleanup_federated_resources` when the source is deleted via `DELETE /namespaces/{namespaceId}/sources/{sourceId}`. Dropping an LF-governed catalog requires the federation provisioner's Lake Formation admin role, which the sources-api Lambda assumes for the teardown. Because these are billable resources with no automatic recovery path, a teardown failure **blocks** the delete (HTTP 500) so the source row remains and the delete can be retried — it is not silently orphaned.

### Per-namespace workgroup

Athena workgroups provide query isolation: each namespace gets its own workgroup `{resource-prefix}{namespaceId}`. Lifecycle, configuration, and naming are managed by the control-plane namespace service — see `packages/control-plane/README.md` and `infra/README.md#per-namespace-workgroups-namespace-service`. The workgroup name is exposed on `GetNamespace.athenaWorkgroupName`.

### Network requirements

Glue Connections used for federation reach source databases over the customer VPC. The discovery Lambda passes:

- **Connector security group** (`{resource-prefix}connector-sg`, env: `CONNECTOR_SECURITY_GROUP_ID`) — outbound to common database ports.
- **Subnet** (env: `CONNECTOR_SUBNET_ID`) — a single private subnet with NAT egress.

> **AZ availability:** Each Glue Connection is bound to one subnet (and therefore one AZ). A failure of that AZ will affect federated queries through that connection. Multi-AZ resilience is tracked as future work; today, customers requiring HA across AZs should provision an additional connection in a different subnet/AZ and switch the catalog to point at it.

The **source database security group** must allow inbound traffic from the connector security group on the database port.

### Querying via Athena

```sql
-- inside the namespace's workgroup. Managed federated catalogs are nested
-- under Athena's default AwsDataCatalog, so queries are 4-part:
--   "AwsDataCatalog"."<catalog>"."<schema>"."<table>"
SELECT *
FROM "AwsDataCatalog"."coa-dev-ds_<hash>"."<schema>"."<table>"
LIMIT 100;
```

> **⚠️ Query syntax migration:** Sources provisioned with the previous SAR
> multiplexer connector were queried with 3-part names
> (`"<catalog>"."<schema>"."<table>"`). Managed Glue federated catalogs are
> nested under `AwsDataCatalog` and require the 4-part form above. Existing
> saved queries and BI tool connections must be updated, e.g.
> `SELECT * FROM "my-jdbc-db"."public"."users"` →
> `SELECT * FROM "AwsDataCatalog"."my-jdbc-db"."public"."users"`.

Both names are available on the source detail object:

```json
{
  "sourceId": "abc-123",
  "sourceType": "DATABASE",
  "sourceSubType": "JDBC_DATABASE",
  "databaseDetails": {
    "glueConnectionName": "coa-dev-ds_a1b2c3d4e5f6a7b8",
    "athenaDataCatalogName": "coa-dev-ds_a1b2c3d4e5f6a7b8"
  }
}
```

### Input validation

JDBC URL components (`host`, `port`, `database_name`) are validated before any AWS call to prevent JDBC parameter injection (e.g., a database name like `mydb;trustServerCertificate=true` on SQL Server). Validation rules:

- **host** — conventional DNS hostname (alphanumerics + `.` `-`) or IPv4,
  max 253 chars. For Snowflake only, the account identifier in the first
  hostname label may also contain internal `_` characters (for example,
  `my_account.snowflakecomputing.com`); suffix labels retain conventional DNS
  rules.
- **port** — integer in `[1, 65535]`.
- **database** — alphanumerics + `_` `-`, max 128 chars.

### Direct PostgreSQL/Redshift search path

PostgreSQL and Redshift sources use the direct JDBC query path for single-source
queries. After each successful scan, the source record's `discoveredSchemas`
field contains every schema that passed the configured include/exclude filters.
Serve uses that complete list, in its stored order, as the transaction's exact
`search_path`.

`public` is not appended implicitly. If queries must resolve objects in
`public`, include that schema in the scan scope so it appears in
`discoveredSchemas`. Before fetching credentials or opening a transaction,
serve validates every schema as an unquoted PostgreSQL identifier. Invalid or
malformed metadata fails the request rather than silently dropping a schema.
A database error from `SET search_path` also fails the request and rolls the
transaction back before customer SQL runs.

For a source that has not completed a scan, serve falls back to a single plain
identifier from `schemaFilter` (or the legacy `schemaName` field), then to
`public`. MySQL and SQL Server do not use this PostgreSQL search-path behavior.

### Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| Athena query fails with "Catalog not found" | Source has not been scanned yet, or scan failed before provisioning | Re-run the scan; check CloudWatch logs for the `sources-db-connector` Lambda |
| Query times out / connection refused | Source DB security group doesn't allow inbound from connector SG | Add inbound rule from connector SG on the DB port |
| Workgroup not found | Namespace was created before the Athena results bucket SSM param was populated | Re-create the namespace or contact the platform team |
| `AccessDenied` on Glue Connection | Discovery Lambda's IAM role missing `glue:GetConnection`/`glue:CreateConnection` on the resource | Verify the IAM policy in `SourcesStack` |

## Pipelines

### Database Scan Pipeline (`sources-db-scan-pipeline`)

Step Functions state machine orchestrating table discovery and enrichment:

```
DbScanQueue (SQS)
  → DbScanTriggerFn (Lambda)
    → sources-db-scan-pipeline (Step Functions)
        1. DbUpdateStatusDiscovering  — mark scan job DISCOVERING
        2. DbDiscovery (Lambda)       — connect to Glue/JDBC, discover tables, write DataZone assets
        3. DbFederation (Lambda)      — JDBC-only: provision managed Glue federated catalog (fatal on failure)
        4. DbUpdateStatusEnriching    — mark scan job ENRICHING
        5. DbEnrichment (ECS Fargate) — Bedrock-powered metadata enrichment, update DataZone assets
                                        (no-op when the source has metadataEnrichmentEnabled=false:
                                         the task short-circuits, skips Bedrock, and transitions
                                         the source straight to PENDING_REVIEW)
        6. DbUpdateStatusCompleted    — mark scan job COMPLETED
        (on error) → DbUpdateStatusFailed + DbUpdateSourceStatusScanFailed → Fail
```

#### Metadata enrichment toggle

`CreateDatabaseSourceInput.metadataEnrichmentEnabled` (default `true`) controls whether the AI enrichment step runs for the source. When set to `false`:

- The flag is persisted on the source record (`metadataEnrichmentEnabled` attribute).
- `DbEnrichment` reads the source row at start, sees the flag, and short-circuits — no Bedrock invocations and no DataZone revisions for AI-generated business metadata.
- The source transitions `SCANNING → PENDING_REVIEW` (skipping `ENRICHING`).
- Stewards can still review the discovered technical metadata. The toggle can be flipped on later via `UpdateSourceMetadata` and a re-scan will run enrichment on the next pipeline pass.

### Bulk Review Worker (`sources-bulk-review-worker`)

SQS-triggered Lambda that runs the async portion of `ApproveSource` / `RejectSource`. The API handler does the conditional status transition + enqueue and returns 202; this worker does the actual DataZone writes:

```
BulkReviewQueue (SQS, visibility 360s, DLQ after 3 receives)
  → BulkReviewWorkerFn (Lambda — 5min timeout, ARM64, VPC)
        1. parse_message              — validate { namespaceId, sourceId, decision, nextToken?, tablesApprovedSoFar? }
        2. _verify_in_transient        — confirm source is still in APPROVING/REJECTING (idempotency guard;
                                         continuations pass because the source stays transient until the last page)
        3. _load_asset_page           — ONE bounded page of DataZone search + parallel get_asset_forms, resuming
                                         from nextToken; bounded by a per-invocation table budget + wall-clock budget
        4. apply_decision_to_table    — shared cascade rules (libs/common/review_logic.py)
        5. _write_revision (parallel) — ThreadPoolExecutor of N create_asset_revision calls; only assets that
                                        actually changed status are written (write-only-when-changed)
        6a. more assets remain        — _enqueue_continuation (re-enqueue with nextToken + running approved count),
                                         leave the source transient; do NOT write terminal
        6b. final page                — _persist_terminal_state (APPROVED / PENDING_REVIEW / *_FAILED + the FULL
                                         accumulated tablesApproved counter)
```

A source too large for one invocation is processed across `ceil(N / budget)` chained invocations — every table is eventually approved with no silent drop (#853). CloudWatch alarms on the worker's error metric and on DLQ depth fire on the first failure.

### JDBC Schema Discovery

The JDBC connector (`coa_sources.database.connectors.jdbc.JdbcConnector`) implements `discover_metadata` for six engines via a per-engine **dialect** (`connectors/dialects.py`):

1. Resolve the engine's dialect and connect via its driver (TLS enforced where the driver exposes it).
2. List schemas via `information_schema.schemata` and apply filters:
   - `schema_filter` regex (include) — matches schema names to keep.
   - `schema_exclude_filter` regex (exclude) — applied after include.
   - When no exclude is provided, system schemas (`pg_catalog`, `information_schema`, `pg_toast.*`) are excluded by default.
3. For each surviving schema, list tables (BASE TABLE + VIEW), apply `table_filter` / `table_exclude_filter`.
4. Pull all surviving tables' columns in a single `information_schema.columns` query (avoids per-table round trips).
5. Build `Table` objects with `database = schema_name` so the canonical `table_id` is `{schema}.{table}` (matches the design's JDBC identifier format).
6. Compute the same 16-character schema hash format as the Glue connector for re-scan change detection.

All schema and table names from configuration are bound via parameter placeholders — never interpolated into SQL — to prevent injection through the regex filter inputs.

#### Deterministic PK/FK discovery

After columns are discovered, primary and foreign keys are read from the ANSI
`information_schema` constraint views (`table_constraints`, `key_column_usage`,
`constraint_column_usage`) in two parameterized queries per schema. Discovered
constraints are tagged `EnrichmentSource.DETERMINISTIC` with `confidence = 1.0`
and populate `Table.primary_key` / `Table.foreign_keys`. They are authoritative:
AI enrichment never overwrites a deterministic primary key, and skips foreign-key
inference entirely when deterministic foreign keys exist.

#### Authentication

Credentials are read from the AWS Secrets Manager secret at
`credentialSecretArn` (the secret must contain `username` and `password`).
Cross-account secrets are read via the assumed `crossAccountRoleArn`. TLS is
always enforced on the connection.

#### Throttle handling & metrics

- Retries are left to botocore. Glue / Lake Formation clients use `standard`
  retry mode (exponential backoff + jitter on throttling); these calls are only
  a handful per source onboarding, so no custom retry is warranted. The Bedrock
  enrichment client uses `adaptive` mode (client-side rate limiting + backoff),
  since per-table LLM invocation is the actual throttle-prone path.
- CloudWatch metrics are written as EMF JSON on stdout (namespace
  `COA/Sources`) — no `PutMetricData` call. **This only becomes a
  metric on the Lambda paths.** EMF auto-extraction is a property of the Lambda
  log pipeline; the enrichment **ECS task** runs with a plain `awsLogs` driver
  and no `PutMetricData` grant, so its `emit_metric` calls land in CloudWatch
  Logs as JSON lines and never become CloudWatch metrics. The enrichment metric
  table below is therefore log-only today — see
  [Observability](#observability--structured-scan-dashboard).

**Discovery metrics** (emitted from `pipeline/discovery_handler.py`, Lambda — live):

| Metric | Unit | Dimensions | Description |
|---|---|---|---|
| `ScanDuration` | Milliseconds | `SourceType` | Total discovery scan time |
| `DiscoveryDuration` | Milliseconds | `SourceType` | Metadata discovery time |
| `ValidationLatency` | Milliseconds | `SourceType` | Connection test time |
| `TablesDiscovered` | Count | `SourceType` | Tables found in source |
| `ConnectionValidation` | Count | `SourceType`, `Result` | Connection test outcome (`Result` = `Success` \| `Failure`) |

**Catalog write metrics** (emitted from `metadata_writer.py`, Lambda — live):

| Metric | Unit | Dimensions | Description |
|---|---|---|---|
| `CatalogAssetWrites` | Count | `Operation` | SageMaker Catalog assets written per discovery. `Operation` = `Create` \| `Update` — the writer has **no delete path**, so this is not full CRUD. Emitted as two aggregate datums per scan, not per asset. |

**Review / acceptance metrics** (emitted from `api/database_routes.py` and
`database/bulk_review/worker.py`, both Lambda — live):

| Metric | Unit | Dimensions | Description |
|---|---|---|---|
| `TablesApprovedByReview` | Count | `ReviewScope` | Tables a human approved. `ReviewScope` = `Table` (single-table review) \| `Bulk` (bulk approve). Only status *transitions* count, so an idempotent re-approve does not inflate the rate. |
| `TablesRejectedByReview` | Count | `ReviewScope` | Tables a human rejected, same scoping. |

**Provisioning metrics** (emitted from `connectors/glue_connection_provisioner.py`, Lambda — live):

| Metric | Unit | Dimensions | Description |
|---|---|---|---|
| `GlueApiThrottles` | Count | `Api` | **Terminal** Glue / Lake Formation throttles, i.e. those that survived botocore's `standard` retry mode. Retried-then-succeeded throttles are invisible here. |

**Enrichment metrics** (emitted from `pipeline/enrichment_handler.py` /
`enrichment/table_enricher.py` — **ECS task, log-only**, see the EMF note above):

| Metric | Unit | Dimensions | Description |
|---|---|---|---|
| `EnrichmentJobDurationMs` | Milliseconds | `Engine`, `NamespaceId` | Total enrichment job wall-clock time |
| `TablesEnriched` | Count | `Engine`, `NamespaceId` | Tables successfully enriched |
| `TablesFailed` | Count | `Engine`, `NamespaceId` | Tables that failed enrichment |
| `TablesSkippedUnchanged` | Count | `Engine`, `NamespaceId` | Tables skipped (no changes) |
| `BedrockInvocationLatencyMs` | Milliseconds | `Engine`, `NamespaceId`, `Stage` | Per-Bedrock-call latency |
| `BedrockInputTokens` | Count | `Engine`, `NamespaceId`, `Stage` | Input tokens per Bedrock call |
| `BedrockOutputTokens` | Count | `Engine`, `NamespaceId`, `Stage` | Output tokens per Bedrock call |
| `BedrockInvocationErrors` | Count | `Engine`, `NamespaceId`, `Stage`, `ErrorType` | Bedrock call failures |
| `EnrichmentEstimatedCostUsd` | None | `Engine`, `NamespaceId` | Estimated cost per scan job |

**Dimensions:**

- `Engine`: `postgresql`, `mysql`, `redshift`, `sqlserver`, or `glue` (`oracle`/`snowflake` are implemented but withheld — see Withheld engines)
- `NamespaceId`: the namespace owning the data source
- `Stage`: `Pass1` (per-table LLM enrichment) or `Pass2` (cross-table FK inference)
- `ErrorType`: `Throttling`, `Validation`, `Guardrail`, or `Other`
- `SourceType`: `DATABASE`, `GLUE_DATABASE`, `JDBC_DATABASE`, … (the source's declared type)
- `Result`: `Success` | `Failure`
- `Operation`: `Create` | `Update`
- `ReviewScope`: `Table` | `Bulk`
- `Api`: the throttled AWS API name (`CreateConnection`, `GrantPermissions`, `RegisterResource`, `CreateCatalog`)

#### Observability — structured scan dashboard

The CloudWatch dashboard `<prefix>-sources-structured-scan` (defined in
`infra/lib/stacks/services/sources-stack.ts`) charts the scan and enrichment
pipeline. Widgets use `SEARCH()` so a new source subtype appears without a stack
change.

| Widget | Source |
|---|---|
| Scan duration p50/p90/p99, stage durations | `COA/Sources` (live) |
| Tables discovered | `COA/Sources` (live) |
| Connection validation Success/Failure | `COA/Sources` (live) |
| Catalog asset writes Create/Update | `COA/Sources` (live) |
| Review approved/rejected + acceptance rate | `COA/Sources` (live) |
| Glue API throttles | `COA/Sources` (live) |
| Scan state-machine outcomes, execution time, failed drill-down | `AWS/States` (AWS-native) |
| Bedrock latency, errors, token usage | `AWS/Bedrock` (AWS-native) |
| Overlay match rate | **dark — placeholder text, pending #114** |

Two caveats are stated on the dashboard itself rather than hidden here:

- **`AWS/Bedrock` is account-wide per `ModelId`.** Sources enrichment and
  ontology induction invoke the same Haiku model, so those series are the sum of
  both pipelines. Per-pipeline LLM attribution needs `PutMetricData` from the
  enrichment ECS task, which it does not have.
- **Overlay match rate has no metric yet.** #114 (SageMaker Catalog Overlay, M2)
  is unbuilt, so that slot is an honest text placeholder instead of a query that
  would render as a permanently empty graph.

#### Engine support

Discovery is implemented per engine via a **dialect** (`connectors/dialects.py`).
Each dialect supplies the driver connection and the catalog SQL; `JdbcConnector`
owns the flow (filtering, `Table` assembly). Supported engines:

| Engine | Default port | Driver (pip) | Notes |
|---|---|---|---|
| PostgreSQL | 5432 | `pg8000` | ANSI `information_schema` |
| Redshift | 5432 | `pg8000` | PostgreSQL wire protocol |
| MySQL | 3306 | `pymysql` | FK via `KEY_COLUMN_USAGE` (no `constraint_column_usage`) |
| SQL Server | 1433 | `python-tds` (`pytds`) | pure-Python, no ODBC |
| Snowflake | 443 | `snowflake-connector-python` | **withheld — not enabled** (see below); **requires `warehouse`** (and optional `role`) in config for `INFORMATION_SCHEMA`; account derived from host |
| Oracle | 1521 | `oracledb` (thin mode) | **withheld — not enabled** (see below); `ALL_*` catalog views + `:n` binds; no Oracle client needed |

**Withheld engines:** Snowflake and Oracle are implemented in full but listed in
`coa_common.constants.PREVIEW_DATABASE_ENGINES`, which filters them out of the
enabled `_DIALECTS` registry. `get_dialect` returns `None` for them, so
`test_connection` and `discover_metadata` reject them as unsupported. They need
more validation before we support them. To enable one, delete its token from
`PREVIEW_DATABASE_ENGINES` (and from the mirror in
`libs/ts-shared/src/constants.ts` so the web app offers it) — nothing else changes.

`POSTGRESQL`/`REDSHIFT` also get a direct query path at serve time
(`queryEngine=JDBC`); all other engines are queried via the Athena federated
catalog (`queryEngine=ATHENA`) but are fully discovered here.

**Dependencies:** `pg8000`, `pymysql`, `oracledb`, `python-tds` are pure/near-pure
Python (no native build deps). `snowflake-connector-python` is heavier (pulls
`pyarrow`/`cryptography`) and adds to the Lambda bundle. All drivers are
lazy-imported per dialect, so a missing optional driver only fails that engine.

**Snowflake configuration:** Snowflake needs a `warehouse` (and optional `role`)
on the `jdbcConfiguration`, e.g.:

```json
{ "engine": "SNOWFLAKE", "host": "acme.snowflakecomputing.com", "databaseName": "ANALYTICS",
  "warehouse": "WH_XS", "role": "REPORTER", "credentialSecretArn": "arn:aws:secretsmanager:..." }
```

**Adding a new engine:** add a `Dialect` subclass — extend
`InformationSchemaDialect` (override only `connect`, and `fetch_constraints` if the
standard FK view is absent) for ANSI-`information_schema` engines, or subclass
`Dialect` directly (like `OracleDialect`) for non-standard catalogs — then add it
to the `_DIALECTS` registry. The Athena federation provisioner is engine-agnostic
(it sets the Glue `ConnectionType` from the configured engine).

### Document Ingestion Pipeline (`sources-doc-ingestion-pipeline`)

Step Functions state machine orchestrating document preprocessing and KG build:

```
DocIngestionQueue (SQS)
  → SourcesDocIngestionTriggerFn (Lambda)
    → sources-doc-ingestion-pipeline (Step Functions)
        1. DocUpdateStatusIngesting   — mark source SCANNING
        2. SourcesPreProcessing (Lambda) — extract text from PDF/DOCX, stage to S3
        3. SourcesKGBuild (ECS Fargate)  — build knowledge graph, index to Neptune + OpenSearch
        4. DocUpdateStatusCompleted   — mark source COMPLETED
        (on error) → DocUpdateStatusFailed → Fail
```

### Document Deletion Pipeline (`sources-doc-deletion-pipeline`)

Step Functions state machine for comprehensive source cleanup:

```
sources-doc-deletion-pipeline (Step Functions)
    1. DeleteS3Objects    — remove raw and staged files from S3
    2. CleanupKG (Lambda) — remove Neptune graph nodes and OpenSearch vectors
    3. DeleteDDBRecord    — remove sources-table item
    4. DocUpdateStatusDeleted — mark source DELETED
    (on error) → DocUpdateStatusDeleteFailed → Fail
```

### S3 Bucket Structure (`sources-data`)

| Prefix | Contents | Lifecycle |
|--------|----------|-----------|
| `{namespaceId}/raw/{uploadId}/` | Raw uploaded files | No expiry |
| `{namespaceId}/staging/` | Pre-processed text files | No expiry |
| `{namespaceId}/extracted/` | Intermediate extraction artifacts | 30-day expiry |

## CDK Context Variables

These context variables control which Docker images are deployed for pipeline tasks. When not set, CDK builds images locally from source (development only).

| Context key | Description |
|-------------|-------------|
| `sources_db_enrichment_image_uri` | ECR image URI for the database enrichment ECS task |
| `sources_preprocessing_image_uri` | ECR image URI for the document preprocessing Lambda |
| `sources_kg_build_image_uri` | ECR image URI for the document KG build ECS task |
| `ecr_repository_arn` | ARN of the shared ECR repository |
| `ecr_repository_name` | Name of the shared ECR repository |

Example (CI deploy):
```bash
cdk deploy coa-dev-sources \
  --context sources_db_enrichment_image_uri=123456789012.dkr.ecr.us-east-1.amazonaws.com/coa:sources-db-enrichment-abc1234 \
  --context sources_preprocessing_image_uri=123456789012.dkr.ecr.us-east-1.amazonaws.com/coa:sources-preprocessing-abc1234 \
  --context sources_kg_build_image_uri=123456789012.dkr.ecr.us-east-1.amazonaws.com/coa:sources-kg-build-abc1234 \
  --context ecr_repository_arn=arn:aws:ecr:us-east-1:123456789012:repository/coa \
  --context ecr_repository_name=coa
```



## Migration

The `sources-table` is populated via a one-time backfill script (run locally, not committed):

```bash
python scripts/backfill_sources.py \
  --doc-sources-table coa-dev-doc-sources \
  --datasources-table coa-dev-datasource-connectors \
  --sources-table coa-dev-sources \
  --region us-east-1
```

### Migrating off the SAR Athena multiplexer connector

Earlier versions deployed an Athena federation connector as a SAR-sourced nested
CloudFormation stack (`AthenaJdbcConnector`, version `2022.4.1`) plus per-source
`Type=LAMBDA` Athena DataCatalogs. That architecture is **removed** — JDBC sources
now use [managed Glue federated catalogs](#athena-queryability) (Glue Connection
with `MANAGED_CONNECTION=true`, registered with Lake Formation, plus a
`FederatedCatalog`) with no connector Lambda.

Removing the nested stack from the CDK code does **not** delete the already-deployed
SAR stack — CloudFormation orphans it. After upgrading, clean it up manually:

```bash
# 1. Find the orphaned SAR connector nested stack
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName, 'AthenaJdbcConnector')].StackName"

# 2. Delete it (this removes the connector Lambda it created)
aws cloudformation delete-stack --stack-name <orphaned-stack-name>

# 3. Verify the connector Lambda is gone
aws lambda list-functions \
  --query "Functions[?contains(FunctionName, 'AthenaJdbcConnector')].FunctionName"
```

Re-scan each JDBC source to provision its managed federated catalog, then update
saved queries / BI tools to the 4-part `AwsDataCatalog` syntax (see
[Querying via Athena](#querying-via-athena)).

## Troubleshooting

### General

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `409 Conflict` on review or metadata edit | Source is in a transient state (`SCANNING`, `ENRICHING`, `APPROVING`, `REJECTING`, `DELETING`) | Wait for the pipeline to complete or check for a stuck execution in Step Functions |
| `POST /sources` returns 400 with no useful message | Request body failed Pydantic validation (`CreateSourceInput`) | Check that `sourceType`, `sourceSubType`, and type-specific fields are all present |
| Scan stuck in `SCANNING` indefinitely | Step Functions execution failed silently or the trigger Lambda dropped the SQS message | Check the DLQ for the scan queue; inspect the Step Functions execution history |
| Source counter drift (namespace shows wrong count) | `adjust_namespace_source_count` swallowed a DDB error | Re-count manually via `GET /sources?sourceType=DATABASE` and fix the namespace record |
| Bulk approve returns 202 but status goes to `APPROVAL_FAILED` | Worker Lambda timed out or hit a DataZone throttle | Check worker CloudWatch logs; increase `BULK_REVIEW_PARALLELISM` if throttle-related; retry the POST |

### Database Sources

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Discovery succeeds but enrichment fails | Bedrock throttling or model access not granted | Check `BedrockThrottleCount` metric; verify Bedrock model access in the account |
| `SCAN_FAILED` immediately after creation | JDBC credentials in Secrets Manager are invalid or unreachable | Verify the secret at `credentialSecretArn` contains `username`/`password`; check SG rules |
| Tables discovered but no columns | `information_schema.columns` query returned empty (permissions issue on source DB) | Grant SELECT on `information_schema` to the connection user |
| Federation step fails with `Insufficient Lake Formation permission(s)` | Federation provisioner role is not registered as LF data-lake admin | Run the LF admin bootstrap; see [Prerequisites](#prerequisite--lake-formation-data-lake-admin) |
| Delete fails with 500 on federated resource cleanup | `FEDERATION_PROVISIONER_ROLE_ARN` is unset or the role lacks LF admin | Set the env var; verify LF admin registration; retry the delete |

### Document Sources

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Preprocessing fails with "unsupported content type" | Uploaded file has a MIME type not in `SUPPORTED_UPLOAD_CONTENT_TYPES` | Check the file extension/type; supported: PDF, DOCX, TXT, MD, HTML |
| KG build ECS task exits with OOM | Document too large for the task memory | Increase task memory in CDK or split the document |
| Delete leaves orphaned Neptune nodes | Deletion pipeline's `CleanupKG` step failed | Check the Step Functions execution; retry the delete |
