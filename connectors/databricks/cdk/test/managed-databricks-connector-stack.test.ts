// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// COA's own deployment: one Lambda serving every Databricks source in one COA environment. Every name
// and path it uses is derived from two scalars, COA_PREFIX and COA_ENV_NAME, so most of what this
// suite asserts is that the derivation produces the values the sources API, the jar and a customer's
// trust policy were told to expect.
import { Match, Template } from "aws-cdk-lib/assertions";
import { CONNECTOR_TAG_KEY } from "coa-connector-cdk";
import {
  CONFIG_SOURCE_ENV_VAR,
  CONNECTOR_ROLE_NAME_SUFFIX,
  MANAGED_MODE,
} from "../lib/constants";
import { MANAGED_METRIC_NAMES } from "../lib/alarms";
import { MANAGED_ENV_VARS, REQUIRED_MANAGED_ENV_VARS } from "../lib/managed-deployment";
import { CONNECTOR_ENV_VARS } from "../lib/single-target";
import { RESERVED_DATASOURCE_ROLE_SEGMENT } from "../lib/reserved-role-prefix";
import {
  COA_SUBNET_IDS,
  COA_VPC_ID,
  CONFIG_SSM_PREFIX,
  DISCOVERY_ROLE,
  ENVIRONMENT_ALARM_COUNT,
  ENVIRONMENT_CONNECTOR_ALARM_COUNT,
  FUNCTION_ARN_PARAMETER,
  MANAGED_FUNCTION_NAME,
  MANAGED_ONLY_ALARM_COUNT,
  RESOURCE_PREFIX,
  ROLE_ARN_PARAMETER,
  SERVE_ROLE,
  actionsOf,
  alarmsIn,
  connectorAlarmsIn,
  connectorEnvironment,
  externalKmsResources,
  managedEnvFor,
  managedStack,
  policyStatements,
  statementWithSid,
  synth,
  synthManaged,
  synthManagedFor,
  useConnectorEnv,
} from "./helpers";

useConnectorEnv();

describe("the two inputs", () => {
  it.each([...REQUIRED_MANAGED_ENV_VARS])("fails synth naming %s when it is missing", (name) => {
    managedEnvFor("coa", "dev");
    delete process.env[name];
    expect(() => managedStack()).toThrow(new RegExp(name));
  });

  it("names BOTH missing inputs in one message", () => {
    // Two requiredEnv calls would exit on the first, costing a fat-jar build per round trip.
    for (const name of REQUIRED_MANAGED_ENV_VARS) {
      delete process.env[name];
    }
    for (const name of REQUIRED_MANAGED_ENV_VARS) {
      expect(() => managedStack()).toThrow(new RegExp(name));
    }
  });

  it("derives every path and name from the pair, environment segment included", () => {
    // The environment segment is what stops a dev registration resolving prod's connector ARN and
    // creating a catalog pointing at prod's connector. Derived rather than supplied, so it cannot be
    // left out and no two of these values can name different environments.
    const template = synthManagedFor("coa", "staging");
    const variables = connectorEnvironment(template);

    expect(variables.COA_CONFIG_SSM_PREFIX).toBe("/coa/staging/connectors/databricks/sources");
    expect(variables.COA_DEPLOYMENT_ID).toBe("coa-staging");
    expect(variables.COA_RESOURCE_PREFIX).toBe("coa-staging-");
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/staging/connectors/databricks/deployment/function-arn",
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/staging/connectors/databricks/deployment/role-arn",
    });
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "coa-staging-managed-databricks-coa-connector",
    });
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `coa-staging-${CONNECTOR_ROLE_NAME_SUFFIX}`,
    });
  });

  it("refuses a pair that cannot name a Lambda and a stack", () => {
    // The toolkit checks the composed name, which is the only place an underscore or a leading digit
    // in either input can be caught before the jar is built.
    expect(() => synthManagedFor("coa", "dev_2")).toThrow(/no underscores/);
  });
});

