// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { GetNamespaceCommand } from "@coa/control-plane-client";
import type { GetNamespaceOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { isFinalLookupFailure } from "@utils/namespace-status";

/** React Query's default number of retries, kept for errors that may pass. */
const MAX_RETRIES = 3;

/**
 * Fetches a single namespace by ID.
 * Disabled when `namespaceId` is empty to prevent unnecessary requests.
 *
 * A 400 or 404 is not retried: the answer will not change. A 403 is retried
 * like 5xx, 401 and network failures, since an expired token also gets 403.
 */
export function useGetNamespace(namespaceId: string) {
  const client = useControlPlaneClient();

  return useQuery<GetNamespaceOutput, Error>({
    queryKey: ["namespace", namespaceId],
    queryFn: async () => {
      return client.send(new GetNamespaceCommand({ namespaceId }));
    },
    enabled: !!namespaceId,
    retry: (failureCount, error) =>
      !isFinalLookupFailure(error) && failureCount < MAX_RETRIES,
  });
}
