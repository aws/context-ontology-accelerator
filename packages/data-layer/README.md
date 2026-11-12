# Data Layer

REST API for programmatic query/retrieval. Thin Lambda adapter that forwards requests to the Context Manager via AgentCore Runtime.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/namespaces/{namespaceId}/query` | Unified NL query (tiered resolution) |
| POST | `/namespaces/{namespaceId}/translate` | NL-to-SPARQL translation only |
| POST | `/namespaces/{namespaceId}/kb/search` | Vector search (OpenSearch) |
| POST | `/namespaces/{namespaceId}/graph/traverse` | Graph traversal (Neptune) |
| GET  | `/namespaces/{namespaceId}/schema` | Schema discovery, returns queryable classes and properties |

## How it works

```
API Client → API Gateway (JWT + Cedar) → Lambda → AgentCore Runtime → Context Manager
```

The Lambda validates input, forwards the caller's access token to AgentCore via HTTPS, and returns the response. No business logic lives here.

## Auth

Use a Cognito **access token** (not ID token) — AgentCore validates `client_id`.

Cedar actions: `query` (query/translate/traverse), `searchDocuments` (kb/search), `viewNamespace` (schema).

## Errors

| Error | HTTP | Meaning |
|-------|------|---------|
| `QueryTranslationError` | 400 | NL-to-SPARQL failed |
| `DataSourceUnavailableError` | 502 | Backend unreachable/timeout |
| `AccessDeniedError` | 403 | SQL firewall deny |

## Dev

```bash
uv run ruff check packages/data-layer                    # lint
uv run mypy packages/data-layer/src --ignore-missing-imports  # type check
pnpm --filter coa-infra exec cdk deploy coa-dev-data-layer coa-dev-api --require-approval=never  # deploy
```

## Related files

- `models/src/main/smithy/serve.smithy` — API contract (operations + shapes)
- `models/src/main/smithy/data-layer.smithy` — Service definition
- `infra/lib/stacks/services/data-layer-stack.ts` — CDK stack
- `packages/context-manager/src/coa_serve/main.py` — Action handlers
