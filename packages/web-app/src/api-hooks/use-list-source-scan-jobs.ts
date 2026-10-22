// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { ListSourceScanJobsCommand } from "@coa/control-plane-client";
import type { ListSourceScanJobsOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { collectAllPages } from "../utils/pagination";

export function useListSourceScanJobs(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();

  return useQuery<ListSourceScanJobsOutput, Error>({
    queryKey: ["sourceScanJobs", namespaceId, sourceId],
    // Every page, merged into one output: the Scan-history tab shows the
    // source's whole history. The server returns rows newest first across
    // pages, so concatenating the pages in order keeps that order.
    queryFn: async () => {
      const items = await collectAllPages(
        "ListSourceScanJobs",
        async (nextToken) => {
          const out = await client.send(
            new ListSourceScanJobsCommand({ namespaceId, sourceId, nextToken }),
          );
          return { items: out.items ?? [], nextToken: out.nextToken };
        },
      );
      return { items };
    },
    enabled: !!namespaceId && !!sourceId,
  });
}
