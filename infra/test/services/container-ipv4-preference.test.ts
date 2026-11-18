// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Annotations, Match } from "aws-cdk-lib/assertions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Manifest } from "aws-cdk-lib/cloud-assembly-schema";
import { resolvePreferIpv4BuildArgs } from "../../lib/context";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { McpStack } from "../../lib/stacks/services/mcp-stack";
import { OntologyStack } from "../../lib/stacks/services/ontology-stack";
import { ServeStack } from "../../lib/stacks/services/serve-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

// Python images that run in the VPC subnets (Fargate tasks and AgentCore
// runtimes). In a dual-stack subnet without IPv6 egress to AWS, an IPv6-first
// connect stalls for minutes, long enough to expire the request's SigV4
// signature (OpenSearch 403). Each image prefers IPv4 via glibc's gai.conf
// (unless built with PREFER_IPV4=false), written before USER drops root.
const VPC_PYTHON_DOCKERFILES = [
  "packages/context-manager/Dockerfile",
  "packages/ontology-engine/Dockerfile",
  "packages/sources/database/enrichment/Dockerfile",
  "packages/sources/documents/kg-build/Dockerfile",
];

describe.each(VPC_PYTHON_DOCKERFILES)("%s IPv4 preference", (file) => {
  const dockerfile = fs.readFileSync(
    path.join(__dirname, "..", "..", "..", file),
    "utf8",
  );

  test("declares PREFER_IPV4 and applies gai.conf only when true, before USER", () => {
    const arg = dockerfile.search(/^ARG PREFER_IPV4=true$/m);
    const gai = dockerfile.search(
      /^RUN if \[ "\$PREFER_IPV4" = "true" \]; then echo "precedence ::ffff:0:0\/96 100" >> \/etc\/gai\.conf; fi$/m,
    );
    const user = dockerfile.search(/^USER /m);
    expect(arg).toBeGreaterThan(-1);
    expect(gai).toBeGreaterThan(arg);
    expect(user).toBeGreaterThan(gai);
  });

  test("uses a glibc base image, since musl ignores gai.conf", () => {
    expect(dockerfile).toMatch(/^FROM .*amazonlinux:2023/m);
  });
});

describe("resolvePreferIpv4BuildArgs", () => {
  const resolve = (context: Record<string, unknown>) =>
    resolvePreferIpv4BuildArgs(new cdk.App({ context }));

  test("defaults to true when unset", () => {
    expect(resolve({})).toEqual({ PREFER_IPV4: "true" });
  });

  test.each([false, "false"])("accepts %p to turn it off", (value) => {
    expect(resolve({ prefer_ipv4: value })).toEqual({ PREFER_IPV4: "false" });
  });

  test.each([true, "true"])("accepts %p to turn it on", (value) => {
    expect(resolve({ prefer_ipv4: value })).toEqual({ PREFER_IPV4: "true" });
  });

  test.each(["yes", "1", "TRUE", ""])("throws on invalid value %p", (bad) => {
    expect(() => resolve({ prefer_ipv4: bad })).toThrow(
      /prefer_ipv4 must be true or false/,
    );
  });
});

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

// No `*_image_uri` for the four VPC images, so CDK builds each one as a Docker
// image asset. The preprocessing Lambda image stays pre-built. Staging is off
// so synth records the build args without copying the repo-root build context.
const ASSET_CONTEXT = {
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
  "aws:cdk:bundling-stacks": [],
  "aws:cdk:disable-asset-staging": true,
};

// Every image pre-built, as in the CI deploys (Kaniko builds passed in by URI).
const PREBUILT_IMAGE_KEYS = [
  "context_manager_image_uri",
  "ontology_engine_image_uri",
  "sources_db_enrichment_image_uri",
  "sources_kg_build_image_uri",
];
const PREBUILT_CONTEXT = Object.fromEntries(
  PREBUILT_IMAGE_KEYS.map((key) => [
    key,
    `123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:${key}`,
  ]),
);

