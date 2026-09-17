// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

function buildStacks(): { network: NetworkStack; template: Template } {
  const app = new cdk.App();
  const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
  const storage = new StorageStack(app, "TestStorage", {
    network,
    env: TEST_ENV,
  });
  return { network, template: Template.fromStack(storage) };
}

describe("StorageStack", () => {
  describe("Neptune", () => {
    it("creates a Neptune subnet group in private subnets", () => {
      const { template } = buildStacks();
      template.resourceCountIs("AWS::Neptune::DBSubnetGroup", 1);
    });

    it("creates a Neptune cluster with IAM auth and encryption enabled", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::Neptune::DBCluster", {
        IamAuthEnabled: true,
        StorageEncrypted: true,
      });
    });

    it("sets backup retention to 7 days", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::Neptune::DBCluster", {
        BackupRetentionPeriod: 7,
      });
    });

    it("deletion protection is off for non-prod environments", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::Neptune::DBCluster", {
        DeletionProtection: false,
      });
    });

    it("deletion protection is enabled for prod environment", () => {
      const app = new cdk.App();
      app.node.setContext("env", "prod");
      const network = new NetworkStack(app, "TestNetworkProd", {
        env: TEST_ENV,
      });
      const storage = new StorageStack(app, "TestStorageProd", {
        network,
        env: TEST_ENV,
      });
      const template = Template.fromStack(storage);
      template.hasResourceProperties("AWS::Neptune::DBCluster", {
        DeletionProtection: true,
      });
    });

    it("creates a Neptune instance with r8g.large", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::Neptune::DBInstance", {
        DBInstanceClass: "db.r8g.large",
      });
    });

    it("applies DESTROY removal policy to cluster and instance", () => {
      const { template } = buildStacks();
      // DESTROY removal policy adds DeletionPolicy: Delete to the resource
      template.hasResource("AWS::Neptune::DBCluster", {
        DeletionPolicy: "Delete",
      });
      template.hasResource("AWS::Neptune::DBInstance", {
        DeletionPolicy: "Delete",
      });
    });
  });

  describe("OpenSearch Serverless", () => {
    it("creates a VECTORSEARCH collection", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::OpenSearchServerless::Collection", {
        Type: "VECTORSEARCH",
      });
    });

    it("creates an encryption policy with AWS-owned key", () => {
      const { template } = buildStacks();
      template.hasResourceProperties(
        "AWS::OpenSearchServerless::SecurityPolicy",
        {
          Type: "encryption",
          Policy: Match.stringLikeRegexp('"AWSOwnedKey":true'),
        },
      );
    });

    it("creates a network policy with VPC endpoint for private access", () => {
      const { template } = buildStacks();
      template.hasResourceProperties(
        "AWS::OpenSearchServerless::SecurityPolicy",
        {
          Type: "network",
          Policy: {
            "Fn::Join": Match.arrayWith([
              Match.arrayWith([Match.stringLikeRegexp("SourceVPCEs")]),
            ]),
          },
        },
      );
    });

    it("collection depends on encryption and network policies", () => {
      const { template } = buildStacks();
      const collections = template.findResources(
        "AWS::OpenSearchServerless::Collection",
      );
      const collection = Object.values(collections)[0] as any;
      expect(collection.DependsOn).toBeDefined();
      expect(collection.DependsOn.length).toBeGreaterThanOrEqual(2);
    });

    it("does NOT create a broad admin/data-access policy on the collection", () => {
      const { template } = buildStacks();
      // Data-plane access is granted to specific workload roles in their own
      // service stacks — the storage stack must not seed a standing admin grant.
      const dataPolicies = template.findResources(
        "AWS::OpenSearchServerless::AccessPolicy",
      );
      for (const p of Object.values(dataPolicies) as any[]) {
        expect(JSON.stringify(p.Properties.Policy)).not.toContain(
          ":role/Admin",
        );
      }
    });
  });

  describe("Ontology artifacts bucket", () => {
    it("has a CORS rule allowing browser GET/HEAD/PUT (presigned upload + large-Turtle download)", () => {
      const { template } = buildStacks();
      // Without this rule the browser blocks the presigned request with a
      // NetworkError, even though server-side access succeeds. GET/HEAD are
      // needed to fetch large proposal Turtle directly from S3 (too big to
      // inline through the 6 MB API Gateway / Lambda response limit).
      template.hasResourceProperties("AWS::S3::Bucket", {
        BucketName: Match.stringLikeRegexp("ontology-artifacts-"),
        CorsConfiguration: {
          CorsRules: Match.arrayWith([
            Match.objectLike({
              AllowedMethods: ["GET", "HEAD", "PUT"],
              AllowedHeaders: ["*"],
              ExposedHeaders: ["ETag", "Content-Length", "Content-Type"],
              AllowedOrigins: ["*"],
            }),
          ]),
        },
      });
    });

    it("uses the provided allowedOrigin in the CORS rule when set", () => {
      const app = new cdk.App();
      const network = new NetworkStack(app, "TestNetwork2", { env: TEST_ENV });
      const storage = new StorageStack(app, "TestStorage2", {
        network,
        env: TEST_ENV,
        allowedOrigin: "https://example.cloudfront.net",
      });
      Template.fromStack(storage).hasResourceProperties("AWS::S3::Bucket", {
        BucketName: Match.stringLikeRegexp("ontology-artifacts-"),
        CorsConfiguration: {
          CorsRules: Match.arrayWith([
            Match.objectLike({
              AllowedOrigins: ["https://example.cloudfront.net"],
            }),
          ]),
        },
      });
    });
  });

  describe("S3 access logging", () => {
    it("provisions a dedicated access-logs target bucket", () => {
      const { template } = buildStacks();
      template.hasResourceProperties("AWS::S3::Bucket", {
        BucketName: Match.stringLikeRegexp("storage-logs-"),
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({ ExpirationInDays: 90, Status: "Enabled" }),
          ]),
        },
      });
    });

    it("enables server access logging on each data bucket with a distinct prefix", () => {
      const { template } = buildStacks();
      for (const [namePattern, prefix] of [
        ["athena-results-", "athena-results/"],
        ["athena-spill-", "athena-spill/"],
        ["ontology-artifacts-", "ontology-artifacts/"],
      ] as const) {
        template.hasResourceProperties("AWS::S3::Bucket", {
          BucketName: Match.stringLikeRegexp(namePattern),
          LoggingConfiguration: Match.objectLike({
            LogFilePrefix: prefix,
          }),
        });
      }
    });

    it("does not make the access-logs bucket self-log (avoids a delivery loop)", () => {
      const { template } = buildStacks();
      const buckets = template.findResources("AWS::S3::Bucket");
      const logBucket = Object.values(buckets).find((b: any) =>
        JSON.stringify(b.Properties?.BucketName ?? "").includes("storage-logs"),
      ) as any;
      expect(logBucket).toBeDefined();
      expect(logBucket.Properties.LoggingConfiguration).toBeUndefined();
    });
  });

  describe("Outputs", () => {
    it("exports Neptune endpoint, port, OpenSearch endpoint and collection name", () => {
      const { template } = buildStacks();
      expect(
        Object.keys(template.findOutputs("NeptuneClusterEndpointOutput"))
          .length,
      ).toBe(1);
      expect(
        Object.keys(template.findOutputs("NeptunePortOutput")).length,
      ).toBe(1);
      expect(
        Object.keys(template.findOutputs("OpenSearchEndpointOutput")).length,
      ).toBe(1);
      expect(
        Object.keys(template.findOutputs("OpenSearchCollectionNameOutput"))
          .length,
      ).toBe(1);
    });
  });
});
