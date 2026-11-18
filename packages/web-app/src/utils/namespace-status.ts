// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  ControlPlaneServiceServiceException,
  NamespaceStatus,
  VkgHealthStatus,
} from "@coa/control-plane-client";
import type { AppMessage } from "../i18n/appMessages";

/**
 * Catalogue message for each namespace lifecycle status. Shared by the
 * namespace list (status cell and status filter) and the namespace detail
 * summary, so the cell and the filter always use the same word.
 */
export const NAMESPACE_STATUS_MESSAGES: Readonly<
  Record<NamespaceStatus, AppMessage>
> = {
  [NamespaceStatus.ACTIVE]: "Active",
  [NamespaceStatus.ARCHIVED]: "Archived",
  [NamespaceStatus.DELETING]: "Deleting",
  [NamespaceStatus.DELETED]: "Deleted",
  [NamespaceStatus.DELETE_FAILED]: "Delete failed",
};

/** Catalogue message for each VKG health status (the badge text). */
export const VKG_HEALTH_MESSAGES: Readonly<
  Record<VkgHealthStatus, AppMessage>
> = {
  [VkgHealthStatus.HEALTHY]: "Healthy",
  [VkgHealthStatus.DEGRADED]: "Degraded",
  [VkgHealthStatus.UNAVAILABLE]: "Unavailable",
  [VkgHealthStatus.PROVISIONING]: "Provisioning",
  [VkgHealthStatus.UNKNOWN]: "Unknown",
};

function lookup(
  table: Readonly<Record<string, AppMessage>>,
  value: string,
): AppMessage | undefined {
  return Object.prototype.hasOwnProperty.call(table, value)
    ? table[value]
    : undefined;
}

/**
 * Namespace status in the reader's locale. A status this console does not
 * know yet (a newer API) is shown raw rather than hidden; an absent status
 * stays "—".
 */
export function localizeNamespaceStatus(
  status: string | undefined,
  t: (message: AppMessage) => string,
): string {
  if (!status) return "—";
  const message = lookup(NAMESPACE_STATUS_MESSAGES, status);
  return message ? t(message) : status;
}

/**
 * Why there is no current namespace although one was asked for (see
 * NamespaceProvider): the server refused the id ("not-found": deleted, never
 * existed, or not accessible; the switcher shows all three with one label),
 * or the lookup failed for another reason ("error").
 */
export type NamespaceUnavailable = "not-found" | "error";

function lookupStatus(error: unknown): number | undefined {
  return error instanceof ControlPlaneServiceServiceException
    ? error.$metadata.httpStatusCode
    : undefined;
}

/**
 * GetNamespace statuses shown as "not-found": 404 (no record, e.g. deleted),
 * 400 (not a namespace id at all) and 403 (the authorizer denied the call).
 * The authorizer answers the same 403 when the caller has no role on the
 * namespace and when the token is expired or invalid, so a 403 counts as
 * "not-found" only once its retries are used up. Anything else (5xx, 401, a
 * network failure) says nothing about the namespace and is "error".
 */
const NOT_FOUND_STATUSES: ReadonlySet<number> = new Set([400, 403, 404]);

/**
 * GetNamespace statuses that are a final answer about the id, so asking again
 * gets the same answer: 400 and 404. A 403 is not final because a retry can
 * carry a refreshed token.
 */
const FINAL_STATUSES: ReadonlySet<number> = new Set([400, 404]);

/** Classify a failed GetNamespace call. */
export function namespaceLookupFailure(error: unknown): NamespaceUnavailable {
  return NOT_FOUND_STATUSES.has(lookupStatus(error) ?? 0)
    ? "not-found"
    : "error";
}

/** Whether a failed GetNamespace call is final and not worth retrying. */
export function isFinalLookupFailure(error: unknown): boolean {
  return FINAL_STATUSES.has(lookupStatus(error) ?? 0);
}

const NAMESPACE_UNAVAILABLE_MESSAGES: Readonly<
  Record<NamespaceUnavailable, AppMessage>
> = {
  "not-found": "Not found or not accessible",
  error: "Could not be loaded",
};

/** Namespace switcher text when there is no current namespace. */
function localizeNamespaceUnavailable(
  reason: NamespaceUnavailable | undefined,
  t: (message: AppMessage) => string,
): string {
  return reason ? t(NAMESPACE_UNAVAILABLE_MESSAGES[reason]) : "—";
}

/**
 * The name the top-bar namespace switcher shows: "Loading…" while the current
 * namespace is being resolved, then its name, or why there is none.
 */
export function namespaceSwitcherName(
  state: {
    isLoading: boolean;
    current: { displayName?: string; name?: string } | undefined;
    unavailable: NamespaceUnavailable | undefined;
  },
  t: (message: AppMessage) => string,
): string {
  if (state.isLoading) return `${t("Loading")}…`;
  return (
    state.current?.displayName ??
    state.current?.name ??
    localizeNamespaceUnavailable(state.unavailable, t)
  );
}

/** VKG health status in the reader's locale; unknown values are shown raw. */
export function localizeVkgHealth(
  status: string,
  t: (message: AppMessage) => string,
): string {
  const message = lookup(VKG_HEALTH_MESSAGES, status);
  return message ? t(message) : status;
}
