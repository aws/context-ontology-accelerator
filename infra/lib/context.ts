// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Construct, Node } from "constructs";
import {
  CTX_RESOURCE_PREFIX,
  CTX_ENV,
  CTX_PROJECT_TAG,
  CTX_GRAPH_BASE_URI,
  CTX_EVENT_SOURCE_PREFIX,
  CTX_LAMBDA_RESERVED_CONCURRENCY,
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
  // EventBridge source DOES track `prefix` (unlike the other brand tokens
  // below). An event source is a ROUTING attribute on a transient message, not
  // a persisted identifier: nothing is written into a data store keyed by it, so
  // deriving it per-deployment orphans nothing. It has to be deployment-scoped
  // because the bus it lands on (`default`) is ACCOUNT-GLOBAL and shared — two
  // deployments in one account+region emitting the same source means each one's
  // rules match the OTHER's events. That is not hypothetical: it provisioned
  // duplicate per-namespace VKG services that only the originating deployment's
  // teardown could ever delete, leaking ECS services and failing
  // test_namespace_delete_deregisters_vkg. The explicit `event_source_prefix`
  // context override is retained for the deliberate cross-deployment-interop
  // case (one deployment consuming another's events).
  const eventSourcePrefix =
    (node.tryGetContext(CTX_EVENT_SOURCE_PREFIX) as string) ?? prefix;
  // The remaining data-plane identifiers are intentionally NOT derived from
  // `prefix`. They are written into Neptune RDF and DataZone metadata, so they
  // must stay stable across deployments and prefixes — a prefix change would
  // re-mint every IRI and orphan the graph data already stored. See the BRAND
  // block in libs/ts-shared/src/constants.ts. They are safe to keep static
  // because each lives inside a container that is ALREADY prefix-isolated (its
  // own Neptune cluster, its own DataZone domain), unlike the shared event bus.
  // `prefix` governs physical resource names via prefixed()/ssmPrefix.
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
 * libs/common/constants.py) — the two must agree exactly or registration writes
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
 * Validates that the value is a non-negative integer and throws at synth time
 * otherwise, matching the fail-fast behavior of the other numeric context
 * knobs (e.g. `dbScanEnrichmentTimeoutMinutes`).
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
