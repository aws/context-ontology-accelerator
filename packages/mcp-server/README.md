# MCP Server

- **Owner**: avijitg
- **LLD**: Serve LLD §3.2.5 (Story 5)
- **Work Item**: #128 (tool definitions + auth), #129 (AgentCore Runtime deployment)
- **Status**: In Progress

## Overview

Exposes Context Ontology Accelerator capabilities as 6 discoverable MCP tools for AI agents via Streamable HTTP
on AgentCore Runtime (`--protocol MCP`, port 8000).

**Architecture**: Thin protocol adapter on a separate AgentCore Runtime.
- Discovery tools (list_metrics, describe_schema) invoke backend Lambdas directly via `lambda:InvokeFunction`
- Execution tools (query, translate_sparql, rag_retrieval, graph_traversal) delegate to the Context Manager via its AgentCore HTTP invocations endpoint
- MCP server handles: MCP protocol framing, JWT extraction, pre-auth (Cedar), audit logging
- Context Manager handles: orchestration, data access (Neptune, Athena, OpenSearch, Bedrock)

The MCP server does NOT run the orchestrator in-process. It has minimal direct data access.
Needs DynamoDB (role resolution), Lambda invoke (discovery), and HTTPS egress (to invoke the CM).

## Tools

| Tool | Category | Description |
|------|----------|-------------|
| `list_metrics` | Discovery | List governed metric definitions (name, description, dimensions) |
| `describe_schema` | Discovery | Describe ontology classes, properties, and data source tables |
| `query` | Execution | End-to-end NL query with tiered resolution |
| `translate_sparql` | Execution | Translate NL to SPARQL using the published ontology |
| `rag_retrieval` | Execution | Retrieve semantically similar document chunks |
| `graph_traversal` | Execution | Traverse the semantic graph for entity relationships |

## Running Locally

```bash
# Full mode (needs deployed metric-service + ontology-engine Lambdas + Context Manager)
SCL_AUTH_ENABLED=false \
CM_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/your_cm_runtime \
METRIC_SERVICE_LAMBDA_ARN=arn:aws:lambda:us-east-1:123456789012:function:coa-dev-metric-api \
ONTOLOGY_PROXY_LAMBDA_ARN=arn:aws:lambda:us-east-1:123456789012:function:coa-dev-ontology-api-proxy \
  uv run python -m coa_mcp

# Test with MCP Inspector
npx @modelcontextprotocol/inspector uv run python -m coa_mcp
```

## IDE Integration (Kiro / Claude Desktop)

Connect your IDE to the deployed MCP server using `mcp-proxy` as a stdio-to-streamable-http bridge.

### Prerequisites

- Node.js 18+ (the proxy runs on Node)
- `uvx` installed (`pip install uv`) — the proxy spawns `uvx mcp-proxy` internally
- AWS credentials for this account on your default profile (any read-only role that can read SSM)
- Deployed `coa-dev-mcp` stack

### How it works

```
IDE (stdio) --> mcp-proxy (Cognito PKCE + Bearer JWT) --> HTTPS --> AgentCore Runtime --> MCP Server
```

The `packages/mcp-proxy` bridge handles authentication itself — there is no token
script. On first run it opens a browser for a Cognito PKCE login (no password
stored), caches a refresh token in `~/.scl/tokens.json`, and keeps you signed in
for 30 days. Each request is authenticated as **your own** Cognito identity, so
Cedar grants and the audit log reflect you — not a shared account. It
auto-discovers the runtime ARN, MCP client id, and issuer from SSM under the
deployment's resource prefix (default `/coa`).

> Earlier revisions documented two shell scripts (`get-token.sh` /
> `start-mcp-proxy.sh`) that minted a Cognito token via `USER_PASSWORD_AUTH` and
> fell back to the shared `*-integ-test-user` secret. That path is removed: it
> signed everyone in as the shared test user (wrong identity + audit) and only
> worked in `dev` (where `USER_PASSWORD_AUTH` is enabled). Use the PKCE proxy
> below instead.

### Step 1: Build the proxy

`coa-mcp-proxy` is an in-repo package (not yet published), so build it from a
checkout and use the absolute path to the built entrypoint:

```bash
cd packages/mcp-proxy
npm install
npx tsc --outDir dist
# use the absolute path printed by:
echo "$PWD/dist/index.js"
```

### Step 2: Configure your IDE