/** Build the Serve, MCP, Ontology and Sources stacks with the given context. */
function buildStacks(context: Record<string, unknown>): {
  app: cdk.App;
  stacks: cdk.Stack[];
} {
  const app = new cdk.App({ context: { ...ASSET_CONTEXT, ...context } });

  const depStack = new cdk.Stack(app, "DepStack", { env: TEST_ENV });
  const vpc = new ec2.Vpc(depStack, "Vpc", { maxAzs: 2 });
  const sg = (id: string) => new ec2.SecurityGroup(depStack, id, { vpc });
  const table = (id: string) =>
    new dynamodb.Table(depStack, id, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
  const rolesTable = table("Roles");
  const resourceRoleMappingsTable = table("RRM");
  const network = new NetworkStack(app, "Network", { env: TEST_ENV });
  const storage = new StorageStack(app, "Storage", { network, env: TEST_ENV });

  const stacks = [
    new ServeStack(app, "Serve", {
      env: TEST_ENV,
      vpc,
      aossSecurityGroup: sg("AossSG"),
      neptuneSecurityGroup: sg("NeptuneSG"),
      lambdaSecurityGroup: sg("LambdaSG"),
      neptuneClusterArn:
        "arn:aws:neptune-db:us-east-1:123456789012:cluster:test-cluster/*",
      neptuneEndpoint:
        "test-cluster.cluster-abc.us-east-1.neptune.amazonaws.com",
      ontologyBucketArn: "arn:aws:s3:::coa-dev-ontology-artifacts",
      vkgEndpoint: "http://vkg.coa-dev-services.local:8080",
      rolesTable,
      resourceRoleMappingsTable,
    }),
    new McpStack(app, "Mcp", {
      env: TEST_ENV,
      vpc,
      rolesTable,
      resourceRoleMappingsTable,
    }),
    new OntologyStack(app, "Ontology", {
      env: TEST_ENV,
      network,
      serviceNamespace: network.serviceNamespace,
      neptuneClusterArn:
        "arn:aws:neptune-db:us-east-1:123456789012:cluster-abc123/*",
      neptuneClusterEndpoint:
        "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
      ontologyArtifactsBucket: cdk.aws_s3.Bucket.fromBucketName(
        network,
        "ArtifactsBucket",
        "coa-dev-ontology-artifacts-123456789012",
      ),
      smusDomainId: "dzd-test123",
      allowedOrigin: "https://test.example.com",
    }),
    new SourcesStack(app, "Sources", {
      network,
      storage,
      allowedOrigin: "https://test.example.com",
      env: TEST_ENV,
    }),
  ];
  return { app, stacks };
}

/** Synth the four stacks and return each image asset's `PREFER_IPV4` build arg. */
function preferIpv4BuildArgs(
  context: Record<string, unknown>,
): Array<[string, string | undefined, string | undefined]> {
  const { app, stacks } = buildStacks(context);
  const { directory } = app.synth();
  return stacks.flatMap((stack) => {
    const manifest = Manifest.loadAssetManifest(
      path.join(directory, `${stack.artifactId}.assets.json`),
    );
    return Object.values(manifest.dockerImages ?? {}).map(
      ({ source }): [string, string | undefined, string | undefined] => [
        stack.stackName,
        source.dockerFile,
        source.dockerBuildArgs?.PREFER_IPV4,
      ],
    );
  });
}

describe("PREFER_IPV4 build arg on the image assets", () => {
  const assets = (value: string) => [
    ["Serve", "packages/context-manager/Dockerfile", value],
    ["Mcp", "packages/context-manager/Dockerfile", value],
    ["Ontology", "packages/ontology-engine/Dockerfile", value],
    ["Sources", "packages/sources/database/enrichment/Dockerfile", value],
    ["Sources", "packages/sources/documents/kg-build/Dockerfile", value],
  ];

  test("defaults to true on every VPC Python image", () => {
    expect(preferIpv4BuildArgs({})).toEqual(assets("true"));
  });

  test("prefer_ipv4=false reaches every VPC Python image", () => {
    expect(preferIpv4BuildArgs({ prefer_ipv4: "false" })).toEqual(
      assets("false"),
    );
  });
});

describe("prefer_ipv4 with pre-built images", () => {
  test("an invalid value still fails synth", () => {
    expect(() =>
      buildStacks({ ...PREBUILT_CONTEXT, prefer_ipv4: "yes" }),
    ).toThrow(/prefer_ipv4 must be true or false/);
  });

  test("prefer_ipv4=false warns for each pre-built image", () => {
    const { stacks } = buildStacks({
      ...PREBUILT_CONTEXT,
      prefer_ipv4: "false",
    });
    const warned = stacks.flatMap((stack) =>
      Annotations.fromStack(stack)
        .findWarning(
          "*",
          Match.stringLikeRegexp("prefer_ipv4=false has no effect"),
        )
        .map(({ entry }) => /from (\w+):/.exec(String(entry.data))?.[1]),
    );
    expect(warned).toEqual([
      "context_manager_image_uri",
      "context_manager_image_uri",
      "ontology_engine_image_uri",
      "sources_db_enrichment_image_uri",
      "sources_kg_build_image_uri",
    ]);
  });

  test("the default does not warn", () => {
    const { stacks } = buildStacks(PREBUILT_CONTEXT);
    for (const stack of stacks) {
      expect(
        Annotations.fromStack(stack).findWarning(
          "*",
          Match.stringLikeRegexp("prefer_ipv4"),
        ),
      ).toEqual([]);
    }
  });
});
