// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Induced ontology detail page (route: /namespaces/:namespaceId/ontology/induced).
 *
 * Uses react-query hooks + the Smithy-generated TS client for data fetching.
 * The ontology IRI is passed as the ``ontology_id`` query param.
 */
import { useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Tabs from "@cloudscape-design/components/tabs";
import { SortableTable } from "@components/SortableTable";
import { useOntologyOverview } from "@api-hooks/use-ontology-overview";
import { useListOntologies } from "@api-hooks/use-list-ontologies";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import {
  deleteOntology,
  downloadOntology,
} from "../../services/ontology-engine";
import { OntologyDeleteWarning } from "@components/ontology/OntologyDeleteWarning";
import { downloadTextFile } from "@utils/download";
import { useApiClient } from "@components/ApiClientProvider";
import { TurtleHighlight } from "@components/TurtleHighlight";
import { PendingReviewBanner } from "@components/ontology/PendingReviewBanner";
import { useAppI18n } from "../../i18n/LocaleProvider";

/** Strip an IRI down to its local name (after the last ``#`` or ``/``). */
export function localName(uri: string | undefined | null): string {
  if (!uri) return "—";
  // Strip trailing # or / before extracting
  const trimmed = uri.replace(/[#/]+$/, "");
  const hash = trimmed.lastIndexOf("#");
  const slash = trimmed.lastIndexOf("/");
  const idx = Math.max(hash, slash);
  return idx >= 0 && idx < trimmed.length - 1
    ? trimmed.slice(idx + 1)
    : trimmed;
}

export function InducedOntologyDetailPage() {
  const { t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const [searchParams] = useSearchParams();
  const ontologyId = searchParams.get("ontology_id") ?? "";
  const navigate = useNavigate();
  const apiClient = useApiClient();

  const {
    data: overview,
    isLoading: loading,
    error,
  } = useOntologyOverview(namespaceId, ontologyId || undefined);
  const { data: ontologies } = useListOntologies(namespaceId);
  const record = ontologies?.find(
    (r) => r.ontologyId === ontologyId || r.uri === ontologyId,
  );

  // Breadcrumb: COA › {Namespace} › Ontology › Induction › {Ontology}
  const { name: nsName } = useNamespace(namespaceId);
  const ontologyLabel = record?.title || localName(ontologyId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: t("Ontology") },
    {
      text: t("Induction"),
      href: `/namespaces/${namespaceId}/ontology`,
      namespaceId,
    },
    { text: ontologyLabel },
  ]);

  const [activeTabId, setActiveTabId] = useState("classes");
  const [turtle, setTurtle] = useState<string | null>(null);
  const [turtleLoading, setTurtleLoading] = useState(false);
  const [turtleError, setTurtleError] = useState<string | null>(null);
  const [downloadBusy, setDownloadBusy] = useState(false);
  // The only induced-ontology delete in the app — without it the Induction
  // page's `blockedByInduced` guard can never be cleared.
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const isDeleting = record?.status === "deleting";

  function closeDeleteModal() {
    setShowDeleteModal(false);
    setDeleteConfirmText("");
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!namespaceId || !ontologyId) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteOntology(apiClient, namespaceId, ontologyId);
      // Teardown is async; the inventory polls, this page doesn't.
      closeDeleteModal();
      navigate(`/namespaces/${namespaceId}/ontology/graph?tab=ontologies`);
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : t("Failed to delete"));
    } finally {
      setDeleteBusy(false);
    }
  }

  // Memoised so it is a stable dependency for the groundedAgainst rollup below;
  // `overview?.classes ?? []` allocates a new array on every render otherwise.
  const classes = useMemo(() => overview?.classes ?? [], [overview?.classes]);
  const relationships = overview?.objectProperties ?? [];
  const attributes = overview?.datatypeProperties ?? [];
  // The overview response is a bounded page (#143); the counts come from the
  // un-paged totals so the UI shows the real scale even when the tables render
  // only the first page. Fall back to the loaded length when totals are absent.
  const totalClasses = overview?.totalClasses ?? classes.length;
  const totalRelationships =
    overview?.totalObjectProperties ?? relationships.length;
  const totalAttributes =
    overview?.totalDatatypeProperties ?? attributes.length;

  // Group per-class `groundedTo` IRIs by owning ontology. Uses graph edges, not
  // the induction report — the report is per-job and gone once accepted.
  const groundedAgainst = useMemo(() => {
    const targets = classes
      .map((c) => c.groundedTo)
      .filter((t): t is string => Boolean(t));
    if (targets.length === 0) return [];
    // Longest-prefix wins so a more specific ontology IRI beats a shorter one.
    const candidates = (ontologies ?? [])
      .filter((r) => r.uri && r.ontologyId !== ontologyId)
      .slice()
      .sort((a, b) => (b.uri?.length ?? 0) - (a.uri?.length ?? 0));
    const counts = new Map<
      string,
      { key: string; label: string; count: number }
    >();
    for (const target of targets) {
      const owner = candidates.find((r) => target.startsWith(r.uri!));
      // Fall back to the IRI namespace so unregistered targets still show.
      const key = owner?.ontologyId ?? target.replace(/[^#/]*$/, "");
      const label = owner?.title || owner?.uri || key;
      const prev = counts.get(key);
      // `key` is kept for React — titles aren't unique.
      counts.set(key, { key, label, count: (prev?.count ?? 0) + 1 });
    }
    return [...counts.values()].sort((a, b) => b.count - a.count);
  }, [classes, ontologies, ontologyId]);
  const groundedClassCount = classes.filter((c) => c.groundedTo).length;

  async function ensureTurtle(): Promise<string | null> {
    if (turtle !== null) return turtle;
    if (!namespaceId || !ontologyId) return null;
    setTurtleLoading(true);
    setTurtleError(null);
    try {
      const text = await downloadOntology(apiClient, namespaceId, ontologyId);
      setTurtle(text);
      return text;
    } catch (e) {
      setTurtleError(
        e instanceof Error ? e.message : t("Failed to load ontology source"),
      );
      return null;
    } finally {
      setTurtleLoading(false);
    }
  }

  async function handleDownload() {
    setDownloadBusy(true);
    try {
      const text = await ensureTurtle();
      if (text === null) {
        // ensureTurtle set turtleError — switch to the Source tab so it's visible
        setActiveTabId("source");
        return;
      }
      const safeName = (record?.title || ontologyId)
        .replace(/[^A-Za-z0-9._-]/g, "_")
        .slice(0, 80);
      downloadTextFile(`${safeName || "ontology"}.ttl`, text);
    } finally {
      setDownloadBusy(false);
    }
  }

  function openClass(classUri: string) {
    navigate(
      `/namespaces/${namespaceId}/ontology/induced/class?ontology_id=${encodeURIComponent(
        ontologyId,
      )}&class_uri=${encodeURIComponent(classUri)}`,
    );
  }

  if (!ontologyId) {
    return (
      <Alert type="error" header={t("Missing ontology")}>
        {t("No ontology was specified.")}
      </Alert>
    );
  }

  return (
    <SpaceBetween size="m">
      <PendingReviewBanner />
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            {isDeleting && (
              <Box padding={{ top: "xxs" }}>
                <StatusIndicator type="in-progress">
                  {t("Delete in progress")}
                </StatusIndicator>
              </Box>
            )}
            <Button
              iconName="download"
              loading={downloadBusy}
              onClick={handleDownload}
            >
              {t("Download .ttl")}
            </Button>
            <Button
              onClick={() => setShowDeleteModal(true)}
              disabled={isDeleting}
              disabledReason={t(
                "A delete is already in progress for this ontology.",
              )}
            >
              {t("Delete ontology")}
            </Button>
          </SpaceBetween>
        }
        description={ontologyId}
      >
        {t("Induced Ontology")}
      </Header>

      {error && (
        <Alert type="warning" header={t("Could not load graph contents")}>
          {error.message}
        </Alert>
      )}

      {/* A stuck delete: still "deleting" but teardown failed. Re-issue to retry. */}
      {isDeleting && record?.deleteError && (
        <Alert type="error" header={t("Delete failed — retry to try again")}>
          {record.deleteError}
        </Alert>
      )}

      {showDeleteModal && (
        <Modal
          visible
          onDismiss={closeDeleteModal}
          header={t('Delete ontology "{name}"?', {
            name: record?.title || localName(ontologyId),
          })}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={closeDeleteModal}>
                  {t("Cancel")}
                </Button>
                <Button
                  variant="primary"
                  loading={deleteBusy}
                  disabled={deleteBusy || deleteConfirmText !== "delete"}
                  onClick={confirmDelete}
                >
                  {t("Delete")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <OntologyDeleteWarning
              ontologyType="induced"
              name={record?.title || localName(ontologyId)}
            />
            {deleteError && (
              <Alert type="error" header={t("Delete failed")}>
                {deleteError}
              </Alert>
            )}
            <FormField label={t('Type "delete" to confirm')}>
              <Input
                value={deleteConfirmText}
                onChange={({ detail }) => setDeleteConfirmText(detail.value)}
                placeholder="delete"
                disabled={deleteBusy}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}

      <Container>
        <KeyValuePairs
          columns={4}
          items={[
            { label: t("Classes"), value: String(totalClasses) },
            {
              label: t("Relationships"),
              value: String(totalRelationships),
            },
            { label: t("Attributes"), value: String(totalAttributes) },
            {
              label: t("Grounded against"),
              value:
                groundedAgainst.length === 0 ? (
                  // Mid-fetch must not read as "ungrounded".
                  <Box color="text-status-inactive">
                    {loading ? "—" : t("None (all classes novel)")}
                  </Box>
                ) : (
                  <SpaceBetween size="xxxs">
                    {groundedAgainst.map((g) => (
                      <Box key={g.key}>
                        {g.label}{" "}
                        <Box
                          variant="small"
                          color="text-status-inactive"
                          display="inline"
                        >
                          (
                          {t(
                            g.count === 1 ? "{count} class" : "{count} classes",
                            { count: g.count },
                          )}
                          )
                        </Box>
                      </Box>
                    ))}
                    <Box variant="small" color="text-status-inactive">
                      {t("{grounded} of {total} classes grounded", {
                        grounded: groundedClassCount,
                        total: totalClasses,
                      })}
                    </Box>
                  </SpaceBetween>
                ),
            },
          ]}
        />
      </Container>

      {(classes.length < totalClasses ||
        relationships.length < totalRelationships ||
        attributes.length < totalAttributes) && (
        <Alert type="info" header={t("Showing a partial view")}>
          {t(
            "This ontology is larger than the page the tables load, so they show the first {classesShown} of {classesTotal} classes, {relationshipsShown} of {relationshipsTotal} relationships, and {attributesShown} of {attributesTotal} attributes. Download the .ttl for the complete ontology.",
            {
              classesShown: classes.length,
              classesTotal: totalClasses,
              relationshipsShown: relationships.length,
              relationshipsTotal: totalRelationships,
              attributesShown: attributes.length,
              attributesTotal: totalAttributes,
            },
          )}
        </Alert>
      )}

      <Tabs
        ariaLabel={t("Induced ontology graph contents")}
        activeTabId={activeTabId}
        onChange={({ detail }) => {
          setActiveTabId(detail.activeTabId);
          if (detail.activeTabId === "source") void ensureTurtle();
        }}
        tabs={[
          {
            id: "classes",
            label: t("Classes ({count})", { count: totalClasses }),
            content: (
              <SortableTable
                loading={loading}
                items={classes}
                trackBy="uri"
                variant="embedded"
                defaultSortingColumnId="name"
                columnDefinitions={[
                  {
                    id: "name",
                    header: t("Class"),
                    isRowHeader: true,
                    sortingComparator: (a, b) =>
                      (a.label || localName(a.uri)).localeCompare(
                        b.label || localName(b.uri),
                      ),
                    cell: (c) => (
                      <Link
                        onFollow={(ev) => {
                          ev.preventDefault();
                          openClass(c.uri!);
                        }}
                      >
                        {c.label || localName(c.uri)}
                      </Link>
                    ),
                  },
                  {
                    id: "origin",
                    header: t("Origin"),
                    // Grounded (0) sorts before Novel (1).
                    sortingComparator: (a, b) =>
                      (a.groundedTo ? 0 : 1) - (b.groundedTo ? 0 : 1),
                    cell: (c) =>
                      c.groundedTo ? (
                        <Badge color="blue">
                          {`${t("Grounded")}${
                            c.matchType ? ` (${c.matchType})` : ""
                          }`}
                        </Badge>
                      ) : (
                        <Badge color="green">{t("Novel")}</Badge>
                      ),
                  },
                  {
                    id: "groundedTo",
                    header: t("Grounded to"),
                    sortingComparator: (a, b) =>
                      (a.groundedTo
                        ? localName(a.groundedTo)
                        : ""
                      ).localeCompare(
                        b.groundedTo ? localName(b.groundedTo) : "",
                      ),
                    cell: (c) =>
                      c.groundedTo ? (
                        <span title={c.groundedTo}>
                          {localName(c.groundedTo)}
                        </span>
                      ) : (
                        "—"
                      ),
                  },
                  {
                    id: "comment",
                    header: t("Description"),
                    sortingComparator: (a, b) =>
                      (a.comment || "").localeCompare(b.comment || ""),
                    cell: (c) => c.comment || "—",
                  },
                ]}
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    {t(
                      "No classes persisted in the graph yet. If you just accepted a proposal, the graph may still be materializing — refresh in a moment.",
                    )}
                  </Box>
                }
              />
            ),
          },
          {
            id: "relationships",
            label: t("Relationships ({count})", {
              count: totalRelationships,
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
                    {t(
                      "No object-property relationships persisted in the graph.",
                    )}
                  </Box>
                }
              />
            ),
          },
          {
            id: "attributes",
            label: t("Attributes ({count})", {
              count: totalAttributes,
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
                    id: "domain",
                    header: t("Class"),
                    sortingComparator: (a, b) =>
                      localName(a.domain).localeCompare(localName(b.domain)),
                    cell: (p) => localName(p.domain),
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
                    {t(
                      "No datatype-property attributes persisted in the graph.",
                    )}
                  </Box>
                }
              />
            ),
          },
          {
            id: "source",
            label: t("Source (Turtle)"),
            content: (
              <SpaceBetween size="s">
                {turtleError && (
                  <Alert type="warning" header={t("Could not load source")}>
                    {turtleError}
                  </Alert>
                )}
                {turtleLoading ? (
                  <Box textAlign="center" padding="l">
                    <Spinner /> {t("Loading source…")}
                  </Box>
                ) : turtle ? (
                  <TurtleHighlight>{turtle}</TurtleHighlight>
                ) : (
                  !turtleError && (
                    <Box color="text-body-secondary">
                      {t("No source available for this ontology.")}
                    </Box>
                  )
                )}
              </SpaceBetween>
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
