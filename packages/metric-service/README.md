# Metric Service

- **Owner**: ancavi
- **LLD**: `.kiro/design_docs/md/Metric Onboarding Service - LLD.md`
- **Status**: Active

## Overview

The Metric Service manages the lifecycle of governed metrics: create, read,
update, delete, bulk delete, OSI import/export, and **validation**. Each metric is stored
as an `owl:Class` in the namespace's published ontology graph (Neptune) and
embedded in OpenSearch for semantic search (Tier 0 routing).

Metrics carry one or more dialect-specific SQL expressions plus metadata
(`dataSourceId`, `sourceTable`, `ontologyConcepts`). Before a metric is
trusted for query resolution, it is run through a 6-check validation system.

## Getting Started

### Prerequisites

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (workspace-level package manager)
- AWS credentials configured (for integration tests)

### Install

From the repo root:

```bash
uv sync
```

### Run Tests

```bash
# Unit tests
uv run pytest packages/metric-service/tests/unit -m unit -v --tb=short
```

> **Note:** The `npx nx test metric-service` target is not yet active — it
> currently prints a `TODO` placeholder instead of running tests. Use the
> direct `pytest` command above until the Nx target is enabled.

### Lint & Format

```bash
npx nx lint metric-service     # ruff check + ruff format --check + mypy
npx nx format metric-service   # auto-format with ruff
```

### Test Structure

| Test file | Covers |
|-----------|--------|
| `test_create_metric.py` | Create flow, validation on create, conflict detection |
| `test_update_delete_metrics.py` | Update (full replace) and single delete |
| `test_bulk_delete.py` | Bulk delete by names list and by filter |
| `test_validator.py` | All 6 validation checks in isolation |
| `test_validate_metric.py` | Validate endpoint (dry-run validation) |
| `test_osi_parser.py` | OSI YAML parsing and serialization |
| `test_import_osi.py` | Synchronous import path |
| `test_async_import.py` | Async import (chunked SQS worker) |
| `test_export_osi.py` | Export to OSI YAML (inline + S3) |
| `test_s3_import_export.py` | Pre-signed URL generation, S3 upload flow |
| `test_neptune_client.py` | SPARQL CRUD operations |
| `test_opensearch_client.py` | Vector embedding operations |
| `test_dataset_resolver.py` | OSI dataset → data source resolution |
| `test_metadata_reconciler.py` | Additive metadata merge logic |
| `test_get_list_metrics.py` | Get/list endpoints, pagination, filters |

#### Integration tests (`tests/integ/`)

End-to-end tests against a **deployed** environment (`@pytest.mark.integ`), run by the CI
`integ-test` job and locally with `uv run pytest packages/metric-service -m integ`.

| Test file | Covers |
|-----------|--------|
| `test_metric_api.py` | Full CRUDL lifecycle + error cases (404 / 400 / 409) |
| `test_metric_complex_validation.py` | Multi-dialect + full `aiContext` round-trip; 4XX validation; PUT full-replacement |
| `test_metric_validate_endpoint.py` | `/validate` dry-run — 200 + `warnings` list, does not persist |
| `test_metric_osi_import_export.py` | OSI import (inline / pre-signed upload-url / async job) + export (inline / S3) |
| `test_metric_bulk_delete_list.py` | Bulk delete (by names / filter); list filtering + nextToken pagination |
| `test_metric_authz_isolation.py` | Auth (401 missing / 403 invalid token) + namespace isolation |

## Package Structure

