// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_BEDROCK_MODEL_ID,
} from "../../lib/constants";
import {
  MODEL_ID_CONFIG_KEYS,
  assertModelIdsInvocableFromRegion,
  geoPrefixOf,
  modelIdRegionConflict,
  normalizeModelIdConfig,
  parseDeploymentConfig,
  regionGeographies,
  resolveConfiguredModelIds,
} from "../../lib/utils/model-region";

describe("geoPrefixOf", () => {
  it.each([
    ["us.anthropic.claude-sonnet-5", "us"],
    ["eu.anthropic.claude-sonnet-4-6", "eu"],
    ["apac.anthropic.claude-sonnet-5", "apac"],
    ["jp.anthropic.claude-haiku-4-5-20251001-v1:0", "jp"],
    ["au.anthropic.claude-sonnet-5", "au"],
    ["ca.amazon.nova-lite-v1:0", "ca"],
    ["global.cohere.embed-v4:0", "global"],
    ["us-gov.anthropic.claude-sonnet-4-6", "us-gov"],
  ])("reads the geography off %s", (id, geo) => {
    expect(geoPrefixOf(id)).toBe(geo);
  });

  it.each([
    "cohere.embed-v4:0",
    "anthropic.claude-sonnet-5",
    "amazon.titan-embed-text-v2:0",
  ])("leaves a bare in-region id unjudged: %s", (id) => {
    expect(geoPrefixOf(id)).toBeUndefined();
  });

  it("leaves an application inference profile ARN unjudged", () => {
    expect(
      geoPrefixOf(
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
      ),
    ).toBeUndefined();
  });

  it("leaves a geography it does not know unjudged (no code change for a new geo)", () => {
    expect(geoPrefixOf("sa.anthropic.claude-sonnet-5")).toBeUndefined();
  });
});

describe("regionGeographies", () => {
  it.each([
    ["us-east-1", ["us", "global"]],
    ["us-west-2", ["us", "global"]],
    ["eu-central-1", ["eu", "global"]],
    ["ap-northeast-1", ["jp", "apac", "global"]],
    ["ap-northeast-3", ["jp", "apac", "global"]],
    ["ap-southeast-2", ["au", "apac", "global"]],
    ["ap-southeast-1", ["apac", "global"]],
    ["ap-south-1", ["apac", "global"]],
    ["ca-central-1", ["ca", "global"]],
    ["sa-east-1", ["global"]],
    ["me-central-1", ["apac", "global"]],
    ["us-gov-west-1", ["us-gov"]],
    ["cn-north-1", []],
  ])("%s → %j", (region, geos) => {
    expect([...regionGeographies(region)].sort()).toEqual([...geos].sort());
  });
});

describe("modelIdRegionConflict", () => {
  // The #1020 report: every default is a us. profile, deployed to Tokyo.
  it.each([
    DEFAULT_BEDROCK_LLM_MODEL_ID,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
    DEFAULT_BEDROCK_CHAT_MODEL_ID,
  ])("flags the built-in default %s in ap-northeast-1", (id) => {
    expect(modelIdRegionConflict(id, "ap-northeast-1")).toMatch(
      /'us\.' inference profile, which Bedrock does not publish in ap-northeast-1/,
    );
  });

  it.each([
    // The verified Tokyo workaround from #1020, plus the docs' example config.
    ["global.anthropic.claude-sonnet-5", "ap-northeast-1"],
    ["global.cohere.embed-v4:0", "ap-northeast-1"],
    ["jp.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-northeast-1"],
    ["apac.anthropic.claude-sonnet-4-6", "ap-northeast-1"],
    ["cohere.embed-v4:0", "ap-northeast-1"],
    ["jp.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-northeast-3"],
    ["au.anthropic.claude-sonnet-5", "ap-southeast-2"],
    ["apac.anthropic.claude-sonnet-5", "ap-southeast-2"],
    ["eu.anthropic.claude-sonnet-4-6", "eu-west-1"],
    ["us.anthropic.claude-sonnet-5", "us-east-1"],
    ["us.cohere.embed-v4:0", "us-west-2"],
    ["ca.amazon.nova-lite-v1:0", "ca-central-1"],
    ["global.anthropic.claude-sonnet-5", "sa-east-1"],
    ["apac.anthropic.claude-sonnet-5", "me-central-1"],
    ["us-gov.anthropic.claude-sonnet-4-6", "us-gov-west-1"],
    // Unknown geography: not judged.
    ["sa.anthropic.claude-sonnet-5", "us-east-1"],
  ])("accepts %s in %s", (id, region) => {
    expect(modelIdRegionConflict(id, region)).toBeUndefined();
  });

  it.each([
    ["jp.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-southeast-2"],
    ["au.anthropic.claude-sonnet-5", "ap-northeast-1"],
    ["eu.anthropic.claude-sonnet-4-6", "us-east-1"],
    ["us.anthropic.claude-sonnet-5", "eu-west-1"],
    ["us.anthropic.claude-sonnet-5", "sa-east-1"],
    ["apac.anthropic.claude-sonnet-5", "us-east-1"],
    ["ca.amazon.nova-lite-v1:0", "us-east-1"],
    ["us.anthropic.claude-sonnet-5", "me-central-1"],
    ["us.anthropic.claude-sonnet-5", "us-gov-west-1"],
    ["global.anthropic.claude-sonnet-5", "us-gov-west-1"],
    ["global.anthropic.claude-sonnet-5", "cn-north-1"],
  ])("rejects %s in %s", (id, region) => {
    expect(modelIdRegionConflict(id, region)).toBeDefined();
  });
});

