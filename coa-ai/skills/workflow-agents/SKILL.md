---
name: workflow-agents
description: Run and call the self-hosted coa-workflow MCP server, whose single `run_workflow` tool drives a deployed COA (SemanticContextLayer) in plain language with the caller's own token. Use when starting the server locally, connecting an MCP client to it, asking it for a namespace's status, running ontology induction through it, or when a `run_workflow` call was refused or failed. For direct REST calls and sign-in, use the control-plane skill; for asking questions of the data, use the query MCP server.
version: 0.2.0
tags: [skill, workflow-agents, mcp, run-workflow, coa]
---

# COA Workflow Agents

## Overview
coa-workflow is an MCP server you run yourself, on your machine or in your own infrastructure. It exposes one tool, `run_workflow`: you describe what you want in plain language, a supervisor agent works it out against the COA REST API, and the reply summarizes the result. Every COA call carries your own ID token, so it can do only what your COA grants allow. For now it answers status questions about a namespace (its sources, induction jobs and proposals) and runs ontology induction over approved sources; the onboarding, acceptance, ontology, metrics and admin agents arrive in later releases.

## Usage
Use this skill when:
- Starting the server, or connecting an MCP client to it.
- You want the status of a namespace's sources, induction jobs and proposals in one answer.
- You want to induce an ontology from approved sources, check an induction job, or list the sources already in an accepted ontology.
- A `run_workflow` call was refused, failed or timed out.

Do not use it when:
- The user wants an answer from the data: that is the query MCP server's job.
- One REST read answers the question: a `run_workflow` call costs an agent run, a `GET` does not (see `coa-ai/skills/control-plane/SKILL.md`).

## Core Concepts
- **Your token, your grants.** The server holds no credentials. It accepts only an ID token from the COA web app client (the client the REST API accepts) and forwards it on every COA call. An access token, or a token from the MCP app client, is refused.
- **One namespace per call.** `namespace_id` is required: the namespace's lowercase UUID v4. The instruction cannot switch it.
- **Names authorize, prose does not.** `confirm_mutations` takes exact mutating tool names, or `["*"]` for all. Omit it for a read-only run. "Yes, do it" in the instruction authorizes nothing, and an unknown name fails the call before anything runs. This release has one mutating tool, `start_induction`.
- **Never auto-approve `run_workflow`.** Keep it off every `autoApprove` list, and show the user each call's `confirm_mutations` before it runs.
- **Private by default.** The server listens on `127.0.0.1`. In a container, publish the port to `127.0.0.1` only.

## Quick Start
1. Configure the server from the deployment's SSM parameters (`<prefix>` is its `resource_prefix`):
   ```bash
   export SCL_API_ENDPOINT=$(aws ssm get-parameter --name /<prefix>/api-endpoint --query Parameter.Value --output text)
   POOL=$(aws ssm get-parameter --name /<prefix>/userpool-id --query Parameter.Value --output text)
   export JWT_ISSUER_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL}"
   export JWT_CLIENT_ID=$(aws ssm get-parameter --name /<prefix>/userpool-client-id --query Parameter.Value --output text)
   ```
   The server also needs AWS credentials that can call Bedrock in `AWS_REGION` (the model runs as you, not as the caller). On a direct-IdP deployment, set `JWT_ISSUER_URL` to the IdP's issuer and `JWT_CLIENT_ID` to its single client.
2. Start it from a checkout (`uv sync --all-packages` first), or as a container:
   ```bash
   uv run python -m coa_workflow_agents          # serves http://127.0.0.1:8000/mcp
   # or
   docker build -f coa-ai/workflow-agents/Dockerfile -t coa-workflow-agents .
   docker run --env-file workflow-agents.env -p 127.0.0.1:8000:8000 coa-workflow-agents
   ```
3. Get a web-client ID token with the control-plane skill's helper:
   ```bash
   TOKEN=$(python3 coa-ai/skills/control-plane/scripts/coa_login.py token --client web)
   ```
4. Connect your MCP client to `http://127.0.0.1:8000/mcp` (Streamable HTTP) with the header `Authorization: Bearer $TOKEN`, and a request timeout of at least 60 minutes. Then:
   ```python
   run_workflow(
       instruction="What is the status of this namespace?",
       namespace_id="abcdef01-2345-4678-89ab-cdef01234567",
   )
   ```

## Configuration
Read from the environment when the server starts. A missing required setting stops it at startup, with `server_config_missing` naming the variables.

