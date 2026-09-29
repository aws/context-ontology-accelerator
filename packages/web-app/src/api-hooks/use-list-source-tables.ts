// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  ListSourceTablesCommand,
  ReviewStatus,
} from "@coa/control-plane-client";
import type { TableSummary } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export interface ListSourceTablesResult {
  items: TableSummary[];
  /** Total number of assets skipped across all pages due to retrieval/parse failures. */
  skippedAssets: number;
}

/** One `ListSourceTables` page, plus the cursor to the next one. */
interface TablesPage {
  items: TableSummary[];
  skippedAssets: number;
  nextToken?: string;
}

/** DataZone's Search API hard limit, so also our page size. */
const PAGE_SIZE = 50;

/**
 * Fetches all tables for a DATABASE source, one page at a time.
 *
 * Pages are exposed as they arrive rather than after the last one lands. This
 * used to be a single `useQuery` whose `queryFn` drained every page in a
 * `do/while` before resolving, so a source with 860 tables meant 18 sequential
 * round-trips behind one spinner and nothing on screen until all 18 finished —
 * a wait proportional to source size with no feedback. `useInfiniteQuery` keeps
 * the same total work but makes each page renderable the moment it returns.
 *
 * Remaining pages are still drained automatically (the effect below), so callers
 * keep getting the complete list without paging by hand; they just get a growing
 * prefix of it in the meantime. `isLoading` covers the FIRST page only —
 * `isLoadingMore` reports that the list is renderable but not yet complete, so
 * a caller can avoid presenting a partial list as final.
 */
export function useListSourceTables(
  namespaceId: string,
  sourceId: string,
  reviewStatus?: ReviewStatus,
) {
  const client = useControlPlaneClient();

  const query = useInfiniteQuery({
    queryKey: ["sourceTables", namespaceId, sourceId, reviewStatus],
    // "" is the first-page cursor: the API takes no nextToken for page 1, and an
    // empty-string sentinel keeps the page-param type a plain `string` (no cast).
    initialPageParam: "",
    queryFn: async ({
      pageParam,
    }: {
      pageParam: string;
    }): Promise<TablesPage> => {
      const result = await client.send(
        new ListSourceTablesCommand({
          namespaceId,
          sourceId,
          maxResults: PAGE_SIZE,
          ...(reviewStatus && { reviewStatus }),
          ...(pageParam && { nextToken: pageParam }),
        }),
      );
      return {
        items: result.items ?? [],
        skippedAssets: result.skippedAssets ?? 0,
        nextToken: result.nextToken ?? undefined,
      };
    },
    getNextPageParam: (lastPage: TablesPage) => lastPage.nextToken,
    enabled: !!namespaceId && !!sourceId,
  });

  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  const pagesLoaded = query.data?.pages.length ?? 0;
  // Keep draining in the background. Consumers of this hook want the whole list
  // (sorting, filtering and batch review all operate over it); the point of the
  // rewrite is that they can render what has arrived while the rest is in
  // flight, not that they now have to ask for it.
  //
  // `pagesLoaded` is a dependency on purpose: the flags alone can settle back to
  // the same pair (hasNextPage=true, isFetchingNextPage=false) they already had,
  // in which case React skips the effect and the drain stalls partway. Each
  // landed page changes the count, so every page is followed by exactly one
  // more request until the cursor runs out.
  //
  // Stop draining once a page has errored: React Query leaves `hasNextPage` true
  // (the last SUCCESSFUL page still carried a cursor) but will not refetch on its
  // own, so without the `!isError` guard this effect condition would stay true
  // with no request in flight — a silent stall. Halting here lets the caller see
  // the error and the partial prefix instead.
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && !query.isError)
      void fetchNextPage();
  }, [
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    pagesLoaded,
    query.isError,
  ]);

  const data: ListSourceTablesResult | undefined = useMemo(() => {
    if (!query.data) return undefined;
    return {
      items: query.data.pages.flatMap((p) => p.items),
      skippedAssets: query.data.pages.reduce((n, p) => n + p.skippedAssets, 0),
    };
  }, [query.data]);

  return {
    data,
    /** First page only — false as soon as there is something to render. */
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    isError: query.isError,
    error: query.error,
    // Renderable but incomplete: more pages are still on the way. False once a
    // page has errored — the drain has stopped, so this is no longer "loading",
    // it is "loaded a partial list and then failed". Without the `!isError`
    // guard `hasNextPage` stays true after a mid-drain failure and this would
    // report a perpetual spinner that never resolves and never errors.
    isLoadingMore: !query.isError && (hasNextPage || isFetchingNextPage),
    /** Pages received so far, for progress messaging. */
    pagesLoaded,
  };
}
