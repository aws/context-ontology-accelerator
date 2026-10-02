// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";
import {
  CONNECTOR_DIMENSION,
  CONNECTOR_METRIC_NAMESPACE,
  CONNECTOR_SPILL_KMS_TAG_KEY,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
} from "./coa-contract";

/**
 * The key prefix a connector spills under: `connectors/{connectorId}/spills`.
 *
 * Not settable. COA's spill-read grant matches this exact shape, and the per-connector segment keeps one
 * connector's read grant off another's spilled rows.
 */
export function spillPrefixFor(connectorId: string): string {
  return `connectors/${connectorId}/spills`;
}

/**
 * Suffix every connector's Lambda name carries. A convention only: COA scopes invoke on the
 * {@link CONNECTOR_TAG_KEY} tag with a wildcard function name.
 */
export const CONNECTOR_FUNCTION_SUFFIX = "-coa-connector";

/** Lambda's own limit on a function name. */
const MAX_FUNCTION_NAME_LENGTH = 64;

/** Default invocation timeout: room for a warehouse resume ahead of a large read. */
const DEFAULT_TIMEOUT = cdk.Duration.minutes(10);

/**
 * Ceiling on the duration alarm's threshold, whatever the timeout.
 *
 * The threshold is normally 80% of the timeout, which stops detecting anything actionable once the
 * timeout is minutes long: a connector 8 minutes into one protocol step has already lost the query.
 */
const MAX_DURATION_ALARM_THRESHOLD = cdk.Duration.seconds(60);

/**
 * What both Lambda and CloudFormation accept, because one call names the function and the stack.
 * Lambda alone would allow underscores and a leading digit, and CloudFormation would then reject the
 * stack after the jar had been built and staged.
 */
const FUNCTION_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9-]*$/;

/**
 * The connector's Lambda name: `{prefix}{connectorId}{@link CONNECTOR_FUNCTION_SUFFIX}`. An app names its
 * stack with the same call, so a redeploy replaces the connector rather than adding one.
 *
 * @throws Error if the result cannot name both a Lambda and a CloudFormation stack.
 */
export function connectorFunctionName(
  connectorId: string,
  prefix?: string,
): string {
  const name = `${prefix ?? ""}${connectorId}${CONNECTOR_FUNCTION_SUFFIX}`;
  if (!FUNCTION_NAME_PATTERN.test(name)) {
    throw new Error(
      `"${name}" cannot name both a Lambda and a CloudFormation stack: it must start with a ` +
        `letter and contain only letters, digits and hyphens — no underscores. Check the prefix ` +
        `${JSON.stringify(prefix ?? "")} and the connector id "${connectorId}".`,
    );
  }
  if (name.length > MAX_FUNCTION_NAME_LENGTH) {
    throw new Error(
      `Function name "${name}" is ${name.length} characters; Lambda allows ` +
        `${MAX_FUNCTION_NAME_LENGTH}. The suffix "${CONNECTOR_FUNCTION_SUFFIX}" is fixed, so ` +
        `shorten the prefix or the connector id.`,
    );
  }
  return name;
}

/** Whether the construct creates an optional piece of a connector or leaves it out. */
export enum Provisioning {
  CREATE = "create",
  NONE = "none",
}

/** Environment variables this construct sets itself; a caller may not also set them. */
export const RESERVED_ENVIRONMENT_KEYS = [
  "JAVA_TOOL_OPTIONS",
  "spill_bucket",
  "spill_prefix",
  "disable_spill_encryption",
] as const;

/** Properties for {@link AthenaFederationConnector}. */
/**
 * Where a VPC-attached connector runs. Grouped so a VPC cannot be given without its subnets.
 */
export interface ConnectorNetwork {
  readonly vpc: ec2.IVpc;

  /** Private subnets with egress. A Lambda in a public subnet gets no internet access at all. */
  readonly subnets: ec2.ISubnet[];

  /**
   * Defaults to one security group created for the function, with HTTPS egress only: enough for
   * the AWS APIs a connector calls and for an HTTPS-speaking source. A source on another port
   * needs a group of your own.
   */
  readonly securityGroups?: ec2.ISecurityGroup[];
}

