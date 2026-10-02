// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { AuthnzStack } from "../../lib/stacks/foundation/authnz-stack";

function buildTemplate(): Template {
  const app = new cdk.App();
  const stack = new AuthnzStack(app, "TestAuthnz");
  return Template.fromStack(stack);
}

describe("AuthnzStack", () => {
  describe("table count", () => {
    it("creates exactly 3 DynamoDB tables", () => {
      const template = buildTemplate();
      template.resourceCountIs("AWS::DynamoDB::Table", 3);
    });
  });

  describe("common table properties", () => {
    it("all tables use PAY_PER_REQUEST billing", () => {
      const template = buildTemplate();
      const tables = template.findResources("AWS::DynamoDB::Table");
      for (const [, resource] of Object.entries(tables)) {
        expect((resource as any).Properties.BillingMode).toBe(
          "PAY_PER_REQUEST",
        );
      }
    });

    it("all tables have point-in-time recovery enabled", () => {
      const template = buildTemplate();
      const tables = template.findResources("AWS::DynamoDB::Table");
      for (const [, resource] of Object.entries(tables)) {
        expect(
          (resource as any).Properties.PointInTimeRecoverySpecification
            .PointInTimeRecoveryEnabled,
        ).toBe(true);
      }
    });

    it("all tables use DELETE removal policy in non-prod (env-aware)", () => {
      const template = buildTemplate();
      const tables = template.findResources("AWS::DynamoDB::Table");
      for (const [, resource] of Object.entries(tables)) {
        expect((resource as any).DeletionPolicy).toBe("Delete");
      }
    });
  });

  describe("Roles table", () => {
    it("has PK/SK key schema and DynamoDB stream", () => {
      const template = buildTemplate();
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: Match.stringLikeRegexp("roles$"),
        KeySchema: Match.arrayWith([
          { AttributeName: "PK", KeyType: "HASH" },
          { AttributeName: "SK", KeyType: "RANGE" },
        ]),
        StreamSpecification: {
          StreamViewType: "NEW_AND_OLD_IMAGES",
        },
      });
    });
  });

  describe("ResourceRoleMappings table", () => {
    it("has PK/SK key schema and DynamoDB stream", () => {
      const template = buildTemplate();
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: Match.stringLikeRegexp("resource-role-mappings$"),
        StreamSpecification: {
          StreamViewType: "NEW_AND_OLD_IMAGES",
        },
      });
    });

    it("has PrincipalIndex GSI with principalKey PK and resourceRoleKey SK", () => {
      const template = buildTemplate();
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: Match.stringLikeRegexp("resource-role-mappings$"),
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "PrincipalIndex",
            KeySchema: [
              { AttributeName: "principalKey", KeyType: "HASH" },
              { AttributeName: "resourceRoleKey", KeyType: "RANGE" },
            ],
            Projection: { ProjectionType: "ALL" },
          }),
        ]),
      });
    });

    it("has NamespaceGrantsIndex GSI with namespaceKey PK and principalRoleKey SK", () => {
      const template = buildTemplate();
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: Match.stringLikeRegexp("resource-role-mappings$"),
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "NamespaceGrantsIndex",
            KeySchema: [
              { AttributeName: "namespaceKey", KeyType: "HASH" },
              { AttributeName: "principalRoleKey", KeyType: "RANGE" },
            ],
            Projection: { ProjectionType: "ALL" },
          }),
        ]),
      });
    });
  });

  describe("CacheInvalidation table", () => {
    it("has PK/SK key schema and no stream", () => {
      const template = buildTemplate();
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        TableName: Match.stringLikeRegexp("cache-invalidation$"),
        KeySchema: Match.arrayWith([
          { AttributeName: "PK", KeyType: "HASH" },
          { AttributeName: "SK", KeyType: "RANGE" },
        ]),
      });
      // Verify no stream on cache invalidation table
      const tables = template.findResources("AWS::DynamoDB::Table", {
        Properties: {
          TableName: Match.stringLikeRegexp("cache-invalidation$"),
        },
      });
      const table = Object.values(tables)[0] as any;
      expect(table.Properties.StreamSpecification).toBeUndefined();
    });
  });

  describe("streams", () => {
    it("exactly 2 tables have DynamoDB streams enabled", () => {
      const template = buildTemplate();
      const tables = template.findResources("AWS::DynamoDB::Table");
      const streamed = Object.values(tables).filter(
        (t: any) => t.Properties.StreamSpecification !== undefined,
      );
      expect(streamed).toHaveLength(2);
    });
  });

  describe("seeded group → role mappings", () => {
    const GROUP = "Platform Admins";
    const ENCODED = "Platform%20Admins";

    function seededItem(group: string): Record<string, { S: string }> {
      const app = new cdk.App();
      const stack = new AuthnzStack(app, "TestAuthnzSeed", {
        claimsMappings: [
          { groupValue: group, mappedRoles: ["platform-admin"] },
        ],
      });
      const resources = Template.fromStack(stack).findResources("Custom::AWS");
      const seeds = Object.values(resources).filter((r: any) =>
        JSON.stringify(r.Properties.Update ?? "").includes("principalKey"),
      );
      expect(seeds).toHaveLength(1);
      // TableName is a CFN token, so Update renders as an Fn::Join rather than
      // a plain string; splice the literal chunks back together to recover it.
      const update = (seeds[0] as any).Properties.Update;
      const payload =
        typeof update === "string"
          ? update
          : (update["Fn::Join"][1] as unknown[])
              .map((part) => (typeof part === "string" ? part : "TOKEN"))
              .join("");
      return JSON.parse(payload).parameters.Item;
    }

    // The authorizer encodes group names before querying the PrincipalIndex
    // GSI, so a raw key written here resolves to zero roles and denies access.
    it("writes principalKey in the encoded form readers query with", () => {
      expect(seededItem(GROUP).principalKey.S).toBe(`Group::${ENCODED}`);
    });

    it("writes PK in the encoded form", () => {
      expect(seededItem(GROUP).PK.S).toBe(`Platform::GLOBAL#Group::${ENCODED}`);
    });

    it("writes principalRoleKey in the encoded form", () => {
      expect(seededItem(GROUP).principalRoleKey.S).toBe(
        `Group::${ENCODED}#ROLE#platform-admin`,
      );
    });

    // principalId is display metadata, not a key, and the Python writers keep
    // it raw; encoding it here would make the UI show percent escapes.
    it("leaves principalId raw", () => {
      expect(seededItem(GROUP).principalId.S).toBe(GROUP);
    });

    it("leaves already-safe group names untouched", () => {
      const item = seededItem("platform-admins");
      expect(item.principalKey.S).toBe("Group::platform-admins");
      expect(item.principalId.S).toBe("platform-admins");
    });

    it("seeds nothing when no claims mappings are configured", () => {
      const resources = buildTemplate().findResources("Custom::AWS");
      const seeds = Object.values(resources).filter((r: any) =>
        JSON.stringify(r.Properties.Update ?? "").includes("principalKey"),
      );
      expect(seeds).toHaveLength(0);
    });
  });
});
