# AuthNZ Role Access Matrix

## Role Hierarchy

| Scope | Role | Description |
|-------|------|-------------|
| Global | `platform-admin` | Full access to all actions on all resources |
| Global | `platform-viewer` | Read-only access across all namespaces |
| Namespace | `namespace-owner` | Full access within their namespace |
| Namespace | `namespace-maintainer` | Full access within their namespace (same as owner) |
| Namespace | `data-steward` | Manage data assets + read/query within their namespace |
| Namespace | `data-analyst` | Read/query only within their namespace; **schema-blind** — denied `viewSource`/`viewOntology`, so it cannot read raw source table/column or ontology class/property names |
| — | (any authenticated user) | Can list namespaces |

## API → Cedar Action Mapping

This table reflects every operation registered on the **`ControlPlaneService`**
Smithy service — the only API behind the Lambda authorizer
(`packages/control-plane/.../authorization/handler.py`). `/health` is
unauthenticated (`@optionalAuth`) and is not listed. Any route not listed here
falls through to `denyAll` (fail-closed) — a dedicated test
(`test_no_secured_smithy_operation_is_left_unmapped`) parses the tracked
Smithy sources and fails if a new `@http` operation ships without an entry
here, so this table cannot silently drift out of sync with the service again.

### Namespace, roles & grants

| API Endpoint | Method | Cedar Action | Resource Type |
|---|---|---|---|
| `/namespaces` | GET | `list` | Namespace |
| `/namespaces` | POST | `manageNamespace` | Namespace |
| `/namespaces/{namespaceId}` | GET | `viewNamespace` | Namespace |
| `/namespaces/{namespaceId}` | PUT | `manageNamespace` | Namespace |
| `/namespaces/{namespaceId}` | DELETE | `deleteNamespace` | Namespace |
| `/namespaces/{namespaceId}/status` | PATCH | `setNamespaceStatus` | Namespace |
| `/roles` | GET | `list` | Namespace |
| `/namespaces/{namespaceId}/roles` | GET | `viewNamespace` | Namespace |
| `/namespaces/{namespaceId}/roles/{roleId}` | GET | `viewNamespace` | Namespace |
| `/namespaces/{namespaceId}/grants` | GET | `viewNamespace` | Namespace |
| `/namespaces/{namespaceId}/grants` | POST | `grantPermissions` | Namespace |
| `/namespaces/{namespaceId}/grants/{grantId}` | DELETE | `grantPermissions` | Namespace |
| `/principals/{principalId}/grants` | GET | `list` | Namespace |
| `/grants` | GET | `viewNamespace` | Namespace |
| `/grants` | POST | `grantPermissions` | Namespace |
| `/grants/{grantId}` | DELETE | `grantPermissions` | Namespace |

### Unified sources (database + document)

