// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Annotations, Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";

// Mock bundlePython to avoid fingerprinting the entire repo root during tests.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };

// Provide fake ECR context so CDK uses fromEcr() instead of fromImageAsset()
// (which would trigger a real Docker build and hang the test).
// Mirrors the approach used in unstructured-stack.test.ts.
const TEST_CONTEXT = {
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_db_enrichment_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db-enrichment-test",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
  sources_kg_build_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg-build-test",
  "aws:cdk:bundling-stacks": [],
};

describe("SourcesStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
    const storage = new StorageStack(app, "TestStorage", {
      network,
      env: TEST_ENV,
    });
    template = Template.fromStack(
      new SourcesStack(app, "TestSources", {
        network,
        storage,
        allowedOrigin: "https://test.example.com",
        env: TEST_ENV,
      }),
    );
  });

  describe("DynamoDB Tables", () => {
    it("creates sources-table with PK and SK string keys", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        KeySchema: Match.arrayWith([
          { AttributeName: "PK", KeyType: "HASH" },
          { AttributeName: "SK", KeyType: "RANGE" },
        ]),
        BillingMode: "PAY_PER_REQUEST",
      });
    });

    it("sources-table has ByNamespace GSI on namespaceId + createdAt", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "ByNamespace",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "createdAt", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });

    it("sources-table has BySourceType GSI on namespaceId + sourceTypeCreatedAt", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "BySourceType",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "sourceTypeCreatedAt", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });

    it("sources-table has ByName GSI on namespaceId + name for O(1) uniqueness checks", () => {
      template.hasResourceProperties("AWS::DynamoDB::Table", {
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({
            IndexName: "ByName",
            KeySchema: Match.arrayWith([
              { AttributeName: "namespaceId", KeyType: "HASH" },
              { AttributeName: "name", KeyType: "RANGE" },
            ]),
          }),
        ]),
      });
    });
  });

  describe("Sources API Lambda", () => {
    it("creates a Python 3.12 ARM64 Lambda function", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Runtime: "python3.12",
        Architectures: ["arm64"],
        Timeout: 30,
        MemorySize: 256,
      });
    });

    it("Lambda has SOURCES_TABLE environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            SOURCES_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("Lambda has SOURCE_SCAN_JOBS_TABLE environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            SOURCE_SCAN_JOBS_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("Lambda has ALLOWED_ORIGIN environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            ALLOWED_ORIGIN: "https://test.example.com",
          }),
        },
      });
    });

    it("Lambda has REVIEW_QUEUE_URL environment variable for bulk approve/reject dispatch", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        Environment: {
          Variables: Match.objectLike({
            REVIEW_QUEUE_URL: Match.anyValue(),
          }),
        },
      });
    });

    it("Lambda has RESOURCE_PREFIX so derived Athena catalog names match the deployment", () => {
      // `_build_catalog_name` falls back to a hard-coded `coa-dev-` when this is
      // unset, which would collapse every environment onto one catalog prefix.
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-api$"),
        Environment: {
          Variables: Match.objectLike({
            RESOURCE_PREFIX: "coa-dev-",
          }),
        },
      });
    });

    it("grants the sources-api role prefix-scoped Athena data-catalog create/delete/get", () => {
      // Custom-connector sources register a LAMBDA-type Athena data catalog at
      // source-create and delete it at teardown. GetDataCatalog backs the
      // get-then-create idempotency check — CreateDataCatalog declares no
      // AlreadyExistsException, so a duplicate name is an opaque 400.
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn: any) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      expect(apiFn).toBeDefined();
      const apiRoleId = (apiFn as any).Properties.Role["Fn::GetAtt"][0];

      const statements = Object.values(
        template.findResources("AWS::IAM::Policy"),
      )
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === apiRoleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);

      expect(
        statements.find((s: any) => s.Sid === "AthenaDataCatalogLifecycle"),
      ).toEqual({
        Sid: "AthenaDataCatalogLifecycle",
        Effect: "Allow",
        Action: [
          "athena:CreateDataCatalog",
          "athena:DeleteDataCatalog",
          "athena:GetDataCatalog",
        ],
        // Prefix-scoped, matching `_build_catalog_name`'s `{prefix}ds_{hash}`.
        Resource:
          "arn:aws:athena:us-east-1:123456789012:datacatalog/coadevds_*",
      });
    });

    it("grants the sources-api role in-account DescribeSecret for the namespace-binding check, and NOT GetSecretValue", () => {
      // At source registration the API verifies a JDBC credential secret carries
      // a `{prefix}:namespace` tag LISTING the registering namespace. That check
      // reads TAGS only (DescribeSecret) — it must never be able to read secret
      // VALUES on this role, and must be scoped to this account (cross-account
      // secrets are gated by their own resource policy, not by this grant).
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn: any) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      expect(apiFn).toBeDefined();
      const apiRoleId = (apiFn as any).Properties.Role["Fn::GetAtt"][0];

      const statements = Object.values(
        template.findResources("AWS::IAM::Policy"),
      )
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === apiRoleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);

      expect(
        statements.find(
          (s: any) => s.Sid === "DescribeSecretForNamespaceBinding",
        ),
      ).toEqual({
        Sid: "DescribeSecretForNamespaceBinding",
        Effect: "Allow",
        Action: "secretsmanager:DescribeSecret",
        Resource: "arn:aws:secretsmanager:*:123456789012:secret:*",
      });

      // Least privilege: the registration path reads tags, never values.
      const grantsGetSecretValue = statements.some((s: any) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        return actions.includes("secretsmanager:GetSecretValue");
      });
      expect(grantsGetSecretValue).toBe(false);
    });

    it("grants the sources-api role Glue GetTags AND GetDatabase for the namespace-ownership check", () => {
      // Source-create refuses a Glue database whose owner has not tagged it for
      // the caller's namespace (finding F-8). The check fails closed, so without
      // this grant every native Glue source create would 403.
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn: any) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      expect(apiFn).toBeDefined();
      const apiRoleId = (apiFn as any).Properties.Role["Fn::GetAtt"][0];

      const statements = Object.values(
        template.findResources("AWS::IAM::Policy"),
      )
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === apiRoleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);

      const stmt = statements.find(
        (s: any) => s.Sid === "GlueOwnershipTagRead",
      );
      expect(stmt).toBeDefined();
      // GetDatabase is REQUIRED, not incidental: Glue authorizes GetTags on a
      // database ARN against glue:GetDatabase on the catalog, so GetTags alone
      // yields AccessDenied and the fail-closed check then refuses every
      // legitimate Glue source. An earlier revision asserted `["glue:GetTags"]`
      // exactly and shipped that 403 to a live account — this assertion exists to
      // stop the narrower policy coming back.
      expect(stmt.Action).toEqual(["glue:GetTags", "glue:GetDatabase"]);
      // Still metadata only: no GetTable(s), no Lake Formation, no data path.
      expect(stmt.Action).not.toContain("glue:GetTables");
    });
  });

  describe("credential-secret namespace binding (secret-read conditions)", () => {
    /** Every IAM policy statement in the stack, flattened. */
    function allStatements(): any[] {
      return Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
        (p: any) => p.Properties.PolicyDocument.Statement,
      );
    }
    const bySid = (sid: string) =>
      allStatements().find((s: any) => s.Sid === sid);

    it("grants nested-catalog Glue access to the discovery and enrichment roles (issue 118)", () => {
      interface PolicyStmt {
        Sid?: string;
        Action: string | string[];
        Resource: string | string[];
      }
      const toArr = (x: string | string[]): string[] =>
        Array.isArray(x) ? x : [x];
      const glue = (suffix: string) =>
        `arn:aws:glue:us-east-1:123456789012:${suffix}`;

      // Both roles' Glue metadata-read statements, targeted by Sid rather than by
      // filtering on catalog/* (which would pass even if one role lost the grant,
      // as long as some other role kept it).
      const discovery: PolicyStmt = bySid("GlueCatalogAccess");
      const enrichment: PolicyStmt = bySid("EnrichmentGlueCatalogAccess");

      // Federated catalogs (catalogId "account:catalogName") authorize
      // GetDatabase/GetTables against the nested-catalog resource itself, so the
      // root `:catalog` alone yields AccessDenied on `catalog/<name>`. Both roles
      // need GetCatalog(s) AND the full resource set (a refactor dropping any one
      // resource must fail here, not just a missing catalog/*).
      for (const stmt of [discovery, enrichment]) {
        const actions = toArr(stmt.Action);
        const resources = toArr(stmt.Resource);
        expect(actions).toEqual(
          expect.arrayContaining(["glue:GetCatalog", "glue:GetCatalogs"]),
        );
        expect(resources).toEqual(
          expect.arrayContaining([
            glue("catalog"),
            glue("catalog/*"),
            glue("database/*"),
            glue("table/*/*"),
            glue("connection/*"),
          ]),
        );
      }
    });

    // Discovery role: an in-account customer secret must carry a
    // `{prefix}:namespace` tag (Null:false = key must be present), so a bypassed write path can't
    // read an arbitrary untagged account secret. Still ANDs the account guard.
    it("conditions the discovery role's in-account secret read on the namespace tag", () => {
      const s = bySid("SecretsManagerCustomerProvided");
      expect(s.Condition).toEqual({
        StringEquals: { "aws:ResourceAccount": "123456789012" },
        Null: { "secretsmanager:ResourceTag/coa:namespace": "false" },
      });
    });

    // Federated-catalog role: in-account read is tag-gated; cross-account read
    // is split into its own statement (customer secret, gated by the customer's
    // resource policy, no tag we control).
    it("splits the federated-catalog secret read into tag-gated in-account and unconditioned cross-account", () => {
      expect(bySid("ReadCredentialSecretInAccount").Condition).toEqual({
        StringEquals: { "aws:ResourceAccount": "123456789012" },
        Null: { "secretsmanager:ResourceTag/coa:namespace": "false" },
      });
      // The cross-account statement also requires aws:ResourceAccount to be
      // PRESENT. A negated condition is satisfied when its key is absent, so
      // StringNotEquals on its own would leave this statement unconditioned in
      // any request context that does not populate the key.
      expect(bySid("ReadCredentialSecretCrossAccount").Condition).toEqual({
        StringNotEquals: { "aws:ResourceAccount": "123456789012" },
        Null: { "aws:ResourceAccount": "false" },
      });
    });

    // The provisioner's PutResourcePolicy write primitive is scoped to
    // onboarded (tagged) in-account secrets, not every account secret.
    it("scopes the federation provisioner's resource-policy write to tagged in-account secrets", () => {
      const s = bySid("SecretResourcePolicyForConsumer");
      expect(s.Action).toEqual([
        "secretsmanager:GetResourcePolicy",
        "secretsmanager:PutResourcePolicy",
      ]);
      expect(s.Condition).toEqual({
        StringEquals: { "aws:ResourceAccount": "123456789012" },
        Null: { "secretsmanager:ResourceTag/coa:namespace": "false" },
      });
    });

    /** IAM statements attached to the role of the Lambda whose name ends `suffix`. */
    function statementsForFunction(suffix: string): any[] {
      const fn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((f: any) =>
        String(f.Properties?.FunctionName ?? "").endsWith(suffix),
      );
      expect(fn).toBeDefined();
      const roleId = (fn as any).Properties.Role["Fn::GetAtt"][0];
      return Object.values(template.findResources("AWS::IAM::Policy"))
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === roleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    }

    // There used to be an unconditioned `SecretsManagerCoaManaged` statement on
    // the discovery role covering `{prefix}datasource-*`. IAM statements are
    // additive, so it exempted exactly the naming convention the platform's own
    // credential secrets use from the tag requirement above. Verified live: with
    // it attached, an untagged secret and a secret tagged for another namespace
    // were both readable.
    it("leaves the discovery role no untagged in-account read path", () => {
      const reads = statementsForFunction("sources-db-connector").filter(
        (s: any) => {
          const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
          return actions.includes("secretsmanager:GetSecretValue");
        },
      );
      expect(reads).toHaveLength(1);
      expect(reads[0].Sid).toBe("SecretsManagerCustomerProvided");
      expect(
        allStatements().some((s: any) => s.Sid === "SecretsManagerCoaManaged"),
      ).toBe(false);
    });

    // Both scan-pipeline handlers re-verify the binding against the STORED row
    // before reading the secret, which needs DescribeSecret (tags), never
    // GetSecretValue.
    it.each([["sources-db-connector"], ["sources-federation-provisioner"]])(
      "grants %s in-account DescribeSecret for the scan-time re-check",
      (suffix: string) => {
        expect(
          statementsForFunction(suffix).find(
            (s: any) => s.Sid === "DescribeSecretForNamespaceBinding",
          ),
        ).toEqual({
          Sid: "DescribeSecretForNamespaceBinding",
          Effect: "Allow",
          Action: "secretsmanager:DescribeSecret",
          Resource: "arn:aws:secretsmanager:*:123456789012:secret:*",
        });
      },
    );

    // The discovery Lambda derives the tag key at runtime, so it needs the same
    // BARE prefix the IAM conditions were written against. Without it the key
    // falls back to the brand default and a non-`coa` deployment's re-check would
    // read a tag nobody writes.
    it("gives the discovery Lambda the RESOURCE_TAG_PREFIX its re-check derives the key from", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({ RESOURCE_TAG_PREFIX: "coa" }),
        },
      });
    });

    // The enrichment task reaches source credentials through the same JDBC
    // connector code, so its read carries the same tag requirement. Its grant was
    // the other untagged in-account read path.
    it("conditions the enrichment task role's credential-secret read on the namespace tag", () => {
      const s = bySid("ReadNamespaceBoundCredentialSecret");
      expect(s.Action).toBe("secretsmanager:GetSecretValue");
      expect(s.Condition).toEqual({
        Null: { "secretsmanager:ResourceTag/coa:namespace": "false" },
      });
    });
  });

  // The tag KEY carries the deployment prefix, so two deployments co-located in
  // one AWS account bind independently — a secret onboarded to `scl` is not
  // readable by a `coa` deployment. The assertions above run under the default
  // prefix (`coa`), where a hardcoded key is indistinguishable from a derived
  // one; this block synthesizes under a different prefix so a regression to a
  // literal `coa:namespace` fails here.
  describe("credential-secret namespace binding (prefix-derived tag key)", () => {
    let scl: Template;

    beforeAll(() => {
      const app = new cdk.App({
        context: { ...TEST_CONTEXT, resource_prefix: "scl", env: "dev" },
      });
      const network = new NetworkStack(app, "SclNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "SclStorage", {
        network,
        env: TEST_ENV,
      });
      scl = Template.fromStack(
        new SourcesStack(app, "SclSources", {
          network,
          storage,
          allowedOrigin: "https://test.example.com",
          env: TEST_ENV,
        }),
      );
    });

    const sclBySid = (sid: string) =>
      Object.values(scl.findResources("AWS::IAM::Policy"))
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
        .find((st: any) => st.Sid === sid);

    it.each([
      "SecretsManagerCustomerProvided",
      "ReadCredentialSecretInAccount",
      "SecretResourcePolicyForConsumer",
    ])("keys %s's tag condition on the deployment prefix", (sid) => {
      expect(sclBySid(sid).Condition.Null).toEqual({
        "secretsmanager:ResourceTag/scl:namespace": "false",
      });
    });

    // The runtime derives the same key from RESOURCE_TAG_PREFIX. If this var is
    // missing or carries the `{prefix}-{env}-` form, registration writes/checks a
    // key the IAM conditions above cannot match and every JDBC source breaks.
    it.each(["sources-api", "sources-federation-provisioner"])(
      "passes the BARE prefix to %s as RESOURCE_TAG_PREFIX",
      (fnSuffix) => {
        const fn = Object.values(
          scl.findResources("AWS::Lambda::Function"),
        ).find((f: any) =>
          String(f.Properties?.FunctionName ?? "").endsWith(fnSuffix),
        );
        expect(fn).toBeDefined();
        expect(
          (fn as any).Properties.Environment.Variables.RESOURCE_TAG_PREFIX,
        ).toBe("scl");
      },
    );

    it("grants the sources-api role s3:ListBucket on the sources bucket", () => {
      // S3 returns NoSuchKey for a missing key only to a caller that also holds
      // ListBucket; otherwise GetObject on an absent key answers AccessDenied.
      // A re-scan that finds no drift writes no backup blob yet still leaves the
      // source in RESCAN_REVIEW, so the tables API reads a key that is legitimately
      // absent. Without this grant the read helper's absent-key branch cannot fire
      // and every no-drift re-scan 500s the tables page. Unit tests mock S3 and
      // cannot catch a missing grant — only this template assertion does.
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn: any) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      expect(apiFn).toBeDefined();
      const apiRoleId = (apiFn as any).Properties.Role["Fn::GetAtt"][0];

      const statements = Object.values(
        template.findResources("AWS::IAM::Policy"),
      )
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === apiRoleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);

      // Bucket-level resource, not a /*/rescan-backup/* prefix: the 403-vs-404
      // choice on GetObject is not governed by an s3:prefix condition, so a
      // narrower grant would leave the 500 in place.
      const listStatements = statements.filter((s: any) =>
        [s.Action].flat().includes("s3:ListBucket"),
      );
      expect(listStatements.length).toBeGreaterThan(0);
    });
  });

  describe("Discovery role — custom Athena federation connectors", () => {
    /** IAM statements attached to the db-connector (discovery) Lambda's role. */
    function discoveryStatements(): any[] {
      const fn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((f: any) =>
        String(f.Properties?.FunctionName ?? "").endsWith(
          "sources-db-connector",
        ),
      );
      expect(fn).toBeDefined();
      const roleId = (fn as any).Properties.Role["Fn::GetAtt"][0];
      return Object.values(template.findResources("AWS::IAM::Policy"))
        .filter((p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === roleId),
        )
        .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    }

    // Discovery runs SHOW/DESCRIBE against the Lambda-backed catalog, and Athena
    // resolves catalog name → connector ARN via GetDataCatalog. The existing
    // AthenaEnumSampling statement is workgroup-scoped only, so without this the
    // very first SHOW DATABASES fails AccessDenied.
    it("grants prefix-scoped athena:GetDataCatalog", () => {
      expect(
        discoveryStatements().find(
          (s: any) => s.Sid === "CustomConnectorCatalogRead",
        ),
      ).toEqual({
        Sid: "CustomConnectorCatalogRead",
        Effect: "Allow",
        Action: "athena:GetDataCatalog",
        Resource:
          "arn:aws:athena:us-east-1:123456789012:datacatalog/coadevds_*",
      });
    });

    // The discovery role reads Glue across the whole account (`database/*`,
    // `table/*/*`), which is what finding F-8 turned into cross-namespace access.
    // The tag is the authorization that read is now checked against, so it has to
    // be readable on the same resources.
    it("grants the discovery role Glue GetTags alongside its account-wide reads", () => {
      const stmt = discoveryStatements().find(
        (s: any) => s.Sid === "GlueCatalogAccess",
      );
      expect(stmt.Action).toContain("glue:GetTags");
      expect(stmt.Resource).toEqual(
        expect.arrayContaining([
          "arn:aws:glue:us-east-1:123456789012:database/*",
        ]),
      );
    });

    // The check recognises this deployment's own federated catalogs by the
    // `{sanitizedPrefix}ds_` shape derived from RESOURCE_PREFIX. Unset, the runtime
    // default (`coa-dev-`) disagrees with `fedResourcePrefix` and our own catalogs
    // read as third-party databases.
    it("gives the discovery Lambda RESOURCE_PREFIX for the ownership check", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({ RESOURCE_PREFIX: "coa-dev-" }),
        },
      });
    });

    // RESOURCE_TAG_PREFIX is the BARE prefix, deliberately not `prefixed("")`. The
    // `{prefix}:namespace` tag key is applied by a data owner and must not carry the
    // environment: `coa-prod:namespace` would make a database tagged in dev
    // invisible to prod. It cannot be inferred at runtime either — stripping
    // `-{env}` needs a value that is not on these Lambdas — so CDK passes it.
    it.each(["sources-db-connector$", "sources-api$"])(
      "gives %s the bare RESOURCE_TAG_PREFIX, without the environment suffix",
      (fnName) => {
        template.hasResourceProperties("AWS::Lambda::Function", {
          FunctionName: Match.stringLikeRegexp(fnName),
          Environment: {
            Variables: Match.objectLike({ RESOURCE_TAG_PREFIX: "coa" }),
          },
        });
      },
    );

    // Two controls covering different things. `aws:CalledVia` keeps this from being an
    // invoke primitive usable directly from discovery code; the resource TAG scopes
    // WHICH functions, since the account must stay a wildcard (the connector lives in
    // the customer's) and Athena exposes no condition key naming the catalog a
    // forward-access-session invoke serves. The account is still not excluded — a
    // connector may be deployed alongside this stack — so the same-account escalation
    // is closed by the Deny below.
    it("grants connector invoke only when Athena is the caller, and only for tagged functions", () => {
      expect(
        discoveryStatements().find(
          (s: any) => s.Sid === "AthenaFederationConnectorInvoke",
        ),
      ).toEqual({
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

    // Without this, an Athena UDF — which needs only StartQueryExecution,
    // granted above, plus InvokeFunction — reaches every Lambda in OUR account,
    // including the Lake-Formation-admin federation provisioner.
    //
    // Account-wide and region-wide, not scoped to our name prefix: the prefix
    // form made a naming convention load-bearing for security and silently
    // refused any connector deployed into this account under the prefix, which
    // is what scripts/deploy-example-connector.sh does. Breadth is the safe
    // direction for a Deny.
    it("denies Athena-mediated invoke of every untagged function in this account", () => {
      expect(
        discoveryStatements().find(
          (s: any) => s.Sid === "DenyAthenaInvokeOfUntaggedFunctions",
        ),
      ).toEqual({
        Sid: "DenyAthenaInvokeOfUntaggedFunctions",
        Effect: "Deny",
        Action: "lambda:InvokeFunction",
        Resource: "arn:aws:lambda:*:123456789012:function:*",
        Condition: {
          "ForAnyValue:StringEquals": {
            "aws:CalledVia": "athena.amazonaws.com",
          },
          StringNotEquals: { "aws:ResourceTag/coa:connector": "true" },
        },
      });
    });

    // StringNotEquals matches an ABSENT key, which is what makes the exemption
    // fail closed. Discovery is the role that runs DESCRIBE, so losing this
    // exemption presents as a source that scans with no keys rather than as a
    // permissions error.
    it("exempts the connector tag by its absence, not by an equality match", () => {
      const condition = discoveryStatements().find(
        (s: any) => s.Sid === "DenyAthenaInvokeOfUntaggedFunctions",
      ).Condition;
      expect(condition.StringNotEquals).toEqual({
        "aws:ResourceTag/coa:connector": "true",
      });
      expect(condition.StringEquals).toBeUndefined();
    });

    // Spill is a record-path mechanism; discovery's entire Athena surface
    // (SHOW/DESCRIBE) is metadata-handler traffic that never spills. Granting it
    // here would widen a second role for no functional gain.
    it("does not grant the discovery role the cross-account spill read", () => {
      const sids = discoveryStatements().map((s: any) => s.Sid);
      expect(sids).not.toContain("AthenaFederationSpillRead");
      expect(sids).not.toContain("AthenaFederationSpillDecryptViaS3");
    });
  });

  describe("Federation Provisioner (Option B isolation)", () => {
    it("creates a dedicated JDBC federation provisioner Lambda in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-federation-provisioner$",
        ),
        Handler: "coa_sources.database.pipeline.federation_handler.handler",
        VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
        Environment: {
          Variables: Match.objectLike({
            FEDERATED_CATALOG_ROLE_ARN: Match.anyValue(),
            ATHENA_SPILL_BUCKET: Match.anyValue(),
          }),
        },
      });
    });

    it("publishes the federation provisioner role ARN for central LF-admin registration", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: Match.stringLikeRegexp(
          ".*Lake Formation data-lake admin.*",
        ),
      });
    });

    it("passes the consumer query role SSM param to the federation provisioner", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-federation-provisioner$",
        ),
        Environment: {
          Variables: Match.objectLike({
            // The env segment is part of the path: the runtime read, the policy naming
            // the parameter ARN and the deploy-time reference all have to resolve the
            // same parameter, and only this environment's.
            CONSUMER_QUERY_ROLE_SSM_PARAM: Match.stringLikeRegexp(
              ".*/dev/serve/runtime-role-arn$",
            ),
          }),
        },
      });
    });

    it("grants the federation provisioner Glue read on federated databases/tables (for GrantPermissions)", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "GlueFederatedCatalogRead",
              Action: [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
              ],
              Resource: Match.arrayWith([
                Match.stringLikeRegexp(".*:database/.*"),
                Match.stringLikeRegexp(".*:table/.*"),
              ]),
            }),
          ]),
        },
      });
    });

    it("grants the federation provisioner Glue read on ALL native databases (for native LF grant)", () => {
      // Required so lakeformation:GrantPermissions can validate the grantor has
      // access to the target native Glue database (GLUE_DATABASE sources, strict-LF accounts).
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "GlueNativeDatabaseRead",
              Action: [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
                // Namespace-ownership tag, re-checked before this LF-admin role
                // grants the shared serve role SELECT on a native database.
                "glue:GetTags",
              ],
              // Wildcard suffixes cover ALL native databases, not just scldevds_* federated ones.
              Resource: Match.arrayWith([
                Match.stringLikeRegexp(":catalog$"),
                Match.stringLikeRegexp(":database/\\*$"),
                Match.stringLikeRegexp(":table/\\*/\\*$"),
              ]),
            }),
          ]),
        },
      });
    });

    it("grants the federation provisioner lakeformation:GrantPermissions", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "LakeFormationFederation",
              Action: Match.arrayWith(["lakeformation:GrantPermissions"]),
            }),
          ]),
        },
      });
    });

    it("registers the provisioner role as an LF admin via a custom resource (non-destructive)", () => {
      // onEvent Lambda role can read/write LF settings + read the role-ARN SSM param.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                "lakeformation:GetDataLakeSettings",
                "lakeformation:PutDataLakeSettings",
              ]),
            }),
          ]),
        },
      });
    });

    it("lets the federated-catalog role decrypt CMK-encrypted secrets via Secrets Manager only", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "DecryptCredentialSecret",
              Action: "kms:Decrypt",
              Condition: {
                StringLike: {
                  "kms:ViaService": "secretsmanager.*.amazonaws.com",
                },
              },
            }),
          ]),
        },
      });
    });

    it("grants the federated-catalog role ENI actions on * (Glue dry-runs them against a wildcard)", () => {
      // Regression cover for a failure that presents as a networking problem:
      // Glue's managed connector pre-flight authorizes DeleteNetworkInterface
      // against `arn:aws:ec2:<region>:<account>:*/*`, so any resource-scoped
      // statement is denied and the connection reports "Unable to access VPC
      // provided in the connection" — naming the subnet and SG, never the denied
      // action. Scoping this statement breaks every federated catalog in a fresh
      // environment. AWS's own AWSGlueServiceRole uses `*` for the same actions.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "Ec2NetworkInterfaceManagement",
              Action: Match.arrayWith([
                "ec2:CreateNetworkInterface",
                "ec2:DeleteNetworkInterface",
              ]),
              Resource: "*",
            }),
          ]),
        },
      });
    });

    it("lets the provisioner assume the federated-catalog role for the secret precheck", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "sts:AssumeRole",
              Resource: Match.anyValue(),
            }),
          ]),
        },
      });
    });
  });

  describe("Cross-account datasource assume (confused-deputy guard)", () => {
    // The role ARN assumed here comes from the CreateSource caller, so the
    // ExternalId — derived from the requesting namespace, never from the request
    // — is what binds an assume to the namespace entitled to it. Without it, a
    // caller with manageSource on any namespace could point a source at another
    // tenant's *-datasource-access-* role and read it.
    const assumeStatements = () => {
      const policies = template.findResources("AWS::IAM::Policy");
      return Object.values(policies)
        .flatMap(
          (p) =>
            p.Properties.PolicyDocument.Statement as Record<string, unknown>[],
        )
        .filter((st) => {
          const resource = JSON.stringify(st.Resource ?? "");
          return (
            st.Action === "sts:AssumeRole" &&
            resource.includes("datasource-access-")
          );
        });
    };

    it("grants assume on customer datasource-access roles in all three consumers", () => {
      // The discovery Lambda, the enrichment task, and the sources API each get
      // their own grant. The sources API's is the newest: it validates the
      // customer's credential-access role at source-create, so a broken trust
      // policy is reported at submit rather than at first scan. The Databricks
      // connector holds the same shape from its own CDK app, which this template
      // does not contain (it is deployed separately, on purpose).
      expect(assumeStatements().length).toBe(3);
    });

    it("requires an ExternalId on every datasource assume grant", () => {
      const statements = assumeStatements();
      expect(statements.length).toBeGreaterThan(0);
      for (const st of statements) {
        expect(st.Condition).toEqual({
          Null: { "sts:ExternalId": "false" },
        });
      }
    });

    // All three are produced by one helper precisely so they cannot drift; this is
    // what asserts the helper did not quietly stop being one shape. Account
    // wildcard included: the customer's credential-access role may live in any
    // account, this deployment's own included, so the reserved NAME prefix plus
    // the target's trust policy is the whole bound.
    it("gives all three the identical account-agnostic reserved-prefix scope", () => {
      const resources = assumeStatements().map((st) => st.Resource);
      expect(resources).toEqual([
        "arn:aws:iam::*:role/coa-dev-datasource-access-*",
        "arn:aws:iam::*:role/coa-dev-datasource-access-*",
        "arn:aws:iam::*:role/coa-dev-datasource-access-*",
      ]);
      for (const resource of resources) {
        // No `123456789012` anywhere in the ARN: an account restriction here would
        // refuse the deliberate cross-account topology.
        expect(String(resource)).not.toContain(TEST_ENV.account);
      }
    });

    it("keeps the Sids the deployed policies shipped with", () => {
      expect(
        assumeStatements()
          .map((st) => st.Sid)
          .sort(),
      ).toEqual([
        "AssumeRoleCoaManaged",
        "AssumeRoleCustomerProvided",
        "AssumeRoleDatasourceAccessValidation",
      ]);
    });

    it("gives the discovery Lambda the prefix it derives the ExternalId from", () => {
      // discovery_handler._external_id presents `{prefix}{namespaceId}`; an unset
      // prefix would make two deployments present the same value for a namespace.
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            RESOURCE_PREFIX: "coa-dev-",
          }),
        },
      });
    });
  });

  describe("Databricks sub-type — sources-API config parameters", () => {
    /** IAM statements attached to the sources-API Lambda's role. */
    const sourcesApiStatements = (): Record<string, unknown>[] => {
      const fn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((f) =>
        String(f.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      expect(fn).toBeDefined();
      const roleId: string = fn!.Properties.Role["Fn::GetAtt"][0];
      return Object.values(template.findResources("AWS::IAM::Policy"))
        .filter((p) =>
          p.Properties.Roles?.some((r: { Ref?: string }) => r.Ref === roleId),
        )
        .flatMap(
          (p) =>
            p.Properties.PolicyDocument.Statement as Record<string, unknown>[],
        );
    };

    const statementWithSid = (sid: string): Record<string, unknown> => {
      const found = sourcesApiStatements().find((st) => st.Sid === sid);
      expect(found).toBeDefined();
      return found!;
    };

    /**
     * Whether an IAM resource pattern would authorise `arn`. The assertions below are
     * NEGATIVE, and a substring check would pass for a pattern that matches by
     * wildcard rather than by literal, so expand `*`/`?` the way IAM does.
     */
    const iamResourceMatches = (pattern: string, arn: string): boolean =>
      new RegExp(
        `^${pattern
          .replace(/[.+^${}()|[\]\\]/g, "\\$&")
          .replace(/\*/g, ".*")
          .replace(/\?/g, ".")}$`,
      ).test(arn);

    const SSM_ARN = "arn:aws:ssm:us-east-1:123456789012:parameter";
    const SOURCES_PREFIX = "/coa/dev/connectors/databricks/sources";
    const DEPLOYMENT_PARAM =
      "/coa/dev/connectors/databricks/deployment/function-arn";

    // `ssmPrefix` is `/${prefix}` with NO environment segment, while physical
    // names are `{prefix}-{env}-{name}`, and environments share an account. Both
    // paths therefore have to insert `dev` explicitly. Asserted as exact strings
    // rather than a regex, because the failure mode of getting this wrong is not a
    // broken deploy — it is a dev registration creating a catalog that points at
    // prod's connector, and a dev role able to repoint a prod source's credential.
    it("passes the per-source parameter prefix, environment segment included", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-api$"),
        Environment: {
          Variables: Match.objectLike({
            DATABRICKS_CONFIG_SSM_PREFIX: SOURCES_PREFIX,
          }),
        },
      });
      expect(SOURCES_PREFIX).not.toBe("/coa/connectors/databricks/sources");
    });

    it("passes the connector-ARN parameter NAME, not the ARN itself", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-api$"),
        Environment: {
          Variables: Match.objectLike({
            DATABRICKS_CONNECTOR_ARN_SSM_PARAM: DEPLOYMENT_PARAM,
          }),
        },
      });
      // The connector is deployed from its own CDK app, so `infra` cannot see its
      // ARN at synth — and passing the name instead is what keeps the ARN out of
      // this template. A value here would mean someone resolved it at synth.
      expect(DEPLOYMENT_PARAM).not.toContain("arn:aws:lambda");
    });

    // Net-new: this role held no SSM write at all. Integrity is the property —
    // the parameter tells the shared connector which secret and which warehouse
    // to use, so repointing one repoints a source at another source's credential
    // with the catalog name unchanged.
    it("grants exactly the two write actions the runtime calls, and no more", () => {
      const stmt = statementWithSid("DatabricksSourceConfigWrite");
      expect(stmt.Effect).toBe("Allow");
      // An exact list rather than `arrayWith`, so a write action cannot reappear on
      // the one path where integrity is the whole point without a test saying why.
      // `ssm:DeleteParameters` (plural) is a distinct action covering the batch API
      // and nothing calls it; `ssm:AddTagsToResource` is absent because nothing tags
      // this parameter — `GetParameter` returns no tags, so the ids live in its body.
      expect(stmt.Action).toEqual(["ssm:PutParameter", "ssm:DeleteParameter"]);
      // The SSM ARN quirk, asserted literally: the resource is `parameter`
      // immediately followed by the parameter NAME, which already begins with `/`.
      // `parameter/${ssmPrefix}/...` is a different path and matches nothing.
      expect(stmt.Resource).toBe(`${SSM_ARN}${SOURCES_PREFIX}/*`);
    });

    // The two subtrees are separated so the sources API cannot overwrite the ARN it
    // later reads, which holds only if neither scope covers the other's path.
    it("cannot write the deployment subtree it reads from", () => {
      const write = String(
        statementWithSid("DatabricksSourceConfigWrite").Resource,
      );
      expect(iamResourceMatches(write, `${SSM_ARN}${DEPLOYMENT_PARAM}`)).toBe(
        false,
      );
      // Guard the guard: the same helper must match what the scope IS for, or a
      // typo in the pattern would make the negative assertion vacuous.
      expect(
        iamResourceMatches(
          write,
          `${SSM_ARN}${SOURCES_PREFIX}/coadevds_abc123`,
        ),
      ).toBe(true);
    });

    it("reads exactly one parameter, and not the per-source subtree it writes", () => {
      const stmt = statementWithSid("ReadDatabricksConnectorArnParam");
      expect(stmt.Effect).toBe("Allow");
      expect(stmt.Action).toBe("ssm:GetParameter");
      // One parameter, no trailing wildcard.
      expect(stmt.Resource).toBe(`${SSM_ARN}${DEPLOYMENT_PARAM}`);
      const read = String(stmt.Resource);
      expect(
        iamResourceMatches(read, `${SSM_ARN}${SOURCES_PREFIX}/coadevds_abc123`),
      ).toBe(false);
      expect(iamResourceMatches(read, `${SSM_ARN}${DEPLOYMENT_PARAM}`)).toBe(
        true,
      );
    });

    // `GetDataCatalog` returns no tags, and with one shared handler ARN
    // `register_lambda_catalog`'s ownership check (catalog type + handler-ARN set)
    // is trivially true for every Databricks catalog. The `coa:sourceId` tag is
    // what is left to tell two of them apart at delete, so both the write and the
    // read of it are required — and scoped to the catalogs this deployment names.
    it("grants catalog tagging and tag reads on this deployment's catalogs only", () => {
      const stmt = statementWithSid("AthenaDataCatalogTagging");
      expect(stmt.Action).toEqual([
        "athena:TagResource",
        "athena:ListTagsForResource",
      ]);
      expect(stmt.Resource).toBe(
        "arn:aws:athena:us-east-1:123456789012:datacatalog/coadevds_*",
      );
    });

    // Already shipped and covering what the new create-time validation needs:
    // in-account, region-wildcard, DescribeSecret only (never GetSecretValue).
    // Asserted here so a narrowing does not silently break the Databricks
    // secret-binding check, which calls DescribeSecret in the secret's own region.
    it("keeps the shipped secret-binding read, in-account and region-wildcard", () => {
      const stmt = statementWithSid("DescribeSecretForNamespaceBinding");
      expect(stmt.Action).toBe("secretsmanager:DescribeSecret");
      expect(stmt.Resource).toBe(
        "arn:aws:secretsmanager:*:123456789012:secret:*",
      );
    });

    // `infra` gains NO connector resources — the connector keeps its own CDK app and
    // its own deploy job, and the two exchange ARNs through SSM.
    it("creates no connector Lambda, spill bucket or connector-ARN parameter", () => {
      const rendered = JSON.stringify(template.toJSON());
      expect(rendered).not.toContain("databricks-connector");
      // The connector's own stack is the sole writer under `deployment/`; a
      // parameter here would mean `infra` had started writing it.
      const params = Object.values(
        template.findResources("AWS::SSM::Parameter"),
      ).map((p) => String(p.Properties?.Name ?? ""));
      expect(params).not.toContain(DEPLOYMENT_PARAM);
      for (const name of params) {
        expect(name).not.toContain("/connectors/databricks/");
      }
    });

    /**
     * The stringly-typed half of the handoff, tripwired.
     *
     * These two path suffixes are composed INDEPENDENTLY in two pnpm workspaces —
     * here, and in `connectors/databricks/cdk/lib/constants.ts`
     * (`CONFIG_SSM_PREFIX_SUFFIX`, `FUNCTION_ARN_PARAMETER_SUFFIX`) — and each
     * suite pins its own literal. So renaming one leaves the other green while
     * the sources API writes where the connector does not read, and the first
     * symptom is a real source create failing to resolve its configuration.
     *
     * A TEXT-level check on purpose: `connectors/` is a separate workspace so those
     * apps stay copyable-out and buildable on their own, and an import here would
     * reintroduce the cross-workspace coupling — including the `aws-cdk-lib`
     * version-equality invariant that measurably broke it — so do NOT "fix" this
     * into an import.
     */
    it("agrees with the connector app on both path suffixes", () => {
      // Both LEAVES, not just the `deployment/` subtree: asserting the subtree alone
      // leaves the `function-arn` leaf free to be renamed on one side and stay green.
      const SHARED_SUFFIXES = [
        "/connectors/databricks/sources",
        "/connectors/databricks/deployment/",
        "/connectors/databricks/deployment/function-arn",
      ];
      const CONNECTOR_STACK = path.join(
        "connectors",
        "databricks",
        "cdk",
        "lib",
        "constants.ts",
      );
      const source = fs.readFileSync(
        path.join(__dirname, "..", "..", "..", CONNECTOR_STACK),
        "utf8",
      );

      // The two sides, checked independently against the same literals, so a
      // failure says WHICH side moved rather than just that they disagree.
      const infraSide = [SOURCES_PREFIX, DEPLOYMENT_PARAM].join("\n");
      const drifted = SHARED_SUFFIXES.flatMap((suffix) =>
        [
          { file: CONNECTOR_STACK, present: source.includes(suffix) },
          {
            file: path.join(
              "infra",
              "lib",
              "stacks",
              "services",
              "sources-stack.ts",
            ),
            present: infraSide.includes(suffix),
          },
        ]
          .filter(({ present }) => !present)
          .map(({ file }) => ({
            suffix,
            absentFrom: file,
            mustAgreeWith: SHARED_SUFFIXES,
            why:
              "The sources API writes `sources/` and reads `deployment/`; the connector " +
              "composes the same two paths independently in a separate pnpm workspace. " +
              "Renaming one side leaves the other green and fails only at a real source create.",
          })),
      );

      expect(drifted).toEqual([]);
    });
  });

  describe("UnexpectedConnectorParameterWrite", () => {
    const PATH_PREFIX = "/coa/dev/connectors/databricks/sources/";

    const rules = () =>
      Object.values(template.findResources("AWS::Events::Rule")).filter((r) =>
        String(r.Properties?.Name ?? "").includes("databricks-parameter"),
      );

    const ruleNamed = (name: string): Record<string, unknown> => {
      const found = rules().find((r) => r.Properties.Name === name);
      expect(found).toBeDefined();
      return found!.Properties;
    };

    // The only detection for this threat: a repointed parameter resolves
    // successfully, so no runtime metric can see it.
    it("watches all three write APIs across the two rules", () => {
      const eventNames = rules().flatMap(
        (r) => r.Properties.EventPattern.detail.eventName as string[],
      );
      // `DeleteParameters` is a distinct CloudTrail event name as well as a distinct
      // IAM action; omitting it would let the batch form escape the detection.
      expect(eventNames.sort()).toEqual([
        "DeleteParameter",
        "DeleteParameters",
        "PutParameter",
      ]);
    });

    it("prefix-matches the singular request shape on the env-scoped path", () => {
      const pattern = ruleNamed("coa-dev-databricks-parameter-writes");
      expect(pattern).toMatchObject({
        EventPattern: {
          source: ["aws.ssm"],
          "detail-type": ["AWS API Call via CloudTrail"],
          detail: {
            eventSource: ["ssm.amazonaws.com"],
            eventName: ["PutParameter", "DeleteParameter"],
            requestParameters: { name: [{ prefix: PATH_PREFIX }] },
          },
        },
      });
    });

    // The batch form carries `names` (a LIST), not `name`. A single pattern naming
    // both keys under `requestParameters` would require both and so match nothing,
    // which is why this is a second rule rather than one with `$or`.
    it("covers the batch shape, which uses names as a list", () => {
      const pattern = ruleNamed("coa-dev-databricks-parameter-batch-deletes");
      expect(pattern).toMatchObject({
        EventPattern: {
          detail: {
            eventName: ["DeleteParameters"],
            requestParameters: { names: [{ prefix: PATH_PREFIX }] },
          },
        },
      });
      // And emphatically NOT the singular key, which would never match a batch call.
      const detail = pattern.EventPattern as {
        detail: { requestParameters: Record<string, unknown> };
      };
      expect(detail.detail.requestParameters.name).toBeUndefined();
    });

    it("sends both rules to one audit log group", () => {
      const logGroup = Object.entries(
        template.findResources("AWS::Logs::LogGroup"),
      ).find(([, g]) =>
        String(g.Properties?.LogGroupName ?? "").includes(
          "databricks-parameter-writes",
        ),
      );
      expect(logGroup).toBeDefined();
      // A year, not a month: the record an investigation reads to decide whether a
      // write was a repoint has to outlive the incident.
      expect(logGroup![1].Properties.RetentionInDays).toBe(365);

      const targets = rules().flatMap(
        (r) => r.Properties.Targets as Array<{ Arn: unknown }>,
      );
      expect(targets).toHaveLength(2);
      for (const target of targets) {
        expect(JSON.stringify(target.Arn)).toContain(logGroup![0]);
      }
    });

    // The allowlist lives in the metric filter, not the event pattern, and this is
    // the reason: `anything-but` in an EventBridge pattern does NOT match an ABSENT
    // key, so a writer with no sessionContext (an IAM user, a root call) would have
    // been silently exempted — the most suspicious principal of the set.
    const filterPattern = (): string => {
      const filter = Object.values(
        template.findResources("AWS::Logs::MetricFilter"),
      ).find((f) =>
        JSON.stringify(f.Properties?.FilterPattern ?? "").includes(
          "sessionIssuer",
        ),
      );
      expect(filter).toBeDefined();
      return JSON.stringify(filter!.Properties.FilterPattern);
    };

    it("exempts the sources-API role by token, not by a hard-coded name", () => {
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      const apiRoleId: string = apiFn!.Properties.Role["Fn::GetAtt"][0];
      // A Ref to the role resolves to its NAME at deploy time. A literal would be
      // wrong the first time the role is replaced.
      expect(filterPattern()).toContain(apiRoleId);
    });

    it("exempts the CDK CloudFormation execution role", () => {
      expect(filterPattern()).toContain("cdk-hnb659fds-cfn-exec-role-");
    });

    it("treats an absent sessionIssuer as unexpected, not as exempt", () => {
      // Load-bearing: a comparison against a missing field is false, so without the
      // NOT EXISTS clause the `!=` terms alone would exempt every principal that has
      // no assumed-role session.
      const pattern = filterPattern();
      expect(pattern).toContain(
        "$.detail.userIdentity.sessionContext.sessionIssuer.userName NOT EXISTS",
      );
      // Disjunctive with the allowlist, so either condition alarms.
      expect(pattern).toContain("||");
    });

    it("alarms on the metric the filter emits", () => {
      template.hasResourceProperties("AWS::CloudWatch::Alarm", {
        AlarmName: "coa-dev-databricks-unexpected-parameter-write",
        Namespace: "COA/Sources",
        MetricName: "UnexpectedConnectorParameterWrite",
        Threshold: 1,
        ComparisonOperator: "GreaterThanOrEqualToThreshold",
        // A write is a discrete event, so "no data" is the normal state.
        TreatMissingData: "notBreaching",
      });
    });

    // So an operator reading the alarm knows what its silence means.
    it("says in its own description that no trail means no detection", () => {
      const alarm = Object.values(
        template.findResources("AWS::CloudWatch::Alarm"),
      ).find(
        (a) =>
          a.Properties?.AlarmName ===
          "coa-dev-databricks-unexpected-parameter-write",
      );
      const description = String(alarm!.Properties.AlarmDescription);
      expect(description).toContain("CloudTrail trail");
      expect(description).toContain("this stack does not create");
      expect(description).toContain("only");
      // The runbook action, so the alarm is actionable without a second lookup.
      expect(description).toContain("credentialSecretArn");
    });

    // Notifying nobody is a deliberate round-one seam (see the next test), but being on
    // no dashboard as well left the one alarm whose threat has no other signal visible
    // only to whoever thought to open the CloudWatch console.
    it("shows the alarm on the stack's OE dashboard, since it notifies nobody", () => {
      const [alarmId] = Object.entries(
        template.findResources("AWS::CloudWatch::Alarm"),
      ).find(
        ([, a]) =>
          a.Properties?.AlarmName ===
          "coa-dev-databricks-unexpected-parameter-write",
      )!;
      const dashboard = Object.values(
        template.findResources("AWS::CloudWatch::Dashboard", {
          Properties: { DashboardName: "coa-dev-sources" },
        }),
      );
      expect(dashboard).toHaveLength(1);
      // The widget carries the alarm's ARN, so the body names its logical id.
      expect(JSON.stringify(dashboard[0].Properties.DashboardBody)).toContain(
        alarmId,
      );
    });

    it("carries no alarm action when the deployment supplies none", () => {
      // `alarmAction` is undefined in bin/app.ts (round one). The detection must
      // record and alarm anyway rather than being blocked on a notification channel.
      const alarm = Object.values(
        template.findResources("AWS::CloudWatch::Alarm"),
      ).find(
        (a) =>
          a.Properties?.AlarmName ===
          "coa-dev-databricks-unexpected-parameter-write",
      );
      expect(alarm!.Properties.AlarmActions).toBeUndefined();
    });

    it("wires the action through the moment one is supplied", () => {
      const app = new cdk.App({ context: TEST_CONTEXT });
      const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "TestStorage", {
        network,
        env: TEST_ENV,
      });
      const actioned = Template.fromStack(
        new SourcesStack(app, "TestSources", {
          network,
          storage,
          env: TEST_ENV,
          alarmAction: {
            addAlarmActions: ({ alarm }) => {
              alarm.addAlarmAction({
                bind: () => ({
                  alarmActionArn:
                    "arn:aws:sns:us-east-1:123456789012:stub-topic",
                }),
              });
            },
          },
        }),
      );
      const alarm = Object.values(
        actioned.findResources("AWS::CloudWatch::Alarm"),
      ).find(
        (a) =>
          a.Properties?.AlarmName ===
          "coa-dev-databricks-unexpected-parameter-write",
      );
      expect(alarm!.Properties.AlarmActions).toEqual([
        "arn:aws:sns:us-east-1:123456789012:stub-topic",
      ]);
    });
  });

  describe("Discovery Athena workgroup", () => {
    // Both Athena clients omit `WorkGroup` when the variable is empty, which lands
    // every SHOW/DESCRIBE in the account's `primary` workgroup — shared, so
    // neither attributable nor independently limitable. The Databricks sub-type
    // makes discovery a per-table fan-out against a COA-operated connector, so the
    // cost and the concurrency become COA's.
    it("creates one deployment-scoped workgroup, not one per namespace", () => {
      const workgroups = Object.values(
        template.findResources("AWS::Athena::WorkGroup"),
      );
      expect(workgroups).toHaveLength(1);
      expect(workgroups[0].Properties.Name).toBe("coa-dev-sources-discovery");
      expect(
        workgroups[0].Properties.WorkGroupConfiguration
          .PublishCloudWatchMetricsEnabled,
      ).toBe(true);
      // Not enforced: both clients pass `ResultConfiguration` explicitly, and
      // enforcing would override a location the caller chose per statement.
      expect(
        workgroups[0].Properties.WorkGroupConfiguration
          .EnforceWorkGroupConfiguration,
      ).toBe(false);
    });

    it("pins the discovery Lambda to it", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            ATHENA_WORKGROUP: "coa-dev-sources-discovery",
          }),
        },
      });
    });

    // ATHENA_WORKGROUP is the workgroup's NAME, not a CloudFormation reference, so
    // nothing orders the two without this.
    it("orders the workgroup before the function that names it", () => {
      const fnId = Object.entries(
        template.findResources("AWS::Lambda::Function"),
      ).find(([, f]) =>
        String(f.Properties?.FunctionName ?? "").endsWith(
          "sources-db-connector",
        ),
      )![0];
      const wgId = Object.keys(
        template.findResources("AWS::Athena::WorkGroup"),
      )[0];
      expect(template.toJSON().Resources[fnId].DependsOn as string[]).toContain(
        wgId,
      );
    });

    // The discovery role's shipped Athena statement is already `workgroup/*`, so
    // pinning a workgroup needs no policy change. Asserted so a later narrowing to
    // a named workgroup does not silently lose enum sampling.
    it("needs no policy change — the Athena scope is already workgroup/*", () => {
      const fn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((f) =>
        String(f.Properties?.FunctionName ?? "").endsWith(
          "sources-db-connector",
        ),
      );
      const roleId: string = fn!.Properties.Role["Fn::GetAtt"][0];
      const stmt = Object.values(template.findResources("AWS::IAM::Policy"))
        .filter((p) =>
          p.Properties.Roles?.some((r: { Ref?: string }) => r.Ref === roleId),
        )
        .flatMap(
          (p) =>
            p.Properties.PolicyDocument.Statement as Record<string, unknown>[],
        )
        .find((st) => st.Sid === "AthenaEnumSampling");
      expect(stmt!.Resource).toBe(
        "arn:aws:athena:us-east-1:123456789012:workgroup/*",
      );
    });
  });

  describe("Bulk Review Pipeline", () => {
    it("creates a SQS queue with SQS-managed encryption and 6-minute visibility timeout", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-queue$"),
        VisibilityTimeout: 360,
        SqsManagedSseEnabled: true,
      });
    });

    it("creates a DLQ with SQS-managed encryption and 14-day retention", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-dlq$"),
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
        SqsManagedSseEnabled: true,
      });
    });

    it("queue has a redrive policy pointing to the DLQ with maxReceiveCount=3", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-bulk-review-queue$"),
        RedrivePolicy: Match.objectLike({
          maxReceiveCount: 3,
        }),
      });
    });

    it("creates a worker Lambda with 5-minute timeout, ARM64, in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-bulk-review-worker$"),
        Runtime: "python3.12",
        Architectures: ["arm64"],
        Timeout: 300,
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("worker has a SQS event source mapping to the bulk review queue", () => {
      // batchSize=1 means SQS feeds one message per worker invocation,
      // matching the worker's per-message idempotency check.
      template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
        BatchSize: 1,
        EventSourceArn: Match.anyValue(),
        FunctionName: Match.anyValue(),
      });
    });

    it("worker can re-enqueue to its own queue: REVIEW_QUEUE_URL env + sqs:SendMessage grant", () => {
      // Self-continuation (#853): a source too large for one invocation pages
      // itself by re-enqueuing with a nextToken, so the worker needs both the
      // queue URL and SendMessage on it. The grant is checked against the
      // worker's own role — the API Lambda also holds SendMessage on this
      // queue, so an unscoped assertion would still pass with the worker's
      // grant removed.
      const queue = Object.entries(
        template.findResources("AWS::SQS::Queue"),
      ).find(([, q]) =>
        /sources-bulk-review-queue$/.test(q.Properties.QueueName),
      );
      const worker = Object.entries(
        template.findResources("AWS::Lambda::Function"),
      ).find(([, f]) =>
        /sources-bulk-review-worker$/.test(f.Properties.FunctionName),
      );
      expect(queue).toBeDefined();
      expect(worker).toBeDefined();
      const [queueId] = queue!;
      const [, workerFn] = worker!;

      // Ref on an AWS::SQS::Queue resolves to the queue URL.
      expect(
        workerFn.Properties.Environment.Variables.REVIEW_QUEUE_URL,
      ).toEqual({ Ref: queueId });

      const workerRoleId = workerFn.Properties.Role["Fn::GetAtt"][0];
      const workerCanSend = Object.values(
        template.findResources("AWS::IAM::Policy"),
      ).some(
        (p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === workerRoleId) &&
          p.Properties.PolicyDocument.Statement.some(
            (s: any) =>
              [s.Action].flat().includes("sqs:SendMessage") &&
              JSON.stringify(s.Resource).includes(queueId),
          ),
      );
      expect(workerCanSend).toBe(true);
    });

    it("worker can write the scan-history store: SOURCE_SCAN_JOBS_TABLE env + DynamoDB write grant", () => {
      // The worker appends a REVIEW audit row to the scan-jobs table on each
      // terminal approve/reject so the console's Scan History tab shows real
      // events. It therefore needs the table name in env AND write access.
      const worker = Object.entries(
        template.findResources("AWS::Lambda::Function"),
      ).find(([, f]) =>
        /sources-bulk-review-worker$/.test(f.Properties.FunctionName),
      );
      expect(worker).toBeDefined();
      const [, workerFn] = worker!;

      expect(
        workerFn.Properties.Environment.Variables.SOURCE_SCAN_JOBS_TABLE,
      ).toBeDefined();

      const workerRoleId = workerFn.Properties.Role["Fn::GetAtt"][0];
      const workerCanWriteDdb = Object.values(
        template.findResources("AWS::IAM::Policy"),
      ).some(
        (p: any) =>
          p.Properties.Roles?.some((r: any) => r.Ref === workerRoleId) &&
          p.Properties.PolicyDocument.Statement.some((s: any) =>
            [s.Action].flat().includes("dynamodb:PutItem"),
          ),
      );
      expect(workerCanWriteDdb).toBe(true);
    });
  });

  describe("Federated Catalog Role — Glue VPC connection", () => {
    // Regression guard: Glue managed/VPC federated connections require
    // ec2:CreateNetworkInterfacePermission (in addition to CreateNetworkInterface)
    // to attach the ENI to the managed service account. Without it MySQL/SQLServer
    // (Athena-federation) sources FAILED at query time with Athena
    // HIVE_METASTORE_ERROR / Glue "Unable to access VPC ... check the policies on
    // the IAM role" — confirmed live 2026-07-29 with valid networking + SG.
    it("grants the federated catalog role ec2:CreateNetworkInterfacePermission scoped to Glue", () => {
      // The grant lives in its own statement, scoped to network-interface/* AND
      // conditioned on ec2:AuthorizedService=glue.amazonaws.com so the role cannot
      // hand ENI-attach permission to an arbitrary service/account (least-privilege).
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "Ec2CreateNetworkInterfacePermissionForGlue",
              Action: "ec2:CreateNetworkInterfacePermission",
              Condition: {
                StringEquals: { "ec2:AuthorizedService": "glue.amazonaws.com" },
              },
            }),
          ]),
        },
      });
    });
  });

  describe("SSM Parameters", () => {
    it("publishes Lambda ARN to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Sources API Lambda ARN",
      });
    });

    it("publishes sources table name to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Sources DynamoDB table name",
      });
    });

    it("publishes source scan jobs table name to SSM", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Description: "Source scan jobs DynamoDB table name",
      });
    });

    // A Databricks source's credential-access role must trust TWO platform principals:
    // the connector's execution role, which reads the credential on every request, and
    // this one, which assumes the role once at registration to `DescribeSecret`. A
    // trust policy naming only the first deploys fine and then refuses the source
    // create with an `AccessDenied` naming no principal.
    it("publishes the sources-API execution ROLE arn, not just the function arn", () => {
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Type: "String",
        Name: "/coa/sources/api-role-arn",
        // GetAtt, never an ARN assembled by hand from the pinned name: the account
        // and partition come from the deployment.
        Value: { "Fn::GetAtt": [Match.anyValue(), "Arn"] },
      });
    });

    it("resolves that ARN from the sources-API function's own role", () => {
      // Guards against the parameter existing but pointing at some other role — the
      // failure mode a shape-only assertion would miss, and the one that produces a
      // trust policy naming a principal that never calls.
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      const apiRoleId: string = apiFn!.Properties.Role["Fn::GetAtt"][0];
      const param = Object.values(
        template.findResources("AWS::SSM::Parameter"),
      ).find((p) => p.Properties?.Name === "/coa/sources/api-role-arn");
      expect(param).toBeDefined();
      expect(param!.Properties.Value["Fn::GetAtt"][0]).toBe(apiRoleId);
    });

    /** The sources-API function's own execution role, as the template renders it. */
    const apiRole = (): Record<string, unknown> => {
      const apiFn = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ).find((fn) =>
        String(fn.Properties?.FunctionName ?? "").endsWith("sources-api"),
      );
      const apiRoleId: string = apiFn!.Properties.Role["Fn::GetAtt"][0];
      const role = template.findResources("AWS::IAM::Role")[apiRoleId];
      expect(role).toBeDefined();
      return role.Properties;
    };

    // The ARN above is copied by hand into every Databricks source's credential-access
    // role, and CloudFormation replaces a role on a RoleName or Path change — so a
    // generated name would move the ARN under every customer at once.
    it("pins the sources-API role's NAME, so the published ARN cannot move", () => {
      expect(apiRole().RoleName).toBe("coa-dev-sources-api-role");
    });

    it("keeps that name inside IAM's 64 and outside the reserved datasource-access prefix", () => {
      // A pinned name is a name that can be chosen wrongly: inside
      // `coa-dev-datasource-access-` the reserved-prefix aspect refuses the synth, and
      // over 64 IAM refuses the deploy. `{prefix}-{env}` is capped at 27 by this
      // stack's longest prefixed role name, so 16 more characters has ample room.
      const name = String(apiRole().RoleName);
      expect(name.length).toBeLessThanOrEqual(64);
      expect(name.startsWith("coa-dev-datasource-access-")).toBe(false);
    });

    // Env-LESS, matching `db-enrichment-role-arn` and `federation-provisioner-role-arn`,
    // its two remaining siblings — pinned here so the choice is a decision on record
    // rather than an accident. See the comment at the parameter for why the shared-name
    // debt is fail-closed for these three.
    //
    // `db-connector-role-arn` left this set: it and serve's runtime role are read by a
    // connector stack deployed from a separate CDK app, where they become IAM grants, so
    // there the shared name fails OPEN. A future change env-scoping the rest of the
    // platform's parameters starts by moving the three names below.
    it("keeps the remaining role parameters on one env-less convention", () => {
      const names = Object.values(template.findResources("AWS::SSM::Parameter"))
        .map((p) => String(p.Properties?.Name ?? ""))
        .filter((name) => name.endsWith("-role-arn"))
        .sort();
      expect(names).toEqual([
        "/coa/dev/sources/db-connector-role-arn",
        "/coa/sources/api-role-arn",
        "/coa/sources/db-enrichment-role-arn",
        "/coa/sources/federation-provisioner-role-arn",
      ]);
      for (const name of names) {
        if (name.endsWith("/db-connector-role-arn")) continue;
        expect(name).not.toContain("/dev/");
      }
    });
  });

  describe("CfnOutputs", () => {
    it("exports SourcesTableName", () => {
      expect(Object.keys(template.findOutputs("SourcesTableName")).length).toBe(
        1,
      );
    });

    it("exports SourceScanJobsTableName", () => {
      expect(
        Object.keys(template.findOutputs("SourceScanJobsTableName")).length,
      ).toBe(1);
    });

    it("exports SourcesApiFnArn", () => {
      expect(Object.keys(template.findOutputs("SourcesApiFnArn")).length).toBe(
        1,
      );
    });
  });

  describe("Telemetry — CloudWatch Dashboard and Alarms", () => {
    it("emits the facade OE dashboard plus the structured-scan dashboard, and no legacy SourcesScanDashboard", () => {
      // One facade dashboard + the #116 structured-scan dashboard.
      template.resourceCountIs("AWS::CloudWatch::Dashboard", 2);
      // Legacy dashboard name must be gone.
      const dashboards = template.findResources("AWS::CloudWatch::Dashboard", {
        Properties: { DashboardName: Match.stringLikeRegexp("sources-scan$") },
      });
      expect(Object.keys(dashboards)).toHaveLength(0);
    });

    it("still emits alarms for the sources pipeline (migrated to facade)", () => {
      // At least the pre-migration alarm count (8) survives the migration.
      const alarms = template.findResources("AWS::CloudWatch::Alarm");
      expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(8);
    });
  });

  describe("Structured Scan Dashboard (#116)", () => {
    // The dashboard body is a JSON string, so charted metric names are asserted
    // by substring. Each name is emitted by packages/sources — a rename on
    // either side silently darkens a widget, which is what this guards.
    const scanDashboardBody = (): string => {
      const dashboards = template.findResources("AWS::CloudWatch::Dashboard", {
        Properties: {
          DashboardName: Match.stringLikeRegexp("sources-structured-scan$"),
        },
      });
      const found = Object.values(dashboards);
      expect(found).toHaveLength(1);
      return JSON.stringify(found[0].Properties.DashboardBody);
    };

    it("names the dashboard <prefix>-sources-structured-scan", () => {
      template.hasResourceProperties("AWS::CloudWatch::Dashboard", {
        DashboardName: Match.stringLikeRegexp("sources-structured-scan$"),
      });
    });

    it("SEARCHes the COA/Sources namespace the Python emitter publishes to", () => {
      // The namespace is a contract between coa_sources.database.metrics
      // NAMESPACE and every SEARCH() here. Renaming one side darkens every
      // custom-metric widget silently, so pin the exact string.
      expect(scanDashboardBody()).toContain("COA/Sources");
    });

    it.each([
      "ScanDuration",
      "TablesDiscovered",
      "ConnectionValidation",
      "CatalogAssetWrites",
      "TablesApprovedByReview",
      "TablesRejectedByReview",
      "GlueApiThrottles",
    ])("charts the %s custom metric", (metricName) => {
      expect(scanDashboardBody()).toContain(metricName);
    });

    it("charts the acceptance-rate formula, not just the metric names", () => {
      // A typo in the MathExpression (e.g. dropped parens) would still pass the
      // metric-name substring checks above but render a broken widget. Pin the
      // exact formula and its label.
      const body = scanDashboardBody();
      expect(body).toContain("approved / (approved + rejected)");
      expect(body).toContain("Acceptance rate");
    });

    it("charts AWS/Bedrock latency and token metrics for the enrichment model", () => {
      const body = scanDashboardBody();
      expect(body).toContain("AWS/Bedrock");
      expect(body).toContain("InvocationLatency");
      expect(body).toContain("InputTokenCount");
      expect(body).toContain("OutputTokenCount");
    });

    it("charts Step Functions execution outcomes", () => {
      const body = scanDashboardBody();
      expect(body).toContain("AWS/States");
      expect(body).toContain("ExecutionsFailed");
      expect(body).toContain("ExecutionTime");
    });

    it("declares overlay match rate as pending #114 instead of charting a metric", () => {
      // Honest placeholder: no overlay code exists yet, so there is nothing to
      // chart. If this ever becomes a real metric, this assertion should fail.
      const body = scanDashboardBody();
      expect(body).toContain("pending #114");
      expect(body).not.toContain("OverlayMatch");
    });
  });

  describe("Database Connector Lambda (DbConnectorFn)", () => {
    it("attaches the shared Lambda SG and the dedicated Snowflake OCSP SG", () => {
      const functions = Object.values(
        template.findResources("AWS::Lambda::Function"),
      ) as any[];
      const fn = functions.find((candidate: any) =>
        String(candidate.Properties?.FunctionName ?? "").endsWith(
          "sources-db-connector",
        ),
      );

      expect(fn).toBeDefined();
      expect(fn.Properties.VpcConfig.SecurityGroupIds).toHaveLength(2);
      expect(
        JSON.stringify(fn.Properties.VpcConfig.SecurityGroupIds),
      ).toContain("DiscoveryOcspSG");
      for (const candidate of functions.filter(
        (item) => item !== fn && item.Properties?.VpcConfig,
      )) {
        expect(
          JSON.stringify(candidate.Properties.VpcConfig.SecurityGroupIds),
        ).not.toContain("DiscoveryOcspSG");
      }
    });

    it("has CONSUMER_QUERY_ROLE_ARN environment variable (from SSM)", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            CONSUMER_QUERY_ROLE_ARN: Match.anyValue(),
          }),
        },
      });
    });

    it("has LF_GRANTOR_ROLE_ARN environment variable", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-connector$"),
        Environment: {
          Variables: Match.objectLike({
            LF_GRANTOR_ROLE_ARN: Match.anyValue(),
          }),
        },
      });
    });
  });

  describe("Database Scan State Machine — SCAN_FAILED source update", () => {
    // The scan-failed branch writes three fields (status, updatedAt,
    // lastScanJobId) to the sources table instead of two. These assertions
    // pin the UpdateItem expression and the scanJobSK → lastScanJobId mapping
    // so a regression in the state machine definition fails the build.
    const stateMachineDefinition = (): string => {
      const machines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      // Serialize every state machine definition (incl. Fn::Join fragments)
      // so we can assert on the rendered Amazon States Language.
      return JSON.stringify(Object.values(machines));
    };

    it("writes lastScanJobId in the SCAN_FAILED source UpdateItem expression", () => {
      const definition = stateMachineDefinition();
      // The update expression sets status (#s), updatedAt (#u) and the new
      // lastScanJobId (#l) attribute.
      expect(definition).toContain("SET #s = :s, #u = :u, #l = :l");
      expect(definition).toContain("lastScanJobId");
      expect(definition).toContain("SCAN_FAILED");
    });

    it("maps scanJobSK from the state input into lastScanJobId", () => {
      const definition = stateMachineDefinition();
      // The lastScanJobId value (:l) is sourced from $.scanJobSK in the
      // execution input, not a literal.
      expect(definition).toContain("$.scanJobSK");
    });

    it("carries only the bounded issues fields out of preprocessing, never the full array (issue 104)", () => {
      const definition = stateMachineDefinition();
      // The preprocess resultSelector and the DDB write must reference the
      // bounded fields the handler now returns, not the unbounded issues array
      // that blew the 256 KB state-payload limit.
      expect(definition).toContain("issues_preview");
      expect(definition).toContain("issues_s3_key");
      expect(definition).toContain("preprocessingIssuesS3Key");
      expect(definition).toContain("preprocessingIssuesTruncated");
      // The raw unbounded array must not be persisted whole.
      expect(definition).not.toContain(
        "States.JsonToString($.preprocessResult.issues)",
      );
      // issues_truncated must persist as a SUBSTITUTED boolean ("BOOL.$"), not a
      // literal path. booleanFromJsonPath given a raw string emits {"BOOL":"$.x"}
      // in aws-cdk-lib 2.260.0, which CreateStateMachine rejects; the stringAt()
      // wrapper makes it "BOOL.$". Normalize escaped quotes before matching since
      // the definition is a JSON-stringified Fn::Join.
      const flat = definition.replace(/\\+"/g, '"');
      expect(flat).toContain('"BOOL.$":"$.preprocessResult.issues_truncated"');
      expect(flat).not.toMatch(/"BOOL":"\$\./);
    });
  });

  describe("Database Scan State Machine — re-scan marker", () => {
    it("passes IS_RESCAN from the execution input to the enrichment task", () => {
      const machines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      const definition = JSON.stringify(Object.values(machines));
      // The enrichment ECS container override reads the re-scan marker from the
      // execution input ($.isRescan) as the IS_RESCAN env var; on a re-scan of
      // an approved source this routes the terminal status to RESCAN_REVIEW.
      expect(definition).toContain("IS_RESCAN");
      expect(definition).toContain("$.isRescan");
    });
  });

  describe("Scan timeout terminal state", () => {
    // Fix A: the DbEnrichment EcsRunTask carries a CATCHABLE per-task timeout
    // (States.Timeout) so an over-long enrichment routes through the error
    // chain to SCAN_FAILED, instead of the un-catchable execution-level
    // ExecutionTimedOut that stranded the source in ENRICHING.
    it("DbEnrichment task has a catchable per-task TimeoutSeconds and the state machine caps 2 min higher", () => {
      const machines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      // The ASL is embedded as an escaped JSON string inside a Fn::Join;
      // strip the backslash escaping so the rendered States Language can be
      // matched directly. The enrichment timeout is configurable
      // (dbScanEnrichmentTimeoutMinutes); default 120 min → taskTimeout 7200 s
      // on DbEnrichment, and the db-scan state-machine `TimeoutSeconds` is that
      // + 2 min = 7320 s. 7320 is unique to this state machine's own timeout,
      // so assert on it to prove the taskTimeout < execution-timeout ordering
      // that keeps the catchable path firing first.
      const definition = JSON.stringify(Object.values(machines)).replace(
        /\\/g,
        "",
      );
      // Per-task deadline on DbEnrichment (default 120 min). Both values live
      // inside the backslash-stripped ASL: the task-level 7200 in the
      // DbEnrichment state, and the state-machine execution ceiling 7320
      // (task + 2 min), which keeps the catchable States.Timeout firing before
      // the un-catchable ExecutionTimedOut.
      expect(definition).toContain('"TimeoutSeconds":7200');
      expect(definition).toContain('"TimeoutSeconds":7320');
    });

    // Fix B: the reaper is the out-of-band backstop for execution-level aborts
    // that are not catchable in-machine.
    it("creates the db-scan reaper Lambda in VPC with SOURCES_TABLE", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-scan-reaper$"),
        Handler: "coa_sources.database.pipeline.reaper_handler.handler",
        Runtime: "python3.12",
        Architectures: ["arm64"],
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
        Environment: {
          Variables: Match.objectLike({
            SOURCES_TABLE: Match.anyValue(),
          }),
        },
      });
    });

    it("has an EventBridge rule on aws.states execution status change filtering TIMED_OUT/ABORTED/FAILED with a Lambda target", () => {
      template.hasResourceProperties("AWS::Events::Rule", {
        EventPattern: Match.objectLike({
          source: ["aws.states"],
          "detail-type": ["Step Functions Execution Status Change"],
          detail: Match.objectLike({
            status: ["TIMED_OUT", "ABORTED", "FAILED"],
          }),
        }),
        Targets: Match.arrayWith([Match.objectLike({ Arn: Match.anyValue() })]),
      });
    });

    it("scopes the reaper rule to the db-scan state machine ARN", () => {
      // The rule must fire only for the db-scan pipeline, not any state machine.
      const rules = template.findResources("AWS::Events::Rule");
      const reaperRule = Object.values(rules).find(
        (r: any) =>
          r.Properties?.EventPattern?.detail?.stateMachineArn !== undefined,
      ) as any;
      expect(reaperRule).toBeDefined();
      expect(
        JSON.stringify(
          reaperRule.Properties.EventPattern.detail.stateMachineArn,
        ),
      ).toContain("DbScanStateMachine");
    });

    // Build a fresh SourcesStack with an extra context override, so the
    // configurable enrichment timeout can be exercised without disturbing the
    // shared `template` from beforeAll.
    const synthWithContext = (extra: Record<string, unknown>): Template => {
      const app = new cdk.App({ context: { ...TEST_CONTEXT, ...extra } });
      const network = new NetworkStack(app, "CtxNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "CtxStorage", {
        network,
        env: TEST_ENV,
      });
      return Template.fromStack(
        new SourcesStack(app, "CtxSources", {
          network,
          storage,
          env: TEST_ENV,
        }),
      );
    };

    it("honors a custom dbScanEnrichmentTimeoutMinutes (task value + 2 min ceiling)", () => {
      const t = synthWithContext({ dbScanEnrichmentTimeoutMinutes: 30 });
      const machines = t.findResources("AWS::StepFunctions::StateMachine");
      const definition = JSON.stringify(Object.values(machines)).replace(
        /\\/g,
        "",
      );
      // 30 min → task 1800 s, state-machine ceiling 32 min → 1920 s.
      expect(definition).toContain('"TimeoutSeconds":1800');
      expect(definition).toContain('"TimeoutSeconds":1920');
    });

    it("rejects a non-positive / non-numeric dbScanEnrichmentTimeoutMinutes at synth", () => {
      expect(() =>
        synthWithContext({ dbScanEnrichmentTimeoutMinutes: 0 }),
      ).toThrow(/dbScanEnrichmentTimeoutMinutes must be a positive number/);
      expect(() =>
        synthWithContext({ dbScanEnrichmentTimeoutMinutes: "abc" }),
      ).toThrow(/dbScanEnrichmentTimeoutMinutes must be a positive number/);
    });
  });

  describe("VPC Configuration — All Lambdas in VPC (security baseline)", () => {
    it("DbScanTriggerFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-db-scan-trigger$"),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("SourcesDocDeletionCleanupFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-doc-deletion-cleanup$"),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("SourcesDocIngestionTriggerFn is deployed in VPC", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(
          ".*sources-doc-ingestion-trigger$",
        ),
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });
  });

  describe("KG Build Observability", () => {
    it("enables Container Insights on the kg-build cluster so task memory is measurable", () => {
      // Without this, a task killed with no exit code leaves no memory data and
      // an OOM cannot be confirmed or ruled out.
      const clusters = template.findResources("AWS::ECS::Cluster");
      const kgBuildCluster = Object.values(clusters).find((c: any) =>
        String(c.Properties?.ClusterName ?? "").includes(
          "sources-doc-kg-build-cluster",
        ),
      ) as any;

      expect(kgBuildCluster).toBeDefined();
      expect(kgBuildCluster.Properties.ClusterSettings).toEqual(
        expect.arrayContaining([
          { Name: "containerInsights", Value: "enabled" },
        ]),
      );
    });

    it("sets DEPENDENCY_LOG_LEVEL on the kg-build container so graphrag INFO logs are retained", () => {
      // graphrag-toolkit logs via stdlib logging; its per-batch pipeline line
      // (num_workers, job_sizes) is INFO and is the only report of effective
      // write parallelism.
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const kgBuildTaskDef = Object.values(taskDefs).find((t: any) =>
        t.Properties?.ContainerDefinitions?.some((c: any) =>
          String(c.Name ?? "").includes("sources-doc-kg-build"),
        ),
      ) as any;

      expect(kgBuildTaskDef).toBeDefined();
      const container = kgBuildTaskDef.Properties.ContainerDefinitions.find(
        (c: any) => String(c.Name ?? "").includes("sources-doc-kg-build"),
      );
      expect(container.Environment).toEqual(
        expect.arrayContaining([
          { Name: "DEPENDENCY_LOG_LEVEL", Value: "INFO" },
        ]),
      );
    });
  });

  describe("KG Build Task Role IAM Permissions", () => {
    it("Bedrock batch job actions are scoped to specific model ARNs, not wildcard", () => {
      const policies = template.findResources("AWS::IAM::Policy");
      const kgBuildPolicy = Object.values(policies).find((p: any) =>
        p.Properties?.PolicyDocument?.Statement?.some(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        ),
      ) as any;

      expect(kgBuildPolicy).toBeDefined();
      const batchJobStatement =
        kgBuildPolicy.Properties.PolicyDocument.Statement.find(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        );

      expect(batchJobStatement).toBeDefined();
      expect(batchJobStatement.Resource).not.toEqual(["*"]);
      const resourceStr = JSON.stringify(batchJobStatement.Resource);
      expect(resourceStr).toContain("foundation-model/*");
      expect(resourceStr).toContain("inference-profile/*");
    });

    it("Bedrock InvokeModel is scoped to specific model ARNs", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock:InvokeModel",
              Resource: Match.arrayWith([
                Match.stringLikeRegexp("arn:aws:bedrock:.*:foundation-model/"),
              ]),
            }),
          ]),
        },
      });
    });

    it("Bedrock batch job actions include all required management operations", () => {
      const policies = template.findResources("AWS::IAM::Policy");
      const kgBuildPolicy = Object.values(policies).find((p: any) =>
        p.Properties?.PolicyDocument?.Statement?.some(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        ),
      ) as any;

      expect(kgBuildPolicy).toBeDefined();
      const batchJobStatement =
        kgBuildPolicy.Properties.PolicyDocument.Statement.find(
          (stmt: any) =>
            Array.isArray(stmt.Action) &&
            stmt.Action.includes("bedrock:CreateModelInvocationJob"),
        );

      expect(batchJobStatement.Action).toEqual(
        expect.arrayContaining([
          "bedrock:CreateModelInvocationJob",
          "bedrock:GetModelInvocationJob",
          "bedrock:ListModelInvocationJobs",
          "bedrock:StopModelInvocationJob",
        ]),
      );
    });

    it("kg-build task role can publish guardrail metrics to COA/Guardrails", () => {
      // The screener emits decisions via PutMetricData (matching the task's
      // other custom metrics), so the task role needs this grant.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "cloudwatch:PutMetricData",
              Effect: "Allow",
              Resource: "*",
              Condition: {
                StringEquals: { "cloudwatch:namespace": "COA/Guardrails" },
              },
            }),
          ]),
        }),
      });
    });
  });

  describe("DB Enrichment Guardrail Wiring (#111 AC5/AC6)", () => {
    // Regression guard: the enrichment task ran UNGUARDED because
    // GUARDRAIL_SSM_PARAM was never set, so _resolve_guardrail_id() always
    // returned None and every Converse call went out without a guardrail.
    const enrichmentContainer = (): any => {
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const taskDef = Object.values(taskDefs).find((t: any) =>
        JSON.stringify(t.Properties?.Family ?? "").includes(
          "sources-db-enrichment-agent",
        ),
      ) as any;
      expect(taskDef).toBeDefined();
      return taskDef.Properties.ContainerDefinitions[0];
    };

    it("sets GUARDRAIL_SSM_PARAM on the enrichment container", () => {
      const env = enrichmentContainer().Environment as Array<{
        Name: string;
        Value: unknown;
      }>;
      const param = env.find((e) => e.Name === "GUARDRAIL_SSM_PARAM");
      expect(param).toBeDefined();
      expect(JSON.stringify(param!.Value)).toContain(
        "/bedrock/retrieval-guardrail-id",
      );
    });

    it("grants the enrichment task role bedrock:ApplyGuardrail", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock:ApplyGuardrail",
              Resource: Match.stringLikeRegexp("arn:aws:bedrock:.*guardrail/"),
            }),
          ]),
        }),
      });
    });

    it("grants the enrichment task role ssm:GetParameter on that exact param", () => {
      // Scoped to the one param, not the whole prefix — ApplyGuardrail is
      // useless without the id, and a wildcard here would leak every param.
      const policies = template.findResources("AWS::IAM::Policy");
      const statements = Object.values(policies).flatMap(
        (p: any) => p.Properties?.PolicyDocument?.Statement ?? [],
      );
      const ssmReads = statements.filter(
        (s: any) => s.Action === "ssm:GetParameter",
      );
      const guardrailRead = ssmReads.find((s: any) =>
        JSON.stringify(s.Resource).includes("/bedrock/retrieval-guardrail-id"),
      );
      expect(guardrailRead).toBeDefined();
      expect(JSON.stringify(guardrailRead.Resource)).not.toContain("*");
    });
  });

  describe("Guarded ECS task defs carry the deployment region", () => {
    // Regression guard: both task defs shipped with GUARDRAIL_SSM_PARAM /
    // RETRIEVAL_GUARDRAIL_ID but no region env var. ECS injects none, so
    // resolve_region() fell back to us-east-1 and ApplyGuardrail was DENIED
    // by the region-scoped IAM policy in every non-us-east-1 deployment —
    // graph_build screening then failed OPEN.
    const containerFor = (family: string): { Environment?: unknown[] } => {
      const taskDefs = template.findResources("AWS::ECS::TaskDefinition");
      const taskDef = Object.values(taskDefs).find((t) =>
        JSON.stringify(
          (t as { Properties?: { Family?: unknown } }).Properties?.Family ?? "",
        ).includes(family),
      ) as {
        Properties: { ContainerDefinitions: { Environment?: unknown[] }[] };
      };
      expect(taskDef).toBeDefined();
      return taskDef.Properties.ContainerDefinitions[0];
    };

    for (const family of [
      "sources-db-enrichment-agent",
      "sources-doc-kg-build",
    ]) {
      it(`${family} sets AWS_DEFAULT_REGION and BEDROCK_REGION to the stack region`, () => {
        const env = (containerFor(family).Environment ?? []) as {
          Name: string;
          Value: unknown;
        }[];
        for (const name of ["AWS_DEFAULT_REGION", "BEDROCK_REGION"]) {
          const entry = env.find((e) => e.Name === name);
          expect(entry).toBeDefined();
          expect(entry!.Value).toEqual({ Ref: "AWS::Region" });
        }
      });
    }
  });

  describe("Bedrock model IDs from deploy config (#94)", () => {
    const renderWithModels = (models: {
      bedrockChatModelId?: string;
      bedrockEmbedModelId?: string;
      bedrockEmbedDimensions?: number;
    }) => {
      const app = new cdk.App({ context: TEST_CONTEXT });
      const network = new NetworkStack(app, "MdlNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "MdlStorage", {
        network,
        env: TEST_ENV,
      });
      return Template.fromStack(
        new SourcesStack(app, "MdlSources", {
          network,
          storage,
          allowedOrigin: "https://test.example.com",
          env: TEST_ENV,
          ...models,
        }),
      );
    };

    const envOf = (t: Template, fnNamePattern: RegExp) => {
      const fn = Object.values(t.findResources("AWS::Lambda::Function")).find(
        (f) => fnNamePattern.test(String(f.Properties?.FunctionName ?? "")),
      );
      return (fn?.Properties?.Environment?.Variables ?? {}) as Record<
        string,
        string
      >;
    };

    it("builds the doc-ingestion inference-profile ARN from the configured chat model", () => {
      // An inlined `us.` profile assembled an ARN that does not exist outside
      // the US, and the trigger passes this string on to the KG-build container.
      const t = renderWithModels({
        bedrockChatModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      });
      const env = envOf(
        t,
        /sources-doc-trigger|documents-trigger|doc.*trigger/i,
      );
      expect(JSON.stringify(env.BEDROCK_MODEL_ARN)).toContain(
        "inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      );
    });

    it("sets the embedding model on the KG-build container so it stops using the Python default", () => {
      const t = renderWithModels({
        bedrockEmbedModelId: "cohere.embed-v4:0",
        bedrockEmbedDimensions: 512,
      });
      const taskDefs = Object.values(
        t.findResources("AWS::ECS::TaskDefinition"),
      );
      const envs = taskDefs.flatMap((d) =>
        (d.Properties?.ContainerDefinitions ?? []).map(
          (c: { Environment?: Array<{ Name: string; Value: unknown }> }) =>
            c.Environment ?? [],
        ),
      );
      const kgEnv = envs.find((e) =>
        e.some((v: { Name: string }) => v.Name === "BEDROCK_EMBED_MODEL_ID"),
      );
      expect(kgEnv).toBeDefined();
      const byName = Object.fromEntries(
        kgEnv!.map((v: { Name: string; Value: unknown }) => [v.Name, v.Value]),
      );
      expect(byName.BEDROCK_EMBED_MODEL_ID).toBe("cohere.embed-v4:0");
      expect(byName.BEDROCK_EMBED_DIMENSIONS).toBe("512");
    });

    it("uses a foundation-model ARN (empty account) for a bare in-region model id", () => {
      // Bare ids are explicitly supported (some models publish geo profiles for
      // only a subset of regions). A foundation model is AWS-owned, so its ARN
      // has NO account field — building an inference-profile ARN for it yields a
      // resource that does not exist and fails at extraction time.
      const t = renderWithModels({
        bedrockChatModelId: "anthropic.claude-haiku-4-5-20251001-v1:0",
      });
      const env = envOf(
        t,
        /sources-doc-trigger|documents-trigger|doc.*trigger/i,
      );
      const arn = JSON.stringify(env.BEDROCK_MODEL_ARN);
      expect(arn).toContain(
        "foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
      );
      expect(arn).not.toContain("inference-profile");
      // Empty account segment: ...:bedrock:<region>::foundation-model/...
      expect(arn).toContain("::foundation-model/");
    });

    it("defaults to the shared us. profile when no config is supplied", () => {
      const t = renderWithModels({});
      const env = envOf(
        t,
        /sources-doc-trigger|documents-trigger|doc.*trigger/i,
      );
      expect(JSON.stringify(env.BEDROCK_MODEL_ARN)).toContain(
        "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
      );
    });
  });

  describe("Preprocessing Lambda reserved concurrency (#48)", () => {
    it("reserves the default concurrency (5) when unset", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-doc-preprocessing$"),
        ReservedConcurrentExecutions: 5,
      });
    });

    it("omits the reservation when lambda_reserved_concurrency=0", () => {
      const app = new cdk.App({
        context: { ...TEST_CONTEXT, lambda_reserved_concurrency: 0 },
      });
      const network = new NetworkStack(app, "NoResNetwork", { env: TEST_ENV });
      const storage = new StorageStack(app, "NoResStorage", {
        network,
        env: TEST_ENV,
      });
      const t = Template.fromStack(
        new SourcesStack(app, "NoResSources", {
          network,
          storage,
          allowedOrigin: "https://test.example.com",
          env: TEST_ENV,
        }),
      );
      t.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-doc-preprocessing$"),
        ReservedConcurrentExecutions: Match.absent(),
      });
    });
  });

  describe("Preprocessing bucket authorization IAM", () => {
    // A caller-named bucket is authorized by its owner-set `{prefix}.namespace`
    // tag, checked in application code. IAM carries the part code cannot: an
    // explicit Deny on the platform's own buckets, which are knowable at synth.
    const preprocessingStatements = (): any[] => {
      const policies = template.findResources("AWS::IAM::Policy");
      const out: any[] = [];
      for (const [id, res] of Object.entries(policies)) {
        if (!id.includes("PreProcessing")) continue;
        out.push(...((res as any).Properties?.PolicyDocument?.Statement ?? []));
      }
      return out;
    };

    it("grants s3:GetBucketTagging so the authorizing tag can be read", () => {
      const acts = preprocessingStatements()
        .filter((st) => st.Effect === "Allow")
        .flatMap((st) => (Array.isArray(st.Action) ? st.Action : [st.Action]));
      expect(acts).toContain("s3:GetBucketTagging");
    });

    it("denies the platform's own buckets outright", () => {
      const deny = preprocessingStatements().find(
        (st) => st.Effect === "Deny" && st.Sid === "DenyPlatformOwnedBuckets",
      );
      expect(deny).toBeDefined();
      const acts = Array.isArray(deny.Action) ? deny.Action : [deny.Action];
      expect(acts).toContain("s3:GetObject");
      expect(acts).toContain("s3:ListBucket");
      // Three buckets, each as bucket ARN plus /* for its objects.
      expect(deny.Resource).toHaveLength(6);
    });

    it("grants sources-api s3:GetBucketTagging for the registration check", () => {
      // Registration verifies the tag up front so the customer is told at create
      // time rather than by a failed scan. sources-api reads no customer object
      // data: its only s3:GetObject is the narrowly-scoped re-scan backup metadata
      // read (`rescan-backup/*`, system-written table/column forms) that the
      // tables API needs for the diff panel. Any GetObject NOT scoped to
      // rescan-backup — a broad grant or one over `raw/*` — is the regression this
      // guards against.
      const policies = template.findResources("AWS::IAM::Policy");
      let sawTagRead = false;
      let sawCustomerObjectRead = false;
      for (const [id, res] of Object.entries(policies)) {
        if (!id.includes("SourcesApi")) continue;
        for (const st of (res as any).Properties?.PolicyDocument?.Statement ??
          []) {
          if (st.Effect !== "Allow") continue;
          const acts = Array.isArray(st.Action) ? st.Action : [st.Action];
          if (acts.includes("s3:GetBucketTagging")) sawTagRead = true;
          if (acts.includes("s3:GetObject")) {
            // Allow only the system-written re-scan backup blob; anything else is
            // a customer-object read this role must not have.
            const resStr = JSON.stringify(st.Resource ?? "");
            if (!resStr.includes("rescan-backup")) sawCustomerObjectRead = true;
          }
        }
      }
      expect(sawTagRead).toBe(true);
      expect(sawCustomerObjectRead).toBe(false);
    });

    it("denies the same actions it allows, GetBucketTagging included", () => {
      const policies = template.findResources("AWS::IAM::Policy");
      let deny: any;
      for (const [id, res] of Object.entries(policies)) {
        if (!id.includes("PreProcessing")) continue;
        for (const st of (res as any).Properties?.PolicyDocument?.Statement ??
          []) {
          if (st.Sid === "DenyPlatformOwnedBuckets") deny = st;
        }
      }
      expect(deny).toBeDefined();
      const acts = Array.isArray(deny.Action) ? deny.Action : [deny.Action];
      // A Deny narrower than the Allow it guards lets a future action escape it.
      expect(acts).toEqual(
        expect.arrayContaining([
          "s3:GetObject",
          "s3:GetObjectTagging",
          "s3:ListBucket",
          "s3:GetBucketTagging",
        ]),
      );
    });

    it("does NOT deny the sources data bucket, which uploads depend on", () => {
      const deny = preprocessingStatements().find(
        (st) => st.Effect === "Deny" && st.Sid === "DenyPlatformOwnedBuckets",
      );
      const rendered = JSON.stringify(deny.Resource);
      expect(rendered).not.toContain("SourcesDataBucket");
    });
  });

  describe("Preprocessing Textract IAM", () => {
    // The table-extraction path (enable_table_extraction) calls
    // textract:AnalyzeDocument; the scanned-PDF path calls DetectDocumentText.
    // The preprocessing role must grant BOTH or the tables path fails at runtime
    // with AccessDeniedException (unit tests mock the Textract client and cannot
    // catch a missing IAM grant — only this template assertion does).
    it("grants the preprocessing role textract:AnalyzeDocument and DetectDocumentText", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: "Allow",
              Action: Match.arrayWith([
                "textract:DetectDocumentText",
                "textract:AnalyzeDocument",
              ]),
            }),
          ]),
        },
      });
    });
  });

  describe("Recurring rescan scheduling", () => {
    it("creates a dedicated EventBridge schedule group", () => {
      template.hasResourceProperties("AWS::Scheduler::ScheduleGroup", {
        Name: Match.stringLikeRegexp("sources-rescan"),
      });
    });

    it("creates a scheduler execution role assumed by scheduler.amazonaws.com", () => {
      template.hasResourceProperties("AWS::IAM::Role", {
        AssumeRolePolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "sts:AssumeRole",
              Principal: { Service: "scheduler.amazonaws.com" },
            }),
          ]),
        },
      });
    });
  });

  describe("Scan pipeline (Step Functions)", () => {
    it("passes the no-drift reviewNeeded signal into the enrichment task env", () => {
      // The enrichment ECS task reads RESCAN_REVIEW_NEEDED to choose the terminal
      // status: a re-scan with no drift (and no carried-forward orphaned tables)
      // returns to APPROVED instead of RESCAN_REVIEW. It is wired from discovery's
      // result. DbDiscovery is a LambdaInvoke without payloadResponseOnly, so the
      // signal is under the Lambda envelope at $.discoveryResult.Payload.reviewNeeded;
      // reading it off $.discoveryResult directly fails the scan at runtime. The
      // container override lives inside the state machine DefinitionString, so
      // assert against that.
      const stateMachines = template.findResources(
        "AWS::StepFunctions::StateMachine",
      );
      const definitions = JSON.stringify(stateMachines);
      expect(definitions).toContain("RESCAN_REVIEW_NEEDED");
      expect(definitions).toContain("$.discoveryResult.Payload.reviewNeeded");
    });
  });

  describe("Recurring rescan scheduling IAM", () => {
    it("grants the API Lambda scoped schedule-management + PassRole permissions", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "ManageRescanSchedules",
              Action: Match.arrayWith([
                "scheduler:CreateSchedule",
                "scheduler:UpdateSchedule",
                "scheduler:DeleteSchedule",
                "scheduler:GetSchedule",
              ]),
            }),
            Match.objectLike({
              Sid: "PassRescanSchedulerRole",
              Action: "iam:PassRole",
              Condition: {
                StringEquals: {
                  "iam:PassedToService": "scheduler.amazonaws.com",
                },
              },
            }),
          ]),
        },
      });
    });

    it("wires the schedule env vars onto the Sources API Lambda", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-api$"),
        Environment: {
          Variables: Match.objectLike({
            RESCAN_SCHEDULE_GROUP: Match.anyValue(),
            RESCAN_TARGET_ARN: Match.anyValue(),
            RESCAN_SCHEDULE_ROLE_ARN: Match.anyValue(),
          }),
        },
      });
    });
  });

  describe("Event-driven rescan (Glue changes)", () => {
    it("creates the Glue-event queue and DLQ, both encrypted at rest", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp("sources-glue-event-queue"),
        SqsManagedSseEnabled: true,
      });
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp("sources-glue-event-dlq"),
        SqsManagedSseEnabled: true,
      });
    });

    it("allows EventBridge to send to the queue, scoped to this account", () => {
      template.hasResourceProperties("AWS::SQS::QueuePolicy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "AllowEventBridgeSend",
              Action: "sqs:SendMessage",
              Principal: { Service: "events.amazonaws.com" },
              Condition: {
                StringEquals: { "aws:SourceAccount": Match.anyValue() },
              },
            }),
          ]),
        },
      });
    });

    it("creates the consumer Lambda with an SQS event source", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-glue-event-rescan$"),
        Handler: "coa_sources.database.glue_event_rescan_handler.handler",
      });
    });

    // The handler returns per-message failures. Without this the whole batch is
    // treated as succeeded, so one bad message is deleted along with the nine
    // good ones and the DLQ never sees it.
    it("reports partial batch failures so a single bad message is retried alone", () => {
      template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
        FunctionResponseTypes: ["ReportBatchItemFailures"],
        BatchSize: 10,
      });
    });

    // Each message can start a scan, so unbounded fan-out races the status lock.
    it("caps consumer concurrency like the other scan-triggering queues", () => {
      template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
        FunctionResponseTypes: ["ReportBatchItemFailures"],
        ScalingConfig: { MaximumConcurrency: 5 },
      });
    });

    // Every other queue and Lambda in the stack is monitored; these were not.
    it("alarms on the consumer and its DLQ", () => {
      const names = Object.values(
        template.findResources("AWS::CloudWatch::Alarm"),
      ).map((a) =>
        String(
          (a as { Properties?: { AlarmName?: string } }).Properties
            ?.AlarmName ?? "",
        ),
      );
      for (const needle of [
        "glue-event-rescan-Fault-Count",
        "glue-event-rescan-Throttled-Count",
        "glue-event-dlq-DLQ-Queue-Message-Count",
      ]) {
        expect(names.some((n) => n.includes(needle))).toBe(true);
      }
    });

    // 6x the consumer's 30s timeout, so a redrive cannot race a still-running
    // invocation.
    it("sets the queue visibility timeout to 6x the consumer timeout", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp("sources-glue-event-queue"),
        VisibilityTimeout: 180,
      });
    });

    // Without it the scan ran and then api_response raised, so every event
    // re-scan logged a failure while having succeeded. Found on a live deploy.
    it("gives the consumer Lambda ALLOWED_ORIGIN so api_response can build a response", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-glue-event-rescan$"),
        Environment: {
          Variables: Match.objectLike({ ALLOWED_ORIGIN: Match.anyValue() }),
        },
      });
    });

    it("grants the API Lambda scoped Glue-event rule management", () => {
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: "ManageGlueEventRules",
              Action: Match.arrayWith([
                "events:PutRule",
                "events:PutTargets",
                "events:DeleteRule",
                "events:RemoveTargets",
                "events:DescribeRule",
              ]),
            }),
          ]),
        },
      });
    });

    it("wires the Glue-event env vars onto the Sources API Lambda", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("sources-api$"),
        Environment: {
          Variables: Match.objectLike({
            GLUE_EVENT_QUEUE_ARN: Match.anyValue(),
            GLUE_EVENT_RULE_PREFIX: Match.anyValue(),
          }),
        },
      });
    });
  });

  describe("Source Deletion Worker (async database-source delete)", () => {
    it("gives the delete queue a visibility timeout above the worker timeout", () => {
      // 16 min > the worker's 15 min. Lower would let SQS redeliver a message
      // whose cleanup is still running, duplicating the teardown mid-flight.
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-delete-queue$"),
        VisibilityTimeout: 960,
        SqsManagedSseEnabled: true,
      });
    });

    it("creates a DLQ with 14-day retention and a 3-attempt redrive policy", () => {
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-delete-dlq$"),
        MessageRetentionPeriod: 14 * 24 * 60 * 60,
        SqsManagedSseEnabled: true,
      });
      template.hasResourceProperties("AWS::SQS::Queue", {
        QueueName: Match.stringLikeRegexp(".*sources-delete-queue$"),
        RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
      });
    });

    it("creates the worker with the 15-minute timeout the API cannot give it, ARM64, in VPC", () => {
      // The reason the worker exists: sources-api is capped at 30s and DataZone
      // asset teardown scales with table count.
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-delete-worker$"),
        Runtime: "python3.12",
        Architectures: ["arm64"],
        Timeout: 900,
        Handler: "coa_sources.api.source_deletion_worker.handler",
        VpcConfig: Match.objectLike({
          SubnetIds: Match.anyValue(),
          SecurityGroupIds: Match.anyValue(),
        }),
      });
    });

    it("reports partial batch failures so one stuck source does not redrive its siblings", () => {
      template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
        FunctionResponseTypes: ["ReportBatchItemFailures"],
        BatchSize: 1,
      });
    });

    it("gives the API the queue URL so it hands off instead of deleting inline", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-api$"),
        Environment: Match.objectLike({
          Variables: Match.objectLike({
            SOURCE_DELETE_QUEUE_URL: Match.anyValue(),
          }),
        }),
      });
    });

    it("gives the worker every table its cleanup touches", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-delete-worker$"),
        Environment: Match.objectLike({
          Variables: Match.objectLike({
            SOURCES_TABLE: Match.anyValue(),
            SOURCE_SCAN_JOBS_TABLE: Match.anyValue(),
            NAMESPACES_TABLE: Match.anyValue(),
            PROJECT_ACCESS_ROLE_ARN: Match.anyValue(),
          }),
        }),
      });
    });

    it("gives the worker RESOURCE_PREFIX so it derives catalog names itself instead of trusting the message", () => {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp(".*sources-delete-worker$"),
        Environment: Match.objectLike({
          Variables: Match.objectLike({ RESOURCE_PREFIX: "coa-dev-" }),
        }),
      });
    });

    it("alarms the delete queue + DLQ so an orphaned teardown pages, not accumulates silently", () => {
      // monitorQueueWithDlq adds a DLQ max-size alarm dimensioned by the DLQ
      // QueueName. Assert at least one CloudWatch alarm references the
      // sources-delete-dlq — without it a failed async delete piles into the DLQ
      // for 14 days with no signal.
      const alarms = template.findResources("AWS::CloudWatch::Alarm");
      const referencesDeleteDlq = Object.values(alarms).some((alarm) =>
        JSON.stringify(alarm).includes("sources-delete-dlq"),
      );
      expect(referencesDeleteDlq).toBe(true);
    });
  });
  /**
   * The reserved `{prefix}-{env}-datasource-access-*` name is the WHOLE bound on
   * which roles COA will assume — the three grants above carry no account
   * restriction on purpose — so a COA-internal role named under it would be
   * assumable by the discovery role, the enrichment task, the sources API and the
   * Databricks connector alike.
   *
   * The mechanism that enforces it app-wide lives in
   * `infra/test/aspects/reserved-role-prefix.test.ts`; this is the same property
   * read off the real synthesised template, which is where a regression appears.
   */
  describe("No COA-internal role falls under the reserved prefix", () => {
    it("names no role in this template under coa-dev-datasource-access-", () => {
      const roles = Object.values(template.findResources("AWS::IAM::Role")).map(
        (r) =>
          `${String(r.Properties?.Path ?? "/")}${String(r.Properties?.RoleName ?? "")}`.replace(
            /^\//,
            "",
          ),
      );
      expect(roles.length).toBeGreaterThan(0);
      for (const role of roles) {
        expect(role.startsWith("coa-dev-datasource-access-")).toBe(false);
      }
    });
  });
});

