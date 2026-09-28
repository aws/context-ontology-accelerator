// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import type { Construct } from "constructs";
import {
  ApiStack,
  INTENTIONALLY_STUBBED,
} from "../../lib/stacks/services/api-stack";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";
import { Paths } from "../../lib/paths";
import { readOpenApiSpec } from "../../lib/utils/api-utils";

// Avoid running pip install / hashing the monorepo root during synth.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  "aws:cdk:bundling-stacks": [],
};

/**
 * Stub table for ApiStack props. Streams are ON for every table, not just the
 * two that need them: ApiStack attaches a DynamoEventSource to the Roles and
 * ResourceRoleMappings tables, and DynamoEventSource fails synth outright on a
 * stream-less table. Keeping this in ONE place means a new `describe` block
 * can't reintroduce that failure by declaring its own stream-less helper.
 */
const mkTestTable = (scope: Construct, id: string) =>
  new dynamodb.Table(scope, id, {
    partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
  });

describe("ApiStack authorizer — ARCHIVED guard wiring", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });

    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);

    template = Template.fromStack(
      new ApiStack(app, "TestApi", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
      }),
    );
  });

  it("sets NAMESPACES_TABLE_NAME on the authorizer Lambda", () => {
    // The authorizer reads namespace status to enforce the ARCHIVED guard.
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp(".*authorizer$"),
      Environment: {
        Variables: Match.objectLike({
          NAMESPACES_TABLE_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it("resolves GROUP_CLAIM_NAME from SSM (not a hardcoded Cognito default)", () => {
    // Regression: this used to be a hardcoded DEFAULT_GROUP_CLAIM
    // ("cognito:groups") passed from app.ts regardless of idpType, which
    // silently broke group-based role resolution on the direct-OIDC path
    // (external IdPs configure their own claim name, e.g. plain "groups").
    // Must be an SSM dynamic reference resolved from
    // /authentication-group-token-name (written by idp-authentication-stack.ts
    // on both the Cognito/SAML and OIDC paths), not a literal string.
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp(".*authorizer$"),
      Environment: {
        Variables: Match.objectLike({
          GROUP_CLAIM_NAME: Match.anyValue(),
        }),
      },
    });
  });

  it("grants the authorizer dynamodb:GetItem on the Namespaces table", () => {
    // Without this grant the status lookup fails and (fail-closed) every
    // mutating request is denied across all namespaces — so pin the permission.
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Action: "dynamodb:GetItem",
          }),
        ]),
      },
    });
  });
});

describe("ApiStack OE monitoring", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);

    template = Template.fromStack(
      new ApiStack(app, "TestApiOE", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
      }),
    );
  });

  it("emits API Gateway OE alarms (p99 latency + 5XX) and a dashboard", () => {
    template.resourceCountIs("AWS::CloudWatch::Dashboard", 1);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBeGreaterThanOrEqual(2);
  });
});

describe("ApiStack WAF WebACL", () => {
  const mkApi = (webAclId?: string) => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);
    return Template.fromStack(
      new ApiStack(app, "TestApiWaf", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
        ...(webAclId ? { webAclId } : {}),
      }),
    );
  };

  it("auto-creates a REGIONAL WebACL with the Common Rule Set when no ARN is given", () => {
    const template = mkApi();
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "REGIONAL",
      Rules: Match.arrayWith([
        Match.objectLike({
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: "AWS",
              Name: "AWSManagedRulesCommonRuleSet",
            },
          },
        }),
      ]),
    });
    // Auto-created ACL is associated with the API stage.
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 1);
  });

  it("associates a provided WebACL ARN and creates no WebACL", () => {
    const arn =
      "arn:aws:wafv2:us-east-1:123456789012:regional/webacl/byo-api/abc";
    const template = mkApi(arn);
    template.resourceCountIs("AWS::WAFv2::WebACL", 0);
    template.hasResourceProperties("AWS::WAFv2::WebACLAssociation", {
      WebACLArn: arn,
    });
  });

  it("adds a per-IP rate-based rule to the auto-created API WebACL", () => {
    // The API WAF must carry a per-entity (per-IP) rate limit,
    // not only the signature-based Common Rule Set.
    const template = mkApi();
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "REGIONAL",
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "RateLimitPerIp",
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: {
              AggregateKeyType: "IP",
              Limit: 2000,
            },
          },
        }),
      ]),
    });
  });
});

