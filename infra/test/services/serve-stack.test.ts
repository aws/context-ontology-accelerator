// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { ServeStack } from "../../lib/stacks/services/serve-stack";
import {
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_RESOURCE_PREFIX,
  DEFAULT_ENV,
  DEFAULT_GRAPH_URI_BASE,
} from "../../lib/constants";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const BASE_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  context_manager_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-dev:latest",
  "aws:cdk:bundling-stacks": [],
};

function createStack(contextOverrides: Record<string, string> = {}): Template {
  const app = new cdk.App({
    context: { ...BASE_CONTEXT, ...contextOverrides },
  });

  const depStack = new cdk.Stack(app, "DepStack", {
    env: { account: "123456789012", region: "us-east-1" },
  });
  const vpc = new ec2.Vpc(depStack, "Vpc", { maxAzs: 2 });
  const aossSg = new ec2.SecurityGroup(depStack, "AossSG", { vpc });
  const neptuneSg = new ec2.SecurityGroup(depStack, "NeptuneSG", { vpc });
  const lambdaSg = new ec2.SecurityGroup(depStack, "LambdaSG", { vpc });
  const mkTable = (id: string) =>
    new dynamodb.Table(depStack, id, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });

  const stack = new ServeStack(app, "TestServe", {
    env: { account: "123456789012", region: "us-east-1" },
    vpc,
    aossSecurityGroup: aossSg,
    neptuneSecurityGroup: neptuneSg,
    lambdaSecurityGroup: lambdaSg,
    neptuneClusterArn:
      "arn:aws:neptune-db:us-east-1:123456789012:cluster:test-cluster/*",
    neptuneEndpoint: "test-cluster.cluster-abc.us-east-1.neptune.amazonaws.com",
    ontologyBucketArn: "arn:aws:s3:::coa-dev-ontology-artifacts",
    vkgEndpoint: "http://vkg.coa-dev-services.local:8080",
    rolesTable: mkTable("Roles"),
    resourceRoleMappingsTable: mkTable("RRM"),
  });

  return Template.fromStack(stack);
}

describe("ServeStack - Security Group egress", () => {
  describe("without JDBC peer CIDRs", () => {
    let template: Template;
    beforeAll(() => {
      template = createStack();
    });

    it("has local-VPC PostgreSQL egress on port 5432", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5432,
            ToPort: 5432,
            Description: Match.stringLikeRegexp("PostgreSQL.*within VPC"),
          }),
        ]),
      });
    });

    it("has local-VPC MySQL egress on port 3306", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 3306,
            ToPort: 3306,
            Description: Match.stringLikeRegexp("MySQL.*within VPC"),
          }),
        ]),
      });
    });

    it("has local-VPC MSSQL egress on port 1433", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 1433,
            ToPort: 1433,
            Description: Match.stringLikeRegexp("MSSQL.*within VPC"),
          }),
        ]),
      });
    });

    it("has local-VPC Redshift egress on port 5439", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5439,
            ToPort: 5439,
            Description: Match.stringLikeRegexp("Redshift.*within VPC"),
          }),
        ]),
      });
    });

    it("does NOT have peer-CIDR egress rules when no peering configured", () => {
      // No rule should reference a CIDR outside the VPC
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.not(
          Match.arrayWith([
            Match.objectLike({
              Description: Match.stringLikeRegexp("peer network"),
            }),
          ]),
        ),
      });
    });
  });

  describe("with JDBC peer CIDRs configured", () => {
    let template: Template;
    beforeAll(() => {
      template = createStack({
        jdbc_peer_vpc_id: "vpc-peer123",
        jdbc_peer_cidrs: "10.20.0.0/16,10.30.0.0/16",
      });
    });

    it("adds PostgreSQL egress to each peer CIDR", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5432,
            ToPort: 5432,
            CidrIp: "10.20.0.0/16",
          }),
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5432,
            ToPort: 5432,
            CidrIp: "10.30.0.0/16",
          }),
        ]),
      });
    });

    it("adds MySQL egress to each peer CIDR", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 3306,
            ToPort: 3306,
            CidrIp: "10.20.0.0/16",
          }),
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 3306,
            ToPort: 3306,
            CidrIp: "10.30.0.0/16",
          }),
        ]),
      });
    });

    it("adds MSSQL egress to each peer CIDR", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 1433,
            ToPort: 1433,
            CidrIp: "10.20.0.0/16",
          }),
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 1433,
            ToPort: 1433,
            CidrIp: "10.30.0.0/16",
          }),
        ]),
      });
    });

    it("adds Redshift egress to each peer CIDR", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5439,
            ToPort: 5439,
            CidrIp: "10.20.0.0/16",
          }),
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5439,
            ToPort: 5439,
            CidrIp: "10.30.0.0/16",
          }),
        ]),
      });
    });
  });

  describe("with JDBC TGW CIDRs configured", () => {
    let template: Template;
    beforeAll(() => {
      template = createStack({
        jdbc_tgw_id: "tgw-abc123",
        jdbc_tgw_cidrs: "172.16.0.0/12",
      });
    });

    it("adds DB port egress to TGW CIDRs", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        SecurityGroupEgress: Match.arrayWith([
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 5432,
            ToPort: 5432,
            CidrIp: "172.16.0.0/12",
          }),
          Match.objectLike({
            IpProtocol: "tcp",
            FromPort: 3306,
            ToPort: 3306,
            CidrIp: "172.16.0.0/12",
          }),
        ]),
      });
    });
  });
});

