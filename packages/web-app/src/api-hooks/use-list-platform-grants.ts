// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useInfiniteQuery } from "@tanstack/react-query";
import { ListPlatformGrantsCommand } from "@coa/control-plane-client";
import type {
  ListPlatformGrantsOutput,
  PlatformGrantSummary,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export type { PlatformGrantSummary };

/**
 * Fetches PLATFORM-scoped grants (principal→platform-role bindings),
 * paginated. These apply across every namespace (e.g. platform-admin,
 * platform-viewer).
 *
 * @param principal - Optional principal id to filter the grants by (e.g.
 * `"me"` to check the current user's own platform grants). Omit to list all
 * platform grants.
 */
export function useListPlatformGrants(principal?: string) {
  const client = useControlPlaneClient();

  return useInfiniteQuery<ListPlatformGrantsOutput, Error>({
    queryKey: ["platform-grants", principal],
    queryFn: async ({ pageParam }) => {
      return client.send(
        new ListPlatformGrantsCommand({
          principal,
          nextToken: pageParam as string | undefined,
        }),
      );
    },
    getNextPageParam: (lastPage) => lastPage.nextToken,
    initialPageParam: undefined as string | undefined,
  });
}
