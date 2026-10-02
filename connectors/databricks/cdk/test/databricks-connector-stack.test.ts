// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// The customer-deployed connector: one workspace, one warehouse, one catalog, one credential. A
// stage-1 customer already deployed this shape and it must keep synthesising the same stack from the
// same variables, so this suite passing unchanged IS the compatibility assertion. COA's own
// deployment is covered by managed-databricks-connector-stack.test.ts.
import * as fs from "fs";
import * as path from "path";
import { Match } from "aws-cdk-lib/assertions";
import {
  CONNECTOR_SPILL_KMS_TAG_KEY,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
} from "coa-connector-cdk";
import {
  CONFIG_SOURCE_ENV_VAR,
  DEFAULT_JAR_PATH,
  HANDLER,
  MANAGED_FUNCTION_NAME_SEGMENT,
  MEMORY_SIZE_MB,
  TIMEOUT_SECONDS,
} from "../lib/constants";
import {
  OPTIONAL_CONNECTOR_ENV_VARS,
  REQUIRED_CONNECTOR_ENV_VARS,
  UNPINNED_DATABASE_OUTPUT,
} from "../lib/single-target";
import {
  DISCOVERY_ROLE,
  ENVIRONMENT_ALARM_COUNT,
  ENVIRONMENT_CONNECTOR_ALARM_COUNT,
  RESOURCE_PREFIX,
  SECRET_ARN,
  SERVE_ROLE,
  alarmsIn,
  connectorAlarmsIn,
  connectorEnvironment,
  externalKmsResources,
  synth,
  useConnectorEnv,
} from "./helpers";

useConnectorEnv();

describe("the connector Lambda", () => {
  it("deploys with this connector's handler under a name that cannot collide", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Handler: HANDLER,
      FunctionName: "databricks-coa-connector",
      Runtime: "java21",
    });
  });

  it("is sized 3008 MB and 600 s, above the construct's memory default", () => {
    // The construct defaults to 1024 MB. Federation cannot express aggregation, so a GROUP BY reads
    // every predicate-matching row out of the warehouse through this function.
    synth().hasResourceProperties("AWS::Lambda::Function", {
      MemorySize: MEMORY_SIZE_MB,
      Timeout: TIMEOUT_SECONDS,
    });
    expect(MEMORY_SIZE_MB).toBe(3008);
    expect(TIMEOUT_SECONDS).toBe(600);
  });

  it("carries the --add-opens flag Arrow needs on Java 17 and later", () => {
    // Without it, metadata calls succeed and every read fails with "Failed to initialize MemoryUtil". The
    // construct sets it; this asserts that setting the connector's own variables did not displace it.
    expect(connectorEnvironment(synth()).JAVA_TOOL_OPTIONS).toBe(
      "--add-opens=java.base/java.nio=ALL-UNNAMED",
    );
  });

  it("carries the coa:connector tag COA's invoke policy matches", () => {
    // Without the tag nothing can invoke the function and the first scan is denied. COA scopes
    // invoke on the tag, not on the name.
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Tags: Match.arrayWith([{ Key: CONNECTOR_TAG_KEY, Value: CONNECTOR_TAG_VALUE }]),
    });
  });

  it("is the ONLY Lambda in the template carrying that tag", () => {
    // `Tags.of(scope)` applies to every taggable child, so a tag applied one level too high makes another
    // function Athena-invocable. This stack has a second function, the bucket's auto-delete-objects custom
    // resource, so the assertion is not vacuous.
    const functions = Object.entries(synth().findResources("AWS::Lambda::Function"));
    expect(functions.length).toBeGreaterThan(1);
    const tagged = functions.filter(([, resource]) =>
      ((resource.Properties?.Tags ?? []) as { Key?: string }[]).some(
        (tag) => tag.Key === CONNECTOR_TAG_KEY,
      ),
    );
    expect(tagged.map(([logicalId]) => logicalId)).toHaveLength(1);
  });

  it("points at the jar the Maven build actually produces", () => {
    // Read out of the pom rather than restated: comparing DEFAULT_JAR_PATH against a literal copied FROM
    // it asserts nothing about Maven, so bumping <version> or renaming <artifactId> stays green and fails
    // at deploy.
    const pom = fs.readFileSync(path.join(__dirname, "..", "..", "pom.xml"), "utf8");
    const parent = /<parent>([\s\S]*?)<\/parent>/.exec(pom)?.[1] ?? "";
    const version = /<version>([^<]+)<\/version>/.exec(parent)?.[1];
    const artifactId = /<artifactId>([^<]+)<\/artifactId>/.exec(
      pom.slice(pom.indexOf("</parent>")),
    )?.[1];

    expect(artifactId).toBe("databricks-connector");
    expect(version).toBeDefined();
    expect(DEFAULT_JAR_PATH).toBe(
      path.join(__dirname, "..", "..", "target", `${artifactId}-${version}.jar`),
    );
  });
});