describe("the function's environment", () => {
  it("sets the mode and carries NONE of the single-target keys", () => {
    // The suite's beforeEach leaves the stage-1 variables set, which is the mistake this shape has to
    // survive: a sourced stage-1 .env. This stack reads none of them, so a managed function cannot end
    // up pinned to one workspace and one credential and serve those to every namespace's catalog.
    process.env.CREDENTIAL_KMS_KEY_ARN =
      "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000";
    const variables = connectorEnvironment(synthManaged());

    expect(variables[CONFIG_SOURCE_ENV_VAR]).toBe(MANAGED_MODE);
    for (const name of CONNECTOR_ENV_VARS) {
      expect(name in variables).toBe(false);
    }
    expect("CREDENTIAL_KMS_KEY_ARN" in variables).toBe(false);
  });

  it("passes all three variables the jar refuses to start without", () => {
    // ConnectionConfigProviders.MANAGED_VARS requires all three, so a stack setting two would synth
    // cleanly and produce a function that never initialises.
    const variables = connectorEnvironment(synthManaged());
    expect(variables.COA_CONFIG_SSM_PREFIX).toBe(CONFIG_SSM_PREFIX);
    expect(variables.COA_RESOURCE_PREFIX).toBe(RESOURCE_PREFIX);
    for (const name of MANAGED_ENV_VARS) {
      expect(variables[name]).toBeDefined();
    }
  });

  it("derives the deployment id the sources API writes into every parameter", () => {
    // The prefix with no trailing hyphen, the same rule the write side uses: the connector refuses a
    // parameter whose deploymentId does not match its own.
    expect(connectorEnvironment(synthManaged()).COA_DEPLOYMENT_ID).toBe("coa-dev");
  });

  it("still carries the fleet-wide row ceiling", () => {
    // It names no workspace, so it survives the mode change and applies to every source.
    process.env.DATABRICKS_MAX_ROWS_PER_TABLE = "50000";
    expect(connectorEnvironment(synthManaged()).DATABRICKS_MAX_ROWS_PER_TABLE).toBe("50000");
  });
});

