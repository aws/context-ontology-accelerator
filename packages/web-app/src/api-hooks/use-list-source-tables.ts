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
 * Pages render as they arrive and the remaining pages are drained
 * automatically. `isLoading` covers the first page only;
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
  // `pagesLoaded` is a dependency on purpose: the flags can settle back to the
  // same pair, in which case React skips the effect and the drain stalls.
  // React Query leaves `hasNextPage` true after a page error, so the drain must
  // also stop on `isError`.
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
    // More pages are on the way; false once a page has errored.
    isLoadingMore: !query.isError && (hasNextPage || isFetchingNextPage),
    /** Pages received so far, for progress messaging. */
    pagesLoaded,
  };
}
