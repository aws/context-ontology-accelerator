# CDK Infrastructure

AWS CDK (TypeScript) infrastructure for the Context Ontology Accelerator.

## Structure

```
infra/
├── bin/app.ts              # CDK app entry point — instantiates all stacks
├── lib/
│   ├── constants.ts        # Shared constants and context key names
│   ├── context.ts          # CDK context helpers (prefix, env)
│   ├── paths.ts            # Portable monorepo path resolution
│   ├── types.ts            # Shared TypeScript types
│   ├── constructs/         # Reusable CDK constructs
│   ├── lambdas/            # Inline Lambda handlers (admin user, etc.)
│   ├── utils/
│   │   └── api-utils.ts    # OpenAPI spec enrichment (integrations, auth)
│   └── stacks/
│       ├── foundation/     # Network, auth, storage, authnz
│       └── services/       # API, namespace, unstructured ingestion
└── test/                   # Jest CDK tests
```

## Adding a New API Route with Lambda Proxy

The API stack uses a **Smithy-generated OpenAPI spec** with Lambda proxy integrations injected at CDK synth time. To add a new API route:

### 1. Define the operation in Smithy

Add your operation to the appropriate `.smithy` file in `models/src/main/smithy/`:

```smithy
@http(method: "POST", uri: "/widgets", code: 201)
operation CreateWidget {
    input := {
        @required
        name: String
    }
    output := {
        @required
        widget: WidgetDetail
    }
    errors: [ValidationError, AccessDeniedError]
}
```

Register it in the service's `operations` list in `control-plane.smithy`:

```smithy
service ControlPlaneService {
    operations: [
        // ... existing operations
        CreateWidget
    ]
}
```

Rebuild the Smithy model to regenerate the OpenAPI spec:

```bash
cd models && ./gradlew clean build
```

### 2. Create the Lambda handler

Create a Python Lambda in the appropriate package (e.g., `packages/control-plane/`). The handler receives API Gateway Lambda proxy events:

```python
def handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"widget": {...}}),
    }
```

### 3. Define the Lambda in CDK

In the relevant service stack (or create a new one), define the Lambda function:

```typescript
const widgetFn = new lambda.Function(this, "WidgetFn", {
  functionName: this.prefixed("widget-handler"),
  runtime: lambda.Runtime.PYTHON_3_12,
  handler: "my_package.widget.handler",
  code: lambda.Code.fromAsset(Paths.controlPlane),
  vpc: props.vpc,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});
```

### 4. Wire the Lambda to the API path

In `bin/app.ts`, pass the Lambda to the `ApiStack` via `pathHandlers`:

```typescript
const api = new ApiStack(app, `${stackPrefix}-api`, {
  // ... other props
  pathHandlers: {
    "/widgets": widgetFn,
    "/widgets/{widgetId}": widgetByIdFn,
    // All HTTP methods on a path route to the same Lambda.
    // The Lambda inspects event.httpMethod to dispatch internally.
  },
});
```

Any Smithy-defined path **not** in `pathHandlers` automatically falls through to a 501 "Not Implemented" stub Lambda. The custom authorizer protects all routes unless explicitly listed in `unsecuredPaths`.

### 5. Deploy and verify

```bash
cd infra && npx cdk deploy coa-dev-api
```

## Path Resolution

All file paths use `infra/lib/paths.ts` which resolves from the monorepo root (found by walking up to `pnpm-workspace.yaml`). This works identically on local machines, CI runners, and fresh clones. Never use `__dirname` with relative `../` chains.

```typescript
import { Paths, fromRoot } from "../paths";

// Pre-built paths
Paths.controlPlane; // packages/control-plane
Paths.controlPlaneOpenApiSpec; // models/build/.../ControlPlaneService.openapi.json

// Custom one-off path
fromRoot("packages/my-new-package");
```

## Commands

