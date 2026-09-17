// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import { Construct } from "constructs";
import { SCLConstruct } from "./scl-construct";
import { AccessLogsBucket } from "./access-logs-bucket";
import { RuntimeConfig, RUNTIME_CONFIG_FILENAME } from "@coa/shared";

/**
 * Build the Content-Security-Policy for the SPA.
 *
 * The primary control is `script-src 'self'`, which blocks inline and
 * cross-origin script execution (the main XSS vector). `index.html` ships only
 * an external module script and Vite bundles its module-preload polyfill into
 * that chunk, so no inline scripts are needed.
 *
 * `connect-src`/`frame-src` are scoped to the app's real endpoints — API
 * and OIDC authority — when those are known at deploy time. Before
 * the API endpoint is wired (first deploy, runtime-config not yet patched)
 * we fall back to scheme-level allowances (`https:`) so the app still
 * functions; a subsequent deploy tightens them automatically. `frame-src`
 * includes the OIDC authority for silent-token-renew iframes.
 *
 * `style-src` retains `'unsafe-inline'` because Cloudscape/emotion inject
 * styles at runtime; this does not weaken script protection. Deployers can
 * supply a fully custom policy via `PublicUIConstructProps.contentSecurityPolicy`.
 *
 * @param cognitoDomainPrefix Cognito hosted-UI domain prefix, when the
 *   deployment provisions a user pool. See the `cognitoHostedUiOrigin`
 *   derivation below for why this is required and not merely nice to have.
 */
export function buildContentSecurityPolicy(
  runtimeConfig: RuntimeConfig,
  override?: string,
  cognitoDomainPrefix?: string,
): string {
  if (override) return override;

  const originOf = (url?: string): string | undefined => {
    if (!url) return undefined;
    try {
      return new URL(url).origin;
    } catch {
      return undefined;
    }
  };

  const apiOrigin = originOf(runtimeConfig.apiEndpoint);
  const authOrigin = originOf(runtimeConfig.authority);

  // AgentCore origin for SSE streaming (Playground fetches directly)
  const agentCoreOrigin = runtimeConfig.serveRuntimeArn
    ? `https://bedrock-agentcore.${runtimeConfig.region}.amazonaws.com`
    : undefined;

  // Cognito user-pool IdP endpoint. The browser OIDC flow always calls this
  // directly (discovery `/.well-known/openid-configuration`, JWKS, token), so
  // it MUST be in the allowlist. Derive it from `region` (always resolved)
  // rather than relying on `authority` — `authority` can be empty/unresolved
  // at synth, which silently drops the origin and blocks sign-in.
  const cognitoOrigin = runtimeConfig.region
    ? `https://cognito-idp.${runtimeConfig.region}.amazonaws.com`
    : undefined;

  // Cognito HOSTED-UI origin — a DIFFERENT host from `cognitoOrigin` above, and
  // the one the browser actually exchanges the authorization code at. Cognito's
  // discovery document puts `authorization_endpoint`, `token_endpoint` and
  // `/oauth2/revoke` on `<prefix>.auth.<region>.amazoncognito.com` while keeping
  // the issuer and JWKS on `cognito-idp.<region>.amazonaws.com`. Allowlisting
  // only the latter blocks the PKCE code→token POST and every sign-in fails with
  // nothing in the app to show it — the violation appears only as a CSP report in
  // devtools (issue #130).
  //
  // Needed in BOTH directives, for two separate flows:
  //   connect-src — the token exchange and the logout-time refresh-token
  //     revocation, both `fetch` (see OIDCProvider.revokeRefreshToken).
  //   frame-src   — `automaticSilentRenew: true` renews in a hidden iframe
  //     pointed at the hosted-UI authorize endpoint.
  //
  // Interpolated rather than parsed: the prefix reaches us as a CFN dynamic
  // reference from SSM, so `new URL()` on it would throw and `originOf` would
  // return undefined — silently reintroducing the bug. Region comes from
  // `runtimeConfig.region` for the same reason `cognitoOrigin` does.
  const cognitoHostedUiOrigin =
    cognitoDomainPrefix && runtimeConfig.region
      ? `https://${cognitoDomainPrefix}.auth.${runtimeConfig.region}.amazoncognito.com`
      : undefined;

  // S3 origins for the browser→S3 presigned PUT/GET used by OSI import/export.
  // Region-scoped, derived from `region` like cognitoOrigin: the OSI bucket lives
  // in a later service stack (cross-stack), so we scope to the region's S3 rather
  // than couple the foundation web stack to it. Both the virtual-hosted host
  // (`<bucket>.s3.<region>.amazonaws.com`, boto3's default) and the path-style
  // host are allowed. Without this, connect-src blocks the presigned upload and
  // OSI import via the UI fails with "Failed to fetch" (issue 103).
  const s3Origins = runtimeConfig.region
    ? [
        `https://*.s3.${runtimeConfig.region}.amazonaws.com`,
        `https://s3.${runtimeConfig.region}.amazonaws.com`,
      ]
    : [];

  const connect = new Set<string>(["'self'"]);
  const frame = new Set<string>(["'self'"]);
  for (const o of [
    apiOrigin,
    authOrigin,
    agentCoreOrigin,
    cognitoOrigin,
    cognitoHostedUiOrigin,
    ...s3Origins,
  ]) {
    if (o) connect.add(o);
  }
  if (authOrigin) frame.add(authOrigin); // OIDC silent-renew iframe
  if (cognitoOrigin) frame.add(cognitoOrigin); // OIDC iframe against the IdP
  if (cognitoHostedUiOrigin) frame.add(cognitoHostedUiOrigin); // silent-renew iframe

  // API endpoint not yet wired → permit scheme-level so the app still works;
  // a re-deploy tightens these to the specific origins above.
  if (!apiOrigin) {
    connect.add("https:");
  }

  return [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    `connect-src ${[...connect].join(" ")}`,
    `frame-src ${[...frame].join(" ")}`,
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
  ].join("; ");
}

