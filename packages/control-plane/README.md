# Control Plane

- **Owner**: TBD
- **Status**: Active

## Overview

The Control Plane manages namespace lifecycle, role-based access control, and DataZone project orchestration. It provides CRUD APIs for namespaces, roles, and grants — each backed by a Lambda handler that routes through API Gateway.

Components:

1. **Namespace API** (`namespace/`) — Create, list, get, update, and delete namespaces. Each namespace maps 1:1 to a DataZone project.
2. **Roles API** (`roles/`) — List platform-level and namespace-scoped role definitions.
3. **Grants API** (`grants/`) — Create, list, and delete principal→role→resource grants (RBAC).
4. **Authorization** (`authorization/`) — Lambda authorizer for API Gateway + DynamoDB Streams handler for grant propagation.

## Getting Started

### Prerequisites

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (workspace-level package manager)
- AWS credentials configured (for integration tests or local DynamoDB calls)

### Install

From the repo root:

```bash
uv sync
```

This installs the control-plane and its workspace dependencies (`coa-common`, `coa-control-plane-server`).

### Run Tests

```bash
# Unit tests (80% coverage threshold enforced)
npx nx test control-plane

# Or directly:
uv run pytest packages/control-plane -m unit -v --tb=short \
  --cov=packages/control-plane --cov-fail-under=80
```

### Lint & Format

```bash
npx nx lint control-plane    # ruff check + ruff format --check + mypy
npx nx format control-plane  # auto-format with ruff
```

### Test Structure

```
tests/
├── unit/          # Pure logic tests, mocked AWS calls
│   ├── test_authz.py                  # Cedar policy evaluation
│   ├── test_authorizer_handler.py     # Lambda authorizer flow
│   ├── test_create_namespace.py       # Namespace creation orchestration
│   ├── test_platform_grants.py        # Platform grant CRUD
│   └── ...
└── integ/         # Integration tests (require live AWS resources)
    ├── conftest.py                              # Multi-persona fixtures (5 Cognito users)
    ├── test_namespace_crud_integ.py             # Namespace lifecycle (create/list/get/update/archive/delete)
    ├── test_grants_crud_integ.py                # Grant CRUD (namespace + platform, data overrides)
    ├── test_roles_integ.py                      # Role listing APIs
    ├── test_authz_namespace_isolation_integ.py  # Cross-namespace isolation + role-scoped access
    ├── test_authz_authentication_integ.py       # Token validation (valid/invalid/expired/missing)
    ├── test_authz_platform_grants_integ.py      # Platform grant admin-only enforcement
    └── test_authz_archived_namespace_integ.py   # Archived namespace mutation guard
```

Tests use `pytest` markers: `@pytest.mark.unit` and `@pytest.mark.integ`. The CI pipeline runs only `unit` by default.

#### Running Integration Tests

```bash
uv run pytest packages/control-plane -m integ -v
```

Requires a deployed stack. The integ suite provisions 5 Cognito test personas (owner, analyst, steward, viewer, no-access), grants them appropriate roles, and exercises the full JWT → authorizer → Cedar evaluation chain against the live API.

## Architecture

```
API Gateway → Lambda Authorizer (Cognito JWT + RBAC check)
  ├─ POST   /namespaces              → create_handler
  ├─ GET    /namespaces              → list_handler
  ├─ GET    /namespaces/{id}         → get_handler
  ├─ PATCH  /namespaces/{id}         → update_handler
  ├─ DELETE /namespaces/{id}         → delete_handler
  ├─ GET    /roles                   → list_platform_roles_handler
  ├─ GET    /namespaces/{id}/roles   → namespace_roles_handler
  ├─ POST   /namespaces/{id}/grants  → grants create_handler
  ├─ GET    /namespaces/{id}/grants  → grants list_handler
  ├─ DELETE /namespaces/{id}/grants/{grantId} → grants delete_handler
  ├─ POST   /grants                  → create_platform_grant_handler
  ├─ GET    /grants                  → list_platform_grants_handler
  └─ DELETE /grants/{grantId}        → delete_platform_grant_handler
```

## Platform Grants API

