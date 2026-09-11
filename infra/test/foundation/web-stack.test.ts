// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { Template, Match } from "aws-cdk-lib/assertions";
import { WebStack } from "../../lib/stacks/foundation";
import {
  readRepoVersion,
  buildRuntimeConfig,
} from "../../lib/stacks/foundation/web-stack";
import { DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";

const BASE_PROPS = {
  isCognitoMode: true,
};

/**
 * Regression coverage for the ExplicitAuthFlows drift bug: the
 * UpdateCognitoCallbacks custom resource calls Cognito's updateUserPoolClient,
 * which is a FULL REPLACEMENT of the client's auth flows. If this list doesn't
 * mirror idp-authentication-stack.ts's addClient() config, a dev deploy of the
 * web stack silently strips ALLOW_USER_PASSWORD_AUTH after the auth stack
 * correctly enabled it — breaking integ-test/CLI Cognito sign-in with no
 * CloudFormation error (the custom resource "succeeds").
 */
describe("WebStack (Cognito ExplicitAuthFlows parity)", () => {
  /**
   * The custom resource's `parameters` are serialized as either:
   * - a plain JSON **string** — when `siteUrl` has no runtime token (e.g. a
   *   static custom domain), CDK can fully resolve the value at synth time; or
   * - an `Fn::Join` of string literals + intrinsics (e.g. `Fn::GetAtt` for the
   *   generated CloudFront domain) — when `siteUrl` depends on a token only
   *   known at deploy time.
   * Neither is safe to `JSON.parse` unconditionally (the Fn::Join form isn't
   * JSON), so normalize both into a searchable string: pass the plain string
   * through as-is, or concatenate only the literal parts of an Fn::Join
   * (skipping intrinsic objects like Fn::GetAtt).
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function joinedCallLiterals(customResource: any): string {
    const call = (customResource.Properties.Update ??
      customResource.Properties.Create) as
      | string
      | { "Fn::Join"?: [string, unknown[]] };
    if (typeof call === "string") return call;
    const parts = call["Fn::Join"]?.[1] ?? [];
    return parts.filter((p): p is string => typeof p === "string").join("");
  }

  test("dev: UpdateCognitoCallbacks includes ALLOW_USER_PASSWORD_AUTH", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebDev", BASE_PROPS),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH","ALLOW_USER_PASSWORD_AUTH"]',
    );
  });

  test("prod: UpdateCognitoCallbacks does NOT include ALLOW_USER_PASSWORD_AUTH", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebProd", BASE_PROPS),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH"]',
    );
    expect(literals).not.toContain("ALLOW_USER_PASSWORD_AUTH");
  });

  test("customDomain still patches Cognito callbacks with the custom UI domain (auth flows unaffected)", () => {
    // NOTE: unlike the pre-customDomain-feature behavior (a bare string that
    // short-circuited all patching), WebStack now always patches Cognito
    // callbacks — customDomain only changes the *siteUrl* used in those
    // callbacks (custom UI domain vs. generated CloudFront domain). This test
    // guards that ExplicitAuthFlows parity (this suite's actual concern)
    // still holds when a custom domain is configured, not just the default path.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCustomDomain", {
        ...BASE_PROPS,
        customDomain: {
          uiDomainName: "app.example.com",
          uiCertificateArn:
            "arn:aws:acm:us-east-1:123456789012:certificate/ui-cert",
          apiDomainName: "api.example.com",
          apiCertificateArn:
            "arn:aws:acm:us-east-1:123456789012:certificate/api-cert",
          hostedZoneId: "Z123456ABCDEFG",
        },
      }),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain("https://app.example.com/authenticate/");
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH","ALLOW_USER_PASSWORD_AUTH"]',
    );
  });
});

describe("WebStack CloudFront WAF WebACL", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function crCall(cr: any): string {
    const call = cr.Properties.Update ?? cr.Properties.Create;
    return typeof call === "string" ? call : "";
  }

  test("uses a provided CloudFront WebACL ARN directly (no SSM reader)", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const arn = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/byo-cf/def";
    const template = Template.fromStack(
      new WebStack(app, "TestWebByoWaf", { ...BASE_PROPS, webAclId: arn }),
    );

    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({ WebACLId: arn }),
    });
    // No custom resource performs an SSM getParameter on the BYO path.
    const readers = Object.values(template.findResources("Custom::AWS")).filter(
      (cr) => crCall(cr).includes("getParameter"),
    );
    expect(readers).toHaveLength(0);
  });

  test("auto-create path reads the WebACL ARN from us-east-1 SSM via a custom resource", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebAutoWaf", {
        ...BASE_PROPS,
        autoWebAclParam: {
          name: "/coa/edge/cloudfront-web-acl-arn",
          region: "us-east-1",
        },
      }),
    );

    const readers = Object.values(template.findResources("Custom::AWS")).filter(
      (cr) => {
        const call = crCall(cr);
        return (
          call.includes("getParameter") &&
          call.includes("/coa/edge/cloudfront-web-acl-arn") &&
          call.includes("us-east-1")
        );
      },
    );
    expect(readers).toHaveLength(1);
  });
});

describe("readRepoVersion (VERSION → runtime-config)", () => {
  test("returns the trimmed, bare-semver contents of the repo-root VERSION file", () => {
    // Independently resolve the monorepo root (dir containing pnpm-workspace.yaml)
    // so the test does not hardcode a version that VERSION bumps will break.
    let dir = __dirname;
    while (!fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) {
      const parent = path.dirname(dir);
      if (parent === dir) throw new Error("monorepo root not found");
      dir = parent;
    }
    const expected = fs.readFileSync(path.join(dir, "VERSION"), "utf-8").trim();

    const version = readRepoVersion();
    expect(version).toBe(expected);
    // The value is interpolated into the UI badge as `v{version}`, so it must
    // be a bare semver (no leading "v", no stray whitespace).
    expect(version).toMatch(/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/);
  });

  test("returns undefined when the VERSION file is missing (deploy is never blocked)", () => {
    const missing = path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "ver-")),
      "VERSION",
    );
    expect(readRepoVersion(missing)).toBeUndefined();
  });

  test("returns undefined for an empty or whitespace-only VERSION file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ver-"));
    const empty = path.join(dir, "VERSION");
    fs.writeFileSync(empty, "   \n\t ");
    expect(readRepoVersion(empty)).toBeUndefined();
  });
});

describe("buildRuntimeConfig (version include/omit)", () => {
  const base = {
    region: "us-east-1",
    stage: "dev",
    authority: "https://issuer",
    clientId: "abc",
  };

  test("includes version when provided", () => {
    expect(buildRuntimeConfig({ ...base, version: "0.2.3" })).toMatchObject({
      version: "0.2.3",
    });
  });

  test("omits the version key entirely when undefined", () => {
    expect(buildRuntimeConfig(base)).not.toHaveProperty("version");
  });

  test("omits the version key when empty", () => {
    expect(buildRuntimeConfig({ ...base, version: "" })).not.toHaveProperty(
      "version",
    );
  });

  test("omits apiEndpoint and serveRuntimeArn when not provided", () => {
    const cfg = buildRuntimeConfig(base);
    expect(cfg).not.toHaveProperty("apiEndpoint");
    expect(cfg).not.toHaveProperty("serveRuntimeArn");
  });
});

describe("WebStack runtime-config.json artifact (end-to-end)", () => {
  test("ships runtime-config.json carrying the repo VERSION", () => {
    // Resolve the repo VERSION independently (no hardcoded value).
    let dir = __dirname;
    while (!fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) {
      dir = path.dirname(dir);
    }
    const expected = fs.readFileSync(path.join(dir, "VERSION"), "utf-8").trim();

    // Synth to a temp outdir and read the actual deploy artifact the
    // BucketDeployment ships — the version lands in this asset, not the
    // CloudFormation template, so this is the only place to assert the full
    // WebStack → runtime-config.json path.
    const outdir = fs.mkdtempSync(path.join(os.tmpdir(), "synth-"));
    const app = new cdk.App({
      outdir,
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    new WebStack(app, "TestWebArtifact", { isCognitoMode: true });
    app.synth();

    const configs: string[] = [];
    const walk = (d: string) => {
      for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        const p = path.join(d, e.name);
        if (e.isDirectory()) walk(p);
        else if (e.name === "runtime-config.json")
          configs.push(fs.readFileSync(p, "utf-8"));
      }
    };
    walk(outdir);

    expect(configs.length).toBeGreaterThan(0);
    expect(configs.some((c) => c.includes(`"version":"${expected}"`))).toBe(
      true,
    );
  });
});

// ═══════════════════════════════════════════════════════════════════
// WebStack — CSP allowlists the Cognito hosted-UI domain (issue #130)
// ═══════════════════════════════════════════════════════════════════
describe("WebStack (CSP hosted-UI origin)", () => {
  /** The synthesized CSP header value from the Response Headers Policy. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function cspOf(template: Template): any {
    const policy = Object.values(
      template.findResources("AWS::CloudFront::ResponseHeadersPolicy"),
    )[0];
    return policy.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig
      .ContentSecurityPolicy.ContentSecurityPolicy;
  }

  test("the CSP is an Fn::Join carrying the hosted-UI host, resolved from SSM", () => {
    // End-to-end for the fix: the prefix arrives as a CFN dynamic reference, so
    // the header cannot be a plain string — it must be a Join whose literal parts
    // include the hosted-UI suffix around the resolved prefix. A plain string here
    // would mean the origin was dropped at synth, which is the bug.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCsp", { isCognitoMode: true }),
    );

    const csp = cspOf(template);
    expect(typeof csp).not.toBe("string");
    const literals: string[] = (csp["Fn::Join"][1] as unknown[]).filter(
      (p): p is string => typeof p === "string",
    );
    const joined = literals.join("");
    expect(joined).toContain(`.auth.`);
    expect(joined).toContain(`.amazoncognito.com`);
    // Both directives, not just connect-src: silent renew uses an iframe.
    expect(joined).toContain("connect-src");
    expect(joined).toContain("frame-src");
    // The exact prefix, never a wildcard host.
    expect(joined).not.toContain("*.auth.");
  });

  test("reads the hosted-UI prefix from the auth stack's parameter", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspParam", { isCognitoMode: true }),
    );

    // `valueForStringParameter` renders as a CFN template Parameter of type
    // AWS::SSM::Parameter::Value<String> whose Default is the path, with the CSP
    // holding only a Ref to it — so the path is asserted here, not in the header.
    // A rename on either side fails here rather than at sign-in time.
    const parameters = template.toJSON().Parameters ?? {};
    const defaults = Object.values(parameters).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (p: any) => p.Default,
    );
    expect(defaults).toContain(
      `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
    );

    // And the CSP really does Ref that parameter, rather than inlining a literal.
    const referenced = Object.entries(parameters)
      .filter(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ([, p]: [string, any]) =>
          p.Default === `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
      )
      .map(([logicalId]) => logicalId);
    expect(referenced.length).toBe(1);
    expect(JSON.stringify(cspOf(template))).toContain(referenced[0]);
  });

  test("direct-OIDC mode neither reads the parameter nor widens the CSP", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspOidc", { isCognitoMode: false }),
    );

    const csp = JSON.stringify(cspOf(template));
    expect(csp).not.toContain("cognito-domain-prefix");
    expect(csp).not.toContain("amazoncognito.com");
  });

  test("the content_security_policy context key replaces the derived policy", () => {
    // The override prop existed but reached nothing, so an operator hitting a CSP
    // gap had to patch CDK source. Wired now — this asserts the wiring, not the
    // prop's existence.
    const custom = "default-src 'none'; script-src 'self'";
    const app = new cdk.App({
      context: {
        resource_prefix: DEFAULT_RESOURCE_PREFIX,
        env: "prod",
      },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspOverride", {
        isCognitoMode: true,
        contentSecurityPolicy: custom,
      }),
    );

    expect(cspOf(template)).toBe(custom);
  });
});
