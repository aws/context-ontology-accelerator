// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";
import { SclMonitoring } from "../../constructs";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import { CustomDomainConfig } from "../../types";
import { resolveContext } from "../../context";
import {
  DEFAULT_THROTTLE_RATE_LIMIT,
  DEFAULT_THROTTLE_BURST_LIMIT,
  DEFAULT_EXPENSIVE_THROTTLE_RATE_LIMIT,
  DEFAULT_EXPENSIVE_THROTTLE_BURST_LIMIT,
  DEFAULT_WAF_RATE_LIMIT_PER_5MIN,
  EXPENSIVE_API_OPERATIONS,
} from "../../constants";
import { Paths, fromRoot } from "../../paths";
import { bundlePython } from "../../utils/python-bundling";
import {
  readOpenApiSpec,
  mergeOpenApiPaths,
  injectLambdaProxyIntegrations,
  fillAuthorizerParameters,
  PathLambdaMap,
  MethodLambdaMap,
} from "../../utils/api-utils";

export interface ApiStackProps extends cdk.StackProps {
  /**
   * CORS allowed origin for the API (e.g. `https://d2xw5y8g9akfew.cloudfront.net`).
   * Defaults to `"*"` when no custom domain is configured (local dev).
   */
  readonly allowedOrigin: string;

  /**
   * Custom domain configuration. When `apiDomainName` + `apiCertificateArn`
   * are set, the API is served via a REGIONAL custom domain with a TLS_1_2
   * security policy and the default `execute-api` endpoint is disabled.
   * The certificate MUST be in this stack's region.
   */
  readonly customDomain?: CustomDomainConfig;

  /**
   * Map of API path → Lambda ARN(s) that handle requests for that path.
   * A string routes all methods to one Lambda. An object routes per-method.
   * Paths not in this map are served by a default 501 "Not Implemented" Lambda.
   */
  readonly pathHandlers?: PathLambdaMap;

  /**
   * Map of API path → SSM parameter name storing the Lambda ARN.
   * Resolved at deploy time to avoid cross-stack CF exports.
   * Multiple paths can point to the same SSM parameter.
   */
  readonly ssmPathHandlers?: Record<string, string>;

  /** VPC for Lambda authorizer placement. */
  readonly vpc: ec2.IVpc;

  /** JWKS URI (optional — auto-discovered from issuer if not set). */
  readonly jwksUri?: string;

  /** DynamoDB tables from AuthnzStack. */
  readonly rolesTable: dynamodb.ITable;
  readonly resourceRoleMappingsTable: dynamodb.ITable;
  readonly cacheInvalidationTable: dynamodb.ITable;

  /**
   * API Gateway stage-wide throttling (steady-state rps / burst bucket).
   * Applied to every method via the stage-level default. Soft limit —
   * see {@link DEFAULT_THROTTLE_RATE_LIMIT} / {@link DEFAULT_THROTTLE_BURST_LIMIT}.
   */
  readonly throttleRateLimit?: number;
  readonly throttleBurstLimit?: number;

  /**
   * Tighter per-operation throttle for expensive, job-launching operations
   * (see {@link EXPENSIVE_API_OPERATIONS}). Applied per-method so one caller
   * hammering an expensive route cannot exhaust the whole stage budget. Soft —
   * see {@link DEFAULT_EXPENSIVE_THROTTLE_RATE_LIMIT} / `...BURST_LIMIT`.
   */
  readonly expensiveThrottleRateLimit?: number;
  readonly expensiveThrottleBurstLimit?: number;

  /**
   * WAF WebACL ARN (REGIONAL scope) to associate with the API stage. When
   * omitted, a REGIONAL WebACL with `AWSManagedRulesCommonRuleSet` is created
   * and associated automatically.
   */
  readonly webAclId?: string;

