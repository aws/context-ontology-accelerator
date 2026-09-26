# Deploying Context Ontology Accelerator

This guide walks you through deploying Context Ontology Accelerator into your AWS account.

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| AWS Account | — | Target deployment account |
| AWS CLI v2 | 2.x | Credential management, stack operations |
| Node.js | 22+ | CDK CLI, web-app build |
| Python | 3.12 | Backend services |
| pnpm | 10+ | TypeScript package management |
| uv | 0.4+ | Python package management |
| Java | 17+ | Smithy code generation |
| Docker | — | Container image builds |

### ARM64 container builds on x86_64 hosts

Several CDK assets are built explicitly for `linux/arm64`: the Context Manager
(Serve), MCP, and VKG images. A native ARM64 machine needs no emulation. An
x86_64 Linux host must have binfmt/QEMU registered before Docker can execute
ARM64 build steps; otherwise the build commonly stops with `exec format error`.

Docker Desktop includes multi-platform emulation on supported installations.
Verify the active Docker builder before deploying:

```bash
docker buildx inspect --bootstrap
docker run --rm --platform linux/arm64 alpine uname -m
```

The builder's platform list should include `linux/arm64`, and the second command
should print `aarch64`. If Docker Engine on Linux does not have ARM64 emulation,
follow [Docker's QEMU setup guidance](https://docs.docker.com/build/building/multi-platform/#qemu).
The [tonistiigi/binfmt installer](https://github.com/tonistiigi/binfmt#installing-emulators)
accepts an architecture-specific install so the host only registers the
emulator needed here:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64

# Verify again before make deploy-dev
docker run --rm --platform linux/arm64 alpine uname -m
```

!!! warning "binfmt installation is privileged"
    Registering binfmt modifies the host kernel configuration and the command
    above runs a privileged container. Follow your organization's host-security
    policy. Where privileged setup is not allowed, use a native ARM64 builder or
    supply prebuilt ARM64 ECR images instead of building the assets locally.
    `context_manager_image_uri` supplies the shared Context Manager image used
    by the Serve and MCP stacks. VKG requires `vkg_image_uri` together with
    `ecr_repository_arn` and `ecr_repository_name`.

If a build still fails:

1. Check whether `CDK_DOCKER` selects Docker, Finch, or another engine. Register
   emulation in the same engine that CDK will use.
2. When Docker is active, re-run `docker buildx inspect --bootstrap` and confirm
   `linux/arm64` is listed.
3. Run the Docker Alpine verification command above. An `exec format error` there is a
   host/emulation problem, before CDK or application code is involved.
4. On a remote or custom builder, inspect the selected builder with
   `docker buildx ls`; registration on the local default engine does not
   configure a different builder automatically.

## AWS Account Setup

Context Ontology Accelerator deploys into a single AWS account and region. Ensure the deploying principal has `AdministratorAccess` or equivalent permissions for the initial deployment.

### Service Quotas

Two account service quotas can block a deploy. The preflight check
(`scripts/preflight-deploy.sh`) validates both before CDK runs, but on a fresh
or sandbox account it is worth confirming them up front:

| Quota | Code | Requirement | If too low |
|-------|------|-------------|------------|
| **VPC** (VPCs per Region) | `L-F678F1CE` | Room for one more VPC in the target region | Delete an unused VPC or request an increase |
| **Lambda** (Concurrent executions) | `L-B99A9384` | Enough unreserved headroom to reserve the deployment's Lambda concurrency (default 5 × 2 functions = 10) above Lambda's account-wide minimum of 10 | Request an increase, **or** deploy with `SCL_LAMBDA_RESERVED_CONCURRENCY=0` (see [Lambda reserved concurrency](#lambda-reserved-concurrency)) |

Check them with:

```bash
aws ec2 describe-vpcs --query 'length(Vpcs)' --output text
aws lambda get-account-settings \
  --query 'AccountLimit.[ConcurrentExecutions,UnreservedConcurrentExecutions]'
```

New accounts sometimes have the Lambda concurrent-executions quota at the
reduced default of `10`, on which reserving *any* concurrency is rejected.
Raising it (`L-B99A9384`) opens an AWS Support case rather than being granted
immediately, so if you are on a reduced-quota account and want to deploy now,
disable the reservations with `SCL_LAMBDA_RESERVED_CONCURRENCY=0`.

These two are the only quotas the preflight check validates. Several others —
OpenSearch Serverless OCUs, Bedrock per-model invocation limits, Fargate vCPU,
ENIs, S3 buckets — can still block a deploy on a new or sandbox account. See
[Appendix A: Quotas to check](#a3-quotas-to-check-before-deploying) for the
fuller list and why each one matters here.

### Region Selection

Context Ontology Accelerator defaults to `us-east-1` if no region is set. To deploy to a different region, export `AWS_DEFAULT_REGION` (used by the AWS CLI and preflight checks) **and** `CDK_DEFAULT_REGION` (used by CDK/`bin/app.ts` for AZ resolution and region-specific config) before running any deploy command:

```bash
export AWS_DEFAULT_REGION=us-west-2
export CDK_DEFAULT_REGION=us-west-2
```

Both must be set consistently — if only one is set, CDK and the AWS CLI can silently target different regions.

### Region Prerequisites

Context Ontology Accelerator **cannot be deployed to every AWS region**. It depends on several
services that are not available everywhere, so the deployable set is the intersection of the regions
where all of them exist.

Regional availability changes continuously as AWS launches services in new regions, so this guide
does not list specific regions — **check each dependency below in your target region before you
deploy**. The fastest way to check all of them at once is the
[AWS Regional Services List](https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/),
filtered to your region.

#### Services to verify

Every service in this table must be available in your target region. The first two are the narrowest
constraints in practice — if either is missing, the region is not usable.

| Service | Used for | Check |
|---------|----------|-------|
| **Amazon Bedrock AgentCore Runtime** | Hosts the Serve (query) and MCP server runtimes | [Supported regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) — note this page lists availability per AgentCore *feature*; you need **Runtime** |
| **Amazon DataZone** / SageMaker Unified Studio | Namespace domain, data-asset catalog | [Endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/datazone.html) |
| **Amazon Neptune** | Knowledge-graph store | [Endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/neptune.html) |
| **Amazon OpenSearch Serverless** | Vector search for retrieval | [Endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/opensearch-service.html) — Serverless is available in fewer regions than managed OpenSearch |
| **Amazon Bedrock** + Guardrails | All LLM and embedding calls; content filtering | [Endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/bedrock.html) — also check the models themselves (see below) |
| **Amazon Athena** (incl. federated query) | SQL over mapped data sources | [Endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/athena.html) |

The remaining services the solution uses — Lambda, API Gateway, ECS on Fargate, S3, DynamoDB,
Cognito, CloudFront, WAF, Glue, Step Functions, SSM, CloudWatch — are available in effectively all
commercial regions and are not usually the limiting factor. That list is not exhaustive; for the
complete inventory, including the services that are only called at run time rather than provisioned
by the deploy, see [Appendix A: AWS Service Inventory](#appendix-a-aws-service-inventory-quotas-and-considerations).

!!! warning "GovCloud and China regions are not supported"
    These partitions lack several required dependencies, and the stacks assume the `aws` partition in
    ARN construction. Deploying there would need code changes beyond region configuration.

#### Bedrock model availability

Availability of the *service* is not enough — the specific **foundation models must also be
enabled in your region**, and the solution's default model IDs are **US cross-region inference
profiles** (`us.anthropic.…`, `us.cohere.embed-v4:0`) that cannot be invoked from a non-US source
region.

If you deploy outside the US, look each model up in
[Regional availability by models](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html)
(it shows In-Region / Geo / Global support per region), then replace the default IDs with a profile
that is invocable from your region — a geographic profile (`eu.`, `jp.`, `au.`), a `global.` profile,
or the bare in-region model ID. The models to check:

| Model | Used for |
|-------|----------|
| Claude Sonnet 4.6 | Induction, grounding |
| Claude Haiku 4.5 | Rerank, enrichment, document ingestion |
| Claude Sonnet 5 | Serve query resolution (NL-to-SPARQL, synthesis) |
| Cohere Embed v4 | All embeddings |

Not every model offers every profile type in every region — Cohere Embed v4, for example, publishes
only `us.` and `eu.` geographic profiles. Also
[request model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) for
each model in the deploy region, and note that `global.` profiles require
[additional IAM/SCP permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html)
beyond what the stacks grant by default.

##### Where the model IDs live

Every model ID is configurable from the SSM deploy config (`/{prefix}/config`) — no source edits are
required to deploy outside the US. Set the keys you need before deploying; each falls back to the
built-in default when omitted. Inside the US a deployment that configures none of them behaves
exactly as it does today; outside the US the defaults are unusable (see the warning below), and
`cdk synth` refuses to proceed until the keys are set.

| Config key | Sets the model for | Default |
|------------|--------------------|---------|
| `bedrockLlmModelId` | Serve query LLM (NL-to-SPARQL, synthesis) | `us.anthropic.claude-sonnet-5` |
| `bedrockEmbedModelId` | **All** embeddings — induction, doc-KG-build, metric matching, serve retrieval | `us.cohere.embed-v4:0` |
| `bedrockEmbedDimensions` | Vector dimension for the embedding model above | `1024` |
| `bedrockInductionLlmModelId` | Ontology induction, grounding rerank, description generation | `us.anthropic.claude-sonnet-5` |
| `bedrockChatModelId` | Source enrichment, constraint inference, document-ingestion extraction | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |

Both **geographic inference profiles** (`us.`, `eu.`, `apac.`, `jp.`, `global.`) and **bare in-region model
IDs** (e.g. `cohere.embed-v4:0`) are accepted — bare IDs matter because some models publish geo
profiles for only a subset of regions. The same resolved values also drive the Bedrock IAM grants and
the CloudWatch dashboards' `ModelId` dimensions, so permissions and cost widgets follow your
configuration automatically.

Example `/{prefix}/config` for an `ap-northeast-1` deployment (a bare in-region ID for the embedding
model because Cohere Embed v4 publishes no `jp.` profile):

```json
{
  "initialAdminEmail": "admin@example.com",
  "bedrockLlmModelId": "global.anthropic.claude-sonnet-5",
  "bedrockEmbedModelId": "cohere.embed-v4:0",
  "bedrockEmbedDimensions": 1024,
  "bedrockInductionLlmModelId": "global.anthropic.claude-sonnet-5",
  "bedrockChatModelId": "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
}
```

Not every model publishes a profile for every geography, so check what your region offers before
setting these: `aws bedrock list-inference-profiles` and `aws bedrock list-foundation-models`.

!!! warning "`us.` profiles cannot be invoked outside the US"
    Bedrock rejects a cross-geography inference profile with
    `ValidationException: The provided model identifier is invalid.` The defaults in the table above are
    all `us.` profiles, so **a non-US deployment must set every model key** — a deploy that configures
    none of them only "behaves as it does today" inside the US.

    Two checks catch this before the first invocation:

    - **`cdk synth` fails** when any effective model ID (configured, or the built-in default when the
      key is absent) is a geographic profile the deploy region's geography does not publish — for
      example `us.` in `ap-northeast-1`. The error lists each offending key, whether the value came
      from `/{prefix}/config` or the default, and the profiles the region does publish. The deploy
      region is taken from `CDK_DEFAULT_REGION` (default `us-east-1`).
    - **`make preflight`** runs the same geography check (as an error) and additionally warns when a
      model is not listed by `aws bedrock list-inference-profiles` / `list-foundation-models` in the
      region — a warning, not an error, because availability is account-scoped.

    Neither check judges bare in-region IDs or `global.` profiles beyond listing them, and neither can
    tell whether your account has been granted access to a model: check that in the Bedrock console.

!!! warning "Changing the embedding model is a data migration"
    All embeddings must use the same model — existing indexes were written with the previous one and
    must be re-ingested, or retrieval degrades silently. `bedrockEmbedDimensions` is baked into the
    OpenSearch index at creation and cannot be changed afterwards. Set both at **initial** deploy.

!!! warning "Only `us-east-1` has been tested"
    `us-east-1` is the default and the only region this solution has been deployed and validated in.
    Other regions that pass the checks above are expected to work, but are unverified — budget time
    for troubleshooting on a first deploy elsewhere.

#### Cross-region and AZ constraints

These apply **no matter which region you deploy to**:

- **`us-east-1` is always involved.** CloudFront-scope WAF WebACLs and CloudFront ACM certificates
  exist only in `us-east-1`, so the `*-edge-waf` stack always deploys there and `uiCertificateArn`
  must be a `us-east-1` certificate. Amazon ECR Public is likewise `us-east-1`-only (build-time).
- **Availability Zones can be restricted within a region.** A service being present in a region does
  not mean it is present in every AZ of that region. In `us-east-1`, AgentCore Runtime supports only
  3 of 6 AZs and the OpenSearch Serverless VPC endpoint is offered in a different subset, so
  `bin/app.ts` resolves the intersection at synth time (`infra/lib/utils/agentcore-az.ts`) and pins
  the VPC to it. If your region turns out to have similar AZ restrictions, add it to
  `AGENTCORE_RESTRICTED_AZ_IDS` in that file — otherwise the AgentCore Runtime or the AOSS endpoint
  can fail to create on an unsupported zone.
- **One region per `SCL_PREFIX`.** See the multi-region warning under Environment Variables below.

### Bootstrap CDK

If this is the first CDK deployment to the account/region:

```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
```

!!! warning "Deploying outside `us-east-1` needs TWO bootstraps"
    The `*-edge-waf` stack always deploys to `us-east-1` (CloudFront-scope WAF WebACLs and CloudFront
    ACM certificates exist only there — see "Cross-region and AZ constraints" above), so that region
    must be bootstrapped as well or the deploy fails partway through on that stack:

    ```bash
    npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>       # your deploy region
    npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1      # always required — edge-waf stack
    ```

    Preflight checks both and fails fast if either is missing.


## Installation

```bash
git clone <repo-url> && cd ontology-accelerator

# Install pinned tool versions (Python 3.12, Java 17, Node 22, pnpm 10.27.0)
curl https://mise.run | sh
mise install

# make setup runs smithy-generate.sh (make generate) + setup-dev.sh
# (uv sync --all-packages, pnpm install, pre-commit hooks)
make setup
```

`make deploy-dev` re-checks required toolchain versions and re-runs Smithy codegen (`make generate`) automatically if `smithy-generated/` is missing or stale, so a manual re-run is only needed if you want generated artifacts refreshed without doing a full deploy.

## Deploy

```bash
make deploy-dev
```

This runs `scripts/deploy.sh` which:

1. **Preflight checks** — verifies toolchain versions (Node, Java, pnpm), Docker, regenerates Smithy artifacts if missing/stale, authenticates to ECR Public, checks VPC quota
2. **Builds all packages** — compiles TypeScript, bundles Lambdas
3. **Synthesizes CloudFormation** — generates templates from CDK
4. **Deploys all stacks** — CDK handles ordering via dependency graph

!!! note "ECR Public authentication is automatic"
    `make deploy-dev` authenticates to ECR Public (`us-east-1`) as part of its preflight checks. No manual `docker login` step is needed.
    The `us-east-1` region here is intentional and unrelated to your deploy region (see "Region Selection" above) — Amazon ECR Public is a single-region service; its registry and control-plane API exist only in `us-east-1` regardless of which region you deploy Context Ontology Accelerator into.


### Stacks Deployed

| Stack | Purpose |
|-------|---------|
| `coa-dev-network` | VPC, subnets, security groups |
| `coa-dev-auth` | Cognito User Pool, OIDC configuration |
| `coa-dev-guardrail` | Bedrock guardrails |
| `coa-dev-storage` | Neptune (graph DB), OpenSearch Serverless |
| `coa-dev-authnz` | DynamoDB tables for roles, grants, Cedar policies |
| `coa-dev-vkg` | Virtual Knowledge Graph (Ontop) |
| `coa-dev-namespace` | Namespace service (SMUS domain) |
| `coa-dev-metric-service` | Metric authoring and validation |
| `coa-dev-api` | API Gateway (routes, authorizer) |
| `coa-dev-serve` | Context Manager (query orchestration) |
| `coa-dev-sources` | Data source ingestion (Glue, JDBC) |
| `coa-dev-data-layer` | REST API for queries |
| `coa-dev-edge-waf` | CloudFront WAF WebACL (us-east-1, auto-created unless an existing ARN is supplied) |
| `coa-dev-web` | CloudFront + S3 (React frontend) |
| `coa-dev-ontology` | Ontology engine (induction, reasoning) |
| `coa-dev-mcp` | MCP Server on AgentCore Runtime |

Total fresh deploy: **~1.5 hours** for CDK/CloudFormation provisioning of all 16 stacks, once dependencies are installed and Smithy artifacts are generated. Neptune and OpenSearch Serverless provisioning, AgentCore Runtime setup, and cross-stack SSM dependency waits account for most of that time — individual stacks vary widely and several run in parallel, so a per-stack breakdown understates the real end-to-end wall-clock time. CDK resolves the deploy order automatically from the dependency graph declared in `bin/app.ts` — you don't need to deploy stacks individually or in this order by hand.

!!! warning "First-time setup adds significant time"
    The ~1.5 hour figure above is CDK/CloudFormation provisioning time only. On a fresh machine or first-time deploy, budget additional time on top of that for: installing `mise`-managed toolchains, `uv sync`/`pnpm install`, Smithy/Gradle codegen (`make generate` — first run downloads Gradle wrappers, openapi-generator, and builds TypeScript clients from scratch), Docker image builds, and CDK bootstrap. Subsequent deploys after initial setup are faster since dependencies and generated artifacts are cached, though CloudFormation provisioning time for a fresh set of stacks remains similar.

### Configuration Options

Override defaults via environment variables:

```bash
# Custom resource prefix (default: coa)
SCL_PREFIX=myproject make deploy-dev

# Use an existing VPC
SCL_VPC_ID=vpc-abc123 make deploy-dev

# SMUS admin principal(s) — override the account's `Admin` role fallback.
# Comma-separated IAM role/user ARN(s) that human admins federate into to
# access the SageMaker Unified Studio console. Required on any account that
# doesn't have a role literally named `Admin` (e.g. IAM Identity Center
# accounts) — `make deploy-dev` checks for that role first and fails fast
# with a clear message if it's missing and this isn't set. For an IAM
# Identity Center account, use your permission set's federated role:
SCL_SMUS_ADMIN_ARNS=arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/us-east-1/AWSReservedSSO_AdministratorAccess_abc123 \
make deploy-dev

# Custom domain for the web app and API — all five are required together
# (all-or-nothing; CDK fails synth if only some are set)
SCL_UI_DOMAIN=ontology.example.com \
SCL_UI_CERT_ARN=arn:aws:acm:us-east-1:123456789012:certificate/ui-cert-id \
SCL_API_DOMAIN=api.ontology.example.com \
SCL_API_CERT_ARN=arn:aws:acm:us-west-2:123456789012:certificate/api-cert-id \
SCL_HOSTED_ZONE_ID=Z0123456789ABCDEFGHIJ \
make deploy-dev
```

!!! note "Custom domain certificate regions"
    `SCL_UI_CERT_ARN` must be an ACM certificate in `us-east-1` (CloudFront requirement) regardless of your deploy region. `SCL_API_CERT_ARN` must be in the same region you're deploying to (API Gateway requirement). CDK validates both at synth time and fails fast with a clear error if either is in the wrong region.

!!! warning "Multi-region deployments"
    S3 buckets and IAM roles are globally scoped, not region-isolated. Deploying the same `SCL_PREFIX` + `env` to a second region **will collide** with an existing deployment. Use a distinct `SCL_PREFIX` per region (e.g., `coa-w2` for `us-west-2`) — do not rely on region alone to disambiguate.

#### Tier-1 metric execution timeout

Tier 1 executes the curated SQL attached to a matched metric. Each statement has
an explicit **35-second** timeout by default, rather than inheriting a database
client's shorter method default. Increase it when valid metric queries over a
large warehouse consistently time out:

```bash
# Allow a Tier-1 metric statement up to 75 seconds (default is 35)
SCL_TIER1_METRIC_TIMEOUT_SECONDS=75 make deploy-dev
```

The value must be an integer from **1 through 300 seconds**. Invalid values fall
back to 35 seconds and emit a configuration warning. The effective timeout can
still be lower: COA clamps it to the remaining outer request deadline and skips
the SQL call when less than one second remains, so this setting cannot keep a
query alive after its request expires.

`scripts/deploy.sh` maps the variable above to the CDK context parameter
`tier1_metric_timeout_s`. For a direct CDK deployment, pass
`--context tier1_metric_timeout_s=75` or set that key in `infra/cdk.json`. The
Serve runtime receives the resulting value as `TIER1_METRIC_TIMEOUT_S`; this
runtime variable is normally managed by the stack rather than set manually.

#### Database scan enrichment timeout

A database source scan runs an enrichment step (an ECS Fargate task that calls Bedrock once per discovered table). It is bounded by a deadline; when the deadline is hit the scan fails cleanly to `SCAN_FAILED` so the source can be deleted or re-scanned, rather than being stranded mid-scan. The default deadline is **120 minutes**, sized to comfortably cover a large source (roughly 2,000 tables at ~30–35 s per table with ten tables enriched in parallel).

Raise it only if you are onboarding a source large enough to exceed that — a scan that fails on the deadline reports `SCAN_FAILED`, and this is the knob to give it more time:

```bash
# Allow up to 180 minutes for enrichment (default is 120)
SCL_DB_SCAN_ENRICHMENT_TIMEOUT_MINUTES=180 make deploy-dev
```

The value is minutes and must be a positive number; CDK fails synth otherwise. On a direct `cdk deploy` (rather than the `make`/`deploy.sh` path) pass it as CDK context instead — `--context dbScanEnrichmentTimeoutMinutes=180`, or set it in the `context` block of `infra/cdk.json`. The Step Functions state-machine ceiling is derived automatically as this value plus two minutes, so the per-task deadline always trips first and routes the source to `SCAN_FAILED`.

#### Lambda reserved concurrency

Two Lambdas — the VKG reloader and the document preprocessor — reserve concurrent executions (default **5** each) to bound their blast radius. On an account whose **Lambda concurrent-executions quota** (`L-B99A9384`) is at the reduced default of **10** — which AWS applies to some new accounts — reserving *any* concurrency is rejected, because it would drop unreserved capacity below Lambda's account-wide minimum of 10. The deploy runs for ~30 minutes and then fails and rolls back on `coa-dev-vkg` (and `coa-dev-sources` after it) with:

```
Specified ReservedConcurrentExecutions for function decreases account's
UnreservedConcurrentExecution below its minimum value of [10].
```

The preflight check (`scripts/preflight-deploy.sh`) catches this before CDK runs. Raising the quota via `request-service-quota-increase` on `L-B99A9384` opens an AWS Support case rather than being granted immediately, so the fast unblock is to deploy without the reservations:

```bash
# Deploy without reserving Lambda concurrency (default is 5 per function)
SCL_LAMBDA_RESERVED_CONCURRENCY=0 make deploy-dev
```

The value must be a non-negative integer; CDK fails synth otherwise. `0` (or unset via context) omits the reservation entirely — the functions then draw from the shared unreserved pool with no dedicated guarantee or cap, which is fine for a single-tenant evaluation. On a direct `cdk deploy`, pass it as context instead — `--context lambda_reserved_concurrency=0`, or set it in the `context` block of `infra/cdk.json`.

### VKG Task Sizing

Each namespace's Virtual Knowledge Graph runs as a Fargate task hosting Ontop.
The task's CPU, memory, and JVM heap are configurable on `VkgStack`, and the
**same values are propagated to the per-namespace reload Lambda** so a task
created at deploy time and one re-created by a later reload are provisioned
identically (a mismatch here is what caused permanently-`UNHEALTHY` VKGs — a
reloaded task ran under-provisioned and the OWL2QL translation pass never
finished).

| `VkgStack` prop | Default | Controls |
|-----------------|---------|----------|
| `cpu` | `1024` (1 vCPU) | Fargate task CPU units. |
| `memoryLimitMiB` | `2048` (2 GB) | Fargate task memory. |
| `ontopJavaArgs` | `-Xmx1536m -Xms512m` | JVM args passed to the Ontop launcher via `ONTOP_JAVA_ARGS`. |

**When to override the defaults:** increase CPU/memory (and heap in step) when a
namespace's VKG reports `DEGRADED` health, when large-ontology reformulation
times out, or for ontologies with many classes/properties where OWL2QL rewriting
is expensive. As a rule of thumb for sizing by ontology complexity (measured by
the class + property count of the induced ontology, which is what drives OWL2QL
rewriting cost):

| Ontology size | Classes + properties | Suggested `cpu` / `memoryLimitMiB` |
|---------------|----------------------|------------------------------------|
| Small         | up to ~200           | `1024` / `2048` (the default)      |
| Medium        | ~200–1000            | `2048` / `4096`                    |
| Large         | ~1000–5000           | `4096` / `8192`                    |
| Very large    | more than ~5000      | `8192` / `16384` (and profile)     |

The `1024` / `2048` default (heap `-Xmx1536m`) is sized for small-to-medium
ontologies (up to ~1000 classes + properties). Treat these as starting points —
if a namespace still reports `DEGRADED` after a reload, step up to the next row.
When `ontopJavaArgs` is left unset the heap is derived automatically from
`memoryLimitMiB` (max heap ~= 75%, initial ~= 25%), so bumping memory alone
scales the heap in step.

**Heap sizing guidance:** set the Ontop max heap (`-Xmx`) to roughly **60–75% of
the task memory**, leaving headroom for the JVM's own overhead and the OS. For
example, a `memoryLimitMiB: 4096` task pairs with `ontopJavaArgs:
"-Xmx3072m -Xms1024m"`. The heap **must** be passed via `ONTOP_JAVA_ARGS` — the
Ontop launcher does not read `JAVA_OPTS`, so a heap set there is silently ignored
and the container falls back to its small built-in default.

Configure these where you instantiate `VkgStack` in `bin/app.ts` (or your
equivalent CDK app entry point):

```typescript
new VkgStack(app, "coa-dev-vkg", {
  // ...existing props...
  cpu: 2048,
  memoryLimitMiB: 4096,
  ontopJavaArgs: "-Xmx3072m -Xms1024m",
});
```

After changing these, redeploy the VKG stack and trigger a reload for existing
namespaces so their tasks pick up the new sizing.

### Internal Environment Variables

These are wired by the infrastructure stacks or carry in-code defaults. They are
not part of the normal deployment interface — most deployments never touch them —
but the ones marked as a *default* below can be overridden on the relevant Lambda
when tuning a large or pathological source.

| Variable | Set By | Purpose |
|----------|--------|---------|
| `SCL_MCP_MODE` | `mcp-stack.ts` | Switches the container entrypoint between Context Manager (default) and MCP Server. When set to `"true"`, the container starts in MCP mode. |
| `BULK_REVIEW_PAGE_BUDGET` | `worker.py` default | Per-invocation table budget for the bulk-review worker (default `1000`); when a source has more tables, the worker processes one page, re-enqueues a continuation, and resumes across chained invocations rather than silently capping. |
| `BULK_REVIEW_WALL_CLOCK_BUDGET_S` | `worker.py` default | Per-invocation wall-clock budget in seconds (default `240`), a second guard under the 5-minute Lambda timeout that stops the worker after the current search page and continues in a fresh invocation when neared. |
| `REVIEW_QUEUE_URL` | `sources-stack.ts` | SQS review-queue URL the bulk-review worker re-enqueues page continuations to, wiring its own self-continuation. |
| `DATAZONE_CLEANUP_BUDGET_S` | `sources_handler.py` default | **Upper clamp** (not an absolute deadline) on DataZone asset cleanup during source delete, in seconds (default `240`). The effective deadline is `min(this value, remaining Lambda time − 2s safety margin)`, so it can shorten the window but never extend past the real timeout. On expiry the delete returns partial-completion counts and the namespace-deletion sweep finishes the remainder, instead of the Lambda being killed mid-request. A malformed value falls back to `240`. |
| `PROBE_TIMEOUT_MS` | `dialects.py` default | Session-level `statement_timeout` (milliseconds, default `30000`) applied on Postgres/Redshift connections before the column-cardinality probe used for enum detection. Bounds the `COUNT(*) / COUNT(DISTINCT …)` probe and the sampling query that follows it, so one wide or high-cardinality column cannot consume the whole 15-minute `sources-db-connector` budget. Must be a positive integer; a malformed or non-positive value logs a warning and falls back to the default rather than removing the bound. Set it **lower** to fail faster on pathological columns, **higher** only if legitimately large tables are being skipped — too high defeats the protection, and a timed-out column is simply treated as "not an enum" (logged as `distinct_probe_timeout`). |

**Migration note for `DATAZONE_CLEANUP_BUDGET_S`.** Before the GH-137 fix this was
an absolute wall-clock deadline that bounded only the delete loop, and its `240`
default was 8× the `sources-api` Lambda's real 30-second timeout — so the
graceful early-exit could never fire and deleting a several-hundred-table source
always timed out. It is now derived from the Lambda's actual remaining time and
bounds the DataZone search/pagination phase as well. **No action is required:**
the new behaviour is self-correcting and strictly safer, and it stays correct if
the Lambda timeout is ever changed. If you previously raised this value to work
around delete timeouts, that override is now redundant and can be removed — it
cannot extend the deadline past the real remaining time.


### API Request Limits and Rate Limiting

Context Ontology Accelerator throttles inbound API traffic at three layers. All limits are **soft** —
they ship with conservative defaults and can be tuned per deployment. Defaults
are defined in `infra/lib/constants.ts`.

| Layer | Default | Applies to |
|-------|---------|------------|
| WAF per-IP rate limit | **2000 requests / 5 min per source IP** (block) | Every request, at the edge (CloudFront) and API (before auth) |
| API Gateway stage-wide throttle | **50 rps / 100 burst** | All API methods |
| API Gateway per-operation throttle | **5 rps / 10 burst** | Expensive, job-launching operations only |

**Expensive operations** (the routes that receive the tighter 5 rps / 10 burst
throttle, because each request launches a long-running job or a heavy graph
write):

- `POST /namespaces/{namespaceId}/induce` — ontology induction
- `POST /namespaces/{namespaceId}/sources/{sourceId}/rescan` — source rescan
- `POST /namespaces/{namespaceId}/import-osi` — metric OSI import
- `POST /namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints`
- `POST /namespaces/{namespaceId}/proposals/{proposalId}/validate`
- `POST /namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints`
- `POST /namespaces/{namespaceId}/proposals/{proposalId}/accept`

**Overriding the limits.** The values are construct props with defaults in
`infra/lib/constants.ts`, not environment variables.

- The **API Gateway throttles** are `ApiStackProps` fields — pass them where
  `ApiStack` is instantiated in `infra/bin/app.ts`:
    - `throttleRateLimit` / `throttleBurstLimit` — stage-wide throttle
    - `expensiveThrottleRateLimit` / `expensiveThrottleBurstLimit` — per-operation throttle
- The **WAF per-IP limit** (`WafWebAclProps.rateLimit`) is not currently plumbed
  through to `bin/app.ts`. The `WafWebAcl` construct is created inside `ApiStack`
  and `EdgeWafStack`, so to change it either edit the
  `DEFAULT_WAF_RATE_LIMIT_PER_5MIN` default in `constants.ts` or add a
  pass-through prop on those two stacks. Setting `rateLimit: 0` on the construct
  disables the rate rule (used when bringing your own WebACL — see below).

To bring an entirely pre-built WebACL instead of the auto-created one, set the
`api_web_acl_id` (REGIONAL, API stage) or `cloudfront_web_acl_id` (CloudFront
edge) CDK context value; the `WafWebAcl` construct — and its rate rule — is then
skipped entirely for that surface.

**When to tune for production.** The defaults suit a modest authenticated-user
population. Raise the stage-wide and per-operation throttles if legitimate
concurrent usage triggers 429s; raise the WAF limit if a shared-egress client
(corporate NAT, VPN) legitimately exceeds 2000 req/5min from one IP.

**Monitoring.** Throttling is visible in CloudWatch:

- API Gateway `4XXError` metric — includes HTTP **429 Too Many Requests**
  returned when a caller exceeds a throttle (the burst bucket is empty).
- WAF `BlockedRequests` metric, rule `{prefix}-{env}-{api|cloudfront}-waf-rate-limit-per-ip`
  — counts IPs blocked by the rate-based rule (the API stage WebACL uses the
  `api` prefix, the CloudFront edge WebACL uses `cloudfront`).

A client receiving **HTTP 429** should back off and retry with jitter; the limit
is per-second steady-state with a short burst allowance, so a brief pause clears
it.

## Environments and production hardening

The deployment's `env` (set via `--context env=...`; `dev` by default) does more than
name resources — several safeguards switch on automatically for a **production**
deployment (`env=prod`). You do not toggle these individually; selecting the prod
environment enables them together.

| Area | Non-production (`dev`, etc.) | Production (`env=prod`) |
|------|------------------------------|--------------------------|
| Custom domain / TLS | Optional; falls back to a permissive CORS origin if no UI domain is set | **Required** — a UI custom domain must be configured or synth fails (CORS is locked to that origin) |
| Guardrail & authorization | Enforced | **Always enforced** — cannot be disabled or run in a fail-open mode |
| Data stores (S3, DynamoDB) | Removed on stack deletion for easy teardown | **Retained** on stack deletion to prevent accidental data loss |
| Graph database (Neptune) | Removed on stack deletion | **Deletion protection enabled** — a stack deletion is blocked until protection is manually disabled |
| Web-app authentication | Development sign-in conveniences enabled | Standard secure remote-password flow only |
| Search collection network access | Reachable for local/integration testing | **VPC-only** |
| API request logging | Full request/response tracing | Reduced (no payload tracing) |

!!! note "Custom domain is a hard requirement in production"
    A production deployment with no UI custom domain fails at synth
    (`Production deployments require a UI custom domain for CORS`). Provision the
    domain and certificates before deploying — the required variables and their
    certificate regions are described under "Configuration Options" above.

!!! warning "Data retention on production stacks"
    In production, S3 and DynamoDB data stores are retained on stack deletion, and
    Neptune has deletion protection enabled — a stack deletion is blocked until that
    protection is manually removed. Removing production data is therefore a deliberate,
    separate action. Plan teardown of a production environment accordingly.

## Post-Deployment

### 1. Create Your First User

By default (`idpType: COGNITO`, no `oidcSettings` configured), the deployment
creates a Cognito User Pool. Add users via the AWS Console or CLI:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <POOL_ID> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true
```

The User Pool ID is in the `coa-dev-auth` stack outputs or SSM at `/<prefix>/userpool-id`.

!!! note "Using your own identity provider instead"
    Context Ontology Accelerator can use an external OIDC-compliant IdP (Okta, Azure AD,
    Auth0, Keycloak, etc.) instead of Cognito — set `idpType: "OIDC"` with
    `oidcSettings` in the SSM config at `/<prefix>/config` **before** running
    `make deploy-dev` (this must be configured pre-deploy; switching IdP types
    afterward requires a redeploy of the `coa-dev-auth` stack). See the
    [Authentication Setup Guide](authentication-setup.md) for the full config
    reference and a step-by-step Okta walkthrough. In OIDC mode, the
    `coa-dev-auth` stack does **not** create a Cognito User Pool at all — the
    commands above won't apply. Instead, create/manage users directly in your
    IdP; access is controlled entirely by [grants](role-permission-management.md)
    against the user's email or IdP group.

### 2. Access the Web App

The web app URL is in the `coa-dev-web` stack's CloudFormation output (`WebAppUrl`). Sign in with the Cognito user you created (or your external IdP's credentials, if using OIDC mode).

### 3. Grant Platform Admin

To give a user full access, grant the `platform-admin` role via the API or web app Permissions page.

## Guardrail Observability

Every Bedrock call routed through a guardrail — the kg-build content screener,
the enrichment and ontology-shape inference tasks, and the serve NL→SPARQL and
retrieval paths — emits a CloudWatch metric and a structured log line on **both**
the allow and the block outcome. This lets operators watch the block rate and
the latency the guardrail adds without any content or PII leaving the request.

### CloudWatch Metrics

All three metrics live in the **`COA/Guardrails`** namespace:

| Metric | Unit | Dimensions | Meaning |
|--------|------|------------|---------|
| `GuardrailInvocations` | Count | `Component`, `Decision` | One per guarded call. `Decision` is `ALLOW` or `BLOCK`. |
| `GuardrailBlocked` | Count | `Component` | Emitted (value `1`) only when the guardrail intervened with a block. |
| `GuardrailLatency` | Milliseconds | `Component` | Wall-clock time the guarded Bedrock call took. |

`Component` is one of `kg-build`, `enrichment`, `ontology-shapes`,
`nl-to-sparql`, or `serve-retrieval`.

**Block rate is not a stored metric** — compute it in a CloudWatch math
expression so there is a single source of truth:

```
100 * (GuardrailBlocked / GuardrailInvocations)
```

(Sum `GuardrailInvocations` across both `Decision` values, or drop the
`Decision` dimension in the metric selector, before dividing.)

### Viewing the metrics

List the metrics and read the last hour of a component's invocations:

```bash
# What's being published
aws cloudwatch list-metrics --namespace COA/Guardrails

# ALLOW+BLOCK invocations for the NL→SPARQL path, 5-min buckets
aws cloudwatch get-metric-statistics \
  --namespace COA/Guardrails --metric-name GuardrailInvocations \
  --dimensions Name=Component,Value=nl-to-sparql \
  --start-time "$(date -u -d '1 hour ago' +%FT%TZ)" \
  --end-time "$(date -u +%FT%TZ)" \
  --period 300 --statistics Sum
```

### Dashboard block-rate widget

A metric-math widget that graphs the per-component block-rate percentage:

```json
{
  "type": "metric",
  "properties": {
    "title": "Guardrail block rate (%)",
    "region": "us-west-2",
    "metrics": [
      [ "COA/Guardrails", "GuardrailBlocked", "Component", "nl-to-sparql", { "id": "b", "visible": false } ],
      [ "COA/Guardrails", "GuardrailInvocations", "Component", "nl-to-sparql", { "id": "i", "visible": false } ],
      [ { "expression": "100 * b / i", "label": "nl-to-sparql", "id": "rate" } ]
    ],
    "stat": "Sum",
    "period": 300
  }
}
```

### Recommended alarm

Guardrails are a security control, so a sustained spike in the block rate is
worth paging on — it signals either an attack (prompt injection, PII probing) or
a misconfigured upstream. Alarm on the block-rate math expression:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name coa-guardrail-block-rate-high \
  --alarm-description "Guardrail block rate exceeded 20% for 15 minutes" \
  --comparison-operator GreaterThanThreshold --threshold 20 \
  --evaluation-periods 3 --datapoints-to-alarm 3 \
  --metrics '[
    {"Id":"rate","Expression":"100 * b / i","Label":"block-rate","ReturnData":true},
    {"Id":"b","MetricStat":{"Metric":{"Namespace":"COA/Guardrails","MetricName":"GuardrailBlocked"},"Period":300,"Stat":"Sum"},"ReturnData":false},
    {"Id":"i","MetricStat":{"Metric":{"Namespace":"COA/Guardrails","MetricName":"GuardrailInvocations"},"Period":300,"Stat":"Sum"},"ReturnData":false}
  ]'