| API Endpoint | Method | Cedar Action | Resource Type |
|---|---|---|---|
| `/namespaces/{namespaceId}/sources` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources` | POST | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/upload-urls` | POST | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}` | DELETE | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/rescan` | POST | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/metadata` | PUT | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/tables` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/scan` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}` | GET | `viewSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/approve` | POST | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/reject` | POST | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review` | PUT | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata` | PATCH | `manageSource` | Source |
| `…/tables/{tableId}/columns/{columnName}/review` | PUT | `manageSource` | Source |
| `…/tables/{tableId}/columns/{columnName}/metadata` | PATCH | `manageSource` | Source |
| `/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys` | PATCH | `manageSource` | Source |

### Metric service

| API Endpoint | Method | Cedar Action | Resource Type |
|---|---|---|---|
| `/namespaces/{namespaceId}/metrics` | GET | `readMetric` | Metric |
| `/namespaces/{namespaceId}/metrics` | POST | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/metrics/{name}` | GET | `readMetric` | Metric |
| `/namespaces/{namespaceId}/metrics/{name}` | PUT | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/metrics/{name}` | DELETE | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/metrics/validate` | POST | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/bulk-delete-metrics` | POST | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/import-osi` | POST | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/import-osi/upload-url` | POST | `manageMetric` | Metric |
| `/namespaces/{namespaceId}/import-jobs/{jobId}` | GET | `viewSource` | Metric |
| `/namespaces/{namespaceId}/export-osi` | GET | `viewSource` | Metric |

### Ontology induction, graph & catalog

| API Endpoint | Method | Cedar Action | Resource Type |
|---|---|---|---|
| `/namespaces/{namespaceId}/induce` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/induce/jobs` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/induce/jobs/{jobId}` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/induce/datasources/induced` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/proposals` | GET | `viewOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}` | GET | `viewOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/accept` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/cancel` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/validate` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}` | GET | `viewOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints/jobs/{jobId}` | GET | `viewOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/{proposalId}/upload-url` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/update` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/proposals/reject` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/ontologies` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies` | DELETE | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}/download` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/upload-url` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}/upload` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}/fetch` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-status/{jobId}` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/ontology/foundational` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/ontology/foundational/{key}/load` | POST | `manageOntology` | Ontology |
| `/namespaces/{namespaceId}/proposals/{proposalId}/repair-datatypes` | POST | `manageOntology` | OntologyProposal |
| `/namespaces/{namespaceId}/graph/traverse` | POST | `traverseGraph` | Namespace |
| `/namespaces/{namespaceId}/graph/ontology-overview` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/schema` | GET | `viewOntology` | Namespace |
| `/namespaces/{namespaceId}/graph/search` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/graph/class` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/graph/object-property` | GET | `viewOntology` | Ontology |
| `/namespaces/{namespaceId}/graph/datatype-property` | GET | `viewOntology` | Ontology |

Two judgement calls worth surfacing:

- **Minting an upload URL is gated as a write** (`manageOntology`, not
  `viewNamespace`) on `upload-url`, `upload`, `fetch`, and `ingest-from-s3`. A
  presigned PUT hands out the ability to place an artifact, so the URL itself is
  gated like the upload it enables — matching the sibling precedents at
  `/proposals/{proposalId}/upload-url` and `/sources/upload-urls`. `fetch` reads
  from a remote source but writes the result into this namespace's store, so it
  is a write despite the name. Polling an ingest job (`ingest-status/{jobId}`)
  only reveals progress, so it stays a read — the same split as
  `/proposals/{proposalId}/validate/jobs/{jobId}`.
- **`manageOntology` brings the archived-namespace mutation guard into play**
  (see below) where `viewNamespace` would not — intended for the writes above,
  but a real behavioural consequence of the choice.

### Platform-scoped (no `{namespaceId}`)

| API Endpoint | Method | Cedar Action | Resource Type |
|---|---|---|---|
| `/system-health` | GET | `list` | Namespace |

`/system-health` has no `{namespaceId}`, so the evaluated resource `id`
resolves to `"*"` and no namespace-scoped grant can ever match it — the same
situation as `/grants` and `/roles`. It is mapped to `list` on `Namespace`,
which `default.cedar` already permits for every authenticated principal (the
same basis `GET /namespaces` and `GET /roles` stand on), rather than to a new
platform-only gate.