describe("ServeStack - OE Monitoring", () => {
  let template: Template;

  beforeAll(() => {
    template = createStack();
  });

  it("emits a serve OE dashboard and Lambda alarms", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(1);
  });
});

describe("ServeStack - Athena S3 permissions", () => {
  let template: Template;

  beforeAll(() => {
    template = createStack();
  });

  /**
   * Collect every action string granted across all IAM policy statements whose
   * resource list mentions the given substring. Statements are keyed on the
   * bucket name rather than a policy logical id so the assertions survive
   * refactors that move statements between policies.
   */
  function actionsForResource(resourceSubstring: string): string[] {
    const policies = template.findResources("AWS::IAM::Policy");
    const actions: string[] = [];
    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement ?? [];
      for (const statement of statements) {
        const serialized = JSON.stringify(statement.Resource ?? "");
        if (!serialized.includes(resourceSubstring)) continue;
        const stmtActions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        for (const action of stmtActions) {
          if (typeof action === "string") actions.push(action);
        }
      }
    }
    return actions;
  }

  // Athena writes result files with a multipart upload once they exceed the
  // single-PutObject threshold, and aborts the upload on failure. Small result
  // sets never exercise this, which is why the gap only surfaced against real
  // data — assert the actions explicitly so it cannot regress silently.
  it("grants multipart and delete actions on the Athena results bucket", () => {
    const actions = actionsForResource("athena-results");
    expect(actions).toContain("s3:AbortMultipartUpload");
    expect(actions).toContain("s3:ListMultipartUploadParts");
    expect(actions).toContain("s3:DeleteObject");
    expect(actions).toContain("s3:PutObject");
  });

  // The managed federated connector WRITES spill data here when results exceed
  // Lambda memory; a read-only policy fails only on large queries.
  it("grants write access on the Athena spill bucket", () => {
    // Account-suffixed on purpose: the bare "athena-spill" substring also matches
    // the cross-account wildcard spill-READ resource, which would let this test
    // pass on statements that say nothing about Orion's own bucket.
    const actions = actionsForResource("athena-spill-123456789012");
    expect(actions).toContain("s3:PutObject");
    expect(actions).toContain("s3:GetObject");
    expect(actions).toContain("s3:AbortMultipartUpload");
    expect(actions).toContain("s3:ListMultipartUploadParts");
    expect(actions).toContain("s3:DeleteObject");
  });

  // Least privilege: the broadened S3 grants must stay scoped to the Athena
  // buckets and must not become a wildcard on all of S3.
  it("does not grant S3 wildcard actions", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    for (const policy of Object.values(policies)) {
      const statements = policy.Properties?.PolicyDocument?.Statement ?? [];
      for (const statement of statements) {
        const stmtActions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        expect(stmtActions).not.toContain("s3:*");
      }
    }
  });
});

