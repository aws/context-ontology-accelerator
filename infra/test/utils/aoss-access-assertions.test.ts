// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Template } from "aws-cdk-lib/assertions";

import {
  rolesGrantedAossApiAccess,
  uncoveredAossRoles,
} from "./aoss-access-assertions";

const APIACCESS = {
  Version: "2012-10-17",
  Statement: [{ Effect: "Allow", Action: "aoss:APIAccessAll", Resource: "*" }],
};

const role = (extra: object = {}) => ({
  Type: "AWS::IAM::Role",
  Properties: { AssumeRolePolicyDocument: {}, ...extra },
});

/** A data-access policy shaped as synth renders it: JSON fragments joined around tokens. */
const dataPolicy = (...roleIds: string[]) => ({
  Type: "AWS::OpenSearchServerless::AccessPolicy",
  Properties: {
    Type: "data",
    Name: "p",
    Policy: {
      "Fn::Join": [
        "",
        [
          '[{"Rules":[],"Principal":["',
          ...roleIds.flatMap((id, i) => [
            ...(i ? ['","'] : []),
            { "Fn::GetAtt": [id, "Arn"] },
          ]),
          '"]}]',
        ],
      ],
    },
  },
});

describe("uncoveredAossRoles", () => {
  it("does not count a role whose logical ID is a prefix of a named one", () => {
    const template = Template.fromJSON({
      Resources: {
        Fn: role(),
        FnExtra: role(),
        Grant: {
          Type: "AWS::IAM::Policy",
          Properties: {
            PolicyName: "g",
            PolicyDocument: APIACCESS,
            Roles: [{ Ref: "Fn" }, { Ref: "FnExtra" }],
          },
        },
        Data: dataPolicy("FnExtra"),
      },
    });
    expect(uncoveredAossRoles(template)).toEqual(["Fn"]);
  });

  it("counts roles granted through a managed policy", () => {
    const template = Template.fromJSON({
      Resources: {
        Attached: role({ ManagedPolicyArns: [{ Ref: "Managed" }] }),
        Listed: role(),
        Managed: {
          Type: "AWS::IAM::ManagedPolicy",
          Properties: { PolicyDocument: APIACCESS, Roles: [{ Ref: "Listed" }] },
        },
        Data: dataPolicy("Listed"),
      },
    });
    expect(rolesGrantedAossApiAccess(template)).toEqual(["Attached", "Listed"]);
    expect(uncoveredAossRoles(template)).toEqual(["Attached"]);
  });

  it("passes when every granted role is named", () => {
    const template = Template.fromJSON({
      Resources: {
        A: role({ Policies: [{ PolicyName: "i", PolicyDocument: APIACCESS }] }),
        B: role(),
        Grant: {
          Type: "AWS::IAM::Policy",
          Properties: {
            PolicyName: "g",
            PolicyDocument: APIACCESS,
            Roles: [{ Ref: "B" }],
          },
        },
        Data: dataPolicy("A", "B"),
      },
    });
    expect(uncoveredAossRoles(template)).toEqual([]);
  });
});