describe("the connector's environment", () => {
  it("lower-cases the catalog and schema, as the connector does", () => {
    // ConnectionConfig folds the case and readerFor rejects anything else, so DATABRICKS_SCHEMA=Sales
    // deploys cleanly and every query gets `Unknown schema: "Sales". This connector serves only "sales"`.
    process.env.DATABRICKS_CATALOG = "MainCatalog";
    process.env.DATABRICKS_SCHEMA = "Sales";
    const variables = connectorEnvironment(synth());
    expect(variables.DATABRICKS_CATALOG).toBe("maincatalog");
    expect(variables.DATABRICKS_SCHEMA).toBe("sales");
  });

  it("passes all five coordinates", () => {
    const variables = connectorEnvironment(synth());
    expect(variables.DATABRICKS_WORKSPACE_HOSTNAME).toBe("dbc-a1b2345c-d6e7.cloud.databricks.com");
    expect(variables.DATABRICKS_HTTP_PATH).toBe("/sql/1.0/warehouses/a1b234c567d8e9fa");
    expect(variables.DATABRICKS_CATALOG).toBe("workspace");
    expect(variables.DATABRICKS_SCHEMA).toBe("coa_dbx_test");
    expect(variables.CREDENTIAL_SECRET_ARN).toBe(SECRET_ARN);
  });

  it("sets no config source, so the jar's own default keeps this deployment in environment mode", () => {
    // Must stay this way: an already-deployed stage-1 stack pulling a newer jar has to behave exactly
    // as before. Only COA's own entry point sets the variable.
    expect(connectorEnvironment(synth())[CONFIG_SOURCE_ENV_VAR]).toBeUndefined();
  });

  describe("DATABRICKS_SCHEMA is optional", () => {
    it("omits the variable entirely when unset, rather than setting it empty", () => {
      // Absent, not empty. An empty Lambda environment variable reads in the console as a value someone
      // cleared by mistake, and the Java side treats it as unset anyway.
      delete process.env.DATABRICKS_SCHEMA;
      const variables = connectorEnvironment(synth());
      expect(variables.DATABRICKS_SCHEMA).toBeUndefined();
      expect("DATABRICKS_SCHEMA" in variables).toBe(false);
    });

    it("treats a blank value as unset, because a shell and CDK disagree about which it sends", () => {
      for (const blank of ["", "   "]) {
        process.env.DATABRICKS_SCHEMA = blank;
        expect(connectorEnvironment(synth()).DATABRICKS_SCHEMA).toBeUndefined();
      }
    });

    it("still requires the catalog, which cannot travel in a request", () => {
      // An Athena federated catalog has one namespace level below the registered name and this connector
      // spends it on the UC schema, so the UC catalog has to be configuration.
      delete process.env.DATABRICKS_SCHEMA;
      delete process.env.DATABRICKS_CATALOG;
      expect(() => synth()).toThrow(/DATABRICKS_CATALOG/);
    });

    it("rejects a malformed schema at synth rather than at the first query", () => {
      // Optional must not mean unvalidated. At query time a malformed pin is indistinguishable from the
      // unpinned mode: both look like "the schema I set is being ignored".
      for (const bad of ["my-schema", "main.sub", "main;x", "1sales"]) {
        process.env.DATABRICKS_SCHEMA = bad;
        expect(() => synth()).toThrow(/DATABRICKS_SCHEMA/);
      }
    });
  });

  it("never carries a credential value, only the secret's ARN", () => {
    // A Lambda environment variable is readable by anyone with lambda:GetFunctionConfiguration and lands
    // in the CloudFormation template in plain text.
    const rendered = JSON.stringify(synth().toJSON());
    for (const forbidden of ["dapi", "client_secret", "OAuth2Secret", "PWD="]) {
      expect(rendered).not.toContain(forbidden);
    }
  });

  it("sets no row ceiling by default, so the connector's own default stands", () => {
    expect(connectorEnvironment(synth()).DATABRICKS_MAX_ROWS_PER_TABLE).toBeUndefined();
  });

  it("passes a configured row ceiling through", () => {
    process.env.DATABRICKS_MAX_ROWS_PER_TABLE = "50000";
    expect(connectorEnvironment(synth()).DATABRICKS_MAX_ROWS_PER_TABLE).toBe("50000");
  });

  it("rejects a non-integer row ceiling at synth rather than at run time", () => {
    // Left to the Java side, a typo deploys untouched, the connector falls back to its default, and the
    // operator believes they changed the ceiling.
    process.env.DATABRICKS_MAX_ROWS_PER_TABLE = "2_000_000";
    expect(() => synth()).toThrow(/positive integer/);
  });

  it("fails synth with an actionable message when a required coordinate is missing", () => {
    for (const name of REQUIRED_CONNECTOR_ENV_VARS) {
      const saved = process.env[name];
      delete process.env[name];
      expect(() => synth()).toThrow(new RegExp(name));
      process.env[name] = saved;
    }
  });

  it("synths without any of the optional variables", () => {
    // The counterpart to the test above, so "optional" is asserted rather than just omitted from the
    // required list. The two lists together account for every variable the stack reads.
    for (const name of OPTIONAL_CONNECTOR_ENV_VARS) {
      delete process.env[name];
    }
    expect(() => synth()).not.toThrow();
  });
});