```text
packages/metric-service/
├── src/coa_metrics/
│   ├── api/
│   │   ├── metric_api_handler.py       # Lambda entry — routes all endpoints
│   │   ├── create_metric.py            # POST /metrics
│   │   ├── get_metric.py              # GET /metrics/{name}
│   │   ├── list_metrics.py            # GET /metrics
│   │   ├── update_metric.py           # PUT /metrics/{name}
│   │   ├── delete_metric.py           # DELETE /metrics/{name}
│   │   ├── bulk_delete_metrics.py     # POST /bulk-delete-metrics
│   │   ├── validate_metric.py         # POST /metrics/validate
│   │   ├── import_osi.py             # POST /import-osi (sync or async dispatch)
│   │   ├── import_worker.py          # SQS worker Lambda (async chunked import)
│   │   ├── import_job_store.py       # DynamoDB job tracking
│   │   ├── get_import_job.py         # GET /import-jobs/{jobId}
│   │   ├── get_osi_upload_url.py     # POST /import-osi/upload-url
│   │   └── export_osi.py            # GET /export-osi
│   ├── validator.py                   # 6-check validation engine
│   ├── neptune_client.py             # SPARQL CRUD (SigV4 + httpx)
│   ├── opensearch_client.py          # Vector embeddings (Bedrock Cohere Embed v4 + AOSS)
│   ├── osi_parser.py                 # OSI v1.0 YAML parse/serialize
│   ├── dataset_resolver.py           # Resolve OSI datasets → data sources
│   ├── metadata_reconciler.py        # Additive metadata merge
│   └── constants.py                  # SqlDialect re-exports
├── examples/
│   └── sample-osi-import.yaml        # Reference OSI file
├── tests/
│   ├── unit/                          # All unit tests
│   └── integ/                         # Integration tests (placeholder)
├── pyproject.toml
├── project.json
└── requirements.txt
```

## Architecture

```
API Gateway → Lambda Authorizer (Cedar RBAC)
  │
  ▼
MetricApiFn (single Lambda, route-based dispatch)
  ├─ Neptune (SPARQL)    — metric storage (named graph per namespace)
  ├─ OpenSearch (AOSS)   — vector embeddings for Tier 0 routing
  ├─ EventBridge         — lifecycle events (metric.published)
  ├─ S3                  — OSI file staging (large imports/exports)
  └─ SQS + Worker Lambda — async import for large OSI files (>30 metrics)
```

Each metric is an `owl:Class` that is `rdfs:subClassOf :GovernedMetric` in the
namespace's ontology named graph, using the same graph URI scheme as the
ontology-engine: `{NDB_GRAPH_URI_BASE}/{namespace}/{encoded ontology_id}`.
The ontology_id for governed metrics is the shared constant
`GOVERNED_METRICS_ONTOLOGY_ID` (`urn:coa:vocab#GovernedMetrics`).

Metrics are linked to ontology classes via `ov:governedMetricFor`, an
`owl:ObjectProperty` that creates IRI→IRI edges traversable in both
directions via SPARQL.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/namespaces/{ns}/metrics` | Create a new metric |
| GET | `/namespaces/{ns}/metrics` | List metrics (paginated, filterable) |
| GET | `/namespaces/{ns}/metrics/{name}` | Get a single metric |
| PUT | `/namespaces/{ns}/metrics/{name}` | Update a metric (full replacement) |
| DELETE | `/namespaces/{ns}/metrics/{name}` | Delete a metric |
| POST | `/namespaces/{ns}/bulk-delete-metrics` | Delete multiple metrics by `names` list or `filter` (`dataSourceId`/`sourceTable`/`namePrefix`); returns `deleted` count and `notFound` list |
| POST | `/namespaces/{ns}/metrics/validate` | Dry-run validation (Checks 1–6) without persisting |
| POST | `/namespaces/{ns}/import-osi` | Import metrics from OSI v1.0 YAML (`s3Key` from upload-url **OR** `content` inline; the two are mutually exclusive). Returns `200` (sync) or `202` with `jobId` (async, >30 metrics) |
| POST | `/namespaces/{ns}/import-osi/upload-url` | Get a pre-signed S3 PUT URL for large OSI files |
| GET | `/namespaces/{ns}/import-jobs/{jobId}` | Poll async import status |
| GET | `/namespaces/{ns}/export-osi` | Export metrics as OSI v1.0 YAML (`format=inline` or `format=s3`) |

### Authentication & Authorization

All endpoints require a valid Bearer token (Cognito/OIDC). Cedar actions:
- Read operations (`GET`): `readMetric`
- Write operations (`POST`, `PUT`, `DELETE`): `manageMetric`

### Query Parameters (List)

| Parameter | Type | Description |
|-----------|------|-------------|
| `dataSourceId` | string | Filter by data source |
| `sourceTable` | string | Filter by source table |
| `maxResults` | integer (1–200) | Page size (default: 50) |
| `nextToken` | string | Pagination cursor |

## OSI Import/Export

Supports the [Open Semantic Interchange (OSI)](https://github.com/open-semantic-interchange/OSI) v1.0 format for metric interchange across platforms.

### Import Flow

```
Client                              API                         S3              SQS Worker
  │                                  │                          │                  │
  ├─ POST /import-osi/upload-url ───►│                          │                  │
  │◄─── { uploadUrl, s3Key } ────────│                          │                  │
  │                                  │                          │                  │
  ├─ PUT <uploadUrl> (YAML body) ────┼─────────────────────────►│                  │
  │                                  │                          │                  │
  ├─ POST /import-osi { s3Key } ────►│                          │                  │
  │                                  ├── read YAML from S3 ────►│                  │
  │                                  ├── parse, count metrics   │                  │
  │                                  │                          │                  │
  │    (≤30 metrics: sync)           │                          │                  │
  │◄─── 200 { metrics, warnings } ──│                          │                  │
  │                                  │                          │                  │
  │    (>30 metrics: async)          │                          │                  │
  │◄─── 202 { jobId, status } ──────│                          │                  │
  │                                  ├── enqueue chunk msg ─────┼─────────────────►│
  │                                  │                          │                  ├── process chunk
  │                                  │                          │                  ├── send continuation
  │                                  │                          │                  └── mark COMPLETED
  ├─ GET /import-jobs/{jobId} ──────►│                          │                  │
  │◄─── { status, progress } ───────│                          │                  │
