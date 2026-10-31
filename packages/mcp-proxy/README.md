# COA MCP Proxy

Stdio bridge for connecting AI coding assistants (Kiro, Claude Desktop, Cursor) to the COA AgentCore MCP Runtime.

Handles Cognito PKCE authentication automatically: browser login on first run, silent token refresh after that (30-day sessions).

## How it works

```
IDE (stdio) → mcp-proxy (Streamable HTTP + Bearer JWT) → AgentCore MCP Runtime
```

1. Auto-discovers runtime config from SSM (`/coa/mcp/runtime-arn`, `/coa/mcp-client-id`, `/coa/issuer`)
2. Authenticates via Cognito PKCE (opens browser, no stored passwords)
3. Caches refresh token in `~/.scl/tokens.json` (600 permissions)
4. Launches `mcp-proxy` to bridge stdio to Streamable HTTP with the JWT

## Setup

### Prerequisites

- Node.js 18+
- [uv](https://docs.astral.sh/uv/) (for `uvx mcp-proxy`)
- AWS credentials that can read SSM parameters (any readonly role)

### Build

Both ways below produce `dist/index.js`. Use its absolute path in your MCP client config.

From a repo checkout (this package is part of the pnpm workspace):

```bash
pnpm install                              # from the repo root
pnpm --dir packages/mcp-proxy run build
```

From the source zip on the console's MCP Server page (no repo access needed):

```bash
unzip coa-mcp-proxy-src.zip
cd coa-mcp-proxy && npm install && npm run build
```

The zip carries a build-only `package.json`, generated from this package's `package.json` when the
web app is built. It keeps every runtime dependency and the `build` and `start` scripts, and adds only
`typescript` and `@types/node` for the build. The test toolchain is left out, so `npm install` works with
the npm that ships with Node 22. `tsx` stays in because the `start` script (`tsx src/index.ts`, which runs
the source without building) uses it; the steps above run `dist/index.js` and do not need it. The
`tests/` folder is in the zip for review only; run the tests from a repo checkout with
`pnpm --dir packages/mcp-proxy test`.

Each of those dependencies is pinned to the exact version recorded for this package in the repo's
`pnpm-lock.yaml`, the versions the repo builds and tests with. Their own dependencies are not pinned:
`npm install` resolves them from the npm registry when you run it. The SHA-256 on the MCP Server page
covers the zip, so it covers the source and the pinned direct versions, not the whole installed tree. It
shows the download is complete and intact; it is served from the same site as the zip, so it does not
prove who published it.

Use `npm run build`, not `npx tsc`: if TypeScript is not installed, `npx tsc` downloads an unrelated
npm package named `tsc` instead of failing.

### Configure Kiro

Add to `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "coa-dev": {
      "command": "node",
      "args": ["packages/mcp-proxy/dist/index.js"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "your-profile"
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

If Kiro reports `spawn node ENOENT`, replace `"node"` with the full path from `which node`.

### Environment Variables

All optional (auto-discovered from SSM if not set):

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region | `us-east-1` |
| `AWS_PROFILE` | AWS profile for SSM lookup | default credential chain |
| `SCL_PREFIX` | SSM parameter prefix | `/scl` |
| `SCL_CLIENT_ID` | Override Cognito client ID | from SSM |
| `SCL_ISSUER` | Override Cognito issuer URL | from SSM |
| `SCL_RUNTIME_ARN` | Override runtime ARN | from SSM |
| `SCL_RUNTIME_URL` | Override full invocations URL | built from ARN |

### First Run

On first connection, the proxy opens your browser to the Cognito login page. Sign in with your credentials. The token is cached for 30 days. No password is stored anywhere on disk.

### Token Refresh

The cached refresh token in `~/.scl/tokens.json` automatically renews the session. If it expires (after 30 days of inactivity), the browser login will open again.

To force re-login:

```bash
rm ~/.scl/tokens.json
```