  /** Alarm action strategy for OE monitoring (SNS topic, etc.). */
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * REST API stack for the Context Ontology Accelerator.
 *
 * Provisions an API Gateway REST API from the Smithy-generated OpenAPI spec with:
 * - **Spec-driven routes** — paths, models, and request validation from Smithy.
 * - **Lambda proxy integration** — one Lambda per API path (unmapped paths get a 501 stub).
 * - **Custom Lambda authorizer** — validates JWTs, resolves roles, evaluates Cedar policies.
 * - **CORS** — configured via the Smithy `@cors` trait; origin injected at synth time.
 * - **CloudWatch logging** — access logs in JSON format with one-month retention.
 * - **Security headers** — gateway responses include HSTS and X-Content-Type-Options.
 */
export class ApiStack extends SCLStack {
  public readonly api: apigw.SpecRestApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // Resolve the Namespaces table name from SSM (avoids a cross-stack export).
    // The authorizer reads namespace status to enforce the ARCHIVED mutation guard.
    const { ssmPrefix } = resolveContext(this.node);
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    // IdP issuer/client ID via SSM (avoids a cross-stack export on the auth
    // stack's UserPoolClient — see idp-authentication-stack.ts for the writer).
    const issuerUrl = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/issuer`,
    );
    const clientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/userpool-client-id`,
    );
    // The JWT claim name carrying group membership — "cognito:groups" for
    // Cognito, or whatever the customer's external IdP uses (configured as
    // oidcSettings.groupClaim, default "groups") for direct OIDC. Read from
    // SSM rather than hardcoding DEFAULT_GROUP_CLAIM ("cognito:groups") here:
    // that constant is wrong on the OIDC path and silently breaks all
    // group-based role resolution (extract_groups_claim always returns []
    // when the configured claim name doesn't match what the IdP sends).
    const groupClaimName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/authentication-group-token-name`,
    );

    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    const namespacesTableArn = `arn:aws:dynamodb:${region}:${account}:table/${namespacesTableName}`;

    // ── Custom Lambda Authorizer ───────────────────────────────────
    const authorizerFn = new lambda.Function(this, "AuthorizerFn", {
      functionName: this.prefixed("authorizer"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_control_plane.authorization.handler.handler",
      code: bundlePython({
        srcDirs: [Paths.controlPlaneSrc, Paths.commonLib],
        requirementsFile: fromRoot("packages/control-plane/requirements.txt"),
      }),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        JWKS_ISSUER: issuerUrl,
        ...(props.jwksUri && { JWKS_URI: props.jwksUri }),
        CLIENT_ID: clientId,
        GROUP_CLAIM_NAME: groupClaimName,
        ROLES_TABLE_NAME: props.rolesTable.tableName,
        RESOURCE_ROLE_MAPPINGS_TABLE_NAME:
          props.resourceRoleMappingsTable.tableName,
        CACHE_INVALIDATION_TABLE_NAME: props.cacheInvalidationTable.tableName,
        NAMESPACES_TABLE_NAME: namespacesTableName,
      },
    });

    props.rolesTable.grantReadData(authorizerFn);
    props.resourceRoleMappingsTable.grantReadData(authorizerFn);
    props.cacheInvalidationTable.grantReadData(authorizerFn);

    // Least-privilege read of namespace status for the ARCHIVED mutation guard.
    authorizerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:GetItem"],
        resources: [namespacesTableArn],
      }),
    );

    // ── Cache Invalidation Stream Handler ──────────────────────────
    // Triggered by DynamoDB Streams on Roles and ResourceRoleMappings
    // tables. On any change, bumps the version counter in the
    // CacheInvalidation table so the authorizer clears its local cache.
    const cacheInvalidationFn = new lambda.Function(
      this,
      "CacheInvalidationFn",
      {
        functionName: this.prefixed("cache-invalidation"),
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "coa_control_plane.authorization.stream_handler.handler",
        code: bundlePython({
          srcDirs: [Paths.controlPlaneSrc, Paths.commonLib],
          requirementsFile: fromRoot("packages/control-plane/requirements.txt"),
        }),
        timeout: cdk.Duration.seconds(10),
        memorySize: 128,
        // Same VPC placement as the authorizer beside it: DynamoDB is reached
        // over the VPC's gateway endpoint, no public egress needed.
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        logRetention: logs.RetentionDays.ONE_MONTH,
        environment: {
          CACHE_INVALIDATION_TABLE_NAME: props.cacheInvalidationTable.tableName,
        },
      },
    );

    // Grant write to the CacheInvalidation table (atomic_increment).
    props.cacheInvalidationTable.grantReadWriteData(cacheInvalidationFn);

    // A dropped stream record means the authorizer's version counter never
    // moves for that grant change, so a revoked role stays cached until the
    // container recycles — the exact gap this wiring exists to close. So:
    // retry indefinitely within the records' 24h stream retention, and park
    // anything still failing at the end of it in a DLQ for replay instead of
    // discarding it silently.
    const cacheInvalidationDlq = new sqs.Queue(this, "CacheInvalidationDlq", {
      queueName: this.prefixed("cache-invalidation-dlq"),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });

    // Wire DynamoDB Streams from Roles and ResourceRoleMappings tables.
    const streamSourceProps = {
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 10,
      maxBatchingWindow: cdk.Duration.seconds(5),
      // -1 = retry until the record ages out of the 24h stream retention.
      retryAttempts: -1,
      onFailure: new lambdaEventSources.SqsDlq(cacheInvalidationDlq),
      // No partial-batch reporting: the handler collapses a whole batch into a
      // single version-counter bump (that bump clears the authorizer's entire
      // local cache, so the exact count is irrelevant — only that it moved).
      // There are no per-record operations to fail independently, so a whole-
      // batch retry on error is both correct and cheap.
    };
    cacheInvalidationFn.addEventSource(
      new lambdaEventSources.DynamoEventSource(
        props.rolesTable,
        streamSourceProps,
      ),
    );
    cacheInvalidationFn.addEventSource(
      new lambdaEventSources.DynamoEventSource(
        props.resourceRoleMappingsTable,
        streamSourceProps,
      ),
    );

    // Any message in the DLQ means an invalidation was dropped after 24h of
    // retries — the authorizer's version counter never moved for that grant
    // change, so a revoked role can stay cached (bounded only by the per-entry
    // TTL, multiplied by warm containers). The DLQ exists to avoid silent
    // loss; without this alarm operators would never know it filled.
    new cloudwatch.Alarm(this, "CacheInvalidationDlqAlarm", {
      alarmName: this.prefixed("cache-invalidation-dlq"),
      alarmDescription:
        "Cache-invalidation records landed in the DLQ — authorizer role cache may be serving revoked roles until TTL expiry",
      metric: cacheInvalidationDlq.metricApproximateNumberOfMessagesVisible({
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── IAM role for API Gateway to invoke the authorizer Lambda ───
    const authorizerRole = new iam.Role(this, "AuthorizerRole", {
      roleName: this.prefixed("apigw-authorizer-role"),
      assumedBy: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      inlinePolicies: {
        InvokeAuthorizer: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["lambda:InvokeFunction"],
              resources: [authorizerFn.functionArn],
            }),
          ],
        }),
      },
    });

    // ── Default stub for unmapped paths ─────────────────────────────
    // Placeholder Lambda that returns 501 for any Smithy-defined path
    // not yet wired to a real handler via `pathHandlers`. This ensures
    // every route in the OpenAPI spec has a valid integration target.
    // The authorizer still protects these routes (unless in unsecuredPaths).
    const stubFn = new lambda.Function(this, "NotImplementedFn", {
      functionName: this.prefixed("api-not-implemented"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromInline(
        'import json\ndef handler(event, context):\n    return {"statusCode": 501, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"message": "Not implemented"})}',
      ),
      timeout: cdk.Duration.seconds(5),
    });

    // ── Build unified OpenAPI spec ─────────────────────────────────
    // The API Gateway serves two Smithy services (control-plane + data-layer)
    // via one REST API. Merge their generated specs at deploy time.
    // The merge is per method, so a path may take methods from both specs; it
    // throws if both define the SAME path+method (see mergeOpenApiPaths).
    const spec = readOpenApiSpec(Paths.controlPlaneOpenApiSpec, {
      CorsOrigin: props.allowedOrigin,
    });
    const dataLayerSpec = readOpenApiSpec(Paths.dataLayerOpenApiSpec, {
      CorsOrigin: props.allowedOrigin,
    });
    mergeOpenApiPaths(spec, dataLayerSpec);
    if (dataLayerSpec.components?.schemas) {
      spec.components = spec.components ?? {};
      spec.components.schemas = {
        ...(spec.components.schemas ?? {}),
        ...dataLayerSpec.components.schemas,
      };
    }
    spec.info.description =
      "Context Ontology Accelerator REST API — namespace, ingestion, and query management.";

    // Resolve Lambda ARNs from SSM (avoids cross-stack CF exports)
    const handlers: PathLambdaMap = { ...(props.pathHandlers ?? {}) };
    const resolvedSsmArns = new Map<string, string>();
    for (const [apiPath, ssmPath] of Object.entries(
      props.ssmPathHandlers ?? {},
    )) {
      if (!resolvedSsmArns.has(ssmPath)) {
        resolvedSsmArns.set(
          ssmPath,
          new cdk.CfnDynamicReference(
            cdk.CfnDynamicReferenceService.SSM,
            ssmPath,
          ).toString(),
        );
      }
      handlers[apiPath] = resolvedSsmArns.get(ssmPath)!;
    }

    const pathLambdaMap: PathLambdaMap = {};
    for (const apiPath of Object.keys(spec.paths ?? {})) {
      pathLambdaMap[apiPath] = handlers[apiPath] ?? stubFn.functionArn;
    }

    injectLambdaProxyIntegrations(spec, {
      pathLambdaMap,
      region: this.region,
    });

    // ── GET /health ────────────────────────────────────────────────
    // Answered by API Gateway itself, not a Lambda. HealthCheck is
    // @optionalAuth in the Smithy model and the sole entry in
    // ``unsecuredPaths`` below, i.e. an unauthenticated probe for a
    // load balancer or an uptime check. A mock integration is the right
    // shape for that: no cold start, no VPC hop, and nothing that can
    // fail independently of the gateway being up.
    //
    // Deliberately not ``/system-health``'s per-backend detail — the
    // Smithy output is a bare ``status``, and an unauthenticated caller
    // should not learn which of Neptune/AOSS/DynamoDB is degraded.
    // Callers wanting that use the authenticated ``GET /system-health``.
    //
    // This runs after injectLambdaProxyIntegrations so it replaces the
    // aws_proxy integration that loop installed (the 501 stub, absent a
    // handler entry). It must also run before fillAuthorizerParameters,
    // which is what strips ``security`` from the operation.
    const healthGet = spec.paths?.["/health"]?.get;
    if (!healthGet) {
      throw new Error(
        "GET /health is missing from the merged API spec, so the health mock " +
          "cannot be attached. The Smithy HealthCheck operation was probably " +
          "renamed or removed — update this block or drop it.",
      );
    }
    healthGet["x-amazon-apigateway-integration"] = {
      type: "mock",
      passthroughBehavior: "when_no_match",
      requestTemplates: { "application/json": '{"statusCode": 200}' },
      responses: {
        default: {
          statusCode: "200",
          // injectLambdaProxyIntegrations declared the security headers on
          // the method response, and Smithy's @cors declared
          // Access-Control-Allow-Origin; a mock has no Lambda to populate
          // them, so they are supplied statically. Cache-Control is the one
          // that matters functionally: an intermediary caching "ok" would
          // keep reporting a healthy API after it stopped being one.
          responseParameters: {
            "method.response.header.Access-Control-Allow-Origin": `'${props.allowedOrigin}'`,
            "method.response.header.Cache-Control": "'no-store'",
            "method.response.header.Strict-Transport-Security":
              "'max-age=63072000; includeSubDomains; preload'",
            "method.response.header.X-Content-Type-Options": "'nosniff'",
          },
          responseTemplates: { "application/json": '{"status":"ok"}' },
        },
      },
    };

    fillAuthorizerParameters(spec, {
      authorizerLambdaArn: authorizerFn.functionArn,
      authorizerRoleArn: authorizerRole.roleArn,
      region: this.region,
      unsecuredPaths: ["/health"],
    });

    // ── Access logs ────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, "ApiAccessLogs", {
      logGroupName: this.prefixed("api-access-logs"),
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── SpecRestApi from the enriched OpenAPI spec ─────────────────
    // When a custom domain + certificate are configured, the API is served
    // over a REGIONAL custom domain with a TLS 1.2 security policy and the
    // raw execute-api endpoint is disabled. Without one, the default
    // execute-api endpoint is used (its TLS floor is AWS-managed).
    const apiCustomDomain =
      props.customDomain?.apiDomainName && props.customDomain?.apiCertificateArn
        ? {
            domainName: props.customDomain.apiDomainName,
            certificateArn: props.customDomain.apiCertificateArn,
          }
        : undefined;

    // ── Per-operation throttling ───────────────────────────────────
    // Expensive, job-launching operations get a tighter per-method throttle so
    // a single caller hammering (e.g.) POST /induce cannot consume the whole
    // stage-wide budget and starve cheap reads. Keyed by `{path}/{METHOD}`,
    // which CDK renders into stage MethodSettings. Only wire routes that the
    // served OpenAPI spec actually defines — a stale entry would otherwise
    // create a MethodSetting for a non-existent method.
    const expensiveRateLimit =
      props.expensiveThrottleRateLimit ?? DEFAULT_EXPENSIVE_THROTTLE_RATE_LIMIT;
    const expensiveBurstLimit =
      props.expensiveThrottleBurstLimit ??
      DEFAULT_EXPENSIVE_THROTTLE_BURST_LIMIT;
    const specPaths = spec.paths ?? {};
    const methodOptions: Record<string, apigw.MethodDeploymentOptions> = {};
    for (const op of EXPENSIVE_API_OPERATIONS) {
      const methodsForPath = specPaths[op.path];
      if (!methodsForPath || !(op.method.toLowerCase() in methodsForPath)) {
        continue; // route not served — skip rather than throttle a phantom method
      }
      methodOptions[`${op.path}/${op.method}`] = {
        throttlingRateLimit: expensiveRateLimit,
        throttlingBurstLimit: expensiveBurstLimit,
      };
    }

    this.api = new apigw.SpecRestApi(this, "RestApi", {
      restApiName: this.prefixed("api"),
      description: "Context Ontology Accelerator REST API",
      apiDefinition: apigw.ApiDefinition.fromInline(spec),
      cloudWatchRole: true,
      ...(apiCustomDomain && {
        disableExecuteApiEndpoint: true,
        domainName: {
          domainName: apiCustomDomain.domainName,
          certificate: acm.Certificate.fromCertificateArn(
            this,
            "ApiCertificate",
            apiCustomDomain.certificateArn,
          ),
          securityPolicy: apigw.SecurityPolicy.TLS_1_2,
          endpointType: apigw.EndpointType.REGIONAL,
        },
      }),
      deployOptions: {
        accessLogDestination: new apigw.LogGroupLogDestination(logGroup),
        accessLogFormat: apigw.AccessLogFormat.jsonWithStandardFields({
          caller: true,
          httpMethod: true,
          ip: true,
          protocol: true,
          requestTime: true,
          resourcePath: true,
          responseLength: true,
          status: true,
          user: true,
        }),
        loggingLevel: apigw.MethodLoggingLevel.INFO,
        // Logs full request/response payloads — only safe in single-developer sandbox accounts.
        dataTraceEnabled: this.envName === "dev",
        tracingEnabled: true,
        // Stage-wide default throttle (renders as the wildcard MethodSetting).
        throttlingRateLimit:
          props.throttleRateLimit ?? DEFAULT_THROTTLE_RATE_LIMIT,
        throttlingBurstLimit:
          props.throttleBurstLimit ?? DEFAULT_THROTTLE_BURST_LIMIT,
        // Per-operation overrides for expensive routes (empty → no extra settings).
        ...(Object.keys(methodOptions).length > 0 ? { methodOptions } : {}),
      },
    });

    // ── Grant API Gateway invoke permission on each unique Lambda ───
    // Use CfnPermission directly (not fn.addPermission) to keep the
    // permission resource in *this* stack, avoiding cross-stack cycles
    // when the Lambda is defined in another stack.
    const apiArn = this.api.arnForExecuteApi();
    const grantedArns = new Set<string>();

    // Collect all unique Lambda ARNs from pathHandlers
    const allArns: string[] = [stubFn.functionArn];
    for (const mapping of Object.values(handlers)) {
      if (typeof mapping === "string") {
        allArns.push(mapping);
      } else {
        const methodMap = mapping as MethodLambdaMap;
        if (methodMap._default) allArns.push(methodMap._default);
        for (const key of Object.keys(methodMap)) {
          if (key !== "_default" && methodMap[key as keyof MethodLambdaMap]) {
            allArns.push(methodMap[key as keyof MethodLambdaMap] as string);
          }
        }
      }
    }

    let permIdx = 0;
    for (const arn of allArns) {
      if (grantedArns.has(arn)) continue;
      grantedArns.add(arn);
      new lambda.CfnPermission(this, `ApiGwInvoke${permIdx++}`, {
        action: "lambda:InvokeFunction",
        functionName: arn,
        principal: "apigateway.amazonaws.com",
        sourceArn: apiArn,
      });
    }

    // ── Gateway responses with security headers ────────────────────
    const gatewayResponseHeaders = {
      "gatewayresponse.header.Access-Control-Allow-Origin": `'${props.allowedOrigin}'`,
      "gatewayresponse.header.Strict-Transport-Security":
        "'max-age=63072000; includeSubDomains; preload'",
      "gatewayresponse.header.X-Content-Type-Options": "'nosniff'",
    };

    this.api.addGatewayResponse("Default4XX", {
      type: apigw.ResponseType.DEFAULT_4XX,
      responseHeaders: gatewayResponseHeaders,
    });

    this.api.addGatewayResponse("Default5XX", {
      type: apigw.ResponseType.DEFAULT_5XX,
      responseHeaders: gatewayResponseHeaders,
    });

    // Optional Route53 alias: API custom domain → API Gateway regional target.
    if (
      apiCustomDomain &&
      props.customDomain?.hostedZoneId &&
      this.api.domainName
    ) {
      new route53.CfnRecordSet(this, "ApiAliasA", {
        hostedZoneId: props.customDomain.hostedZoneId,
        name: `${apiCustomDomain.domainName}.`,
        type: "A",
        aliasTarget: {
          dnsName: this.api.domainName.domainNameAliasDomainName,
          hostedZoneId: this.api.domainName.domainNameAliasHostedZoneId,
          evaluateTargetHealth: false,
        },
      });
    }

    // ── WAF WebACL association (REGIONAL) ──────────────────────────
    // Associate the API stage with a WAF WebACL. Use the caller-provided ARN
    // when set; otherwise auto-create a REGIONAL WebACL with the AWS-managed
    // Common Rule Set. REST API stages require a REGIONAL-scope WebACL in the
    // API's own region (associated via CfnWebACLAssociation on the stage ARN).
    //
    // NOTE: SizeRestrictions_QUERYSTRING is excluded from the managed rule set
    // because DataZone's SearchAssets pagination tokens (~2.8KB) exceed the
    // rule's 2,048-byte limit, causing false-positive blocks on paginated
    // ListSourceTables requests. A custom size-restriction rule (priority 0)
    // re-applies the query-string size limit to all OTHER paths, so only the
    // paginated /tables endpoint is exempt.
    let apiWebAclArn: string;
    if (props.webAclId) {
      apiWebAclArn = props.webAclId;
    } else {
      const wafName = this.prefixed("api-waf");
      const webAcl = new wafv2.CfnWebACL(this, "ApiWafResource", {
        // Name intentionally omitted — let CloudFormation generate a unique
        // name to avoid collision during replacement of the prior WebACL.
        scope: "REGIONAL",
        defaultAction: { allow: {} },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: wafName,
          sampledRequestsEnabled: true,
        },
        rules: [
          // Rule 0: Block oversized query strings (>4096 bytes) EXCEPT on
          // paginated paths that carry DataZone tokens (/sources/*/tables).
          {
            name: "SizeRestrictions-QueryString-ExceptPaginatedTables",
            priority: 0,
            action: { block: {} },
            statement: {
              andStatement: {
                statements: [
                  // Query string > 4096 bytes (generous limit for DataZone tokens)
                  {
                    sizeConstraintStatement: {
                      fieldToMatch: { queryString: {} },
                      comparisonOperator: "GT",
                      size: 4096,
                      textTransformations: [{ priority: 0, type: "NONE" }],
                    },
                  },
                  // AND NOT a paginated tables/sources path
                  {
                    notStatement: {
                      statement: {
                        byteMatchStatement: {
                          fieldToMatch: { uriPath: {} },
                          positionalConstraint: "CONTAINS",
                          searchString: "/tables",
                          textTransformations: [
                            { priority: 0, type: "LOWERCASE" },
                          ],
                        },
                      },
                    },
                  },
                ],
              },
            },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: `${wafName}-size-qs-custom`,
              sampledRequestsEnabled: true,
            },
          },
          // Rule 1: AWS Managed Common Rule Set with SizeRestrictions_QUERYSTRING
          // excluded (set to COUNT). The custom rule above re-applies the
          // restriction to non-paginated paths.
          {
            name: "AWS-AWSManagedRulesCommonRuleSet",
            priority: 1,
            statement: {
              managedRuleGroupStatement: {
                vendorName: "AWS",
                name: "AWSManagedRulesCommonRuleSet",
                excludedRules: [{ name: "SizeRestrictions_QUERYSTRING" }],
              },
            },
            overrideAction: { none: {} },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: `${wafName}-common-rule-set`,
              sampledRequestsEnabled: true,
            },
          },
          // Rule 2: Per-IP rate limit — blocks any single IP exceeding
          // DEFAULT_WAF_RATE_LIMIT_PER_5MIN requests per rolling 5-minute
          // window. Complements the signature-based Common Rule Set with a
          // volumetric guard against scraping / brute-force from one source.
          {
            name: "RateLimitPerIp",
            priority: 2,
            action: { block: {} },
            statement: {
              rateBasedStatement: {
                limit: DEFAULT_WAF_RATE_LIMIT_PER_5MIN,
                aggregateKeyType: "IP",
              },
            },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: `${wafName}-rate-limit-per-ip`,
              sampledRequestsEnabled: true,
            },
          },
        ],
      });
      apiWebAclArn = webAcl.attrArn;
    }

    new wafv2.CfnWebACLAssociation(this, "ApiWafAssociation", {
      resourceArn: this.api.deploymentStage.stageArn,
      webAclArn: apiWebAclArn,
    });

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs)
    // ================================================================
    new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("api"),
      alarmAction: props.alarmAction,
    }).monitorApi(this.api);

    new cdk.CfnOutput(this, "ApiEndpoint", {
      value: this.api.url,
      description: "REST API endpoint URL",
    });
  }
}
