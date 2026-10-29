// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AppMessage } from "../i18n/appMessages";

/**
 * Principal type (`User` / `Group`) in the reader's locale, for every table
 * that lists grants. Any other value (e.g. a newer principal type) is shown
 * raw, and an absent value is "—".
 */
export function localizePrincipalType(
  principalType: string | undefined,
  t: (message: AppMessage) => string,
): string {
  if (!principalType) return "—";
  // The wire values of the API's PrincipalType enum. Compared as literals so
  // the helper has no runtime dependency on the generated client.
  if (principalType === "User") return t("User");
  if (principalType === "Group") return t("Group");
  return principalType;
}