Platform grants bind a principal (User or Group) to a **PLATFORM-scoped** role —
`platform-admin` or `platform-viewer` — that applies across **all** namespaces.
They are distinct from namespace grants (`/namespaces/{id}/grants`), which scope a
role to a single namespace. Internally a platform grant is stored under the
synthetic `Platform::GLOBAL` resource (`resourceId="GLOBAL"`), reusing the same
`ResourceRoleMappings` table and GSIs as namespace grants.

| Method | Path | Cedar action | Who may call |
|---|---|---|---|
| `POST` | `/grants` | `grantPermissions` (`resource_id="*"`) | `platform-admin` only |
| `GET` | `/grants` | `viewNamespace` | `platform-admin`, `platform-viewer` |
| `DELETE` | `/grants/{grantId}` | `grantPermissions` (`resource_id="*"`) | `platform-admin` only |

All endpoints require a valid bearer token (Cognito/OIDC). See
[authz-access-matrix.md](docs/authz-access-matrix.md) for the full authorization model.

### `POST /grants` — create a platform grant

```http
POST /grants
Content-Type: application/json

{ "principalType": "User", "principalId": "alice@example.com", "role": "platform-admin" }
```

Returns `201` with the created `PlatformGrantSummary` (`grantId`, `principalType`,
`principalId`, `role`, `grantedBy`, `grantedAt`). The `role` must be an existing
platform role; `principalId` is capped at 256 chars and may not contain the
reserved key delimiters (`#`, `|`, `::`). Returns `404` if the role does not
exist, `409` if the principal already has that platform role, `400` on validation
errors, and `403` if the caller is not `platform-admin`.

### `GET /grants` — list platform grants (paginated)

Returns `{ "grants": [PlatformGrantSummary], "nextToken"?: string }`. Optional
query parameters:

- `maxResults` — page size, `1`–`100` (default `25`).
- `nextToken` — opaque cursor returned by the previous page; absent in the final page.
- `principal` — filter to a single principal key (e.g. `User::alice@example.com`).

### `DELETE /grants/{grantId}` — revoke a platform grant

Returns `200` on success and `404` if the grant does not exist (the handler
verifies existence before deleting). A `grantId` that does not decode to a
platform resource also returns `404`, so namespace grants cannot be revoked here.

## Grants API — Data Access Restrictions

In addition to the role-based action authorization enforced by Cedar at the API Gateway authorizer, a grant can carry **optional, per-grant data-access overrides** that the SQL Firewall (in `context-manager`) enforces at query execution time. These overrides scope **what data** the principal can read, independent of **which APIs** they can call.

| Field | Type | Purpose | Enforced by |
|---|---|---|---|
| `tableAllowlist` | `list[string]` | Allowlist of bare table names the principal may reference. Schema-qualified references match the bare name. | SQL Firewall, table check |
| `columnDenylist` | `map[string, list[string]]` | Per-table list of columns that may not be projected. `SELECT *` over a restricted table is denied. | SQL Firewall, column check |
| `allowedMetrics` | `list[string]` | Allowlist of metric IDs the principal may evaluate via Tier 1. | Metric service (Tier 1) |

