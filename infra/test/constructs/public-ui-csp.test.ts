// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { buildContentSecurityPolicy } from "../../lib/constructs/public-ui-construct";
import type { RuntimeConfig } from "@coa/shared";

const baseConfig: RuntimeConfig = {
  region: "us-east-1",
  stage: "dev",
  authority: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123",
  clientId: "client-abc",
};

/** Parse a CSP string into a directive → sources map. */
function parse(csp: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const part of csp.split(";").map((p) => p.trim())) {
    if (!part) continue;
    const [name, ...sources] = part.split(/\s+/);
    out[name] = sources;
  }
  return out;
}

describe("buildContentSecurityPolicy", () => {
  it("always allows the Cognito IdP endpoint in connect-src, derived from region even when authority is unset", () => {
    // Regression: `authority` can be empty/unresolved at synth; the Cognito
    // origin must still be allowlisted or the OIDC login flow (discovery/JWKS/
    // token) is blocked by connect-src.
    const csp = parse(
      buildContentSecurityPolicy({
        region: "us-west-2",
        stage: "dev",
        clientId: "client-abc",
        // authority intentionally omitted
      } as RuntimeConfig),
    );
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining(["https://cognito-idp.us-west-2.amazonaws.com"]),
    );
  });

  it("allows the region's S3 origin in connect-src for the OSI presigned upload", () => {
    // OSI import PUTs directly to S3 from the browser (issue 103). Without the
    // S3 origin here, connect-src blocks the upload with "Failed to fetch".
    const csp = parse(
      buildContentSecurityPolicy({
        ...baseConfig,
        apiEndpoint: "https://api.example.cloudfront.net/prod",
      }),
    );
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining([
        "https://*.s3.us-east-1.amazonaws.com",
        "https://s3.us-east-1.amazonaws.com",
      ]),
    );
  });

  it("blocks inline/cross-origin scripts with script-src 'self'", () => {
    const csp = parse(buildContentSecurityPolicy(baseConfig));
    expect(csp["script-src"]).toEqual(["'self'"]);
  });

  it("applies clickjacking + base-uri + object-src hardening", () => {
    const csp = parse(buildContentSecurityPolicy(baseConfig));
    expect(csp["frame-ancestors"]).toEqual(["'none'"]);
    expect(csp["object-src"]).toEqual(["'none'"]);
    expect(csp["base-uri"]).toEqual(["'self'"]);
    expect(csp["default-src"]).toEqual(["'self'"]);
  });

  it("scopes connect-src to the specific API/authority origins when known", () => {
    const csp = parse(
      buildContentSecurityPolicy({
        ...baseConfig,
        apiEndpoint: "https://api.example.cloudfront.net/prod",
      }),
    );
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining([
        "'self'",
        "https://api.example.cloudfront.net",
        "https://cognito-idp.us-east-1.amazonaws.com",
      ]),
    );
    // Fully wired → no scheme-level wildcards.
    expect(csp["connect-src"]).not.toContain("https:");
  });

  it("falls back to scheme-level connect-src before endpoints are wired", () => {
    const csp = parse(buildContentSecurityPolicy(baseConfig)); // no api
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining(["'self'", "https:"]),
    );
  });

  it("allows the OIDC authority in frame-src for silent renew", () => {
    const csp = parse(buildContentSecurityPolicy(baseConfig));
    expect(csp["frame-src"]).toEqual(
      expect.arrayContaining([
        "'self'",
        "https://cognito-idp.us-east-1.amazonaws.com",
      ]),
    );
  });

  it("returns the caller override verbatim when provided", () => {
    const custom = "default-src 'none'; script-src 'self'";
    expect(buildContentSecurityPolicy(baseConfig, custom)).toBe(custom);
  });

  it("ignores malformed endpoint URLs without throwing", () => {
    const csp = parse(
      buildContentSecurityPolicy({ ...baseConfig, apiEndpoint: "not a url" }),
    );
    // apiEndpoint unusable → treated as not-wired → scheme fallback present.
    expect(csp["connect-src"]).toContain("https:");
  });
});