describe("ServeStack - Redshift execution engine IAM", () => {
  let template: Template;

  beforeAll(() => {
    template = createStack();
  });

  it("grants the serve runtime the Redshift Data API actions", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: Match.arrayWith([
              "redshift-data:ExecuteStatement",
              "redshift-data:DescribeStatement",
              "redshift-data:GetStatementResult",
            ]),
          }),
        ]),
      },
    });
  });

  it("grants redshift-serverless:GetCredentials scoped to workgroups", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: "redshift-serverless:GetCredentials",
            Resource: Match.stringLikeRegexp(
              "arn:aws:redshift-serverless:.*:workgroup/\\*",
            ),
          }),
        ]),
      },
    });
  });

  it("sets the REDSHIFT_SERVE_DATABASE env var on the runtime", () => {
    template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        REDSHIFT_SERVE_DATABASE: "dev",
      }),
    });
  });

  it("resolves GROUP_CLAIM_NAME from SSM (not a hardcoded Cognito default)", () => {
    // Regression: this used to be a hardcoded DEFAULT_GROUP_CLAIM
    // ("cognito:groups") passed from app.ts regardless of idpType, which
    // silently broke group-based role resolution on the direct-OIDC path
    // (external IdPs configure their own claim name, e.g. plain "groups").
    // Must be an SSM dynamic reference resolved from
    // /authentication-group-token-name (written by idp-authentication-stack.ts
    // on both the Cognito/SAML and OIDC paths), not a literal string.
    template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        GROUP_CLAIM_NAME: Match.anyValue(),
      }),
    });
  });
});

