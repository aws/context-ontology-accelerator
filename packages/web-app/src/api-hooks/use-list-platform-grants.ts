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
 *
 * Note: adding `principal` to the query key changes it from `["platform-grants"]`
 * to `["platform-grants", principal]`. For the one existing no-arg caller
 * (`IdentityPage`) this is a one-time cache-key change — `undefined` is a
 * present array element, not an absent one, so the old cached entry is
 * orphaned and the first read after this change re-fetches. `invalidateQueries`
 * still catches the new key by its default prefix match, so grant
 * create/delete continues to refresh the list correctly.
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
