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
| `lf_admin_role_arn`        | (none)             | Existing Lake Formation data-lake admin role to run the LF-admin custom resource as. Required on accounts that already have LF admins |
| `api_throttle_rate_limit`  | `50`               | API Gateway stage requests-per-second rate limit               |
| `api_throttle_burst_limit` | `100`              | API Gateway stage burst capacity                               |

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

There is a one-time bootstrap caveat: `PutDataLakeSettings` may only be called by
an existing LF admin once an account already has any admins, and the caller is the
custom resource's own Lambda role.

- **Greenfield accounts** (no LF admins yet) self-bootstrap — no action needed.
- **Accounts that already have LF admins:** supply a role you already own and have
  already registered as an LF admin, via the `lf_admin_role_arn` context variable.
  The custom resource runs as that role, so the call is authorized on the first
  deploy.

  ```bash
  cdk deploy coa-dev-sources -c lf_admin_role_arn=arn:aws:iam::<account>:role/<your-lf-admin>
  ```

  The role must be assumable by `lambda.amazonaws.com`. COA attaches the
  permissions it needs (LF settings read/write, one SSM parameter read, Lambda
  logging) to it; it does not need them beforehand.

Without `lf_admin_role_arn` on an account that already has admins, the
`coa-<env>-sources` **stack fails at deploy time** on the `LakeFormationAdmin`
custom resource. It does not fail later at scan time, and registering the
federation provisioner role by hand does not help — that is the role being
*registered*, not the one making the call.

Recovering without `lf_admin_role_arn` is possible but awkward, because the
auto-created caller role is deleted by the rollback and re-created under a new
auto-generated name on retry, invalidating any registration made against the old
ARN. It requires `cdk deploy --no-rollback` so the role survives with a stable
ARN, registering it out-of-band, then deploying again. Prefer
`lf_admin_role_arn`.

#### What COA changes about your Lake Formation posture

Worth knowing before the first deploy, on any account:

- Your existing data-lake admins are **preserved**. The custom resource reads the
  current settings, appends one principal, and writes them back; it never
  overwrites the admin list. Other settings, including the
  `IAM_ALLOWED_PRINCIPALS` defaults that make LF defer to IAM, are round-tripped
  untouched — deploying COA does not flip an account into strict LF mode.
- On a **greenfield** account, the first deploy takes `DataLakeAdmins` from empty
  to non-empty, and LF then begins enforcing admin-only access to settings. Any
  principal of yours that previously managed LF settings through IAM permission
  alone — a console role, your own IaC — stops being able to. Add it as an admin
  alongside COA's (supplying `lf_admin_role_arn` is the easiest way to do both in
  one up-front registration).
- `cdk destroy` deregisters the principal COA registered. If you also register it
  by hand, expect teardown to remove it.

See `infra/README.md` (Lake Formation bootstrap) for the exact registration
commands and troubleshooting.

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
- See the [Package Guide](package-guide.md) for how to add or implement a package.
- See `CONTRIBUTING.md` for coding standards and PR process.
