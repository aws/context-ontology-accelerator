// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Template } from "aws-cdk-lib/assertions";

/**
 * AOSS authorizes a data-plane call only if IAM grants aoss:APIAccessAll AND a
 * data-access policy names the caller. No policy may name the account root,
 * so every role with the IAM grant needs its own entry; this checks that.
 */

interface Statement {
  Effect?: unknown;
  Action?: unknown;
}

const isRecord = (x: unknown): x is Record<string, unknown> =>
  typeof x === "object" && x !== null && !Array.isArray(x);

/** IAM and synth both allow a single value where a list is expected. */
const toArray = (x: unknown): unknown[] =>
  Array.isArray(x) ? x : x === undefined || x === null ? [] : [x];

const statementsOf = (doc: unknown): Statement[] =>
  isRecord(doc) ? toArray(doc.Statement).filter(isRecord) : [];

const grantsApiAccessAll = (statement: Statement): boolean =>
  statement.Effect === "Allow" &&
  toArray(statement.Action).some(
    (a) => a === "aoss:APIAccessAll" || a === "aoss:*",
  );

/** A role reference as a key: its logical ID for `{Ref}`, else its name or JSON. */
const roleKey = (role: unknown): string =>
  typeof role === "string"
    ? role
    : isRecord(role) && typeof role.Ref === "string"
      ? role.Ref
      : JSON.stringify(role);

/** Roles (logical IDs, or a name for an imported role) granted aoss:APIAccessAll. */
export function rolesGrantedAossApiAccess(template: Template): string[] {
  const roles = new Set<string>();
  const grantingManaged = new Set<string>();
  for (const type of ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"]) {
    for (const [id, res] of Object.entries(template.findResources(type))) {
      const props: unknown = res.Properties;
      if (!isRecord(props)) continue;
      if (!statementsOf(props.PolicyDocument).some(grantsApiAccessAll))
        continue;
      if (type === "AWS::IAM::ManagedPolicy") grantingManaged.add(id);
      for (const role of toArray(props.Roles)) roles.add(roleKey(role));
    }
  }
  for (const [id, res] of Object.entries(
    template.findResources("AWS::IAM::Role"),
  )) {
    const props: unknown = res.Properties;
    if (!isRecord(props)) continue;
    if (
      toArray(props.Policies).some(
        (p) =>
          isRecord(p) &&
          statementsOf(p.PolicyDocument).some(grantsApiAccessAll),
      ) ||
      toArray(props.ManagedPolicyArns).some((arn) =>
        grantingManaged.has(roleKey(arn)),
      )
    )
      roles.add(id);
  }
  return [...roles].sort();
}

/**
 * Principals named by a data-access policy, keyed like rolesGrantedAossApiAccess:
 * `Fn::GetAtt [id, Arn]` gives the logical ID, a literal role ARN the role name.
 */
export function dataPolicyPrincipals(policy: unknown): string[] {
  const tokens: unknown[] = [];
  // Synth renders the policy as a Fn::Join of JSON fragments and tokens; parse it
  // with each token as a placeholder string so only Principal entries are read.
  const join = isRecord(policy) ? policy["Fn::Join"] : undefined;
  const text =
    Array.isArray(join) && typeof join[0] === "string" && Array.isArray(join[1])
      ? join[1]
          .map((part: unknown) =>
            typeof part === "string"
              ? part
              : `@@token${tokens.push(part) - 1}@@`,
          )
          .join(join[0])
      : String(policy);
  const statements: unknown = JSON.parse(text);
  return toArray(statements)
    .filter(isRecord)
    .flatMap((s) =>
      toArray(s.Principal)
        .filter((p): p is string => typeof p === "string")
        .map((p) => {
          const token = /^@@token(\d+)@@$/.exec(p);
          if (!token)
            return p.includes(":role/") ? (p.split("/").pop() ?? p) : p;
          const ref = tokens[Number(token[1])];
          const getAtt = isRecord(ref) ? ref["Fn::GetAtt"] : undefined;
          return Array.isArray(getAtt) &&
            getAtt[1] === "Arn" &&
            typeof getAtt[0] === "string"
            ? getAtt[0]
            : JSON.stringify(ref);
        }),
    );
}

/** Roles granted aoss:APIAccessAll that no data-access policy names exactly. */
export function uncoveredAossRoles(template: Template): string[] {
  const named = new Set(
    Object.values(
      template.findResources("AWS::OpenSearchServerless::AccessPolicy", {
        Properties: { Type: "data" },
      }),
    ).flatMap((p) => dataPolicyPrincipals(p.Properties.Policy)),
  );
  return rolesGrantedAossApiAccess(template).filter((role) => !named.has(role));
}

/** Asserts every aoss:APIAccessAll role is named by a data-access policy, and none names `:root`. */
export function expectAossGrantsCoveredByDataPolicies(
  template: Template,
): void {
  expect(rolesGrantedAossApiAccess(template).length).toBeGreaterThan(0);
  expect(uncoveredAossRoles(template)).toEqual([]);
  for (const p of Object.values(
    template.findResources("AWS::OpenSearchServerless::AccessPolicy", {
      Properties: { Type: "data" },
    }),
  ))
    expect(JSON.stringify(p.Properties.Policy)).not.toContain(":root");
}
