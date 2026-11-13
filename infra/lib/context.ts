// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { createHash } from "node:crypto";

import * as cdk from "aws-cdk-lib";
import { Construct, Node } from "constructs";
import {
  CTX_RESOURCE_PREFIX,
  CTX_ENV,
  CTX_PROJECT_TAG,
  CTX_GRAPH_BASE_URI,
  CTX_EVENT_SOURCE_PREFIX,
  CTX_LAMBDA_RESERVED_CONCURRENCY,
  CTX_GUARDRAIL_TIER,
  CTX_GUARDRAIL_PROFILE,
  CTX_PREFER_IPV4,
  DEFAULT_RESOURCE_PREFIX,
  DEFAULT_ENV,
  DEFAULT_PROJECT_TAG,
  DEFAULT_GRAPH_BASE_URI,
  DEFAULT_URN_PREFIX,
  DEFAULT_VOCAB_PREFIX,
  DEFAULT_DZ_TYPE_PREFIX,
  DEFAULT_LAMBDA_RESERVED_CONCURRENCY,
} from "./constants";

/** Resolved context values available to any stack or construct. */
export interface ResolvedContext {
  readonly prefix: string;
  readonly envName: string;
  readonly projectTag: string;
  readonly ssmPrefix: string;
  /** Base authority for knowledge-graph IRIs (e.g. "http://coa.amazon.com"). */
  readonly graphBaseUri: string;
  /** URN scheme prefix (e.g. "coa" → urn:coa:...). */
  readonly urnPrefix: string;
  /** RDF vocab short prefix (e.g. "coa" → coa:isMapped). */
  readonly vocabPrefix: string;
  /**
   * EventBridge source prefix. Tracks `resource_prefix` so co-located
   * deployments don't cross-trigger on the shared account-global bus
   * (e.g. "scl" → scl.ontology). Override with `event_source_prefix`.
   */
  readonly eventSourcePrefix: string;
  /** DataZone type name prefix in PascalCase (e.g. "Coa" → CoaTableMetadata). */
  readonly dzTypePrefix: string;
}

/** Read resource_prefix and env from CDK context with defaults. */
export function resolveContext(node: Node): ResolvedContext {
  const prefix =
    (node.tryGetContext(CTX_RESOURCE_PREFIX) as string) ??
    DEFAULT_RESOURCE_PREFIX;
  const envName = (node.tryGetContext(CTX_ENV) as string) ?? DEFAULT_ENV;
  const projectTag =
    (node.tryGetContext(CTX_PROJECT_TAG) as string) ?? DEFAULT_PROJECT_TAG;
  const graphBaseUri =
    (node.tryGetContext(CTX_GRAPH_BASE_URI) as string) ??
    DEFAULT_GRAPH_BASE_URI;
  // The default bus is account-global, so the event source tracks `prefix`
  // to keep two deployments' rules from matching each other's events. Override
  // with `event_source_prefix` for deliberate cross-deployment interop.
  const eventSourcePrefix =
    (node.tryGetContext(CTX_EVENT_SOURCE_PREFIX) as string) ?? prefix;
  // Written into Neptune RDF and DataZone metadata, so these must not change
  // with `prefix`; see BRAND in libs/ts-shared/src/constants.ts.
  const urnPrefix = DEFAULT_URN_PREFIX;
  const vocabPrefix = DEFAULT_VOCAB_PREFIX;
  const dzTypePrefix = DEFAULT_DZ_TYPE_PREFIX;
  return {
    prefix,
    envName,
    projectTag,
    ssmPrefix: `/${prefix}`,
    graphBaseUri,
    urnPrefix,
    vocabPrefix,
    eventSourcePrefix,
    dzTypePrefix,
  };
}

/** Return `{prefix}-{env}-{name}`. */
export function prefixed(node: Node, name: string): string {
  const { prefix, envName } = resolveContext(node);
  return `${prefix}-${envName}-${name}`;
}

/** Longest name OpenSearch Serverless accepts for a collection or policy. */
export const AOSS_NAME_MAX_LENGTH = 32;

/**
 * `prefixed(name)` for an OpenSearch Serverless collection or policy, which
 * must be at most 32 characters. A name that fits is returned unchanged, so
 * existing deployments keep their names and nothing is replaced. A longer one
 * (a long `resource_prefix` or env name) keeps its leading characters and ends
 * in a short hash of the full name, so it stays unique and stable per
 * deployment.
 */