describe("the connector role", () => {
  it("reads parameters under the sources subtree and nowhere else", () => {
    const statement = statementWithSid(synthManaged(), "ReadDatabricksSourceParameters");

    expect(actionsOf(statement)).toEqual(["ssm:GetParameter"]);
    // No separator before the prefix: an SSM parameter ARN is `parameter` immediately followed by a
    // name that already starts with "/", and `parameter//coa/...` matches nothing.
    expect(statement.Resource).toBe(
      `arn:aws:ssm:us-east-1:123456789012:parameter${CONFIG_SSM_PREFIX}/*`,
    );
  });

  it("cannot reach the deployment subtree holding its own published ARNs", () => {
    // Matched as a pattern rather than eyeballed, because a prefix one segment shorter still produces
    // a plausible-looking ARN. The role ARN matters most: a customer's trust policy names it, so a
    // connector able to rewrite it could redirect every future credential owner.
    const statement = statementWithSid(synthManaged(), "ReadDatabricksSourceParameters");
    const granted = String(statement.Resource);
    const pattern = new RegExp(
      `^${granted.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`,
    );

    expect(
      pattern.test(`arn:aws:ssm:us-east-1:123456789012:parameter${CONFIG_SSM_PREFIX}/coadevds_x`),
    ).toBe(true);
    for (const published of [FUNCTION_ARN_PARAMETER, ROLE_ARN_PARAMETER]) {
      expect(pattern.test(`arn:aws:ssm:us-east-1:123456789012:parameter${published}`)).toBe(false);
    }
  });

  it("holds no SSM action beyond GetParameter, so it cannot write what CloudFormation published", () => {
    // The write on both `deployment/` parameters belongs to the deploy role. An ssm:PutParameter here
    // — even scoped to `sources/` — would let the connector rewrite the configuration it is about to
    // read.
    const ssmActions = policyStatements(synthManaged())
      .flatMap((statement) => actionsOf(statement))
      .filter((action) => action.startsWith("ssm:"));

    expect(ssmActions).toEqual(["ssm:GetParameter"]);
  });

  it("assumes only the reserved datasource-access prefix, with no account restriction", () => {
    // Registration validates a source's role ARN against this same scope; the two agreeing is what
    // stops COA accepting a source its connector then cannot read.
    const statement = statementWithSid(synthManaged(), "AssumeRoleCoaManaged");

    expect(actionsOf(statement)).toEqual(["sts:AssumeRole"]);
    // Account-agnostic on purpose: COA is deployed into the customer's account, so a rule excluding
    // the deployment account would refuse the most common topology outright.
    expect(statement.Resource).toBe(`arn:aws:iam::*:role/${RESOURCE_PREFIX}datasource-access-*`);
    expect(String(statement.Resource)).toContain("iam::*:");
    // Null on sts:ExternalId, so an assume presenting none fails closed at IAM.
    expect(statement.Condition).toEqual({ Null: { "sts:ExternalId": "false" } });
    // No account condition of any kind, on either side of the assume.
    const conditionKeys = Object.values(statement.Condition ?? {}).flatMap((operands) =>
      Object.keys(operands),
    );
    for (const key of conditionKeys) {
      expect(key.toLowerCase()).not.toContain("account");
    }
  });

  it("holds NO secretsmanager action at all", () => {
    // The credential is read as an assumed session behind a role COA does not own, so the connector's
    // reach at any instant is one session scoped by a policy COA did not write. That is what makes one
    // shared connector acceptable, and it holds only while this is empty.
    const template = synthManaged();
    const withSecrets = policyStatements(template).filter((statement) =>
      actionsOf(statement).some((action) => action.startsWith("secretsmanager:")),
    );

    expect(withSecrets).toEqual([]);
    // And nowhere else in the template, so no resource policy or environment variable reintroduces one
    // either.
    expect(JSON.stringify(template.toJSON())).not.toContain("secretsmanager");
  });

  it("holds no KMS action beyond its own spill key", () => {
    // Nothing on any external key; what remains is the construct's own spill-key grant, inherent to
    // writing spill at all. Its Resource is an Fn::GetAtt rather than a literal ARN, which is how
    // externalKmsResources tells the two apart.
    const template = synthManaged();
    expect(externalKmsResources(template)).toEqual([]);

    const kmsStatements = policyStatements(template).filter((statement) =>
      actionsOf(statement).some((action) => action.startsWith("kms:")),
    );
    for (const statement of kmsStatements) {
      expect(typeof statement.Resource).not.toBe("string");
    }
  });

  it("grants invoke to COA's serve and discovery roles", () => {
    const template = synthManaged();
    for (const role of [SERVE_ROLE, DISCOVERY_ROLE]) {
      template.hasResourceProperties("AWS::Lambda::Permission", {
        Action: "lambda:InvokeFunction",
        Principal: role,
      });
    }
    template.resourceCountIs("AWS::Lambda::Permission", 2);
  });
});

describe("the VPC", () => {
  it("runs in COA's VPC, in the subnets the deploy script read from COA's parameters", () => {
    synthManaged().hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: MANAGED_FUNCTION_NAME,
      VpcConfig: { SubnetIds: COA_SUBNET_IDS },
    });
  });

  it("refuses to synthesise outside a VPC, naming the script that supplies it", () => {
    managedEnvFor("coa", "dev");
    delete process.env.CONNECTOR_VPC_ID;
    delete process.env.CONNECTOR_SUBNET_IDS;
    expect(() => managedStack()).toThrow(
      /CONNECTOR_VPC_ID and CONNECTOR_SUBNET_IDS are not set[\s\S]*deploy-managed-databricks-connector\.sh/,
    );
  });

  it("uses its own HTTPS-only security group, whatever CONNECTOR_SECURITY_GROUP_IDS says", () => {
    process.env.CONNECTOR_SECURITY_GROUP_IDS = "sg-0badbadbadbadbad0";
    const template = synthManaged();
    template.resourceCountIs("AWS::EC2::SecurityGroup", 1);
    template.hasResourceProperties("AWS::EC2::SecurityGroup", {
      VpcId: COA_VPC_ID,
      SecurityGroupEgress: [Match.objectLike({ FromPort: 443, ToPort: 443 })],
    });
    expect(JSON.stringify(template.toJSON())).not.toContain("sg-0badbadbadbadbad0");
  });
});