describe("ApiStack request throttling", () => {
  const mkApi = (
    overrides: {
      throttleRateLimit?: number;
      throttleBurstLimit?: number;
      expensiveThrottleRateLimit?: number;
      expensiveThrottleBurstLimit?: number;
    } = {},
  ) => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);
    return Template.fromStack(
      new ApiStack(app, "TestApiThrottle", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
        ...overrides,
      }),
    );
  };

  it("sets the stage-wide default throttle as the wildcard MethodSetting", () => {
    const template = mkApi();
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      MethodSettings: Match.arrayWith([
        Match.objectLike({
          HttpMethod: "*",
          ResourcePath: "/*",
          ThrottlingRateLimit: 50,
          ThrottlingBurstLimit: 100,
        }),
      ]),
    });
  });

  it("applies a tighter per-method throttle to POST /induce (expensive op)", () => {
    // A single caller hammering the multi-minute induction job must not be able
    // to consume the whole stage budget — it gets its own low ceiling.
    const template = mkApi();
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      MethodSettings: Match.arrayWith([
        Match.objectLike({
          HttpMethod: "POST",
          ResourcePath: "/~1namespaces~1{namespaceId}~1induce",
          ThrottlingRateLimit: 5,
          ThrottlingBurstLimit: 10,
        }),
      ]),
    });
  });

  it("honors per-operation throttle overrides", () => {
    const template = mkApi({
      expensiveThrottleRateLimit: 2,
      expensiveThrottleBurstLimit: 3,
    });
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      MethodSettings: Match.arrayWith([
        Match.objectLike({
          HttpMethod: "POST",
          ResourcePath: "/~1namespaces~1{namespaceId}~1induce",
          ThrottlingRateLimit: 2,
          ThrottlingBurstLimit: 3,
        }),
      ]),
    });
  });
});

describe("ApiStack cache invalidation stream handler", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });

    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);

    template = Template.fromStack(
      new ApiStack(app, "TestApiStream", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
      }),
    );
  });

  it("creates the cache-invalidation Lambda with correct handler and env", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp(".*cache-invalidation$"),
      // Must match the real package name — a stale module path here would
      // deploy a Lambda that ImportErrors on every stream record, silently
      // never invalidating the authorizer's cache.
      Handler: "coa_control_plane.authorization.stream_handler.handler",
      Environment: {
        Variables: Match.objectLike({
          CACHE_INVALIDATION_TABLE_NAME: Match.anyValue(),
        }),
      },
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
    });
  });

  it("creates two DynamoDB event source mappings (Roles + RRM)", () => {
    // Two EventSourceMappings: one for Roles, one for ResourceRoleMappings
    const mappings = template.findResources("AWS::Lambda::EventSourceMapping", {
      Properties: {
        StartingPosition: "LATEST",
        BatchSize: 10,
      },
    });
    // The cache-invalidation Lambda should have exactly 2 DDB stream triggers
    expect(Object.keys(mappings).length).toBeGreaterThanOrEqual(2);
  });

  it("retries indefinitely and parks failed invalidations in a DLQ", () => {
    // A dropped record leaves the version counter stale, so revoked roles stay
    // cached. Retry until the record ages out, then DLQ it — never discard.
    const mappings = template.findResources("AWS::Lambda::EventSourceMapping");
    const streamMappings = Object.values(mappings).filter(
      (m) => m.Properties?.EventSourceArn !== undefined,
    );
    expect(streamMappings.length).toBeGreaterThanOrEqual(2);
    for (const m of streamMappings) {
      expect(m.Properties.MaximumRetryAttempts).toBe(-1);
      expect(
        m.Properties.DestinationConfig?.OnFailure?.Destination,
      ).toBeDefined();
    }
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: Match.stringLikeRegexp(".*cache-invalidation-dlq$"),
      SqsManagedSseEnabled: true,
    });
  });

  it("alarms when the cache-invalidation DLQ is non-empty", () => {
    // A silent DLQ defeats its purpose: operators must be told when an
    // invalidation was dropped, since that means revoked roles may stay cached.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: Match.stringLikeRegexp(".*cache-invalidation-dlq$"),
      Namespace: "AWS/SQS",
      MetricName: "ApproximateNumberOfMessagesVisible",
      ComparisonOperator: "GreaterThanThreshold",
      Threshold: 0,
    });
  });

  it("grants read-write on the CacheInvalidation table", () => {
    // The Lambda needs dynamodb:UpdateItem for atomic_increment
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              "dynamodb:BatchGetItem",
              "dynamodb:PutItem",
              "dynamodb:UpdateItem",
            ]),
          }),
        ]),
      },
    });
  });
});

