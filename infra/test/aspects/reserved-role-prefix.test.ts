// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { Construct } from "constructs";
import { SCLStack } from "../../lib/constructs/scl-stack";
import { RESERVED_DATASOURCE_ROLE_SEGMENT } from "../../lib/aspects/reserved-role-prefix";

/**
 * The synth-time half of the credential-reach bound.
 *
 * The `sts:AssumeRole` grants are scoped to
 * `arn:aws:iam::*:role/{prefix}-{env}-datasource-access-*` with no account
 * restriction, so the ARN suffix — IAM `path` + `roleName` — is the whole
 * boundary. Cases are split by which half carries the prefix: `roleName` is
 * CloudFormation-generated for most roles here, `path` is a literal whenever it
 * is set at all.
 */
class ProbeStack extends SCLStack {
  constructor(
    scope: Construct,
    id: string,
    role: { readonly roleName?: string; readonly path?: string },
  ) {
    super(scope, id);
    this.addComponentTag("probe");
    new iam.Role(this, "Probe", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      roleName: role.roleName,
      path: role.path,
    });
  }
}

const synth = (role: {
  readonly roleName?: string;
  readonly path?: string;
}): Template =>
  Template.fromStack(new ProbeStack(new cdk.App(), "Probe", role));

describe("ReservedRoleNamePrefix", () => {
  it("exports the segment the assume grants are built from", () => {
    expect(RESERVED_DATASOURCE_ROLE_SEGMENT).toBe("datasource-access-");
  });

  it("allows an ordinary prefixed role name", () => {
    expect(() => synth({ roleName: "coa-dev-sources-api" })).not.toThrow();
  });

  it("allows an unnamed role, whose generated name cannot start with the prefix", () => {
    expect(() => synth({})).not.toThrow();
  });

  it("fails the synth for a role named under the reserved prefix", () => {
    expect(() =>
      synth({ roleName: "coa-dev-datasource-access-probe" }),
    ).toThrow(
      /falls under the reserved datasource-access prefix "coa-dev-datasource-access-"/,
    );
  });

  it("names the offending role and the construct path in the message", () => {
    expect(() =>
      synth({ roleName: "coa-dev-datasource-access-probe" }),
    ).toThrow(/coa-dev-datasource-access-probe.*Probe\/Probe\/Resource/s);
  });

  // IAM matches the wildcard against everything after `role/`, the path included.
  it("catches the prefix in the role path, not just the role name", () => {
    expect(() =>
      synth({
        roleName: "harmless",
        path: "/coa-dev-datasource-access-x/",
      }),
    ).toThrow(/coa-dev-datasource-access-x\/harmless/);
  });

  // The case a name-first check misses, and the common shape here: an unnamed
  // role at a reserved path still deploys as
  // `role/coa-dev-datasource-access-audit/{generated}`, which the grant matches.
  // So the path must be read before any early return on the name.
  it("catches a reserved path on a role with NO explicit name", () => {
    expect(() => synth({ path: "/coa-dev-datasource-access-audit/" })).toThrow(
      /Role "coa-dev-datasource-access-audit\/\*".*falls under the reserved/s,
    );
  });

  it("allows an unrelated path", () => {
    expect(() =>
      synth({ roleName: "harmless", path: "/service-role/" }),
    ).not.toThrow();
  });

  // A path always ends with `/` and the prefix contains none, so a normalisation
  // that concatenates the two cannot start matching across the boundary.
  it("does not match a prefix straddling the path and the name", () => {
    expect(() =>
      synth({ roleName: "access-probe", path: "/coa-dev-datasource/" }),
    ).not.toThrow();
  });

  // A token is a `string` at the type level, so the next three are ordinary
  // calls. Nothing about a fully tokenised name is knowable at synth, and
  // throwing would block a legitimate deployment-time-named role.
  it("allows a fully tokenised name, which is genuinely undecidable", () => {
    expect(() => synth({ roleName: cdk.Aws.ACCOUNT_ID })).not.toThrow();
  });

  // The literal head is inside the grant, so the deployed role matches however
  // the token resolves.
  it("catches a name whose literal head is under the prefix, with a token tail", () => {
    expect(() =>
      synth({
        roleName: `coa-dev-datasource-access-${cdk.Aws.ACCOUNT_ID}`,
      }),
    ).toThrow(
      /unresolved name .* contains the reserved datasource-access prefix/,
    );
  });

  // Over-approximation on record: the prefix anywhere in the resolved structure
  // fails the build, because a head-only check would miss `Fn::Sub`.
  it("also catches the prefix mid-token, erring toward failing the build", () => {
    expect(() =>
      synth({
        roleName: `svc-coa-dev-datasource-access-${cdk.Aws.ACCOUNT_ID}`,
      }),
    ).toThrow(/contains the reserved datasource-access prefix/);
  });

  // A role that is fine under one deployment's prefix is an escalation under
  // another.
  it("follows the deployment's own prefix and environment", () => {
    const app = new cdk.App({
      context: { resource_prefix: "scl", env: "prod" },
    });
    expect(() =>
      Template.fromStack(
        new ProbeStack(app, "Probe", {
          roleName: "scl-prod-datasource-access-probe",
        }),
      ),
    ).toThrow(
      /reserved datasource-access prefix "scl-prod-datasource-access-"/,
    );
  });

  it("does not reject a name that only matches another deployment's prefix", () => {
    const app = new cdk.App({
      context: { resource_prefix: "scl", env: "prod" },
    });
    expect(() =>
      Template.fromStack(
        new ProbeStack(app, "Probe", {
          roleName: "coa-dev-datasource-access-probe",
        }),
      ),
    ).not.toThrow();
  });
});