Add to `.kiro/settings/mcp.json` (or Claude Desktop's `claude_desktop_config.json`),
pointing `command` at the `dist/index.js` path from Step 1:

```json
{
  "mcpServers": {
    "coa": {
      "command": "node",
      "args": ["/absolute/path/to/mcp-proxy/dist/index.js"],
      "env": {
        "AWS_REGION": "us-east-1",
        "RESOURCE_PREFIX": "coa"
      },
      "autoApprove": [
        "list_metrics",
        "describe_schema",
        "query",
        "translate_sparql",
        "rag_retrieval",
        "graph_traversal"
      ]
    }
  }
}
```

Claude Code registers the server with one command instead of an edited file:

```bash
claude mcp add coa -- \
  env AWS_REGION=us-east-1 RESOURCE_PREFIX=coa \
  node /absolute/path/to/mcp-proxy/dist/index.js
```

Do **not** set `AWS_PROFILE` to a profile you don't have — the proxy uses the
default credential chain, and a bogus profile breaks SSM discovery.

### Step 3: Verify

Reconnect the `coa` server from the IDE's MCP panel (Kiro: Cmd+Shift+P →
"Reconnect MCP Servers"), or just invoke a tool. The first call opens a browser
for the Cognito login; after you sign in, `~/.scl/tokens.json` is written and the
tools become available. Ask the agent to run `list_metrics` for a namespace you
can access — a working setup returns the governed metrics.

### Token / session lifetime

The PKCE session refreshes silently for 30 days. If it lapses after long
inactivity, the next call reopens the browser login. To force a fresh login,
delete `~/.scl/tokens.json`.

See `packages/mcp-proxy/README.md` for the proxy's full option/override reference.

## Auth Model

- **Inbound JWT**: Validated by AgentCore Runtime platform (not re-validated by this server)
- **Authorization**: Cedar policies evaluated against the **delegating user** (not the agent)
- **Agent identity**: Logged for audit, never used for authorization decisions
- **Profile resolution**: User's grant resolved to `InvokeRequest.profile` before execution
- **Fail closed**: Empty/missing grant = DENY (not unrestricted access)

## Cedar Policy Configuration

Cedar evaluation is wired into the grant resolver. When `ROLES_TABLE_NAME` is set,
the server loads Cedar policies from DynamoDB and evaluates them before tool execution.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ROLES_TABLE_NAME` | Yes (prod) | DynamoDB table containing Cedar policies per role. When empty, Cedar evaluation is skipped (local dev). |
| `RRM_TABLE_NAME` | Yes (prod) | ResourceRoleMappings table for role resolution. |
| `AWS_REGION` | Yes | AWS region for DynamoDB access. |

### Tool to Cedar Action Mapping

| MCP Tool | Cedar Action | Required Role |
|----------|-------------|---------------|
| `list_metrics` | `viewNamespace` | data-analyst, data-steward, namespace-owner |
| `describe_schema` | `viewNamespace` | data-analyst, data-steward, namespace-owner |
| `query` | `query` | data-analyst, data-steward, namespace-owner |
| `translate_sparql` | `query` | data-analyst, data-steward, namespace-owner |
| `rag_retrieval` | `searchDocuments` | data-analyst, data-steward, namespace-owner |
| `graph_traversal` | `viewNamespace` | data-analyst, data-steward, namespace-owner |

### Backward Compatibility

- Cedar evaluation is **optional**. When `ROLES_TABLE_NAME` is empty (local dev, tests without DDB), Cedar evaluation is skipped entirely.
- The Playground/SSE path (context-manager) does not pass `cedar_action`, so Cedar evaluation does not apply to it.

For Cedar policy syntax and schema, see `libs/common/src/coa_authorization/seed/`.

## Integration Tests

Integration tests validate the MCP server against a live deployed AgentCore Runtime.

### Prerequisites

- Deployed `coa-dev-mcp` stack (CDK)
- Valid Cognito user tokens
- Network access to the AgentCore endpoint

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_RUNTIME_ARN` | Yes | Deployed MCP AgentCore Runtime ARN (from `coa-dev-mcp` stack outputs) |
| `MCP_BEARER_TOKEN` | Yes | Valid Cognito JWT for a user WITH namespace access |
| `MCP_NO_ACCESS_TOKEN` | No | Valid Cognito JWT for a user WITHOUT namespace grants (for authz denial tests; skipped if unset) |
| `MCP_TEST_NAMESPACE` | Yes | Namespace ID the bearer token user has access to |
| `AWS_REGION` | No | Defaults to `us-east-1` |

### Running

```bash
export MCP_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/..."
export MCP_BEARER_TOKEN="eyJ..."
export MCP_TEST_NAMESPACE="550e8400-..."
export MCP_NO_ACCESS_TOKEN="eyJ..."  # optional

uv run pytest packages/mcp-server/tests/integ -m integ -v --tb=short
```
