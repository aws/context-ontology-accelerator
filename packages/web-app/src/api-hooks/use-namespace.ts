// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useGetNamespace } from "./use-get-namespace";

export interface NamespaceInfo {
  /**
   * Human-readable name for display (e.g. a breadcrumb crumb):
   * `displayName > name > id > ""`. Falls back to the id until the name
   * loads, or to `""` when there is no id at all.
   */
  name: string;
  /** `true` only once `GetNamespace(id)` succeeds. */
  canAccess: boolean;
  /** Whether the underlying `GetNamespace` request is still in flight. */
  isLoading: boolean;
}

/**
 * Resolves a namespace id to both its display name and whether the current
 * user can open it, from a single shared `GetNamespace(id)` call.
 *
 * Access comes from `GetNamespace(id)` succeeding, which the server authorizes
 * with Cedar policies. It is a UX signal that fails safe: while loading or on
 * any error `canAccess` is `false`.
 */
export function useNamespace(namespaceId: string | undefined): NamespaceInfo {
  const { data, isSuccess, isLoading } = useGetNamespace(namespaceId ?? "");

  return {
    name:
      data?.namespace?.displayName ??
      data?.namespace?.name ??
      namespaceId ??
      "",
    canAccess: !!namespaceId && isSuccess,
    isLoading,
  };
}
