// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Turning graph sources off must deploy in one `cdk deploy --all`, from any earlier
// state. The producer stacks are updated before their consumers, and CloudFormation
// refuses to drop an export that a deployed consumer still imports, so network and
// storage must export the same names with the switch on and off. While off, the only
// trace of the feature is one security group with no rules and these two exports.
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const CONTEXT = {
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_db_enrichment_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:pre",
  sources_kg_build_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg",
  "aws:cdk:bundling-stacks": [],
};

// The logical IDs and auto-export names that environments deployed from the first
// two commits of the branch already carry; they must not change.
const SG_ID = "SparqlSourceEgressSG6BE00EC2";
const SG_EXPORT =
  "ExportsOutputFnGetAttSparqlSourceEgressSG6BE00EC2GroupId1C5CD2FA";
const CLUSTER_EXPORT = "ExportsOutputRefNeptuneClusterF0D32436";

function synth(graphSources?: string) {
  const app = new cdk.App({
    context: {
      ...CONTEXT,
      ...(graphSources ? { graph_sources: graphSources } : {}),
    },
  });
  const network = new NetworkStack(app, "Network", { env: TEST_ENV });
  const storage = new StorageStack(app, "Storage", { network, env: TEST_ENV });
  const sources = new SourcesStack(app, "Sources", {
    network,
    storage,
    allowedOrigin: "https://test.example.com",
    env: TEST_ENV,
  });
  return {
    network: Template.fromStack(network).toJSON(),
    storage: Template.fromStack(storage).toJSON(),
    sources: Template.fromStack(sources).toJSON(),
  };
}

const exportNames = (template: any): string[] =>
  Object.entries(template.Outputs ?? {})
    .filter(([, o]: [string, any]) => o.Export)
    .map(([id]) => id)
    .sort();

describe("graph sources switch: exports stay put so the switch can be flipped in one deploy", () => {
  const on = synth("on");
  const off = synth();

  it.each(["network", "storage"] as const)(
    "%s exports the same names on and off",
    (stack) => {
      expect(exportNames(off[stack])).toEqual(exportNames(on[stack]));
    },
  );

  it("keeps the egress security group's logical ID and export name", () => {
    expect(off.network.Resources[SG_ID]?.Type).toBe("AWS::EC2::SecurityGroup");
    expect(on.network.Resources[SG_ID]?.Type).toBe("AWS::EC2::SecurityGroup");
    expect(exportNames(off.network)).toContain(SG_EXPORT);
    expect(exportNames(off.storage)).toContain(CLUSTER_EXPORT);
  });

  it("leaves the security group inert while off: no ingress, only CDK's disallow-all egress placeholder", () => {
    const sg = off.network.Resources[SG_ID].Properties;
    expect(sg.SecurityGroupIngress).toBeUndefined();
    expect(sg.SecurityGroupEgress).toEqual([
      {
        CidrIp: "255.255.255.255/32",
        Description: "Disallow all traffic",
        FromPort: 252,
        IpProtocol: "icmp",
        ToPort: 86,
      },
    ]);
    const rulesOnSg = Object.values(off.network.Resources).filter(
      (r: any) =>
        [
          "AWS::EC2::SecurityGroupIngress",
          "AWS::EC2::SecurityGroupEgress",
        ].includes(r.Type) && JSON.stringify(r.Properties).includes(SG_ID),
    );
    expect(rulesOnSg).toEqual([]);
  });

  it("opens 8182 egress only when on", () => {
    const egress = JSON.stringify(
      on.network.Resources[SG_ID].Properties.SecurityGroupEgress,
    );
    expect(egress).toContain('"FromPort":8182');
    expect(
      JSON.stringify(off.network.Resources[SG_ID].Properties),
    ).not.toContain("8182");
  });

  it("keeps the Neptune cluster identical on and off (no replacement)", () => {
    const cluster = (t: any) =>
      Object.values(t.Resources).filter(
        (r: any) => r.Type === "AWS::Neptune::DBCluster",
      );
    expect(cluster(off.storage)).toHaveLength(1);
    expect(cluster(off.storage)).toEqual(cluster(on.storage));
  });

  it("names the platform cluster for graph verify by its fixed identifier, not by an import", () => {
    const fns = Object.values(on.sources.Resources).filter(
      (r: any) =>
        r.Type === "AWS::Lambda::Function" &&
        r.Properties.Environment?.Variables?.PLATFORM_NEPTUNE_CLUSTER_ID,
    ) as any[];
    expect(fns).toHaveLength(1);
    const clusterId =
      fns[0].Properties.Environment.Variables.PLATFORM_NEPTUNE_CLUSTER_ID;
    const storageCluster = Object.values(on.storage.Resources).find(
      (r: any) => r.Type === "AWS::Neptune::DBCluster",
    ) as any;
    expect(clusterId).toBe(storageCluster.Properties.DBClusterIdentifier);
    expect(JSON.stringify(on.sources)).not.toContain(CLUSTER_EXPORT);
  });
});