describe("the function ARN handoff", () => {
  it("publishes the ARN at the sub-type's deployment path", () => {
    // Keyed on the sub-type, not on the connector: a path carrying the function's name would make COA
    // reconstruct that name to build the lookup.
    const template = synthManaged();
    template.resourceCountIs("AWS::SSM::Parameter", 2);
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: FUNCTION_ARN_PARAMETER,
      Value: { "Fn::GetAtt": Match.arrayWith(["Arn"]) },
    });
  });

  it("names the parameter in an output, so the deploy log records the handoff", () => {
    synthManaged().hasOutput("FunctionArnParameterName", { Value: FUNCTION_ARN_PARAMETER });
  });
});

describe("the connector role handoff", () => {
  // A customer's credential-access role has to name this connector's execution role in its trust
  // policy, so the ARN is published rather than guessed from the function's configuration at
  // onboarding time — a wrong guess is AccessDenied on every query with nothing to point at.

  it("publishes the role ARN beside the function ARN, under deployment/", () => {
    synthManaged().hasResourceProperties("AWS::SSM::Parameter", {
      Name: ROLE_ARN_PARAMETER,
      // The role's Arn attribute, not the function's.
      Value: { "Fn::GetAtt": Match.arrayWith(["Arn"]) },
    });
  });

  it("sits under deployment/ rather than sources/, which the connector role can read", () => {
    // The sibling subtree exists so the connector holds no write anywhere and no read here.
    expect(ROLE_ARN_PARAMETER.startsWith("/coa/dev/connectors/databricks/deployment/")).toBe(true);
    expect(ROLE_ARN_PARAMETER).not.toContain("/sources/");
    // A dev reader resolving prod's connector role would send a data owner a principal from the wrong
    // deployment.
    expect(ROLE_ARN_PARAMETER).toContain("/dev/");
  });

  it("publishes the value in an output too, so the deploy log carries it", () => {
    // The value rather than the path: whoever ran the deploy has to send this to each source's
    // credential owner.
    synthManaged().hasOutput("ConnectorRoleArn", {
      Value: { "Fn::GetAtt": Match.arrayWith(["Arn"]) },
    });
  });
});

describe("the connector role's name is final too", () => {
  // Every Databricks source's credential-access role names this role's ARN by hand in its trust
  // policy, and CloudFormation replaces a role on a RoleName or Path change — which a construct-id
  // rename or a stack delete/recreate both cause. Left generated, the ARN would move and every trust
  // policy in the fleet would break at once, with no repair from COA's side.

  const CONNECTOR_ROLE_NAME = `${RESOURCE_PREFIX}${CONNECTOR_ROLE_NAME_SUFFIX}`;

  const namedRoles = (template: Template): Record<string, string> =>
    Object.fromEntries(
      Object.entries(template.findResources("AWS::IAM::Role"))
        .filter(([, role]) => typeof role.Properties?.RoleName === "string")
        .map(([logicalId, role]) => [logicalId, String(role.Properties.RoleName)]),
    );

  it("names the execution role explicitly", () => {
    expect(CONNECTOR_ROLE_NAME).toBe("coa-dev-databricks-connector-role");
    synthManaged().hasResourceProperties("AWS::IAM::Role", { RoleName: CONNECTOR_ROLE_NAME });
  });

  it("pins the role the function actually runs as, and the one published", () => {
    // The pin is worthless if it landed on the bucket's auto-delete-objects role instead.
    const template = synthManaged();
    const named = namedRoles(template);
    expect(Object.values(named)).toEqual([CONNECTOR_ROLE_NAME]);

    const [logicalId] = Object.keys(named);
    const connector = Object.values(template.findResources("AWS::Lambda::Function")).find(
      (fn) => fn.Properties?.FunctionName === MANAGED_FUNCTION_NAME,
    );
    expect(JSON.stringify(connector?.Properties?.Role)).toContain(logicalId);
    const parameter = Object.values(template.findResources("AWS::SSM::Parameter")).find(
      (p) => p.Properties?.Name === ROLE_ARN_PARAMETER,
    );
    expect(JSON.stringify(parameter?.Properties?.Value)).toContain(logicalId);
  });

  it("falls outside the reserved datasource-access prefix, which synth would refuse", () => {
    // Inside the prefix this stack's own AssumeRoleCoaManaged grant covers, the connector could assume
    // its own role.
    expect(
      CONNECTOR_ROLE_NAME.startsWith(`${RESOURCE_PREFIX}${RESERVED_DATASOURCE_ROLE_SEGMENT}`),
    ).toBe(false);
  });

  it("fits IAM's 64 characters at the longest prefix the platform can deploy", () => {
    // `{prefix}-{env}` is capped at 27 by infra's own longest prefixed role name, so this is the worst
    // case. Derived from the function name it would not fit: that one is already 63 here.
    const names = Object.values(namedRoles(synthManagedFor("mycompany-analytics", "production")));

    expect(names).toEqual(["mycompany-analytics-production-databricks-connector-role"]);
    expect(names[0].length).toBeLessThanOrEqual(64);
  });
});