/**
 * Catalog-name uniqueness is a security invariant for the Databricks sub-type: one
 * connector Lambda serves every Databricks source and resolves which warehouse and
 * which credential secret to use from the Athena catalog name it was invoked under,
 * so two sources aliasing to one name alias to one credential.
 *
 * `build_catalog_name` truncates to 41 characters AFTER concatenation, so the digest
 * is what gets cut — only 2 hex digits survive at a 36-character sanitised prefix
 * and none at 38. Its own unit tests cannot catch this, because the prefix is a
 * DEPLOYMENT parameter rather than an input, which is why the check is at synth.
 */
describe("Athena catalog-name prefix budget", () => {
  /** Synthesise the sources stack under a given prefix/env, returning its warnings. */
  const synth = (context: Record<string, unknown>): string[] => {
    const app = new cdk.App({ context: { ...TEST_CONTEXT, ...context } });
    const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
    const storage = new StorageStack(app, "TestStorage", {
      network,
      env: TEST_ENV,
    });
    const stack = new SourcesStack(app, "TestSources", {
      network,
      storage,
      env: TEST_ENV,
    });
    // Annotations land as construct metadata; `Annotations.fromStack` is the
    // assertion-library view of them and is what distinguishes "warned" from
    // "threw" — the whole point of the two thresholds below.
    return Annotations.fromStack(stack)
      .findWarning("*", Match.anyValue())
      .map((w) => String(w.entry.data));
  };

  const digestWarnings = (context: Record<string, unknown>): string[] =>
    synth(context).filter((message) =>
      message.includes("Sanitised resource prefix"),
    );

  it("is silent for the default prefix", () => {
    // `coa` + `dev` sanitises to `coadev` — 6 characters, whole digest intact.
    expect(digestWarnings({})).toEqual([]);
  });

  // 22, not 19. `build_catalog_name` prepends its second `ds_` only when the name would
  // not start with a letter, and `safe_prefix` is `[a-z0-9]*`, so the 6-character
  // overhead applies only to a DIGIT-leading prefix. For a letter-leading one the
  // overhead is 3 and the whole digest survives to 22.
  it("is silent at 22 sanitised characters when the prefix starts with a letter", () => {
    // 19 + len("dev") = 22 → 41 - 3 - 22 = 16.
    expect(
      digestWarnings({ resource_prefix: "abcdefghijklmnopqrs", env: "dev" }),
    ).toEqual([]);
  });

  it("warns at 23 when the prefix starts with a letter", () => {
    const warnings = digestWarnings({
      resource_prefix: "abcdefghijklmnopqrst",
      env: "dev",
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("15 of 16 characters survive");
    expect(warnings[0]).toContain("starts with a letter");
  });

  // The digit-leading branch, where the second `ds_` really is prepended. Reachable:
  // `safe_prefix` keeps digits, and S3 and IAM both accept a digit-leading name.
  it("is silent at 19 but warns at 20 when the prefix starts with a digit", () => {
    // 16 + len("dev") = 19 → 41 - 6 - 19 = 16.
    expect(
      digestWarnings({ resource_prefix: "9bcdefghijklmnop", env: "dev" }),
    ).toEqual([]);

    // 17 + len("dev") = 20 → 41 - 6 - 20 = 15.
    const warnings = digestWarnings({
      resource_prefix: "9bcdefghijklmnopq",
      env: "dev",
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('"9bcdefghijklmnopqdev" is 20 characters');
    expect(warnings[0]).toContain(
      'starts with a digit, so build_catalog_name prepends a second "ds_"',
    );
    expect(warnings[0]).toContain(
      "at most 19 keep the whole 16-character digest",
    );
    expect(warnings[0]).toContain("15 of 16 characters survive");
    expect(warnings[0]).toContain("alias to one credential");
    expect(warnings[0]).toContain(
      'RESOURCE_PREFIX is "9bcdefghijklmnopq-dev-"',
    );
  });

  /**
   * Why there is no hard-failure case to test: the collision-prone range cannot be
   * reached. The longest prefixed name in this stack is a 35-character `iam.Role` and
   * IAM caps a role name at 64, which bounds `len(prefix) + len(env)` at 27, so the
   * digest keeps at least 8 characters even in the digit-leading branch. These cases
   * pin that ceiling from both sides: shorten that role name and the last one starts
   * passing, which is when this reasoning needs revisiting.
   */
  it("keeps 11 digest characters at the longest deployable letter-leading prefix", () => {
    // 24 + len("dev") = 27 sanitised, the maximum this stack's own names allow.
    const warnings = digestWarnings({
      resource_prefix: "a".repeat(24),
      env: "dev",
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("11 of 16 characters survive");
  });

  it("keeps 8 digest characters in the worst deployable case, digit-leading", () => {
    const warnings = digestWarnings({
      resource_prefix: `9${"a".repeat(23)}`,
      env: "dev",
    });
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("8 of 16 characters survive");
  });

  it("fails on IAM's role-name limit, not on this check, one character longer", () => {
    // 28 sanitised would keep only 10 (letter-leading) or 7 (digit-leading) digest
    // characters — but the deployment cannot synthesise at all, and the error names the
    // role rather than the prefix budget. This is also why `mycompany-analytics` +
    // `production` (28) was never a deployment this check could have bricked.
    expect(() =>
      synth({ resource_prefix: "a".repeat(25), env: "dev" }),
    ).toThrow(/Invalid roleName/);
  });

  it("counts the sanitised length, not the raw prefix", () => {
    // `[^a-z0-9]` goes before the budget applies, exactly as `build_catalog_name`
    // does. RESOURCE_PREFIX here is `a-b-c-d-e-f-g-h-i-j-prod-`, 25 raw characters
    // but only 14 sanitised — so a check written against the raw string would warn
    // about a prefix that is entirely fine. (Case folding is not exercised: an
    // uppercase prefix never reaches this check, because S3 rejects the bucket name
    // first.)
    expect(
      digestWarnings({ resource_prefix: "a-b-c-d-e-f-g-h-i-j", env: "prod" }),
    ).toEqual([]);
  });
});