describe("resolveConfiguredModelIds", () => {
  it("applies the same default each stack applies when a key is absent", () => {
    const resolved = resolveConfiguredModelIds({});
    expect(resolved.map((r) => r.key)).toEqual([...MODEL_ID_CONFIG_KEYS]);
    expect(resolved).toEqual(
      expect.arrayContaining([
        {
          key: "bedrockLlmModelId",
          modelId: DEFAULT_BEDROCK_LLM_MODEL_ID,
          origin: "default",
        },
        {
          key: "bedrockEmbedModelId",
          modelId: DEFAULT_BEDROCK_MODEL_ID,
          origin: "default",
        },
        {
          key: "bedrockInductionLlmModelId",
          modelId: DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
          origin: "default",
        },
        {
          key: "bedrockChatModelId",
          modelId: DEFAULT_BEDROCK_CHAT_MODEL_ID,
          origin: "default",
        },
      ]),
    );
  });

  it("takes the configured value verbatim and says so", () => {
    const resolved = resolveConfiguredModelIds({
      bedrockEmbedModelId: "cohere.embed-v4:0",
    });
    expect(resolved).toContainEqual({
      key: "bedrockEmbedModelId",
      modelId: "cohere.embed-v4:0",
      origin: "config",
    });
  });

  it("normalizes empty model-id config values to the deployed defaults", () => {
    const resolved = resolveConfiguredModelIds({
      bedrockLlmModelId: "",
      bedrockEmbedModelId: "   ",
    });
    expect(resolved).toContainEqual({
      key: "bedrockLlmModelId",
      modelId: DEFAULT_BEDROCK_LLM_MODEL_ID,
      origin: "default",
    });
    expect(resolved).toContainEqual({
      key: "bedrockEmbedModelId",
      modelId: DEFAULT_BEDROCK_MODEL_ID,
      origin: "default",
    });
  });
});

describe("deployment config parsing", () => {
  const PARAM = "/coa/config";

  it("removes empty model IDs before the config reaches the stacks", () => {
    expect(
      normalizeModelIdConfig({
        bedrockLlmModelId: "",
        bedrockEmbedModelId: " ",
        bedrockChatModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      }),
    ).toEqual({
      bedrockChatModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    });
  });

  it.each([
    ["not JSON", "{"],
    ["a non-object root", "[]"],
    ["a non-string model id", '{"bedrockLlmModelId":42}'],
    [
      "whitespace in a model id",
      '{"bedrockLlmModelId":"us.anthropic. claude-sonnet-5"}',
    ],
  ])("rejects %s", (_case, raw) => {
    expect(() => parseDeploymentConfig(raw, PARAM)).toThrow(PARAM);
  });

  it("preserves unrelated deployment settings", () => {
    expect(
      parseDeploymentConfig(
        '{"bedrockLlmModelId":"","customDomain":"example.com"}',
        PARAM,
      ),
    ).toEqual({ customDomain: "example.com" });
  });
});

describe("assertModelIdsInvocableFromRegion", () => {
  const PARAM = "/coa/config";

  it("is silent for the default config in us-east-1 (today's deploys are unaffected)", () => {
    expect(() =>
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({}),
        "us-east-1",
        PARAM,
      ),
    ).not.toThrow();
  });

  it("fails synth for the default config in ap-northeast-1 and names every key", () => {
    let message = "";
    try {
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({}),
        "ap-northeast-1",
        PARAM,
      );
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain("deploy region ap-northeast-1");
    for (const key of MODEL_ID_CONFIG_KEYS) {
      expect(message).toContain(`- ${key}:`);
    }
    // Where the bad value came from, and the fix.
    expect(message).toContain(
      `the built-in default; ${PARAM} does not set bedrockEmbedModelId`,
    );
    expect(message).toContain(`Set each key in the SSM parameter ${PARAM}`);
    expect(message).toContain("a 'jp.' geographic profile");
    expect(message).toContain("a 'apac.' geographic profile");
    expect(message).toContain("a 'global.' profile");
    expect(message).toContain("the bare in-region model id");
  });

  it("passes the verified Tokyo configuration from #1020", () => {
    expect(() =>
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({
          bedrockLlmModelId: "global.anthropic.claude-sonnet-5",
          bedrockEmbedModelId: "global.cohere.embed-v4:0",
          bedrockInductionLlmModelId: "global.anthropic.claude-sonnet-4-6",
          bedrockChatModelId: "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        }),
        "ap-northeast-1",
        PARAM,
      ),
    ).not.toThrow();
  });

  it("reports only the offending keys when the config is partially right", () => {
    let message = "";
    try {
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({
          bedrockLlmModelId: "global.anthropic.claude-sonnet-5",
          bedrockEmbedModelId: "cohere.embed-v4:0",
          // induction and chat left at their us. defaults
        }),
        "ap-northeast-1",
        PARAM,
      );
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain("- bedrockInductionLlmModelId:");
    expect(message).toContain("- bedrockChatModelId:");
    expect(message).not.toContain("- bedrockLlmModelId:");
    expect(message).not.toContain("- bedrockEmbedModelId:");
  });

  it("attributes a wrong value that came from the config to the config", () => {
    let message = "";
    try {
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({
          bedrockChatModelId: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        }),
        "eu-west-1",
        "/acme/config",
      );
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain("- bedrockChatModelId:");
    expect(message).toContain("(set in /acme/config)");
    expect(message).toContain("a 'eu.' geographic profile");
  });

  it("does not offer a 'global.' profile where none is published", () => {
    let message = "";
    try {
      assertModelIdsInvocableFromRegion(
        resolveConfiguredModelIds({}),
        "us-gov-west-1",
        PARAM,
      );
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain("a 'us-gov.' geographic profile");
    expect(message).not.toContain("'global.' profile");
  });
});