export interface AthenaFederationConnectorProps {
  /**
   * The connector's id — its folder name under `connectors/`. Every unique resource name derives
   * from it, so a second connector cannot reshape the first one's stack.
   */
  readonly connectorId: string;

  /**
   * Tells two deployments of the *same* connector apart when they share an account. Give the stack
   * the same prefix, through {@link connectorFunctionName}: a second deployment reusing the first's
   * stack name replaces it.
   */
  readonly functionNamePrefix?: string;

  /** Fully-qualified handler class, e.g. `dev.coa.example.ExampleCompositeHandler`. */
  readonly handler: string;

  /** Path to the fat JAR. Too large for inline code, so it ships as an S3 asset. */
  readonly jarPath: string;

  /**
   * ARNs of every COA role that reaches this connector through Athena: serve, which runs the queries, and
   * discovery, which runs `DESCRIBE`. Each gets `lambda:InvokeFunction` plus spill read.
   */
  readonly queryRoleArns?: readonly string[];

  /**
   * Pins the execution role's name, for a connector whose role is named in someone else's trust
   * policy: a generated name changes on any replacement, breaking every policy naming the old ARN at
   * once with no repair from this side. At most 64 characters, IAM's limit.
   */
  readonly roleName?: string;

  /**
   * Lambda runtime. Defaults to `java21`, the oldest Java runtime still on Amazon Linux 2023.
   * `java17` is the AL2 variant, and AL2 is past end of life.
   */
  readonly runtime?: lambda.Runtime;

  /**
   * Instruction-set architecture. Defaults to `arm64`, which every Lambda COA's own `infra` deploys
   * uses and which is cheaper per GB-second at the same memory.
   *
   * Safe for a pure-Java connector, and this one is: the fat jar's only native code is the Databricks
   * driver's bundled lz4, which ships `linux/aarch64` alongside `linux/amd64`. Set `X86_64` if you add
   * a dependency carrying an amd64-only native — the failure is an `UnsatisfiedLinkError` at the first
   * invocation that reaches it, not at deploy.
   */
  readonly architecture?: lambda.Architecture;

  /**
   * Attaches the function to a VPC. Unset, it runs outside any VPC.
   *
   * Attached, the function reaches only what the subnets route to, so they need NAT or VPC endpoints
   * for every service it calls: the source itself, S3 and KMS for spill, and whatever else the
   * connector reads. A missing route shows up as the first query timing out, never at deploy.
   */
  readonly network?: ConnectorNetwork;

  /** Invocation timeout. Defaults to 10 minutes. */
  readonly timeout?: cdk.Duration;

  /** Memory. Defaults to 1024 MB — enough to buffer a block before it spills. */
  readonly memorySize?: number;

  /**
   * Whether the connector gets a spill bucket. Defaults to {@link Provisioning.CREATE}.
   *
   * Under {@link Provisioning.NONE} a response over Athena's 6 MB limit fails silently: the query returns
   * `SUCCEEDED` with zero rows and nothing is logged.
   */
  readonly spill?: Provisioning;

  /**
   * What `cdk destroy` does to the spill bucket and its key. Defaults to
   * {@link cdk.RemovalPolicy.DESTROY}, which also empties the bucket.
   *
   * `RETAIN` for production: a destroy takes the CMK and up to a day of spill with it, silently, and
   * neither comes back.
   */
  readonly spillRemovalPolicy?: cdk.RemovalPolicy;

  /** Connector-specific environment variables, from the connector's own stack. */
  readonly environment?: Record<string, string>;

  /** Lambda description. */
  readonly description?: string;

  /**
   * Whether the connector gets the three Lambda health alarms — throttles, error rate, and duration
   * against its own timeout. Defaults to {@link Provisioning.CREATE}.
   *
   * {@link Provisioning.NONE} for a deployment whose operator watches the function some other way.
   */
  readonly alarms?: Provisioning;

  /**
   * SNS topic every alarm on this connector notifies. **An alarm without one notifies nobody** — it
   * changes state in the console and that is all.
   */
  readonly alarmTopicArn?: string;
}

