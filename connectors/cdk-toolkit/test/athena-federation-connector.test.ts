// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as lambda from "aws-cdk-lib/aws-lambda";
import {
  AthenaFederationConnector,
  AthenaFederationConnectorProps,
  CONNECTOR_FUNCTION_SUFFIX,
  Provisioning,
} from "../src/athena-federation-connector";
import {
  CONNECTOR_SPILL_KMS_TAG_KEY,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
} from "../src/coa-contract";

// A stand-in for the fat JAR, so the tests do not require `mvn package` to have run. The construct
// copies it into the package layout without inspecting its contents.
const FAKE_JAR = path.join(__dirname, "..", "cdk.out", "test-fixture.jar");

const SERVE_ROLE =
  "arn:aws:iam::999988887777:role/scl-dev-AgentCoreRuntimeRole-ABC123";

beforeAll(() => {
  fs.mkdirSync(path.dirname(FAKE_JAR), { recursive: true });
  fs.writeFileSync(FAKE_JAR, "not really a jar");
});

function synth(props: Partial<AthenaFederationConnectorProps> = {}): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "TestStack", {
    env: { account: "123456789012", region: "eu-central-1" },
  });
  new AthenaFederationConnector(stack, "Connector", {
    connectorId: "example",
    handler: "dev.coa.example.ExampleCompositeHandler",
    jarPath: FAKE_JAR,
    queryRoleArns: [SERVE_ROLE],
    ...props,
  });
  return Template.fromStack(stack);
}

describe("connector Lambda", () => {
  it("sets the Arrow --add-opens flag, without which every read fails", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Handler: "dev.coa.example.ExampleCompositeHandler",
      Environment: {
        Variables: Match.objectLike({
          JAVA_TOOL_OPTIONS: "--add-opens=java.base/java.nio=ALL-UNNAMED",
        }),
      },
    });
  });

  it("keeps the COA suffix when a prefix resolves a name conflict", () => {
    // The suffix is a convention, not a grant — invoke is scoped on the coa:connector tag. A
    // prefix is offered rather than a free-form name so the convention survives.
    synth({ functionNamePrefix: "acme-dev-" }).hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `acme-dev-example${CONNECTOR_FUNCTION_SUFFIX}`,
    });
  });

  it("refuses a name that either a Lambda or a stack would reject", () => {
    expect(() => synth({ functionNamePrefix: "acme.dev." })).toThrow(
      /cannot name both a Lambda and a CloudFormation stack/,
    );
    expect(() => synth({ functionNamePrefix: "x".repeat(60) })).toThrow(
      /Lambda allows 64/,
    );
    // The two Lambda accepts and CloudFormation does not. Both used to pass here, synth clean,
    // and fail at CreateStack — after the jar had already been built and staged.
    expect(() => synth({ functionNamePrefix: "acme_dev-" })).toThrow(
      /no underscores/,
    );
    expect(() => synth({ functionNamePrefix: "2acme-" })).toThrow(
      /must start with a letter/,
    );
  });

  it("names the function from the connector id plus the conventional suffix", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `example${CONNECTOR_FUNCTION_SUFFIX}`,
    });
    expect(CONNECTOR_FUNCTION_SUFFIX).toBe("-coa-connector");
  });

  it("ships the JAR as an S3 asset rather than inline code", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Code: Match.objectLike({ S3Bucket: Match.anyValue(), S3Key: Match.anyValue() }),
    });
  });

  it("nests the JAR under lib/, which is what keeps it under Lambda's size limit", () => {
    // Lambda counts the package's extracted size and does not extract a nested jar. Flattened, the
    // connector's classes expand past the 250 MB limit on a current federation SDK, and the failure
    // is a deploy CloudFormation rejects rather than anything a template assertion would catch.
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "AssetStack", {
      env: { account: "123456789012", region: "eu-central-1" },
    });
    new AthenaFederationConnector(stack, "Connector", {
      connectorId: "example",
      handler: "dev.coa.example.ExampleCompositeHandler",
      jarPath: FAKE_JAR,
      queryRoleArns: [SERVE_ROLE],
    });
    const assembly = app.synth();
    const staged = fs
      .readdirSync(assembly.directory)
      .filter((entry) => entry.startsWith("asset."))
      .map((entry) =>
        path.join(assembly.directory, entry, "lib", path.basename(FAKE_JAR)),
      );
    expect(staged.filter((jar) => fs.existsSync(jar))).toHaveLength(1);
  });

  it("runs on a supported Java runtime with room to buffer a block", () => {
    // java21, not java17: java17 is the Amazon Linux 2 variant and AL2 is past end of life.
    // arm64 matches every Lambda `infra` deploys, and the jar carries no amd64-only native.
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "java21",
      Architectures: ["arm64"],
      MemorySize: 1024,
      Timeout: 600,
    });
  });

  it("takes an x86_64 override, for a fork whose jar carries an amd64-only native", () => {
    synth({ architecture: lambda.Architecture.X86_64 }).hasResourceProperties(
      "AWS::Lambda::Function",
      { Architectures: ["x86_64"] },
    );
  });

  it("refuses a missing JAR with the command that builds it", () => {
    expect(() => synth({ jarPath: path.join(os.tmpdir(), "nope.jar") })).toThrow(
      /Connector JAR not found[\s\S]*mvn -q -B package -pl example -am/,
    );
  });

  it("registers no Athena data catalog: the catalog belongs to the querying account", () => {
    synth().resourceCountIs("AWS::Athena::DataCatalog", 0);
  });
});

