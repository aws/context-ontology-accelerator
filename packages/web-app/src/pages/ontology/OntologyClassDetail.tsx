// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology class drill-down page
 * (route: /namespaces/:namespaceId/ontology/induced/class).
 *
 * Uses the same ``useOntologyOverview`` hook and client-side filters to scope
 * relationships and attributes to one class. No new backend needed.
 */
import { useMemo } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Tabs from "@cloudscape-design/components/tabs";
import { SortableTable } from "@components/SortableTable";
import { useOntologyOverview } from "@api-hooks/use-ontology-overview";
import { useListOntologies } from "@api-hooks/use-list-ontologies";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import { localName } from "./InducedOntologyDetail";
import { useAppI18n } from "../../i18n/LocaleProvider";

export function OntologyClassDetailPage() {
  const { t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const [searchParams] = useSearchParams();
  const ontologyId = searchParams.get("ontology_id") ?? "";
  const classUri = searchParams.get("class_uri") ?? "";

  const {
    data: overview,
    isLoading: loading,
    error,
  } = useOntologyOverview(namespaceId, ontologyId || undefined);

  const cls = useMemo(
    () => overview?.classes?.find((c) => c.uri === classUri) ?? null,
    [overview, classUri],
  );

  // Breadcrumb — origin-aware. Reached two ways:
  //   • from the Explorer "Expand" link (?from=explorer):
  //       COA › {Namespace} › Ontology › Explorer › {Class}
  //   • from the Induced Ontology page (default):
  //       COA › {Namespace} › Ontology › Induction › {Ontology} › {Class}
  const fromOrigin = searchParams.get("from");
  const { data: ontologies } = useListOntologies(namespaceId);
  const { name: nsName } = useNamespace(namespaceId);
  const ontologyLabel =
    ontologies?.find((r) => r.ontologyId === ontologyId || r.uri === ontologyId)
      ?.title || localName(ontologyId);
  const classLabel = cls?.label || localName(classUri);
  const nsCrumb = {
    text: nsName,
    href: `/namespaces/${namespaceId}`,
    namespaceId,
  };
  useBreadcrumbTrail(
    fromOrigin === "explorer"
      ? [
          nsCrumb,
          { text: t("Ontology") },
          {
            text: t("Explorer"),
            href: `/namespaces/${namespaceId}/ontology/graph`,
            namespaceId,
          },
          { text: classLabel },
        ]
      : [
          nsCrumb,
          { text: t("Ontology") },
          {
            text: t("Induction"),
            href: `/namespaces/${namespaceId}/ontology`,
            namespaceId,
          },
          {
            text: ontologyLabel,
            href: `/namespaces/${namespaceId}/ontology/induced?ontology_id=${encodeURIComponent(
              ontologyId,
            )}`,
            namespaceId,
          },
          { text: classLabel },
        ],
  );

  const relationships = useMemo(
    () =>
      (overview?.objectProperties ?? []).filter(
        (p) => p.domain === classUri || p.range === classUri,
      ),
    [overview, classUri],
  );
  const attributes = useMemo(
    () =>
      (overview?.datatypeProperties ?? []).filter((p) => p.domain === classUri),
    [overview, classUri],
  );

  if (!ontologyId || !classUri) {
    return (
      <Alert type="error" header={t("Missing class")}>
        {t("No class was specified.")}
      </Alert>
    );
  }

  return (
    <SpaceBetween size="m">
      <Header variant="h1" description={classUri}>
        {cls?.label || localName(classUri)}
      </Header>

      {error && (
        <Alert type="warning" header={t("Could not load class")}>
          {error.message}
        </Alert>
      )}

      <Container>
        <KeyValuePairs
          columns={3}
          items={[
            {
              label: t("Origin"),
              value: cls?.groundedTo ? (
                <Badge color="blue">
                  {`${t("Grounded")}${
                    cls.matchType ? ` (${cls.matchType})` : ""
                  }`}
                </Badge>
              ) : (
                <Badge color="green">{t("Novel")}</Badge>
              ),
            },
            {
              label: t("Grounded to"),
              value: cls?.groundedTo ? localName(cls.groundedTo) : "—",
            },
            { label: t("Description"), value: cls?.comment || "—" },
          ]}
        />
      </Container>

      <Tabs
        ariaLabel={t("Class relationships and attributes")}
        tabs={[
          {
            id: "relationships",
            label: t("Relationships ({count})", {
              count: relationships.length,
            }),
            content: (
              <SortableTable
                loading={loading}
                items={relationships}
                trackBy="uri"
                variant="embedded"
                defaultSortingColumnId="name"
                columnDefinitions={[
                  {
                    id: "name",
                    header: t("Relationship"),
                    isRowHeader: true,
                    sortingComparator: (a, b) =>
                      (a.label || localName(a.uri)).localeCompare(
                        b.label || localName(b.uri),
                      ),
                    cell: (p) => p.label || localName(p.uri),
                  },
                  {
                    id: "domain",
                    header: t("Subject class"),
                    sortingComparator: (a, b) =>
                      localName(a.domain).localeCompare(localName(b.domain)),
                    cell: (p) => localName(p.domain),
                  },
                  {
                    id: "range",
                    header: t("Object class"),
                    sortingComparator: (a, b) =>
                      localName(a.range).localeCompare(localName(b.range)),
                    cell: (p) => localName(p.range),
                  },
                ]}
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    {t("This class has no object-property relationships.")}
                  </Box>
                }
              />
            ),
          },
          {
            id: "attributes",
            label: t("Attributes ({count})", {
              count: attributes.length,
            }),
            content: (
              <SortableTable
                loading={loading}
                items={attributes}
                trackBy="uri"
                variant="embedded"
                defaultSortingColumnId="name"
                columnDefinitions={[
                  {
                    id: "name",
                    header: t("Attribute"),
                    isRowHeader: true,
                    sortingComparator: (a, b) =>
                      (a.label || localName(a.uri)).localeCompare(
                        b.label || localName(b.uri),
                      ),
                    cell: (p) => p.label || localName(p.uri),
                  },
                  {
                    id: "range",
                    header: t("Datatype"),
                    sortingComparator: (a, b) =>
                      localName(a.range).localeCompare(localName(b.range)),
                    cell: (p) => localName(p.range),
                  },
                ]}
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    {t("This class has no datatype-property attributes.")}
                  </Box>
                }
              />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