describe("the credential grant", () => {
  it("grants the function read on exactly the named secret", () => {
    synth().hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["secretsmanager:GetSecretValue"]),
            Resource: SECRET_ARN,
          }),
        ]),
      }),
    });
  });

  it("scopes the grant to the complete ARN, suffix included", () => {
    // fromSecretNameV2 would grant every secret whose name is a prefix of this one, because Secrets
    // Manager appends a random six-character suffix to every ARN.
    const rendered = JSON.stringify(synth().toJSON());
    expect(rendered).toContain("databricks-connector-pat-AbCdEf");
    expect(rendered).not.toContain("databricks-connector-test-pat-??????");
  });

  it("grants no kms:Decrypt on any external key when the secret uses the AWS-managed one", () => {
    // The only KMS statement should be the spill key's, whose Resource is an Fn::GetAtt on a key this
    // stack creates rather than a literal ARN.
    expect(externalKmsResources(synth())).toEqual([]);
  });

  it("grants kms:Decrypt on a customer-managed key when one is named", () => {
    // No existing grant covers this, and without it every read fails with access-denied at run time rather
    // than at deploy. The rendered Action is a bare string rather than a one-element array, because
    // grantDecrypt on an imported key adds exactly one action.
    const keyArn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000";
    process.env.CREDENTIAL_KMS_KEY_ARN = keyArn;
    synth().hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({ Action: "kms:Decrypt", Resource: keyArn }),
        ]),
      }),
    });
    expect(externalKmsResources(synth())).toEqual([keyArn]);
  });
});