```

A high `GuardrailLatency` p90/p99 (e.g. above a few hundred ms) is also worth an
alarm — it surfaces guardrail evaluation slowing down the request path.

### Structured decision logs

Alongside the metrics, each decision writes one JSON log line
(`event: "guardrail_decision"`) to the component's CloudWatch Logs group:

| Field | Example | Meaning |
|-------|---------|---------|
| `component` | `nl-to-sparql` | Which decision site emitted the line. |
| `decision` | `ALLOW` / `BLOCK` | The guardrail outcome. |
| `filter_type` | `CONTENT` / `PII` / `TOPIC` / `NONE` | Which policy family fired (most-severe wins; `NONE` on an allow). |
| `latency_ms` | `142.3` | Wall-clock of the guarded call. |

The log **never** carries the matched content or the PII value — only the
policy *category* — so these lines are safe to retain and query. Find recent
blocks across a log group with CloudWatch Logs Insights:

```
fields @timestamp, component, filter_type, latency_ms
| filter event = "guardrail_decision" and decision = "BLOCK"
| sort @timestamp desc
```

> **Note on the region.** Metrics are published in the deployed region (the ECS
> tasks resolve it from `BEDROCK_REGION`/`LLM_REGION`, not `AWS_REGION`). If a
> dashboard is empty, confirm you are looking at the region the stack deployed
> to, not `us-east-1`.

## Updating

To deploy updates after pulling new code:

```bash
git pull
make deploy-dev
```

CDK performs incremental updates — only changed stacks are redeployed.

## Tearing Down

```bash
make destroy-dev
```

This runs `scripts/destroy.sh dev`, which orchestrates teardown of resources CFN can't cleanly delete on its own before running `cdk destroy --all`:

1. **Deletes AgentCore Runtimes** (serve + mcp) directly via the Bedrock AgentCore API.
2. **Waits for AgentCore-owned ENIs to detach** from their security groups — see the [AgentCore ENI wait](#agentcore-eni-wait-can-take-hours) note below. This step is currently a no-op pending a shorter, reliable detach signal.
3. **Deletes VKG's per-namespace ECS services** (created outside CloudFormation by the ontology-reload Lambda) and waits for them to reach `INACTIVE`.
4. **Force-deletes the DataZone domain** via `delete-domain --skip-deletion-check`, which cascades through every project, asset, and asset type under it — CloudFormation's own delete can't do this (see [FAQ](#why-does-namespace-stack-fail-to-delete-with-domain-not-empty)).
5. **Runs `cdk destroy --all`**, then verifies no stacks remain.

Same environment-variable overrides as `make deploy-dev` — pass the same `SCL_PREFIX` and region you deployed with:

```bash
SCL_PREFIX=myproject AWS_DEFAULT_REGION=us-west-2 make destroy-dev
```

Additional destroy-specific variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCL_DESTROY_YES` | unset | Set to `1` to skip the interactive confirmation prompt (e.g. in CI). |
| `SCL_ENI_WAIT_MAX_SECONDS` | `600` | Max wait for AgentCore ENI detach (step 2 — currently disabled). |
| `SCL_ECS_WAIT_MAX_SECONDS` | `300` | Max wait for VKG ECS services to reach `INACTIVE`. |
| `SCL_DOMAIN_WAIT_MAX_SECONDS` | `300` | Max wait for the DataZone domain to finish deleting. |