!!! note "`query` and `searchDocuments`"
    The control-plane authorizer maps `POST /namespaces/{namespaceId}/query` to
    `query` and `POST /namespaces/{namespaceId}/kb/search` to `searchDocuments`.
    They are also evaluated on the serve / context-manager path (the
    Playground/agent runtime), which uses its own JWT authorizer and the same
    role model.

    On that path, `query` is evaluated by the **namespace admission gate** at the
    top of the runtime entrypoint — before any dispatch, and for every
    namespace-scoped surface (the Tier-1/2/3 query path plus the isolated
    `translate` / `kbSearch` / `graphTraverse` operations) — and again at SQL
    execution time for Tier 1/2 statements. The admission gate is what covers the
    paths that generate no SQL (Tier-3 retrieval and synthesis). See the
    [context-manager README](../../context-manager/README.md#cedar-authorization).

## Role × Action Access Matrix

✅ = Allowed | ❌ = Denied

| Cedar Action | platform-admin | platform-viewer | namespace-owner | namespace-maintainer | data-steward | data-analyst | (authenticated) |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `list` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (Namespace only) |
| `viewNamespace` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `viewSource` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `viewOntology` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `query` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `traverseGraph` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `readMetric` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `searchDocuments` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `manageSource` | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `manageOntology` | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `manageMetric` | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `manageNamespace` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `setNamespaceStatus` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `deleteNamespace` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `grantPermissions` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `manageRoles` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |

## Schema Visibility (`viewSource` / `viewOntology`)

`viewNamespace` was decomposed into three distinct read actions so a
**schema-blind `data-analyst`** can query and use the namespace console without
ever seeing the raw schema vocabulary. Every role except `data-analyst`
holds all three, so this is behaviour-preserving for everyone else.

| Action | Gates | Held by `data-analyst`? |
|---|---|---|
| `viewNamespace` | Namespace metadata, roles, grants — the reads the console needs | ✅ yes |
| `viewSource` | Source **schema** reads — table/column names (`/sources…` GET rows) | ❌ no |
| `viewOntology` | Ontology **schema** reads — class/property names (induce, proposals, ontologies, graph GET rows) | ❌ no |

Enforcement points (all must agree; each fails closed unless stated):

1. **Control-plane route map** (`handler.py` `_PATH_MAPPING`) — the GET rows
   above are gated on `viewSource`/`viewOntology`; the 5 namespace
   metadata/roles/grants reads stay on `viewNamespace`.
2. **Serve response scrub** (`context-manager` `coa_serve/schema_visibility.py`)
   — for a caller without the right, serve withholds the schema-bearing fields
   of a query response (generated SQL/SPARQL, trace step details) and redacts
   withheld identifiers from the synthesized answer, string result-row values
   and `supportingContent`; error text is sanitized the same way; replayed
   session turns are re-scrubbed with the caller's CURRENT rights; and SQL that
   references no table or only literals is refused rather than answered, so it
   cannot be used to probe the schema.
3. **MCP tool map** (`mcp-server` `grant_resolver.py` `TOOL_CEDAR_ACTION`) —
   `describe_schema` is hard-gated on `viewOntology` (pure ontology dump).
   `translate_sparql` and `graph_traversal` stay on their working-tool action
   (`query` / `traverseGraph`) so a schema-blind analyst can still RUN them.
4. **MCP output suppression** (`mcp-server` `server.py`) — the second layer
   behind serve: those two tools' schema-bearing fields are withheld unless the
   caller holds `viewOntology` (below).
5. **Metric-service field withholding** (`metric-service`
   `coa_metrics/api/schema_visibility.py`) — metric reads stay on `readMetric`,
   which every role holds; the SQL expression and `sourceTable` are blanked
   without `viewSource` and `ontologyConcepts` is dropped without
   `viewOntology`. An update that omits `ontologyConcepts` keeps the stored
   concepts, and blank SQL or a blank `sourceTable` is rejected, so a save from
   a withheld form cannot erase them.
6. **Grant-listing override stripping** (`control-plane`
   `grants/schema_visibility.py`) — `tableAllowlist` / `columnDenylist` are
   stripped from namespace grant listings without `canViewSource`, and from the
   caller's own listing on every grant outside `viewSourceNamespaces` (`*` =
   platform-wide `viewSource`, every namespace visible).

The authorizer forwards the rights to points 5 and 6 as authorizer-context
flags (`canViewSource`, `canViewOntology`, `viewSourceNamespaces`; names and
parsing shared in `libs/common` `coa_authorization/schema_visibility.py`). Only
`"true"` (case-insensitive) grants. Missing-flag rules:

- **Metric reads:** if NEITHER flag is present the definition is returned in
  full. This is deliberate, for the deploy window in which the metric service is
  live before the authorizer that sets the flags, and for dev mode without a
  roles table. A request carrying one flag withholds the other.
- **Grant listings:** a missing key withholds (fail closed). The authorizer
  therefore always emits the key on those routes when it allows the request; in
  dev mode without a roles table it emits the withholding value (`"false"` /
  `""`).

### Artifact gate on the serve path

