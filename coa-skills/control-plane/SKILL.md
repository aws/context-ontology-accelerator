---
name: control-plane
description: Drive a deployed COA (SemanticContextLayer) through its control-plane REST API from an agent - sign in with PKCE, scan and review sources, approve them, start induction, and review, accept, cancel or delete ontologies. Use when loading data into COA, building or rebuilding a namespace's ontology, getting a token for the REST API or MCP server without copying it from the browser, or debugging a source or proposal that is stuck. For sending questions to the serve runtime, read external-docs/content/serve.md.
version: 1.0.0
tags: [skill, control-plane, auth, cognito, pkce, sources, induction, ontology, coa]
---

# COA Control Plane

## Overview
The control plane is COA's REST API for everything before a question is asked: sources, steward review, induction, proposals, ontologies and grants. This skill explains the lifecycle and its traps, and ships `scripts/coa_login.py`, a sign-in helper that gets and refreshes tokens with PKCE so nobody copies a token out of browser DevTools.

## Usage
Use this skill when:
- Signing an agent in to the control-plane REST API or the MCP server.
- Connecting a Glue database or document source, then scanning, reviewing and approving it.
- Starting induction, then reviewing, accepting, cancelling or deleting the result.
- A source or proposal is stuck, or the live ontology still shows a class you removed.

For query requests and the SSE protocol, read `external-docs/content/serve.md`.

## Core Concepts

- **Docs first.** Read the page for the task in `external-docs/content/` before you act: `sources.md`, `ontologies.md`, `agent-access.md`, `role-permission-management.md`. API shapes are in `models/src/main/smithy/*.smithy`. Check the deployed version against `VERSION` and read the release notes for anything newer.
- **Lifecycle.** Scan a source, review its column notes and keys, approve it, then induce. Induction makes a proposal: a draft ontology plus R2RML mappings. Validate and accept it to make it live.
- **One open proposal per namespace.** A new induction returns 409 until the open one is accepted, rejected or cancelled.
- **Accept only adds.** Accepting merges into the live ontology. To rename or remove a class, delete the ontology and induce again.
- **Tables build the ontology.** "Table to Ontology" induction turns tables into classes. Documents build a separate document graph used for search answers.
- **Document formats.** Document sources read PDF, TXT, DOCX, MD and HTML. Load spreadsheets as Glue tables instead (see below).
- **Deleting files is not enough.** A re-scan of a document source never removes a file deleted from S3. Delete the source and create it again.
- **Busy sources.** COA refuses to delete a source while it is scanning (409). Wait for the scan to end.
- **Namespace tag.** Every Glue database, document bucket and JDBC secret needs the tag `<prefix>:namespace = <namespace ID>`. The prefix is the deployment's `resource_prefix`, `coa` by default.
- **Models.** Model IDs live in the SSM deploy config `/{prefix}/config`, not in the repo.
- **MCP is read-only.** The MCP server's 6 tools (`list_metrics`, `describe_schema`, `query`, `translate_sparql`, `rag_retrieval`, `graph_traversal`) cannot edit sources or ontologies. Edits go through this API.
- **Permissions.** `manageOntology` covers induce, edit, accept and delete. `data-steward` has it and `data-analyst` does not.

## Quick Start

COA has two public app clients. Use the one that matches the call:

| `--client` | Use for | Redirect |
|---|---|---|
| `web` | Control-plane REST API | `http://localhost:5173/authenticate/` |
| `mcp` | MCP server, or direct calls to the serve AgentCore runtime (not the REST API) | `http://localhost:9876/oauth/callback` |

Every REST endpoint, including the serve runtime's `POST /namespaces/{ns}/query`, takes `web` tokens only and rejects `mcp` tokens with 403 "explicit deny". Use `mcp` tokens with the MCP server, or when invoking the serve AgentCore runtime directly. The `web` redirect is registered only on `dev` deployments. On other environments, add it to the web client first.

Run these from the repository root. The script is at `coa-skills/control-plane/scripts/coa_login.py`.