/**
 * One Athena Query Federation connector: a Lambda and, by default, its own spill bucket and the
 * customer-managed key encrypting it.
 *
 * A construct rather than a runbook because the pieces COA's IAM is scoped to fail at different times: a
 * missing `coa:connector` tag denies the first scan loudly, a missing spill grant only above 6 MB.
 */
export class AthenaFederationConnector extends Construct {
  /** The connector Lambda. Register this ARN as an Athena `LAMBDA` data catalog. */
  public readonly connectorFunction: lambda.Function;

  /** The connector's own spill bucket, unless {@link Provisioning.NONE} was chosen. */
  public readonly spillBucket?: s3.Bucket;

  /** The customer-managed key encrypting {@link spillBucket}, created alongside it. */
  public readonly spillKey?: kms.Key;

  /** Every alarm on this connector, the three below plus any the connector's own stack added. */
  public readonly alarms: cloudwatch.Alarm[] = [];

  /** Where alarms notify, when {@link AthenaFederationConnectorProps.alarmTopicArn} was given. */
  public readonly alarmTopic?: sns.ITopic;

  /**
   * The function's name as a literal string, unlike `connectorFunction.functionName`, which is a
   * CloudFormation token even when the name was supplied. Using that one in an alarm name yields an
   * `Fn::Join` that resolves correctly and cannot be read by anyone looking for the alarm.
   */
  public readonly functionName: string;

  private readonly connectorId: string;

