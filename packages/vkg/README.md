# Virtual Knowledge Graph (VKG)

Translation-only service wrapping Ontop 5.x. Translates SPARQL queries into SQL using ontology mappings, without connecting to a real database.

## Architecture

- **Ontop 5.x** — SPARQL-to-SQL translator (runs on port 8081 internally)
- **translate-server.py** — Python HTTP facade (port 8080) exposing `/translate` and `/health`
- **H2 in-memory** — schema-only database for Ontop's mapping parser (no real data)

At startup, the entrypoint downloads ontology + mappings from S3, loads the schema into H2, and starts both services.

## Production

The container is built from this Dockerfile and deployed to ECS. Ontology artifacts are fetched from S3 at runtime:

```
s3://{ONTOLOGY_BUCKET}/ontologies/{NAMESPACE}/{VERSION}/
  ├── ontology.ttl      # OWL ontology
  ├── mappings.obda     # Ontop OBDA mappings
  └── schema.sql        # H2 schema for mapping validation
```

To upload artifacts for a namespace:

```bash
aws s3 cp ./ontology.ttl s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
aws s3 cp ./mappings.obda s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
aws s3 cp ./schema.sql s3://$ONTOLOGY_BUCKET/ontologies/insurance/latest/
```

## Local Testing

Test fixtures are in `tests/fixtures/`. To run the VKG container locally with real S3 artifacts:

```bash
# Build the image
docker build -t coa-vkg:local packages/vkg/

# Run with real S3 artifacts (requires AWS credentials)
docker run -d --name coa-vkg -p 8090:8080 \
  -e ONTOLOGY_BUCKET=coa-dev-neptune-data-123456789012 \
  -e NAMESPACE=insurance \
  -e VERSION=latest \
  -e AWS_DEFAULT_REGION=us-east-1 \
  coa-vkg:local
```

### Test the translation endpoint

```bash
curl -s http://localhost:8090/sparql/translate \
  -H "Content-Type: application/json" \
  -d '{"sparql": "SELECT ?x WHERE { ?x a <http://example.org/insurance#Claim> }", "namespace": "insurance"}'
```

### Test fixtures

| File | Purpose |
|------|---------|
| `tests/fixtures/insurance.obda` | Sample OBDA mappings for the insurance namespace (Claim → claims table) |
| `tests/fixtures/schema.sql` | H2 schema mirroring Athena `insurance_lake.claims` for Ontop validation |

## Container Startup and Troubleshooting

### S3 download retry

At startup, the entrypoint downloads ontology artifacts from S3 with **3 attempts** and exponential backoff (2s, 4s, 8s delays). Expected log output during a transient failure:

```
[VKG] Downloading artifacts...
[VKG] WARN: S3 download attempt 1/3 failed — retrying in 2s
[VKG] WARN: S3 download attempt 2/3 failed — retrying in 4s
```

If all 3 attempts fail, the container enters **degraded mode** (503 on all `/sparql/translate` requests) and logs:

```
[VKG] WARN: Failed to download from S3 after 3 attempts — starting in degraded mode
```

**Common S3 issues:**
- Verify the S3 path exists: `aws s3 ls s3://$ONTOLOGY_BUCKET/ontologies/$NAMESPACE/$VERSION/`
- Check IAM permissions on the task role (needs `s3:GetObject` on `ontologies/*`)
- Confirm the bucket name matches the CDK-deployed bucket

**Recovery:** Fix the S3 artifacts, then restart the task. The health check will replace degraded containers automatically (see below).

## Task Sizing and Environment Variables

The VKG task's resource allocation and JVM heap are controlled by environment
variables set on the ECS task definition (and passed through by the reload
Lambda). Getting these right matters: an under-provisioned task cannot finish the
OWL2QL translation pass for larger ontologies, which leaves the container failing
its health check and the namespace reporting `DEGRADED` health indefinitely.

| Variable | Default | Purpose |
|----------|---------|---------|
| `VKG_TASK_CPU` | `1024` (1 vCPU) | Fargate task CPU units. Set on the reload Lambda; used when it registers a new task definition. |
| `VKG_TASK_MEMORY` | `2048` (2 GB) | Fargate task memory. |
| `ONTOP_JAVA_ARGS` | `-Xmx1536m -Xms512m` | JVM args for the Ontop launcher (read directly by the container). The reload Lambda's `VKG_ONTOP_JAVA_ARGS` is written into the task def as this variable. |

**`ONTOP_JAVA_ARGS`, not `JAVA_OPTS`.** The Ontop launcher reads its heap
settings from `ONTOP_JAVA_ARGS`. Setting `JAVA_OPTS` has **no effect** — the heap
is silently ignored and Ontop starts at its small built-in default regardless of
task memory. The entrypoint keeps a backward-compatibility fallback
(`ONTOP_JAVA_ARGS="${ONTOP_JAVA_ARGS:-${JAVA_OPTS:-}}"`) so a task def that still
only sets `JAVA_OPTS` is honoured, but new configuration should always set
`ONTOP_JAVA_ARGS`.