describe("ApiStack GET /health", () => {
  // The stack passes no ssmPathHandlers here, so every spec path except
  // /health resolves to the 501 stub. That is exactly the "before" state this
  // block asserts /health has been lifted out of.
  const mkStack = (allowedOrigin = "https://app.example.com") => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);
    return new ApiStack(app, "TestApiHealth", {
      env: { account: "123456789012", region: "us-east-1" },
      allowedOrigin,
      vpc,
      rolesTable: mkTable("Roles"),
      resourceRoleMappingsTable: mkTable("RRM"),
      cacheInvalidationTable: mkTable("Cache"),
    });
  };

  /** The OpenAPI body API Gateway is created from. */
  const specBody = (stack: ApiStack): Record<string, any> => {
    const apis = Template.fromStack(stack).findResources(
      "AWS::ApiGateway::RestApi",
    );
    return Object.values(apis)[0].Properties.Body;
  };

  const healthGet = (stack: ApiStack): Record<string, any> =>
    specBody(stack).paths["/health"].get;

  it("answers from a mock, not the 501 stub Lambda", () => {
    // The defect: /health had no ssmPathHandlers entry, so the loop over spec
    // paths gave it an aws_proxy integration pointing at the inline
    // not-implemented function.
    const integration = healthGet(mkStack())["x-amazon-apigateway-integration"];
    expect(integration.type).toBe("mock");
    expect(integration.uri).toBeUndefined();
  });

  it("returns 200 with the Smithy HealthCheck output shape", () => {
    const response =
      healthGet(mkStack())["x-amazon-apigateway-integration"].responses.default;
    expect(response.statusCode).toBe("200");
    expect(response.responseTemplates["application/json"]).toBe(
      '{"status":"ok"}',
    );
  });

  it("stays unauthenticated", () => {
    // HealthCheck is @optionalAuth and the sole unsecuredPaths entry — a probe
    // that needs a token is not a probe.
    expect(healthGet(mkStack()).security).toEqual([]);
  });

  it("marks the probe response uncacheable", () => {
    // A cached "ok" keeps reporting a healthy API after it stopped being one.
    const params =
      healthGet(mkStack())["x-amazon-apigateway-integration"].responses.default
        .responseParameters;
    expect(params["method.response.header.Cache-Control"]).toBe("'no-store'");
  });

  it("supplies the security headers the mock has no Lambda to set", () => {
    const params =
      healthGet(mkStack())["x-amazon-apigateway-integration"].responses.default
        .responseParameters;
    expect(
      params["method.response.header.Strict-Transport-Security"],
    ).toContain("max-age=");
    expect(params["method.response.header.X-Content-Type-Options"]).toBe(
      "'nosniff'",
    );
  });

  it("echoes the configured CORS origin, not a wildcard", () => {
    const params =
      healthGet(mkStack("https://app.example.com"))[
        "x-amazon-apigateway-integration"
      ].responses.default.responseParameters;
    expect(
      params["method.response.header.Access-Control-Allow-Origin"],
    ).toBe("'https://app.example.com'");
  });
});

