// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Synth-time check that every Bedrock model id a deployment will use can be
 * invoked from the region it is being deployed to (#1020).
 *
 * The default model ids are `us.` cross-region inference profiles. Bedrock only
 * publishes a geography's profiles inside that geography, so a deploy to
 * ap-northeast-1 with no `/{prefix}/config` reaches CREATE_COMPLETE and then
 * fails at the first Bedrock call with `ValidationException: The provided
 * model identifier is invalid` — embedding, induction, serve and enrichment all
 * at once. IAM does not catch it either: the grants are wildcard
 * inference-profile ARNs. Nothing before this module looked at the ids against
 * the region.
 *
 * What is judged here is deliberately narrow: a geographic profile prefix that
 * contradicts the deploy region's geography. That is deterministic and
 * knowable at synth (`us.` is never invocable from `ap-*`). Whether a specific
 * model is *available* in the region is not judged — availability is
 * account-scoped and changes over time — and stays a preflight warning
 * (`scripts/preflight-deploy.sh` §9). Unknown prefixes and bare in-region ids
 * pass, for the same reason `isInferenceProfileId` counts segments instead of
 * allow-listing geos: a newly launched geography must not need a code change
 * to deploy.
 */

import type { SsmConfig } from "../types";
import {
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
  DEFAULT_BEDROCK_LLM_MODEL_ID,
  DEFAULT_BEDROCK_MODEL_ID,
} from "../constants";
import { isInferenceProfileId } from "./bedrock-utils";

/** The SSM config keys that name a model, and the default each stack applies. */
export const MODEL_ID_CONFIG_KEYS = [
  "bedrockLlmModelId",
  "bedrockEmbedModelId",
  "bedrockInductionLlmModelId",
  "bedrockChatModelId",
] as const;
export type ModelIdConfigKey = (typeof MODEL_ID_CONFIG_KEYS)[number];

/**
 * Mirrors the `props.x ?? DEFAULT_*` fallback each stack applies, so the check
 * sees what the stacks will actually deploy. Kept next to the check rather
 * than read from the stacks because app.ts hands the raw config value to each
 * stack; the stacks' own tests pin that they fall back to these constants.
 */
export const MODEL_ID_DEFAULTS: Readonly<Record<ModelIdConfigKey, string>> = {
  bedrockLlmModelId: DEFAULT_BEDROCK_LLM_MODEL_ID,
  bedrockEmbedModelId: DEFAULT_BEDROCK_MODEL_ID,
  bedrockInductionLlmModelId: DEFAULT_BEDROCK_INDUCTION_MODEL_ID,
  bedrockChatModelId: DEFAULT_BEDROCK_CHAT_MODEL_ID,
};

export interface ResolvedModelId {
  readonly key: ModelIdConfigKey;
  readonly modelId: string;
  /** Whether the value came from the SSM config or from the built-in default. */
  readonly origin: "config" | "default";
}

/**
 * Normalize the four model-id fields before they reach either validation or
 * the stacks. Empty values are treated as unset, matching the runtime fallback
 * used by Serve; non-empty values containing whitespace are rejected
 * rather than deployed as an identifier Bedrock cannot resolve.
 */
export function normalizeModelIdConfig(config: SsmConfig): SsmConfig {
  const normalized = { ...config };
  const values = normalized as Record<string, unknown>;
  for (const key of MODEL_ID_CONFIG_KEYS) {
    const value = values[key];
    if (value == null || value === "") {
      delete values[key];
      continue;
    }
    if (typeof value !== "string") {
      throw new Error(`${key} must be a string`);
    }
    if (value.trim() === "") {
      delete values[key];
      continue;
    }
    if (/\s/.test(value)) {
      throw new Error(`${key} must not contain whitespace`);
    }
  }
  return normalized;
}

/** Parse the SSM deployment config and validate the model-id field shapes. */
export function parseDeploymentConfig(
  raw: string,
  configParameterName: string,
): SsmConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`SSM ${configParameterName} is not valid JSON (${msg})`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`SSM ${configParameterName} must contain a JSON object`);
  }
  try {
    return normalizeModelIdConfig(parsed as SsmConfig);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(
      `SSM ${configParameterName} contains invalid model configuration (${msg})`,
    );
  }
}

/** The effective model id for every key, with where it came from. */
export function resolveConfiguredModelIds(
  config: Pick<SsmConfig, ModelIdConfigKey>,
): ResolvedModelId[] {
  const normalized = normalizeModelIdConfig(config);
  return MODEL_ID_CONFIG_KEYS.map((key) => {
    const configured = normalized[key];
    // Empty values were normalized away before both this check and stack
    // construction, so the effective value here is the value deployed.
    return configured != null
      ? { key, modelId: configured, origin: "config" }
      : { key, modelId: MODEL_ID_DEFAULTS[key], origin: "default" };
  });
}