Steps 1-4 are idempotent — if the script exits early or a step warns and continues, re-running it later picks up from an already-deleted/in-progress state.

!!! warning
    This deletes all resources including databases and stored data. Neptune and OpenSearch data is not recoverable after deletion.

If `make destroy-dev` reports stacks that didn't fully delete, it also scans their CloudFormation events for two known failure signatures and prints the exact fix — see [Troubleshooting](#troubleshooting) below. For anything else, inspect the specific failure with:

```bash
aws cloudformation describe-stack-events --stack-name <PREFIX>-dev-<stack> --region <REGION>
```

### Manual destroy (advanced / non-`dev` environments)

`make destroy-dev` always targets `env=dev`. For any other environment, or if you need to run the underlying steps individually, call the script directly with the environment name:

```bash
cd <repo-root>
AWS_DEFAULT_REGION=<REGION> SCL_PREFIX=<PREFIX> ./scripts/destroy.sh <env>
```

Or fall back to a plain `cdk destroy` (skips all the pre-cleanup steps above — expect the known failures documented in Troubleshooting):

```bash
cd infra
AWS_DEFAULT_REGION=<REGION> npx cdk destroy --all \
  --context env=dev \
  --context resource_prefix=<PREFIX>
```

## Troubleshooting

### `make deploy-dev` fails preflight with "no IAM role named 'Admin'"