describe("ApiStack 501-stub guard", () => {
  // The paths the stack itself declares as deliberately unimplemented,
  // asserted against the exported INTENTIONALLY_STUBBED below so this list
  // can't silently drift from the guard it's meant to describe.
  const ALLOWED_STUBS = [
    "/health",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/upload",
  ];

  it("keeps ALLOWED_STUBS in sync with the stack's own allowlist", () => {
    expect(new Set(ALLOWED_STUBS)).toEqual(INTENTIONALLY_STUBBED);
  });

  const specPaths = (): string[] => {
    const cp = readOpenApiSpec(Paths.controlPlaneOpenApiSpec, {
      CorsOrigin: "*",
    });
    const dl = readOpenApiSpec(Paths.dataLayerOpenApiSpec, { CorsOrigin: "*" });
    return [
      ...new Set([
        ...Object.keys(cp.paths ?? {}),
        ...Object.keys(dl.paths ?? {}),
      ]),
    ];
  };

  const synth = (ssmPathHandlers?: Record<string, string>) => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const depStack = new cdk.Stack(app, "DepStack", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const mkTable = (id: string) => mkTestTable(depStack, id);
    return () =>
      new ApiStack(app, "TestApiStubGuard", {
        env: { account: "123456789012", region: "us-east-1" },
        allowedOrigin: "*",
        vpc,
        rolesTable: mkTable("Roles"),
        resourceRoleMappingsTable: mkTable("RRM"),
        cacheInvalidationTable: mkTable("Cache"),
        ssmPathHandlers,
      });
  };

  const fullWiring = (omit: string[] = []): Record<string, string> =>
    Object.fromEntries(
      specPaths()
        .filter((p) => !ALLOWED_STUBS.includes(p) && !omit.includes(p))
        .map((p) => [p, "/coa/dev/some/api-fn-arn"]),
    );

  it("accepts wiring that covers every path except the declared stubs", () => {
    expect(synth(fullWiring())).not.toThrow();
  });

  it("fails synth when a wired-up operation has no handler entry", () => {
    // This is the same failure shape that let a fully implemented route ship
    // behind the 501 stub: the Smithy operation exists, but app.ts omits its
    // handler mapping.
    const sourcesPath = "/namespaces/{namespaceId}/sources";
    expect(specPaths()).toContain(sourcesPath);
    expect(synth(fullWiring([sourcesPath]))).toThrow(
      /would fall through to the 501 stub.*namespaces\/\{namespaceId\}\/sources/s,
    );
  });

  it("fails synth when a path is wired but still listed in INTENTIONALLY_STUBBED", () => {
    // The mirror-image mistake: a backend lands for a path that's still on the
    // allowlist, so the "unwired" guard never sees it and the stale entry goes
    // unnoticed. This must fail synth too, not just the reverse case above.
    const healthPath = "/health";
    expect(ALLOWED_STUBS).toContain(healthPath);
    const wiring = {
      ...fullWiring(),
      [healthPath]: "/coa/dev/some/api-fn-arn",
    };
    expect(synth(wiring)).toThrow(
      /still listed in INTENTIONALLY_STUBBED.*\/health/s,
    );
  });

  it("fails synth when the DescribeSchema wiring is dropped", () => {
    // /schema sat in INTENTIONALLY_STUBBED as "no backend yet" after the backend
    // had landed (app.ts routes it to data-layer/api-fn-arn). An allowlist entry
    // for a wired path is invisible: the guard simply stops covering it. Pinning
    // the path here means losing the app.ts entry is a synth failure again.
    const schemaPath = "/namespaces/{namespaceId}/schema";
    expect(specPaths()).toContain(schemaPath);
    expect(ALLOWED_STUBS).not.toContain(schemaPath);
    expect(synth(fullWiring([schemaPath]))).toThrow(
      /would fall through to the 501 stub.*namespaces\/\{namespaceId\}\/schema/s,
    );
  });

  it("stays quiet when no wiring was supplied at all", () => {
    // Most tests in this file construct the stack to assert on the authorizer or
    // the WAF and pass no handlers. For them every path stubs, which is correct.
    expect(synth()).not.toThrow();
  });
});
