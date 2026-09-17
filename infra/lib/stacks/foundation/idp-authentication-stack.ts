// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";
import { UserPoolUser } from "../../constructs/user-pool-user";
import { resolveContext } from "../../context";
import {
  CTX_IDP_TYPE,
  CTX_CALLBACK_URLS,
  CTX_LOGOUT_URLS,
  DEFAULT_CALLBACK_URLS,
  DEFAULT_LOGOUT_URLS,
  DEFAULT_GROUP_CLAIM,
  DEFAULT_ADMIN_USERNAME,
  DEFAULT_ADMIN_GROUP,
  DEFAULT_ADMIN_EMAIL,
  DEFAULT_ADMIN_ROLE,
} from "../../constants";
import {
  IdpType,
  IdpAuthenticationStackProps,
  SsmConfig,
  ClaimMapping,
  InitialCognitoUserAttributes,
} from "../../types";

/**
 * Foundation stack: identity provider authentication infrastructure.
 *
 * Supports three deployment-time modes (LLD §2.2.5):
 *  - COGNITO  → provisions Cognito User Pool + Client + optional federation
 *  - SAML     → provisions Cognito User Pool + Client + SAML provider(s)
 *  - OIDC     → no Cognito; stores external issuer/clientId/jwksUri in SSM
 */
export class IdpAuthenticationStack extends SCLStack {
  public readonly userPool?: cognito.UserPool;
  public readonly userPoolClient?: cognito.UserPoolClient;
  public readonly mcpClient?: cognito.UserPoolClient;
  public readonly clientId: string;
  public readonly mcpClientId: string;
  public readonly issuerUrl: string;
  public readonly initialClaimsMappings: ClaimMapping[];
  public readonly initialUserDetails: InitialCognitoUserAttributes;

  /**
   * Convert a JSON-serializable SsmConfig into CDK-typed stack props.
   *
   * Call from app.ts:
   * ```ts
   * const authProps = IdpAuthenticationStack.fromSsmConfig(ssmConfig);
   * new IdpAuthenticationStack(app, id, authProps);
   * ```
   */
  static fromSsmConfig(cfg: SsmConfig): IdpAuthenticationStackProps {
    return {
      ...(cfg.idpType && { idpType: cfg.idpType }),
      ...(cfg.oidcSettings && { oidcSettings: cfg.oidcSettings }),
      ...(cfg.claimsMappings && { claimsMappings: cfg.claimsMappings }),
      ...(cfg.initialAdminEmail && {
        initialUserDetails: {
          username: cfg.initialAdminEmail,
          email: cfg.initialAdminEmail,
          group: "Admin",
        },
      }),
      ...(cfg.samlProviders && {
        samlProviders: cfg.samlProviders.map((p) => ({
          name: p.name,
          metadata: p.metadataUrl
            ? cognito.UserPoolIdentityProviderSamlMetadata.url(p.metadataUrl)
            : cognito.UserPoolIdentityProviderSamlMetadata.file(
                p.metadataContent ?? "",
              ),
          userPool: undefined as any, // filled by constructor
        })),
      }),
      ...(cfg.oidcProviders && {
        oidcProviders: cfg.oidcProviders.map((p) => ({
          name: p.name,
          clientId: p.clientId,
          clientSecret: p.clientSecret,
          issuerUrl: p.issuerUrl,
          scopes: p.scopes,
          userPool: undefined as any, // filled by constructor
        })),
      }),
      ...(cfg.cognitoCustomAttributes && {
        cognitoCustomAttributes: Object.fromEntries(
          Object.entries(cfg.cognitoCustomAttributes).map(([k, v]) => [
            k,
            v === "number"
              ? new cognito.NumberAttribute({ mutable: true })
              : new cognito.StringAttribute({ mutable: true }),
          ]),
        ),
      }),
      ...(cfg.attributeMapping && {
        attributeMapping: Object.fromEntries(
          Object.entries(cfg.attributeMapping).map(([k, v]) => [
            k,
            cognito.ProviderAttribute.other(v),
          ]),
        ),
      }),
      ...(cfg.refreshTokenValidityHours && {
        refreshTokenValidity: cdk.Duration.hours(cfg.refreshTokenValidityHours),
      }),
    };
  }

