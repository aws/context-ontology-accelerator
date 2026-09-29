# Getting Started

## Prerequisites

- Python 3.12 (pinned via `.python-version` — uv will use this automatically)
- Node.js 22+ (for CDK CLI and web-app)
- [pnpm](https://pnpm.io/) (Node package manager — installed via `mise` or `npm install -g pnpm`)
- Docker (for local dev services and container image builds)
- Java 17+ and Gradle (for Smithy codegen)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- AWS CLI v2 (for deployments and ECR auth)

## Initial Setup

```bash
# Clone the repository
git clone <repo-url>
cd ontology-accelerator

# Install uv, sync all packages, set up pre-commit hooks
make setup
```

## Development Workflow

### Format, lint, and test

```bash
# Auto-format code (fixes lint errors + formatting)
make format

# Run linting (ruff + mypy + TypeScript type-check + prettier)
make lint

# Run unit tests (Python pytest + CDK Jest)
make test

# Run integration tests (requires deployed dev stack)
make test-integ
```

### CDK (infrastructure)

```bash
# Build all TS packages and validate CDK stacks
pnpm nx run-many -t build
pnpm --filter coa-infra exec cdk synth

# Deploy to dev environment
make deploy-dev
```

> **CDK CLI:** If you see a version mismatch error, update: `npm install -g aws-cdk@latest`

#### CDK context variables

All stacks use context-driven configuration via `CoaStack` base class:

| Variable                   | Default            | Description                                                    |
|----------------------------|--------------------|----------------------------------------------------------------|
| `resource_prefix`          | `coa`              | Prefix for all resource names                                  |
| `env`                      | `dev`              | Deployment environment                                         |
| `project_tag`              | `semantic-context` | AWS `Project` tag value                                        |
| `vpc_id`                   | (none)             | Import an existing VPC instead of creating one                 |
| `aoss_max_ocu`             | `96`               | Max OCU capacity for OpenSearch Serverless (indexing + search) |
| `aoss_min_ocu`             | `2`                | Min OCU capacity for OpenSearch Serverless (indexing + search). Set to `0` for scale-to-zero — see below |
| `api_throttle_rate_limit`  | `50`               | API Gateway stage requests-per-second rate limit               |
| `api_throttle_burst_limit` | `100`              | API Gateway stage burst capacity                               |
| `lambda_reserved_concurrency` | `5`             | Reserved concurrency for the VKG-reload and doc-preprocessing Lambdas; `0` disables reserving (needed on reduced Lambda-quota accounts) |
| `serve_nl2sql_graph_expand` | (unset — on)    | Tier-2 flat NL→SQL ontology foreign-key expansion; `false` turns it off. See [Deploying](deploying.md#tier-2-nlsql-ontology-foreign-key-expansion) |
| `serve_nl2sql_graph_expand_max_tables` | (unset — `8`) | How many walked tables the expansion may append to the NL→SQL prompt; `0` appends none |

Resource naming follows `{prefix}-{env}-{name}` (e.g. `coa-dev-neptune`).

#### OpenSearch Serverless capacity and scale-to-zero

The vector collection is a **NextGen** collection (`generation: "NEXTGEN"` on the
collection group), not Classic. NextGen supports scaling compute to zero when
idle, which removes the always-on OCU cost floor, but COA ships a minimum of
**2 OCU** rather than 0 so that a default deployment does not pay cold-start
latency on its first query after an idle period.

To opt into scale-to-zero:

```bash
cdk deploy -c aoss_min_ocu=0
```

Valid `aoss_min_ocu` values are `0`, `2`, `4`, `8`, `16`, or multiples of 16.
Leave standby replicas alone — AWS rejects `standbyReplicas: DISABLED` for
NextGen, which manages replicas internally.

The tradeoff at minimum 0: compute scales down after a period of inactivity and
takes roughly ten seconds to return, and at very low OCU the NextGen circuit
breaker sheds load with HTTP 429s under an ingestion burst. That is usually the
right trade for dev and sandbox environments, and usually the wrong one for an
environment serving interactive queries or running large scans.

#### First-time deployment

Deploying for the first time to a new AWS account requires bootstrapping CDK
and authenticating to ECR Public — see
[Deploying Context Ontology Accelerator](deploying.md) for those one-time steps (both
are also handled automatically by `make deploy-dev`'s preflight checks).

#### Lake Formation bootstrap (required for JDBC data sources)

JDBC sources are provisioned as Lake Formation–governed Glue federated catalogs.
Creating those catalogs requires the federation provisioner's Lambda role to be a
Lake Formation **data-lake admin**. The `LakeFormationAdmin` custom resource
registers it automatically and non-destructively (it appends to the existing
admin list rather than overwriting it).

There is no manual bootstrap step, on greenfield or on existing accounts alike.

`PutDataLakeSettings` is authorized by the **IAM permission** `lakeformation:PutDataLakeSettings`, which the custom resource's Lambda role already carries — *not* by the caller's membership in `DataLakeAdmins`. A principal holding that permission can read the settings and modify the admin list whether or not it is itself an admin, and whether or not the account already has admins.

#### What COA changes about your Lake Formation settings

Worth knowing before the first deploy, but nothing here needs action:

- **Your existing data-lake admins are preserved.** The custom resource reads the
  current settings, appends one principal if absent, and writes them back. It never
  overwrites the admin list. Other settings, including the `IAM_ALLOWED_PRINCIPALS`
  defaults that make LF defer to IAM, are round-tripped untouched — deploying COA
  does not flip an account into strict LF mode.
- **One principal is added:** the federation provisioner role, published at
  `/{prefix}/sources/federation-provisioner-role-arn`. It needs data-lake admin
  standing to grant `DATA_LOCATION_ACCESS` on per-source Glue connections during a
  JDBC scan.
- **`cdk destroy` removes it again,** deregistering only the principal COA
  registered.
- The read-modify-write has no compare-and-swap, so a governance tool that
  reconciles LF settings concurrently could race with it. Unlikely to matter in
  practice; worth knowing if you run one.

See `infra/README.md` (Lake Formation bootstrap) for troubleshooting.

#### Customizing deployments

Pass context overrides via environment variables:

```bash
# Custom prefix
SCL_PREFIX=myproj make deploy-dev

# Import existing VPC
SCL_VPC_ID=vpc-0abc123 make deploy-dev

# Multiple overrides
SCL_PREFIX=acme SCL_VPC_ID=vpc-xyz make deploy-dev
```

#### Deployment architecture

See [Deploying Context Ontology Accelerator](deploying.md) for the full stack list
(16 stacks total), dependency order, and per-stack purpose — CDK resolves
ordering automatically from the dependency graph in `bin/app.ts`.

> **Cost warning:** Neptune + OpenSearch Serverless cost ~$930/mo when idle.
> Destroy stacks when not actively testing — see
> [Deploying Context Ontology Accelerator: Tearing Down](deploying.md) for the
> recommended `make destroy-dev` command.

### Smithy codegen

Requires Java 17+ and Gradle. Generates OpenAPI specs, Python server stubs, and TypeScript client from `.smithy` models.

```bash
make generate
```

If you don't edit `.smithy` files, you don't need to run this.

### Web app (landing page)

```bash
cd packages/web-app
cp public/runtime-config.example.json public/runtime-config.json
# Edit runtime-config.json with your OIDC provider details (authority, clientId)
pnpm install
pnpm dev        # opens at http://localhost:5173
```

> **Authentication required:** The web app uses OIDC authentication. See
> `packages/web-app/README.md` in the repository for full
> configuration and identity provider setup.

## Available Make Targets

| Target             | Description                                                                      |
| ------------------ | -------------------------------------------------------------------------------- |
| `make setup`       | Install uv + pnpm, sync packages, set up pre-commit                              |
| `make generate`    | Run Smithy codegen → populate `smithy-generated/`                                |
| `make format`      | Auto-format Python (ruff) + TypeScript (prettier) via Nx                         |
| `make lint`        | Lint + type-check all packages via Nx                                            |
| `make test`        | Run unit tests via Nx                                                            |
| `make test-integ`  | Run integration tests                                                            |
| `make build`       | Build all packages via Nx                                                        |
| `make deploy-dev`  | Deploy to dev environment via CDK (supports `SCL_PREFIX`, `SCL_VPC_ID` env vars) |
| `make destroy-dev` | Tear down all dev stacks (AgentCore Runtimes, VKG ECS services, DataZone domain, then `cdk destroy --all`) — see the [Deploying guide](deploying.md), "Tearing Down" section |
| `make docs`        | Serve docs site locally (MkDocs)                                                 |

## Next Steps

- See the **[API Reference](#/api-reference)** for the full Control Plane and Data Layer API contracts (sources, metrics, ontologies, namespaces, grants, and the Serve/query endpoints).
- See the [Structured Data Source Guide](sources.md) for every source type COA supports — Glue, the JDBC engines, and custom connectors including the ready-made Databricks SQL Warehouse connector — and how to register each one.
- See the [Package Guide](package-guide.md) for how to add or implement a package.
- See `CONTRIBUTING.md` for coding standards and PR process.
