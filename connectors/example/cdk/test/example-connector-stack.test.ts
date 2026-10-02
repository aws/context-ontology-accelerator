// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { Provisioning } from "coa-connector-cdk";
import {
  CONNECTOR_DATABASE,
  CONNECTOR_TABLES,
  DEFAULT_JAR_PATH,
  HANDLER,
  ExampleConnectorStack,
  ExampleConnectorStackProps,
} from "../lib/example-connector-stack";

// A stand-in for the fat JAR, so the tests do not require `mvn package` to have run.
// CDK stages a `.jar` as an archive asset without inspecting its contents.
const FAKE_JAR = path.join(__dirname, "..", "cdk.out", "test-fixture.jar");

const SERVE_ROLE = "arn:aws:iam::999988887777:role/scl-dev-serve-role";

const TOUCHED = ["EXAMPLE_BULK_ROWS", "EXAMPLE_BULK_ROW_BYTES", "FUNCTION_NAME_PREFIX"];
let saved: Record<string, string | undefined>;

beforeAll(() => {
  fs.mkdirSync(path.dirname(FAKE_JAR), { recursive: true });
  fs.writeFileSync(FAKE_JAR, "not really a jar");
});

beforeEach(() => {
  saved = {};
  for (const name of TOUCHED) {
    saved[name] = process.env[name];
    delete process.env[name];
  }
});

afterEach(() => {
  for (const name of TOUCHED) {
    if (saved[name] === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = saved[name];
    }
  }
});

function synth(props: Partial<ExampleConnectorStackProps> = {}): Template {
  const app = new cdk.App();
  const stack = new ExampleConnectorStack(app, "example-coa-connector", {
    env: { account: "123456789012", region: "eu-central-1" },
    jarPath: FAKE_JAR,
    queryRoleArns: [SERVE_ROLE],
    ...props,
  });
  return Template.fromStack(stack);
}

describe("the stack a customer copies", () => {
  it("deploys the connector Lambda with this connector's handler", () => {
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Handler: HANDLER,
      FunctionName: "example-coa-connector",
    });
  });

  it("points at the jar the Maven build actually produces", () => {
    // Read out of the poms, not restated: comparing DEFAULT_JAR_PATH against a literal copied
    // FROM it asserted nothing about Maven, so bumping <version> or renaming <artifactId> stayed
    // green here and failed at deploy, after deploy-dev, holding the dev lock.
    const pom = fs.readFileSync(
      path.join(__dirname, "..", "..", "pom.xml"),
      "utf8",
    );
    // The <parent> block declares the inherited version AND a different artifactId, so read the
    // version from it and take this module's own artifactId from what follows.
    const parent = /<parent>([\s\S]*?)<\/parent>/.exec(pom)?.[1] ?? "";
    const version = /<version>([^<]+)<\/version>/.exec(parent)?.[1];
    const artifactId = /<artifactId>([^<]+)<\/artifactId>/.exec(
      pom.slice(pom.indexOf("</parent>")),
    )?.[1];

    expect(artifactId).toBe("example-connector");
    expect(version).toBeDefined();
    expect(DEFAULT_JAR_PATH).toBe(
      path.join(__dirname, "..", "..", "target", `${artifactId}-${version}.jar`),
    );
  });

  it("grants every COA role that reaches it — serve AND discovery", () => {
    // Two roles, because two COA components call Athena: serve runs the queries, discovery runs
    // DESCRIBE, which is the only way the @pk/@fk tags are ever read. One grant is not enough.
    const discoveryRole = "arn:aws:iam::999988887777:role/scl-dev-sources-role";
    const template = synth({ queryRoleArns: [SERVE_ROLE, discoveryRole] });
    for (const role of [SERVE_ROLE, discoveryRole]) {
      template.hasResourceProperties("AWS::Lambda::Permission", {
        Action: "lambda:InvokeFunction",
        Principal: role,
      });
    }
    template.resourceCountIs("AWS::Lambda::Permission", 2);
  });

  it("gets its own spill bucket", () => {
    synth().resourceCountIs("AWS::S3::Bucket", 1);
  });

  it("registers no Athena data catalog: that belongs to the querying account", () => {
    synth().resourceCountIs("AWS::Athena::DataCatalog", 0);
  });
});

describe("bulk_rows sizing from the environment", () => {
  it("sets nothing by default, so the connector's small defaults stand", () => {
    const template = synth();
    const functions = template.findResources("AWS::Lambda::Function");
    const variables = Object.values(functions)
      .map((resource) => resource.Properties?.Environment?.Variables)
      .find((vars) => vars?.spill_prefix !== undefined);
    expect(variables?.example_bulk_rows).toBeUndefined();
    expect(variables?.example_bulk_row_bytes).toBeUndefined();
  });

  it("passes an over-6MB sizing through, so spill can be exercised without a code change", () => {
    process.env.EXAMPLE_BULK_ROWS = "4096";
    process.env.EXAMPLE_BULK_ROW_BYTES = "2048";
    synth().hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: Match.objectLike({
          example_bulk_rows: "4096",
          example_bulk_row_bytes: "2048",
        }),
      },
    });
    // 4096 x 2048 = 8 MiB in one split, comfortably over Athena's 6 MB response limit.
    expect(4096 * 2048).toBeGreaterThan(6 * 1024 * 1024);
  });
});

describe("outputs the integration tests read", () => {
  // Keys, not values: a test resolves them by name through describe_stacks, so a renamed key is
  // indistinguishable from a stack that was never deployed — it finds nothing and skips while
  // looking healthy. Declared at stack level precisely so the key is the logical id verbatim;
  // the construct's own outputs come back hash-suffixed, e.g. ConnectorFunctionArn7CB8EEBF.
  const EXPECTED = [
    "ConnectorFunctionArn",
    "ConnectorDatabase",
    "ConnectorTables",
    "SpillBucket",
    "SpillKeyArn",
  ];

  it("publishes every key by its exact name", () => {
    const outputs = synth().findOutputs("*");
    for (const key of EXPECTED) {
      expect(Object.keys(outputs)).toContain(key);
    }
  });

  it("exports none of them", () => {
    // An Export would create a deletion dependency between this stack and any importer, which is
    // wrong for an artifact standing in for a separate customer account.
    const outputs = synth().findOutputs("*");
    for (const [key, output] of Object.entries(outputs)) {
      expect(output.Export).toBeUndefined();
    }
    expect(Object.keys(outputs).length).toBeGreaterThan(0);
  });

  it("names the database and tables the Java declares", () => {
    // Restated across languages with nothing enforcing it, so at least pin what is published.
    expect(CONNECTOR_DATABASE).toBe("example_source");
    synth().hasOutput("ConnectorDatabase", { Value: "example_source" });
    synth().hasOutput("ConnectorTables", {
      Value: "customers,orders,order_lines,shipment_lines,bulk_rows",
    });
    expect(CONNECTOR_TABLES).toHaveLength(5);
  });

  it("reports spill resources as absent rather than omitting them under Provisioning.NONE", () => {
    const template = synth({ spill: Provisioning.NONE });
    template.hasOutput("SpillBucket", { Value: "<none>" });
    template.hasOutput("SpillKeyArn", { Value: "<none>" });
  });
});
