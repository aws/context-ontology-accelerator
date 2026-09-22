// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
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
      const toArr = (x: string | string[]): string[] => (Array.isArray(x) ? x : [x]);
      const glue = (suffix: string) => `arn:aws:glue:us-east-1:123456789012:${suffix}`;

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
        expect(actions).toEqual(expect.arrayContaining(["glue:GetCatalog", "glue:GetCatalogs"]));
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
            CONSUMER_QUERY_ROLE_SSM_PARAM: Match.stringLikeRegexp(
              ".*/serve/runtime-role-arn$",
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

    it("grants assume on customer datasource-access roles in both consumers", () => {
      // The discovery Lambda and the enrichment task each get their own grant.
      expect(assumeStatements().length).toBe(2);
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
        out.push(
          ...((res as any).Properties?.PolicyDocument?.Statement ?? []),
        );
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
        for (const st of (res as any).Properties?.PolicyDocument?.Statement ?? []) {
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
        for (const st of (res as any).Properties?.PolicyDocument?.Statement ?? []) {
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
});
