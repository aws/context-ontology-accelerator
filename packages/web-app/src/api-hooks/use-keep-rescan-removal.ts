// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  KeepRescanRemovalCommand,
  type KeepRescanRemovalInput,
  type KeepRescanRemovalOutput,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

/**
 * Decline a re-scan-flagged removal so approving the re-scan keeps the table
 * (or, when `columnName` is given, one column of it) instead of deleting it.
 *
 * Only valid while the source is in RESCAN_REVIEW. Idempotent server-side.
 * Invalidates the table list, the source, and the single-table detail so the
 * `pendingDeletion` badge clears after a keep.
 */
export function useKeepRescanRemoval(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    KeepRescanRemovalOutput,
    Error,
    Pick<KeepRescanRemovalInput, "tableId" | "columnName">
  >({
    mutationFn: (input) =>
      client.send(
        new KeepRescanRemovalCommand({
          namespaceId,
          sourceId,
          ...input,
        }),
      ),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["sourceTables", namespaceId, sourceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["sourceTable", namespaceId, sourceId, variables.tableId],
      });
    },
  });
}