describe("spill", () => {
  it("gets its own bucket and its own customer-managed key", () => {
    // Never a shared bucket: one connector's read grant would cover another's spilled rows, and spilled
    // rows are query results.
    const template = synth();
    template.resourceCountIs("AWS::S3::Bucket", 1);
    template.resourceCountIs("AWS::KMS::Key", 1);
  });

  it("tags the spill key coa:connector-spill, which COA's key policy matches", () => {
    // Fails only above 6 MB, where a misconfigured connector looks healthy. Spill is the normal case here,
    // because aggregate reads exceed 6 MB routinely.
    synth().hasResourceProperties("AWS::KMS::Key", {
      Tags: Match.arrayWith([{ Key: CONNECTOR_SPILL_KMS_TAG_KEY, Value: CONNECTOR_TAG_VALUE }]),
    });
  });

  it("spills under connectors/databricks/spills, which is what COA's read grant matches", () => {
    expect(connectorEnvironment(synth()).spill_prefix).toBe("connectors/databricks/spills");
  });

  it("encrypts each block client-side as well", () => {
    expect(connectorEnvironment(synth()).disable_spill_encryption).toBe("false");
  });

  it("destroys the bucket and key on a destroy, whatever the deployment", () => {
    // A customer-deployed connector's spill bucket belongs to whoever deployed it. Only COA's own
    // deployment retains, and only in prod.
    synth().hasResource("AWS::S3::Bucket", { DeletionPolicy: "Delete" });
  });
});

describe("COA's two roles", () => {
  it("grants invoke to serve AND discovery", () => {
    // Two roles, because two COA components call Athena: serve runs the queries, discovery runs DESCRIBE,
    // which is the only way the @pk/@fk tags are read. Grant serve alone and the connector answers SELECT
    // perfectly while no declared key reaches COA.
    const template = synth();
    for (const role of [SERVE_ROLE, DISCOVERY_ROLE]) {
      template.hasResourceProperties("AWS::Lambda::Permission", {
        Action: "lambda:InvokeFunction",
        Principal: role,
      });
    }
    template.resourceCountIs("AWS::Lambda::Permission", 2);
  });

  it("registers no Athena data catalog: that belongs to the querying account", () => {
    synth().resourceCountIs("AWS::Athena::DataCatalog", 0);
  });
});

describe("the VPC", () => {
  it("stays outside any VPC by default, as a stage-1 deployment did", () => {
    const template = synth();
    const [fn] = Object.values(template.findResources("AWS::Lambda::Function")).filter(
      (resource) => resource.Properties?.Handler === HANDLER,
    );
    expect(fn.Properties.VpcConfig).toBeUndefined();
    template.resourceCountIs("AWS::EC2::SecurityGroup", 0);
  });

  it("attaches to the customer's VPC and security groups when the variables are set", () => {
    process.env.CONNECTOR_VPC_ID = "vpc-0c1c2c3c4c5c6c7c8";
    process.env.CONNECTOR_SUBNET_IDS = "subnet-0c1c2c3c4c5c6c7c9";
    process.env.CONNECTOR_SECURITY_GROUP_IDS = "sg-0c1c2c3c4c5c6c7ca";
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Handler: HANDLER,
      VpcConfig: {
        SubnetIds: ["subnet-0c1c2c3c4c5c6c7c9"],
        SecurityGroupIds: ["sg-0c1c2c3c4c5c6c7ca"],
      },
    });
  });
});