/**
 * Geographic inference-profile prefixes Bedrock publishes today. `global` is
 * invocable from commercial regions; the isolated partitions (`us-gov-*`,
 * `cn-*`) publish no `global` profiles.
 */
const KNOWN_GEO_PREFIXES: ReadonlySet<string> = new Set([
  "us",
  "eu",
  "apac",
  "jp",
  "au",
  "ca",
  "us-gov",
  "global",
]);

/**
 * The leading geography segment of an inference-profile id, or undefined for
 * a bare model id (`cohere.embed-v4:0`), an ARN, or a prefix this module does
 * not know (a future geography — left unjudged on purpose).
 */
export function geoPrefixOf(modelId: string): string | undefined {
  if (!isInferenceProfileId(modelId)) return undefined;
  const prefix = modelId.split(".")[0].toLowerCase();
  return KNOWN_GEO_PREFIXES.has(prefix) ? prefix : undefined;
}

/**
 * The geography names (as inference-profile prefixes) that profiles published
 * in `region` can carry. Japan and Australia regions carry both their national
 * prefix and `apac`; Bedrock also places UAE (`me-central-1`) in APAC for
 * cross-region inference. Other commercial regions retain `global` only unless
 * they are assigned to a published geography here.
 */
export function regionGeographies(region: string): ReadonlySet<string> {
  const r = region.toLowerCase();
  if (r.startsWith("us-gov-")) return new Set(["us-gov"]);
  if (r.startsWith("cn-")) return new Set();
  if (r.startsWith("us-")) return new Set(["us", "global"]);
  if (r.startsWith("eu-")) return new Set(["eu", "global"]);
  if (r.startsWith("ca-")) return new Set(["ca", "global"]);
  if (r === "ap-northeast-1" || r === "ap-northeast-3") {
    return new Set(["jp", "apac", "global"]);
  }
  if (r === "ap-southeast-2" || r === "ap-southeast-4") {
    return new Set(["au", "apac", "global"]);
  }
  if (r === "me-central-1") return new Set(["apac", "global"]);
  if (r.startsWith("ap-")) return new Set(["apac", "global"]);
  // sa-, other me-, af-, il-, mx-, … : no geographic profiles of their own, but
  // `global` profiles are published there.
  return new Set(["global"]);
}

/**
 * Why `modelId` cannot be invoked from `region`, or undefined when nothing
 * contradicts. Only geography is judged (see the module comment).
 */
export function modelIdRegionConflict(
  modelId: string,
  region: string,
): string | undefined {
  const geo = geoPrefixOf(modelId);
  if (geo === undefined) return undefined;
  if (regionGeographies(region).has(geo)) return undefined;
  return `'${modelId}' is a '${geo}.' inference profile, which Bedrock does not publish in ${region}`;
}

/**
 * Throw at synth when any effective model id contradicts the deploy region.
 * All conflicts are reported at once, each naming the config key, where the
 * value came from, and how to fix it — the fix is always the same SSM edit, so
 * the message says so instead of pointing at the docs alone.
 */
export function assertModelIdsInvocableFromRegion(
  resolved: readonly ResolvedModelId[],
  region: string,
  configParameterName: string,
): void {
  const conflicts = resolved.flatMap((r) => {
    const reason = modelIdRegionConflict(r.modelId, region);
    if (!reason) return [];
    const source =
      r.origin === "config"
        ? `set in ${configParameterName}`
        : `the built-in default; ${configParameterName} does not set ${r.key}`;
    return [`  - ${r.key}: ${reason} (${source})`];
  });
  if (conflicts.length === 0) return;
  const geos = regionGeographies(region);
  const options = [
    ...[...geos]
      .filter((g) => g !== "global")
      .map((g) => `a '${g}.' geographic profile`),
    ...(geos.has("global") ? ["a 'global.' profile"] : []),
    "the bare in-region model id",
  ];
  throw new Error(
    [
      `Bedrock model ids cannot be invoked from the deploy region ${region}:`,
      ...conflicts,
      `Set each key in the SSM parameter ${configParameterName} to a model published in ${region}` +
        ` (${options.join(", ")}), then re-run.` +
        ` See "Where the model IDs live" in external-docs/content/deploying.md.`,
    ].join("\n"),
  );
}
