// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { GuardrailStack } from "../../lib/stacks/foundation";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
};

describe("GuardrailStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    template = Template.fromStack(new GuardrailStack(app, "TestGuardrail"));
  });

  test("creates Bedrock Guardrail with content filters", () => {
    template.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-guardrail`,
      ContentPolicyConfig: {
        FiltersConfig: [
          { Type: "SEXUAL", InputStrength: "HIGH", OutputStrength: "HIGH" },
          { Type: "VIOLENCE", InputStrength: "HIGH", OutputStrength: "HIGH" },
          { Type: "HATE", InputStrength: "HIGH", OutputStrength: "HIGH" },
          { Type: "INSULTS", InputStrength: "HIGH", OutputStrength: "HIGH" },
          { Type: "MISCONDUCT", InputStrength: "HIGH", OutputStrength: "HIGH" },
          {
            Type: "PROMPT_ATTACK",
            InputStrength: "HIGH",
            OutputStrength: "NONE",
          },
        ],
      },
    });
  });

  test("creates PII anonymization config on the primary guardrail", () => {
    template.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-guardrail`,
      SensitiveInformationPolicyConfig: {
        PiiEntitiesConfig: [
          { Type: "EMAIL", Action: "ANONYMIZE" },
          { Type: "PHONE", Action: "ANONYMIZE" },
          { Type: "NAME", Action: "ANONYMIZE" },
          { Type: "US_SOCIAL_SECURITY_NUMBER", Action: "ANONYMIZE" },
          { Type: "CREDIT_DEBIT_CARD_NUMBER", Action: "ANONYMIZE" },
        ],
      },
    });
  });

  test("primary guardrail denies out-of-scope topics", () => {
    template.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-guardrail`,
      TopicPolicyConfig: {
        TopicsConfig: [
          { Name: "System internals disclosure", Type: "DENY" },
          { Name: "Malicious code generation", Type: "DENY" },
        ],
      },
    });
  });

  test("primary guardrail enables contextual grounding + relevance checks", () => {
    template.hasResourceProperties("AWS::Bedrock::Guardrail", {
      Name: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-guardrail`,
      ContextualGroundingPolicyConfig: {
        FiltersConfig: [
          { Type: "GROUNDING", Threshold: 0.7 },
          { Type: "RELEVANCE", Threshold: 0.7 },
        ],
      },
    });
  });

  test("denied-topic definitions and examples fit Bedrock's limits", () => {
    const guardrails = template.findResources("AWS::Bedrock::Guardrail");
    const topics = Object.values(guardrails).flatMap(
      (r) => r.Properties?.TopicPolicyConfig?.TopicsConfig ?? [],
    ) as { Name: string; Definition: string; Examples: string[] }[];
    expect(topics.length).toBeGreaterThan(0);
    for (const t of topics) {
      expect(t.Name).toMatch(/^[0-9a-zA-Z\-_ !?.]{1,100}$/);
      expect(t.Definition.length).toBeLessThanOrEqual(200);
      expect(t.Examples.length).toBeLessThanOrEqual(5);
      for (const ex of t.Examples) expect(ex.length).toBeLessThanOrEqual(100);
    }
  });

  test("provisions exactly two guardrails", () => {
    template.resourceCountIs("AWS::Bedrock::Guardrail", 2);
  });

  test("retrieval guardrail has NO PII policy (content screening only)", () => {
    const guardrails = template.findResources("AWS::Bedrock::Guardrail");
    const retrieval = Object.values(guardrails).find(
      (r) =>
        r.Properties?.Name ===
        `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-retrieval-guardrail`,
    );
    expect(retrieval).toBeDefined();
    // Anonymizing PII during document/chunk screening would quarantine any
    // document containing a name and strip named entities from the KG.
    expect(
      retrieval?.Properties?.SensitiveInformationPolicyConfig,
    ).toBeUndefined();
    // Denied topics would quarantine ingested security docs; grounding needs a
    // model response, which document screening doesn't have.
    expect(retrieval?.Properties?.TopicPolicyConfig).toBeUndefined();
    expect(
      retrieval?.Properties?.ContextualGroundingPolicyConfig,
    ).toBeUndefined();
    // It must still screen for prompt injection.
    const filters =
      retrieval?.Properties?.ContentPolicyConfig?.FiltersConfig ?? [];
    expect(
      filters.some((f: { Type: string }) => f.Type === "PROMPT_ATTACK"),
    ).toBe(true);
  });

  test("creates SSM params for guardrail", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/bedrock/guardrail-id`,
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/bedrock/guardrail-version`,
    });
  });

  test("has no Cognito resources", () => {
    template.resourceCountIs("AWS::Cognito::UserPool", 0);
  });
});