describe("ServeStack - custom Athena federation connector IAM", () => {
  let template: Template;
  /** Statements attached to the AgentCore runtime's execution role only. */
  let runtimeStatements: any[];
  /** Every statement in the stack, for the negative guards below. */
  let allStatements: any[];

  const actionsOf = (s: any): string[] =>
    Array.isArray(s.Action) ? s.Action : [s.Action];

  beforeAll(() => {
    template = createStack();
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    allStatements = policies.flatMap(
      (p: any) => p.Properties?.PolicyDocument?.Statement ?? [],
    );
    // Scope to the runtime's own role: collecting across every policy in the
    // stack would let a statement mistakenly attached to the AOSS proxy Lambda
    // satisfy all of these assertions.
    const runtimeRolePolicy = policies.find((p: any) =>
      String(p.Properties?.PolicyName ?? "").includes(
        "AgentCoreRuntimeExecutionRole",
      ),
    );
    expect(runtimeRolePolicy).toBeDefined();
    runtimeStatements = (runtimeRolePolicy as any).Properties.PolicyDocument
      .Statement;
  });

  const bySid = (sid: string) =>
    runtimeStatements.find((s: any) => s.Sid === sid);

  // Two controls, each covering what the other cannot. `aws:CalledVia` makes this an
  // Athena-only capability rather than a general invoke primitive; the resource TAG is
  // what scopes WHICH functions, since the account must stay a wildcard (the connector
  // lives in the customer's) and Athena exposes no condition key naming the catalog a
  // forward-access-session invoke serves.
  it("grants connector invoke only when Athena is the caller, and only for tagged functions", () => {
    expect(bySid("AthenaFederationConnectorInvoke")).toEqual({
      Sid: "AthenaFederationConnectorInvoke",
      Effect: "Allow",
      Action: "lambda:InvokeFunction",
      Resource: "arn:aws:lambda:us-east-1:*:function:*",
      Condition: {
        "ForAnyValue:StringEquals": {
          "aws:CalledVia": "athena.amazonaws.com",
        },
        StringEquals: { "aws:ResourceTag/coa:connector": "true" },
      },
    });
  });

  // The tag is the whole resource scope — the ARN is a bare wildcard — so losing the
  // condition silently restores an invoke-anything-in-any-account-via-Athena grant.
  it("does not grant invoke without the connector tag condition", () => {
    const condition = bySid("AthenaFederationConnectorInvoke").Condition;
    expect(condition.StringEquals).toHaveProperty(
      "aws:ResourceTag/coa:connector",
    );
  });

  // Without this, an Athena UDF (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`) —
  // which needs only StartQueryExecution, already held, plus InvokeFunction —
  // reaches every Lambda in OUR account, including the Lake-Formation-admin
  // federation provisioner. Same-account invokes need no resource policy, so
  // nothing else would stop it.
  //
  // Account-wide and region-wide, NOT scoped to our name prefix. The prefix form
  // made a naming convention load-bearing for security and silently refused any
  // connector deployed into this account under the prefix — which is exactly what
  // scripts/deploy-example-connector.sh does.
  it("denies Athena-mediated invoke of every untagged function in this account", () => {
    expect(bySid("DenyAthenaInvokeOfUntaggedFunctions")).toEqual({
      Sid: "DenyAthenaInvokeOfUntaggedFunctions",
      Effect: "Deny",
      Action: "lambda:InvokeFunction",
      Resource: "arn:aws:lambda:*:123456789012:function:*",
      // Conditioned on CalledVia, so it cannot touch the direct invokes this role
      // makes legitimately — the AOSS proxy gets an unconditioned grantInvoke.
      // Conditioned on the tag's ABSENCE, so a connector deployed alongside this
      // stack is reachable while everything of ours stays denied.
      Condition: {
        "ForAnyValue:StringEquals": {
          "aws:CalledVia": "athena.amazonaws.com",
        },
        StringNotEquals: { "aws:ResourceTag/coa:connector": "true" },
      },
    });
  });

  // StringNotEquals matches an ABSENT key, which is what makes the exemption fail
  // closed: drop the condition and every same-account function is denied again,
  // connector included; invert it to StringEquals and the Deny denies precisely
  // the one thing it must permit.
  it("exempts the connector tag by its absence, not by an equality match", () => {
    const condition = bySid("DenyAthenaInvokeOfUntaggedFunctions").Condition;
    expect(condition.StringNotEquals).toEqual({
      "aws:ResourceTag/coa:connector": "true",
    });
    expect(condition.StringEquals).toBeUndefined();
  });

  // The KEY PREFIX is what bounds this grant, not the account: the bucket belongs to
  // the customer, so it cannot be pinned, and every connector is required to spill
  // under `connectors/{connectorId}/spills/`. That is what lets the statement span
  // every account INCLUDING this one without becoming a general S3 read — a bucket
  // here is reachable only at that path, which nothing else of ours writes to.
  // aws:RequestedRegion stands in for the region S3 ARNs do not carry.
  it("scopes the spill read to the connector spill prefix and this region", () => {
    expect(bySid("AthenaFederationSpillRead")).toEqual({
      Sid: "AthenaFederationSpillRead",
      Effect: "Allow",
      Action: "s3:GetObject",
      Resource: "arn:aws:s3:::*/connectors/*/spills/*",
      Condition: {
        "ForAnyValue:StringEquals": {
          "aws:CalledVia": "athena.amazonaws.com",
        },
        StringEquals: { "aws:RequestedRegion": "us-east-1" },
      },
    });
  });

  // Spanning our own account is the point of the prefix scoping — it is the one
  // topology the previous aws:ResourceAccount exclusion could not serve, since a
  // connector deployed alongside Orion could never spill. Asserted so a future
  // "tighten this" change has to confront the trade-off rather than silently
  // reintroduce it.
  it("does not exclude this account from the spill read", () => {
    expect(bySid("AthenaFederationSpillRead").Condition).not.toHaveProperty(
      "StringNotEquals",
    );
  });

  // The bucket-level actions DID prove necessary — a cross-account spill read
  // returned 403 with the bucket policy granting all three, and simulating the
  // role showed GetObject `allowed` while the other two were `implicitDeny`.
  // They went into their own statements as planned, so this assertion still
  // holds and now guards against them being folded back in: on a wildcard
  // object resource they would be unscopable, since `s3:prefix` gates ListBucket
  // only and the path patterns cannot apply to a bucket ARN.
  it("does not grant bucket-level S3 actions on the wildcard spill resource", () => {
    const actions = actionsOf(bySid("AthenaFederationSpillRead"));
    expect(actions).not.toContain("s3:ListBucket");
    expect(actions).not.toContain("s3:GetBucketLocation");
  });

  // GetBucketLocation has no `s3:prefix` key, so it cannot share a statement with
  // ListBucket: a StringLike against the absent key evaluates false and would deny
  // the call. Hence two statements for two actions. It also cannot be prefix-scoped
  // at all — it takes a bucket ARN — and reveals only a bucket's region, which is
  // the least sensitive thing S3 will answer.
  it("grants spill GetBucketLocation via Athena in this region", () => {
    expect(bySid("AthenaFederationSpillBucketLocation")).toEqual({
      Sid: "AthenaFederationSpillBucketLocation",
      Effect: "Allow",
      Action: "s3:GetBucketLocation",
      Resource: "arn:aws:s3:::*",
      Condition: {
        "ForAnyValue:StringEquals": {
          "aws:CalledVia": "athena.amazonaws.com",
        },
        StringEquals: { "aws:RequestedRegion": "us-east-1" },
      },
    });
  });

  // The s3:prefix condition is the only thing keeping a wildcard-bucket ListBucket
  // from being an enumeration primitive, and it matters more now that the statement
  // spans this account: without it, this would list any bucket Orion owns.
  it("scopes spill ListBucket to the connector spill prefix", () => {
    expect(bySid("AthenaFederationSpillList")).toEqual({
      Sid: "AthenaFederationSpillList",
      Effect: "Allow",
      Action: "s3:ListBucket",
      Resource: "arn:aws:s3:::*",
      Condition: {
        "ForAnyValue:StringEquals": {
          "aws:CalledVia": "athena.amazonaws.com",
        },
        StringEquals: { "aws:RequestedRegion": "us-east-1" },
        StringLike: { "s3:prefix": "connectors/*/spills/*" },
      },
    });
  });

  // Every S3 spill statement spans this account, so `aws:CalledVia` is now the only
  // thing keeping them off the direct-invoke path. Asserted across all three rather
  // than per statement: dropping it from any one turns that statement into a plain
  // S3 grant on the serve role.
  it("gates every S3 spill statement on Athena as the caller", () => {
    for (const sid of [
      "AthenaFederationSpillRead",
      "AthenaFederationSpillBucketLocation",
      "AthenaFederationSpillList",
    ]) {
      expect(bySid(sid).Condition["ForAnyValue:StringEquals"]).toEqual({
        "aws:CalledVia": "athena.amazonaws.com",
      });
    }
  });

  // The KMS grant is scoped by a tag on the KEY, not by excluding this account — a
  // spill path is not expressible for kms:Decrypt (S3 Bucket Keys put the BUCKET arn
  // in kms:EncryptionContext, not the object's), so a tag is the only per-resource
  // handle KMS offers. Tighter than the exclusion in BOTH directions: it keeps this
  // off Orion's own keys and off arbitrary foreign keys, which the exclusion allowed.
  it("scopes the spill KMS decrypt to tagged keys, in any account", () => {
    const condition = bySid("AthenaFederationSpillDecryptViaS3").Condition;
    expect(condition.StringEquals).toEqual({
      "kms:ViaService": "s3.us-east-1.amazonaws.com",
      "aws:ResourceTag/coa:connector-spill": "true",
    });
    // Spanning this account is what lets a connector deployed alongside Orion spill
    // at all; the previous exclusion made that impossible.
    expect(condition).not.toHaveProperty("StringNotEquals");
  });

  // Both keys must sit in ONE StringEquals object. Two `StringEquals` properties in
  // the object literal would silently overwrite each other, dropping kms:ViaService
  // and widening the grant to any KMS caller — a mistake made once already.
  it("keeps kms:ViaService alongside the tag rather than replacing it", () => {
    expect(
      bySid("AthenaFederationSpillDecryptViaS3").Condition.StringEquals[
        "kms:ViaService"
      ],
    ).toBe("s3.us-east-1.amazonaws.com");
  });

  // kms:ViaService, NOT aws:CalledVia: under bucket-level SSE-KMS, S3 is the
  // immediate KMS caller. Conditions within a statement are ANDed, so adding
  // aws:CalledVia would make the grant depend on whether S3 appends itself to
  // the chain — undocumented, and it would fail closed on every spilled query.
  it("conditions spill decrypt on S3 as the KMS caller, for a tagged key", () => {
    expect(bySid("AthenaFederationSpillDecryptViaS3")).toEqual({
      Sid: "AthenaFederationSpillDecryptViaS3",
      Effect: "Allow",
      Action: "kms:Decrypt",
      Resource: "arn:aws:kms:us-east-1:*:key/*",
      Condition: {
        StringEquals: {
          "kms:ViaService": "s3.us-east-1.amazonaws.com",
          "aws:ResourceTag/coa:connector-spill": "true",
        },
      },
    });
  });

  // Positive guard over EVERY statement in the stack, not just the sid'd ones:
  // an unconditioned kms:Decrypt is exactly what a future `key.grantDecrypt()`
  // or `bucket.grantRead()` renders, and CDK-generated statements carry no Sid.
  it("grants no kms:Decrypt anywhere without a kms:ViaService condition", () => {
    const unguarded = allStatements.filter(
      (s: any) =>
        actionsOf(s).includes("kms:Decrypt") &&
        s.Condition?.StringEquals?.["kms:ViaService"] === undefined,
    );
    expect(unguarded).toEqual([]);
  });
});