describe("connector-specific settings", () => {
  it("passes manifest environment straight through", () => {
    synth({
      environment: { example_bulk_rows: "4096", example_bulk_row_bytes: "2048" },
    }).hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: Match.objectLike({
          example_bulk_rows: "4096",
          example_bulk_row_bytes: "2048",
        }),
      },
    });
  });

  it("refuses an environment key the construct manages", () => {
    // Silently winning or silently losing would both read as "spill config ignored".
    expect(() =>
      synth({ environment: { spill_bucket: "someone-elses-bucket" } }),
    ).toThrow(/which this construct manages/);
  });
});

describe("spill bucket", () => {
  it("always creates one, for connector-to-connector isolation", () => {
    const template = synth();
    template.resourceCountIs("AWS::S3::Bucket", 1);
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: Match.anyValue(),
      }),
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it("enables spill encryption explicitly", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: Match.objectLike({ disable_spill_encryption: "false" }),
      },
    });
  });

  it("expires spill data after a day", () => {
    synth().hasResourceProperties("AWS::S3::Bucket", {
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ Id: "expire-spill", ExpirationInDays: 1, Status: "Enabled" }),
        ]),
      },
    });
  });

  it("puts the connector id in the spill prefix, so COA can grant per connector", () => {
    const functions = synth().findResources("AWS::Lambda::Function");
    const variables = Object.values(functions)
      .map((resource) => resource.Properties?.Environment?.Variables)
      .find((vars) => vars?.spill_prefix !== undefined);
    expect(variables?.spill_prefix).toBe("connectors/example/spills");
  });

  it("survives the same role given twice, which is how one role does both COA jobs", () => {
    // grantInvoke derives its construct id from the principal, so a duplicate used to fail synth
    // with "There is already a Construct with name..." — naming neither the ARN nor the caller.
    const template = synth({ queryRoleArns: [SERVE_ROLE, SERVE_ROLE] });
    template.resourceCountIs("AWS::Lambda::Permission", 1);
    const statements = Object.values(template.findResources("AWS::S3::BucketPolicy"))
      .flatMap((policy) => policy.Properties?.PolicyDocument?.Statement ?? [])
      .filter((statement: { Sid?: string }) => statement.Sid?.startsWith("CoaSpillRead"));
    expect(statements).toHaveLength(1);
  });

  it("grants the bucket-level actions GetSplits needs before any spill exists", () => {
    // SpillLocationVerifier calls HeadBucket before returning splits, which needs s3:ListBucket on
    // the BUCKET, not on the prefix. Without it every query fails in GetSplits — not just the ones
    // large enough to spill — so the scope of this one is easy to tighten away by accident.
    const policies = synth().findResources("AWS::IAM::Policy");
    const bucketLevel = Object.values(policies)
      .flatMap((policy) => policy.Properties?.PolicyDocument?.Statement ?? [])
      .filter((statement: { Action?: unknown }) => {
        const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
        return actions.includes("s3:ListBucket");
      });
    expect(bucketLevel.length).toBeGreaterThan(0);
    for (const statement of bucketLevel) {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      expect(actions).toContain("s3:GetBucketLocation");
      // Resource is the bucket itself — an object pattern would not satisfy HeadBucket.
      expect(JSON.stringify(statement.Resource)).not.toContain("/*");
    }
  });

  it("grants the connector write access under the spill prefix, and nowhere wider", () => {
    // The arrayWith assertion below proves one statement names the prefix; it cannot fail on an
    // ADDITIONAL wider statement, which is what "and nowhere wider" claims. So enumerate every
    // object-level resource the connector's own policy grants and assert none is the bare bucket.
    const policies = synth().findResources("AWS::IAM::Policy");
    const resources = Object.values(policies)
      .flatMap((policy) => policy.Properties?.PolicyDocument?.Statement ?? [])
      .flatMap((statement: { Resource?: unknown }) =>
        Array.isArray(statement.Resource) ? statement.Resource : [statement.Resource],
      )
      .map((resource) => JSON.stringify(resource))
      .filter((resource) => resource.includes("SpillBucket"));
    expect(resources.length).toBeGreaterThan(0);
    for (const resource of resources) {
      // Either the bucket itself (list/locate) or an object path under the spill prefix — never
      // an unscoped object grant like `${bucket}/*`.
      if (resource.includes("/")) {
        expect(resource).toContain("connectors/example/spills/");
      }
    }
  });

  it("grants the connector write access only under the spill prefix", () => {
    synth().hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Resource: Match.arrayWith([
              Match.objectLike({
                "Fn::Join": Match.arrayWith([
                  Match.arrayWith([Match.stringLikeRegexp("connectors/example/spills/\\*")]),
                ]),
              }),
            ]),
          }),
        ]),
      }),
    });
  });

  it("creates no bucket under Provisioning.NONE, and sets no spill_bucket", () => {
    // Legitimate for a source that cannot exceed 6 MB, and it fails on the first response
    // that would have spilled — so what matters is that nothing half-configured is left
    // behind: no bucket, no grant, and no spill_bucket pointing at nothing.
    const template = synth({ spill: Provisioning.NONE });
    template.resourceCountIs("AWS::S3::Bucket", 0);
    template.resourceCountIs("AWS::S3::BucketPolicy", 0);
    const functions = template.findResources("AWS::Lambda::Function");
    const variables = Object.values(functions)
      .map((resource) => resource.Properties?.Environment?.Variables)
      .find((vars) => vars?.spill_prefix !== undefined);
    expect(variables?.spill_bucket).toBeUndefined();
    expect(variables?.disable_spill_encryption).toBe("false");
  });

  it("still lets the serve role invoke the connector under Provisioning.NONE", () => {
    // The invoke grant is unrelated to spill; only the bucket grants disappear.
    synth({ spill: Provisioning.NONE }).hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: SERVE_ROLE,
    });
  });

});