export function aossName(node: Node, name: string): string {
  const full = prefixed(node, name);
  if (full.length <= AOSS_NAME_MAX_LENGTH) {
    return full;
  }
  const hash = createHash("sha256").update(full).digest("hex").slice(0, 6);
  const head = full
    .slice(0, AOSS_NAME_MAX_LENGTH - hash.length - 1)
    .replace(/-+$/, "");
  return `${head}-${hash}`;
}

/**
 * ARN of the platform Lambda named `prefixed(name)` in the caller's account and
 * region. Lets a stack address another stack's function by its fixed name,
 * without the cross-stack reference (and dependency) that `functionArn` brings.
 */
export function prefixedLambdaArn(scope: Construct, name: string): string {
  return cdk.Stack.of(scope).formatArn({
    service: "lambda",
    resource: "function",
    resourceName: prefixed(scope.node, name),
    arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
  });
}

/**
 * Resource-tag key that binds a resource the platform reads, but does not own,
 * to the namespaces entitled to it — today a JDBC source's credential secret,
 * tagged `{prefix}:namespace = "<namespaceId> [<namespaceId> ...]"`.
 *
 * Carries `prefix` (not the static brand) for the same reason `eventSourcePrefix`
 * does: the resource being tagged is account-global and therefore shared, so two
 * deployments co-located in one account must bind independently. A secret
 * onboarded to `scl` is not readable by a `coa` deployment, whose IAM conditions
 * name their own key.
 *
 * Single source of truth for the synth side. The runtime derives the same key
 * from `RESOURCE_TAG_PREFIX` (see `namespace_tag_key()` in
 * libs/common/src/coa_common/constants.py) — the two must agree exactly or registration writes
 * a tag the IAM conditions cannot match.
 */
export function namespaceTagKey(node: Node): string {
  return `${resolveContext(node).prefix}:namespace`;
}

/**
 * Resolve the `lambda_reserved_concurrency` context value shared by the VKG
 * reload and doc-preprocessing Lambdas.
 *
 * Returns the reserved-concurrency count to apply, or `undefined` meaning "do
 * not reserve" (pass straight to `reservedConcurrentExecutions` — CDK omits the
 * property when it is `undefined`). A value of `0` maps to `undefined` so that
 * on reduced-quota accounts the reservation can be disabled without CDK
 * rejecting a literal `0` reservation.
 *
 * Throws at synth unless the value is a non-negative integer.
 */
export function resolveLambdaReservedConcurrency(
  node: Node,
): number | undefined {
  const raw: unknown = node.tryGetContext(CTX_LAMBDA_RESERVED_CONCURRENCY);
  const value = Number(raw ?? DEFAULT_LAMBDA_RESERVED_CONCURRENCY);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(
      `${CTX_LAMBDA_RESERVED_CONCURRENCY} must be a non-negative integer, got: ${String(raw)}`,
    );
  }
  return value === 0 ? undefined : value;
}

/** Resolved Bedrock Guardrails safeguard tier (see {@link resolveGuardrailTier}). */
export interface GuardrailTier {
  /** CloudFormation tier name for the content and topic policies. */
  readonly tierName: "CLASSIC" | "STANDARD";
  /**
   * System-defined guardrail profile id. Always set for the Standard tier; on
   * Classic it only grants the callers' IAM (see {@link resolveGuardrailTier}).
   */
  readonly profileId?: string;
}

const GUARDRAIL_PROFILE_ID = /^[a-z]+(?:-[a-z]+)*\.guardrail\.v[0-9]+:[0-9]+$/;

/**
 * Resolve the `guardrail_tier` / `guardrail_profile` context pair.
 *
 * The default is the Classic tier, which is what an unset tier has always
 * produced. Classic content filters support English, French and Spanish only:
 * a Japanese prompt injection is not detected. The Standard tier
 * supports more languages but requires cross-Region inference through a
 * system-defined guardrail profile, so the screened text can be processed in
 * any destination Region of that profile (for example `apac.guardrail.v1:0`
 * from ap-northeast-1 also routes to ap-northeast-2/3, ap-south-1 and
 * ap-southeast-1/2). That is a data residency decision per deployment, so the
 * Standard tier is opt-in and needs the profile named explicitly.
 *
 * A profile with the Classic tier is allowed on purpose: it grants the callers'
 * IAM on the profile without changing the guardrails. The guardrail stack
 * deploys before the stacks whose roles call it, so switching an existing
 * deployment straight to Standard leaves those roles without the profile
 * permission until their stacks update, and every ApplyGuardrail in between
 * fails with AccessDeniedException. Deploying with the profile first and the
 * tier second avoids that window.
 *
 * Throws at synth time on an unknown tier, a Standard tier without a profile,
 * or a malformed profile id.
 */
