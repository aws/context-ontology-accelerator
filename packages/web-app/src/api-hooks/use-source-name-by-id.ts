// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { useListSources } from "./use-list-sources";

/**
 * Resolve `dataSourceId` → human-readable source name for a namespace.
 *
 * Returns a Map keyed by `sourceId`. When a source has no name, the id is used
 * as the fallback value.
 */
export function useSourceNameById(namespaceId: string): Map<string, string> {
  const { data: sourcesData } = useListSources(namespaceId);
  return useMemo(() => {
    const map = new Map<string, string>();
    const items = sourcesData?.pages.flatMap((p) => p.items ?? []) ?? [];
    for (const s of items) {
      if (s.sourceId) map.set(s.sourceId, s.name ?? s.sourceId);
    }
    return map;
  }, [sourcesData]);
}
