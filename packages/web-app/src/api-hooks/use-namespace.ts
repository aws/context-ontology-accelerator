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
  /**
   * Whether the current user can open this namespace. Derived from
   * `GetNamespace(id)` succeeding rather than erroring (notably with the
   * server's `AccessDeniedError`). `true` only once the request has
   * positively confirmed access; `false` while loading, on any error, or for
   * an empty id (fail-safe).
   */
  canAccess: boolean;
  /** Whether the underlying `GetNamespace` request is still in flight. */
  isLoading: boolean;
}

/**
 * Resolves a namespace id to both its display name and whether the current
 * user can open it, from a single shared `GetNamespace(id)` call.
 *
 * Authorization in COA is enforced server-side (Cedar policies); the client
 * has no role/permission abstraction. Rather than re-deriving access
 * client-side by enumerating every grant type that could authorize a
 * namespace (namespace grant, platform grant, group grant, etc. — a list
 * that silently drifts out of sync with the server's policy), this hook asks
 * the server directly: `GetNamespace(id)` succeeds only when the caller is
 * authorized to open the namespace and the namespace exists (`namespace.smithy`
 * declares both `AccessDeniedError` and `NotFoundError` on the operation).
 * `canAccess` is simply "did that request succeed".
 *
 * **Fail-safe by design.** This is a UX signal, not a security control — it
 * can never grant visibility the server would deny, because it IS the
 * server's answer. When the request hasn't resolved yet (loading) or failed
 * for any reason (403, 404, network), `canAccess` is `false`, so a crumb
 * stays plain text rather than linking into a namespace the user may not be
 * allowed to open.
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