All three fields are validated for shape at write time (`tableAllowlist`/`allowedMetrics` must be `list[string]`; `columnDenylist` must be `map[string, list[string]]`); malformed payloads return `400 Bad Request`. All comparisons in the SQL Firewall are case-insensitive — see [context-manager SQL Firewall docs](../context-manager/README.md#sql-firewall) for enforcement details.

The fields are optional and omitted from the persisted item when unset, so existing grants are not affected.

### Example: create a grant restricted to two tables, denying PII columns

```http
POST /namespaces/{namespaceId}/grants
Content-Type: application/json

{
  "principalType": "User",
  "principalId": "alice@company.com",
  "role": "data-analyst",
  "tableAllowlist": ["orders", "customers"],
  "columnDenylist": {
    "customers": ["ssn", "date_of_birth"]
  },
  "allowedMetrics": ["revenue_total", "order_count"]
}
```

Response (`201 Created`) echoes the same overrides on the `grant` object. With this grant, queries like `SELECT name FROM customers` succeed but `SELECT ssn FROM customers` and `SELECT * FROM payroll` are denied at query execution.

See the Smithy contract in `models/src/main/smithy/grant.smithy` for the canonical schema and the [Authorization Access Matrix](docs/authz-access-matrix.md#data-level-authorization-sql-firewall) for the two-layer authorization model.

## Namespace Creation Flow

When a namespace is created:

1. Reserve the name in DynamoDB (conditional write to prevent duplicates)
2. Create a DataZone project in the SMUS domain
3. Add the shared project-access role as a project member
4. Write namespace record, role templates, and owner grant in a DynamoDB transaction
5. Confirm the reservation

```
Client → CreateNamespace API
  ├─ Reserve name (DDB conditional put)
  ├─ CreateProject (DataZone)
  ├─ CreateProjectMembership (project-access role)
  ├─ TransactWrite (namespace + roles + owner grant)
  └─ Confirm reservation
```

## DataZone Project-Access Role

All services that need DataZone project-level access (search assets, create revisions, etc.) assume a single shared **project-access role** rather than each having direct DataZone permissions.

### Design

```
┌─────────────────────────┐
│  project-access role    │  ← CfnUserProfile created at deploy time
│  (DataZone permissions) │  ← CreateProjectMembership per namespace
└──────▲──▲──▲──▲─────────┘
       │  │  │  │  sts:AssumeRole (fixed session names)
       │  │  │  │
connector  │  │  future-service-N
     enrichment │
          datasource-api
```

### Rationale

DataZone V2 auto-creates a user profile per unique IAM role session. If services use random session names (e.g., `datasource-api-{uuid}`), the domain accumulates orphaned profiles. Additionally, `CreateUserProfile` raises `ValidationException` with inconsistent messages when a profile already exists — there is no dedicated exception class for idempotency.

### Rules

- The `CfnUserProfile` is created in CDK at deploy time (namespace-stack)
- Each assuming service uses a **fixed session name** (`"connector-service"`, `"enrichment-task"`, `"datasource-api"`)
- Only `CreateProjectMembership` is called at namespace creation time
- New services that need DataZone access: `grantAssumeRole` in CDK + `PROJECT_ACCESS_ROLE_ARN` env var + fixed session name
- The trust policy on the project-access role is scoped to the account; each assuming role also needs explicit `sts:AssumeRole` permission

### Why not per-role profiles?

| Approach | Problem |
|----------|---------|
| `CreateUserProfile` per role at runtime | Fails on second call with inconsistent `ValidationException` messages |
| Random session names | Creates N profiles per invocation, polluting the domain |
| Direct DataZone permissions per role | Every new service needs profile onboarding + project membership |

## Authorization (Cedar Policies)

The authorizer Lambda evaluates Cedar policies to determine access. Policies are loaded from DynamoDB (seeded from `src/coa_authorization/seed/*.cedar`).

### Role Naming Convention

Role identifiers use **kebab-case** (e.g., `namespace-owner`, `data-steward`, `data-analyst`). These must match exactly between:
- Cedar policy files (`seed/*.cedar`)
- DynamoDB role records (`SK = ROLE#<role-id>`)
- Grant records in ResourceRoleMappings

### Path Mapping

The `_PATH_MAPPING` dict in `authorization/handler.py` maps each `(API route, HTTP method)` pair to a `(Cedar action, resource type)` tuple. Unmapped routes default to `("denyAll", "Namespace")` — a fail-closed design that rejects requests to unregistered routes.

> **Note:** The mapping covers **every** operation registered on the `ControlPlaneService` Smithy service (`models/src/main/smithy/control-plane.smithy`) — the single API Gateway behind this authorizer. These operations are implemented by several backend packages (namespace/role/grant handlers here, unified sources, metric-service, and the ontology engine) but are all mounted on one API. Routes served by the serve / context-manager path (Playground query/search) use a separate JWT authorizer and are not mapped here. For every namespace-scoped route the evaluated resource `id` and `namespace` attribute are both set to the path `{namespaceId}`, so owner/maintainer (match on `id`) and data-steward/data-analyst (match on `namespace`) policies resolve against the same namespace.

### Access Matrix

See [`docs/authz-access-matrix.md`](docs/authz-access-matrix.md) for the full role × action permission matrix.

## Authorizer Lambda Internals

Source: [`src/coa_control_plane/authorization/handler.py`](src/coa_control_plane/authorization/handler.py)

The authorizer is an API Gateway REQUEST-type Lambda authorizer. It runs on every API call and produces an IAM policy document. API Gateway does not cache the result (`authorizerResultTtl` is 0).

### Token Validation

The authorizer supports two IdP configurations, auto-detected from the `JWKS_ISSUER` environment variable:

| IdP | Detection | Implementation |
|-----|-----------|----------------|
| **Cognito** | `JWKS_ISSUER` contains `cognito-idp` and `.amazonaws.com/` | `CognitoTokenAuthorizer` — extracts user pool ID from the issuer URL |
| **Generic OIDC** | Anything else | `OIDCTokenAuthorizer` — uses `JWKS_URI`, `CLIENT_ID`, and `GROUP_CLAIM_NAME` |

Both produce a `TokenValidationResult` containing `principal_id` (email), `sub`, `groups`, and `token_type`.

### Role Resolution

After token validation, the handler queries the `ResourceRoleMappings` table's `PrincipalIndex` GSI for all grants matching the principal and their IdP groups:

```
Query PrincipalIndex where principalKey = "User::<email>"
Query PrincipalIndex where principalKey = "Group::<group>" (for each IdP group)
```

Grants with `resourceId="GLOBAL"` become **global roles** (e.g., `platform-admin`). All others become **resource roles** with a `resourceUID` for Cedar scoping.

### Cedar Evaluation

The handler loads Cedar policy text from the Roles table for each resolved role (plus `default`), concatenates them, and calls `cedarpy.is_authorized()`. If evaluation throws any exception, the request is denied (fail-closed).

### Cache Invalidation

The authorizer caches resolved roles in-memory across Lambda warm invocations
(the API Gateway authorizer result cache is disabled — `authorizerResultTtl` is
0 — so every request otherwise re-queries the `PrincipalIndex` GSI). Two
independent bounds keep that cache from serving revoked authorization:

1. **Version counter (primary).** A DynamoDB Streams handler
   ([`authorization/stream_handler.py`](src/coa_control_plane/authorization/stream_handler.py))
   watches the Roles and ResourceRoleMappings tables and bumps a version
   counter in `CACHE_INVALIDATION_TABLE`. On each invocation the authorizer
   reads the counter and clears its local cache on mismatch, so a grant change
   takes effect within seconds. The stream event sources retry until a record
   ages out of the 24h stream retention and park anything still failing in the
   `cache-invalidation-dlq` SQS queue — a dropped record would otherwise leave
   the counter permanently behind the tables.
2. **Per-entry max age (backstop).** Entries expire after
   `ROLES_CACHE_TTL_SECONDS` (default 60s) regardless of the counter, so a
   broken stream or an unreadable counter bounds staleness to that window
   instead of the container's lifetime. The cache is also capped at 1000
   entries.

### Wildcard Resource ARN

When Cedar allows a request, the returned IAM policy uses a wildcard resource ARN (`arn:.../*`) rather than the specific method ARN. Cedar has already enforced the (action, resource) for the request, so the policy covers every method on the stage.

### Authorizer Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JWKS_ISSUER` | Yes | Token issuer URL (Cognito user pool URL or OIDC issuer) |
| `JWKS_URI` | No | Explicit JWKS endpoint (OIDC only; derived from issuer if absent) |
| `CLIENT_ID` | No | Expected audience claim |
| `GROUP_CLAIM_NAME` | No | JWT claim containing groups (default: `"groups"`) |
| `RESOURCE_ROLE_MAPPINGS_TABLE_NAME` | Yes | Table for role resolution queries |
| `ROLES_TABLE_NAME` | Yes | Table for loading Cedar policies |
| `CACHE_INVALIDATION_TABLE_NAME` | No | Version counter table (stream-driven invalidation disabled if absent — the per-entry max age still applies) |
| `ROLES_CACHE_TTL_SECONDS` | No | Max age of an in-memory role cache entry (default `60`) |
| `ALLOW_WITHOUT_ROLES` | No | Set `"true"` in dev to bypass Cedar evaluation |

## `coa_authorization` Module

Source: [`src/coa_authorization/`](src/coa_authorization/)

A standalone Python package providing Cedar policy evaluation primitives and the canonical schema for the COA authorization model. Used by the authorizer Lambda at runtime and by unit tests to verify policy logic.

### Key Exports

| Symbol | Purpose |
|--------|---------|
| `policy_evaluator.evaluate()` | Evaluate a Cedar authorization request (policies + principal + action + resource) |
| `policy_evaluator.load_seed_policies()` | Load all `seed/*.cedar` files (for tests and DDB seeding) |
| `policy_evaluator.load_seed_policy(name)` | Load a single named policy |
| `cedar_schema.SCL_SCHEMA` | Cedar namespace prefix (`"SCL"`) |
| `cedar_schema.EntityType` | Enum of Cedar entity types (`USER`, `NAMESPACE`, `TABLE`, etc.) |
| `cedar_schema.Action` | Enum of Cedar actions (`VIEW_NAMESPACE`, `QUERY`, `GRANT_PERMISSIONS`, etc.) |

### Seed Policies

Located in `src/coa_authorization/seed/`. Each `.cedar` file defines permissions for one role:

| File | Role | Summary |
|------|------|---------|
| `default.cedar` | (all authenticated) | Allows `list` on `Namespace` only |
| `global_admin.cedar` | `platform-admin` | Permits all actions on all resources |
| `global_viewer.cedar` | `platform-viewer` | Read-only across all namespaces |
| `namespace_owner.cedar` | `namespace-owner` | Full access scoped by `resource.id` match |
| `namespace_maintainer.cedar` | `namespace-maintainer` | Same as owner (functionally identical) |
| `namespace_data_steward.cedar` | `data-steward` | Manage data assets + read/query |
| `namespace_data_analyst.cedar` | `data-analyst` | Read/query only |

### Adding or Modifying a Policy

1. Create/edit the `.cedar` file in `src/coa_authorization/seed/`
2. Ensure the role name in the policy matches the kebab-case role ID in DynamoDB (`ROLE#<role-id>`)
3. Run `uv run pytest packages/control-plane -m unit -k test_authz` to validate the policy logic
4. The deployment pipeline seeds updated policies into the Roles table at deploy time

## Environment Variables

### Namespace API Lambda

| Variable | Source | Required | Description |
|----------|--------|----------|-------------|
| `NAMESPACES_TABLE` | CDK | Yes | Namespaces DynamoDB table |
| `ROLES_TABLE` | CDK | Yes | Roles DynamoDB table |
| `RESOURCE_ROLE_MAPPINGS_TABLE` | CDK | Yes | Grants DynamoDB table |
| `SOURCES_TABLE` | CDK | No | Unified sources table (for delete cleanup) |
| `SOURCE_SCAN_JOBS_TABLE` | CDK | No | Source scan jobs table (for delete cleanup) |
| `ONTOLOGY_ENGINE_TABLE` | CDK | No | Ontology engine table (for delete precondition check) |
| `METRIC_IMPORT_JOBS_TABLE` | CDK | No | Metric import jobs table (for delete precondition check) |
| `DATAZONE_DOMAIN_ID` | CDK | Yes | SMUS domain ID |
| `DATAZONE_PROJECT_PROFILE_ID` | CDK | Yes | Default project profile ID |
| `PROJECT_ACCESS_ROLE_ARN_SSM` | CDK | Yes | SSM param path for project-access role ARN |
| `ALLOWED_ORIGIN` | CDK | Yes | CORS allowed origin |

### Roles API Lambda

| Variable | Source | Required | Description |
|----------|--------|----------|-------------|
| `ROLES_TABLE` | CDK | Yes | Roles DynamoDB table |
| `ALLOWED_ORIGIN` | CDK | Yes | CORS allowed origin |

### Grants API Lambda

| Variable | Source | Required | Description |
|----------|--------|----------|-------------|
| `RESOURCE_ROLE_MAPPINGS_TABLE` | CDK | Yes | Grants DynamoDB table |
| `ROLES_TABLE` | CDK | Yes | Roles table (validate role exists) |
| `ALLOWED_ORIGIN` | CDK | Yes | CORS allowed origin |

## DELETE /namespaces/{namespaceId} Behavior

The delete endpoint validates preconditions before removing namespace resources:

**Preconditions:**
- Namespace must be in `ARCHIVED` or `DELETE_FAILED` status
- No child resources (sources, ontology jobs, metrics) may exist

If child resources exist, the endpoint returns `409` with a blockers array:
```json
{"message": "Cannot delete namespace: child resources still exist.", "blockers": ["2 source(s) still exist"]}
```

**Deletion phases (ordered):**
1. External resources: Athena workgroup, DataZone project
2. DDB cleanup: role mappings, roles
3. Atomic transaction: namespace record + name reservation

If any phase fails, status is set to `DELETE_FAILED`. Retry the DELETE to resume.

## DynamoDB Schema

### Namespaces Table

| Key | Pattern | Description |
|-----|---------|-------------|
| PK | `NS#{namespaceId}` | Namespace partition |
| SK | `METADATA` | Namespace metadata record |
| SK | `RESERVATION` | Name reservation (TTL-based) |

**GSI: NameByIndex** — `name` (PK) for uniqueness checks.

### Roles Table

| Key | Pattern | Description |
|-----|---------|-------------|
| PK | `NS#{namespaceId}` or `GLOBAL` | Scope |
| SK | `ROLE#{roleId}` | Role definition |

### ResourceRoleMappings Table

| Key | Pattern | Description |
|-----|---------|-------------|
| PK | `{resourceType}::{resourceId}#{principalType}::{principalId}` | What the grant covers and who holds it |
| SK | `ROLE#{roleId}` | Role granted |

**GSI: NamespaceGrantsIndex** — `namespaceKey` (PK), `principalRoleKey` (SK) for listing grants by namespace.

#### Optional grant override attributes

The grant item may also carry the following sparse top-level attributes — they are written only when supplied on `CreateGrant` and omitted otherwise. They are read back unchanged into the SQL Firewall's `profile` (see [context-manager SQL Firewall](../context-manager/README.md#sql-firewall)) at query time.

| Attribute | Type | Description |
|---|---|---|
| `tableAllowlist` | `List<String>` | Allowlist of bare table names the principal may query. |
| `columnDenylist` | `Map<String, List<String>>` | Per-table list of columns that must not be projected. |
| `allowedMetrics` | `List<String>` | Allowlist of metric IDs the principal may evaluate (Tier 1). |

Example item with overrides populated:

```json
{
  "PK": "Namespace::demo#User::alice@company.com",
  "SK": "ROLE#data-analyst",
  "principalType": "User",
  "principalId": "alice@company.com",
  "role": "data-analyst",
  "resourceType": "Namespace",
  "resourceId": "demo",
  "principalKey": "User::alice@company.com",
  "resourceRoleKey": "Namespace::demo#ROLE#data-analyst",
  "namespaceKey": "NS#demo",
  "principalRoleKey": "User::alice@company.com#ROLE#data-analyst",
  "grantedBy": "admin@company.com",
  "grantedAt": "2026-06-01T09:30:00.000+00:00",
  "tableAllowlist": ["orders", "customers"],
  "columnDenylist": {"customers": ["ssn", "date_of_birth"]},
  "allowedMetrics": ["revenue_total"]
}
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| 403 on all requests after deploying new roles | Authorizer Lambda's local cache is stale and the stream handler hasn't bumped the version counter | Check that the DynamoDB Streams trigger is active on the Roles/ResourceRoleMappings tables; verify `CACHE_INVALIDATION_TABLE_NAME` is set |
| 403 on second API call with same token, first call succeeds | Authorizer returned a method-specific IAM policy | Verify the authorizer returns wildcard resource ARN (`/*`) on Allow decisions |
| `ConditionalCheckFailedException` on namespace create | Name reservation already exists (possibly orphaned from a crashed attempt) | Wait for TTL expiry (5 min) or manually delete the `NS_NAME#<name>` / `RESERVATION` item |
| `Role 'X' not found in namespace` on grant creation | Role templates weren't copied to the namespace during creation | Check `NAMESPACE_TEMPLATE` partition in the Roles table; re-run namespace creation if templates are missing |
| Athena workgroup not created (namespace has no `athenaWorkgroupName`) | `ATHENA_RESULTS_BUCKET_SSM` env var missing or SSM param not found | Set the SSM parameter and update the namespace (or delete/recreate) |
| Cedar evaluation always denies | No policies loaded — `default` role record missing from Roles table | Verify `PK=GLOBAL, SK=ROLE#default` exists in the Roles table with a `cedarPolicy` attribute |
