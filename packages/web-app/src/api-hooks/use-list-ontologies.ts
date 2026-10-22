// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import {
  ListOntologiesCommand,
  type OntologyRecord,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import {
  hasOntologyId,
  type ListedOntology,
} from "../services/ontology-engine";
import { collectAllPages } from "../utils/pagination";

export type { OntologyRecord, ListedOntology };

export function useListOntologies(namespaceId: string | undefined) {
  const client = useControlPlaneClient();
  return useQuery<ListedOntology[], Error>({
    queryKey: ["ontologies", namespaceId],
    queryFn: async () => {
      // Every page: callers look a specific ontology up in this list, so a
      // first-page-only result would miss it once a namespace has more than one
      // page. A page can be short or empty while nextToken is still set.
      const rows = await collectAllPages(
        "ListOntologies",
        async (nextToken) => {
          const out = await client.send(
            new ListOntologiesCommand({ namespaceId: namespaceId!, nextToken }),
          );
          return { items: out.ontologies ?? [], nextToken: out.nextToken };
        },
      );
      // Same boundary narrowing as listOntologies(): the generated type makes
      // the @required id optional, so drop rows that can't be keyed or linked.
      return rows.filter(hasOntologyId);
    },
    enabled: !!namespaceId,
  });
}