The Playground/agent debug artifacts are gated per-artifact by exactly the
schema each discloses: the generated **SQL** (`query_used`) on `viewSource`, the
generated **SPARQL** (`sparql_generated`) on `viewOntology`
(`context-manager` orchestrator `_resolve_include_sql` / `_resolve_include_sparql`,
fail-closed). Artifacts are returned to any role holding the right (the coarse
role-list pre-gate was removed in favour of this single per-artifact
enforcement point); a `data-analyst`, holding neither, never sees them.

### Output suppression on the MCP tools

`translate_sparql` and `graph_traversal` are authorized on `query`/`traverseGraph`
(which `data-analyst` holds), so the tools run — but their whole schema-bearing
response fields are withheld unless the caller separately holds `viewOntology`:
`translate_sparql` drops `sparqlQuery`; `graph_traversal` drops
`entities`/`relationships`. Whole-field, shape-preserving withhold (the field is
emptied, not deleted), fail-closed: the `viewOntology` decision is evaluated
once by `resolve_grant` from the same loaded policy text as the tool action,
cached on the resolved profile (`schemaVisibility`), and any evaluation error
yields "withhold". Serve has already stripped these fields and every trace step
`detail` before MCP sees the response, so this layer is defense in depth.
Local dev (auth disabled) is exempt so development is not blinded.

## Scope Notes

