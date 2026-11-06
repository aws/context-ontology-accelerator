// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useGetNamespace, useListNamespaces } from "@api-hooks";
import type { NamespaceDetail, NamespaceSummary } from "@api-hooks";

interface NamespaceContextValue {
  current: NamespaceSummary | undefined;
  namespaceId: string;
  namespaces: NamespaceSummary[];
  setNamespace: (namespaceId: string) => void;
  isLoading: boolean;
}

const NamespaceContext = createContext<NamespaceContextValue | undefined>(
  undefined,
);

/** Literal `/namespaces/<segment>` routes that are not a namespace id. */
const NON_ID_SEGMENTS = new Set(["create"]);

/** Extract the namespaceId segment from paths like /namespaces/:id/... */
function namespaceIdFromPath(pathname: string): string | undefined {
  const id = pathname.match(/^\/namespaces\/([^/]+)/)?.[1];
  return id && !NON_ID_SEGMENTS.has(id) ? id : undefined;
}

/** The summary fields of a GetNamespace detail, for an id not yet in the list. */
function toSummary(detail: NamespaceDetail): NamespaceSummary {
  const {
    namespaceId,
    name,
    displayName,
    status,
    language,
    sourceCount,
    createdAt,
    updatedAt,
  } = detail;
  return {
    namespaceId,
    name,
    displayName,
    status,
    language,
    sourceCount,
    createdAt,
    updatedAt,
  };
}

export const NamespaceProvider: React.FC<React.PropsWithChildren> = ({
  children,
}) => {
  const navigate = useNavigate();
  const location = useLocation();
  const {
    data,
    isLoading: isListLoading,
    hasNextPage,
    isFetching,
    isFetchNextPageError,
    fetchNextPage,
  } = useListNamespaces();
  const [selectedId, setSelectedId] = useState<string | undefined>();

  // The list API pages its results (25 per page). The switcher has to reach
  // every namespace the user can see, so load the remaining pages here; the
  // namespace list page shares this query and its cache. Wait while any fetch
  // is in flight (including a refetch after create/delete) so a next-page call
  // never cancels it, and stop on a next-page error instead of retrying it in
  // a loop.
  useEffect(() => {
    if (hasNextPage && !isFetching && !isFetchNextPageError) {
      void fetchNextPage({ cancelRefetch: false });
    }
  }, [hasNextPage, isFetching, isFetchNextPageError, fetchNextPage]);

  const namespaces = useMemo(() => {
    return data?.pages.flatMap((p) => p.namespaces ?? []) ?? [];
  }, [data]);

  // A namespace named in the URL is the current one, whether or not it is in
  // the loaded part of the list. Remember it so pages outside /namespaces/:id
  // (Playground, MCP) keep acting on the namespace the user last opened.
  const urlId = namespaceIdFromPath(location.pathname);
  useEffect(() => {
    if (urlId) setSelectedId(urlId);
  }, [urlId]);
  const activeId = urlId ?? selectedId;

  const listed = activeId
    ? namespaces.find((n) => n.namespaceId === activeId)
    : undefined;
  // Not in the list (yet): ask for that one namespace. This shares the
  // ["namespace", id] cache entry with the namespace detail page.
  const lookup = useGetNamespace(activeId && !listed ? activeId : "");
  const lookedUp =
    lookup.isSuccess && lookup.data.namespace
      ? lookup.data.namespace
      : undefined;
  const lookupFailed = !listed && lookup.isError;

  const current = useMemo(() => {
    if (!activeId) return namespaces[0];
    if (listed) return listed;
    if (lookedUp) return toSummary(lookedUp);
    // A remembered choice the server no longer returns (deleted, or access
    // removed) falls back to the first namespace, as before. A namespace that
    // is still being resolved, or the one named in the URL, never falls back:
    // a Playground question must not go to a different namespace.
    if (lookupFailed && activeId !== urlId) return namespaces[0];
    return undefined;
  }, [activeId, listed, lookedUp, lookupFailed, urlId, namespaces]);

  const namespaceId = current?.namespaceId ?? "";
  const isLoading =
    isListLoading || (!!activeId && !current && lookup.isLoading);

  const setNamespace = useCallback(
    (id: string) => {
      setSelectedId(id);
      // Don't navigate away from the Playground page
      if (!location.pathname.includes("/playground")) {
        navigate(`/namespaces/${id}/sources`);
      }
    },
    [navigate, location.pathname],
  );

  const value = useMemo<NamespaceContextValue>(
    () => ({ current, namespaceId, namespaces, setNamespace, isLoading }),
    [current, namespaceId, namespaces, setNamespace, isLoading],
  );

  return (
    <NamespaceContext.Provider value={value}>
      {children}
    </NamespaceContext.Provider>
  );
};

export function useCurrentNamespace(): NamespaceContextValue {
  const ctx = useContext(NamespaceContext);
  if (!ctx)
    throw new Error(
      "useCurrentNamespace must be used inside NamespaceProvider",
    );
  return ctx;
}