describe("COA access", () => {
  it("lets the serve role invoke the connector", () => {
    // A cross-account principal needs an allow on both sides; this stack owns this side.
    synth().hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: SERVE_ROLE,
    });
  });

  it("lets the serve role read spilled blocks", () => {
    // Spill objects are read with the querying role's credentials, not the connector's.
    synth().hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Principal: { AWS: SERVE_ROLE },
            Action: Match.anyValue(),
          }),
        ]),
      }),
    });
  });

  it("grants GetObject under the spill prefix, since the querying role reads the blocks", () => {
    synth().hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "CoaSpillRead0",
            Action: "s3:GetObject",
            Principal: { AWS: SERVE_ROLE },
          }),
        ]),
      }),
    });
  });

  it("conditions kms:Decrypt on kms:ViaService, not aws:CalledVia", () => {
    // Under bucket-level SSE-KMS the immediate KMS caller is S3, not Athena, so a CalledVia
    // condition would fail closed on every spilled query.
    synth().hasResourceProperties("AWS::KMS::Key", {
      KeyPolicy: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "CoaSpillDecrypt0",
            Action: "kms:Decrypt",
            Principal: { AWS: SERVE_ROLE },
            Condition: {
              StringEquals: {
                "kms:ViaService": Match.anyValue(),
              },
            },
          }),
        ]),
      }),
    });
  });

  it("leaves no unconditioned kms:Decrypt in the key policy", () => {
    // The trap this guards: bucket.grantRead() on a KMS bucket also calls
    // encryptionKey.grantDecrypt(), which adds an unconditioned Decrypt and would silently
    // defeat the ViaService condition above. Explicit statements are used instead.
    const keys = synth().findResources("AWS::KMS::Key");
    const statements = Object.values(keys)[0]?.Properties?.KeyPolicy?.Statement ?? [];
    const externalDecrypts = statements.filter(
      (statement: { Action?: unknown; Principal?: { AWS?: unknown } }) =>
        JSON.stringify(statement.Action ?? "").includes("kms:Decrypt") &&
        JSON.stringify(statement.Principal ?? {}).includes(SERVE_ROLE),
    );
    expect(externalDecrypts.length).toBeGreaterThan(0);
    for (const statement of externalDecrypts) {
      expect(statement).toHaveProperty("Condition");
    }
  });

  it("lets the connector's own role generate a data key, so it can write spill", () => {
    synth().hasResourceProperties("AWS::KMS::Key", {
      KeyPolicy: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "ConnectorGenerateSpillDataKey",
            Action: "kms:GenerateDataKey",
          }),
        ]),
      }),
    });
  });

  it("grants every role: serve runs the queries, discovery runs DESCRIBE", () => {
    const second = "arn:aws:iam::111122223333:role/other-serve-role";
    const template = synth({ queryRoleArns: [SERVE_ROLE, second] });
    template.resourceCountIs("AWS::Lambda::Permission", 2);
    template.hasResourceProperties("AWS::Lambda::Permission", { Principal: second });
  });

  it("deploys with no grants when none are supplied", () => {
    const template = synth({ queryRoleArns: [] });
    template.resourceCountIs("AWS::Lambda::Permission", 0);
  });
});

