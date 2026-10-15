# Cedar Policy Authoring Guide

Context Ontology Accelerator uses [Cedar](https://www.cedarpolicy.com/) for fine-grained, policy-based authorization. This guide covers the COA Cedar schema, the built-in role policies, and common authorization patterns.

## How Cedar Works in COA

Every API request passes through the Lambda authorizer, which:

1. Resolves the user's roles (from DynamoDB grants)
2. Maps the API route to a Cedar `(action, resource)` pair
3. Loads Cedar policies for the user's roles
4. Evaluates all policies — if **any** policy produces `permit`, access is granted

Policies are stored in DynamoDB and loaded per-request. Built-in role policies are seeded at deploy time from `.cedar` files.

## COA Cedar Schema

### Namespace

The Cedar namespace for all COA entities is `COA`.

### Entity Types

| Entity | Description |
|--------|-------------|
| `SCL::User` | An authenticated principal (human or agent) |
| `SCL::Namespace` | A logical workspace scoping all other resources |
| `SCL::Source` | A registered data source — structured database **or** unstructured document source — managed via the unified `/sources` API |
| `SCL::Metric` | A business metric definition |
| `SCL::Ontology` | A managed ontology within a namespace |
| `SCL::OntologyProposal` | A proposed ontology change from induction, pending review |

### Actions

| Action | Category | Description |
|--------|----------|-------------|
| `list` | Discovery | Enumerate resources the principal can see |
| `viewNamespace` | Read | View namespace details and child resources |
| `query` | Read | Execute data queries |
| `readMetric` | Read | Read metric definitions |
| `searchDocuments` | Read | Search document sources |
| `manageNamespace` | Write | Create, update namespace settings |
| `deleteNamespace` | Write | Delete a namespace |
| `manageSource` | Write | Create, update, delete data sources (structured & document) |
| `manageMetric` | Write | Create, update, delete metrics |
| `manageOntology` | Write | Create, update, delete ontologies and review proposals |
| `grantPermissions` | Admin | Assign/revoke roles to principals |
| `manageRoles` | Admin | Create/edit role definitions |

### Principal Attributes

The `SCL::User` entity carries these attributes:

```
{
  "userId": "alice@company.com",
  "groups": ["engineering", "data-team"],
  "globalRoles": ["platform-admin"],
  "resourceRoles": [
    { "role": "data-steward", "resourceUID": "ns-uuid-123" }
  ]
}
```

### Resource Attributes

Resources carry:
- `id` — the resource's unique identifier (e.g., namespace UUID)
- `namespace` — the owning namespace ID (for sub-resources like Source)

## Built-in Policies

The platform ships with these role policies:

### `platform-admin` (global)

```cedar
permit (principal, action, resource)
when { principal.globalRoles.contains("platform-admin") };
```

Allows everything, everywhere.

### `platform-viewer` (global)

```cedar
permit (principal, action == SCL::Action::"viewNamespace", resource)
when { principal.globalRoles.contains("platform-viewer") };

permit (principal, action == SCL::Action::"query", resource)
when { principal.globalRoles.contains("platform-viewer") };
```

Read-only access across all namespaces.

### `namespace-owner` (scoped)

```cedar
permit (principal, action, resource)
when {
  principal.resourceRoles.contains({
    "role": "namespace-owner",
    "resourceUID": resource.id
  })
};
```

Full access, but only when the resource's `id` matches the namespace where the role was granted.

### `data-steward` (scoped)

Permits `manageSource`, `manageOntology`, `manageMetric`, `query`, `readMetric`, `searchDocuments`, `viewNamespace`, and `list` — all scoped to the granted namespace.

### `data-analyst` (scoped)

Permits `query`, `readMetric`, `searchDocuments`, `viewNamespace`, and `list` — read-only access scoped to the granted namespace.

### `default` (all users)

```cedar
permit (principal, action == SCL::Action::"list", resource is SCL::Namespace);
```

All authenticated users can list namespaces (API-level filtering controls what's returned).

## Testing Policies

### Unit testing with Python

The `coa_authorization` package includes a policy evaluator you can use in tests:

```python
from coa_authorization.policy_evaluator import evaluate, load_seed_policy

# Load your custom policy
my_policy = open("my-custom-role.cedar").read()
default_policy = load_seed_policy("default")
policies = f"{default_policy}\n{my_policy}"

# Test: user with custom role can query
result = evaluate(
    policies=policies,
    user_id="bob@company.com",
    action="query",
    resource_type="Namespace",
    resource_id="ns-123",
    resource_roles=[{"role": "my-custom-role", "resourceUID": "ns-123"}],
)
assert result.allowed

# Test: user without role cannot query
result = evaluate(
    policies=policies,
    user_id="eve@company.com",
    action="query",
    resource_type="Namespace",
    resource_id="ns-123",
    resource_roles=[],
)
assert not result.allowed
```

### Dry-run in the authorizer

Set `ALLOW_WITHOUT_ROLES=true` on the authorizer Lambda in a **dev-only** environment to bypass Cedar evaluation while testing. Never enable this in production.

## Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Policy injection | Policies are stored in DynamoDB, not user-supplied at request time |
| Fail-open risk | Unknown routes default to `denyAll` action; Cedar evaluation exceptions deny |
| Stale cache | DynamoDB Streams handler bumps a version counter; authorizer clears cache on mismatch |
| Roles table unavailable | The API authorizer and the MCP server deny. The query runtime (Context Manager) reuses each role's last successfully loaded policy for up to 15 minutes (`SCL_CEDAR_POLICY_MAX_STALE_S`), then denies; it never falls back to the shipped default policies |
| SPARQL injection (ontology concepts) | Identifiers are validated against a strict CURIE/IRI allowlist before any SPARQL query |

## Reference

- [Cedar language specification](https://docs.cedarpolicy.com/policies/syntax-policy.html)
