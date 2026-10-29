// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Template, Match } from "aws-cdk-lib/assertions";
import { VkgStack } from "../../lib/stacks/services/vkg-stack";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  "aws:cdk:bundling-stacks": [],
};

const PREFIX = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}`;

/**
 * Render a VkgStack template with additional CDK context merged over the
 * defaults. Each call gets its own App so context does not leak between tests.
 */
function renderVkgWithContext(
  extraContext: Record<string, unknown>,
): Template {
  const app = new cdk.App({ context: { ...TEST_CONTEXT, ...extraContext } });
  const network = new NetworkStack(app, "CtxNetwork");
  const bucketStack = new cdk.Stack(app, "CtxBucketStack");
  const bucket = new s3.Bucket(bucketStack, "OntologyBucket", {
    bucketName: `${PREFIX}-ontology-artifacts`,
  });
  return Template.fromStack(
    new VkgStack(app, "CtxVkg", {
      network,
      serviceNamespace: network.serviceNamespace,
      ontologyBucket: bucket,
    }),
  );
}

describe("VkgStack", () => {
  let template: Template;
  let templateWithBucket: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork");
    const bucketStack = new cdk.Stack(app, "BucketStack");
    const bucket = new s3.Bucket(bucketStack, "OntologyBucket", {
      bucketName: `${PREFIX}-ontology-artifacts`,
    });

    template = Template.fromStack(
      new VkgStack(app, "TestVkg", {
        network,
        serviceNamespace: network.serviceNamespace,
        ontologyBucket: bucket,
      }),
    );

    // Test with a different externally provided bucket — same path now that
    // ontologyBucket is required, kept for assertion symmetry.
    const app2 = new cdk.App({ context: TEST_CONTEXT });
    const network2 = new NetworkStack(app2, "TestNetwork2");
    const mockStack = new cdk.Stack(app2, "MockStack");
    const ontologyBucket = new s3.Bucket(mockStack, "OntologyBucket", {
      bucketName: `${PREFIX}-ontology-artifacts`,
    });

    templateWithBucket = Template.fromStack(
      new VkgStack(app2, "TestVkg2", {
        network: network2,
        serviceNamespace: network2.serviceNamespace,
        ontologyBucket,
      }),
    );
  });

  // ── ECS Cluster ──────────────────────────────────────────────────

  test("creates ECS cluster for VKG service", () => {
    template.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterName: `${PREFIX}-vkg-cluster`,
    });
  });

  test("does not create its own Cloud Map namespace (uses shared from NetworkStack)", () => {
    const namespaces = template.findResources(
      "AWS::ServiceDiscovery::PrivateDnsNamespace",
    );
    expect(Object.keys(namespaces).length).toBe(0);
  });

  // ── Task Definition ──────────────────────────────────────────────

  test("creates Fargate task definition with correct sizing", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      Family: `${PREFIX}-vkg-service`,
      Cpu: "1024",
      Memory: "2048",
      RequiresCompatibilities: ["FARGATE"],
      NetworkMode: "awsvpc",
    });
  });

  test("task definition has container with port mapping", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Name: `${PREFIX}-ontop`,
          PortMappings: Match.arrayWith([
            Match.objectLike({
              ContainerPort: 8080,
              Protocol: "tcp",
            }),
          ]),
        }),
      ]),
    });
  });

  test("container has health check configured", () => {
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

  test("container has required environment variables", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "ONTOLOGY_BUCKET" }),
            Match.objectLike({ Name: "ONTOLOGY_PREFIX", Value: "ontologies/" }),
            Match.objectLike({ Name: "ENDPOINT_PORT", Value: "8080" }),
          ]),
        }),
      ]),
    });
  });

  test("container sets Ontop heap via ONTOP_JAVA_ARGS, not the dead JAVA_OPTS (#149 cause C)", () => {
    // The Ontop launcher reads ONTOP_JAVA_ARGS; setting JAVA_OPTS was a silent
    // no-op that left the heap unbounded (#149 cause C). Assert the correct var
    // is present so CDK can never regress to JAVA_OPTS.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "ONTOP_JAVA_ARGS", Value: "-Xmx1536m -Xms512m" }),
          ]),
        }),
      ]),
    });
    // Guard: JAVA_OPTS must NOT appear in any container environment.
    const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
    for (const td of Object.values(taskDefs)) {
      for (const container of td.Properties.ContainerDefinitions ?? []) {
        for (const envVar of container.Environment ?? []) {
          expect(envVar.Name).not.toBe("JAVA_OPTS");
        }
      }
    }
  });

  test("reload Lambda carries the task-sizing env as the single source of truth (#149 cause B)", () => {
    // The reload Lambda re-registers the per-namespace task def from these env
    // vars, so they must match the CDK-provisioned service. Without them a
    // reload would silently under-provision (#149 cause B).
    template.hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: Match.objectLike({
          VKG_TASK_CPU: Match.anyValue(),
          VKG_TASK_MEMORY: Match.anyValue(),
          VKG_ONTOP_JAVA_ARGS: "-Xmx1536m -Xms512m",
        }),
      },
    });
  });

  test("Ontop heap scales with memoryLimitMiB when ontopJavaArgs is not overridden", () => {
    // With a larger task memory and no explicit ontopJavaArgs, the heap derives
    // from memory (max ~=75%, initial ~=25%) so raising memory alone scales the
    // heap in step — 8192 -> -Xmx6144m -Xms2048m. Both the container and the
    // reload Lambda must carry the derived value so they stay in lockstep.
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "ScaleNetwork");
    const bucketStack = new cdk.Stack(app, "ScaleBucketStack");
    const bucket = new s3.Bucket(bucketStack, "OntologyBucket", {
      bucketName: `${PREFIX}-ontology-artifacts`,
    });
    const scaled = Template.fromStack(
      new VkgStack(app, "ScaleVkg", {
        network,
        serviceNamespace: network.serviceNamespace,
        ontologyBucket: bucket,
        memoryLimitMiB: 8192,
      }),
    );
    scaled.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "ONTOP_JAVA_ARGS", Value: "-Xmx6144m -Xms2048m" }),
          ]),
        }),
      ]),
    });
    scaled.hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: Match.objectLike({
          VKG_TASK_MEMORY: "8192",
          VKG_ONTOP_JAVA_ARGS: "-Xmx6144m -Xms2048m",
        }),
      },
    });
  });

  test("container has CloudWatch logging configured", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          LogConfiguration: Match.objectLike({
            LogDriver: "awslogs",
            Options: Match.objectLike({
              "awslogs-stream-prefix": "vkg",
            }),
          }),
        }),
      ]),
    });
  });

  // ── No static Fargate Service ────────────────────────────────────

  test("does not create a static VKG service (per-namespace only)", () => {
    const services = template.findResources("AWS::ECS::Service");
    expect(Object.keys(services).length).toBe(0);
  });

  // ── IAM Permissions ──────────────────────────────────────────────

  test("task role has S3 read access for ontology artifacts", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["s3:GetObject*", "s3:GetBucket*"]),
            Effect: "Allow",
          }),
        ]),
      },
    });
  });

  // ── Reload monitoring ───────────────────────────────────

  test("creates a reload-failure alarm on the undimensioned ReloadFailed roll-up metric", () => {
    // The reload Lambda emits ReloadFailed both per-Namespace and as an
    // undimensioned cluster-wide roll-up. The alarm must evaluate the roll-up
    // (a plain Metric), NOT a SUM(SEARCH(...)) expression: CloudWatch metric
    // alarms reject SEARCH() at deploy time ("SEARCH is not supported on Metric
    // Alarms"). The undimensioned series receives data from every namespace, so
    // a single plain-metric alarm still fires on any namespace's reload failure.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `${PREFIX}-vkg-reload-failed`,
      Namespace: "COA/VKG",
      MetricName: "ReloadFailed",
      Statistic: "Sum",
      Threshold: 1,
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
    });
  });

  test("reload-failure alarm is NOT backed by a SEARCH/MathExpression (deploy-time invalid)", () => {
    // Regression guard for the "SEARCH is not supported on Metric Alarms"
    // CloudFormation deploy failure: the alarm must render as a single-metric
    // alarm (Namespace/MetricName/Statistic on the resource), never a Metrics[]
    // array carrying an Expression.
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const reloadAlarm = Object.values(alarms).find(
      (r) => r.Properties?.AlarmName === `${PREFIX}-vkg-reload-failed`,
    );
    expect(reloadAlarm).toBeDefined();
    expect(reloadAlarm?.Properties?.Metrics).toBeUndefined();
    expect(JSON.stringify(reloadAlarm?.Properties)).not.toContain("SEARCH");
  });

  test("reload Lambda may publish only COA/VKG metrics", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "cloudwatch:PutMetricData",
            Condition: {
              StringEquals: { "cloudwatch:namespace": "COA/VKG" },
            },
          }),
        ]),
      },
    });
  });

  // ── SSM Parameters ───────────────────────────────────────────────

  test("publishes VKG container image URI to SSM for reload Lambda", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/vkg/container-image`,
    });
  });

  test("publishes VKG cluster ARN to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/vkg/cluster-arn`,
    });
  });

  test("publishes VKG endpoint pattern to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/vkg/endpoint`,
    });
  });

  // ── CloudFormation Outputs ───────────────────────────────────────

  test("exports VkgClusterArn output", () => {
    template.hasOutput("VkgClusterArn", {});
  });

  test("exports VkgEndpointPattern output", () => {
    template.hasOutput("VkgEndpointPattern", {});
  });

  // ── S3 Bucket (imported via fromBucketName) ───────────────────────

  test("uses imported ontology bucket when not provided", () => {
    // Stack imports bucket via fromBucketName — no AWS::S3::Bucket resource is created.
    // Verify the task role has S3 access referencing the expected bucket name.
    const buckets = template.findResources("AWS::S3::Bucket");
    expect(Object.keys(buckets).length).toBe(0);
  });

  test("does not create bucket when one is provided externally", () => {
    const vkgBuckets = templateWithBucket.findResources("AWS::S3::Bucket");
    expect(Object.keys(vkgBuckets).length).toBe(0);
  });

  // ── VPC Configuration ─────────────────────────────────────────────

  test("VkgReloadFn is deployed in VPC (security baseline)", () => {
    // Per .kiro/steering/cdk-infrastructure.md, all Lambda functions must be
    // deployed inside a VPC. VkgReloadFn manipulates ECS resources that are
    // themselves VPC-bound.
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-vkg-reload`,
      VpcConfig: Match.objectLike({
        SubnetIds: Match.anyValue(),
        SecurityGroupIds: Match.anyValue(),
      }),
    });
  });

  // ── Reserved concurrency (configurable; #48) ─────────────────────

  test("VkgReloadFn reserves the default concurrency (5) when unset", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-vkg-reload`,
      ReservedConcurrentExecutions: 5,
    });
  });

  test("VkgReloadFn honours a custom lambda_reserved_concurrency value", () => {
    const t = renderVkgWithContext({ lambda_reserved_concurrency: 3 });
    t.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-vkg-reload`,
      ReservedConcurrentExecutions: 3,
    });
  });

  test("VkgReloadFn omits the reservation when lambda_reserved_concurrency=0", () => {
    // On reduced-quota accounts (Lambda concurrent-executions = 10) any
    // reservation is rejected; 0 must drop the property entirely, not set 0.
    const t = renderVkgWithContext({ lambda_reserved_concurrency: 0 });
    t.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-vkg-reload`,
      ReservedConcurrentExecutions: Match.absent(),
    });
  });

  test("synth fails fast on an invalid lambda_reserved_concurrency", () => {
    expect(() => renderVkgWithContext({ lambda_reserved_concurrency: -1 })).toThrow(
      /lambda_reserved_concurrency must be a non-negative integer/,
    );
    expect(() =>
      renderVkgWithContext({ lambda_reserved_concurrency: "abc" }),
    ).toThrow(/lambda_reserved_concurrency must be a non-negative integer/);
  });

  // ── OE monitoring (cdk-monitoring-constructs) ────────────────────

  test("emits facade cluster CPU/memory alarms and a vkg dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(2);
  });

  // ── Ontology-reload rule: cross-deployment isolation ──────────────

  test("reload rule matches a resource_prefix-scoped event source, not a shared one", () => {
    // Two deployments in one account+region share the account-global `default`
    // bus. If the rule matched only the static brand source, each deployment's
    // rule would fire on the OTHER's ontology.published events and provision
    // duplicate per-namespace VKG services that its own teardown never deletes.
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, resource_prefix: "sclz" },
    });
    const network = new NetworkStack(app, "IsoNetwork");
    const isoBucketStack = new cdk.Stack(app, "IsoBucketStack");
    const isoBucket = new s3.Bucket(isoBucketStack, "OntologyBucket", {
      bucketName: "sclz-dev-ontology-artifacts",
    });
    const stack = new VkgStack(app, "IsoVkg", {
      network,
      serviceNamespace: network.serviceNamespace,
      ontologyBucket: isoBucket,
    });
    const rules = Template.fromStack(stack).findResources("AWS::Events::Rule");
    const sources = Object.values(rules).flatMap(
      (r) => r.Properties?.EventPattern?.source ?? [],
    );

    expect(sources).toContain("sclz.ontology");
  });

  // ── Scheduled reload sweep ────────────────────────────────────────

  test("schedules a weekly VKG reload sweep invoking the reload Lambda with {sweep:true}", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: `${PREFIX}-vkg-reload-sweep`,
      ScheduleExpression: "rate(7 days)",
      Targets: Match.arrayWith([
        Match.objectLike({ Input: '{"sweep":true}' }),
      ]),
    });
  });

  test("reload Lambda may list services in the cluster (for the sweep)", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          // ecs:ListServices has no resource type — must be Resource:"*",
          // scoped to this cluster via the ecs:cluster condition key.
          Match.objectLike({
            Action: "ecs:ListServices",
            Effect: "Allow",
            Resource: "*",
            Condition: {
              ArnEquals: { "ecs:cluster": Match.anyValue() },
            },
          }),
        ]),
      },
    });
  });

  // ── Deploy-time image rollout ─────────────────────────────────────

  /** The single deploy-time rollout custom resource in a template. */
  function rolloutResource(t: Template): {
    id: string;
    res: { Properties: Record<string, unknown>; DependsOn?: string[] };
  } {
    const found = t.findResources("Custom::AWS");
    const entries = Object.entries(found).filter(([id]) =>
      id.startsWith("VkgImageRollout"),
    );
    expect(entries).toHaveLength(1);
    const [id, res] = entries[0];
    return {
      id,
      res: res as {
        Properties: Record<string, unknown>;
        DependsOn?: string[];
      },
    };
  }

  function logicalIdOf(t: Template, type: string, prefix: string): string {
    const ids = Object.keys(t.findResources(type)).filter((id) =>
      id.startsWith(prefix),
    );
    expect(ids).toHaveLength(1);
    return ids[0];
  }

  test("a deploy invokes the reload Lambda's sweep asynchronously", () => {
    const { res } = rolloutResource(template);
    const create = JSON.stringify(res.Properties.Create);
    const update = JSON.stringify(res.Properties.Update);
    expect(create).toEqual(update);
    expect(create).toContain('\\"service\\":\\"Lambda\\"');
    expect(create).toContain('\\"action\\":\\"invoke\\"');
    // Async: a slow or failing ECS rollout must never block or fail the deploy.
    expect(create).toContain('\\"InvocationType\\":\\"Event\\"');
    expect(create).toContain("sweep");
    expect(create).toContain("deploy");
    // No onDelete: tearing the stack down must not restart services.
    expect(res.Properties.Delete).toBeUndefined();
  });

  const ECR_CONTEXT = {
    ecr_repository_arn:
      "arn:aws:ecr:us-east-1:111111111111:repository/coa-images",
    ecr_repository_name: "coa-images",
  };

  const updateProps = (r: { res: { Properties: Record<string, unknown> } }) =>
    JSON.stringify(r.res.Properties.Update);

  test("an asset-built image rolls out only when its content-hash URI changes", () => {
    // The asset tag is the content hash: same build -> same URI -> identical
    // properties -> CloudFormation makes no call.
    const a1 = rolloutResource(renderVkgWithContext({}));
    const a2 = rolloutResource(renderVkgWithContext({}));
    expect(updateProps(a1)).toEqual(updateProps(a2));
    expect(updateProps(a1)).not.toContain("deployId");
  });

  test("a CI image tag runs the rollout on every deploy, since the tag may be reused", () => {
    // `{repo}:{name}-latest` is pushed again for each build, so the URI does
    // not change when the image does. A per-synth deploy id changes the
    // properties every time; the handler's digest check keeps it cheap.
    const a1 = rolloutResource(
      renderVkgWithContext({
        ...ECR_CONTEXT,
        vkg_image_uri: "repo:vkg-latest",
      }),
    );
    const a2 = rolloutResource(
      renderVkgWithContext({
        ...ECR_CONTEXT,
        vkg_image_uri: "repo:vkg-latest",
      }),
    );
    expect(updateProps(a1)).toContain("vkg-latest");
    expect(updateProps(a1)).toContain("deployId");
    expect(updateProps(a1)).not.toEqual(updateProps(a2));
  });

  // ── Digest comparison permissions ─────────────────────────────────

  /** Every statement attached to the reload Lambda's role. */
  function reloadStatements(t: Template): Record<string, unknown>[] {
    const policies = Object.entries(t.findResources("AWS::IAM::Policy")).filter(
      ([id]) => id.startsWith("VkgReloadFnServiceRoleDefaultPolicy"),
    );
    expect(policies).toHaveLength(1);
    return (
      policies[0][1] as {
        Properties: {
          PolicyDocument: { Statement: Record<string, unknown>[] };
        };
      }
    ).Properties.PolicyDocument.Statement;
  }

  test("the reload Lambda may read image digests only from the VKG image repository", () => {
    const ctxTemplate = renderVkgWithContext({
      ...ECR_CONTEXT,
      vkg_image_uri: "repo:vkg-latest",
    });
    const ecrStatements = reloadStatements(ctxTemplate).filter((st) =>
      JSON.stringify(st.Action).includes("ecr:"),
    );
    expect(ecrStatements).toEqual([
      {
        Action: "ecr:DescribeImages",
        Effect: "Allow",
        Resource: ECR_CONTEXT.ecr_repository_arn,
      },
    ]);
  });

  test("an asset-built image scopes ecr:DescribeImages to the asset repository", () => {
    const ecrStatements = reloadStatements(template).filter((st) =>
      JSON.stringify(st.Action).includes("ecr:"),
    );
    expect(ecrStatements).toHaveLength(1);
    expect(ecrStatements[0].Action).toEqual("ecr:DescribeImages");
    const resource = JSON.stringify(ecrStatements[0].Resource);
    expect(resource).toContain("container-assets");
    expect(resource).not.toContain('"*"');
  });

  test("the reload Lambda may list and describe tasks only in the VKG cluster", () => {
    const statements = reloadStatements(template);
    const list = statements.find((st) => st.Action === "ecs:ListTasks");
    expect(list).toBeDefined();
    expect(list!.Resource).toEqual("*");
    expect(JSON.stringify(list!.Condition)).toContain("ecs:cluster");
    const describe = statements.find((st) => st.Action === "ecs:DescribeTasks");
    expect(describe).toBeDefined();
    expect(JSON.stringify(describe!.Resource)).toContain(":task/");
  });

  test("the rollout runs after the SSM image parameter and the reload Lambda exist", () => {
    const { res } = rolloutResource(template);
    const ssmId = logicalIdOf(
      template,
      "AWS::SSM::Parameter",
      "SsmVkgContainerImage",
    );
    const fnId = logicalIdOf(template, "AWS::Lambda::Function", "VkgReloadFn");
    expect(res.DependsOn).toEqual(expect.arrayContaining([ssmId, fnId]));
  });

  test("the rollout may only invoke the reload Lambda", () => {
    const fnId = logicalIdOf(template, "AWS::Lambda::Function", "VkgReloadFn");
    const policies = template.findResources("AWS::IAM::Policy");
    const rolloutPolicies = Object.entries(policies).filter(([id]) =>
      id.startsWith("VkgImageRolloutCustomResourcePolicy"),
    );
    expect(rolloutPolicies).toHaveLength(1);
    const doc = (
      rolloutPolicies[0][1] as {
        Properties: { PolicyDocument: { Statement: unknown[] } };
      }
    ).Properties.PolicyDocument;
    expect(doc.Statement).toEqual([
      {
        Action: "lambda:InvokeFunction",
        Effect: "Allow",
        Resource: { "Fn::GetAtt": [fnId, "Arn"] },
      },
    ]);
  });

  test("the rollout handler function is named with the deployment prefix", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-vkg-image-rollout`,
    });
  });
});