export interface PublicUIConstructProps {
  /** Path to the built web-app assets directory */
  readonly websiteContentPath?: string;
  /** Runtime config to write as JSON to S3 */
  readonly runtimeConfig: RuntimeConfig;
  /** Optional WAF WebACL ARN to associate with the distribution */
  readonly webAclId?: string;
  /**
   * Optional override for the Content-Security-Policy header value. When
   * omitted, a policy is derived from `runtimeConfig` (script-src 'self' plus
   * connect/frame-src scoped to the API and OIDC authority origins).
   *
   * Wired from the `content_security_policy` CDK context key in `bin/app.ts`, so
   * an operator can extend the policy without patching this source.
   */
  readonly contentSecurityPolicy?: string;
  /**
   * Cognito hosted-UI domain prefix (`<prefix>.auth.<region>.amazoncognito.com`),
   * when the deployment provisions a user pool. Omitted for a direct-OIDC
   * deployment, which has no Cognito hosted UI. Feeds the CSP — without it the
   * browser blocks the OAuth token exchange and every sign-in fails (issue #130).
   */
  readonly cognitoDomainPrefix?: string;
  /**
   * Custom domain alias for the distribution. When set with
   * {@link uiCertificateArn}, the distribution serves this domain and the
   * `TLS_V1_2_2021` minimum-protocol policy takes effect (the default
   * `*.cloudfront.net` certificate otherwise forces the policy to TLSv1).
   */
  readonly uiDomainName?: string;
  /** ACM certificate ARN for {@link uiDomainName}. MUST be in us-east-1. */
  readonly uiCertificateArn?: string;
}

