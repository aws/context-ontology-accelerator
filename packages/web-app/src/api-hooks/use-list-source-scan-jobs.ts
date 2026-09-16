// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { ListSourceScanJobsCommand } from "@coa/control-plane-client";
import type { ListSourceScanJobsOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export function useListSourceScanJobs(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();

  return useQuery<ListSourceScanJobsOutput, Error>({
    queryKey: ["sourceScanJobs", namespaceId, sourceId],
    queryFn: () =>
      client.send(new ListSourceScanJobsCommand({ namespaceId, sourceId })),
    enabled: !!namespaceId && !!sourceId,
  });
}
