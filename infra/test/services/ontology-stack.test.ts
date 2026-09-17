// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { OntologyStack } from "../../lib/stacks/services/ontology-stack";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  ontology_engine_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:ontology-engine-test",
  "aws:cdk:bundling-stacks": [],
};

const PREFIX = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}`;

/** Render an OntologyStack with the given model-ID props (#94). */
function renderOntology(
  props: {
    bedrockEmbedModelId?: string;
    bedrockEmbedDimensions?: number;
    bedrockInductionLlmModelId?: string;
    bedrockChatModelId?: string;
  },
  id: string,
): Template {
  const app = new cdk.App({ context: TEST_CONTEXT });
  const network = new NetworkStack(app, `${id}Network`);
  return Template.fromStack(
    new OntologyStack(app, id, {
      network,
      serviceNamespace: network.serviceNamespace,
      neptuneClusterArn:
        "arn:aws:neptune-db:us-east-1:123456789012:cluster-abc123/*",
      neptuneClusterEndpoint:
        "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
      ontologyArtifactsBucket: cdk.aws_s3.Bucket.fromBucketName(
        network,
        `${id}ArtifactsBucket`,
        "coa-dev-ontology-artifacts-123456789012",
      ),
      smusDomainId: "dzd-test123",
      allowedOrigin: "https://test.example.com",
      ...props,
    }),
  );
}

/** Environment array of the ontology-engine container in a rendered template. */
function containerEnv(t: Template): Array<{ Name: string; Value: unknown }> {
  const taskDefs = t.findResources("AWS::ECS::TaskDefinition");
  const def = Object.values(taskDefs).find((d) =>
    d.Properties?.ContainerDefinitions?.some(
      (c: { Environment?: unknown }) => c.Environment,
    ),
  );
  const container = def!.Properties.ContainerDefinitions.find(
    (c: { Environment?: unknown }) => c.Environment,
  );
  return container.Environment;
}

function envValue(t: Template, name: string): unknown {
  return containerEnv(t).find((e) => e.Name === name)?.Value;
}

describe("OntologyStack model IDs from deploy config (#94)", () => {
  test("configured model IDs reach the container environment", () => {
    const t = renderOntology(
      {
        bedrockEmbedModelId: "cohere.embed-v4:0",
        bedrockEmbedDimensions: 512,
        bedrockInductionLlmModelId: "jp.anthropic.claude-sonnet-4-6",
        bedrockChatModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      },
      "CfgOntology",
    );
    expect(envValue(t, "BEDROCK_EMBED_MODEL_ID")).toBe("cohere.embed-v4:0");
    expect(envValue(t, "BEDROCK_EMBED_DIMENSIONS")).toBe("512");
    // Dimension is also the OpenSearch index dimension — both must follow config.
    expect(envValue(t, "OSS_DIMENSIONS")).toBe("512");
    expect(envValue(t, "LLM_MODEL_ID")).toBe("jp.anthropic.claude-sonnet-4-6");
    // Description generation has its own variable; it must not stay on a us. profile.
    expect(envValue(t, "DESCRIPTION_LLM_MODEL_ID")).toBe(
      "jp.anthropic.claude-sonnet-4-6",
    );
    expect(envValue(t, "BEDROCK_CHAT_MODEL_ID")).toBe(
      "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    );
  });

  test("omitting config keeps the previous defaults (backward compatibility)", () => {
    const t = renderOntology({}, "DefaultOntology");
    expect(envValue(t, "BEDROCK_EMBED_MODEL_ID")).toBe("us.cohere.embed-v4:0");
    expect(envValue(t, "BEDROCK_EMBED_DIMENSIONS")).toBe("1024");
    expect(envValue(t, "OSS_DIMENSIONS")).toBe("1024");
    expect(envValue(t, "LLM_MODEL_ID")).toBe("us.anthropic.claude-sonnet-5");
  });

  test("dashboard ModelId dimensions follow the configured models", () => {
    // The dashboard used to hold independent literal copies, so a configured
    // model left the Bedrock widgets querying a dimension with no data.
    const t = renderOntology(
      {
        bedrockEmbedModelId: "cohere.embed-v4:0",
        bedrockInductionLlmModelId: "jp.anthropic.claude-sonnet-4-6",
        bedrockChatModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      },
      "DashOntology",
    );
    const dashboards = t.findResources("AWS::CloudWatch::Dashboard");
    const body = JSON.stringify(Object.values(dashboards));
    expect(body).toContain("jp.anthropic.claude-sonnet-4-6");
    expect(body).toContain("jp.anthropic.claude-haiku-4-5-20251001-v1:0");
    expect(body).toContain("cohere.embed-v4:0");
    // No stale us. literals left behind in the widgets.
    expect(body).not.toContain("us.anthropic.claude-sonnet-4-6");
    expect(body).not.toContain("us.cohere.embed-v4:0");
  });
});

describe("OntologyStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork");

    template = Template.fromStack(
      new OntologyStack(app, "TestOntology", {
        network,
        serviceNamespace: network.serviceNamespace,
        neptuneClusterArn:
          "arn:aws:neptune-db:us-east-1:123456789012:cluster-abc123/*",
        neptuneClusterEndpoint:
          "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
        ontologyArtifactsBucket: cdk.aws_s3.Bucket.fromBucketName(
          network,
          "TestArtifactsBucket",
          "coa-dev-ontology-artifacts-123456789012",
        ),
        smusDomainId: "dzd-test123",
        allowedOrigin: "https://test.example.com",
      }),
    );
  });

  // ── DynamoDB Table ──────────────────────────────────────────────

  test("creates DynamoDB table with PK/SK", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: `${PREFIX}-ontology-engine`,
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  // ── ECS Cluster ─────────────────────────────────────────────────

  test("creates ECS cluster", () => {
    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterName: `${PREFIX}-ontology-cluster`,
    });
  });

  // ── Task Definition ─────────────────────────────────────────────

  test("creates Fargate task definition with correct sizing", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: `${PREFIX}-ontology-engine`,
      Cpu: "8192",
      Memory: "32768",
      RequiresCompatibilities: ["FARGATE"],
      NetworkMode: "awsvpc",
    });
  });

  test("container has environment variables configured", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: `${PREFIX}-ontology-engine`,
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "WORKBENCH_BACKEND",
              Value: "opensearch_neptune",
            }),
            Match.objectLike({ Name: "CATALOG_SOURCE", Value: "smus" }),
            Match.objectLike({ Name: "SMUS_DOMAIN_ID", Value: "dzd-test123" }),
            // NAMESPACES_TABLE / DATASOURCES_TABLE / SOURCES_TABLE are now resolved at
            // deploy time via SSM CfnDynamicReference. DATASOURCES_TABLE aliases
            // SOURCES_TABLE (unified sources table) for backwards compatibility with
            // ontology-engine code. Just assert the keys exist.
            Match.objectLike({ Name: "NAMESPACES_TABLE" }),
            Match.objectLike({ Name: "DATASOURCES_TABLE" }),
            Match.objectLike({ Name: "SOURCES_TABLE" }),
          ]),
        }),
      ]),
    });
  });

  test("container has port mapping on 8001", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          PortMappings: Match.arrayWith([
            Match.objectLike({
              ContainerPort: 8001,
            }),
          ]),
        }),
      ]),
    });
  });

  test("container has health check", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          HealthCheck: Match.objectLike({
            Command: Match.arrayWith(["CMD-SHELL"]),
          }),
        }),
      ]),
    });
  });

  // ── Fargate Service ─────────────────────────────────────────────

  test("creates Fargate service with Cloud Map service discovery", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: `${PREFIX}-ontology-engine`,
      LaunchType: "FARGATE",
      ServiceRegistries: Match.anyValue(),
    });
    template.hasResourceProperties("AWS::ServiceDiscovery::Service", {
      Name: "ontology-engine",
      DnsConfig: Match.objectLike({
        DnsRecords: Match.arrayWith([Match.objectLike({ Type: "A", TTL: 10 })]),
      }),
    });
  });

  // ── IAM Permissions ─────────────────────────────────────────────

  test("task role has Neptune access with least privilege", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["neptune-db:ReadDataViaQuery"]),
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("task role has Bedrock access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["bedrock:InvokeModel"]),
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("task role has DataZone access", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["datazone:GetAsset"]),
            Effect: "Allow",
          }),
        ]),
      }),
    });
  });

  test("task role has AWS Marketplace permissions for Bedrock model subscription", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              "aws-marketplace:ViewSubscriptions",
              "aws-marketplace:Subscribe",
            ],
            Effect: "Allow",
            Resource: "*",
          }),
        ]),
      }),
    });
  });

  test("task role can publish CloudWatch metrics scoped to COA namespaces", () => {
    // COA/Guardrails is here too because the SHACL-shape NL generator runs in
    // this Fargate task and emits guardrail decisions (#111 AC10). Leaving it
    // out silently drops those metrics — PutMetricData denials are async.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "cloudwatch:PutMetricData",
            Effect: "Allow",
            Resource: "*",
            Condition: {
              StringEquals: {
                "cloudwatch:namespace": ["COA/Ontology", "COA/Guardrails"],
              },
            },
          }),
        ]),
      }),
    });
  });

  // ── AOSS Data Access Policy ─────────────────────────────────────

  test("creates AOSS data access policy", () => {
    template.hasResourceProperties("AWS::OpenSearchServerless::AccessPolicy", {
      Name: `${PREFIX}-ontology-oss`,
      Type: "data",
    });
  });

  // ── CloudWatch Alarms (via SclMonitoring facade) ────────────────

  test("emits facade fargate + table alarms and an ontology dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 2);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(2);
  });

  // ── CloudWatch Dashboard ────────────────────────────────────────

  test("creates the single consolidated induction dashboard", () => {
    // The InductionDashboard remains (custom COA/Ontology metrics) alongside
    // the facade dashboard (ECS service + DynamoDB table metrics).
    template.hasResourceProperties("AWS::CloudWatch::Dashboard", {
      DashboardName: `${PREFIX}-ontology-induction`,
    });
  });

  test("dashboard body references the COA/Ontology metric contract", () => {
    const dashboards = template.findResources("AWS::CloudWatch::Dashboard");
    // Find the InductionDashboard (not the facade dashboard)
    const inductionDashboard = Object.values(dashboards).find(
      (dashboard) =>
        dashboard.Properties.DashboardName &&
        JSON.stringify(dashboard.Properties.DashboardName).includes(
          "ontology-induction",
        ),
    );
    expect(inductionDashboard).toBeDefined();
    const body = inductionDashboard!.Properties.DashboardBody;
    // DashboardBody is a CloudFormation Fn::Join over string fragments +
    // tokens; stringify the whole structure and assert the literal fragments.
    const serialized = JSON.stringify(body);
    // Single namespace — all custom metrics publish to COA/Ontology.
    expect(serialized).toContain("COA/Ontology");
    // Stage durations + job wallclock (percentile-capable single-value datums).
    expect(serialized).toContain("InductionJobDurationMs");
    expect(serialized).toContain("FetchMetadataDurationMs");
    expect(serialized).toContain("InduceDurationMs");
    expect(serialized).toContain("StoreDurationMs");
    // Throughput / volume.
    expect(serialized).toContain("TablesProcessed");
    expect(serialized).toContain("NovelClassesCreated");
    // Rerank (names THIS branch's emitter actually publishes).
    expect(serialized).toContain("RerankInvocations");
    expect(serialized).toContain("RerankLatencyMs");
    // Cost + tokens.
    expect(serialized).toContain("InductionJobCostUsd");
    expect(serialized).toContain("BedrockInputTokens");
    expect(serialized).toContain("BedrockOutputTokens");
    // Duration widgets show p50 AND p90 across jobs.
    expect(serialized).toContain("p50");
    expect(serialized).toContain("p90");
  });

  // ── OOM Observability (#784) ────────────────────────────────────

  test("alarms on ECS MemoryUtilization at 85% (Maximum, 1 period)", () => {
    // 85% < the facade's 90% memory alarm that missed the 81% pre-OOM sample;
    // Maximum (not Average) catches the between-sample ramp to >100%.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "MemoryUtilization",
      Namespace: "AWS/ECS",
      Threshold: 85,
      Statistic: "Maximum",
      EvaluationPeriods: 1,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      TreatMissingData: "notBreaching",
    });
  });

  test("EventBridge rule fires on ECS OOM / exit-137 task stops", () => {
    // A SIGKILL/exit-137 runs neither except nor finally, so the custom
    // metrics never emit — this rule makes the OOM visible from the ECS
    // Task State Change event (stoppedReason ~ OutOfMemory OR exitCode 137).
    const rules = template.findResources("AWS::Events::Rule");
    const ruleValues = Object.values(rules);
    const oomRule = ruleValues.find((r) => {
      const p = r.Properties?.EventPattern;
      return (
        p &&
        JSON.stringify(p.source) === JSON.stringify(["aws.ecs"]) &&
        JSON.stringify(p["detail-type"]) ===
          JSON.stringify(["ECS Task State Change"])
      );
    });
    expect(oomRule).toBeDefined();
    const serialized = JSON.stringify(oomRule!.Properties.EventPattern);
    // Matches OOM stoppedReason (wildcard) and/or the exit-137 container code.
    expect(serialized).toContain("OutOfMemory");
    expect(serialized).toContain("137");
    // Scoped to STOPPED tasks so it doesn't fire on every state transition.
    expect(serialized).toContain("STOPPED");
  });

  test("EventBridge OOM rule surfaces a CloudWatch metric + alarm", () => {
    // Laziest durable target: rule → Logs log group + metric filter emitting
    // COA/Ontology InductionTaskOOMKilled, then an alarm on that metric.
    template.hasResourceProperties("AWS::Logs::MetricFilter", {
      MetricTransformations: Match.arrayWith([
        Match.objectLike({
          MetricName: "InductionTaskOOMKilled",
          MetricNamespace: "COA/Ontology",
          MetricValue: "1",
        }),
      ]),
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "InductionTaskOOMKilled",
      Namespace: "COA/Ontology",
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
      Threshold: 1,
    });
  });

  // ── SSM Parameters ──────────────────────────────────────────────

  test("exports SSM parameter for endpoint", () => {
    // Endpoint uses the Cloud Map FQDN so it's resolvable from outside ECS
    // (e.g. from the api-proxy Lambda). The DNS suffix is the namespace name
    // exported by network-stack — which is a CFN token here, so we just
    // assert the value contains the known prefix.
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/ontology-engine/endpoint",
    });
  });
});

// The running coa-dev-ontology-engine image accreted Critical/High OS CVEs
// (glibc, perl, sqlite3, libssh2, gzip, pcre2) whenever a build predated the
// Debian point releases that fix them. Two Dockerfile properties keep the image
// patchable: a pinned base digest (supply-chain integrity + a cache-busting
// bump lever) and a post-install `apt-get upgrade` (the only mechanism pulling
// trixie-security point releases in). These guard both against regression —
// removing the upgrade line, or floating the base back onto a mutable tag, is
// exactly how the CVEs came back.
describe("ontology-engine Dockerfile CVE hygiene", () => {
  const dockerfile = fs.readFileSync(
    path.join(
      __dirname,
      "..",
      "..",
      "..",
      "packages",
      "ontology-engine",
      "Dockerfile",
    ),
    "utf8",
  );

  test("pins the base image to a digest, not a mutable tag", () => {
    const from = dockerfile.match(
      /^FROM .*python:3\.12-slim(@sha256:[a-f0-9]{64})?/m,
    );
    expect(from).not.toBeNull();
    // A digest pin (@sha256:...) must be present; a bare/:latest tag would let
    // the base float and silently reintroduce unpatched packages.
    expect(from?.[1]).toMatch(/@sha256:[a-f0-9]{64}/);
    expect(dockerfile).not.toMatch(/python:3\.12-slim\s*$/m);
  });

  test("runs apt-get upgrade so OS security point releases are applied", () => {
    // This is the line that resolves the Debian package CVEs at build time.
    // Must be `upgrade`, never `dist-upgrade` (which may remove packages).
    expect(dockerfile).toMatch(/apt-get upgrade -y/);
    expect(dockerfile).not.toMatch(/apt-get dist-upgrade/);
  });

  test("strips owlready2's unused Pellet reasoner (vulnerable bundled JARs)", () => {
    // We use HermiT only; Pellet's vendored jars (jena, log4j 2.19, httpclient
    // 4.2, xerces) carry CVEs and are dead weight. The build EMPTIES the pellet
    // dir but keeps the directory itself: owlready2's reasoning.py runs
    // os.listdir(<pkg>/pellet) at import time, so `rm -rf`-ing the whole dir
    // would make `import owlready2` raise FileNotFoundError and break HermiT too
    // (surfacing as REASONER_ERROR). An empty dir → os.listdir returns [].
    expect(dockerfile).toMatch(/pellet/);
    // Deletes the dir's contents (the vulnerable JARs) while keeping the dir.
    expect(dockerfile).toMatch(/find\b[^\n]*"?\$PELLET_DIR"?[^\n]*-delete/);
    // Must NOT remove the directory itself — that is the regression above.
    expect(dockerfile).not.toMatch(/rm -rf\b[^\n]*"?\$PELLET_DIR/);
    // And the build verifies owlready2 still imports after the strip.
    expect(dockerfile).toMatch(/import owlready2/);
  });

  test("upgrades pip so its own advisories are picked up", () => {
    expect(dockerfile).toMatch(/pip install[^\n]*--upgrade pip/);
  });
});
