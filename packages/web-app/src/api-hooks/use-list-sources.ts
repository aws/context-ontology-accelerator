// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useInfiniteQuery } from "@tanstack/react-query";
import { ListSourcesCommand } from "@coa/control-plane-client";
import type { ListSourcesOutput, SourceType } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { useEffect } from "react";

// Active statuses that warrant polling for updates
const ACTIVE_STATUSES = new Set(["REGISTERED", "SCANNING", "ENRICHING"]);
const POLL_INTERVAL_MS = 10_000;

export function useListSources(namespaceId: string, sourceType?: SourceType) {
  const client = useControlPlaneClient();

  const query = useInfiniteQuery<ListSourcesOutput, Error>({
    queryKey: ["sources", namespaceId, sourceType ?? "all"],
    queryFn: async ({ pageParam }) => {
      return client.send(
        new ListSourcesCommand({
          namespaceId,
          sourceType,
          maxResults: 100,
          nextToken: pageParam as string | undefined,
        }),
      );
    },
    getNextPageParam: (lastPage) => lastPage.nextToken,
    initialPageParam: undefined as string | undefined,
    refetchInterval: (q) => {
      const pages = q.state.data?.pages ?? [];
      const hasActive = pages.some((page) =>
        (page.items ?? []).some((item) =>
          ACTIVE_STATUSES.has(item.status ?? ""),
        ),
      );
      return hasActive ? POLL_INTERVAL_MS : false;
    },
    enabled: !!namespaceId,
  });

  // Auto-fetch all pages so summary counts are accurate
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  return query;
}
