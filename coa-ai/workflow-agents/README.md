# coa-workflow-agents

A self-hosted MCP server whose agents drive a deployed COA through its REST API, with the caller's
own token. It exposes one tool, `run_workflow`; today the supervisor answers status questions about
a namespace. Running and calling it: `coa-ai/skills/workflow-agents/SKILL.md`.

## Layout

| Module | What it does |
|---|---|
| `server.py`, `__main__.py` | The MCP server (`python -m coa_workflow_agents`): verifies each call's token, builds a supervisor per call, runs it on a worker thread with a deadline and progress. |
| `agents/` | The supervisor (`supervisor.py`) and the Bedrock model settings (`model.py`). |
| `intervention.py` | The gate that denies a mutating tool unless `confirm_mutations` names it. |
| `config.py` | Settings from the environment (see the SKILL's Configuration table). |
| `log_policy.py`, `observability.py` | The fail-closed log policy and the run's log events. |
| `tools/status.py` | The supervisor's read-only `get_namespace_status`. |
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
- **New log events.** Every event the package logs must be declared in `log_policy.EVENT_FIELDS`,
  with its fields; a unit test fails otherwise. Never log caller data.
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
`tests/integ/test_local_server.py` starts the server on loopback against the deployed API and makes
one read-only run on Bedrock, so it needs AWS credentials that can call Bedrock.
