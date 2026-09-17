// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import {
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_BEDROCK_MODEL_ID,
} from "../lib/constants";
import { geoPrefixOf, regionGeographies } from "../lib/utils/model-region";

/**
 * The default model ids live in four places that cannot import each other:
 * libs/ts-shared (what the stacks deploy), the Python runtimes (what a
 * container falls back to when its env var is missing), and
 * scripts/preflight-deploy.sh (what the operator is warned about before any
 * of that runs). #1020 was a case of all of them agreeing on a value that is
 * wrong outside the US; the next drift would be them disagreeing with each
 * other. This test pins them together, source text against exported value.
 */

const REPO = path.join(__dirname, "..", "..");
const read = (...p: string[]) => fs.readFileSync(path.join(REPO, ...p), "utf8");

function match(source: string, re: RegExp, what: string): string {
  const m = source.match(re);
  if (!m) throw new Error(`could not find ${what} (${re})`);
  return m[1];
}

describe("model-id defaults agree across TypeScript, Python and preflight", () => {
  const preflight = read("scripts", "preflight-deploy.sh");
  const preflightDefault = (name: string) =>
    match(
      preflight,
      new RegExp(`^_DEFAULT_${name}_MODEL_ID="([^"]+)"`, "m"),
      `_DEFAULT_${name}_MODEL_ID in preflight-deploy.sh`,
    );

  it("serve query LLM: ts-shared = coa_serve config.py = coa_serve clients/bedrock.py = preflight", () => {
    const configPy = read(
      "packages",
      "context-manager",
      "src",
      "coa_serve",
      "config.py",
    );
    const clientPy = read(
      "packages",
      "context-manager",
      "src",
      "coa_serve",
      "clients",
      "bedrock.py",
    );
    const literal = /os\.environ\.get\("BEDROCK_MODEL_ID",\s*"([^"]+)"\)/;
    expect(
      match(configPy, literal, "BEDROCK_MODEL_ID default in config.py"),
    ).toBe(DEFAULT_BEDROCK_LLM_MODEL_ID);
    expect(
      match(
        clientPy,
        literal,
        "BEDROCK_MODEL_ID default in clients/bedrock.py",
      ),
    ).toBe(DEFAULT_BEDROCK_LLM_MODEL_ID);
    expect(preflightDefault("LLM")).toBe(DEFAULT_BEDROCK_LLM_MODEL_ID);
  });

  it("embedding model: ts-shared = coa_common.constants.DEFAULT_EMBED_MODEL_ID = preflight", () => {
    const constantsPy = read(
      "libs",
      "common",
      "src",
      "coa_common",
      "constants.py",
    );
    expect(
      match(
        constantsPy,
        /^DEFAULT_EMBED_MODEL_ID:\s*str\s*=\s*"([^"]+)"/m,
        "DEFAULT_EMBED_MODEL_ID",
      ),
    ).toBe(DEFAULT_BEDROCK_MODEL_ID);
    expect(preflightDefault("EMBED")).toBe(DEFAULT_BEDROCK_MODEL_ID);
  });

  it("chat model: ts-shared = coa_common.bedrock.FALLBACK_CHAT_MODEL_ID = preflight", () => {
    const bedrockPy = read("libs", "common", "src", "coa_common", "bedrock.py");
    expect(
      match(
        bedrockPy,
        /^FALLBACK_CHAT_MODEL_ID\s*=\s*"([^"]+)"/m,
        "FALLBACK_CHAT_MODEL_ID",
      ),
    ).toBe(DEFAULT_BEDROCK_CHAT_MODEL_ID);
    expect(preflightDefault("CHAT")).toBe(DEFAULT_BEDROCK_CHAT_MODEL_ID);
  });

  it("induction model: ts-shared = preflight", () => {
    // The ontology-engine container always receives LLM_MODEL_ID from the
    // stack, so there is no Python fallback to pin here (its in-process
    // fallback is a different, older id — tracked separately).
    expect(preflightDefault("INDUCTION")).toBe(
      DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
    );
  });
});

/**
 * preflight-deploy.sh re-implements the region → geographies and id → geo
 * prefix mapping in bash because it runs before anything is built. Run the
 * bash functions and compare with the TypeScript they mirror.
 */
describe("preflight's bash geography mapping mirrors model-region.ts", () => {
  const script = path.join(REPO, "scripts", "preflight-deploy.sh");
  const callBash = (fn: string, arg: string): string =>
    execFileSync(
      "bash",
      [
        "-c",
        // eval rather than `source <(...)`: macOS ships bash 3.2, where
        // sourcing a process substitution does not define the function.
        `eval "$(sed -n '/^_DEFAULT_.*_MODEL_ID=/p' "$0")"; ` +
          `eval "$(sed -n "/^${fn}()/,/^}/p" "$0")"; ${fn} "$1"`,
        script,
        arg,
      ],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    ).trim();

  it.each([
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "ca-central-1",
    "ap-northeast-1",
    "ap-northeast-3",
    "ap-southeast-2",
    "ap-southeast-4",
    "ap-southeast-1",
    "ap-south-1",
    "sa-east-1",
    "me-central-1",
    "us-gov-west-1",
    "cn-north-1",
  ])("_coa_region_geographies %s", (region) => {
    const bash = callBash("_coa_region_geographies", region)
      .split(/\s+/)
      .filter(Boolean)
      .sort();
    expect(bash).toEqual([...regionGeographies(region)].sort());
  });

  it.each([
    "us.anthropic.claude-sonnet-5",
    "eu.anthropic.claude-sonnet-4-6",
    "apac.anthropic.claude-sonnet-5",
    "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "au.anthropic.claude-sonnet-5",
    "ca.amazon.nova-lite-v1:0",
    "global.cohere.embed-v4:0",
    "us-gov.anthropic.claude-sonnet-4-6",
    "cohere.embed-v4:0",
    "anthropic.claude-sonnet-5",
    "sa.anthropic.claude-sonnet-5",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc",
  ])("_coa_model_geo_prefix %s", (id) => {
    expect(callBash("_coa_model_geo_prefix", id) || undefined).toBe(
      geoPrefixOf(id),
    );
  });

  it("resolves empty model-id values to the same defaults as TypeScript", () => {
    const rows = callBash(
      "_coa_resolve_model_ids",
      '{"bedrockLlmModelId":"","bedrockEmbedModelId":" "}',
    )
      .split("\n")
      .map((line) => line.split("\t"));
    expect(rows).toContainEqual([
      "bedrockLlmModelId",
      DEFAULT_BEDROCK_LLM_MODEL_ID,
      "default",
    ]);
    expect(rows).toContainEqual([
      "bedrockEmbedModelId",
      DEFAULT_BEDROCK_MODEL_ID,
      "default",
    ]);
  });

  it.each([
    ["malformed JSON", "{"],
    ["a non-object root", "[]"],
    ["a non-string model id", '{"bedrockLlmModelId":42}'],
    [
      "whitespace in a model id",
      '{"bedrockLlmModelId":"us.anthropic. claude-sonnet-5"}',
    ],
  ])("fails closed for %s", (_case, config) => {
    expect(() => callBash("_coa_resolve_model_ids", config)).toThrow();
  });
});