export class PublicUIConstruct extends SCLConstruct {
  public readonly uiBucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: PublicUIConstructProps) {
    super(scope, id);

    // S3 bucket for static website assets — fully private, accessed only via CloudFront OAC
    const uiAccessLogs = new AccessLogsBucket(this, "UIAccessLogs", {
      nameSuffix: "ui-logs",
    });
    this.uiBucket = new s3.Bucket(this, "UIBucket", {
      bucketName: `${this.prefixed("ui-assets")}-${cdk.Stack.of(this).account}-${cdk.Stack.of(this).region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: uiAccessLogs.bucket,
      serverAccessLogsPrefix: "ui-assets/",
      enforceSSL: true,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Security headers policy
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      "SecurityHeaders",
      {
        responseHeadersPolicyName: this.prefixed("security-headers"),
        securityHeadersBehavior: {
          strictTransportSecurity: {
            accessControlMaxAge: cdk.Duration.seconds(63072000), // 2 years
            includeSubdomains: true,
            preload: true,
            override: true,
          },
          contentTypeOptions: { override: true },
          frameOptions: {
            frameOption: cloudfront.HeadersFrameOption.DENY,
            override: true,
          },
          referrerPolicy: {
            referrerPolicy:
              cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
            override: true,
          },
          contentSecurityPolicy: {
            contentSecurityPolicy: buildContentSecurityPolicy(
              props.runtimeConfig,
              props.contentSecurityPolicy,
              props.cognitoDomainPrefix,
            ),
            override: true,
          },
        },
        customHeadersBehavior: {
          customHeaders: [
            {
              header: "Cache-Control",
              value: "no-store, max-age=0",
              override: false,
            },
          ],
        },
      },
    );

    // CloudFront distribution with S3 OAC origin
    const s3Origin = origins.S3BucketOrigin.withOriginAccessControl(
      this.uiBucket,
    );

    // Dedicated CloudFront access-log bucket. Legacy standard logging delivers
    // via a bucket ACL grant, so this bucket enables ACLs
    // (BUCKET_OWNER_PREFERRED) while still blocking all public access.
    const cfAccessLogs = new AccessLogsBucket(this, "CfAccessLogs", {
      nameSuffix: "cf-logs",
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
    });

    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: this.prefixed("web-distribution"),
      enableLogging: true,
      logBucket: cfAccessLogs.bucket,
      logFilePrefix: "cloudfront/",
      defaultBehavior: {
        origin: s3Origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy,
      },
      additionalBehaviors: {
        // runtime-config.json must never be cached — it contains per-environment
        // API endpoints and auth settings that change between deploys.
        [RUNTIME_CONFIG_FILENAME]: {
          origin: s3Origin,
          viewerProtocolPolicy:
            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          responseHeadersPolicy,
        },
      },
      defaultRootObject: "index.html",
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      // SPA routing: return index.html for 403/404 so client-side routing works
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
        },
      ],
      ...(props.webAclId && { webAclId: props.webAclId }),
      // Custom domain + ACM cert (us-east-1). Attaching a cert is what makes
      // minimumProtocolVersion (TLS_V1_2_2021) take effect — the default
      // CloudFront certificate forces the policy to TLSv1.
      ...(props.uiDomainName &&
        props.uiCertificateArn && {
          domainNames: [props.uiDomainName],
          certificate: acm.Certificate.fromCertificateArn(
            this,
            "UiCertificate",
            props.uiCertificateArn,
          ),
        }),
    });

    // Deploy website content + runtime config JSON to S3
    const sources: s3deploy.ISource[] = [
      s3deploy.Source.jsonData(RUNTIME_CONFIG_FILENAME, props.runtimeConfig),
    ];
    if (props.websiteContentPath) {
      sources.unshift(s3deploy.Source.asset(props.websiteContentPath));
    }

    new s3deploy.BucketDeployment(this, "DeployWebsite", {
      sources,
      destinationBucket: this.uiBucket,
      distribution: this.distribution,
      distributionPaths: ["/*"],
    });

    // Outputs
    new cdk.CfnOutput(this, "DistributionDomainName", {
      value: this.distribution.distributionDomainName,
      description: "CloudFront distribution domain name",
    });

    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
      description: "CloudFront distribution ID",
    });

    new cdk.CfnOutput(this, "WebsiteURL", {
      value: `https://${this.distribution.distributionDomainName}`,
      description: "Website URL",
    });
  }
}
