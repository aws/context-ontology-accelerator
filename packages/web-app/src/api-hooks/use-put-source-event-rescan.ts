// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  useMutation,
  useQueryClient,
  UseMutationOptions,
} from "@tanstack/react-query";
import {
  PutSourceEventRescanCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type {
  PutSourceEventRescanCommandOutput,
  EventRescanConfig,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

/** Enable or disable event-driven (Glue-change) rescans for a source. */
export function usePutSourceEventRescan(
  namespaceId: string,
  sourceId: string,
  options?: Omit<
    UseMutationOptions<
      PutSourceEventRescanCommandOutput,
      ControlPlaneServiceServiceException,
      boolean
    >,
    "mutationFn"
  >,
) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    PutSourceEventRescanCommandOutput,
    ControlPlaneServiceServiceException,
    boolean
  >({
    ...options,
    mutationFn: (enabled: boolean) =>
      client.send(
        new PutSourceEventRescanCommand({ namespaceId, sourceId, enabled }),
      ),
    // Spread the caller's options first so a caller onSuccess cannot replace
    // this one and skip the cache refresh. Return the caller's result so the
    // mutation still waits for a promise it returns.
    onSuccess: (...args) => {
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
      return options?.onSuccess?.(...args);
    },
  });
}

export type { EventRescanConfig };