**Symptom:** deploy stops immediately with `ERROR: No SMUS admin principal configured, and this account has no IAM role named 'Admin' to fall back to.`

**Cause:** the `*-namespace` stack's `DomainLoginRole` needs at least one IAM role/user ARN to trust — the specific principal(s) human admins federate into to access the SMUS console. It defaults to the account's `Admin` role, which is an Amazon-internal account convention, not something AWS or this project creates. `make deploy-dev` checks whether that role actually exists before running CDK, so accounts without it fail here instead of several stacks deep into a CloudFormation rollback.

**Fix:** set `SCL_SMUS_ADMIN_ARNS` to the ARN(s) of the role(s) your admins assume — comma-separated for more than one. For an IAM Identity Center account, this is your permission set's federated role:

```bash
SCL_SMUS_ADMIN_ARNS=arn:aws:iam::<account>:role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_AdministratorAccess_<suffix> \
make deploy-dev
```

**If you hit the underlying CloudFormation failure directly** (e.g. running `cdk deploy` by hand, bypassing the preflight): `coa-dev-namespace` reaches `CREATE_FAILED` → `ROLLBACK_COMPLETE` on `DomainLoginRole` with `Invalid principal in policy`. If you retry with `SCL_SMUS_ADMIN_ARNS` now set and the stack fails again — this time on `SMUSDomain` with `Domain name already exists under this account (Service: DataZone, Status Code: 409)` — the first rollback left an orphaned DataZone domain behind (CloudFormation's stack rollback deletes the stack's other resources, but not this one). Force-delete the leftover domain, then retry:

```bash
DOMAIN_ID=$(aws datazone list-domains --query "items[?name=='<PREFIX>-<ENV>-smus-catalog'].id" --output text)
aws datazone delete-domain --identifier "$DOMAIN_ID" --skip-deletion-check
# wait for status DELETED, then delete the ROLLBACK_COMPLETE stack and redeploy
```

### AgentCore ENI wait can take hours

**Symptom:** the `*-mcp` and `*-serve` stacks fail to delete their security groups with `has a dependent object` (`DependencyViolation`), even after `make destroy-dev` deletes the AgentCore Runtimes.

**Cause:** AgentCore Runtime provisions VPC network interfaces (ENIs) directly, outside CloudFormation. Runtime deletion doesn't synchronously release them — in practice this has been observed to take **several hours, and sometimes days**, not the few minutes originally expected. CloudFormation has no visibility into these ENIs and tries to delete the security group immediately, before they're gone.

**Fix:** there isn't a fast one. `make destroy-dev` already deletes the Runtimes as its first step to start the detach clock as early as possible, but you generally need to **wait and retry later** (a few hours to a day) rather than intervene:

```bash
# Re-run later — steps 1-4 are idempotent and safe to repeat.
make destroy-dev
```

Do not attempt to manually detach or delete the ENIs yourself — they're AWS-managed and manual intervention doesn't speed up the release. If it's still stuck after a day or more, treat it as an AWS-side issue rather than something to fix locally.