describe("the controls COA's IAM matches on", () => {
  it("tags the function, the one control that fails fast", () => {
    // COA's invoke policy is scoped to this tag with a wildcard account and function name, so
    // without it the function cannot be invoked at all and the first scan is denied.
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Tags: Match.arrayWith([
        Match.objectLike({ Key: CONNECTOR_TAG_KEY, Value: CONNECTOR_TAG_VALUE }),
      ]),
    });
  });

  it("tags the spill key, which is why it must be customer-managed", () => {
    // aws/s3 can be neither tagged nor have its policy edited, so the key has to be a CMK the
    // template creates.
    synth().hasResourceProperties("AWS::KMS::Key", {
      Tags: Match.arrayWith([
        Match.objectLike({ Key: CONNECTOR_SPILL_KMS_TAG_KEY, Value: CONNECTOR_TAG_VALUE }),
      ]),
    });
  });

  it("encrypts the bucket with that key, with a bucket key to avoid per-object KMS calls", () => {
    const template = synth();
    template.resourceCountIs("AWS::KMS::Key", 1);
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            BucketKeyEnabled: true,
            ServerSideEncryptionByDefault: Match.objectLike({ SSEAlgorithm: "aws:kms" }),
          }),
        ],
      },
    });
  });

  it("creates neither key nor bucket under Provisioning.NONE", () => {
    const template = synth({ spill: Provisioning.NONE });
    template.resourceCountIs("AWS::KMS::Key", 0);
    template.resourceCountIs("AWS::S3::Bucket", 0);
  });
});