/**
 * The aspect is attached by `SCLStack`'s constructor, so it is opt-in by
 * inheritance: a stack written `extends cdk.Stack` gets no check and nothing
 * about its own code looks wrong. Attaching at `App` scope would close that by
 * construction, but `infra/bin/app.ts` reads SSM at module scope and cannot be
 * synthesised here, and it would miss a stack a test synthesises directly. So the
 * inheritance itself is what gets asserted.
 *
 * A source scan rather than `instanceof` over the barrel exports, because a stack
 * not exported from `index.ts` is exactly the one a `cdk synth` would deploy.
 */
describe("every stack inherits the aspect", () => {
  // All of `infra/lib`, not just `lib/stacks`: a stack defined beside a construct
  // would otherwise escape the scan.
  const LIB_DIR = path.join(__dirname, "..", "..", "lib");

  function sourceFiles(directory: string): string[] {
    return fs
      .readdirSync(directory, { withFileTypes: true })
      .flatMap((entry) => {
        const full = path.join(directory, entry.name);
        if (entry.isDirectory()) {
          return sourceFiles(full);
        }
        return entry.isFile() && entry.name.endsWith(".ts") ? [full] : [];
      });
  }

  function classDeclarations(): Array<{
    file: string;
    className: string;
    baseClass: string;
  }> {
    const found: Array<{
      file: string;
      className: string;
      baseClass: string;
    }> = [];
    for (const file of sourceFiles(LIB_DIR)) {
      const source = fs.readFileSync(file, "utf8");
      const pattern =
        /^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)\s+extends\s+([\w.]+)/gm;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(source)) !== null) {
        found.push({
          file: path.relative(LIB_DIR, file),
          className: match[1],
          baseClass: match[2],
        });
      }
    }
    return found;
  }

  it("finds the classes it means to check", () => {
    // Without this, a regex that silently stops matching would read as a clean
    // bill of health.
    const declarations = classDeclarations();
    expect(declarations.length).toBeGreaterThanOrEqual(20);
    const names = declarations.map((c) => c.className);
    expect(names).toContain("SourcesStack");
    expect(names).toContain("SCLStack");
  });

  // Keyed on the BASE class: `endsWith("Stack")` alone would let a stack called
  // `SourcesPipeline` opt out silently.
  it("lets nothing but SCLStack extend cdk.Stack directly", () => {
    const offenders = classDeclarations().filter(
      ({ className, baseClass }) =>
        (baseClass === "cdk.Stack" || baseClass === "Stack") &&
        className !== "SCLStack",
    );
    expect(offenders).toEqual([]);
  });

  // Kept alongside the rule above: this one catches a `*Stack` extending some
  // third thing (a stale local base class), which that check would not see.
  it("declares no *Stack class extending anything but SCLStack", () => {
    const offenders = classDeclarations().filter(
      ({ className, baseClass }) =>
        className.endsWith("Stack") &&
        className !== "SCLStack" &&
        baseClass !== "SCLStack",
    );
    expect(offenders).toEqual([]);
  });

  it("keeps SCLStack itself the thing that attaches the aspect", () => {
    // If the attachment moves out of the base class, the invariants above stop
    // meaning anything.
    const base = fs.readFileSync(
      path.join(LIB_DIR, "constructs", "scl-stack.ts"),
      "utf8",
    );
    expect(base).toContain("ReservedRoleNamePrefix");
    expect(base).toContain("Aspects.of(this).add");
  });
});