describe("spill survives a destroy in prod", () => {
  // A `cdk destroy` against a production deployment takes the CMK and up to a day of spill with it,
  // silently, and neither comes back.

  it("retains the spill bucket and its key in a prod deployment", () => {
    const template = synthManagedFor("coa", "prod");

    template.hasResource("AWS::S3::Bucket", {
      DeletionPolicy: "Retain",
      UpdateReplacePolicy: "Retain",
    });
    template.hasResource("AWS::KMS::Key", { DeletionPolicy: "Retain" });
    // And no auto-delete custom resource, which would empty the bucket the retention exists to keep.
    expect(JSON.stringify(template.toJSON())).not.toContain("Custom::S3AutoDeleteObjects");
  });

  it("still destroys them in a non-prod deployment", () => {
    const template = synthManaged();

    template.hasResource("AWS::S3::Bucket", { DeletionPolicy: "Delete" });
    template.hasResource("AWS::KMS::Key", { DeletionPolicy: "Delete" });
    expect(JSON.stringify(template.toJSON())).toContain("Custom::S3AutoDeleteObjects");
  });
});

describe("the function's name is final", () => {
  it("derives exactly the name the managed deployment is committed to", () => {
    // athena:CreateDataCatalog stores the handler ARN and never re-resolves it, so every catalog COA
    // creates embeds this ARN and a later rename orphans all of them with no in-place repair.
    synthManaged().hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: MANAGED_FUNCTION_NAME,
    });
  });

  it("carries the reserved -managed- segment", () => {
    expect(MANAGED_FUNCTION_NAME).toContain("-managed-");
  });

  it("ignores FUNCTION_NAME_PREFIX, which a customer-deployed connector sets", () => {
    // The name is derived from the two inputs and cannot be overridden: a prefix left in the shell by
    // a stage-1 deployment must not rename COA's own connector, since every Athena catalog already
    // created embeds the old ARN.
    process.env.FUNCTION_NAME_PREFIX = "sales-";
    synthManaged().hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: MANAGED_FUNCTION_NAME,
    });
  });

  it("still tags only the connector function, on this path too", () => {
    const functions = Object.values(synthManaged().findResources("AWS::Lambda::Function"));
    expect(functions.length).toBeGreaterThan(1);
    const tagged = functions.filter((resource) => {
      const tags: { Key?: string }[] = resource.Properties?.Tags ?? [];
      return tags.some((tag) => tag.Key === CONNECTOR_TAG_KEY);
    });

    expect(tagged).toHaveLength(1);
    expect(tagged[0].Properties.FunctionName).toBe(MANAGED_FUNCTION_NAME);
  });
});