```bash
make deploy-dev         # Full deploy: preflight → build → synth → deploy all stacks
make destroy-dev         # Full teardown: pre-destroy checks → cdk destroy --all → verify
make preflight           # Run pre-deploy checks only (pip, Docker, Smithy, VPC quota)
npx cdk synth            # Synthesize CloudFormation templates
npx cdk deploy <stack>   # Deploy a specific stack
npx cdk diff <stack>     # Preview changes
npx jest                 # Run CDK tests
```

### Deploying to a different region

The default region is `us-east-1`. To deploy to a different region:

1. Bootstrap CDK in the target region: `cdk bootstrap aws://<account>/<region>`
2. Set a unique prefix to avoid name collisions on global resources (S3 buckets, IAM roles):
   ```bash
   AWS_DEFAULT_REGION=us-east-2 SCL_PREFIX=coa-use2 make deploy-dev
   ```

### Pre-deploy preflight

`make deploy-dev` runs `scripts/preflight-deploy.sh` before building. It validates:

- **pip** — detects macOS Xcode stubs that look like pip but fail at runtime
- **Docker** — required for Lambda bundling fallback when local pip is unavailable
- **Smithy artifacts** — ensures `smithy-generated/openapi/` contains valid specs
- **VPC quota** — queries the account VPC limit and blocks if saturated

### Tearing down (`make destroy-dev`)

`make destroy-dev` runs `scripts/destroy.sh dev`, which orchestrates the
teardown of resources CFN doesn't fully own before running `cdk destroy`,
rather than relying on CFN's own delete handlers or custom resources for
these:

1. **Delete AgentCore Runtimes** (serve + mcp) directly via
   `bedrock-agentcore-control delete-agent-runtime`, resolving each
   runtime's ID from the `/serve/runtime-arn` and `/mcp/runtime-arn` SSM
   parameters.
2. **Wait for AgentCore-owned ENIs to detach** from the runtimes'
   security groups (`ec2 describe-network-interfaces`). AgentCore
   Runtime ENIs are provisioned outside CFN and can take a long time to
   detach after Runtime delete — observed up to several hours in
   practice, not a hang (#661, see also
   https://github.com/hashicorp/terraform-provider-aws/issues/47399 for
   the equivalent Terraform-side report). If they haven't detached
   within `SCL_ENI_WAIT_MAX_SECONDS` (default 600s), the script **stops
   here** rather than proceeding into `cdk destroy`, where
   `DeleteSecurityGroup` would just fail with `DependencyViolation`.
   **Currently disabled** in `destroy.sh` (commented out) given the wait
   time observed in practice — re-enable once a shorter, reliable ENI
   detach signal is confirmed.
3. **Delete VKG's per-namespace ECS services** and wait for them to
   reach `INACTIVE`. These are created outside CFN by the VKG reload
   Lambda reacting to `ontology.published` events, so ECS's
   `DeleteCluster` fails with `ClusterContainsServicesException` while
   any are active.
