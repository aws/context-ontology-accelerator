// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as cr from "aws-cdk-lib/custom-resources";
import * as iam from "aws-cdk-lib/aws-iam";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { PublicUIConstruct } from "../../constructs/public-ui-construct";
import { RuntimeConfig } from "@coa/shared";
import { SCLStack } from "../../constructs/scl-stack";
import { resolveContext } from "../../context";
import { fromRoot } from "../../paths";
import { CustomDomainConfig } from "../../types";

/**
 * Read the monorepo version from the repo-root VERSION file (the single source
 * of truth kept in sync across all manifests by `scripts/sync_version.py`).
 * Surfaced to the frontend via runtime-config.json so the UI shows the deployed
 * version without a rebuild. Returns undefined if the file is missing so a
 * deploy is never blocked on it.
 */
export function readRepoVersion(
  versionFile: string = fromRoot("VERSION"),
): string | undefined {
  try {
    return fs.readFileSync(versionFile, "utf-8").trim() || undefined;
  } catch {
    return undefined;
  }
}

/**
 * Assemble the {@link RuntimeConfig} written to `runtime-config.json`. Optional
 * fields are omitted (not emitted as `undefined`) so a missing apiEndpoint,
 * serveRuntimeArn, or version leaves no key behind — the frontend treats an
 * absent key and an absent value identically. Pure and exported so the
 * include/omit behavior is unit-testable without a synth.
 */
export function buildRuntimeConfig(args: {
  region: string;
  stage: string;
  authority: string;
  clientId: string;
  apiEndpoint?: string;
  serveRuntimeArn?: string;
  version?: string;
}): RuntimeConfig {
  const {
    region,
    stage,
    authority,
    clientId,
    apiEndpoint,
    serveRuntimeArn,
    version,
  } = args;
  return {
    region,
    stage,
    authority,
    clientId,
    ...(apiEndpoint && { apiEndpoint }),
    ...(serveRuntimeArn && { serveRuntimeArn }),
    ...(version && { version }),
  };
}

export interface WebStackProps extends cdk.StackProps {
  /**
   * Whether the auth stack provisioned a Cognito User Pool (COGNITO/SAML
   * idpType) — gates the Cognito-specific callback-URL patch below, which
   * has no equivalent for a direct external-OIDC deployment (no user pool
   * to patch). Read from SSM instead of the {@link WebStackProps}'s
   * userPoolId directly to avoid a cross-stack export on the auth stack's
   * UserPool/UserPoolClient (see idp-authentication-stack.ts).
   */
  readonly isCognitoMode: boolean;
  /** Backend API endpoint URL. */
  readonly apiEndpoint?: string;
  /** API Gateway REST API ID (used to update CORS origin after CloudFront creation). */
  readonly apiRestApiId?: string;
  /** API Gateway stage name (used to trigger redeployment after CORS update). */
  readonly apiStageName?: string;
  /** Path to built web-app assets directory. */
  readonly websiteContentPath?: string;
  /** Custom domain configuration for the UI and/or API. */
  readonly customDomain?: CustomDomainConfig;
  /** When true, deploy frontend via ECS Fargate + ALB instead of CloudFront + S3. */
  readonly enablePrivateEndpoints?: boolean;
  /** Optional WAF WebACL ARN (CLOUDFRONT scope). Takes precedence over {@link autoWebAclParam}. */
  readonly webAclId?: string;
  /**
   * Cross-region SSM parameter holding an auto-created CLOUDFRONT WebACL ARN
   * (published by {@link EdgeWafStack} in us-east-1). Read via a custom
   * resource when {@link webAclId} is not provided directly.
   */
  readonly autoWebAclParam?: { readonly name: string; readonly region: string };
  /** AgentCore Runtime ARN for SSE streaming queries. */
  readonly serveRuntimeArn?: string;
  /**
   * Fully custom Content-Security-Policy header value, replacing the derived
   * one. Wired from the `content_security_policy` CDK context key in
   * `bin/app.ts` — the escape hatch existed on {@link PublicUIConstructProps}
   * but reached nothing, so extending the policy meant patching CDK source
   * (issue #130).
   */
  readonly contentSecurityPolicy?: string;
}

/**
 * Web application hosting stack for the Context Ontology Accelerator.
 *
 * Supports two deployment modes:
 * - **Public (default)** — CloudFront + S3 via {@link PublicUIConstruct}.
 *   Static assets are served from S3 through a CloudFront distribution with OAC,
 *   security headers, and SPA routing (404/403 → `index.html`).
 * - **Private** — ECS Fargate + ALB (not yet implemented; throws if `enablePrivateEndpoints` is set).
 *
 * On first deploy without a custom domain, the CloudFront URL is auto-generated.
 * This stack automatically patches:
 * 1. **Cognito callback URLs** — updates the user pool client with the CloudFront origin.
 * 2. **API Gateway CORS** — updates gateway response headers with the CloudFront origin
 *    and triggers an API redeployment.
 *
 * When `customDomain` is provided, both patches are skipped since the origin is
 * known upfront and passed directly to the auth and API stacks.
 *
 * Runtime configuration (`runtime-config.json`) is injected into S3 at deploy time
 * with the OIDC authority, client ID, and API endpoint so the React app can
 * discover backends without a rebuild.
 */