describe("ServeStack - query LLM model", () => {
  it("emits BEDROCK_MODEL_ID with the shared default when bedrockLlmModelId is unset", () => {
    createStack().hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        BEDROCK_MODEL_ID: DEFAULT_BEDROCK_LLM_MODEL_ID,
      }),
    });
  });
});

describe("ServeStack - guardrail off switch", () => {
  it("sets no SERVE_GUARDRAILS_DISABLED variable unless the context key is passed", () => {
    // The switch removes the prompt-attack boundary, so the default deployment
    // must not carry the variable at all — not even set to "false", which invites
    // someone to flip it in the console.
    const runtimes = createStack().findResources(
      "AWS::BedrockAgentCore::Runtime",
    );
    for (const runtime of Object.values(runtimes)) {
      expect(runtime.Properties.EnvironmentVariables).not.toHaveProperty(
        "SERVE_GUARDRAILS_DISABLED",
      );
    }
  });

  it("passes the context value through to the runtime when set", () => {
    createStack({ serve_guardrails_disabled: "true" }).hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      {
        EnvironmentVariables: Match.objectLike({
          SERVE_GUARDRAILS_DISABLED: "true",
        }),
      },
    );
  });

  it("refuses to synthesize a prod stack with guardrails disabled", () => {
    expect(() =>
      createStack({ env: "prod", serve_guardrails_disabled: "true" }),
    ).toThrow(/SERVE_GUARDRAILS_DISABLED cannot be enabled in prod/);
  });

  it("refuses every spelling serve itself honours", () => {
    // The guard parses the same truthy set as config._guardrails_disabled(), so
    // "yes" cannot slip past a check that only looked for "true".
    for (const value of ["1", "on", "yes", "TRUE", " true "]) {
      expect(() =>
        createStack({ env: "prod", serve_guardrails_disabled: value }),
      ).toThrow(/cannot be enabled in prod/);
    }
  });

  it("leaves a prod stack alone for values serve treats as off", () => {
    // Not just permissiveness: a benchmark script that templates the key in
    // unconditionally as "false" must not break prod deployments.
    expect(() =>
      createStack({ env: "prod", serve_guardrails_disabled: "false" }),
    ).not.toThrow();
  });
});