1. Set three values. `COA_DOMAIN` is the OAuth base URL that serves `/oauth2/authorize` and `/oauth2/token`: the Cognito hosted-UI domain by default, or a custom domain in front of it. On a Cognito deployment, read all three from SSM (`<prefix>` is the deployment's `resource_prefix`):
   ```bash
   P=<prefix>; R=<region>
   export COA_DOMAIN=https://$(aws ssm get-parameter --name /$P/cognito-domain-prefix --query Parameter.Value --output text).auth.$R.amazoncognito.com
   export COA_WEB_CLIENT_ID=$(aws ssm get-parameter --name /$P/userpool-client-id --query Parameter.Value --output text)
   export COA_MCP_CLIENT_ID=$(aws ssm get-parameter --name /$P/mcp-client-id --query Parameter.Value --output text)
   ```
2. Sign in once. The browser must run on the same machine as the script:
   ```bash
   python3 coa-skills/control-plane/scripts/coa_login.py login --client web
   python3 coa-skills/control-plane/scripts/coa_login.py status --client web   # expiry only, never tokens
   ```
   Tokens are cached in `~/.coa/tokens-web.json` and `~/.coa/tokens.json` (mode 600). Pass `--file` to put either somewhere else. `COA_TOKEN_FILE` changes the `mcp` cache only.
3. Call the API with a fresh ID token. The helper refreshes it on its own, and tells you to sign in again when the refresh token has expired:
   ```bash
   TOKEN=$(python3 coa-skills/control-plane/scripts/coa_login.py token --client web)
   curl -s -H "Authorization: Bearer $TOKEN" "$API/namespaces/$NS/sources" | jq '.items[]'
   ```
   In Python, with the scripts folder on `sys.path`: `from coa_login import get_token; get_token("id", client="web")`.

Common calls (prefix each path with `$API/namespaces/$NS`):

| Step | Call | List key |
|---|---|---|
| List sources | `GET /sources` | `items`, paged with `nextToken` |
| Delete a source | `DELETE /sources/{sourceId}` | |
| Start induction | `POST /induce` with `{"datasource_ids": [...], "ontology_uri_prefix": "...", "strategy": "table_to_ontology"}` | |
| Check induction | `GET /induce/jobs/{jobId}` | |
| List or read proposals | `GET /proposals`, `GET /proposals/{proposalId}` | `proposals`, paged with `nextToken` |
| Accept or cancel | `POST /proposals/{proposalId}/accept`, `POST /proposals/{proposalId}/cancel` | |
| List ontologies | `GET /ontologies` | `ontologies`, paged with `nextToken` |
| Delete an ontology | `DELETE /ontologies?ontology_id=<URL-encoded IRI>` (202; poll `GET /ontologies` until it is gone) | |

Response shapes are in `models/src/main/smithy/`. A page can be short or empty while `nextToken` is set, so keep paging until it is absent. The induction, proposal and ontology endpoints take snake_case request keys, as shown above.

Before deleting an ontology, save it with `GET /ontologies/{ontologyId}/download`.

## Load spreadsheets as tables

1. Save each sheet as a CSV: one header row, unique column names, no merged cells, no line breaks in cells.
2. Put each CSV in its own S3 folder, such as `curated/<table>/data.csv`.
3. Create one Glue table per folder with OpenCSVSerde and `skip.header.line.count = 1`. A Glue crawler picks a CSV format that splits values containing commas.
4. Tag the Glue database with the namespace tag.
5. Check each table's row count in Athena before connecting it to COA.

## Common errors

| Symptom | Cause | Fix |
|---|---|---|
| 403 "explicit deny" on the REST API | Token from the `mcp` client | Sign in with `--client web` |
| "sign-in expired" from `token` | Refresh token expired (12 hours for `web`, 30 days for `mcp`) | Run `login` again |
| Empty list from `GET /sources` | Reading `.sources` | The list is under `items` |
| 409 on `POST /induce` | A proposal is still open | Accept, reject or cancel it |
| 409 on source delete | Source is scanning | Wait for the scan to end |
| A removed class still shows | Accept only adds | Delete the ontology and induce again |
| A deleted document still answers | Re-scans don't remove files | Delete and re-create the document source |
| 400 on source create | Missing namespace tag | Tag the database, bucket or secret |
| Answers cover the whole dataset | The question names no entity | Name a specific customer, product or order |

## Files
- `scripts/coa_login.py` — PKCE sign-in helper for both app clients, with token caching (mode 600) and automatic refresh. Standard library only.
