// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import {
  GetOntologyOverviewCommand,
  type OntologyOverview,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export type { OntologyOverview };
export type {
  OntologyClassSummary,
  OntologyPropertySummary,
} from "@coa/control-plane-client";

// Bound each collection so the response stays well under the API Gateway /
// Lambda 6 MB response limit (#143). The detail view renders this page and
// shows the true totals (totalClasses / totalObjectProperties /
// totalDatatypeProperties) from the response.
export const OVERVIEW_PAGE_LIMIT = 2000;

export function useOntologyOverview(
  namespaceId: string | undefined,
  ontologyId: string | undefined,
) {
  const client = useControlPlaneClient();
  return useQuery<OntologyOverview, Error>({
    queryKey: ["ontology-overview", namespaceId, ontologyId],
    queryFn: () =>
      client.send(
        new GetOntologyOverviewCommand({
          namespaceId: namespaceId!,
          ontologyId: ontologyId!,
          limit: OVERVIEW_PAGE_LIMIT,
        }),
      ),
    enabled: !!namespaceId && !!ontologyId,
  });
}
