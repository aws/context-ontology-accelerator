# Role & Permission Management Guide

The Context Ontology Accelerator uses a two-layer authorization model: **Cedar policies** control which API actions a user can perform, and optional **data-level restrictions** on grants control which tables and columns they can query.

!!! tip "Full request/response schemas"
    For the complete request/response schema for every grants/roles
    endpoint below, see the **[API Reference](#/api-reference)** (Control
    Plane API → Grant management, Role management) — it's generated
    directly from the API contract and always current.

!!! important "Grants do not create or validate identities in your IdP"
    Granting a role in Context Ontology Accelerator **does not touch your identity
    provider (IdP)**. A grant records an authorization *intent* for a principal
    (a user email or a group name) — it does not create the user, verify that
    the user or group exists, or modify anything in Cognito or your external
    IdP.

>   A grant is only *evaluated* when a request arrives carrying a token. It
    takes effect when a user authenticates with a matching email (for `User`
    grants) or presents a matching `groups` claim in their JWT (for `Group`
    grants). Until then the grant simply lies dormant.

>   **This means you can grant a role to a principal who does not yet exist in
    the IdP, and the system will accept it.** This is expected behavior:

>   - (1) The principal must already exist in your IdP (or the group must already
      be present in the relevant users' token claims) for the grant to have any
      effect. Create the user/group in your IdP first.

>   - (2) Context Ontology Accelerator does **not** validate principal existence at
    grant time. For third-party/external IdPs it has no read access to the
    IdP's directory, so existence checks would require custom, per-deployment
    integration. Richer grant-time validation is tracked as a potential
    future enhancement.

>   - (3) Treat granting `platform-admin` with the same care as any privileged
    access: because there is no existence gate, double-check the email or
    group name before granting.

## Role Hierarchy

### Platform Roles (Global)

Platform roles apply across **all** namespaces without any namespace-specific grant.

| Role | Description |
|------|-------------|
| `platform-admin` | Full access to all actions on all resources in all namespaces |
| `platform-viewer` | Read-only access across all namespaces (view, query, search) |

### Namespace Roles (Scoped)

Namespace roles are granted per-namespace and only apply within that namespace.

| Role | Permissions |
|------|------------|
| `namespace-owner` | Full access within the namespace (manage, delete, grant permissions) |
| `namespace-maintainer` | Same as owner (functionally identical) |
| `data-steward` | Manage data sources, ontologies, metrics + read/query access |
| `data-analyst` | Read-only: query data, view metrics, search documents |

### Permission Matrix

| Action | platform-admin | platform-viewer | namespace-owner | data-steward | data-analyst |
|--------|:-:|:-:|:-:|:-:|:-:|
| List namespaces | ✅ | ✅ | ✅ | ✅ | ✅ |
| View namespace | ✅ | ✅ | ✅ | ✅ | ✅ |
| Create namespace | ✅ | ❌ | ❌ | ❌ | ❌ |
| Update namespace | ✅ | ❌ | ✅ | ❌ | ❌ |
| Delete namespace | ✅ | ❌ | ✅ | ❌ | ❌ |
| Manage data sources | ✅ | ❌ | ✅ | ✅ | ❌ |
| Manage ontologies | ✅ | ❌ | ✅ | ✅ | ❌ |
| Manage metrics | ✅ | ❌ | ✅ | ✅ | ❌ |
| Query data | ✅ | ✅ | ✅ | ✅ | ✅ |
| Grant permissions | ✅ | ❌ | ✅ | ❌ | ❌ |

## Managing Platform Grants

Platform grants are managed from the **Identity** page in the web app, or via
`POST /grants` (**CreatePlatformGrant**), `GET /grants` (**ListPlatformGrants**),
and `DELETE /grants/{grantId}` (**DeletePlatformGrant**) — see the
[API Reference](#/api-reference) for the full request/response schemas.
Grants accept either `principalType: "User"` (an email) or `"Group"`
(matching an IdP group name).

!!! note
    The **Grant access** dialog on the Identity page validates only the
    *format* of the email or group name — it does not check whether that
    principal exists in your IdP (see "Grants do not create or validate
    identities in your IdP" above).
    A grant to a not-yet-provisioned user or group is accepted and becomes
    active once a matching identity authenticates.

## Managing Namespace Grants

Namespace grants are managed from the **Permissions** tab within a namespace,
or via `POST /namespaces/{namespaceId}/grants` (**CreateGrant**) — see the
[API Reference](#/api-reference) for the full request/response schema.

### Grant with Data-Level Restrictions

You can restrict **what data** a user can query by adding optional override
fields (`tableAllowlist`, `columnDenylist`, `allowedMetrics`) to the same
**CreateGrant** request. These restrictions are enforced independently of
Cedar (at query execution time by the SQL Firewall) — see the
[API Reference](#/api-reference) for the exact field types.

| Field | Type | Effect |
|-------|------|--------|
| `tableAllowlist` | `list[string]` | Only these tables can be queried. Any other table reference is denied, and other tables' names are left out of the query trace and response metadata. |
| `columnDenylist` | `map[string, list[string]]` | These columns cannot be projected. `SELECT *` on a restricted table is denied. |
| `allowedMetrics` | `list[string]` | Only these metric IDs can be evaluated. |

!!! note
    Data restrictions are **optional**. When omitted, the user has full query access to all data within their namespace (subject to Cedar action-level permissions).

### How Two-Layer Authorization Works

```
Request arrives
    │
    ▼
Layer 1: Cedar (API Gateway Authorizer)
    "Can this user call this API on this namespace?"
    │
    ├─ Deny → 403 Forbidden
    └─ Allow ▼
    │
Layer 2: SQL Firewall (at query execution)
    "Can this user access these specific tables/columns?"
    │
    ├─ Deny → Query denied with reason
    └─ Allow → Query executes
```

Both layers must allow for a query to succeed.

#### How restrictions combine

Two rules, one per layer:

- **Layer 1 (Cedar) is action-level and most-permissive.** Roles union: if any role
  the principal holds permits the action, it is allowed. Platform roles grant
  actions broadly — `platform-viewer` permits `query`, `translateQuery`,
  `searchDocuments`, `traverseGraph` and the read actions across *all* namespaces,
  and `platform-admin` permits every action on every resource.
- **Layer 2 (SQL firewall) is data-level and most-restrictive.** Restrictions
  **accumulate and never dissolve**:
  - `columnDenylist` — a column is denied if **any** grant on that namespace denies
    it.
  - `tableAllowlist` / `allowedMetrics` — the union of the lists that grants
    actually **declare**. A grant that declares none contributes no constraint; it
    does *not* remove the constraints other grants declared. Access is unrestricted
    only when **no** grant declares a list.

Restrictions are also **namespace-scoped**: they come only from grants on the
namespace being queried, and a `GLOBAL`-scoped platform grant never participates.

**No role bypasses layer 2 — including `platform-admin`.** This mirrors how the
rest of the system already works: an explicit `Deny` beats any `Allow` in IAM,
`forbid` beats `permit` in Cedar, and this project's own `default.cedar` states
that its "no mutations unless ACTIVE" invariant is *not* a role-overridable default
even for `platform-admin`.

##### What this means in practice

| Grants held on `ns-Z` | Effective data restrictions |
|---|---|
| `data-analyst` with `columnDenylist: {customers: [ssn]}` | `ssn` denied |
| the same, **plus** `platform-admin` (GLOBAL) | `ssn` still denied |
| the same, **plus** `namespace-owner` on `ns-Z` (no restrictions) | `ssn` still denied |
| `data-analyst` allowing `[customers]` + `reviewer` allowing `[orders]` | both tables allowed |
| `platform-admin` only, no grant on `ns-Z` | none — no grant declares any |

The third row is the one worth internalising: **adding a broader role does not
widen data access.** To lift a restriction, revoke or amend the grant that carries
it. That is deliberate — the alternative silently disabled PII denylists whenever a
principal also held an unrestricted role on the same namespace.

Note the last row: restrictions are *sparse opt-in*. A principal with no grant on a
namespace has nothing to derive restrictions from, so `platform-admin` alone reads
without data-level limits. Restrict by attaching a restricted grant, not by
expecting platform roles to be limited by default.

**Break-glass.** If an operator genuinely needs to read past a restriction, amend
or issue the grant explicitly. That is recorded in the grants table — durable,
attributable, and revocable — rather than waived by configuration. There is no
configuration switch that relaxes the merge: the restrictive semantics are not
operator-overridable.

**Declared-empty restrictions.** An explicitly empty `tableAllowlist` (or
`allowedMetrics`) on a grant means "nothing permitted" — a deny-all restriction —
and is distinct from omitting the field, which means "no constraint from this
grant." Deny-all grants are created via the API by passing `[]` explicitly; the
web console's blank field intentionally omits the field (unrestricted). Listings
return the declared-empty value so a deny-all grant is never mistaken for an
unrestricted one.

### List Namespace Grants

`GET /namespaces/{namespaceId}/grants` — see **ListNamespaceGrants** in the
[API Reference](#/api-reference).

#### Data-Level Restrictions in the Grants Table (Web UI)

The **Permissions** tab renders each grant in a table that includes three
columns surfacing the data-level restrictions described above:

| Column | Source field | When unrestricted shows | Otherwise shows |
|--------|--------------|-------------------------|-----------------|
| **Allowed tables** | `tableAllowlist` | a muted **All** label (no table restriction) | one blue badge per allowed table |
| **Allowed metrics** | `allowedMetrics` | a muted **All** label (no metric restriction) | one blue badge per allowed metric |
| **Denied columns** | `columnDenylist` | a muted **None** label (no column denied) | each table followed by its denied columns as red badges |

The muted **All** / **None** labels mean **full access** for that dimension —
they are not "no access". A populated cell lists the specific tables/metrics
that are allowed, or the columns that are denied, so reviewers can audit
least-privilege at a glance without opening each grant.

### Revoke a Namespace Grant

`DELETE /namespaces/{namespaceId}/grants/{grantId}` — see **DeleteGrant** in
the [API Reference](#/api-reference).

## Group-Based Grants

Instead of granting roles to individual users, you can grant to **IdP groups** using the same **CreateGrant** request with `principalType: "Group"`. Any user whose token contains that group (via the `groups` claim) inherits the role.

This is the recommended approach for teams — it keeps access in sync with your identity provider without manual per-user grants.

## Viewing Your Own Grants

Any user can view their own grants via `GET /principals/{principalId}/grants`
— see **ListPrincipalGrants** in the [API Reference](#/api-reference).

## Listing Available Roles

`GET /roles` lists platform roles (**ListPlatformRoles**);
`GET /namespaces/{namespaceId}/roles` lists namespace roles
(**ListNamespaceRoles**) — see the [API Reference](#/api-reference) for both.

## Common Patterns

### Onboarding a new team

1. Create a namespace for the team
2. Grant `data-steward` to the team lead (User grant)
3. Grant `data-analyst` to the team's IdP group (Group grant)
4. Optionally add data restrictions for sensitive tables

### Restricting access to PII

Grant `data-analyst` to the group (e.g. `junior-analysts`) with a
`columnDenylist` covering the sensitive columns (e.g. `customers.ssn`,
`customers.date_of_birth`, `employees.salary`) — the same **CreateGrant**
request shown under "Grant with Data-Level Restrictions" above, targeted at
a `Group` principal.

### Platform-wide read-only access for auditors

Grant `platform-viewer` to an auditors group via `POST /grants` with
`principalType: "Group"` — see **CreatePlatformGrant** in the
[API Reference](#/api-reference).

`platform-viewer` does **not** relax namespace-scoped data restrictions, so it
composes with the PII pattern above: an auditor who also holds `data-analyst` with
a `columnDenylist` can reach every namespace but still cannot read the denied
columns. See "How restrictions combine" above.
