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

  it("also allows the legacy global S3 host in connect-src (us-east-1 presigns it)", () => {
    // Regression for issue #211: boto3 presigns S3 URLs for a us-east-1 bucket
    // using the legacy global virtual-hosted host `<bucket>.s3.amazonaws.com`
    // (no region segment), NOT the regional `s3.us-east-1.amazonaws.com`. The
    // region-only allowlist above never matches it, so the browser refuses the
    // presigned proposal fetch with "Failed to fetch" and the proposal detail
    // page shows "Could not load proposal". The global host must be allowlisted
    // alongside the regional forms. Both the virtual-hosted wildcard and the
    // path-style host are included, mirroring the regional pair.
    const csp = parse(
      buildContentSecurityPolicy({
        ...baseConfig,
        apiEndpoint: "https://api.example.cloudfront.net/prod",
      }),
    );
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining([
        "https://*.s3.amazonaws.com",
        "https://s3.amazonaws.com",
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

describe("buildContentSecurityPolicy — Cognito hosted-UI origin (issue #130)", () => {
  const PREFIX = "coa-prod-auth-123456789012";
  const HOSTED_UI = `https://${PREFIX}.auth.us-east-1.amazoncognito.com`;

  it("allows the hosted-UI origin in connect-src for the OAuth token exchange", () => {
    // The regression this pins: the token endpoint is on the hosted-UI domain,
    // NOT on the issuer's `cognito-idp.<region>.amazonaws.com`. Allowlisting only
    // the issuer blocks the PKCE code→token POST and every sign-in fails, with
    // nothing visible in the app — only a CSP violation in devtools.
    const csp = parse(
      buildContentSecurityPolicy(baseConfig, undefined, PREFIX),
    );
    expect(csp["connect-src"]).toEqual(expect.arrayContaining([HOSTED_UI]));
  });

  it("allows the hosted-UI origin in frame-src for automaticSilentRenew", () => {
    // Silent renew runs in a hidden iframe against the hosted-UI authorize
    // endpoint, so connect-src alone is not enough: tokens would stop renewing
    // and the session would die at the first expiry even if login succeeded.
    const csp = parse(
      buildContentSecurityPolicy(baseConfig, undefined, PREFIX),
    );
    expect(csp["frame-src"]).toEqual(expect.arrayContaining([HOSTED_UI]));
  });

  it("keeps the issuer origin as well — they are different hosts", () => {
    const csp = parse(
      buildContentSecurityPolicy(baseConfig, undefined, PREFIX),
    );
    // Discovery and JWKS stay on the issuer; both origins are required.
    for (const directive of ["connect-src", "frame-src"]) {
      expect(csp[directive]).toEqual(
        expect.arrayContaining([
          "https://cognito-idp.us-east-1.amazonaws.com",
          HOSTED_UI,
        ]),
      );
    }
  });

  it("builds the origin by interpolation, so an unresolved SSM token survives", () => {
    // The prefix arrives as a CFN dynamic reference. Parsing it (`new URL`)
    // would throw and silently drop the origin — which is exactly how the
    // `authority`-derived origin fails today. Interpolation keeps the token
    // intact for CloudFormation to resolve at deploy time.
    const token = "${Token[TOKEN.123]}";
    const csp = parse(buildContentSecurityPolicy(baseConfig, undefined, token));
    expect(csp["connect-src"]).toEqual(
      expect.arrayContaining([
        `https://${token}.auth.us-east-1.amazoncognito.com`,
      ]),
    );
  });

  it("omits the hosted-UI origin for a direct-OIDC deployment", () => {
    // No user pool → no hosted UI → nothing to allowlist. Asserted so the fix
    // cannot become an unconditional widening of the policy.
    const csp = parse(buildContentSecurityPolicy(baseConfig));
    expect(
      csp["connect-src"].some((s) => s.includes("amazoncognito.com")),
    ).toBe(false);
    expect(csp["frame-src"].some((s) => s.includes("amazoncognito.com"))).toBe(
      false,
    );
  });

  it("never emits a wildcard amazoncognito.com host", () => {
    // Canary: the cheap fix for #130 is `https://*.auth.<region>.amazoncognito.com`,
    // which would allow every Cognito hosted UI in the region. Keep it exact.
    const csp = buildContentSecurityPolicy(baseConfig, undefined, PREFIX);
    expect(csp).not.toContain("*.auth.");
  });

  it("still lets a caller override win over the derived hosted-UI origin", () => {
    const custom = "default-src 'none'";
    expect(buildContentSecurityPolicy(baseConfig, custom, PREFIX)).toBe(custom);
  });
});