4. **Force-delete the DataZone domain** via
   `datazone delete-domain --skip-deletion-check`, resolving the domain
   ID from the `/smus/domain-id` SSM parameter, then wait for it to fully
   delete. CFN already calls `DeleteProject` with `skipDeletionCheck=true`
   internally (confirmed via CloudTrail), but that cascade can't clear a
   shared asset type (e.g. `CoaRelationalTable`, owned by the system
   project) while OTHER projects' assets still reference that type — and
   every namespace has its own project holding its scanned-table assets
   of that type, so this isn't an edge case. That stalls the system
   project's delete ("failed to stabilize due to internal failure"),
   which stalls `DefaultProjectProfile`'s, which stalls the domain's
   ("Domain cannot be deleted because there are existing projects under
   this domain") — confirmed against a real stuck stack. DataZone's own
   `delete-domain --skip-deletion-check` is a true force-delete at the
   domain level with no such cross-project carve-out. The domain and its
   `FormType`, `Project`, `PolicyGrant`, `ProjectProfile`, `UserProfile`,
   `ProjectMembership`, and `Owner` child resources are
   `RemovalPolicy.RETAIN` in `namespace-stack.ts` so CFN never attempts
   (and fails) to delete any of them itself — this step is what actually
   tears the whole tree down.
5. **`cdk destroy --all`** with the same context resolution as
   `deploy.sh` (`SCL_PREFIX`, `SCL_PROJECT_TAG`, `SCL_VPC_ID` env vars
   are honored), then verify no `${SCL_PREFIX:-coa}-dev-*` stacks remain.
   If any stack is `DELETE_FAILED`, the script scans its stack events for
   two known upstream CDK `autoDeleteObjects` failure modes —
   `GetBucketTagging`/`NoSuchTagSet` (the custom resource's tag check
   racing bucket-policy propagation) and "bucket not empty" (the same
   race leaving objects undeleted) — and prints the exact remediation
   command for the specific bucket, rather than requiring a wiki lookup.
   These are a known, still-open CDK library limitation (the bucket
   policy granting the cleanup Lambda access already has a correct,
   explicit `DependsOn` ahead of the custom resource — this isn't a
   missing grant in this repo's code) — the script detects and guides
   through them, it doesn't prevent them.

Prompts for confirmation before starting (skip with `SCL_DESTROY_YES=1`,
e.g. in CI). Steps 1-4 are idempotent — if the script exits early (or a
step warns and continues), re-running it later picks up from an
already-deleted/in-progress state.

CDK destroys stacks in reverse of the dependency graph declared in
`bin/app.ts` (the same `addDependency()` calls used for deploy ordering) —
this script does not hand-roll stack-by-stack ordering for the CFN-owned
resources.

## Deployment Architecture

Stacks are deployed in dependency order (managed by `addDependency()` in `bin/app.ts`). Use `make deploy-dev` for a full deploy.

| Stack                | Resources                                                                                                                                                                          | Key Outputs                                                 | Dependencies                       | ~Deploy Time |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ---------------------------------- | ------------ |
| `coa-dev-network`    | VPC, subnets, VPC endpoints (S3, DynamoDB, Bedrock, AgentCore, ECR, SSM, CloudWatch), Neptune SG, ECS SG, Lambda SG, **Connector SG** (Glue Connection / Athena federation egress) | VpcId                                                       | —                                  | 3 min        |
| `coa-dev-auth`       | Cognito User Pool, App Client, SSM parameters                                                                                                                                      | Issuer URL, Client ID                                       | —                                  | 2 min        |
| `coa-dev-guardrail`  | Bedrock Guardrail                                                                                                                                                                  | GuardrailId                                                 | —                                  | 1 min        |
| `coa-dev-storage`    | Neptune cluster, OpenSearch Serverless collection, S3 buckets (assets, logs, **athena-results** 7-day retention, **athena-spill** 1-day retention)                                 | Neptune ARN, AOSS ARN                                       | network                            | 15 min       |
| `coa-dev-authnz`     | DynamoDB tables (roles, resource mappings), seed roles                                                                                                                             | Table ARNs                                                  | —                                  | 3 min        |
| `coa-dev-namespace`  | DataZone domain, project, asset types, **per-namespace Athena workgroups** (created/deleted with namespace lifecycle)                                                              | Domain ID                                                   | network, authnz                    | 5 min        |
| `coa-dev-api`        | API Gateway, Lambda authorizer, **cache-invalidation Lambda** (DynamoDB Streams → authorizer role-cache version bump, DLQ `coa-dev-cache-invalidation-dlq`), Lambda handlers                | API URL, REST API ID                                        | network, auth                      | 3 min        |
| `coa-dev-structured` | Structured ingestion Lambda, DynamoDB tables, **per-datasource Glue Connections + Athena federated catalogs** (created during scan)                                                | Table ARNs                                                  | network, namespace                 | 3 min        |
| `coa-dev-serve`      | AgentCore Runtime, private ALB, DNS resolver Lambda, target group                                                                                                                  | `AlbDnsName`, `AgentRuntimeArn`, `AgentRuntimeEndpointName` | network, auth, storage, structured | 10 min       |
| `coa-dev-web`        | CloudFront distribution, S3 bucket, VPC Origin (`/ws`)                                                                                                                             | Distribution URL                                            | auth, api, serve                   | 5 min        |
| `coa-dev-vkg`        | VKG ECS Fargate service                                                                                                                                                            | —                                                           | network                            | 5 min        |

### Serve Stack (`coa-dev-serve`)

Deploys the Context Manager on Bedrock AgentCore Runtime with WebSocket connectivity.

**Resources:**

- **AgentCore Runtime** — Container-based service hosting the Context Manager orchestrator
- **Private ALB** — Internal Application Load Balancer routing `/ws` to AgentCore
- **DNS Resolver Lambda** — Custom resource that resolves AgentCore VPC endpoint hostname to ENI IPs at deploy time (1 target per AZ)
- **IP Target Group** — Registers VPC endpoint IPs as ALB targets (HTTPS:443)
- **Security Groups** — ALB egress to VPC CIDR on 443; AgentCore egress to VPC + NAT

**WebSocket Connectivity:**

The ALB exposes a `/ws` path that rewrites to the full AgentCore WebSocket path (`/runtimes/<encoded-arn>/ws?qualifier=<endpoint>`). Browser clients access it through CloudFront VPC Origin.

| Access Method               | URL                                                                | Use Case                                                               |
| --------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| Via CloudFront (production) | `wss://<CloudFrontDomain>/ws`                                      | Browser clients (Playground UI) in public deployment mode              |
| Via ALB (VPC-internal)      | `ws://<AlbDnsName>/ws`                                             | Services within the VPC (e.g., ECS Fargate in private deployment mode) |
| Direct VPC endpoint         | `wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws` | Backend services (Lambda, ECS) with VPC access + JWT token             |

> **Note:** The ALB is private (not internet-facing). Browser clients access it through CloudFront VPC Origin in public mode, or directly in private deployment mode (ECS Fargate + ALB).

Obtain `AlbDnsName` from CDK outputs:

```bash
npx cdk deploy coa-dev-serve 2>&1 | grep AlbDnsName
```

**Authentication (JWT):**

AgentCore Runtime is configured with a Custom JWT Authorizer that validates tokens from the Cognito User Pool (or external OIDC provider).

| Property    | Source                             | Description                                       |
| ----------- | ---------------------------------- | ------------------------------------------------- |
| `issuerUrl` | `IdpAuthenticationStack.issuerUrl` | OIDC discovery base URL                           |
| `clientId`  | `IdpAuthenticationStack.clientId`  | Allowed audience for JWT `aud` claim validation   |

These are automatically populated from the auth stack. The authorizer validates:

- Token signature (via JWKS from discovery URL)
- `aud` claim matches `clientId`
- Token expiry

Clients pass the **ID Token** as `Authorization: Bearer <token>`.

> **Note:** The authorizer uses `allowedAudience` (validating the `aud` claim). ID tokens carry the user's identity (`email`) and group memberships (`groups` claim) needed for Cedar authorization. The `allowedClients` option is intentionally omitted because `client_id` is an access-token-only claim — setting it would reject ID tokens. This approach is IdP-agnostic and works with any OIDC-compliant provider (Cognito, Okta, Azure AD, etc.).

**IdP mode behavior:**

- **COGNITO** — issuerUrl = `https://cognito-idp.<region>.amazonaws.com/<poolId>`, clientId = User Pool Client ID
- **OIDC** — issuerUrl and clientId from external provider config (stored in SSM)
- **SAML** — Same as COGNITO (SAML federates into Cognito)

**Troubleshooting:**

| Symptom                             | Cause                           | Fix                                                          |
| ----------------------------------- | ------------------------------- | ------------------------------------------------------------ |
| 403 "Authorization method mismatch" | Using SigV4 instead of JWT      | Use `Authorization: Bearer <id_token>` header                |
| 403 "Token audience mismatch"       | Wrong client ID in `aud` claim  | Ensure the ID token's `aud` matches the configured `clientId`|
| 403 "Claim 'client_id' mismatch"    | Wrong App Client ID in config              | Verify `clientId` matches the Cognito App Client used for authentication        |
| ALB targets unhealthy               | VPC endpoint not reachable                 | Check security group egress, verify VPC endpoint exists                         |
| WebSocket connection refused        | ALB security group missing ingress         | Add ingress rule from CloudFront prefix list or VPC CIDR                        |

## Operational Monitoring

Each service stack provisions CloudWatch dashboards and alarms via the shared
`SclMonitoring` construct (`lib/constructs/monitoring.ts`), a thin builder over
[`cdk-monitoring-constructs`](https://github.com/cdklabs/cdk-monitoring-constructs).
A stack instantiates it once and registers the resources it owns
(`monitorLambda`, `monitorApi`, `monitorFargateService`, `monitorQueueWithDlq`,
`monitorStateMachine`, `monitorTable`, `monitorClusterCpuMem`).

### Dashboards

One dashboard per service, named `{prefix}-{service}` (e.g. `coa-dev-sources`).
Find them in the CloudWatch console under **Dashboards**, or:

```bash
aws cloudwatch list-dashboards --query "DashboardEntries[?starts_with(DashboardName,'coa-')].DashboardName"
```

| Dashboard              | Covers                                                              |
| ---------------------- | ------------------------------------------------------------------ |
| `{prefix}-api`         | API Gateway (p99 latency, 5XX)                                     |
| `{prefix}-sources`     | 8 Lambdas, 3 Step Functions, 3 SQS queues + DLQs                   |
| `{prefix}-metric-service` | 2 Lambdas, import queue + DLQ, import-jobs table                |
| `{prefix}-ontology`    | Fargate service + ontology-engine table (+ pre-existing `-induction`) |
| `{prefix}-serve`       | AOSS search-proxy Lambda                                           |
| `{prefix}-vkg`         | ECS cluster CPU/memory                                             |

> Ratio/rate widgets ("Time to drain", "TPS", "Producer vs Consumer") show a
> "data-points dropped (NaN)" note while a resource is **idle** — the expression
> divides by zero traffic. They render normally once the resource takes load;
> this is expected `cdk-monitoring-constructs` behavior, not a misconfiguration.

### Alarm thresholds

Thresholds are centralized as named constants at the top of
`lib/constructs/monitoring.ts` (single source of truth):

| Signal                     | Threshold        |
| -------------------------- | ---------------- |
| Lambda fault count         | > 1              |
| Lambda throttle count      | > 1              |
| API Gateway p99 latency    | > 5 s            |
| API Gateway 5XX count      | > 1              |
| Step Function failures     | > 1              |
| SQS DLQ message count      | > 1              |
| DynamoDB throttled events  | > 1              |
| ECS CPU / memory           | > 90%            |

### ⚠️ Alarms do not send notifications yet

Alarms are created **action-ready** (`actionsEnabled: true`) but with **no
notification destination wired** — they change state in the console but page no
one. Wiring is a single seam: `bin/app.ts` declares
`const alarmAction: IAlarmActionStrategy | undefined = undefined` and forwards it
to every stack. Supplying an `IAlarmActionStrategy` (SNS topic, chatbot) there
propagates to all dashboards/alarms with no per-stack change. Until then,
operators must watch the dashboards directly.

**Not covered:** Bedrock AgentCore Runtime *internal* health (MCP server, serve
context-manager) — `cdk-monitoring-constructs` has no native AgentCore support;
tracked as a follow-up.

## Athena Federation Architecture

JDBC data sources (PostgreSQL, MySQL, Redshift, Oracle, SQL Server, Snowflake) are queryable via Amazon Athena federated query. Three pieces of infrastructure support this:

### Storage (Storage Stack)

| Bucket                             | Purpose                                                                                   | Retention | Encryption                                            | SSM Parameter                           |
| ---------------------------------- | ----------------------------------------------------------------------------------------- | --------- | ----------------------------------------------------- | --------------------------------------- |
| `{prefix}athena-results-{account}` | Athena query results, one subdirectory per namespace (`s3://.../{namespaceId}/`)          | 7 days    | S3-managed (SSE-S3), enforce SSL, block public access | `/{prefix}/query/athena-results-bucket` |
| `{prefix}athena-spill-{account}`   | Spill data for Athena federated connectors when query results exceed Lambda memory limits | 1 day     | S3-managed (SSE-S3), enforce SSL, block public access | `/{prefix}/query/athena-spill-bucket`   |

Both buckets follow the production retention pattern: `RemovalPolicy.RETAIN` on `prod`, `DESTROY` (with `autoDeleteObjects`) on non-prod.

### Networking (Network Stack)

The **Connector Security Group** (`{prefix}connector-sg`) is attached to every Glue Connection used for federated queries.

- **Direction**: outbound only (`allowAllOutbound: true`); no inbound rules.
- **Purpose**: lets Glue Connections initiate JDBC sessions to source databases (port-agnostic at the SG level since database engines vary).
- **Customer requirement**: source-database security groups (RDS, Redshift, etc.) must allow inbound traffic from this SG on the database port. The SG ID is exported via `/{prefix}/network/connector-sg-id` for cross-account / cross-VPC reference.

> **Single-AZ caveat:** Glue Connections take exactly one `SubnetId`, so each connection lives in a single AZ. The structured stack currently picks the first `PRIVATE_WITH_EGRESS` subnet, meaning all federated queries flow through one AZ. An AZ outage will impact federated queries until the connection is recreated against another subnet. Multi-AZ resilience is tracked as future work; customers needing HA today should provision a second connection in another subnet and switch the catalog when needed.

### Per-namespace workgroups (Namespace Service)

Each namespace gets its own Athena workgroup, named `{prefix}{namespaceId}`:

- **Lifecycle**: created during namespace creation; deleted (with `RecursiveDeleteOption=true`) during namespace deletion.
- **Configuration**: result location `s3://{athena-results-bucket}/{namespaceId}/`, S3-managed encryption, Athena engine v3, `EnforceWorkGroupConfiguration=true`, CloudWatch metrics enabled.
- **Tagging**: `{tag-prefix}:namespace=<namespaceId>`, `{tag-prefix}:managed=true` for cost allocation and ownership tracking.
- **Failure handling**: if the workgroup cannot be confirmed to exist (e.g., the SSM parameter for the results bucket is missing or Athena returns an unexpected error), the `athenaWorkgroupName` field is **not** stored on the namespace record. We never persist a name for a workgroup we did not create.

### Per-data-source resources (Structured Stack)

For every JDBC data source on first scan:

| Resource                           | Naming                                   |
| ---------------------------------- | ---------------------------------------- |
| Glue Connection                    | `{prefix}ds-{datasourceId}`              |
| Athena DataCatalog (type `LAMBDA`) | `{prefix}ds_{sha256(datasourceId)[:32]}` |

For JDBC sources, the platform creates a **managed AWS Glue Data Catalog federated connector**: a Glue Connection with `AthenaProperties.MANAGED_CONNECTION=true` (AWS runs the connector — no Lambda, no CloudFormation stack), registered with Lake Formation (`register-resource --with-federation`), then a Glue federated catalog (`create-catalog` with a `FederatedCatalog` block). The source is then queryable under Athena's default `AwsDataCatalog` as `AwsDataCatalog.<catalog>.<schema>.<table>`, governed by Lake Formation.

Per-environment shared resources (Structured Stack): a single Glue Data Catalog / Lake Formation role (`{prefix}federated-catalog-role`, trusted by Glue + Lake Formation) set as each connection's `ROLE_ARN`. No connector Lambda or SAR application is deployed.

Provisioning runs in a **dedicated `{prefix}sources-federation-provisioner` Lambda**, invoked as its own JDBC-only step in the scan pipeline (after discovery, before enrichment). It is intentionally separate from the discovery connector so the broad Lake Formation privilege stays isolated to a single-purpose role. The step is **fatal**: if provisioning fails, the pipeline's Catch marks the scan `FAILED` and the source `SCAN_FAILED`, since a source that isn't queryable is not a successful scan.

**Required bootstrap (LF data-lake admin):** creating a federated catalog from a per-source connection requires `DATA_LOCATION_ACCESS` on that connection, which only a Lake Formation **data-lake admin** can grant (a non-admin cannot self-grant, and hybrid-access mode does not bypass it). The federation provisioner role must therefore be registered as an LF data-lake admin. This is **automated** by the `LakeFormationAdmin` custom resource (`infra/lib/constructs/lakeformation-admin.ts`), which registers the role non-destructively (read `DataLakeAdmins` → append → write, preserving existing admins).

There are two distinct roles here, and conflating them is the usual source of confusion:

| Role | Purpose | Where it comes from |
| --- | --- | --- |
| Federation provisioner (`{prefix}sources-federation-provisioner`) | The **target** — gets registered as an LF admin | Created by this stack; ARN published to `/{prefix}/sources/federation-provisioner-role-arn` |
| LF-admin custom resource's onEvent Lambda role | The **caller** — invokes `PutDataLakeSettings` | Auto-created by this stack, or supplied via `lf_admin_role_arn` |

`PutDataLakeSettings` may only be called by an existing data-lake admin once an account has any admins, and the caller is the second role. So:

- **Greenfield accounts** (zero LF admins): nothing to do. LF has no admin to enforce, so the auto-created caller role self-bootstraps.
- **Accounts that already have LF admins:** deploy with `lf_admin_role_arn` set to a role you already own and have already registered as an LF admin:

  ```bash
  cdk deploy coa-dev-sources -c lf_admin_role_arn=arn:aws:iam::<account>:role/<your-lf-admin>
  ```

  The role must be assumable by `lambda.amazonaws.com`; the construct attaches the IAM permissions the handler needs (LF settings read/write, one SSM parameter read, Lambda logging), so it does not need them beforehand.

Registering the *provisioner* role by hand does **not** substitute for this — that is the role being registered, not the one making the call, and the custom resource re-issues `PutDataLakeSettings` on every deploy even when the target is already in the admin list.

**Why there is no "register the caller role before the first deploy" option.** Earlier revisions of this doc said exactly that. It cannot work: without `lf_admin_role_arn` the caller role is created by the very deploy that needs it, and it is auto-named, so there is no ARN to register beforehand. Worse, the failed deploy's rollback deletes it, a first-ever create lands in `ROLLBACK_COMPLETE` (not updatable — it must be deleted and re-created), and the replacement gets a fresh random name suffix, invalidating any registration made against the old ARN.

**Troubleshooting — the `coa-<env>-sources` stack fails on `FederationProvisionerLfAdmin`** with an access-denied error from `PutDataLakeSettings`: the account already has LF admins and `lf_admin_role_arn` was not supplied. Re-deploy with it set. Note the failure is at **deploy** time — you will not reach a scan.

If you must recover an already-failed deploy without `lf_admin_role_arn`, use `--no-rollback` so the caller role survives with a stable ARN, register it, then deploy again:

```bash
cdk deploy coa-dev-sources --no-rollback
aws cloudformation describe-stack-resources --stack-name coa-dev-sources \
  --query "StackResources[?contains(LogicalResourceId,'FederationProvisionerLfAdminOnEventServiceRole')].PhysicalResourceId" \
  --output text
# Merge that role into the existing DataLakeAdmins as a current admin, then re-deploy.
aws lakeformation get-data-lake-settings
aws lakeformation put-data-lake-settings --data-lake-settings '{ ...existing settings..., \
  "DataLakeAdmins": [ ...existing admins..., {"DataLakePrincipalIdentifier": "<caller-role-arn>"} ] }'
```

**Troubleshooting — `CreateCatalog` fails with `Insufficient Lake Formation permission(s): Required Create Catalog on Catalog`:** the provisioner role is not an LF data-lake admin, i.e. the custom resource never succeeded. This is a Lake Formation authorization error, **not** IAM — adding `glue:CreateCatalog` / `lakeformation:*` to the role's IAM policy will not fix it. Confirm the sources stack deployed cleanly, then re-trigger the scan.

**What COA changes about your LF posture:** existing admins and all other settings (including the `IAM_ALLOWED_PRINCIPALS` defaults that make LF defer to IAM) are round-tripped untouched — COA does not flip an account into strict LF mode. But on a greenfield account the first deploy takes `DataLakeAdmins` from empty to non-empty, after which LF enforces admin-only access to settings; any principal that previously managed LF settings via IAM permission alone loses that ability until it is added as an admin. `cdk destroy` deregisters the principal COA registered.

The provisioning step is fatal, so a failed run marks the scan `FAILED` and the source `SCAN_FAILED`. After granting admin, re-trigger a scan on the affected source to retry. A failed attempt may leave an orphaned Glue connection (`{prefix}ds_*`); on retry it is reused via the handled `AlreadyExistsException`, or it can be deleted manually once it leaves the in-progress state.

Both per-source resources are deleted when the data source is deleted (the unified sources `_handle_delete` flow in `packages/sources/src/.../api/sources_handler.py`) and are surfaced to API consumers as `glueConnectionName` / `athenaDataCatalogName` on `GetSource` responses. See `packages/sources/README.md#athena-queryability` for end-user query examples.

## Upgrade Notes for Athena Federation

These notes apply to operators upgrading an existing deployment to a release that includes the Athena federation feature.

### What gets created automatically

- **Storage stack** provisions both new S3 buckets (`{prefix}athena-results-{account}` and `{prefix}athena-spill-{account}`) and their SSM parameters on the next `cdk deploy`.
- **Network stack** creates the `{prefix}connector-sg` security group and exports its ID under `/{prefix}/network/connector-sg-id` on the next `cdk deploy`.
- **Structured stack** picks up the connector SG and subnet wiring; no manual action needed.

No downtime is expected for these stack updates — all resources are additive.

### What does NOT change retroactively

- **Existing namespaces will NOT receive Athena workgroups.** The `_create_athena_workgroup` step runs only inside `CreateNamespace`. Namespaces created before the upgrade will have no `athenaWorkgroupName` and Athena queries against their federated catalogs are not yet supported. To enable federation on a legacy namespace, recreate it under a new name (or run a backfill workgroup-creation script — not provided).
- **Existing JDBC data sources will NOT have Glue Connections / Athena catalogs until the next successful scan.** Federation provisioning is invoked from the discovery handler (`_provision_athena_federation`) and runs at most once per source (idempotent — it skips if `glueConnectionName` is already populated). Triggering a scan on each source after the upgrade is sufficient; no DDB rewrite is required.
- **S3/Iceberg-backed Glue data sources are intentionally skipped.** Athena queries them natively via `AwsDataCatalog`, so no Glue Connection / federated catalog is created and `glueConnectionName` / `athenaDataCatalogName` remain null on those sources.

### Required customer action

- **Database security-group inbound rules must be updated** to allow traffic from `{prefix}connector-sg` on each database's listening port (e.g., PostgreSQL `5432`, MySQL `3306`, SQL Server `1433`, Oracle `1521`). Without this rule, scans will succeed (Glue Connection creation only validates configuration) but Athena `SELECT` queries will time out at connection time.
- The connector SG ID is published as `/{prefix}/network/connector-sg-id` in SSM Parameter Store for use in cross-account or external IaC.

### Rollback

- Removing the feature requires deleting per-namespace workgroups (`{prefix}{namespaceId}`) and per-source Glue Connections / Athena DataCatalogs (`{prefix}ds-{datasourceId}`) before destroying the storage/network resources, otherwise CDK delete will fail. The namespace `delete_handler` and the unified sources `_handle_delete` flow handle this automatically when those resources are deleted via the API.
