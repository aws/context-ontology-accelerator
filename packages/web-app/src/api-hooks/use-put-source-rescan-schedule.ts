// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  useMutation,
  useQueryClient,
  UseMutationOptions,
} from "@tanstack/react-query";
import {
  PutSourceRescanScheduleCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type {
  PutSourceRescanScheduleCommandOutput,
  RescanSchedule,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export interface PutRescanScheduleInput {
  readonly enabled: boolean;
  readonly scheduleExpression?: string;
  readonly timezone?: string;
}

/** Configure (or disable) the recurring rescan schedule for a source. */
export function usePutSourceRescanSchedule(
  namespaceId: string,
  sourceId: string,
  options?: Omit<
    UseMutationOptions<
      PutSourceRescanScheduleCommandOutput,
      ControlPlaneServiceServiceException,
      PutRescanScheduleInput
    >,
    "mutationFn"
  >,
) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    PutSourceRescanScheduleCommandOutput,
    ControlPlaneServiceServiceException,
    PutRescanScheduleInput
  >({
    ...options,
    mutationFn: (input: PutRescanScheduleInput) =>
      client.send(
        new PutSourceRescanScheduleCommand({
          namespaceId,
          sourceId,
          enabled: input.enabled,
          scheduleExpression: input.scheduleExpression,
          timezone: input.timezone,
        }),
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

export type { RescanSchedule };
