// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { ListSourcesCommand } from "@coa/control-plane-client";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import { useApiClient } from "@components/ApiClientProvider";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";
import { isForbiddenError } from "@utils";
import { useAppI18n } from "../../i18n/LocaleProvider";
import { listOntologies } from "../../services/ontology-engine";

export type SchemaKind = "source" | "ontology";

/** Route of the namespace-agnostic Playground (it picks the namespace itself). */
const PLAYGROUND_PATH = "/playground";
const PLAYGROUND_TOKEN = "{playground}";

const NoSchemaAccessMessage: React.FC<{ kind: SchemaKind }> = ({ kind }) => {
  const { t } = useAppI18n();
  return (
    <StatusIndicator type="info">
      {kind === "source"
        ? t(
            "Your role doesn't include access to this namespace's data sources.",
          )
        : t("Your role doesn't include access to this namespace's ontology.")}
    </StatusIndicator>
  );
};

/**
 * Neutral notice shown when the caller's role does not include source or
 * ontology visibility (e.g. a schema-blind `data-analyst`). Not an error: the
 * role is denied these reads by design and can still query.
 *
 * This is the compact form, for slots inside an existing page (e.g. a table's
 * empty state). A gated route uses {@link NoSchemaAccessPage} instead.
 */
export const NoSchemaAccess: React.FC<{ kind: SchemaKind }> = ({ kind }) => {
  const { t } = useAppI18n();
  return (
    <Box padding="l" textAlign="center" data-testid="no-schema-access">
      <NoSchemaAccessMessage kind={kind} />
      <Box variant="p" color="text-body-secondary">
        {t("You can still query this namespace in the Playground.")}
      </Box>
    </Box>
  );
};

/**
 * Whole-page form of the notice, rendered when the gate replaces a route.
 * Gives the page its own `h1` and breadcrumb trail (the gated page never
 * mounts, so neither would otherwise exist) and links to the Playground.
 */
export const NoSchemaAccessPage: React.FC<{ kind: SchemaKind }> = ({
  kind,
}) => {
  const { t } = useAppI18n();
  const navigate = useNavigate();
  const { namespaceId = "" } = useParams();
  const { name: nsName } = useNamespace(namespaceId);
  const title = kind === "source" ? t("Sources") : t("Ontology");
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: title },
  ]);

  // The sentence is one catalogue entry with a token where the link goes, so
  // each locale keeps its own word order around the link.
  const [before, after = ""] = t(
    "You can still query this namespace in the {playground}.",
  ).split(PLAYGROUND_TOKEN);

  return (
    <Container header={<Header variant="h1">{title}</Header>}>
      <Box padding="l" textAlign="center" data-testid="no-schema-access">
        <NoSchemaAccessMessage kind={kind} />
        <Box variant="p" color="text-body-secondary">
          {before}
          <Link
            href={PLAYGROUND_PATH}
            onFollow={(e) => {
              e.preventDefault();
              navigate(PLAYGROUND_PATH);
            }}
          >
            {t("Playground")}
          </Link>
          {after}
        </Box>
      </Box>
    </Container>
  );
};

/**
 * Route-level gate for source and ontology pages.
 *
 * Probes the namespace's source (or ontology) list for a single item (a
 * positive answer is cached for five minutes; "no access" is re-checked on
 * every visit) and renders {@link NoSchemaAccessPage} on a 403 instead of
 * letting the page render its own error panels. Any other outcome renders the
 * page, which keeps handling its own errors.
 */
export const SchemaAccessGate: React.FC<{
  kind: SchemaKind;
  children: React.ReactNode;
}> = ({ kind, children }) => {
  const { namespaceId = "" } = useParams();
  const controlPlane = useControlPlaneClient();
  const apiClient = useApiClient();
  const probe = useQuery<boolean>({
    queryKey: ["schema-access", kind, namespaceId],
    enabled: Boolean(namespaceId),
    // Cache only a positive answer. A 403 can also mean an expired token or an
    // authorizer evaluation error, so "no access" is re-checked on every visit
    // rather than shown for minutes to a role that actually has access.
    staleTime: (query) => (query.state.data === false ? 0 : 5 * 60 * 1000),
    gcTime: 5 * 60 * 1000,
    retry: false,
    queryFn: async () => {
      try {
        // Both probes ask for one item: only the authorization answer
        // matters, not the list.
        if (kind === "source") {
          await controlPlane.send(
            new ListSourcesCommand({ namespaceId, maxResults: 1 }),
          );
        } else {
          await listOntologies(apiClient, namespaceId, { limit: 1 });
        }
        return true;
      } catch (err) {
        // Only an authorization denial gates the page; anything else is the
        // page's own error to report.
        return !isForbiddenError(err);
      }
    },
  });

  if (namespaceId && probe.isLoading) {
    return (
      <Box textAlign="center" padding="l">
        <Spinner size="large" />
      </Box>
    );
  }
  if (probe.data === false) {
    return <NoSchemaAccessPage kind={kind} />;
  }
  return <>{children}</>;
};
