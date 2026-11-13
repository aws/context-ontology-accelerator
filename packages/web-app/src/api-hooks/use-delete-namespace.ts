// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  DeleteNamespaceCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type { DeleteNamespaceInput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export interface DeleteNamespaceBlockers {
  message: string;
  blockers: string[];
}

/**
 * Mutation hook to delete a namespace.
 *
 * Returns 409 with a blockers array if child resources still exist; onError
 * receives the raw exception, so call parseBlockers on it.
 */
export function useDeleteNamespace() {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    void,
    ControlPlaneServiceServiceException,
    DeleteNamespaceInput
  >({
    mutationFn: async (input) => {
      await client.send(new DeleteNamespaceCommand(input));
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["namespace", variables.namespaceId],
      });
      queryClient.invalidateQueries({ queryKey: ["namespaces"] });
    },
  });
}

/**
 * Parse blockers from a 409 ConflictError response body.
 */
export function parseBlockers(error: unknown): DeleteNamespaceBlockers | null {
  if (
    error &&
    typeof error === "object" &&
    "$metadata" in (error as Record<string, unknown>)
  ) {
    const svcError = error as ControlPlaneServiceServiceException;
    if (svcError.$metadata?.httpStatusCode === 409) {
      const msg = svcError.message || "Conflict";
      try {
        const body = JSON.parse(msg);
        if (body.blockers) {
          return { message: body.message, blockers: body.blockers };
        }
      } catch {
        // message is a plain string (Smithy extracts message field directly)
      }
      return { message: msg, blockers: [] };
    }
  }
  return null;
}