```

### Async Chunking

Large imports are split into chunks of 50 metrics. The worker processes one chunk per SQS message, then sends a continuation message for the next chunk. Progress is tracked atomically in DynamoDB (ADD expressions for counters, list_append for errors).

### Dialect Mapping

| OSI Dialect | Internal Dialect |
|-------------|-----------------|
| `ANSI_SQL` | `POSTGRESQL` |
| `SNOWFLAKE` | `SNOWFLAKE` |
| `DATABRICKS` | `DATABRICKS` |

### Dataset Resolution

On import, OSI `datasets` entries must resolve to onboarded data sources. Resolution strategy:
1. If the dataset has a `data_source_id` in `custom_extensions`, verify it exists
2. Otherwise, attempt name-based matching against known sources
3. ALL datasets must resolve — unresolved datasets block the import

### Metadata Reconciliation

When importing over existing metrics, metadata is merged additively with priority:
`STEWARD > OSI_IMPORTED > AI_GENERATED`. Steward edits are never overwritten.
Synonyms are unioned (case-insensitive dedup); descriptions only update if the current source priority is lower.

### Sample

See [`examples/sample-osi-import.yaml`](examples/sample-osi-import.yaml) for a complete OSI v1.0 example with multiple metrics, datasets, and `ai_context` blocks.

## Metric Validation

### Philosophy: "Author early, validate continuously"

Validation distinguishes two severities:

- **ERROR** — blocks metric creation. Only **Check 1 (SQL syntax)** is an
  error. A metric with a syntax error in any dialect cannot be created.
- **WARNING** — non-blocking. Checks 2–6 surface issues (missing tables,
  unknown columns, type mismatches, unlinked ontology concepts) but the
  metric is still published.

### The 6 Checks

| # | Check | Severity | What it verifies |
|---|-------|----------|------------------|
| 1 | SQL syntax | **ERROR** | Each dialect expression parses with `sqlglot` for the declared dialect. |
| 2 | Table references | WARNING | Every table referenced in the SQL exists for the metric's `dataSourceId`. |
| 3 | Column references | WARNING | Referenced columns exist in their tables (and types are resolvable). |
| 4 | Dimension columns | WARNING | Columns used in `GROUP BY` exist in the referenced tables. |
| 5 | Filter compatibility | WARNING | Columns in `WHERE` exist and their type is compatible with the operator. |
| 6 | Ontology linkage | WARNING | Each `ontologyConcepts` entry resolves to an `owl:Class` in the namespace graph. |

### Graceful Degradation

Validation never crashes metric creation. Checks are skipped when their backing dependency is unavailable:

- **Checks 2–5** are skipped when `DATA_SOURCES_TABLE` is unset or DynamoDB is unreachable.
- **Check 6** is skipped when Neptune is unreachable.
- If `sqlglot` cannot be imported, soft validation is skipped entirely.

Ontology concept identifiers are validated against a strict CURIE/IRI
allowlist before any SPARQL query is constructed (defense-in-depth against
SPARQL injection).

### Validate Endpoint

`POST /namespaces/{ns}/metrics/validate` runs all 6 checks against a metric definition supplied in the request body **without persisting it** (dry-run). Useful for "validate before create" UX workflows and re-validation after schema/ontology changes.

**Request body** matches the create metric schema (name, description, expression, dataSourceId, sourceTable, ontologyConcepts).

**Response `200`:**
```json
{
  "warnings": [
    { "field": "column_reference", "message": "Column 'discount_pct' not found in table 'orders'", "severity": "INFO" }
  ]
}
```

### Validation on Create / Update

`POST /metrics` and `PUT /metrics/{name}` run the same checks implicitly.
Check 1 (syntax) is enforced as a hard error (400); Checks 2–6 are returned as
non-blocking `warnings` on the `201`/`200` response body.

## Environment Variables

### MetricApiFn Lambda

| Variable | Required | Description |
|----------|----------|-------------|
| `NEPTUNE_ENDPOINT` | Yes | Neptune SPARQL endpoint (SigV4 auth) |
| `NDB_GRAPH_URI_BASE` | No | Base URL for per-namespace graph URIs. Must match ontology-engine config. Default: `https://ontology-workbench.local` |
| `OPENSEARCH_ENDPOINT` | Yes | OpenSearch Serverless collection endpoint |
| `DATA_SOURCES_TABLE` | No | DynamoDB table for source/table/column metadata (Checks 2–5). Skipped when unset |
| `OSI_BUCKET_NAME` | Yes | S3 bucket for OSI file staging (upload + async read) |
| `IMPORT_QUEUE_URL` | Yes | SQS URL for async import messages |
| `IMPORT_JOBS_TABLE` | Yes | DynamoDB table for import job tracking |
| `EVENTBRIDGE_BUS_NAME` | No | EventBridge bus name (default: `default`) |
| `BEDROCK_EMBED_MODEL_ID` | No | Embedding model. Takes precedence over `BEDROCK_MODEL_ID` (default: `DEFAULT_EMBED_MODEL_ID` — `us.cohere.embed-v4:0`) |
| `BEDROCK_MODEL_ID` | No | Embedding model, legacy fallback name (default: `DEFAULT_EMBED_MODEL_ID` — `us.cohere.embed-v4:0`) |
| `OSS_INDEX_PREFIX` | No | OpenSearch index name prefix — shared with ontology-engine (default: `ontology-workbench-embeddings`) |
| `OSS_DIMENSIONS` | No | Embedding vector dimensions (default: `1024`) |
| `ALLOWED_ORIGIN` | No | CORS allowed origin |
| `LOG_LEVEL` | No | Log level (default: `INFO`) |

