# coa-workflow-agents

The client layer of the COA workflow agents: a REST client for the COA Control Plane API and the
helpers the agents' tools share. There is no agent, server or runtime in the package yet.

## Layout

| Module | What it does |
|---|---|
| `auth/caller.py` | The verified caller, and `CallerTokenProvider`, which forwards their ID token. It refuses the token within 60 s of expiry, since a forwarded token cannot be refreshed. |
| `clients/coa_api_client.py` | `CoaApiClient`: one method per REST operation the agents call, plus presigned S3 upload and download. Raises `CoaApiError` on an HTTP error; transport errors propagate as `requests` exceptions. |
| `tools/polling.py` | `poll_to_terminal`: polls an async COA job to one outcome, retrying transient poll failures with backoff. |
| `run_context.py` | The per-run deadline and progress sink that `poll_to_terminal` reads. |
| `tools/_shared.py` | Response-shape and paging helpers, a sandbox for model-supplied file paths, and presigned-URL redaction. |

## Rules worth knowing

- **The caller's token, always.** The client holds no credentials; every call carries the token its
  `token_provider` returns, so COA applies the caller's own grants.
- **Retries.** Only POSTs that carry a `clientToken` are retried: a timeout, a connection error or a
  503 is retried with the same token, which COA de-duplicates, and a 409 saying the token's first
  request is still running is waited out. Every other call is sent once.
- **New endpoints.** A new request path's literal words must be added to `_ROUTE_WORDS` (logged routes
  keep only those words), and the route must be added to `tests/routes.py`. Unit tests fail until both are done.
- **Handler field names.** Several handlers read snake_case names that differ from the Smithy model.
  The method docstrings name each one; keep them when you change a body.

## Tests

```bash
uv run pytest coa-ai/workflow-agents/tests/unit -m unit -q
# Against a deployed environment, with AWS credentials (see AGENTS.md for the variables):
uv run pytest coa-ai/workflow-agents/tests/integ -m integ --import-mode=importlib
```

`tests/integ/test_route_drift.py` probes every route in `tests/routes.py` against the deployed API.
`tests/integ/test_client_smoke.py` drives `CoaApiClient` with a forwarded token.