describe("the reserved managed function-name prefix", () => {
  it("refuses a prefix ending in the reserved segment, which would take COA's stack over", () => {
    // No variable check can see this one, and it does not collide: bin/app.ts derives the STACK name
    // from the same prefix, so this UPDATES COA's managed stack in place and keeps its function ARN.
    // Every Athena catalog COA registered embeds that ARN and would keep invoking this function, now
    // in single-endpoint mode where the catalog name is ignored.
    process.env.FUNCTION_NAME_PREFIX = `${RESOURCE_PREFIX}${MANAGED_FUNCTION_NAME_SEGMENT}`;

    expect(() => synth()).toThrow(/reserved "managed-" segment/);
    // "Pick another prefix" alone reads as a naming quibble rather than a cross-namespace read.
    expect(() => synth()).toThrow(/UPDATE COA's managed connector in place/);
    expect(() => synth()).toThrow(/another's rows/);
  });

  it("allows a customer prefix that merely contains the segment elsewhere", () => {
    // Checked on the ENDING, because only a prefix ending in the reserved segment derives the same
    // function name a managed deployment does.
    process.env.FUNCTION_NAME_PREFIX = `${RESOURCE_PREFIX}managed-eu-`;
    expect(() => synth()).not.toThrow();
  });
});

describe("what only COA's own deployment has", () => {
  it("creates NO SSM parameter", () => {
    // A customer-deployed connector has no COA to hand anything to, and writing into COA's parameter
    // tree from a customer's account is not a permission it has or should ask for.
    const template = synth();
    template.resourceCountIs("AWS::SSM::Parameter", 0);
    expect(Object.keys(template.findOutputs("*"))).not.toContain("ConnectorRoleArn");
  });

  it("pins no role name", () => {
    // A customer-deployed connector's role is named in no trust policy, and pinning it would replace
    // the role in every stage-1 deployment on the next deploy for no gain.
    const named = Object.values(synth().findResources("AWS::IAM::Role")).filter(
      (role) => typeof role.Properties?.RoleName === "string",
    );
    expect(named).toEqual([]);
  });
});

describe("outputs", () => {
  const EXPECTED = [
    "ConnectorFunctionArn",
    "ConnectorDatabase",
    "DatabricksCatalog",
    "CredentialSecretArn",
    "SpillBucket",
    "SpillKeyArn",
  ];

  it("publishes every key by its exact name", () => {
    // Declared at stack level so the key is the logical id verbatim. A construct's own outputs come back
    // hash-suffixed, and a test resolving a mangled key finds nothing and skips while looking healthy.
    const outputs = synth().findOutputs("*");
    for (const key of EXPECTED) {
      expect(Object.keys(outputs)).toContain(key);
    }
  });

  it("exports none of them", () => {
    const outputs = synth().findOutputs("*");
    for (const output of Object.values(outputs)) {
      expect(output.Export).toBeUndefined();
    }
    expect(Object.keys(outputs).length).toBeGreaterThan(0);
  });

  it("names the schema the connector exposes, so a test can find it without guessing", () => {
    synth().hasOutput("ConnectorDatabase", { Value: "coa_dbx_test" });
    synth().hasOutput("DatabricksCatalog", { Value: "workspace" });
  });

  it("still publishes ConnectorDatabase when unpinned, saying so rather than naming a schema", () => {
    // Always present, so a consumer never has to handle a missing key, but not schema-shaped: an unpinned
    // connector has no single database, and a value like "all" pasted into an Athena catalog registration
    // would fail as an obscure lookup rather than an obvious mistake.
    delete process.env.DATABRICKS_SCHEMA;
    const template = synth();
    expect(Object.keys(template.findOutputs("*"))).toContain("ConnectorDatabase");
    template.hasOutput("ConnectorDatabase", { Value: UNPINNED_DATABASE_OUTPUT });
    // The catalog is still concrete — it is what SHOW DATABASES is run against.
    template.hasOutput("DatabricksCatalog", { Value: "workspace" });
  });

  it("publishes the lower-cased names, so the outputs match what the connector answers to", () => {
    // These outputs are what the README tells an operator to paste into the Athena catalog registration
    // and COA's onboarding form, and the raw casing points at a schema the connector rejects.
    process.env.DATABRICKS_CATALOG = "MainCatalog";
    process.env.DATABRICKS_SCHEMA = "Sales";
    const template = synth();
    template.hasOutput("ConnectorDatabase", { Value: "sales" });
    template.hasOutput("DatabricksCatalog", { Value: "maincatalog" });
    // And the output agrees with the environment variable rather than coinciding with it.
    expect(connectorEnvironment(template).DATABRICKS_SCHEMA).toBe("sales");
  });
});

describe("connector alarms", () => {
  const ALARM_TOPIC = "arn:aws:sns:us-east-1:123456789012:coa-connector-alarms";

  it("alarms on all four metrics the connector emits, plus the three Lambda ones", () => {
    synth().resourceCountIs("AWS::CloudWatch::Alarm", ENVIRONMENT_ALARM_COUNT);
  });

  it.each([
    ["ConnectorConfigResolutionFailures", "config-resolution-failures", 0],
    ["ConnectorWarehouseConnectFailures", "warehouse-connect-failures", 5],
    ["ConnectorTableCeilingExceeded", "table-ceiling-exceeded", 0],
  ])("alarms on %s in the namespace the jar emits into", (metricName, suffix, threshold) => {
    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: `databricks-coa-connector-${suffix}`,
      Namespace: "COA/Connectors",
      MetricName: metricName,
      Threshold: threshold,
      ComparisonOperator: "GreaterThanThreshold",
      Dimensions: [{ Name: "Connector", Value: "databricks" }],
    });
  });

  it("describes a configuration failure in this mode's terms", () => {
    // The one alarm whose cause differs between the two stacks: here it is a missing variable, there an
    // absent or repointed parameter.
    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "databricks-coa-connector-config-resolution-failures",
      AlarmDescription: Match.stringLikeRegexp(".*four required DATABRICKS_\\* variables.*"),
    });
  });

  it("thresholds rows-returned against the default ceiling when none is configured", () => {
    // 80% of 2,000,000 — the leading indicator, so it has to fire before the ceiling refuses a query.
    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "databricks-coa-connector-rows-returned-p95",
      MetricName: "ConnectorRowsReturned",
      ExtendedStatistic: "p95",
      Threshold: 1600000,
      EvaluationPeriods: 2,
    });
  });

  it("moves the rows-returned threshold with a configured ceiling", () => {
    process.env.DATABRICKS_MAX_ROWS_PER_TABLE = "50000";

    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "databricks-coa-connector-rows-returned-p95",
      Threshold: 40000,
    });
  });

  it("does not dimension on Catalog, which would stop matching once a second source is registered", () => {
    const connectorAlarms = connectorAlarmsIn(synth());

    // Guards the loop below against passing on an empty list.
    expect(connectorAlarms).toHaveLength(ENVIRONMENT_CONNECTOR_ALARM_COUNT);
    for (const alarm of connectorAlarms) {
      expect(alarm.Properties?.Dimensions).toEqual([
        { Name: "Connector", Value: "databricks" },
      ]);
    }
  });

  it("notifies nobody without ALARM_TOPIC_ARN, and every alarm with it", () => {
    for (const alarm of alarmsIn(synth())) {
      expect(alarm.Properties?.AlarmActions).toBeUndefined();
    }

    process.env.ALARM_TOPIC_ARN = ALARM_TOPIC;
    const withTopic = alarmsIn(synth());

    // Guards the loop below against passing on an empty list.
    expect(withTopic).toHaveLength(ENVIRONMENT_ALARM_COUNT);
    for (const alarm of withTopic) {
      expect(alarm.Properties?.AlarmActions).toEqual([ALARM_TOPIC]);
    }
  });

  it("names alarms with the function-name prefix, so two deployments do not collide", () => {
    process.env.FUNCTION_NAME_PREFIX = "sales-";

    synth().hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "sales-databricks-coa-connector-table-ceiling-exceeded",
    });
  });
});