### Import Worker Lambda

| Variable | Required | Description |
|----------|----------|-------------|
| `NEPTUNE_ENDPOINT` | Yes | Neptune SPARQL endpoint |
| `NDB_GRAPH_URI_BASE` | No | Base URL for per-namespace graph URIs (default: `https://ontology-workbench.local`) |
| `OSI_BUCKET_NAME` | Yes | S3 bucket for reading OSI files |
| `IMPORT_QUEUE_URL` | Yes | SQS URL for continuation messages |
| `IMPORT_JOBS_TABLE` | Yes | DynamoDB table for job progress tracking |
| `DATA_SOURCES_TABLE` | No | For dataset resolution validation |

## DynamoDB Schema

### Import Jobs Table (`metric-import-jobs`)

| Key | Pattern | Description |
|-----|---------|-------------|
| PK | `NS#{namespaceId}` | Namespace partition |
| SK | `IMPORT#{jobId}` | Import job record |

Attributes: `jobId`, `status` (`IN_PROGRESS` / `COMPLETED` / `FAILED`), `s3Key`, `metricsTotal`, `metricsProcessed`, `metricsCreated`, `metricsUpdated`, `errors[]`, `warnings[]`, `createdAt`, `updatedAt`.

Progress counters use DynamoDB `ADD` expressions for safe concurrent chunk updates.