describe("ServeStack - deep-reasoning budget variables", () => {
  // These budgets were renamed from AGENTIC_* when the execution mode was
  // rebranded to "deep reasoning". The stack's non-default values (110s budget vs
  // the 30s code default) are what make the mode usable at all, and serve reads
  // them by name — so a half-reverted rename silently drops the deployment back to
  // the code defaults with no error anywhere. These assertions fail if that happens.
  it("passes the DEEP_REASONING_* budgets to the runtime", () => {
    createStack().hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        DEEP_REASONING_TIME_BUDGET_S: "110",
        DEEP_REASONING_PER_TOOL_TIMEOUT_S: "45",
        DEEP_REASONING_SYNTHESIS_RESERVE_S: "25",
      }),
    });
  });

  it("sets no pre-rename AGENTIC_* budget variables", () => {
    // Both spellings present would be ambiguous: config.py prefers the new name,
    // so a stale AGENTIC_* would look effective while being ignored.
    const runtimes = createStack().findResources(
      "AWS::BedrockAgentCore::Runtime",
    );
    for (const runtime of Object.values(runtimes)) {
      for (const stale of [
        "AGENTIC_TIME_BUDGET_S",
        "AGENTIC_PER_TOOL_TIMEOUT_S",
        "AGENTIC_SYNTHESIS_RESERVE_S",
      ]) {
        expect(runtime.Properties.EnvironmentVariables).not.toHaveProperty(
          stale,
        );
      }
    }
  });

  it("honours the deep_reasoning_* CDK context overrides", () => {
    createStack({
      deep_reasoning_time_budget_s: "150",
      deep_reasoning_per_tool_timeout_s: "60",
      deep_reasoning_synthesis_reserve_s: "30",
    }).hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        DEEP_REASONING_TIME_BUDGET_S: "150",
        DEEP_REASONING_PER_TOOL_TIMEOUT_S: "60",
        DEEP_REASONING_SYNTHESIS_RESERVE_S: "30",
      }),
    });
  });
});