describe("outputs", () => {
  it("publishes nothing that names one endpoint", () => {
    // A managed deployment has no single catalog, schema or secret: publishing "the" catalog could only
    // name whichever value the deployer's shell happened to carry.
    const keys = Object.keys(synthManaged().findOutputs("*"));
    for (const key of ["ConnectorDatabase", "DatabricksCatalog", "CredentialSecretArn"]) {
      expect(keys).not.toContain(key);
    }
  });

  it("still publishes the function ARN and the spill pair", () => {
    const keys = Object.keys(synthManaged().findOutputs("*"));
    for (const key of [
      "ConnectorFunctionArn",
      "ConnectorRoleArn",
      "FunctionArnParameterName",
      "SpillBucket",
      "SpillKeyArn",
      "ConfigSsmPrefix",
    ]) {
      expect(keys).toContain(key);
    }
  });
});

describe("alarms", () => {
  it("adds exactly the two managed-mode alarms, and nothing else", () => {
    // A measured delta, so an alarm added to both stacks leaves this green.
    const environmentAlarms = alarmsIn(synth()).length;
    const managedAlarms = alarmsIn(synthManaged()).length;

    expect(environmentAlarms).toBe(ENVIRONMENT_ALARM_COUNT);
    expect(managedAlarms).toBe(environmentAlarms + MANAGED_ONLY_ALARM_COUNT);
  });

  it.each([
    [MANAGED_METRIC_NAMES.configThrottles, "config-throttles"],
    [MANAGED_METRIC_NAMES.credentialAssumeFailures, "credential-assume-failures"],
  ])("alarms on %s at the first breach", (metricName, suffix) => {
    synthManaged().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `${MANAGED_FUNCTION_NAME}-${suffix}`,
      Namespace: "COA/Connectors",
      MetricName: metricName,
      Threshold: 0,
      ComparisonOperator: "GreaterThanThreshold",
      Dimensions: [{ Name: "Connector", Value: "databricks" }],
    });
  });

  it("names the metrics the jar emits", () => {
    // Restated from ConnectorMetrics on the Java side: a disagreement leaves the alarm in
    // INSUFFICIENT_DATA rather than failing, so a rename on one side has to be a red test.
    expect(MANAGED_METRIC_NAMES.configThrottles).toBe("ConnectorConfigThrottles");
    expect(MANAGED_METRIC_NAMES.credentialAssumeFailures).toBe("ConnectorCredentialAssumeFailures");
  });

  it("describes a configuration failure in this mode's terms", () => {
    // Here an unresolvable configuration is an absent or repointed parameter rather than a missing
    // variable, and the two need different first actions.
    synthManaged().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `${MANAGED_FUNCTION_NAME}-config-resolution-failures`,
      AlarmDescription: Match.stringLikeRegexp(".*COA_CONFIG_SSM_PREFIX.*"),
    });
  });

  it("dimensions NONE of the connector alarms on Catalog", () => {
    // A catalog dimension would make the alarm stop matching the day a second source is registered,
    // which for the assume-failure alarm is the day it is most needed.
    const connectorAlarms = connectorAlarmsIn(synthManaged());

    // Guards the loop below against passing on an empty list.
    expect(connectorAlarms).toHaveLength(
      ENVIRONMENT_CONNECTOR_ALARM_COUNT + MANAGED_ONLY_ALARM_COUNT,
    );
    for (const alarm of connectorAlarms) {
      expect(alarm.Properties?.Dimensions).toEqual([{ Name: "Connector", Value: "databricks" }]);
    }
  });

  it("notifies the alarm topic from both new alarms when one is given", () => {
    process.env.ALARM_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:coa-connector-alarms";
    const alarms = alarmsIn(synthManaged());

    // The assertion is that EVERY alarm carries the action, so the loop has to be known to have
    // covered the managed-only two as well.
    expect(alarms).toHaveLength(ENVIRONMENT_ALARM_COUNT + MANAGED_ONLY_ALARM_COUNT);
    for (const alarm of alarms) {
      expect(alarm.Properties?.AlarmActions).toEqual([
        "arn:aws:sns:us-east-1:123456789012:coa-connector-alarms",
      ]);
    }
  });
});