export function resolveGuardrailTier(node: Node): GuardrailTier {
  const rawTier: unknown = node.tryGetContext(CTX_GUARDRAIL_TIER);
  const rawProfile: unknown = node.tryGetContext(CTX_GUARDRAIL_PROFILE);
  const tier = String(rawTier ?? "classic")
    .trim()
    .toLowerCase();
  const profile =
    rawProfile === undefined || rawProfile === null
      ? ""
      : String(rawProfile).trim();
  if (tier !== "classic" && tier !== "standard") {
    throw new Error(
      `${CTX_GUARDRAIL_TIER} must be "classic" or "standard", got: ${String(rawTier)}`,
    );
  }
  if (profile && !GUARDRAIL_PROFILE_ID.test(profile)) {
    throw new Error(
      `${CTX_GUARDRAIL_PROFILE} must be a system-defined guardrail profile id such as "apac.guardrail.v1:0", got: ${String(rawProfile)}`,
    );
  }
  if (tier === "standard" && !profile) {
    throw new Error(
      `${CTX_GUARDRAIL_TIER}=standard needs ${CTX_GUARDRAIL_PROFILE} set to a system-defined guardrail profile id such as "apac.guardrail.v1:0"`,
    );
  }
  return {
    tierName: tier === "standard" ? "STANDARD" : "CLASSIC",
    ...(profile ? { profileId: profile } : {}),
  };
}

/**
 * Extra IAM resources a role that calls a guardrail needs.
 *
 * Empty when no profile is set. With a profile, ApplyGuardrail (and Converse
 * with a guardrail) must also be allowed on the guardrail profile in every
 * destination Region, so the ARN uses a Region wildcard rather than listing the
 * profile's current destinations, which AWS can extend.
 */
export function guardrailProfileResources(scope: Construct): string[] {
  const { profileId } = resolveGuardrailTier(scope.node);
  if (!profileId) return [];
  const account = cdk.Stack.of(scope).account;
  return [`arn:aws:bedrock:*:${account}:guardrail-profile/${profileId}`];
}

/**
 * Resolve the `prefer_ipv4` context value into the Docker build args for the
 * Python images that run in the VPC subnets (`PREFER_IPV4` in each Dockerfile).
 *
 * On by default: harmless where IPv4 works, and in a dual-stack subnet without
 * IPv6 egress to AWS it stops IPv6-first connects from stalling long enough to
 * expire a request's SigV4 signature. Accepts a boolean (cdk.json) or the
 * string "true"/"false" (the CLI `--context` bridge) and throws at synth time
 * on anything else.
 *
 * Call it on every path, so an invalid value fails synth even when the stack
 * uses a pre-built image. Pass `prebuiltImageContextKey` (the `*_image_uri`
 * key) when it does: that image was built without these args and keeps the
 * Dockerfile default, so `prefer_ipv4=false` cannot reach it, and synth warns
 * instead of ignoring the value silently.
 */
export function resolvePreferIpv4BuildArgs(
  scope: Construct,
  prebuiltImageContextKey?: string,
): Record<string, string> {
  const raw: unknown = scope.node.tryGetContext(CTX_PREFER_IPV4);
  const value = String(raw ?? true);
  if (value !== "true" && value !== "false") {
    throw new Error(
      `${CTX_PREFER_IPV4} must be true or false, got: ${String(raw)}`,
    );
  }
  if (prebuiltImageContextKey && value === "false") {
    cdk.Annotations.of(scope).addWarningV2(
      `coa:preferIpv4Ignored.${prebuiltImageContextKey}`,
      `${CTX_PREFER_IPV4}=false has no effect on the pre-built image from ` +
        `${prebuiltImageContextKey}: it keeps the Dockerfile default and ` +
        "prefers IPv4. Build that image with --build-arg PREFER_IPV4=false " +
        "to keep the IPv6-first order.",
    );
  }
  return { PREFER_IPV4: value };
}