### Why does namespace stack fail to delete with "domain not empty"?

**Symptom:** the `*-namespace` stack fails to delete with `Domain cannot be deleted because there are existing projects under this domain`, or the `SystemProject`/`DefaultProjectProfile` resources fail with `failed to stabilize due to internal failure` or `deletion prevented by project ...`.

**Cause:** every namespace creates its own DataZone project, and CloudFormation's native `DeleteDomain`/`DeleteProject` calls can't force past a shared asset type (`CoaRelationalTable`) that's still referenced by other projects' assets — which is always true while more than one namespace exists. `make destroy-dev` handles this automatically (step 4: `delete-domain --skip-deletion-check`, a true force-delete at the domain level). If you're running a bare `cdk destroy` instead of `make destroy-dev`, you'll hit this.

**Fix:** use `make destroy-dev` instead of a raw `cdk destroy`. If you're already stuck on a bare `cdk destroy`, resolve the SMUS domain ID from SSM and force-delete it directly, then retry:

```bash
DOMAIN_ID=$(aws ssm get-parameter --name /<PREFIX>/smus/domain-id --region <REGION> --query Parameter.Value --output text)
aws datazone delete-domain --identifier "$DOMAIN_ID" --skip-deletion-check --region <REGION>
# wait for it to finish deleting, then retry:
make destroy-dev
```

### Why does the VKG stack fail to delete the ECS cluster?

