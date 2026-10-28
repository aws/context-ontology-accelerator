// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { GuardrailStack } from "../../lib/stacks/foundation";
import {
  guardrailProfileResources,
  resolveGuardrailTier,
} from "../../lib/context";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

// #1255: the Classic tier's content filters don't detect Japanese prompt
// injection. `guardrail_tier=standard` + `guardrail_profile` opts in to the
// Standard tier, which needs a cross-Region guardrail profile.

const BASE = { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: DEFAULT_ENV };
const ENV = { account: "123456789012", region: "ap-northeast-1" };

function guardrails(context: Record<string, unknown>) {
  const app = new cdk.App({ context: { ...BASE, ...context } });
  const stack = new GuardrailStack(app, "TestGuardrail", { env: ENV });
  const found = Template.fromStack(stack).findResources(
    "AWS::Bedrock::Guardrail",
  );
  const byName = (suffix: string) =>
    Object.values(found).find(
      (r) =>
        r.Properties?.Name ===
        `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-${suffix}`,
    )?.Properties;
  return {
    primary: byName("guardrail"),
    retrieval: byName("retrieval-guardrail"),
  };
}

describe("resolveGuardrailTier", () => {
  const node = (context: Record<string, unknown>) =>
    new cdk.App({ context }).node;

  test("defaults to Classic", () => {
    expect(resolveGuardrailTier(node({}))).toEqual({ tierName: "CLASSIC" });
    expect(resolveGuardrailTier(node({ guardrail_tier: "Classic" }))).toEqual({
      tierName: "CLASSIC",
    });
  });

  test("Standard with a profile", () => {
    expect(
      resolveGuardrailTier(
        node({
          guardrail_tier: "STANDARD",
          guardrail_profile: " apac.guardrail.v1:0 ",
        }),
      ),
    ).toEqual({ tierName: "STANDARD", profileId: "apac.guardrail.v1:0" });
  });

  test.each([
    "us.guardrail.v1:0",
    "eu.guardrail.v1:0",
    "us-gov.guardrail.v1:0",
  ])("accepts the system-defined profile id %s", (profile) => {
    expect(
      resolveGuardrailTier(
        node({ guardrail_tier: "standard", guardrail_profile: profile }),
      ).profileId,
    ).toBe(profile);
  });

  test("Classic with a profile keeps Classic and carries the profile", () => {
    expect(
      resolveGuardrailTier(node({ guardrail_profile: "apac.guardrail.v1:0" })),
    ).toEqual({ tierName: "CLASSIC", profileId: "apac.guardrail.v1:0" });
  });

  test.each([
    [{ guardrail_tier: "standard" }, /needs guardrail_profile/],
    [
      { guardrail_tier: "standard", guardrail_profile: "apac" },
      /must be a system-defined guardrail profile id/,
    ],
    [
      {
        guardrail_tier: "standard",
        guardrail_profile:
          "arn:aws:bedrock:ap-northeast-1:123456789012:guardrail-profile/apac.guardrail.v1:0",
      },
      /must be a system-defined guardrail profile id/,
    ],
    [
      { guardrail_profile: "apac" },
      /must be a system-defined guardrail profile id/,
    ],
    [{ guardrail_tier: "premium" }, /must be "classic" or "standard"/],
  ])("rejects %j", (context, message) => {
    expect(() => resolveGuardrailTier(node(context))).toThrow(message);
  });
});

describe("GuardrailStack tier", () => {
  test("Classic leaves the template as before (no tier, no cross-Region)", () => {
    for (const g of Object.values(guardrails({}))) {
      expect(g).toBeDefined();
      expect(g?.CrossRegionConfig).toBeUndefined();
      expect(g?.ContentPolicyConfig?.ContentFiltersTierConfig).toBeUndefined();
    }
    expect(
      guardrails({}).primary?.TopicPolicyConfig?.TopicsTierConfig,
    ).toBeUndefined();
  });

  test("a profile on Classic leaves the guardrails unchanged", () => {
    const before = guardrails({});
    const after = guardrails({ guardrail_profile: "apac.guardrail.v1:0" });
    expect(after).toEqual(before);
  });

  test("Standard sets both guardrails' tiers and the profile", () => {
    const { primary, retrieval } = guardrails({
      guardrail_tier: "standard",
      guardrail_profile: "apac.guardrail.v1:0",
    });
    const profileArn =
      "arn:aws:bedrock:ap-northeast-1:123456789012:guardrail-profile/apac.guardrail.v1:0";
    for (const g of [primary, retrieval]) {
      expect(g?.ContentPolicyConfig?.ContentFiltersTierConfig).toEqual({
        TierName: "STANDARD",
      });
      expect(g?.CrossRegionConfig).toEqual({ GuardrailProfileArn: profileArn });
    }
    expect(primary?.TopicPolicyConfig?.TopicsTierConfig).toEqual({
      TierName: "STANDARD",
    });
    // The retrieval guardrail still has no topic policy to put a tier on.
    expect(retrieval?.TopicPolicyConfig).toBeUndefined();
    // Filter strengths are unchanged by the tier.
    expect(
      retrieval?.ContentPolicyConfig?.FiltersConfig.find(
        (f: { Type: string }) => f.Type === "PROMPT_ATTACK",
      ),
    ).toEqual({
      Type: "PROMPT_ATTACK",
      InputStrength: "MEDIUM",
      OutputStrength: "NONE",
    });
  });
});

describe("guardrailProfileResources", () => {
  const stack = (context: Record<string, unknown>) =>
    new cdk.Stack(new cdk.App({ context }), "S", { env: ENV });

  test("is empty without a profile", () => {
    expect(guardrailProfileResources(stack({}))).toEqual([]);
  });

  test("is granted on Classic too, so roles can update before the tier", () => {
    expect(
      guardrailProfileResources(
        stack({ guardrail_profile: "apac.guardrail.v1:0" }),
      ),
    ).toEqual([
      "arn:aws:bedrock:*:123456789012:guardrail-profile/apac.guardrail.v1:0",
    ]);
  });

  test("covers every destination Region of the profile for Standard", () => {
    expect(
      guardrailProfileResources(
        stack({
          guardrail_tier: "standard",
          guardrail_profile: "apac.guardrail.v1:0",
        }),
      ),
    ).toEqual([
      "arn:aws:bedrock:*:123456789012:guardrail-profile/apac.guardrail.v1:0",
    ]);
  });
});