**Heap vs. task memory.** Size the max heap (`-Xmx`) at roughly **60–75% of
`VKG_TASK_MEMORY`**, leaving room for JVM overhead and the OS. E.g. a 4 GB task
pairs with `-Xmx3072m -Xms1024m`.

**Verifying the applied sizing.** Check the running task's definition:

```bash
aws ecs describe-task-definition --task-definition <family> \
  --query 'taskDefinition.{cpu:cpu,memory:memory,env:containerDefinitions[0].environment}'
```

Confirm `cpu`/`memory` match your intended values and that the container
environment contains `ONTOP_JAVA_ARGS` (and **not** `JAVA_OPTS`). At startup the
entrypoint also logs the resolved value: `[VKG] Ontop heap args
(ONTOP_JAVA_ARGS): '...'`.

For how to configure these from CDK (the `VkgStack` `cpu`, `memoryLimitMiB`, and
`ontopJavaArgs` props), see the **VKG Task Sizing** section of the deployment
guide.

## Health Check and Self-Healing

### /health endpoint

| Status | Meaning |
|--------|---------|
| `200` | Ontop loaded, ready to translate |
| `503` | Degraded mode (ontology not loaded) |

### Self-healing mechanism

The Docker/ECS health check calls `curl -sf http://localhost:8080/health`. Since `-f` fails on non-2xx responses, a container stuck in degraded mode (returning 503) will be marked **UNHEALTHY** by ECS after 5 consecutive failures (30s interval = ~2.5 min).

ECS then replaces the task automatically. The replacement container retries S3 download from scratch.

**Health check parameters:**
- Interval: 30s
- Timeout: 10s
- Start period: 180s (grace period for S3 download + Ontop initialization)
- Retries: 5

### Checking container health

```bash
# From inside the container
curl -sf http://localhost:8080/health

# From ECS (check task health status)
aws ecs describe-tasks --cluster $CLUSTER --tasks $TASK_ARN \
  --query 'tasks[0].containers[0].healthStatus'
```

### Expected lifecycle during reload failures

1. EventBridge triggers reload Lambda
2. Lambda force-deploys the ECS service (rolling update)
3. New task starts, retries S3 download (3 attempts)
4. If S3 fails: container enters degraded mode → health check fails after ~2.5 min → ECS replaces
5. Cycle repeats until S3 artifacts are available

## Image Reloads and the Scheduled Sweep

VKG runs **one ECS service per namespace** (`<prefix>-vkg-<namespace>`). No static
"default" service exists — a namespace's service is created the first time its
ontology is published. The `<prefix>-vkg-reload` Lambda owns provisioning and
image refresh; the latest image URI is published to SSM
(`/<prefix>/vkg/container-image`) on every CDK deploy.

### Two ways the reload Lambda runs

| Trigger | Event | Scope |
|---------|-------|-------|
| `ontology.published` (EventBridge) | `{"detail": {"namespace": "...", "version": "..."}}` | The one namespace that was published |
| Scheduled sweep (EventBridge, weekly) | `{"sweep": true}` | **Every** `<prefix>-vkg-*` service in the cluster |

On each run the Lambda resolves the latest image from SSM and, per service,
registers a new task-definition revision when the image differs (else forces a
new deployment), always with the ECS circuit breaker + rollback.

### Why the scheduled sweep exists

The `ontology.published` trigger only refreshes a namespace **when its ontology
is re-accepted**. A long-lived namespace that is never republished keeps its
provision-time image indefinitely, so base-image digest bumps (Renovate, or a
Dockerfile pin change) never reach a running task and stale, Inspector-flagged
VKG images accumulate. A CDK deploy does **not** fix this either — it only
updates the shared template task def and the SSM image parameter, not the
existing per-namespace services.

The weekly sweep (EventBridge `rate(7 days)` → reload Lambda with
`{"sweep": true}`) closes the gap: it reconciles every VKG service to the latest
SSM image. It is a no-op per namespace when the image already matches, and a
single namespace's failure is isolated so the rest are still patched.

### Manual sweep (on demand)

```bash
aws lambda invoke --function-name <prefix>-vkg-reload \
  --payload "$(printf '{"sweep":true}' | base64)" /tmp/out.json
cat /tmp/out.json   # -> {"status":"sweep_complete","total":N,"triggered":M,...}
```

### IAM

The reload Lambda role holds, in addition to the per-namespace deploy
permissions, **`ecs:ListServices` scoped to the VKG cluster** — the sweep needs
it to enumerate `<prefix>-vkg-*` services. It is read-only and cluster-scoped
via the `ecs:cluster` condition key (`ecs:ListServices` has no IAM resource
type, so it is granted on `Resource: "*"` and constrained to this cluster by
condition).

### Observability

Both paths emit `ReloadTriggered` / `ReloadFailed` CloudWatch metrics under the
`COA/VKG` namespace (dimensioned by `Namespace` plus an undimensioned roll-up the
`ReloadFailed` alarm evaluates). Sweep-level failures — the SSM image can't be
resolved, or `ListServices` errors — emit `ReloadFailed` with namespace `sweep`
and end the run cleanly rather than raising uncaught.