**Symptom:** the `*-vkg` stack fails with `The Cluster cannot be deleted while Services are active` (`ClusterContainsServicesException`).

**Cause:** per-namespace VKG services are created outside CloudFormation by the ontology-reload Lambda, so CloudFormation has no record of them and can't clean them up itself. `make destroy-dev` deletes these directly (step 3) before `cdk destroy` reaches the cluster.

**Fix:** use `make destroy-dev` instead of a raw `cdk destroy`. If already stuck, delete the services manually then retry:

```bash
CLUSTER=<PREFIX>-dev-vkg-cluster
for SVC in $(aws ecs list-services --cluster "$CLUSTER" --region <REGION> --query 'serviceArns[]' --output text); do
  aws ecs update-service --cluster "$CLUSTER" --service "$SVC" --desired-count 0 --region <REGION>
  aws ecs delete-service --cluster "$CLUSTER" --service "$SVC" --force --region <REGION>
done
make destroy-dev
```

### `GetBucketTagging: AccessDenied` or `NoSuchTagSet` on a bucket cleanup custom resource

**Symptom:** a stack fails to delete with something like:

```
User: arn:...assumed-role/<prefix>-storage-CustomS3AutoDeleteObjectsCustomReso-XXXXX
is not authorized to perform: s3:GetBucketTagging on resource: "arn:aws:s3:::<bucket>"
```

or

```
Received response status [FAILED] from custom resource.
Message returned: NoSuchTagSet: The TagSet does not exist
```

**Cause:** this is a known, currently-open timing issue in CDK's `Custom::S3AutoDeleteObjects` custom resource — its delete handler checks the bucket's `aws-cdk:auto-delete-objects` tag before proceeding, and that check can race the bucket policy that grants it access. This isn't caused by a missing IAM grant in Context Ontology Accelerator's own code (the grant is present and correctly ordered in the template) — it's an upstream CDK library limitation.

**Fix:** re-tag the bucket so the custom resource recognizes it, then retry:

```bash
aws s3api put-bucket-tagging --bucket <bucket-name> --region <REGION> \
  --tagging 'TagSet=[{Key=aws-cdk:auto-delete-objects,Value=true}]'
make destroy-dev
```

`make destroy-dev` detects this failure signature automatically in its post-destroy check and prints the exact command with the specific bucket name filled in — you don't need to identify the bucket yourself.

### A bucket fails to delete because it's "not empty"

**Symptom:** a bucket resource (commonly an access-logs bucket, e.g. `AccessLogsBucketCD784A59`) fails to delete with `The bucket you tried to delete is not empty`.

**Cause:** same upstream `Custom::S3AutoDeleteObjects` race as above, just surfacing as leftover objects instead of a tag-check failure.

**Fix:** force-delete every object version, then retry:

```bash
aws s3api delete-objects --bucket <bucket-name> --region <REGION> --delete "$(
  aws s3api list-object-versions --bucket <bucket-name> --region <REGION> \
    --output json --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}')"
make destroy-dev
```

`make destroy-dev` detects this failure signature automatically and prints the exact command with the specific bucket name filled in.

After a `DELETE_FAILED` stack is resolved (via any of the fixes above), verify no orphaned resources remain:

```bash
aws cloudformation list-stacks --region <REGION> \
  --stack-status-filter DELETE_FAILED | grep <PREFIX>-dev
```

This should return nothing.

## Appendix A — AWS Service Inventory, Quotas, and Considerations

This appendix consolidates every AWS service the solution touches, the account
quotas worth checking before a deploy, and the non-obvious considerations behind
specific resource choices. It is scoped to the **single-account, single-region
evaluation deployment** this guide describes.

