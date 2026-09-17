// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  useMutation,
  useQueryClient,
  UseMutationOptions,
} from "@tanstack/react-query";
import {
  RescanSourceCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type { RescanSourceCommandOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export interface RescanSourceVariables {
  /**
   * Acknowledge that starting this re-scan discards an open re-scan review.
   * The API rejects the call with 409 without it when the source is in
   * RESCAN_REVIEW, because a fresh re-scan re-diffs against the last approved
   * state and drops the decisions and edits made in the open review.
   */
  readonly confirmDiscardOpenReview?: boolean;
}

export function useRescanSource(
  namespaceId: string,
  sourceId: string,
  options?: Omit<
    UseMutationOptions<
      RescanSourceCommandOutput,
      ControlPlaneServiceServiceException,
      RescanSourceVariables
    >,
    "mutationFn"
  >,
) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    RescanSourceCommandOutput,
    ControlPlaneServiceServiceException,
    RescanSourceVariables
  >({
    mutationFn: ({ confirmDiscardOpenReview }: RescanSourceVariables) =>
      client.send(
        new RescanSourceCommand({
          namespaceId,
          sourceId,
          confirmDiscardOpenReview,
        }),
      ),
    onSuccess: (...args) => {
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
      options?.onSuccess?.(...args);
    },
    ...options,
  });
}
