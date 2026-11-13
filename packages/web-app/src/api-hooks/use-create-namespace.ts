// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  useMutation,
  useQueryClient,
  UseMutationOptions,
} from "@tanstack/react-query";
import {
  CreateNamespaceCommand,
  ControlPlaneServiceServiceException,
} from "@coa/control-plane-client";
import type {
  CreateNamespaceInput,
  CreateNamespaceOutput,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export function useCreateNamespace(
  options?: Omit<
    UseMutationOptions<
      CreateNamespaceOutput,
      ControlPlaneServiceServiceException,
      CreateNamespaceInput
    >,
    "mutationFn"
  >,
) {
  const client = useControlPlaneClient();
  const queryClient = useQueryClient();

  return useMutation<
    CreateNamespaceOutput,
    ControlPlaneServiceServiceException,
    CreateNamespaceInput
  >({
    ...options,
    mutationFn: async (input) => {
      return client.send(new CreateNamespaceCommand(input));
    },
    // See use-create-source: a caller onSuccess must not skip this refresh.
    onSuccess: (...args) => {
      void queryClient.invalidateQueries({ queryKey: ["namespaces"] });
      return options?.onSuccess?.(...args);
    },
  });
}
