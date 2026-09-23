# Context Ontology Accelerator — Web App

- **Owner**: TBD
- **Status**: Active

## Overview

The web app is a React SPA providing the management console for the Context Ontology Accelerator. It handles namespace lifecycle, source onboarding/review, ontology visualization, metric management, permissions management, and an interactive query playground.

Built on the [Cloudscape Design System](https://cloudscape.design/) and communicates with the backend via the Smithy-generated `@coa/control-plane-client` and a generic `ApiClient` (for endpoints not yet in the Smithy model).

## Getting Started

### Prerequisites

- Node.js ≥ 18
- npm (workspace root manages dependencies)

### Install

From the repo root:

```bash
npm install
```

### Configure

Copy the example runtime config and fill in your dev environment values:

```bash
cp packages/web-app/public/runtime-config.example.json packages/web-app/public/runtime-config.json
```

Edit `public/runtime-config.json`:

```json
{
  "region": "us-east-1",
  "authority": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX",
  "clientId": "your-oauth-client-id",
  "apiEndpoint": "https://your-api.execute-api.us-east-1.amazonaws.com/prod/",
  "wsEndpoint": "wss://your-api.execute-api.us-east-1.amazonaws.com/prod",
  "version": "0.1.0"
}
```

| Field         | Required | Description                                                   |
| ------------- | -------- | ------------------------------------------------------------- |
| `region`      | Yes      | AWS region                                                    |
| `authority`   | Yes      | OIDC issuer URL (Cognito User Pool or generic OIDC)           |
| `clientId`    | Yes      | OAuth client ID                                               |
| `apiEndpoint` | No       | Backend API base URL (falls back to relative `/api`)          |
| `wsEndpoint`  | No       | WebSocket endpoint for Playground chat (relative or absolute) |
| `version`     | No       | Deployed monorepo version, auto-populated from the repo-root `VERSION` file at deploy time; shown as a badge in the UI |

### Run

```bash
cd packages/web-app
npm run dev
```

Opens at `http://localhost:5173`. Vite proxies `/ws` to `localhost:8080` and `/api/ontology` to `localhost:8001` for local backend development.

### Build

```bash
npm run build    # TypeScript check + Vite production build → dist/
npm run preview  # Preview the production build locally
```

### Test

```bash
# Vitest with v8 coverage. Enforced floor (ratcheting toward the 80% ORR QAL-A-2
# target): lines 58%, statements 58%, functions 58%, branches 70%.
npm run test
```

### UI Testing (Playwright)

End-to-end browser tests live under `tests/e2e/` and drive a real headless
Chromium against a **deployed** environment (not the local build). They cover
the core steward/analyst journeys: auth, namespaces, data-source onboarding,
metrics, grants, and the Playground.

#### One-time setup

```bash
cd packages/web-app
pnpm exec playwright install --with-deps chromium
```

#### Configuration

The suite reads these environment variables (no defaults for credentials; the
base URL falls back to the local dev server):

| Variable       | Required            | Description                                                                                    |
| -------------- | ------------------- | ---------------------------------------------------------------------------------------------- |
| `E2E_BASE_URL` | Yes (for real runs) | Deployed web app URL, e.g. `https://<id>.cloudfront.net`. Defaults to `http://localhost:5173`. |
| `E2E_USERNAME` | Yes                 | Cognito test user email                                                                        |
| `E2E_PASSWORD` | Yes                 | Cognito test user password                                                                     |

The Glue source lifecycle spec needs two more. The catalog id is an AWS account
id, so it has no default — the same rule the credentials above follow:

| Variable                 | Required                | Description                                                                          |
| ------------------------ | ----------------------- | ------------------------------------------------------------------------------------ |
| `E2E_GLUE_CATALOG_ID`    | Yes (for the Glue spec) | AWS account id owning the Glue catalog. No default — the Glue spec skips when unset. |
| `E2E_GLUE_DATABASE_NAME` | No                      | Glue database to onboard. Defaults to `hcp360`.                                      |

That spec is also gated behind `E2E_SLOW` (it runs a real scan and enrichment,
several minutes), so it needs both `E2E_SLOW=1` and `E2E_GLUE_CATALOG_ID` to run.

When credentials are absent, every environment-dependent test **skips** (the run
still exits 0), so the suite is safe to run anywhere.

#### Running

```bash
# full suite against a deployed env
E2E_BASE_URL=https://<host> E2E_USERNAME=<email> E2E_PASSWORD=<pwd> \
  npm run test:e2e

# a single feature area
E2E_BASE_URL=... E2E_USERNAME=... E2E_PASSWORD=... \
  pnpm exec playwright test --project=namespaces

# a single spec
... pnpm exec playwright test tests/e2e/specs/grants/grant-role.spec.ts

# enumerate without running
pnpm exec playwright test --list

# type-check the e2e sources
npm run typecheck:e2e
```

#### Reports and traces

```bash
pnpm exec playwright show-report   # open the HTML report
```

On failure, screenshots, traces, and video are written to `test-results/` and
the HTML report to `playwright-report/` (both gitignored). Open a trace with
`pnpm exec playwright show-trace test-results/<...>/trace.zip`.

#### How it works

- A `setup` project logs in once via Cognito and saves a stored auth state;
  feature-area projects reuse it so individual tests start authenticated.
- Tests create uniquely-named entities and clean them up automatically (the
  namespaces fixture archives + deletes what it created, even on failure).
- A few tests are `test.fixme` because they depend on backend state not yet
  available in dev (a completed source scan) or a known infra config gap
  (Cognito sign-out URL); they enable with a one-line change once unblocked.

#### CI

The `ui-test` job runs the suite on the mainline pipeline after `deploy-dev`,
resolving the deployed URL and reusing the integration test Cognito user. It is
non-blocking (`allow_failure`) for now. See `ci/README.md` → "UI E2E tests".

For authoring conventions (selector strategy, Cloudscape gotchas, adding a new
feature area), see the `playwright-ui-tests` agent skill under
`.claude/skills/playwright-ui-tests/`.

### Lint & Format

```bash
npm run lint     # ESLint + Prettier check
npm run format   # ESLint --fix + Prettier --write
```

## Stack

| Layer               | Technology                                   |
| ------------------- | -------------------------------------------- |
| Framework           | React 18 + TypeScript                        |
| Design system       | Cloudscape Design System                     |
| Build               | Vite 6                                       |
| Routing             | React Router v6                              |
| Data fetching       | TanStack React Query v5                      |
| Auth                | `oidc-client-ts` (Authorization Code + PKCE) |
| Testing             | Vitest + Testing Library + happy-dom         |
| Graph visualization | React Flow (@xyflow/react) + d3-force for force-directed layout |

## Architecture

```
main.tsx
  └─ BrowserRouter
       └─ RuntimeContextProvider    ← loads /runtime-config.json
            └─ ControlPlaneClientProvider  ← Smithy client (namespaces, roles, grants)
                 └─ ApiClientProvider      ← generic fetch client (sources, metrics, ontology)
                      └─ QueryClientProvider  ← React Query cache
                           └─ Auth            ← OIDC login gate
                                └─ App        ← routes + AppLayout shell
```

### Provider Stack

| Provider                     | Purpose                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| `RuntimeContextProvider`     | Fetches `/runtime-config.json` at boot, provides region/auth/endpoint config                              |
| `ControlPlaneClientProvider` | Instantiates the Smithy-generated `ControlPlaneServiceClient` with OIDC token injection                   |
| `ApiClientProvider`          | Provides a generic `{ get, post, put, del }` client for non-Smithy endpoints (sources, metrics, ontology) |
| `QueryClientProvider`        | TanStack React Query — caching, deduplication, background refresh                                         |
| `Auth`                       | Gates all children behind OIDC login; renders login page for unauthenticated users                        |
| `NamespaceProvider`          | Tracks the "current namespace" selection across navigation; provides namespace switcher in top-nav        |

## Routes

| Route                                            | Component                   | Description                                                                    |
| ------------------------------------------------ | --------------------------- | ------------------------------------------------------------------------------ |
| `/namespaces`                                    | `NamespaceList`             | Paginated table with status filtering, sorting, bulk status transitions        |
| `/namespaces/create`                             | `CreateNamespace`           | Create namespace form                                                          |
| `/namespaces/:id`                                | `GetNamespace`              | Namespace detail — summary, data sources tab, edit modal                       |
| `/namespaces/:id/permissions`                    | `NamespacePermissions`      | Namespace members (grant/revoke) + roles tabs                                  |
| `/namespaces/:id/sources`                        | `SourceList`                | Unified source list — summary dashboard, type/status filters, search, sortable |
| `/namespaces/:id/sources/connect`                | `ConnectSource`             | Multi-step wizard (Glue, JDBC, S3, file upload)                                |
| `/namespaces/:id/sources/:sourceId`              | `SourceDetail`              | Source detail — connection settings, table review, actions                     |
| `/namespaces/:id/sources/:dsId/tables/:tableId`  | `TableDetail`               | Individual table detail with column review                                     |
| `/namespaces/:id/ontology`                       | `OntologyPage`              | Induction — start a run, review proposals, manage reference ontologies         |
| `/namespaces/:id/ontology/proposals/:proposalId` | `ProposalDetailPage`        | Proposal detail (accept/reject/validate)                                       |
| `/namespaces/:id/ontology/graph`                 | `GraphSearchPage`           | Explorer — Classes graph/list, ontology inventory, and inducted sources tabs   |
| `/namespaces/:id/ontology/induced`               | `InducedOntologyDetailPage` | Induced-ontology detail — browse its classes, relationships, and axioms        |
| `/namespaces/:id/metrics`                        | `MetricList`                | Metric list with OSI import/export                                             |
| `/namespaces/:id/metrics/create`                 | `MetricForm`                | Create metric form (multi-dialect SQL editor)                                  |
| `/namespaces/:id/metrics/:name`                  | `MetricDetail`              | Metric detail view                                                             |
| `/namespaces/:id/metrics/:name/edit`             | `MetricForm`                | Edit metric (same form, pre-populated)                                         |
| `/identity`                                      | `IdentityPage`              | Platform-scoped grants (platform-admin/viewer) + platform roles                |
| `/profile`                                       | `ProfilePage`               | Current user profile, token info, group memberships                            |
| `/playground`                                    | `Playground`                | Interactive query playground (WebSocket chat)                                  |

## Package Structure

```text
packages/web-app/
├── src/
│   ├── App.tsx                    # Route definitions + AppLayout shell
│   ├── main.tsx                   # Entry point — provider tree
│   ├── api-hooks/                 # React Query hooks (one per API operation)
│   ├── auth/                      # OIDC provider + UserContext
│   ├── components/                # Shared components and providers
│   │   ├── Auth/                  # Login gate
│   │   ├── RuntimeContext/        # Runtime config loader
│   │   ├── ControlPlaneClientProvider/ # Smithy client
│   │   ├── ApiClientProvider/     # Generic REST client
│   │   ├── NamespaceProvider/     # Current namespace tracking
│   │   ├── Chat/                  # Playground chat widget
│   │   └── graph/                 # React Flow graph components
│   ├── pages/                     # Page components (one per route)
│   │   └── ontology/             # Ontology sub-pages
│   ├── services/                  # Service clients (ontology-engine)
│   ├── utils/                     # Helpers, error types, grant overrides
│   └── app-types/                 # Shared TypeScript interfaces
├── public/
│   ├── runtime-config.json        # Per-environment config (gitignored)
│   └── runtime-config.example.json
├── tests/
│   ├── unit/                      # Vitest unit tests
│   │   ├── api-hooks/            # Hook tests (mocked clients)
│   │   ├── pages/                # Page component tests
│   │   ├── components/           # Shared component tests
│   │   └── utils/                # Utility tests
│   └── e2e/                       # Playwright E2E tests (setup, fixtures, pages, specs)
├── package.json
├── vite.config.ts                 # Build config + dev proxy
├── vitest.config.ts               # Test config (happy-dom, aliases, coverage thresholds)
├── tsconfig.json                  # TypeScript config with path aliases
└── eslint.config.js               # ESLint flat config
```

## API Hooks

All data-fetching hooks live in `src/api-hooks/` and follow a consistent pattern:

- **Query hooks** (`useGet*`, `useList*`) — wrap `useQuery` / `useInfiniteQuery`, keyed by resource ID, disabled when the ID is empty
- **Mutation hooks** (`useCreate*`, `useUpdate*`, `useDelete*`) — wrap `useMutation`, invalidate related query caches on success

Two client patterns coexist:

| Client                    | Used by                     | Hook pattern                                             |
| ------------------------- | --------------------------- | -------------------------------------------------------- |
| `useControlPlaneClient()` | Namespace, role, grant CRUD | Smithy client methods (type-safe, auto-serialized)       |
| `useApiClient()`          | Sources, metrics, ontology  | Generic `get/post/put/del` with manual path construction |

Hooks are re-exported from `src/api-hooks/index.ts` for barrel imports via the `@api-hooks` path alias.

## Authentication

The app uses **OIDC Authorization Code flow with PKCE** via `oidc-client-ts`.

### Setup

1. Create an OIDC client (e.g. Cognito User Pool app client)
2. Set **callback URL** to `http://localhost:5173/authenticate/` (dev) or `<origin>/authenticate/` (prod)
3. Set **sign-out URL** to your app origin
4. Put `authority` and `clientId` in `runtime-config.json`

### Login Flow

1. Unauthenticated → `/login` page
2. Click "Login" → redirect to OIDC provider
3. Provider redirects back to `/authenticate/`
4. App exchanges auth code for tokens, stores in session storage
5. Redirects to `/`

### Token Refresh

The `OIDCProvider.getIdToken()` and `getAccessToken()` methods silently refresh tokens when they expire within 120 seconds. If silent refresh fails, the user is prompted to re-authenticate.

### Accessing User Info

```tsx
import { useUser } from "@auth";

function MyComponent() {
  const { user, logout } = useUser();
  // user.userid, user.email, user.groups
}
```

## Deployment

The build output (`dist/`) is a static SPA. Serve it behind CloudFront or any static host. Key deployment requirements:

1. Serve `runtime-config.json` alongside the static assets (environment-specific, not baked into the build)
2. Configure SPA fallback routing (all paths → `index.html`)
3. Set CORS on the API Gateway to allow the app's origin

## Path Aliases

TypeScript path aliases are configured in both `tsconfig.json` and `vite.config.ts`:

| Alias         | Maps to                             |
| ------------- | ----------------------------------- |
| `@auth`       | `src/auth/`                         |
| `@api-hooks`  | `src/api-hooks/`                    |
| `@components` | `src/components/`                   |
| `@pages`      | `src/pages/`                        |
| `@utils`      | `src/utils/`                        |
| `@app-types`  | `src/app-types/`                    |
| `@coa/shared` | `../../libs/ts-shared/src/index.ts` |

## Troubleshooting

| Symptom                                                 | Likely Cause                                          | Fix                                                                                    |
| ------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Blank page with "No runtime-config.json detected"       | Missing or malformed config file                      | Copy `runtime-config.example.json` → `runtime-config.json` and fill in values          |
| Login redirects loop endlessly                          | Callback URL mismatch in OIDC provider settings       | Verify the provider's callback URL matches `<origin>/authenticate/` exactly            |
| 403 on API calls after login                            | User lacks the required Cedar role                    | Check the user's grants in the control-plane (use `/identity` page for platform roles) |
| WebSocket disconnects in Playground                     | `wsEndpoint` not configured or backend not running    | Set `wsEndpoint` in `runtime-config.json`; verify the AgentCore runtime is running     |
| Tests fail with "Cannot find module @cloudscape-design" | happy-dom ESM resolution issue                        | Ensure `vitest.config.ts` has `server.deps.inline` for Cloudscape packages             |
| CORS errors in browser console                          | API Gateway missing CORS headers for the app's origin | Check `ALLOWED_ORIGIN` on the backend Lambda; ensure it matches the dev server URL     |
