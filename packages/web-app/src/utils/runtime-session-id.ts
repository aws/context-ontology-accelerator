// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AgentCore Runtime requires `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` to
 * be at least 33 characters (enforced server-side — a short value 400s with
 * "Member must have length greater than or equal to 33").
 *
 * The OIDC `sub` claim is NOT guaranteed to satisfy this: Cognito issues UUIDs
 * (36 chars, always safe), but other IdPs — e.g. enterprise SSO — issue short
 * human-readable usernames (e.g. "jdoe1234", 8 chars), which fail the AgentCore
 * constraint outright.
 */
const MIN_RUNTIME_SESSION_ID_LENGTH = 33;

/**
 * Derive a stable, ≥33-char AgentCore runtime session id from an OIDC `sub`.
 * Deterministic per `sub` (same user always maps to the same session id, so
 * AgentCore's sticky routing still groups a user's requests together).
 *
 * The `NNN-` length prefix is what makes the encoding injective, and it is not
 * cosmetic: padding alone collides. `"noah"` and `"noah0"` both pad to
 * `"noah0000…"`, which would put two distinct users on ONE AgentCore session.
 * With the prefix, `sub` is recoverable from the output (declared length, then
 * that many characters), so distinct subs cannot share a session id.
 *
 * Applied to every `sub`, not just short ones: a long `sub` that happened to
 * equal some short `sub`'s padded form would collide the same way.
 *
 * Returns undefined if `sub` is falsy (caller omits the header entirely).
 */
export function deriveRuntimeSessionId(
  sub: string | undefined,
): string | undefined {
  if (!sub) return undefined;
  const prefixed = `${sub.length.toString().padStart(3, "0")}-${sub}`;
  return prefixed.padEnd(MIN_RUNTIME_SESSION_ID_LENGTH, "0");
}