describe("ServeStack - Tier-1 metric timeout", () => {
  it("passes the explicit default to the runtime", () => {
    createStack().hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        TIER1_METRIC_TIMEOUT_S: "35",
      }),
    });
  });

  it("honours the tier1_metric_timeout_s CDK context override", () => {
    createStack({
      tier1_metric_timeout_s: "75",
    }).hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        TIER1_METRIC_TIMEOUT_S: "75",
      }),
    });
  });
});

describe("ServeStack - credential-secret namespace binding", () => {
  // JDBC credential secrets are granted to the serve runtime per secret, by a
  // resource policy whose StringLike condition matches the namespace as a whole
  // entry in the secret's `<prefix>:namespace` tag. Same-account access is
  // satisfied by EITHER the identity policy or the resource policy, so a broad
  // identity grant covering those secrets makes that condition unenforceable —
  // `{prefix}-*` matches the platform's own `{prefix}-{env}-datasource-*`
  // credential-secret naming. Verified against a live account: unconditioned, a
  // secret tagged for a DIFFERENT namespace was readable. Requiring the tag to be
  // ABSENT here keeps the two mechanisms disjoint.
  it("excludes namespace-bound secrets from the runtime's broad identity grant", () => {
    const template = createStack();
    const statements = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).flatMap((p: any) => p.Properties.PolicyDocument.Statement);

    const reads = statements.filter((s: any) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.includes("secretsmanager:GetSecretValue");
    });
    expect(reads).toHaveLength(1);
    expect(reads[0].Sid).toBe("ReadPlatformSecrets");
    expect(reads[0].Condition).toEqual({
      Null: { "secretsmanager:ResourceTag/coa:namespace": "true" },
    });
  });

  // The key is prefix-derived, so a regression to a literal `coa:namespace` would
  // silently exclude the wrong key on a non-`coa` deployment — re-opening the gap
  // while appearing to close it.
  it("derives the tag key from the deployment prefix", () => {
    const template = createStack({ resource_prefix: "scl" });
    const statements = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    const read = statements.find((s: any) => s.Sid === "ReadPlatformSecrets");
    expect(read.Condition).toEqual({
      Null: { "secretsmanager:ResourceTag/scl:namespace": "true" },
    });
  });
});

describe("ServeStack - GRAPH_URI_TEMPLATE reader/writer alignment", () => {
  // Serve reads the named graphs that metric-service and ontology-engine write.
  // If its prefix stops matching their base, it matches zero graphs and the
  // symptom is "no metrics published", not an error. These pin that invariant.
  it("derives the template from DEFAULT_GRAPH_URI_BASE, the writers' base", () => {
    createStack().hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        GRAPH_URI_TEMPLATE: `${DEFAULT_GRAPH_URI_BASE}/{namespace}`,
      }),
    });
  });

  it("keeps the {namespace} placeholder query_utils requires", () => {
    // query_utils.resolve_graph_uri_template() raises ValueError without it.
    const runtimes = createStack().findResources(
      "AWS::BedrockAgentCore::Runtime",
    );
    const values = Object.values(runtimes);
    expect(values.length).toBeGreaterThan(0);
    for (const runtime of values) {
      const template = runtime.Properties.EnvironmentVariables
        .GRAPH_URI_TEMPLATE as string;
      expect(template).toContain("{namespace}");
      expect(template.split("{namespace}")[0]).toBe(
        `${DEFAULT_GRAPH_URI_BASE}/`,
      );
    }
  });

  it("throws on a graph_uri_template context override instead of ignoring it", () => {
    expect(() =>
      createStack({
        graph_uri_template: "https://someone-elses-base.example/{namespace}",
      }),
    ).toThrow(/graph_uri_template.*was removed/s);
  });

  it("names DEFAULT_GRAPH_URI_BASE in that error so the fix is actionable", () => {
    expect(() =>
      createStack({ graph_uri_template: "https://x.example/{namespace}" }),
    ).toThrow(/DEFAULT_GRAPH_URI_BASE/);
  });
});
