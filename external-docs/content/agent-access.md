# Agent Access Guide

Context Ontology Accelerator supports programmatic access from AI agents and
automation tools. Agents **always act on behalf of a user** using the same
OIDC three-legged (3LO) auth flow as the web app — there are no separate
machine-to-machine credentials. An agent carries the user's delegated token,
and every request is authorized against that user's grants.

## Overview

There are two ways agents connect, both under the same delegated-auth model:

- **MCP (Model Context Protocol)** — the primary path for AI agents (Claude,
  Amazon Q, IDE integrations like Kiro/Claude Desktop/Cursor). Ontology
  Accelerator runs an MCP server on AgentCore Runtime exposing 6 tools —
  `list_metrics` and `describe_schema` for discovery, plus `query`,
  `translate_sparql`, `rag_retrieval`, and `graph_traversal` for execution.
  See "MCP Server Setup" below.
- **Direct REST API** — for scripts, notebooks, or custom automation that
  isn't going through an MCP client. Uses the same endpoints the web app
  calls; see "Making API Calls" below.

Either way, an agent needs:

1. A valid OIDC token obtained on behalf of a user via the 3LO Authorization
   Code flow (with PKCE)
2. The acting user to have the necessary grants in the system (User- or
   Group-based, same as any human user)

The agent never has its own standalone identity or permissions — it inherits
exactly what the user it represents is allowed to do.

!!! tip "Full request/response schemas"
    For MCP tool schemas, connect an MCP client and let it introspect the
    server. For the direct REST API, see the
    **[API Reference](#/api-reference)** — Control Plane API for grants,
    metrics, sources, etc., and Data Layer (Serve) API for `Query` and the
    other runtime operations. This guide covers authentication and grant
    setup specific to agents; those references are the source of truth for
    schemas.

## MCP Server Setup

The MCP server runs on AgentCore Runtime, reachable over **Streamable HTTP**.
Configure your MCP client (Claude Desktop, Kiro, Cursor, etc.) with:

- The MCP server's AgentCore Runtime endpoint (from the `<prefix>-<env>-mcp` stack outputs)
- The user's Bearer token: an **ID token issued to the MCP/CLI app client**
  (`McpClientId`), obtained via the same OIDC 3LO + PKCE flow described below

For the MCP/CLI flow specifically, the platform provisions a dedicated public
client with a `localhost:9876/oauth/callback` redirect URI (see the
[Authentication Setup Guide](authentication-setup.md)) so IDE integrations can
complete the PKCE login without a server-side secret.

### Which token to send

| You call | Send | Read the client ID from |
|---|---|---|
| The MCP server | ID token from the MCP/CLI client | `McpClientId` (auth stack output), or SSM `/<prefix>/mcp-client-id` |
| The REST API | ID token from the web app client | `UserPoolClientId`, or SSM `/<prefix>/userpool-client-id` |

The MCP server rejects any other token with HTTP 401, starting with the
first request a client sends. That includes an ID token from the web app
client, and a Cognito access token from either client, because Cognito access
tokens carry no `aud` claim. On a deployment that uses an external OIDC
provider, both interfaces use the same client ID.

Each tool call is authorized exactly like a REST call: the server extracts
the Bearer token from the request, forwards it to the same authorizer path,
and the acting user's grants determine what the tool can do — a `data-analyst`
grant restricted to certain tables via `tableAllowlist` restricts `query` tool
calls the same way it would restrict a direct API call.

## Obtaining a Token (3LO, on behalf of a user)

Agents obtain tokens through the standard Authorization Code flow with PKCE,
the same flow the web app uses. The user authenticates once (interactively),
and the agent uses the resulting tokens to call the API on their behalf.

1. The user completes the OIDC Authorization Code + PKCE login, authorizing the
   agent.
2. The agent exchanges the authorization code for an ID token, access token,
   and refresh token.
3. The agent calls the API with the user's bearer token, refreshing it with the
   refresh token as needed.

In the examples below, `CLIENT_ID` is the MCP/CLI client ID for a token you
send to the MCP server, or the web app client ID for the REST API (see
[Which token to send](#which-token-to-send)), and `id_token` is the token to
send.

```bash
# Example: exchange an authorization code obtained via the 3LO flow
TOKEN=$(curl -s -X POST "https://your-domain.auth.us-east-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&client_id=CLIENT_ID&code=AUTH_CODE&redirect_uri=REDIRECT_URI&code_verifier=PKCE_VERIFIER" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id_token'])")
```

When the token nears expiry, refresh it instead of re-prompting the user:

```bash
TOKEN=$(curl -s -X POST "https://your-domain.auth.us-east-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token&client_id=CLIENT_ID&refresh_token=REFRESH_TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id_token'])")
```

## Granting Access

Because an agent acts on behalf of a user, you do **not** grant access to the
agent itself — you grant access to the user (or an IdP group the user belongs
to), via `POST /namespaces/{namespaceId}/grants` — see **CreateGrant** in the
[API Reference](#/api-reference) and the
[Role & Permission Management Guide](role-permission-management.md) for
assigning roles.

For restricted access (the user — and therefore the agent — can only query
specific tables), add `tableAllowlist` / `allowedMetrics` to the same grant
request.

## Making API Calls (Direct REST, without MCP)

For agents or scripts not using an MCP client, call the REST API directly
with the user's bearer token — see the [API Reference](#/api-reference) for
every available endpoint (metrics, sources, ontologies, grants under the
Control Plane API; `Query` and the other runtime operations under the Data
Layer API).

## Auditing Agent Actions

All agent calls — MCP tool invocations and direct REST calls alike — pass
through the same authorizer as direct human access. Because the agent acts
on behalf of a user, the audited `principalId` is the acting user's identity.
The authorizer logs every decision with:

- `principalId` — the acting user's identity
- `effect` — Allow/Deny
- `globalRoles` / resource roles

Query CloudWatch Logs Insights:

```
fields @timestamp, principalId, effect, resource
| filter principalId = "alice@company.com"
| sort @timestamp desc
| limit 50
```

For data-level access auditing (which tables/columns were queried), check the
context-manager logs which record SQL Firewall decisions.

## Best Practices

| Practice | Rationale |
|----------|-----------|
| Scope grants to the acting user | The agent inherits exactly the user's permissions — keep them least-privilege |
| Apply `tableAllowlist` on grants | Agents rarely need every table the user can otherwise reach |
| Refresh tokens instead of re-prompting | Use the refresh token for long-running workflows |
| Monitor DLQs and error rates | Agent failures are silent without monitoring |
| Grant to Groups when possible | Manage access via IdP group membership |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `401` on API call | Token expired or malformed | Refresh the user's token; verify the issuer/`authority` |
| `403` on API call | The acting user has no grant | Create a grant for the user's principal ID |
| Token expires mid-workflow | No refresh handling | Use the refresh token to renew the user's token |
| Agent can authenticate but queries return empty | Data-level restrictions too narrow | Check `tableAllowlist` and `allowedMetrics` on the user's grant |
