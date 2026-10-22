// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { ListPrincipalGrantsCommand } from "@coa/control-plane-client";
import type { ListPrincipalGrantsOutput } from "@coa/control-plane-client";
import { useControlPlaneClient } from "../components/ControlPlaneClientProvider";
import { collectAllPages } from "../utils/pagination";

const SELF_PRINCIPAL = "me";

export function useListPrincipalGrants() {
  const client = useControlPlaneClient();

  return useQuery<ListPrincipalGrantsOutput, Error>({
    queryKey: ["principals", SELF_PRINCIPAL, "grants"],
    // Every page, merged into one output: the profile lists all of the
    // caller's grants. A page can be short or empty while nextToken is set.
    queryFn: async () => {
      const grants = await collectAllPages(
        "ListPrincipalGrants",
        async (nextToken) => {
          const out = await client.send(
            new ListPrincipalGrantsCommand({
              principalId: SELF_PRINCIPAL,
              nextToken,
            }),
          );
          return { items: out.grants ?? [], nextToken: out.nextToken };
        },
      );
      return { grants };
    },
  });
}
