// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { NamespaceStatus, VkgHealthStatus } from "@coa/control-plane-client";
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

/** VKG health status in the reader's locale; unknown values are shown raw. */
export function localizeVkgHealth(
  status: string,
  t: (message: AppMessage) => string,
): string {
  const message = lookup(VKG_HEALTH_MESSAGES, status);
  return message ? t(message) : status;
}