## Storage

### Neptune (SPARQL)

Each metric is stored as RDF triples in the namespace's ontology named graph
(`{NDB_GRAPH_URI_BASE}/{namespace}/{encoded GOVERNED_METRICS_ONTOLOGY_ID}`),
the same graph URI scheme used by the ontology-engine for classes/properties.

The `MetricNeptuneClient` provides:
- `create_metric` — INSERT DATA into the named graph
- `get_metric` — SELECT query by metric IRI
- `list_metrics` — SELECT with optional filters, OFFSET/LIMIT pagination
- `update_metric` — DELETE WHERE + INSERT DATA (full replacement)
- `delete_metric` — DELETE WHERE for all triples with the metric subject
- `bulk_delete_metrics` — single DELETE WHERE for multiple metrics
- `resolve_class_uris` — batch-resolves class names/labels to full URIs via
  SPARQL VALUES query across all namespace graphs. The create/update API calls
  this automatically so users can provide friendly class names (e.g. `"Policy"`)
  which are resolved to full IRIs at write time.

`ontologyConcepts` are stored as `owl:ObjectProperty` edges
(`ov:governedMetricFor`) from the metric IRI to the class IRI — traversable
in both directions:
```sparql
# Metric → classes
SELECT ?class WHERE { <metric-uri> ov:governedMetricFor ?class }
# Class → metrics (reverse)
SELECT ?metric WHERE { ?metric ov:governedMetricFor <class-uri> }
```

Neptune auth uses SigV4 via `botocore.auth` + `httpx`.

### OpenSearch (AOSS)

Metrics are embedded using Bedrock Cohere Embed v4 (1024-dim) and stored in the
shared ontology vector index (same index as ontology-engine classes/properties),
using `ontology_vector_index_name(OSS_INDEX_PREFIX, namespace)`.

The model is **not** configured here — it comes from
`coa_common.constants.DEFAULT_EMBED_MODEL_ID`, the single source of truth shared
by every embedding producer and consumer. Every writer to this index must use the
same model or the vectors are cross-model incomparable and retrieval silently
degrades to noise. Note that Cohere Embed v4 and the previously-used Titan Text
Embeddings V2 are both 1024-dim, so a mismatch does not fail loudly — it just
returns bad neighbours.

Document schema conforms to the ontology-engine's mapping:
```json
{
  "entity_uri": "urn:coa:{ns}:metric:{name}",
  "ontology_id": "urn:coa:vocab#GovernedMetrics",
  "entity_type": "metric",
  "embedding_type": "lexical",
  "model_id": "us.cohere.embed-v4:0",
  "namespace": "{ns}",
  "text": "name description synonyms instructions examples",
  "embedding": [1024-dim vector]
}
```

Filtered by `entity_type: "metric"` for metric-only searches. Used by the
context-manager's Tier 0 routing for semantic metric discovery.

Auth uses SigV4 (`aoss` service) via `opensearch-py` + `requests_aws4auth`.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Create returns 409 | Metric with that name already exists in the namespace | Use PUT to update, or delete first |
| Validation warnings but metric still created | Expected — only Check 1 (syntax) blocks creation; Checks 2–6 are advisory | Review warnings and fix at your pace |
| Import returns 202 but job stays `IN_PROGRESS` | Worker Lambda errored or continuation message was lost | Check the import worker CloudWatch logs; check the DLQ |
| Import job `FAILED` immediately | OSI YAML parse error or dataset resolution failure | Check the `errors` field on the job; verify datasets reference valid `data_source_id` values |
| Neptune timeout on list/get | Large namespace or Neptune instance under-provisioned | Check Neptune CloudWatch metrics; consider increasing instance size |
| OpenSearch embed fails (metric still created) | AOSS collection unreachable or Bedrock throttled | Best-effort — metric exists in Neptune; re-embed by updating the metric |
| `sqlglot` import error in Lambda | Missing dependency in Lambda package | Verify `sqlglot>=25.0` is in the Lambda bundle |
| Export returns empty `metrics: []` | No metrics in the namespace, or Neptune graph name mismatch | Verify metrics exist via GET /metrics; check `NEPTUNE_ENDPOINT` |
