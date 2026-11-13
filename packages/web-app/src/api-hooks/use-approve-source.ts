// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApproveSourceCommand } from "@coa/control-plane-client";
import type {
  ApproveSourceInput,
  ApproveSourceOutput,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

/**
 * Trigger an asynchronous bulk approve for a DATABASE source.
 *
 * Returns 202 immediately and transitions the source's status to APPROVING.
 * Callers should poll `useGetSource` until the status leaves APPROVING.
 * Invalidates the source detail and tables queries so the polling shows
 * the transient state right away.
 */
export function useApproveSource(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    ApproveSourceOutput,
    Error,
    Omit<ApproveSourceInput, "namespaceId" | "sourceId">
  >({
    mutationFn: () =>
      client.send(new ApproveSourceCommand({ namespaceId, sourceId })),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["sourceTables", namespaceId, sourceId],
      });
      // The prefix matches every sourceType-filtered list.
      queryClient.invalidateQueries({ queryKey: ["sources", namespaceId] });
    },
  });
}