| Variable | Purpose | Default |
|---|---|---|
| `SCL_API_ENDPOINT` | Base URL of the COA REST API | required |
| `JWT_ISSUER_URL` | Issuer every caller token must carry | required |
| `JWT_CLIENT_ID` | The only accepted token audience: the web app client | required |
| `AWS_REGION` | Region for Bedrock (and of the Cognito issuer you build) | `AWS_DEFAULT_REGION`, else `us-east-1` |
| `GROUP_CLAIM_NAME` | Group claim on a non-Cognito IdP | `groups` |
| `SCL_MCP_HOST` | Listen address | `127.0.0.1` (the container sets `0.0.0.0`; publish to `127.0.0.1` only) |
| `SCL_MCP_PORT` | Listen port | `8000` |
| `SCL_MCP_ALLOWED_HOSTS` | Extra `Host` header values to accept (`name`, `name:port` or `name:*`), for a server reached under your own hostname. Any other host or a non-loopback `Origin` is refused, so a page that rebinds a hostname to your machine cannot use the server | empty: loopback only (`127.0.0.1`, `localhost`, `[::1]`, any port). `*` is refused |
| `SCL_ALLOW_CORS_ORIGINS` | Comma-separated browser origins allowed cross-origin, for a local browser MCP client | empty (no CORS). Only exact `http://127.0.0.1:<port>`, `http://localhost:<port>` or `http://[::1]:<port>`; `*` or any other origin stops the server at startup |
| `SCL_RUN_DEADLINE_S` | Run deadline in seconds; a hung call fails 120 s later | `3300` |
| `SCL_BEDROCK_MODEL_ID` | Bedrock model or inference profile | empty: the Strands default model |
| `SCL_BEDROCK_TEMPERATURE` | Model temperature | unset: not sent (newer models reject it) |
| `SCL_GUARDRAIL_ID`, `SCL_GUARDRAIL_VERSION` | A Bedrock guardrail of your own on every model call | none; the version defaults to `DRAFT` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` | `INFO` |

A `workflow-agents.env` for `docker run --env-file`, with the values from step 1 of the Quick Start:
```
SCL_API_ENDPOINT=https://<api-id>.execute-api.<region>.amazonaws.com/<stage>
JWT_ISSUER_URL=https://cognito-idp.<region>.amazonaws.com/<user-pool-id>
JWT_CLIENT_ID=<web app client id>
AWS_REGION=<region>
```
The container also needs AWS credentials for Bedrock: pass `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` with `-e`, or run it where an instance or task role provides them. Never put them in the env file you share.

## The call contract
```python
run_workflow(instruction: str, namespace_id: str, confirm_mutations: list[str] | None = None) -> str
```
- The reply's first line is `Namespace: <id>`, then the supervisor's answer.
- Progress notifications stream while it runs. The first is `run <run_id>`; quote that id when asking about a run. Then come a `still working` heartbeat every 30 s, the state of any COA job a tool waits on, and `calling <AgentName>` (e.g. `calling InductionAgent`) on each hand-off to a sub-agent. That message is sent as the sub-agent starts, not when it finishes.
- Polls stop a minute before the run deadline (`SCL_RUN_DEADLINE_S`, default 3300 s, or your token's expiry less a minute when that is sooner).

## Running induction
Induction is a real, billable Bedrock job over sources that are already `APPROVED`; the call waits for it up to about 54 minutes by default (the run deadline less a minute); a longer job is reported as `still_running` and keeps running in COA. Name `start_induction` in `confirm_mutations`:
```python
run_workflow(
    instruction="Induce an ontology from source <sourceId>",
    namespace_id="abcdef01-2345-4678-89ab-cdef01234567",
    confirm_mutations=["start_induction"],
)
```
- The reply names the job id, its final status and the proposal to review. Accepting the proposal is not in this release: use the web app or the REST API.
- The strategy defaults to `table_to_ontology` and the URI prefix to the namespace's own; ask for another only when you need it. The document-to-table bridge (`bridge_to_tables`) runs on an already accepted ontology and reads no sources.
- `start_induction` needs COA v0.3.9 or later: it sends camelCase field names, which earlier releases reject with a 422.
- Naming `start_induction` in `confirm_mutations` authorizes it for the whole run, not for one call. The agent is told to start one job and not to repeat it, but only that instruction stops a second start; COA's one-job-per-namespace rule bounds the cost.
- A listing (`list_induction_jobs`, `list_induced_datasources`) returns at most 500 entries and says `truncated` when more may exist (narrow `list_induction_jobs` with a status; the sources list has no filter).
- COA runs one induction at a time per namespace, and refuses a new one while an earlier induction's proposal is still unreviewed. The reply names the running job or the proposal.
- When the run deadline arrives first, the reply says the job is still running and names it. It was not cancelled: ask "get the status of induction job <jobId>" in a later, read-only call. Never start another to check on it.

## Errors
| Error | Cause | Fix |
|---|---|---|
| `Missing Authorization header` | No bearer token sent | Send `Authorization: Bearer <id token>` |
| `audience mismatch` | Token from the MCP app client, or another client | Sign in with `--client web` |
| `only an ID token` | An access token | Send the ID token |
| `expires within a minute` | The token is about to expire | Fetch a fresh one and retry |
| `namespace_id must be a COA namespace id` | Not a lowercase UUID v4 | Use the id COA issued |
| `confirm_mutations names unknown mutating tool(s)` | A typo, or a tool this release does not have | Use a listed name, or omit it |
| The reply says `start_induction` was denied | `confirm_mutations` did not name it | Call again with `confirm_mutations=["start_induction"]` |
| `Workflow failed (<Class>)` | The run raised; only the class is shown | Retry; check the server's log by `run_id` |
| `Workflow timed out after <N>s` | The run passed its deadline (`SCL_RUN_DEADLINE_S`, or the token's expiry) plus 120 s | Check the namespace's state before retrying: COA operations it started may still be running. Then raise `SCL_RUN_DEADLINE_S` or use a fresher token, and keep the client's request timeout at least that long |
| `Missing required environment variable(s)` at startup | Server started without its configuration | Set the variables in step 1 |
