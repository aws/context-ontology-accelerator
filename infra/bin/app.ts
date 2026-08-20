#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import {
  CTX_RESOURCE_PREFIX,
  CTX_ENV,
  DEFAULT_RESOURCE_PREFIX,
  DEFAULT_ENV,
} from "../lib/constants";
import { Paths } from "../lib/paths";
import { SsmConfig, CustomDomainConfig, IdpType } from "../lib/types";
import {
  resolveAgentCoreAzNames,
  resolveEndpointServiceAzNames,
} from "../lib/utils/agentcore-az";
import { BrandEnvAspect } from "../lib/utils/brand-env";

// Foundation stacks
import {
  AuthnzStack,
  EdgeWafStack,
  GuardrailStack,
  IdpAuthenticationStack,
  NetworkStack,
  StorageStack,
  WebStack,
} from "../lib/stacks/foundation";

// Service stacks
import {
  ApiStack,
  DataLayerStack,
  McpStack,
  MetricServiceStack,
  NamespaceStack,
  OntologyStack,
  ServeStack,
  SourcesStack,
  VkgStack,
} from "../lib/stacks/services";

const app = new cdk.App();

const prefix =
  (app.node.tryGetContext(CTX_RESOURCE_PREFIX) as string) ??
  DEFAULT_RESOURCE_PREFIX;
const envName = (app.node.tryGetContext(CTX_ENV) as string) ?? DEFAULT_ENV;
const stackPrefix = `${prefix}-${envName}`;

/**
 * Read deployment configuration from SSM Parameter Store.
 * Falls back to empty config (all defaults) if the parameter doesn't exist.
 */
async function getConfigFromSSM(): Promise<SsmConfig> {
  const region = process.env.CDK_DEFAULT_REGION ?? "us-east-1";
  const paramName =
    (app.node.tryGetContext("configParameterName") as string | undefined) ??
    `/${prefix}/config`;

  try {
    const ssm = new SSMClient({ region });
    const res = await ssm.send(
      new GetParameterCommand({ Name: paramName, WithDecryption: true }),
    );
    return JSON.parse(res.Parameter?.Value ?? "{}") as SsmConfig;
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`SSM ${paramName} not found (${msg}), using defaults`);
    return {};
  }
}

