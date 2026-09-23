// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "fs";

const HTTP_METHODS = ["get", "put", "post", "delete", "patch", "head"] as const;

/**
 * Reads an OpenAPI JSON file and replaces placeholder values.
 *
 * NOTE: Placeholders are replaced via string substitution. Do not pass
 * user-controlled values — placeholder values must be trusted constants.
 */
export function readOpenApiSpec(
  filePath: string,
  placeholders?: Record<string, string>,
): Record<string, any> {
  let content = readFileSync(filePath, "utf-8");
  if (placeholders) {
    for (const [key, value] of Object.entries(placeholders)) {
      content = content.replaceAll(`\${${key}}`, value);
    }
  }
  return JSON.parse(content);
}

/**
 * Merges the `paths` of an overlay OpenAPI spec into `base`, mutating `base`.
 *
 * One REST API fronts two Smithy services, so their generated specs are
 * combined at synth time. The merge is per method, which lets each spec
 * contribute a different method on a shared path.
 *
 * A path+method defined by BOTH specs is a modelling error, not a conflict to
 * resolve silently. API Gateway takes one integration per path, so a plain
 * object spread would publish the losing operation's contract (its query
 * parameters and response schema) in front of the winning operation's handler —
 * an API whose documentation contradicts its behaviour, with a green build.
 * Fail `cdk synth` instead.
 *
 * Scoped to `paths` deliberately: `components.schemas` legitimately overlaps,
 * because shapes shared via `common.smithy` (e.g. `ValidationError`) are
 * emitted identically into both specs.
 */
export function mergeOpenApiPaths(
  base: Record<string, any>,
  overlay: Record<string, any>,
): Record<string, any> {
  base.paths = base.paths ?? {};

  for (const [apiPath, methods] of Object.entries<any>(overlay.paths ?? {})) {
    const existing = base.paths[apiPath];
    if (!existing) {
      base.paths[apiPath] = methods;
      continue;
    }

    const clashes = Object.keys(methods).filter((method) => method in existing);
    if (clashes.length > 0) {
      throw new Error(
        `OpenAPI merge conflict on "${apiPath}": [${clashes.join(", ")}] ` +
          `defined by both specs. A path+method resolves to a single API Gateway ` +
          `integration, so give the duplicate operation its own path or remove it ` +
          `from its service.`,
      );
    }

    base.paths[apiPath] = { ...existing, ...methods };
  }

  return base;
}

/**
 * Mapping of API path to the Lambda ARN(s) that handle it.
 *
 * - A string value routes all HTTP methods to that Lambda.
 * - An object value routes specific methods to different Lambdas,
 *   with an optional `_default` fallback for unmapped methods.
 *
 * @example
 * ```ts
 * {
 *   "/namespaces": {
 *     GET: listNamespacesFn.functionArn,
 *     _default: createNamespaceFn.functionArn,
 *   },
 *   "/namespaces/{namespaceId}": namespaceByIdFn.functionArn,
 * }
 * ```
 */
export type MethodLambdaMap = { _default?: string } & Partial<
  Record<Uppercase<(typeof HTTP_METHODS)[number]>, string>
>;
export type PathLambdaMap = Record<string, string | MethodLambdaMap>;

/**
 * Injects `x-amazon-apigateway-integration` on every operation,
 * using AWS_PROXY (Lambda proxy) integration with one Lambda per API path.
 */
export function injectLambdaProxyIntegrations(
  spec: Record<string, any>,
  opts: { pathLambdaMap: PathLambdaMap; region: string },
): Record<string, any> {
  const paths = spec.paths ?? {};

  for (const [apiPath, methods] of Object.entries<any>(paths)) {
    const mapping = opts.pathLambdaMap[apiPath];
    if (!mapping) {
      throw new Error(
        `No Lambda ARN mapped for path "${apiPath}". ` +
          `Mapped paths: [${Object.keys(opts.pathLambdaMap).join(", ")}]`,
      );
    }

    const isSimple = typeof mapping === "string";
    const defaultArn = isSimple ? mapping : mapping._default;

    for (const method of HTTP_METHODS) {
      const operation = methods[method];
      if (!operation) continue;

      const methodKey = method.toUpperCase() as Uppercase<typeof method>;
      const lambdaArn = isSimple
        ? mapping
        : ((mapping as MethodLambdaMap)[methodKey] ?? defaultArn);

      if (!lambdaArn) {
        throw new Error(
          `No Lambda ARN for ${method.toUpperCase()} ${apiPath}. ` +
            `Provide a method-specific or _default mapping.`,
        );
      }

      const uri = `arn:aws:apigateway:${opts.region}:lambda:path/2015-03-31/functions/${lambdaArn}/invocations`;

      addSecurityResponseHeaders(operation);

      operation["x-amazon-apigateway-integration"] = {
        type: "aws_proxy",
        httpMethod: "POST",
        uri,
        passthroughBehavior: "when_no_match",
      };
    }
  }

  return spec;
}

/**
 * Replaces `Fn::Sub` placeholders in the authorizer config with actual values.
 */
export function fillAuthorizerParameters(
  spec: Record<string, any>,
  opts: {
    authorizerLambdaArn: string;
    authorizerRoleArn: string;
    region: string;
    /** Paths that should remain unauthenticated (e.g. "/health"). */
    unsecuredPaths?: string[];
  },
): Record<string, any> {
  const invokeUri = `arn:aws:apigateway:${opts.region}:lambda:path/2015-03-31/functions/${opts.authorizerLambdaArn}/invocations`;
  const unsecured = new Set(opts.unsecuredPaths ?? []);

  const schemes = spec.components?.securitySchemes ?? {};
  const schemeNames = Object.keys(schemes);
  for (const scheme of Object.values<any>(schemes)) {
    const authorizer = scheme["x-amazon-apigateway-authorizer"];
    if (!authorizer) continue;

    // API Gateway requires apiKey type for custom Lambda authorizers;
    // Smithy generates "http"/"Bearer" which APIGW doesn't recognise.
    scheme.type = "apiKey";
    scheme.name = "Authorization";
    scheme.in = "header";
    delete scheme.scheme;
    delete scheme.description;

    authorizer.authorizerUri = invokeUri;
    authorizer.authorizerCredentials = opts.authorizerRoleArn;
  }

  // API Gateway ignores top-level `security`; it must be set per-operation.
  if (schemeNames.length > 0) {
    const securityEntry = schemeNames.map((name) => ({ [name]: [] }));
    const paths = spec.paths ?? {};
    for (const [apiPath, methods] of Object.entries<any>(paths)) {
      for (const method of HTTP_METHODS) {
        const operation = methods[method];
        if (!operation) continue;
        if (unsecured.has(apiPath)) {
          operation.security = [];
        } else if (!operation.security) {
          operation.security = securityEntry;
        }
      }
    }
  }

  return spec;
}

// ── Internal helpers ──────────────────────────────────────────────────────────

function addSecurityResponseHeaders(operation: any): void {
  const responses = operation.responses ?? {};
  for (const response of Object.values<any>(responses)) {
    response.headers = {
      ...(response.headers ?? {}),
      "Strict-Transport-Security": { schema: { type: "string" } },
      "X-Content-Type-Options": { schema: { type: "string" } },
      "X-Frame-Options": { schema: { type: "string" } },
      "Cache-Control": { schema: { type: "string" } },
      "Referrer-Policy": { schema: { type: "string" } },
    };
  }
}
