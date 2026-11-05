// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  useMutation,
  useQueryClient,
  UseMutationOptions,
} from "@tanstack/react-query";
import {
  DeleteSourceCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type { DeleteSourceCommandOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export function useDeleteSource(
  namespaceId: string,
  sourceId: string,
  options?: Omit<
    UseMutationOptions<
      DeleteSourceCommandOutput,
      ControlPlaneServiceServiceException,
      void
    >,
    "mutationFn"
  >,
) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    DeleteSourceCommandOutput,
    ControlPlaneServiceServiceException,
    void
  >({
    ...options,
    mutationFn: () =>
      client.send(new DeleteSourceCommand({ namespaceId, sourceId })),
    // Spread the caller's options first so a caller onSuccess cannot replace
    // this one and skip the cache refresh. Return the caller's result so the
    // mutation still waits for a promise it returns.
    onSuccess: (...args) => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
      queryClient.invalidateQueries({
        queryKey: ["source", namespaceId, sourceId],
      });
      // Namespace counters drop after delete — see the corresponding
      // create hook for the matching invalidation on the create path.
      queryClient.invalidateQueries({ queryKey: ["namespaces"] });
      queryClient.invalidateQueries({
        queryKey: ["namespace", namespaceId],
      });
      return options?.onSuccess?.(...args);
    },
  });
}