async function deploy(): Promise<void> {
  const config = await getConfigFromSSM();

  // Resolve AgentCore-supported AZ names for the deploying account.
  // Only us-east-1 has restricted AZs (3/6 unsupported); all other
  // AgentCore regions support every AZ, so resolution is skipped and
  // NetworkStack uses the default maxAzs: 2. For restricted regions,
  // resolution MUST succeed during a real deploy — a silent fallback
  // would reintroduce the exact AZ bug this module prevents.
  //
  // Skip resolution entirely when CDK_DEFAULT_ACCOUNT is not set (CI
  // synth without credentials). The synth still validates the template
  // compiles; AZ pinning only matters for actual deploys.
  const deployRegion = process.env.CDK_DEFAULT_REGION ?? "us-east-1";
  const hasCredentials = !!process.env.CDK_DEFAULT_ACCOUNT;
  const importingVpc = !!(app.node.tryGetContext("vpc_id") as
    | string
    | undefined);
  let agentCoreAzNames: string[] | undefined;
  if (!importingVpc && hasCredentials) {
    // The OpenSearch Serverless control-plane VPC endpoint
    // (com.amazonaws.<region>.aoss) is offered in only a subset of AZs. Pin
    // the VPC to AgentCore AZs that ALSO support it so both the AgentCore
    // Runtime ENIs and the AOSS endpoint land on valid zones (e.g. in
    // us-east-1, AgentCore allows {1a,1b,1c} but AOSS only {1b,1c,1d} — the
    // intersection {1b,1c} avoids the 1a endpoint-create failure).
    const aossAzNames = await resolveEndpointServiceAzNames(
      deployRegion,
      `com.amazonaws.${deployRegion}.aoss`,
    );
    agentCoreAzNames = await resolveAgentCoreAzNames(
      deployRegion,
      2,
      undefined,
      aossAzNames,
    );
  }
  const authProps = IdpAuthenticationStack.fromSsmConfig(config);
  // Custom domains are all-or-nothing: provide every field or none.
  const cdFields = {
    uiDomainName: app.node.tryGetContext("ui_domain") as string | undefined,
    uiCertificateArn: app.node.tryGetContext("ui_cert_arn") as
      | string
      | undefined,
    apiDomainName: app.node.tryGetContext("api_domain") as string | undefined,
    apiCertificateArn: app.node.tryGetContext("api_cert_arn") as
      | string
      | undefined,
    hostedZoneId: app.node.tryGetContext("hosted_zone_id") as
      | string
      | undefined,
  };
  const cdValues = Object.values(cdFields);
  if (cdValues.some(Boolean) && !cdValues.every(Boolean)) {
    throw new Error(
      "Custom domain requires ALL of: ui_domain, ui_cert_arn, api_domain, " +
        "api_cert_arn, hosted_zone_id — or none of them.",
    );
  }
  const customDomain: CustomDomainConfig | undefined = cdValues.every(Boolean)
    ? (cdFields as CustomDomainConfig)
    : undefined;

  // Certificate region checks (fail fast at synth).
  const certRegion = (arn: string): string | undefined => arn.split(":")[3];
  if (customDomain) {
    if (certRegion(customDomain.uiCertificateArn) !== "us-east-1") {
      throw new Error(
        `Custom domain: uiCertificateArn must be an ACM certificate in us-east-1 (CloudFront requirement); got region ${certRegion(customDomain.uiCertificateArn)}.`,
      );
    }
    if (certRegion(customDomain.apiCertificateArn) !== deployRegion) {
      throw new Error(
        `Custom domain: apiCertificateArn must be in the API region ${deployRegion}; got ${certRegion(customDomain.apiCertificateArn)}.`,
      );
    }
  }

  const allowedOrigin = customDomain
    ? `https://${customDomain.uiDomainName}`
    : envName === "prod"
      ? (() => {
          throw new Error(
            "Production deployments require a UI custom domain for CORS. Set the custom-domain context (ui_domain, ...) or SSM config.",
          );
        })()
      : "*";

  // ── WAF WebACL inputs ──────────────────────────────────────────────
  // Optionally bring your own WebACL ARN per surface. API Gateway needs a
  // REGIONAL-scope ARN (this region); CloudFront needs a CLOUDFRONT-scope ARN
  // (us-east-1). When a surface's ARN is omitted, a WebACL with
  // AWSManagedRulesCommonRuleSet is auto-created for it.
  const apiWebAclId = app.node.tryGetContext("api_web_acl_id") as
    | string
    | undefined;
  const cloudfrontWebAclId = app.node.tryGetContext("cloudfront_web_acl_id") as
    | string
    | undefined;
  // CloudFront auto-create: a CLOUDFRONT WebACL must live in us-east-1, so it
  // is provisioned by a dedicated EdgeWafStack and its ARN is read by the
  // WebStack from SSM. Skipped without credentials (CI synth) since the
  // us-east-1 stack needs a concrete account.
  const deployAccount = process.env.CDK_DEFAULT_ACCOUNT;
  const cfWebAclParamName = `/${prefix}/edge/cloudfront-web-acl-arn`;

  // OE monitoring alarm action seam. Undefined in round one: alarms are
  // action-ready but route nowhere until a strategy (SNS/chatbot) is wired
  // here. Wiring it here propagates to every stack's SclMonitoring with no
  // per-stack change.
  const alarmAction: IAlarmActionStrategy | undefined = undefined;

  // ── Foundation stacks ──────────────────────────────────────────────
  const network = new NetworkStack(app, `${stackPrefix}-network`, {
    agentCoreAzNames,
  });
  const auth = new IdpAuthenticationStack(
    app,
    `${stackPrefix}-auth`,
    authProps,
  );
  const guardrail = new GuardrailStack(app, `${stackPrefix}-guardrail`);
  const storage = new StorageStack(app, `${stackPrefix}-storage`, {
    network,
    allowedOrigin,
  });
  const authnz = new AuthnzStack(app, `${stackPrefix}-authnz`, {
    claimsMappings: auth.initialClaimsMappings,
  });
  const vkg = new VkgStack(app, `${stackPrefix}-vkg`, {
    network,
    serviceNamespace: network.serviceNamespace,
    ontologyBucket: storage.ontologyArtifactsBucket,
    alarmAction,
  });

  const smusDomain = new NamespaceStack(app, `${stackPrefix}-namespace`, {
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    allowedOrigin,
    vkgClusterArn: vkg.cluster.clusterArn,
    cloudMapNamespaceId: network.serviceNamespace.namespaceId,
    ontologyBucketName: storage.ontologyArtifactsBucket.bucketName,
    ontologyEngineEndpoint: `http://ontology-engine.${prefix}-${envName}-services.local:8001`,
  });
  // The deletion pipeline reads /opensearch/{endpoint,collection-name} to drop
  // the namespace's GraphRAG doc-KG indexes on teardown. CDK cannot infer this
  // from valueForStringParameter, so the dependency must be explicit or a fresh
  // account deploys the namespace stack before those params exist.
  smusDomain.addDependency(storage); // needs /opensearch/endpoint + /opensearch/collection-name

  // ── Service stacks ─────────────────────────────────────────────────
  const metricService = new MetricServiceStack(
    app,
    `${stackPrefix}-metric-service`,
    {
      vpc: network.vpc,
      lambdaSecurityGroup: network.lambdaSecurityGroup,
      aossSecurityGroup: network.aossSecurityGroup,
      neptuneEndpoint: storage.neptuneClusterEndpoint,
      neptuneClusterArn: storage.neptuneClusterArn,
      allowedOrigin,
      alarmAction,
    },
  );
  // Metric validation reads SMUS catalog via SSM-resolved params.
  metricService.addDependency(smusDomain); // /namespace/namespaces-table-name, /smus/domain-id, /smus/dz-project-access-role-arn

  const api = new ApiStack(app, `${stackPrefix}-api`, {
    allowedOrigin,
    ...(customDomain && { customDomain }),
    ...(apiWebAclId && { webAclId: apiWebAclId }),
    alarmAction,
    vpc: network.vpc,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    cacheInvalidationTable: authnz.cacheInvalidationTable,
    ssmPathHandlers: {
      // Namespace CRUD + status lifecycle (GET/PUT/DELETE + PATCH status)
      "/namespaces": `/${prefix}/namespace/api-fn-arn`,
      "/namespaces/{namespaceId}": `/${prefix}/namespace/api-fn-arn`,
      "/namespaces/{namespaceId}/status": `/${prefix}/namespace/api-fn-arn`,
      "/namespaces/{namespaceId}/sources": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/rescan": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}": `/${prefix}/sources/api-fn-arn`,
      // Resource-oriented review endpoints (replaces the bulk POST /review god endpoint)
      "/namespaces/{namespaceId}/sources/{sourceId}/approve": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/reject": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/{sourceId}/metadata": `/${prefix}/sources/api-fn-arn`,
      "/namespaces/{namespaceId}/sources/upload-urls": `/${prefix}/sources/api-fn-arn`,
      "/roles": `/${prefix}/namespace/platform-roles-api-fn-arn`,
      "/namespaces/{namespaceId}/roles": `/${prefix}/namespace/roles-api-fn-arn`,
      "/namespaces/{namespaceId}/roles/{roleId}": `/${prefix}/namespace/roles-api-fn-arn`,
      "/namespaces/{namespaceId}/grants": `/${prefix}/namespace/grants-api-fn-arn`,
      "/namespaces/{namespaceId}/grants/{grantId}": `/${prefix}/namespace/grants-api-fn-arn`,
      "/principals/{principalId}/grants": `/${prefix}/namespace/grants-api-fn-arn`,
      // Platform-scoped grants (cross-namespace roles: platform-admin/viewer)
      "/grants": `/${prefix}/namespace/grants-api-fn-arn`,
      "/grants/{grantId}": `/${prefix}/namespace/grants-api-fn-arn`,
      // ── Metric service ──────────────────────────────────────────
      "/namespaces/{namespaceId}/metrics": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/metrics/{name}": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/metrics/validate": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/bulk-delete-metrics": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/import-osi": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/import-osi/upload-url": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/import-jobs/{jobId}": `/${prefix}/metric/api-fn-arn`,
      "/namespaces/{namespaceId}/export-osi": `/${prefix}/metric/api-fn-arn`,
      // ── Ontology induction ────────────────────────────────────────
      "/namespaces/{namespaceId}/induce": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/induce/jobs": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/induce/jobs/{jobId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/induce/datasources": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/induce/datasources/induced": `/${prefix}/ontology-engine/api-fn-arn`,
      // Note: /induce/datasources/induced takes precedence over /induce/datasources
      // because API GW resolves longer literal paths before greedy ones.
      "/namespaces/{namespaceId}/proposals": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/accept": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/cancel": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints/jobs/{jobId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/validate": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/repair-datatypes": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/{proposalId}/upload-url": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/update": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/proposals/reject": `/${prefix}/ontology-engine/api-fn-arn`,
      // ── Ontology engine — graph, ontologies, embeddings, validation ─
      "/namespaces/{namespaceId}/ontology/foundational": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontology/foundational/{key}/load": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/search": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/ontology-overview": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/class": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/object-property": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/datatype-property": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/{ontologyId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/{ontologyId}/download": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/upload-url": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/{ontologyId}/fetch": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-status/{jobId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/embeddings": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/embeddings/batch": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/embeddings/search": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/embeddings/by-entity/{entityUri}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/embeddings/by-ontology/{ontologyId}": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/validate": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/validate/jobs": `/${prefix}/ontology-engine/api-fn-arn`,
      "/namespaces/{namespaceId}/validate/jobs/{jobId}": `/${prefix}/ontology-engine/api-fn-arn`,
      // ── System health ──────────────────────────────────────────────
      "/system-health": `/${prefix}/ontology-engine/api-fn-arn`,
      // ── Data Layer (Serve) — runtime query/retrieval ───────────────
      "/namespaces/{namespaceId}/query": `/${prefix}/data-layer/api-fn-arn`,
      "/namespaces/{namespaceId}/translate": `/${prefix}/data-layer/api-fn-arn`,
      "/namespaces/{namespaceId}/kb/search": `/${prefix}/data-layer/api-fn-arn`,
      "/namespaces/{namespaceId}/graph/traverse": `/${prefix}/data-layer/api-fn-arn`,
      // DescribeSchema — data-layer proxies to the ontology-engine api-fn.
      "/namespaces/{namespaceId}/schema": `/${prefix}/data-layer/api-fn-arn`,
    },
  });

  const webAppDist = Paths.webAppDist;

  const serve = new ServeStack(app, `${stackPrefix}-serve`, {
    vpc: network.vpc,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    aossSecurityGroup: network.aossSecurityGroup,
    neptuneSecurityGroup: network.neptuneSecurityGroup,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneEndpoint: storage.neptuneClusterEndpoint,
    ontologyBucketArn: storage.ontologyArtifactsBucket.bucketArn,
    athenaResultsBucketName: storage.athenaResultsBucket.bucketName,
    vkgEndpoint: `http://vkg.${prefix}-${envName}-services.local:${vkg.containerPort}`,
    agentCoreAzNames,
    bedrockLlmModelId: config.bedrockLlmModelId,
    allowedOverrideModels: config.allowedOverrideModels,
    alarmAction,
  });
  serve.addDependency(smusDomain); // needs /namespace/namespaces-table-name SSM param
  serve.addDependency(auth); // needs /issuer, /userpool-client-id, /mcp-client-id SSM params

  const sources = new SourcesStack(app, `${stackPrefix}-sources`, {
    network,
    storage,
    allowedOrigin,
    alarmAction,
  });
  sources.addDependency(smusDomain); // needs SSM params from namespace stack
  sources.addDependency(storage); // needs Neptune/OpenSearch endpoints
  sources.addDependency(serve); // needs /serve/runtime-role-arn SSM param (LF consumer grant)

  api.addDependency(sources);
  api.addDependency(metricService);
  api.addDependency(auth); // needs /issuer, /userpool-client-id SSM params
  api.addDependency(smusDomain); // needs /namespace/namespaces-table-name SSM param (authorizer)

  metricService.addDependency(sources); // /sources/sources-table-name SSM param

  const dataLayer = new DataLayerStack(app, `${stackPrefix}-data-layer`, {
    vpc: network.vpc,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    allowedOrigin,
  });
  dataLayer.addDependency(serve); // needs /{prefix}/serve/runtime-arn SSM param
  api.addDependency(dataLayer);

  // CloudFront WebACL: use a caller-provided ARN, else auto-create in
  // us-east-1 via EdgeWafStack and have WebStack read the ARN from SSM.
  let edgeWaf: EdgeWafStack | undefined;
  let cfAutoWebAclParam: { name: string; region: string } | undefined;
  if (!cloudfrontWebAclId && deployAccount) {
    edgeWaf = new EdgeWafStack(app, `${stackPrefix}-edge-waf`, {
      env: { account: deployAccount, region: "us-east-1" },
      webAclArnParameterName: cfWebAclParamName,
    });
    cfAutoWebAclParam = { name: cfWebAclParamName, region: "us-east-1" };
  }

  const web = new WebStack(app, `${stackPrefix}-web`, {
    // Cognito User Pool (and its callback-URL patch) only exists on the
    // COGNITO/SAML idpType path — see idp-authentication-stack.ts. Derived
    // from the same SSM config `auth` was built from, not from `auth`'s
    // construct properties, so this stack has no direct reference into the
    // auth stack's resource tree (see PR for the export-lock this avoids).
    isCognitoMode: config.idpType !== IdpType.OIDC,
    apiEndpoint: api.api.url,
    apiRestApiId: api.api.restApiId,
    apiStageName: api.api.deploymentStage.stageName,
    ...(cloudfrontWebAclId
      ? { webAclId: cloudfrontWebAclId }
      : cfAutoWebAclParam
        ? { autoWebAclParam: cfAutoWebAclParam }
        : {}),
    serveRuntimeArn: serve.queryEndpoint,
    ...(customDomain && { customDomain }),
    ...(fs.existsSync(webAppDist) && { websiteContentPath: webAppDist }),
  });
  web.addDependency(auth);
  web.addDependency(api);
  web.addDependency(serve);
  if (edgeWaf) web.addDependency(edgeWaf);

  // VKG stack created above (vkg variable used by serve stack for endpoint)

  // ── Ontology Engine ────────────────────────────────────────────────
  // SSM params are read via valueForStringParameter (CFN dynamic refs);
  // CDK can't infer dependencies from those, so declare them here so a
  // fresh-account `cdk deploy --all` synthesizes parents first.
  const ontology = new OntologyStack(app, `${stackPrefix}-ontology`, {
    network,
    serviceNamespace: network.serviceNamespace,
    neptuneClusterArn: storage.neptuneClusterArn,
    neptuneClusterEndpoint: storage.neptuneClusterEndpoint,
    ontologyArtifactsBucket: storage.ontologyArtifactsBucket,
    smusDomainId: smusDomain.domainId,
    allowedOrigin,
    alarmAction,
  });
  ontology.addDependency(smusDomain); // /namespace/namespaces-table-name, /smus/dz-project-access-role-arn
  ontology.addDependency(sources); // /sources/sources-table-name

  // ApiStack reads /ontology-engine/api-fn-arn via SSM (CFN dynamic ref).
  api.addDependency(ontology);

  // DataLayerStack reads /{prefix}/ontology-engine/api-fn-arn and
  // /{prefix}/metric/api-fn-arn via SSM (CFN dynamic refs) for its
  // direct-invoke DescribeSchema and ListMetrics handlers — the same source
  // MCP's discovery tools read. CDK cannot infer dependency ordering from
  // ``valueForStringParameter``, so declare it explicitly. Without these,
  // a fresh-account ``cdk deploy --all`` can resolve dataLayer before the
  // parent stacks publish their ARNs, silently baking a dummy value into
  // the Lambda env and 502ing schema/metric-catalog calls until a redeploy.
  dataLayer.addDependency(ontology);
  dataLayer.addDependency(metricService);

  // TODO: Re-enable other service stacks as they are implemented
  // const controlPlane = new ControlPlaneStack(app, `${stackPrefix}-control-plane`);
  // const structured = new StructuredStack(app, `${stackPrefix}-structured`);

  const mcp = new McpStack(app, `${stackPrefix}-mcp`, {
    vpc: network.vpc,
    rolesTable: authnz.rolesTable,
    resourceRoleMappingsTable: authnz.resourceRoleMappingsTable,
    agentCoreAzNames,
  });
  mcp.addDependency(serve); // needs CM runtime-arn SSM param
  mcp.addDependency(metricService); // needs /metric/api-fn-arn SSM param (discovery tools)
  mcp.addDependency(ontology); // needs /ontology-engine/api-fn-arn SSM param (discovery tools)
  mcp.addDependency(auth); // needs /issuer, /userpool-client-id, /mcp-client-id SSM params

  // Inject the resolved brand tokens (BRAND_PREFIX / GRAPH_BASE_URI /
  // EVENT_SOURCE_PREFIX) into every Lambda and ECS container. Applied as an
  // aspect so a new function cannot fall back to the compiled-in default brand.
  // AgentCore runtimes are not constructs the aspect can reach, so serve-stack
  // and mcp-stack spread `brandEnv()` into their environmentVariables directly.
  cdk.Aspects.of(app).add(new BrandEnvAspect());

  app.synth();
}

void deploy();
