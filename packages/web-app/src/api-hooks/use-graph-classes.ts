// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useInfiniteQuery } from "@tanstack/react-query";
import { useApiClient } from "@components/ApiClientProvider";
import {
  searchEntitiesPaged,
  type GraphSearchResult,
} from "../services/ontology-engine";

/**
 * How many classes each backend fetch pulls. The List view displays a smaller
 * slice per page (see {@link CLASS_DISPLAY_PAGE_SIZE}) and only triggers the
 * next backend fetch when the user pages into not-yet-loaded territory — so a
 * multi-thousand-class namespace stays a handful of round-trips, not one per
 * displayed page.
 */
export const CLASS_FETCH_CHUNK_SIZE = 100;

/** How many classes the List view shows per Cloudscape page. */
export const CLASS_DISPLAY_PAGE_SIZE = 20;

/**
 * Paginated class listing for the Ontology Explorer, backed by the graph-search
 * endpoint's ``{hits, total_count}`` envelope, in chunks of
 * {@link CLASS_FETCH_CHUNK_SIZE}.
 *
 * An empty query is sent as ``*`` (the backend's match-all wildcard).
 * ``ontologyId`` restricts results to that ontology's named graph. The backend
 * returns exactly ``min(limit, remaining)`` classes, so a short chunk means the
 * end of the result set. ``enabled: false`` holds the query off entirely.
 */
export function useGraphClasses(
  namespaceId: string | undefined,
  query: string,
  ontologyId?: string,
  enabled = true,
) {
  const apiClient = useApiClient();
  const q = query.trim() || "*";

  return useInfiniteQuery<GraphSearchResult, Error>({
    queryKey: ["graph-classes", namespaceId, q, ontologyId ?? null],
    enabled: Boolean(namespaceId) && enabled,
    queryFn: async ({ pageParam }) => {
      const offset = (pageParam as number) ?? 0;
      return searchEntitiesPaged(apiClient, namespaceId!, q, {
        kind: "class",
        ontology_id: ontologyId,
        limit: CLASS_FETCH_CHUNK_SIZE,
        offset,
      });
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.hits.length < CLASS_FETCH_CHUNK_SIZE) return undefined;
      const loaded = allPages.reduce((n, p) => n + p.hits.length, 0);
      return loaded < lastPage.total_count ? loaded : undefined;
    },
  });
}
