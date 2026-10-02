// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { GetSourceScanJobCommand } from "@coa/control-plane-client";
import type { GetSourceScanJobOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export function useGetSourceScanJob(
  namespaceId: string,
  sourceId: string,
  jobId: string,
  refetchInterval?: number,
) {
  const client = useControlPlaneClient();

  return useQuery<GetSourceScanJobOutput, Error>({
    queryKey: ["sourceScanJob", namespaceId, sourceId, jobId],
    queryFn: () =>
      client.send(
        new GetSourceScanJobCommand({ namespaceId, sourceId, jobId }),
      ),
    enabled: !!namespaceId && !!sourceId && !!jobId,
    refetchInterval,
  });
}