- **Global roles** (`platform-admin`, `platform-viewer`) apply across all namespaces unconditionally.
- **Namespace roles** are scoped via `resourceUID` — they only grant access when the resource's `id` or `namespace` attribute matches the namespace the role was granted on.
- For every namespace-scoped route, the authorizer sets **both** the evaluated resource `id` **and** its `namespace` attribute to the `{namespaceId}` from the path. This lets owner/maintainer policies (which match on `resource.id`) and data-steward/data-analyst policies (which match on `resource.namespace`) resolve against the same namespace. Platform routes (`/grants`, `/roles`, `/principals/...`, `/system-health`) use `"*"`, which no namespace-scoped role can satisfy — so only global roles (or, for `list`, any authenticated principal) apply there.
- **Resource types are per-route.** Each route is evaluated against its proper Cedar entity type — `Source` (unified database/document sources), `Metric`, `Ontology`, `OntologyProposal`, or `Namespace` — rather than always `Namespace`. Authorization is still **namespace-scoped**: role grants target a namespace (`resourceUID = namespaceId`), and the evaluated `id`/`namespace` are the namespace id. The typed resources document intent and provide the foundation for future per-resource (resource-id-level) policies; no current policy keys on a sub-resource id.
- The `default.cedar` policy grants `list` on `Namespace` resources to all authenticated users (allows listing namespaces; API-level filtering restricts what's returned).
- `namespace-owner` and `namespace-maintainer` are functionally identical (both use `permit(principal, action, resource)` with a resource ID match).
- **Platform grants** (`/grants`) manage `platform-admin`/`platform-viewer` bindings. The authorizer maps the write operations (`POST`, `DELETE`) to `grantPermissions` with `resource_id="*"`, so only `platform-admin` (whose `global_admin.cedar` policy permits all actions) is allowed — a `namespace-owner` is matched by a specific `resourceUID` and never satisfies `"*"`. The read operation (`GET`) maps to `viewNamespace`, so `platform-viewer` may also list platform grants.

## Archived Namespace Mutation Guard

A namespace in **`ARCHIVED`** status is frozen: its data is retained for audit,
but no **mutating** action may be performed against it or its child resources.
This is enforced by a Cedar `forbid` policy (`seed/archived_namespace_guard.cedar`)
that **overrides every `permit`** — including `platform-admin` and
`namespace-owner`. Archived-state immutability is a hard invariant, not a
role-overridable default.

**Blocked while `ARCHIVED`:** `manageSource`, `manageMetric`, `manageOntology`,
`manageNamespace`, `grantPermissions`, `manageRoles`.

**Still allowed while `ARCHIVED`:**

- All reads — `viewNamespace`, `viewSource`, `viewOntology`, `list`, `query`,
  `readMetric`, `searchDocuments` (queries against archived namespaces are
  intentionally permitted).
- `setNamespaceStatus` — so the namespace can be reactivated (`ARCHIVED → ACTIVE`).
  This is why the status endpoint maps to its own action rather than
  `manageNamespace`.
- `deleteNamespace` — deletion is only legal from `ARCHIVED`.

**How it works:** for a mutating route carrying a `{namespaceId}`, the authorizer
reads the namespace's status from the Namespaces table and passes it to Cedar as
`context.namespaceStatus`. The guard fires when that value is `"ARCHIVED"`. Reads
and lifecycle actions skip the status fetch entirely, so they incur no extra
latency and remain available. If the status lookup fails, the request is denied
(fail-closed).

> **Propagation:** the namespace status is read on every mutating request and
> never cached, so archiving takes effect on the next request.

## Data-Level Authorization (SQL Firewall)

The matrix above describes **action-level** authorization — which Cedar actions a principal may invoke on which resources. The system also enforces a second, independent layer of **data-level** authorization at query execution time. Both layers run on every authenticated query.

| Layer | Enforced where | What it controls | Mechanism |
|---|---|---|---|
| **Action-level (Cedar)** | API Gateway Lambda authorizer | Which API operations the principal may invoke on which namespace (e.g., "may alice run `query` against namespace `demo`?") | Cedar policies + RBAC (`ResourceRoleMappings`) |
| **Data-level (SQL Firewall)** | `context-manager` at query execution (`tier2/sql_firewall.py`) | Which **tables** and **columns** the principal may read once a query has been authorized at the action level | `tableAllowlist`, `columnDenylist`, `allowedMetrics` carried on the principal's grant |

The two layers compose: the request must be permitted at **both** layers to reach data.

```
client query
   │
   ▼
API Gateway ──► Lambda Authorizer ──► Cedar policy evaluation
   │                                    │
   │                                    ├─ deny → 403 (action denied)
   │                                    └─ allow ▼
   │
   ▼
context-manager (Tier 1 / Tier 2 resolution)
   │
   ▼
SQL Firewall ──► safety check (SELECT-only, dangerous funcs)
   │
   ├─ unsafe → UnsafeSQLError
   │
   └─ safe ──► table allowlist / column denylist (from grant)
                │
                ├─ deny → empty result + denial reason
                └─ allow → execute query
```

### Examples

| Scenario | Cedar (action) | SQL Firewall (data) | Outcome |
|---|---|---|---|
| `data-analyst` on `demo` runs `SELECT * FROM orders` | ✅ `query` allowed | ✅ no restrictions on grant | Query runs |
| Same principal, grant has `tableAllowlist: ["customers"]` | ✅ `query` allowed | ❌ `orders` not in allowlist | Denied at firewall |
| Same principal, grant has `columnDenylist: {"customers": ["ssn"]}`, query is `SELECT name FROM customers` | ✅ `query` allowed | ✅ `ssn` not referenced | Query runs |
| Same principal, query is `SELECT * FROM customers` | ✅ `query` allowed | ❌ `SELECT *` may expose `ssn` | Denied at firewall |
| `data-analyst` calls `manageSource` | ❌ action denied | (not reached) | 403 from authorizer |

### Key properties

- **Independent enforcement points.** The Cedar layer runs at the edge (authorizer Lambda); the data layer runs deep in the request flow inside `context-manager`. Bypassing one does not bypass the other.
- **Sparse, opt-in.** Data-level restrictions are optional fields on a grant. When they are absent, only the Cedar layer applies — every query allowed at the action level is also allowed at the data layer.
- **Case-insensitive comparisons.** SQL identifiers are case-insensitive for unquoted names; the firewall normalizes both query references and profile entries to lower-case so casing variations cannot bypass the rules.
- **Fail-closed.** Parse failures, `SELECT *` over a restricted table, or malformed grant profiles all deny. See `packages/context-manager/src/coa_serve/tier2/sql_firewall.py`.

For the field schema and DynamoDB persistence, see [Grants API — Data Access Restrictions](../README.md#grants-api--data-access-restrictions) and the `ResourceRoleMappings` table docs.