describe("operational alarms", () => {
  const ALARM_TOPIC = "arn:aws:sns:eu-central-1:123456789012:coa-connector-alarms";

  it("creates the three Lambda health alarms by default", () => {
    const template = synth();

    template.resourceCountIs("AWS::CloudWatch::Alarm", 3);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `example${CONNECTOR_FUNCTION_SUFFIX}-throttles`,
      MetricName: "Throttles",
      Threshold: 0,
      ComparisonOperator: "GreaterThanThreshold",
    });
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `example${CONNECTOR_FUNCTION_SUFFIX}-error-rate`,
      Threshold: 1,
    });
  });

  it("alarms on duration against the connector's own timeout, not a fixed number", () => {
    // 80% of 60s. A per-connector timeout means a shared absolute threshold would be wrong for one
    // of any two connectors.
    synth({ timeout: cdk.Duration.seconds(60) }).hasResourceProperties(
      "AWS::CloudWatch::Alarm",
      {
        AlarmName: `example${CONNECTOR_FUNCTION_SUFFIX}-duration-p99`,
        MetricName: "Duration",
        ExtendedStatistic: "p99",
        Threshold: 48000,
      },
    );
  });

  it("caps the duration threshold, so a timeout sized for a resume still alarms usefully", () => {
    // 80% of the default 600s would be 8 minutes, by which point the query is already lost.
    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `example${CONNECTOR_FUNCTION_SUFFIX}-duration-p99`,
      Threshold: 60000,
    });
  });

  it("treats missing data as not breaching, so an idle connector does not alarm", () => {
    const alarms = synth().findResources("AWS::CloudWatch::Alarm");

    for (const [name, alarm] of Object.entries(alarms)) {
      expect(alarm.Properties.TreatMissingData).toBe("notBreaching");
      expect(name).toBeTruthy();
    }
  });

  it("creates no alarms when asked not to", () => {
    synth({ alarms: Provisioning.NONE }).resourceCountIs("AWS::CloudWatch::Alarm", 0);
  });

  it("notifies nobody unless a topic is given", () => {
    // The failure this guards is an alarm that looks configured and pages no one.
    const alarms = synth().findResources("AWS::CloudWatch::Alarm");

    for (const alarm of Object.values(alarms)) {
      expect(alarm.Properties.AlarmActions).toBeUndefined();
    }
  });

  it("wires every alarm to the topic when one is given", () => {
    const alarms = synth({ alarmTopicArn: ALARM_TOPIC }).findResources(
      "AWS::CloudWatch::Alarm",
    );

    expect(Object.keys(alarms)).toHaveLength(3);
    for (const alarm of Object.values(alarms)) {
      expect(alarm.Properties.AlarmActions).toEqual([ALARM_TOPIC]);
    }
  });

  it("exposes the alarms it created, so a connector's stack can find them", () => {
    const stack = new cdk.Stack(new cdk.App(), "S", {
      env: { account: "123456789012", region: "eu-central-1" },
    });
    const connector = new AthenaFederationConnector(stack, "Connector", {
      connectorId: "example",
      handler: "dev.coa.example.ExampleCompositeHandler",
      jarPath: FAKE_JAR,
    });

    expect(connector.alarms).toHaveLength(3);
  });

  it("dimensions a connector metric so it matches what the jar emits", () => {
    const stack = new cdk.Stack(new cdk.App(), "S", {
      env: { account: "123456789012", region: "eu-central-1" },
    });
    const connector = new AthenaFederationConnector(stack, "Connector", {
      connectorId: "databricks",
      handler: "h",
      jarPath: FAKE_JAR,
    });

    const fleet = connector.connectorMetric("ConnectorRowsReturned");
    expect(fleet.namespace).toBe("COA/Connectors");
    expect(fleet.dimensions).toEqual({ Connector: "databricks" });

    // A catalog narrows it; omitting one is the only view available for metrics emitted below the
    // request, which carry no catalog.
    const scoped = connector.connectorMetric("ConnectorRowsReturned", {
      catalog: "acme_dbx",
    });
    expect(scoped.dimensions).toEqual({
      Connector: "databricks",
      Catalog: "acme_dbx",
    });
  });
});