export class WebStack extends SCLStack {
  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);

    const { ssmPrefix } = resolveContext(this.node);

    // IdP issuer/client ID via SSM (avoids a cross-stack export on the auth
    // stack's UserPool/UserPoolClient — see idp-authentication-stack.ts for
    // the writer, populated on both the Cognito/SAML and direct-OIDC paths).
    const authority = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/issuer`,
    );
    const clientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/userpool-client-id`,
    );
    // Only populated on the Cognito/SAML path (idp-authentication-stack.ts's
    // OIDC branch never writes this parameter) — gated by isCognitoMode so a
    // direct-OIDC deployment never attempts to resolve a nonexistent param.
    const userPoolId = props.isCognitoMode
      ? ssm.StringParameter.valueForStringParameter(
          this,
          `${ssmPrefix}/userpool-id`,
        )
      : "";
    // Hosted-UI domain prefix, for the CSP. Same Cognito-only gate as above:
    // the OIDC branch of the auth stack provisions no hosted UI, so there is no
    // parameter to read and nothing to allowlist (a direct-OIDC deployment's
    // token endpoint is on its own authority, already covered).
    const cognitoDomainPrefix = props.isCognitoMode
      ? ssm.StringParameter.valueForStringParameter(
          this,
          `${ssmPrefix}/cognito-domain-prefix`,
        )
      : undefined;

    const uiDomainName = props.customDomain?.uiDomainName;
    const uiCertificateArn = props.customDomain?.uiCertificateArn;
    const apiDomainName = props.customDomain?.apiDomainName;

    // Prefer the custom API domain when configured; otherwise the direct
    // API endpoint. Undefined when neither is set — buildRuntimeConfig omits it.
    const apiEndpoint = apiDomainName
      ? `https://${apiDomainName}`
      : props.apiEndpoint;

    const runtimeConfig = buildRuntimeConfig({
      region: this.region,
      stage: this.envName,
      authority,
      clientId,
      apiEndpoint,
      serveRuntimeArn: props.serveRuntimeArn,
      version: readRepoVersion(),
    });

    if (props.enablePrivateEndpoints) {
      throw new Error(
        "Private endpoints (ECS frontend) not yet implemented. Set enablePrivateEndpoints to false.",
      );
    }

    // ── Resolve the CloudFront WebACL ARN ──────────────────────────
    // Prefer a caller-provided ARN. Otherwise, on the auto-create path, read
    // the ARN published by the us-east-1 EdgeWafStack from its SSM parameter.
    // The read is cross-region (CLOUDFRONT WebACLs live in us-east-1) so it
    // goes through a custom resource with an explicit region rather than a
    // CFN dynamic reference (which cannot cross regions).
    let resolvedWebAclId = props.webAclId;
    if (!resolvedWebAclId && props.autoWebAclParam) {
      const { name, region } = props.autoWebAclParam;
      const webAclReader = new cr.AwsCustomResource(
        this,
        "ReadCloudFrontWebAcl",
        {
          installLatestAwsSdk: false,
          onUpdate: {
            service: "SSM",
            action: "getParameter",
            parameters: { Name: name },
            region,
            physicalResourceId: cr.PhysicalResourceId.of(`cf-web-acl-${name}`),
          },
          policy: cr.AwsCustomResourcePolicy.fromStatements([
            new iam.PolicyStatement({
              actions: ["ssm:GetParameter"],
              resources: [
                `arn:aws:ssm:${region}:${this.account}:parameter${name}`,
              ],
            }),
          ]),
        },
      );
      resolvedWebAclId = webAclReader.getResponseField("Parameter.Value");
    }

    const publicUI = new PublicUIConstruct(this, "PublicUI", {
      websiteContentPath: props.websiteContentPath,
      runtimeConfig,
      webAclId: resolvedWebAclId,
      ...(cognitoDomainPrefix && { cognitoDomainPrefix }),
      ...(props.contentSecurityPolicy && {
        contentSecurityPolicy: props.contentSecurityPolicy,
      }),
      ...(uiDomainName &&
        uiCertificateArn && { uiDomainName, uiCertificateArn }),
    });

    // The origin the browser is actually served from — the custom UI domain
    // when configured, otherwise the generated CloudFront domain (only known
    // at deploy time). Cognito callbacks are patched to match.
    const siteUrl = uiDomainName
      ? `https://${uiDomainName}`
      : `https://${publicUI.distribution.distributionDomainName}`;

    // Optional: create Route53 alias records (A + AAAA) → CloudFront when a
    // hosted zone is provided. Uses L1 CfnRecordSet so only the zone id is
    // needed (no zone-name lookup). Z2FDTNDATAQYW2 is CloudFront's fixed alias
    // hosted-zone id. When omitted, DNS is managed externally.
    const hostedZoneId = props.customDomain?.hostedZoneId;
    if (hostedZoneId && uiDomainName) {
      for (const recordType of ["A", "AAAA"] as const) {
        new route53.CfnRecordSet(this, `UiAlias${recordType}`, {
          hostedZoneId,
          name: `${uiDomainName}.`,
          type: recordType,
          aliasTarget: {
            dnsName: publicUI.distribution.distributionDomainName,
            hostedZoneId: "Z2FDTNDATAQYW2",
            evaluateTargetHealth: false,
          },
        });
      }
    }

    // ── Patch Cognito callback URLs ────────────────────────────────
    // Uses raw AWS API values (not CDK enums) because AwsCustomResource
    // calls the Cognito API directly. updateUserPoolClient is a full
    // replacement — all parameters must match the auth stack's addClient()
    // config or they will be reset to defaults.
    // Mapping: authFlows.userSrp → ALLOW_USER_SRP_AUTH,
    //          (implicit)        → ALLOW_REFRESH_TOKEN_AUTH,
    //          OAuthScope.OPENID → "openid", etc.
    if (props.isCognitoMode) {
      new cr.AwsCustomResource(this, "UpdateCognitoCallbacks", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "CognitoIdentityServiceProvider",
          action: "updateUserPoolClient",
          parameters: {
            UserPoolId: userPoolId,
            ClientId: clientId,
            CallbackURLs: [
              `${siteUrl}/authenticate/`,
              ...(this.envName === "dev"
                ? ["http://localhost:5173/authenticate/"]
                : []),
            ],
            LogoutURLs: [`${siteUrl}/`],
            ExplicitAuthFlows: [
              "ALLOW_USER_SRP_AUTH",
              "ALLOW_REFRESH_TOKEN_AUTH",
              // Must mirror idp-authentication-stack.ts addClient() authFlows.
              // updateUserPoolClient is a full replacement, so omitting this
              // in dev silently strips ALLOW_USER_PASSWORD_AUTH (added there
              // for integ-test/CLI sign-in) after this custom resource runs.
              ...(this.envName === "dev" ? ["ALLOW_USER_PASSWORD_AUTH"] : []),
            ],
            AllowedOAuthFlows: ["code"],
            AllowedOAuthScopes: ["email", "openid", "profile"],
            SupportedIdentityProviders: ["COGNITO"],
            AllowedOAuthFlowsUserPoolClient: true,
            // Must mirror idp-authentication-stack.ts addClient(). This is a
            // full replacement, so omitting it would reset the client to the
            // API default (LEGACY), re-enabling user-existence error leakage.
            PreventUserExistenceErrors: "ENABLED",
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `cognito-callbacks-${clientId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["cognito-idp:UpdateUserPoolClient"],
            resources: [
              `arn:aws:cognito-idp:${this.region}:${this.account}:userpool/${userPoolId}`,
            ],
          }),
        ]),
      });
    }

    // ── Patch API Gateway CORS origin ──────────────────────────────
    if (props.apiRestApiId && props.apiStageName) {
      const corsHeaders = {
        "gatewayresponse.header.Access-Control-Allow-Origin": `'${siteUrl}'`,
        "gatewayresponse.header.Strict-Transport-Security":
          "'max-age=63072000; includeSubDomains; preload'",
        "gatewayresponse.header.X-Content-Type-Options": "'nosniff'",
      };

      const apiArn = `arn:aws:apigateway:${this.region}::/restapis/${props.apiRestApiId}`;

      // Update 4XX gateway response
      new cr.AwsCustomResource(this, "UpdateApiCors4XX", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "putGatewayResponse",
          parameters: {
            restApiId: props.apiRestApiId,
            responseType: "DEFAULT_4XX",
            responseParameters: corsHeaders,
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-cors-4xx-${props.apiRestApiId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:PUT", "apigateway:PATCH", "apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });

      // Update 5XX gateway response
      new cr.AwsCustomResource(this, "UpdateApiCors5XX", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "putGatewayResponse",
          parameters: {
            restApiId: props.apiRestApiId,
            responseType: "DEFAULT_5XX",
            responseParameters: corsHeaders,
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-cors-5xx-${props.apiRestApiId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:PUT", "apigateway:PATCH", "apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });

      // Trigger API redeployment so the CORS changes take effect
      new cr.AwsCustomResource(this, "RedeployApi", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "createDeployment",
          parameters: {
            restApiId: props.apiRestApiId,
            stageName: props.apiStageName,
            description: "Auto-redeploy after CORS origin update",
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-redeploy-${Date.now()}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });
    }
  }
}