It complements rather than repeats the sections above — see
[Service Quotas](#service-quotas) for the two quotas the preflight check
validates, [Region Prerequisites](#region-prerequisites) for regional
availability, and [API Request Limits](#api-request-limits-and-rate-limiting)
for inbound throttling.

### A.1 Services provisioned by CDK

Every service below is created by `make deploy-dev`. "Owning stacks" uses the
default `coa-dev-` prefix; substitute your own `SCL_PREFIX`. Derived from the
CDK module imports under `infra/lib/stacks/` — grep there for the current
mapping if a stack has since been refactored.

| Service | Used for | Owning stack(s) |
|---------|----------|-----------------|
| **Amazon VPC** (EC2) | Private networking for all compute; Lambdas and tasks are VPC-bound | `network`, and every service stack |
| **AWS Lambda** | API handlers, workers, custom resources, ontology reload | `api`, `data-layer`, `sources`, `ontology`, `vkg`, `namespace`, `metric-service`, `serve` |
| **Amazon ECS on Fargate** | Long-running containers: enrichment, doc ingestion, Ontop VKG | `sources`, `ontology`, `vkg` |
| **Amazon Neptune** | Knowledge-graph store (RDF/SPARQL) | `storage` |
| **Amazon OpenSearch Serverless** | Vector search for retrieval and grounding | `storage` (collection); `sources`, `ontology`, `serve`, `metric-service` (access policies) |
| **Amazon DynamoDB** | Job/proposal state, roles, grants, Cedar policies, sessions | `authnz`, `api`, `sources`, `ontology`, `namespace`, `metric-service`, `serve`, `mcp` |
| **Amazon S3** | Ontology artifacts, staged documents, web assets, access logs | `storage`, `sources`, `ontology`, `vkg`, `metric-service`, `web` |
| **Amazon Bedrock** | All LLM and embedding inference | Called by `ontology`, `sources`, `serve`, `mcp` |
| **Bedrock Guardrails** | Content/PII filtering on guarded calls | `guardrail` |
| **Bedrock AgentCore Runtime** | Hosts the Serve and MCP runtimes | `serve`, `mcp` |
| **Amazon DataZone** / SageMaker Unified Studio | Namespace domain, projects, data-asset catalog | `namespace` |
| **Amazon API Gateway** | REST API surface, authorizer, per-route throttles | `api` |
| **Amazon Cognito** | Default identity provider (skipped in OIDC mode) | `auth` |
| **AWS WAF** (WAFv2) | Per-IP rate limiting at the edge and API stage | `api`, `edge-waf` |
| **Amazon CloudFront** | Web-app CDN and TLS termination | `web` |
| **AWS Step Functions** | Source-scan orchestration, namespace-deletion pipeline | `sources`, `namespace` |
| **Amazon SQS** | Review queue, bulk-review worker continuations, async work | `api`, `sources`, `metric-service` |
| **Amazon EventBridge** | Scan/ingest event routing and scheduled rules | `sources`, `ontology`, `vkg` |
| **AWS Cloud Map** (servicediscovery) | Per-namespace VKG service discovery | `network`, `ontology`, `vkg` |
| **Amazon ECR** | Container images for Fargate tasks and AgentCore | `sources`; image assets in `serve`, `mcp` |
| **AWS Systems Manager** (SSM) | Cross-stack parameters and deploy config | Every stack |
| **Amazon CloudWatch** + Logs | Metrics, log groups, dashboards | `api`, `ontology`, `vkg`, `sources` |
| **AWS IAM** | Task/function roles, least-privilege grants | Every stack |
| **AWS Certificate Manager** | TLS certs for custom API/UI domains (optional) | `api`, `web` |
| **Amazon Route 53** | DNS records for custom domains (optional) | `api`, `web` |

### A.2 Services called at runtime but not provisioned

These are invoked by application code via the AWS SDK. They are **not created by
the deploy**, so they either already exist in your account, are created
per-source when you onboard data, or must be provisioned separately. This is the
group most easily missed when scoping IAM permissions or regional availability.

| Service | Used for | Called from |
|---------|----------|-------------|
| **Amazon Athena** | SQL over mapped data sources; table sampling; namespace cleanup | `packages/context-manager/.../clients/athena.py`, `packages/sources/.../database/connectors/athena_sampler.py`, `packages/control-plane/.../namespace/` |
| **AWS Glue** | Data Catalog reads; JDBC connection provisioning for scans | `packages/sources/.../database/connectors/glue_catalog.py`, `glue_connection_provisioner.py` |
| **AWS Lake Formation** | Permission grants when provisioning Glue connections | `packages/sources/.../database/connectors/glue_connection_provisioner.py` |
| **Amazon Textract** | OCR for scanned/PDF documents during preprocessing | `packages/sources/.../documents/preprocessing/handler.py` |
| **Amazon Redshift Data API** | Querying Redshift-backed sources | `packages/context-manager/.../clients/redshift_data.py` |
| **AWS Secrets Manager** | Data-source credentials (e.g. JDBC) | `packages/sources`, `packages/context-manager` |
| **AWS STS** | Role assumption and caller identity across services | Widely used (~10 modules) |


### A.3 Quotas to check before deploying

The preflight check (`scripts/preflight-deploy.sh`) validates only the first two.
The rest are listed because they are plausible blockers on a **new or sandbox
account**, where reduced default quotas are common. Confirm the ones relevant to
your usage rather than requesting increases for all of them.

Quota codes and the AWS defaults below were read from
`service-quotas list-aws-default-service-quotas`. Defaults change over time and
your account may already differ — treat them as a starting point and check your
own applied values with the commands that follow.

| Service | Quota (code) | AWS default | Why it matters here |
|---------|--------------|-------------|---------------------|
| **VPC** | VPCs per Region (`L-F678F1CE`) | 5 | Deploy creates one VPC unless `SCL_VPC_ID` is set. Preflight-checked — see [Service Quotas](#service-quotas) |
| **Lambda** | Concurrent executions (`L-B99A9384`) | 1,000 (10 on some new accounts) | Two functions reserve concurrency (default 5 each). Preflight-checked — see [Lambda reserved concurrency](#lambda-reserved-concurrency) |
| **ECS / Fargate** | Fargate On-Demand vCPU resource count (`L-3032A538`) | **6** | **The tightest fit here.** Tasks are 2 vCPU / 8 GB and 4 vCPU / 16 GB, and VKG runs one service **per namespace** — so a single enrichment task plus two namespaces' VKG services can exhaust the default |
| **OpenSearch Serverless** | Indexing max OCU (`L-50FA809B`), search max OCU (`L-4E98D4EB`) | 10 each | The collection runs with standby replicas enabled, which roughly doubles OCU consumption — see [A.4](#a4-considerations) |
| **Amazon Bedrock** | Per-model invocation TPM / RPM — one quota per model **per inference profile** (e.g. `L-CCA5DF70`, cross-region RPM for Claude Haiku 4.5) | Varies widely by model (hundreds of thousands to hundreds of millions of tokens/min) | Induction throughput is bounded by model tokens-per-minute far more often than by compute; a large scan can hit it. Look up the four models in [Bedrock model availability](#bedrock-model-availability) for the profile you actually invoke |
| **Amazon Athena** | Active DML queries (`L-FC5F6546`) | 200 | Concurrent structured queries at serve time share this pool |
| **Amazon Neptune** | DB instances (`L-368A3E00`) | 40 | Deploy creates one instance. The more likely constraint is not the count but whether the `db.r8g.large` Graviton class is offered in your region and AZ |
| **Amazon Textract** | `DetectDocumentText` TPS (`L-75788A8B`) | 25 | Only relevant if ingesting scanned documents at volume |

Quotas that are **not** worth checking, having confirmed the headroom: the deploy
creates 16 CloudFormation stacks against a default of 2,000 (`L-0485CB21`), about
11 S3 buckets against a far higher bucket limit, and consumes ENIs against a
default of 5,000 per Region (`L-DF5E4CA3`). A single NAT gateway
(`natGateways: 1`, `maxAzs: 2`) means one Elastic IP. AgentCore's slow ENI
release after teardown is a real problem, but it is a *timing* issue rather than a
quota one — see [AgentCore ENI wait](#agentcore-eni-wait-can-take-hours).

Check a specific quota's default and your account's applied value with:

```bash
# Find the code for a quota (search by name within a service)
aws service-quotas list-aws-default-service-quotas --service-code fargate \
  --query "Quotas[?contains(QuotaName, 'vCPU')].[QuotaCode,QuotaName,Value]" --output table

# Your account's applied value, which includes any increases already granted
aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 \
  --query 'Quota.[QuotaName,Value]' --output text
```

!!! warning "Quota increases are not always immediate"
    Some increases are auto-approved; others (notably Lambda concurrent
    executions, `L-B99A9384`) open an AWS Support case. If you are on a
    reduced-quota account and need to deploy today, prefer the documented
    workarounds — e.g. `SCL_LAMBDA_RESERVED_CONCURRENCY=0` — over waiting on an
    increase.

### A.4 Considerations

Non-obvious choices and consequences worth knowing before you deploy. Ordered
roughly by how likely each is to surprise you.

**Bedrock model quotas are the real throughput ceiling.** Induction and
enrichment are LLM-bound, not CPU-bound. A database scan calls Bedrock once per
discovered table, which is why the enrichment step has a 120-minute deadline
(see [Database scan enrichment timeout](#database-scan-enrichment-timeout)). If
scans fail on that deadline against a large source, check per-model TPM
throttling before raising the timeout — more time does not help if you are being
throttled.

**OpenSearch Serverless has a cost and OCU floor.** The collection is
`VECTORSEARCH` type inside a `NEXTGEN` collection group, which requires
`standbyReplicas: ENABLED` (see `infra/lib/stacks/foundation/storage-stack.ts`).
Standby replicas roughly double OCU consumption versus a single-AZ
configuration, and Serverless bills a minimum OCU allocation whether or not you
are querying — so an idle evaluation deployment still accrues cost here. This is
a durability/availability requirement of the collection group type, not a tunable
knob.

**Neptune runs a single provisioned instance, not Serverless.** One
`db.r8g.large` primary with no read replica, IAM (SigV4) auth, encryption at
rest, and 7-day backups. Deletion protection is enabled **only** when
`envName === "prod"`. Two consequences for a `dev` deploy: there is no
availability guarantee from a second instance, and the cluster is deletable — so
`make destroy-dev` will remove the graph and its data irreversibly.

**Neptune and OpenSearch dominate deploy time.** They, plus AgentCore Runtime
setup and cross-stack SSM waits, are why a fresh deploy takes ~1.5 hours. A
deploy that looks stuck is usually waiting on one of them — check
`aws cloudformation list-stacks` before intervening.

**Teardown is not symmetric with deploy.** AgentCore Runtime provisions ENIs
outside CloudFormation and can hold them for hours to days after runtime
deletion, blocking security-group deletion. VKG creates ECS services outside
CloudFormation (one per namespace), and DataZone domains need a force-delete.
`make destroy-dev` handles all three; a bare `cdk destroy` does not. Budget for
teardown taking longer than deploy, and see [Tearing Down](#tearing-down).

**Per-namespace resources scale with tenant count.** VKG runs one ECS service
per namespace, created by the ontology-reload Lambda rather than CloudFormation.
Fargate task count, ENIs, and Cloud Map registrations therefore grow as
namespaces are added — relevant to the Fargate vCPU and ENI quotas in
[A.3](#a3-quotas-to-check-before-deploying), and the reason those quotas are
worth checking against your expected namespace count rather than against a
single-namespace evaluation.

**Runtime-only services expand the IAM and regional surface.** The services in
[A.2](#a2-services-called-at-runtime-but-not-provisioned) — Athena, Glue, Lake
Formation, Textract, Redshift Data API, Secrets Manager — are not created by the
deploy but are called by it. Athena and Glue are exercised by any structured
source; Textract only by scanned-document ingestion; Redshift Data API only by
Redshift-backed sources. If you deploy to a region missing one of the services
you actually use, the failure appears at feature-use time rather than at deploy
time.

**`us-east-1` is always in the picture.** Even for a non-`us-east-1` deployment:
the CloudFront-scope WAF WebACL and the UI ACM certificate must live in
`us-east-1`, and ECR Public (build-time image pulls) is `us-east-1`-only. See
[Cross-region and AZ constraints](#cross-region-and-az-constraints).

**Availability Zones matter, not just regions.** AgentCore Runtime and the
OpenSearch Serverless VPC endpoint support different AZ subsets within
`us-east-1`, so `bin/app.ts` pins the VPC to their intersection via
`infra/lib/utils/agentcore-az.ts`. A service being listed as available in your
region does not mean it is available in every AZ of it.