  constructor(
    scope: Construct,
    id: string,
    props?: IdpAuthenticationStackProps,
  ) {
    super(scope, id, props);
    this.addComponentTag("foundation");

    const { ssmPrefix } = resolveContext(this.node);

    const idpType: IdpType =
      props?.idpType ??
      (this.node.tryGetContext(CTX_IDP_TYPE) as string as IdpType) ??
      IdpType.COGNITO;

    // ── Initial user & claims ──────────────────────────────────────
    this.initialUserDetails = {
      username: props?.initialUserDetails?.username ?? DEFAULT_ADMIN_USERNAME,
      group: props?.initialUserDetails?.group ?? DEFAULT_ADMIN_GROUP,
      email: props?.initialUserDetails?.email ?? DEFAULT_ADMIN_EMAIL,
    };

    this.initialClaimsMappings = [...(props?.claimsMappings ?? [])];
    if (idpType === IdpType.COGNITO || idpType === IdpType.SAML) {
      this.initialClaimsMappings.push({
        groupValue: this.initialUserDetails.group,
        mappedRoles: [DEFAULT_ADMIN_ROLE],
      });
    }

    // ── OIDC path: no Cognito resources ────────────────────────────
    if (idpType === IdpType.OIDC) {
      const oidc = props?.oidcSettings;
      if (!oidc)
        throw new Error("oidcSettings is required when idpType is OIDC");
      if (!oidc.clientId?.trim() || !oidc.issuerUrl?.trim()) {
        throw new Error(
          "OIDC clientId and issuerUrl must be non-empty strings",
        );
      }
      this.clientId = oidc.clientId;
      // TODO: external OIDC uses same clientId for both web and MCP flows.
      // If we need separate scopes/audience per client, register a second
      // client in the external IdP and accept it via oidcSettings.mcpClientId.
      this.mcpClientId = oidc.clientId;
      this.issuerUrl = oidc.issuerUrl;

      new ssm.StringParameter(this, "SsmUserPoolClientId", {
        parameterName: `${ssmPrefix}/userpool-client-id`,
        stringValue: oidc.clientId,
      });
      new ssm.StringParameter(this, "SsmMcpClientId", {
        parameterName: `${ssmPrefix}/mcp-client-id`,
        stringValue: this.mcpClientId,
      });
      new ssm.StringParameter(this, "SsmAuthIssuer", {
        parameterName: `${ssmPrefix}/issuer`,
        stringValue: oidc.issuerUrl,
      });
      new ssm.StringParameter(this, "SsmGroupTokenName", {
        parameterName: `${ssmPrefix}/authentication-group-token-name`,
        stringValue: oidc.groupClaim ?? DEFAULT_GROUP_CLAIM,
      });
      if (oidc.jwksUri) {
        new ssm.StringParameter(this, "SsmJwksUri", {
          parameterName: `${ssmPrefix}/jwks-uri`,
          stringValue: oidc.jwksUri,
        });
      }

      new cdk.CfnOutput(this, "IdpType", { value: "OIDC" });
      new cdk.CfnOutput(this, "Issuer", { value: oidc.issuerUrl });
      return;
    }

    // ── Cognito path (COGNITO or SAML) ─────────────────────────────
    const rawCallbackUrls =
      this.node.tryGetContext(CTX_CALLBACK_URLS) ?? DEFAULT_CALLBACK_URLS;
    const rawLogoutUrls =
      this.node.tryGetContext(CTX_LOGOUT_URLS) ?? DEFAULT_LOGOUT_URLS;
    const callbackUrls: string[] =
      typeof rawCallbackUrls === "string"
        ? JSON.parse(rawCallbackUrls)
        : rawCallbackUrls;
    const logoutUrls: string[] =
      typeof rawLogoutUrls === "string"
        ? JSON.parse(rawLogoutUrls)
        : rawLogoutUrls;

    if (
      !callbackUrls.length ||
      !callbackUrls.every((u) => /^https:\/\/.+/.test(u))
    )
      throw new Error("callbackUrls must be a non-empty array of HTTPS URLs");
    if (!logoutUrls.length || !logoutUrls.every((u) => /^https:\/\/.+/.test(u)))
      throw new Error("logoutUrls must be a non-empty array of HTTPS URLs");

    this.userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: this.prefixed("user-pool"),
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      customAttributes: props?.cognitoCustomAttributes,
    });

    // Hoisted into a named constant because it is also published to SSM below,
    // for the web stack's CSP. Keep it a plain synth-time string (prefix +
    // account, both resolved) so the consumer can interpolate it into an origin.
    const cognitoDomainPrefix = `${this.prefixed("auth")}-${this.account}`;
    const domain = this.userPool.addDomain("CognitoDomain", {
      cognitoDomain: {
        domainPrefix: cognitoDomainPrefix,
      },
    });

    // ── Register federated identity providers ──────────────────────
    const supportedIdPs: cognito.UserPoolClientIdentityProvider[] = [
      cognito.UserPoolClientIdentityProvider.COGNITO,
    ];

    for (const samlCfg of props?.samlProviders ?? []) {
      if (!samlCfg.name) throw new Error("SAML provider name is required");
      new cognito.UserPoolIdentityProviderSaml(this, `Saml-${samlCfg.name}`, {
        ...samlCfg,
        userPool: this.userPool,
        attributeMapping:
          samlCfg.attributeMapping ?? this.buildAttrMapping(props),
      });
      supportedIdPs.push(
        cognito.UserPoolClientIdentityProvider.custom(samlCfg.name),
      );
    }

    for (const oidcCfg of props?.oidcProviders ?? []) {
      if (!oidcCfg.name) throw new Error("OIDC provider name is required");
      new cognito.UserPoolIdentityProviderOidc(this, `Oidc-${oidcCfg.name}`, {
        ...oidcCfg,
        userPool: this.userPool,
        scopes: oidcCfg.scopes ?? ["openid", "email", "profile"],
        attributeMapping:
          oidcCfg.attributeMapping ?? this.buildAttrMapping(props),
      });
      supportedIdPs.push(
        cognito.UserPoolClientIdentityProvider.custom(oidcCfg.name),
      );
    }

    // ── User Pool Client ───────────────────────────────────────────
    // In dev, also enable USER_PASSWORD_AUTH to make local/CLI sign-in
    // (e.g. boto3 InitiateAuth USER_PASSWORD_AUTH) easier for testing.
    // Production deployments stay restricted to USER_SRP_AUTH only.
    this.userPoolClient = this.userPool.addClient("UserPoolClient", {
      userPoolClientName: this.prefixed("client"),
      generateSecret: false,
      authFlows: {
        userSrp: true,
        ...(this.envName === "dev" && { userPassword: true }),
      },
      supportedIdentityProviders: supportedIdPs,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls,
        logoutUrls,
      },
      preventUserExistenceErrors: true,
      refreshTokenValidity:
        props?.refreshTokenValidity ?? cdk.Duration.hours(12),
    });

    this.clientId = this.userPoolClient.userPoolClientId;

    // ── MCP/CLI Public Client (PKCE, localhost callbacks) ──────────
    // Separate client for IDE integrations (Kiro, Claude Desktop, Cursor).
    // Uses OAuth Authorization Code + PKCE with localhost redirect.
    // No client secret (public client per RFC 8252).
    this.mcpClient = this.userPool.addClient("McpClient", {
      userPoolClientName: this.prefixed("mcp-client"),
      generateSecret: false,
      authFlows: {
        userSrp: true,
        ...(this.envName === "dev" && { userPassword: true }),
      },
      supportedIdentityProviders: supportedIdPs,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: ["http://localhost:9876/oauth/callback"],
        logoutUrls: ["http://localhost:9876/"],
      },
      preventUserExistenceErrors: true,
      refreshTokenValidity: cdk.Duration.days(30),
      // MCP sessions are long-lived (IDE keeps connection open). Extend
      // access/id token TTL to 24h to avoid hourly reconnects. This only
      // affects the MCP client; the web app client keeps the default 1h.
      accessTokenValidity: cdk.Duration.hours(24),
      idTokenValidity: cdk.Duration.hours(24),
    });

    this.mcpClientId = this.mcpClient.userPoolClientId;
    this.issuerUrl = `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}`;

    // ── SSM Parameters ─────────────────────────────────────────────
    new ssm.StringParameter(this, "SsmUserPoolId", {
      parameterName: `${ssmPrefix}/userpool-id`,
      stringValue: this.userPool.userPoolId,
    });
    new ssm.StringParameter(this, "SsmUserPoolClientId", {
      parameterName: `${ssmPrefix}/userpool-client-id`,
      stringValue: this.userPoolClient.userPoolClientId,
    });
    new ssm.StringParameter(this, "SsmMcpClientId", {
      parameterName: `${ssmPrefix}/mcp-client-id`,
      stringValue: this.mcpClient!.userPoolClientId,
    });
    new ssm.StringParameter(this, "SsmAuthIssuer", {
      parameterName: `${ssmPrefix}/issuer`,
      stringValue: this.issuerUrl,
    });
    // Hosted-UI domain PREFIX (not the full URL). The browser's OAuth token
    // exchange, refresh-token revocation, and silent-renew iframe all go to
    // `<prefix>.auth.<region>.amazoncognito.com`, which is a different host from
    // the issuer above — so the web stack's Content-Security-Policy has to
    // allowlist it or every sign-in is blocked by the browser (issue #130).
    //
    // Published here rather than re-derived in the web stack so the naming rule
    // lives in exactly one place: a drifting copy would not fail a deploy, it
    // would silently block login. The prefix rather than `domain.baseUrl()`
    // because the consumer reads it with `valueForStringParameter` and gets a
    // CFN dynamic reference — an unresolved token cannot be parsed into an
    // origin at synth, but it can be interpolated into one.
    new ssm.StringParameter(this, "SsmCognitoDomainPrefix", {
      parameterName: `${ssmPrefix}/cognito-domain-prefix`,
      stringValue: cognitoDomainPrefix,
    });
    new ssm.StringParameter(this, "SsmGroupTokenName", {
      parameterName: `${ssmPrefix}/authentication-group-token-name`,
      stringValue: DEFAULT_GROUP_CLAIM,
    });

    new cdk.CfnOutput(this, "IdpType", { value: idpType });
    new cdk.CfnOutput(this, "UserPoolId", { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: this.userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, "McpClientId", {
      value: this.mcpClient!.userPoolClientId,
      description: "Cognito client ID for MCP/CLI (PKCE, localhost redirect)",
    });
    new cdk.CfnOutput(this, "CognitoDomainUrl", {
      value: domain.baseUrl(),
      description: "Cognito Hosted UI domain URL",
    });

    // ── Provision initial admin user ───────────────────────────────
    new UserPoolUser(this, "AdminUser", {
      userPool: this.userPool,
      userDetails: this.initialUserDetails,
    });
  }

  /** Convert props.attributeMapping to CDK AttributeMapping if provided. */
  private buildAttrMapping(
    props?: IdpAuthenticationStackProps,
  ): cognito.AttributeMapping | undefined {
    if (!props?.attributeMapping) return undefined;
    return {
      custom: Object.fromEntries(
        Object.entries(props.attributeMapping).map(([k, v]) => [k, v]),
      ),
    };
  }
}