  constructor(scope: Construct, id: string, props: AthenaFederationConnectorProps) {
    super(scope, id);
    this.connectorId = props.connectorId;

    if (!fs.existsSync(props.jarPath)) {
      throw new Error(
        `Connector JAR not found at ${props.jarPath}. Build it first:\n` +
          `  cd connectors && mvn -q -B package -pl ${props.connectorId} -am\n` +
          `or from the connector's CDK app, where deploy builds it: pnpm run deploy`,
      );
    }

    const functionName = connectorFunctionName(
      props.connectorId,
      props.functionNamePrefix,
    );
    const spillPrefix = spillPrefixFor(props.connectorId);
    this.functionName = functionName;

    // autoDeleteObjects follows this policy below — S3's L2 refuses RETAIN with auto-delete.
    const spillRemovalPolicy = props.spillRemovalPolicy ?? cdk.RemovalPolicy.DESTROY;

    if ((props.spill ?? Provisioning.CREATE) === Provisioning.CREATE) {
      // Customer-managed because COA's key policy is scoped to a tag. SSE-KMS is required.
      this.spillKey = new kms.Key(this, "SpillKey", {
        description: `Spill encryption for the "${props.connectorId}" COA connector`,
        enableKeyRotation: true,
        removalPolicy: spillRemovalPolicy,
      });
      cdk.Tags.of(this.spillKey).add(CONNECTOR_SPILL_KMS_TAG_KEY, CONNECTOR_TAG_VALUE);

      // One-day expiry: spill data is scratch, and anything older than its query is garbage.
      this.spillBucket = new s3.Bucket(this, "SpillBucket", {
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        enforceSSL: true,
        encryption: s3.BucketEncryption.KMS,
        encryptionKey: this.spillKey,
        // Without this a CMK turns every spilled block into billable KMS traffic.
        bucketKeyEnabled: true,
        versioned: false,
        lifecycleRules: [
          { id: "expire-spill", expiration: cdk.Duration.days(1), enabled: true },
        ],
        removalPolicy: spillRemovalPolicy,
        autoDeleteObjects: spillRemovalPolicy === cdk.RemovalPolicy.DESTROY,
      });
    }

    const environment: Record<string, string> = {
      // MANDATORY on Java 17+. Arrow reaches into java.nio internals, and without this metadata
      // calls succeed while every read fails with "Failed to initialize MemoryUtil" — which
      // presents as "discovery works, queries are broken".
      JAVA_TOOL_OPTIONS: "--add-opens=java.base/java.nio=ALL-UNNAMED",
      spill_prefix: spillPrefix,
      // Client-side encryption of each block, independent of the bucket's SSE-KMS above: the key is
      // generated per query and rides on the Split, so it costs no KMS calls and needs no grant.
      disable_spill_encryption: "false",
    };
    if (this.spillBucket !== undefined) {
      environment.spill_bucket = this.spillBucket.bucketName;
    }
    // Refused rather than merged: either outcome reads as "the spill configuration is ignored".
    for (const [key, value] of Object.entries(props.environment ?? {})) {
      const reserved: readonly string[] = RESERVED_ENVIRONMENT_KEYS;
      if (reserved.includes(key)) {
        throw new Error(
          `Connector "${props.connectorId}" sets environment variable "${key}", which this ` +
            `construct manages. Remove it from the connector's stack. Managed keys: ` +
            `${reserved.join(", ")}.`,
        );
      }
      environment[key] = value;
    }

    // Lambda counts a deployment package's EXTRACTED size against its 250 MB limit and does not
    // extract a nested jar, so the fat jar ships as `lib/<jar>`: on the Java runtime's classpath,
    // counted at its own size. A fresh temp directory each time, because the asset hash is over
    // directory contents and a reused one would carry a previous build's jar in as a second copy.
    const packageDir = cdk.FileSystem.mkdtemp("coa-connector-");
    fs.mkdirSync(path.join(packageDir, "lib"));
    fs.copyFileSync(
      props.jarPath,
      path.join(packageDir, "lib", path.basename(props.jarPath)),
      fs.constants.COPYFILE_FICLONE,
    );

    const network = props.network;
    const securityGroups =
      network === undefined
        ? undefined
        : (network.securityGroups ?? [this.httpsOnlySecurityGroup(network.vpc)]);

    this.connectorFunction = new lambda.Function(this, "Function", {
      functionName,
      vpc: network?.vpc,
      vpcSubnets: network === undefined ? undefined : { subnets: network.subnets },
      securityGroups,
      runtime: props.runtime ?? lambda.Runtime.JAVA_21,
      architecture: props.architecture ?? lambda.Architecture.ARM_64,
      handler: props.handler,
      code: lambda.Code.fromAsset(packageDir),
      timeout: props.timeout ?? DEFAULT_TIMEOUT,
      memorySize: props.memorySize ?? 1024,
      environment,
      // An explicit log group rather than `logRetention`, which is deprecated and provisions a
      // custom-resource Lambda to set retention after the fact.
      logGroup: new logs.LogGroup(this, "LogGroup", {
        // Named where anyone would look: left to CDK it gets a generated name, and every "check the
        // connector's CloudWatch logs" instruction leads nowhere.
        logGroupName: `/aws/lambda/${functionName}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      description:
        props.description ??
        `Athena Query Federation connector "${props.connectorId}"`,
    });

    if (props.roleName !== undefined) {
      this.pinRoleName(props.roleName, functionName);
    }

    // The control that fails fast: COA's invoke policy matches this tag, so without it nothing
    // can invoke the function and the first scan is denied.
    cdk.Tags.of(this.connectorFunction).add(CONNECTOR_TAG_KEY, CONNECTOR_TAG_VALUE);

    if (this.spillBucket !== undefined && this.spillKey !== undefined) {
      // grantReadWrite on a KMS bucket also grants the key access this role needs to write.
      this.spillBucket.grantReadWrite(this.connectorFunction, `${spillPrefix}/*`);
      // Named explicitly too: it is the grant COA's contract calls for, and a reader will look for
      // it rather than infer it from grantReadWrite's side effects.
      this.spillKey.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: "ConnectorGenerateSpillDataKey",
          principals: [this.connectorFunction.grantPrincipal],
          actions: ["kms:GenerateDataKey"],
          resources: ["*"],
        }),
      );
      // The SDK's SpillLocationVerifier calls HeadBucket before returning splits, which needs
      // s3:ListBucket — a bucket-level call grantReadWrite's object pattern does not always cover.
      this.connectorFunction.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "SpillBucketLocate",
          actions: ["s3:GetBucketLocation", "s3:ListBucket"],
          resources: [this.spillBucket.bucketArn],
        }),
      );
    }

    this.grantQueryAccess(props.queryRoleArns ?? [], spillPrefix);

    if (props.alarmTopicArn !== undefined) {
      this.alarmTopic = sns.Topic.fromTopicArn(this, "AlarmTopic", props.alarmTopicArn);
    }
    if ((props.alarms ?? Provisioning.CREATE) === Provisioning.CREATE) {
      this.createLambdaAlarms(props.timeout ?? DEFAULT_TIMEOUT);
    }

    new cdk.CfnOutput(this, "ConnectorFunctionArn", {
      value: this.connectorFunction.functionArn,
      description:
        "Register this ARN as an Athena LAMBDA data catalog in the querying account",
    });
    new cdk.CfnOutput(this, "SpillBucketName", {
      value:
        this.spillBucket?.bucketName ??
        "<none: a response over 6 MB has been observed returning SUCCEEDED with zero rows>",
      description: "spill_bucket the connector writes large responses to",
    });
  }

  /**
   * Renames the role this construct's Lambda L2 created. The L2 only accepts a whole role through
   * `role`, and a role passed that way gets none of the managed policies the L2 attaches to its own.
   */
  private httpsOnlySecurityGroup(vpc: ec2.IVpc): ec2.SecurityGroup {
    const group = new ec2.SecurityGroup(this, "SecurityGroup", {
      vpc,
      description: "Athena federation connector - HTTPS egress only",
      allowAllOutbound: false,
    });
    group.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS: the source and the AWS APIs the connector calls",
    );
    return group;
  }

  private pinRoleName(roleName: string, functionName: string): void {
    const cfnRole = this.connectorFunction.role?.node.defaultChild;
    if (!(cfnRole instanceof iam.CfnRole)) {
      throw new Error(
        `Cannot pin the execution role name of "${functionName}" to "${roleName}": its role was not ` +
          `created by this construct. Drop roleName, or name the role you passed in.`,
      );
    }
    cfnRole.roleName = roleName;
  }

  /**
   * One of the connector's own EMF metrics, dimensioned to this connector.
   *
   * Use this rather than a hand-built {@link cloudwatch.Metric}: the namespace and the dimension
   * name have to match what the jar emits, and a mismatch leaves the alarm in `INSUFFICIENT_DATA`
   * for ever rather than failing.
   *
   * @param catalog restricts the metric to one Athena catalog. Omit for the fleet-wide view, the
   *                only one available for metrics emitted below the request.
   */
  public connectorMetric(
    metricName: string,
    props: cloudwatch.MetricOptions & { readonly catalog?: string } = {},
  ): cloudwatch.Metric {
    const { catalog, ...metricOptions } = props;
    return new cloudwatch.Metric({
      namespace: CONNECTOR_METRIC_NAMESPACE,
      metricName,
      dimensionsMap: {
        [CONNECTOR_DIMENSION]: this.connectorId,
        ...(catalog !== undefined ? { Catalog: catalog } : {}),
      },
      ...metricOptions,
    });
  }

  /**
   * Adds an alarm, wired to {@link alarmTopic} if there is one and recorded in {@link alarms}.
   *
   * The topic is held by this construct, so a connector's own stack cannot add an alarm that looks
   * configured and pages nobody.
   */
  public addAlarm(id: string, props: cloudwatch.AlarmProps): cloudwatch.Alarm {
    const alarm = new cloudwatch.Alarm(this, id, props);
    if (this.alarmTopic !== undefined) {
      alarm.addAlarmAction(new cloudwatchActions.SnsAction(this.alarmTopic));
    }
    this.alarms.push(alarm);
    return alarm;
  }

  /**
   * The three alarms that apply to any connector, whatever it talks to.
   *
   * Thresholds are deliberately not tunable: each is anchored to something structural — zero, one
   * percent, or a fraction of this function's own timeout — rather than to a workload.
   */
  private createLambdaAlarms(timeout: cdk.Duration): void {
    const period = cdk.Duration.minutes(5);

    // Any throttle at all. One connector serves every source pointed at it, so this is not one
    // source degrading — it is all of them, simultaneously, and COA sees only failed queries.
    this.addAlarm("ThrottlesAlarm", {
      alarmName: `${this.functionName}-throttles`,
      alarmDescription:
        "The connector is being throttled. Every COA source pointed at it is affected at once; " +
        "raise reserved concurrency.",
      metric: this.connectorFunction.metricThrottles({ period, statistic: "Sum" }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // A rate, not a count: a connector serving a busy schema and one serving a quiet one cannot share
    // an absolute threshold, and the quiet one is where a count set for the busy one never fires.
    this.addAlarm("ErrorRateAlarm", {
      alarmName: `${this.functionName}-error-rate`,
      alarmDescription:
        "Over 1% of connector invocations failed. Check the connector's log group for the " +
        "classified error prefix before looking at COA.",
      metric: new cloudwatch.MathExpression({
        expression: "100 * errors / invocations",
        usingMetrics: {
          errors: this.connectorFunction.metricErrors({ period, statistic: "Sum" }),
          invocations: this.connectorFunction.metricInvocations({ period, statistic: "Sum" }),
        },
        label: "Error rate (%)",
        period,
      }),
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      // An idle connector divides by zero, which yields no data rather than an error.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Against this function's own timeout rather than a fixed number of seconds, because a timed-out
    // invocation is the worst diagnosis available: it names no table, suggests no action, and Athena
    // retries it. Capped, so a timeout sized for a warehouse resume does not push the threshold past
    // the point where an alert is still worth acting on.
    const warningMillis = Math.min(
      Math.round(timeout.toMilliseconds() * 0.8),
      MAX_DURATION_ALARM_THRESHOLD.toMilliseconds(),
    );
    this.addAlarm("DurationAlarm", {
      alarmName: `${this.functionName}-duration-p99`,
      alarmDescription:
        `p99 invocation duration is over ${Math.round(warningMillis / 1000)}s, against a ` +
        `${timeout.toSeconds()}s timeout. Past the timeout, queries fail as timeouts that name ` +
        "nothing and are retried at full cost.",
      metric: this.connectorFunction.metricDuration({ period, statistic: "p99" }),
      threshold: warningMillis,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }

  /**
   * Lets COA's querying principals reach this connector. Three resource policies per principal,
   * because a cross-account principal needs an allow from both sides and this stack owns one: the
   * Lambda's, so Athena can invoke as that principal; the bucket's `s3:GetObject` under the spill
   * prefix, since spill objects are read with the *querying* role's credentials; and the key's
   * `kms:Decrypt`, since the bucket is SSE-KMS.
   *
   * The key condition is `kms:ViaService`, because under bucket-level SSE-KMS the immediate KMS
   * caller is S3 and `aws:CalledVia` would depend on undocumented behaviour. The statements are
   * written out by hand because `bucket.grantRead()` on a KMS bucket also calls `grantDecrypt()`,
   * adding an **unconditioned** `kms:Decrypt` that defeats the condition.
   */
  private grantQueryAccess(
    queryRoleArns: readonly string[],
    spillPrefix: string,
  ): void {
    const viaS3 = `s3.${cdk.Stack.of(this).region}.amazonaws.com`;

    // Deduped (grantInvoke's construct id derives from the principal) and sorted (the Sid indexes
    // below must not depend on the order the ARNs arrived in).
    const unique = [...new Set(queryRoleArns)].sort();

    unique.forEach((roleArn, index) => {
      const principal = new iam.ArnPrincipal(roleArn);
      this.connectorFunction.grantInvoke(principal);

      const spillBucket = this.spillBucket;
      const spillKey = this.spillKey;
      if (spillBucket === undefined || spillKey === undefined) {
        // Nothing to read: with no bucket, a response that would have spilled never becomes an
        // object anyone could fetch.
        return;
      }

      spillBucket.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: `CoaSpillRead${index}`,
          principals: [principal],
          actions: ["s3:GetObject"],
          resources: [spillBucket.arnForObjects(`${spillPrefix}/*`)],
        }),
      );

      spillKey.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: `CoaSpillDecrypt${index}`,
          principals: [principal],
          actions: ["kms:Decrypt"],
          // A key policy's resource is the key it is attached to; "*" means exactly that.
          resources: ["*"],
          conditions: { StringEquals: { "kms:ViaService": viaS3 } },
        }),
      );
    });
  }
}
