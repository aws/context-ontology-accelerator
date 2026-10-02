// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ontology management page — induction, proposals, and the ontologies
 * catalog (both induced ontologies and foundational/uploaded ontologies).
 *
 * All ontology-engine calls go through API Gateway via `useApiClient()`.
 * Datasource list comes from `/namespaces/{ns}/sources` (SourcesStack).
 */
import React, { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Tabs from "@cloudscape-design/components/tabs";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import Multiselect from "@cloudscape-design/components/multiselect";
import type { MultiselectProps } from "@cloudscape-design/components/multiselect";
import Select from "@cloudscape-design/components/select";
import Slider from "@cloudscape-design/components/slider";
import RadioGroup from "@cloudscape-design/components/radio-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Link from "@cloudscape-design/components/link";
import Popover from "@cloudscape-design/components/popover";
import { formatTimestamp } from "@utils/helpers";
import {
  proposalBlocksNewInduction,
  proposalScope,
  proposalScopeSortKey,
  proposalSourceTypeLabel,
} from "./ontology/induce/helpers";
import {
  listFoundationalOntologies,
  listOntologies,
  deleteOntology,
  listProposals,
  triggerInduction,
  getInductionJob,
  uploadOntologyFile,
  loadFoundationalOntology,
  type FoundationalOntology,
  type OntologyRecord,
  type ListedOntology,
  type Proposal,
} from "../services/ontology-engine";
import {
  useApiClient,
  type ApiClient,
  ApiError,
} from "@components/ApiClientProvider";
import { SortableTable } from "@components/SortableTable";
import { OntologyDeleteWarning } from "@components/ontology/OntologyDeleteWarning";
import {
  ontologyTypeDisplay,
  ontologyTypeGroup,
} from "@utils/ontology-display";
import { InductionStages } from "./ontology/induce/InductionStages";
import Badge from "@cloudscape-design/components/badge";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import FileUpload from "@cloudscape-design/components/file-upload";
import Input from "@cloudscape-design/components/input";

/** Map a custom-uploaded ontology filename to the format string the
 *  uploadOntologyFile endpoint expects. */
function inferOntologyFormat(name: string): "turtle" | "rdf+xml" | "json-ld" {
  const lower = name.toLowerCase();
  if (
    lower.endsWith(".rdf") ||
    lower.endsWith(".owl") ||
    lower.endsWith(".xml")
  )
    return "rdf+xml";
  if (lower.endsWith(".jsonld") || lower.endsWith(".json")) return "json-ld";
  return "turtle";
}

/** Parse an ISO string or epoch (seconds or milliseconds) into epoch
 *  milliseconds; null when unparseable. Tolerant because scan-history `at` and
 *  proposal `created_at` can reach the raw client in different shapes. */
function toEpochMs(v: unknown): number | null {
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
  if (typeof v === "string") {
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  return null;
}

/** Newest resolved-re-scan time (epoch ms) in a source's scan history — the
 *  latest REVIEW row flagged isRescan — or null if there is none. */
function latestRescanMs(
  items:
    | Array<{ eventType?: string; isRescan?: boolean; at?: unknown }>
    | undefined,
): number | null {
  let max: number | null = null;
  for (const e of items ?? []) {
    if (e.eventType === "REVIEW" && e.isRescan === true) {
      const ms = toEpochMs(e.at);
      if (ms !== null && (max === null || ms > max)) max = ms;
    }
  }
  return max;
}

export function OntologyPage() {
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const [activeTab, setActiveTab] = useState("proposals");
  const [inducting, setInducting] = useState(false);
  const [inductionError, setInductionError] = useState<string | null>(null);
  // Set when a run is short-circuited by duplicate detection: the proposal_id of
  // the pre-existing accepted proposal with an identical structural fingerprint.
  // We surface a notice with a link to it (rather than auto-navigating) so the
  // user stays in control and understands no new proposal was created.
  const [duplicateProposalId, setDuplicateProposalId] = useState<string | null>(
    null,
  );
  const [showInductionModal, setShowInductionModal] = useState(false);
  const [selectedDatasources, setSelectedDatasources] = useState<
    MultiselectProps.Option[]
  >([]);
  const [datasourceOptions, setDatasourceOptions] = useState<
    MultiselectProps.Option[]
  >([]);
  const [docSourceOptions, setDocSourceOptions] = useState<
    MultiselectProps.Option[]
  >([]);
  const [datasourceError, setDatasourceError] = useState<string | null>(null);
  // Names of APPROVED database sources whose ACCEPTED induction is out of date:
  // the source was re-scanned and re-approved after its latest accepted
  // induction. Drives the "stale induction" warning, which lists them.
  const [staleInducedSourceNames, setStaleInducedSourceNames] = useState<
    string[]
  >([]);
  // Names of APPROVED database sources whose newest induction is a PENDING
  // proposal that predates the source's re-scan — the proposal awaiting review
  // was built on the older source. Drives the "pending induction is stale"
  // warning. A source is in at most one of these two lists (routed by whichever
  // its newest induction is).
  const [stalePendingSourceNames, setStalePendingSourceNames] = useState<
    string[]
  >([]);
  const [strategy, setStrategy] = useState("table_to_ontology");
  const [groundingMode, setGroundingMode] = useState("ENHANCED");
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.8);
  const [pollingJobId, setPollingJobId] = useState<string | null>(null);
  const [pollingJobStatus, setPollingJobStatus] = useState<string>("pending");
  const [pollingJobError, setPollingJobError] = useState<string | undefined>();
  // Server-derived "an induction is already in flight or awaiting review"
  // signal. Unlike the ephemeral `inducting` flag, this is computed from the
  // backend proposal list, so the Start-induction lock SURVIVES a browser
  // refresh (the backend enforces one-at-a-time with a 409; this just reflects
  // it in the UI so the user isn't invited to trigger a guaranteed conflict).
  const [inductionLocked, setInductionLocked] = useState(false);
  // Bumped after a trigger / 409 to re-derive the lock from the server.
  const [lockRefreshKey, setLockRefreshKey] = useState(0);
  // Foundational ontology grounding — catalog fetched lazily when the
  // Start Induction modal opens, wired to grounding_ontology_ids.
  const [foundationalCatalog, setFoundationalCatalog] = useState<
    FoundationalOntology[] | null
  >(null);
  const [foundationalError, setFoundationalError] = useState<string | null>(
    null,
  );
  const [selectedFoundationalKeys, setSelectedFoundationalKeys] = useState<
    string[]
  >([]);
  // Already-loaded foundational ontologies for the namespace — fetched
  // alongside the curated catalog so the grounding Multiselect only offers
  // ontologies that are actually queryable. A foundational counts as loaded
  // when there is a registry row whose URI matches the catalog entry's URI
  // AND embedding_count > 0 (registry row alone isn't enough; the Match step
  // needs embeddings).
  const [loadedOntologyRecords, setLoadedOntologyRecords] = useState<
    OntologyRecord[]
  >([]);

  const navigate = useNavigate();
  const apiClient = useApiClient();

  useEffect(() => {
    // Load available DATABASE sources via the unified sources endpoint.
    // SourcesStack endpoint /sources is the post-!169 source-of-truth that
    // covers both Glue and JDBC datasources; the legacy /data-sources endpoint
    // (StructuredStack) only sees JDBC sources.
    let cancelled = false;
    setDatasourceError(null);
    apiClient
      .get<{
        items: Array<{
          sourceId: string;
          name?: string;
          sourceType?: string;
          sourceSubType?: string;
          status?: string;
          tablesDiscovered?: number;
        }>;
      }>(`/namespaces/${namespaceId}/sources?sourceType=DATABASE`)
      .then((data) => {
        if (cancelled) return;
        setDatasourceOptions(
          (data.items || [])
            .filter((s) => s.status === "APPROVED" || s.status === "COMPLETED")
            .map((s) => ({
              value: s.sourceId,
              label: s.name || s.sourceId,
              description: [
                s.sourceType,
                s.sourceSubType,
                s.tablesDiscovered != null
                  ? `${s.tablesDiscovered} tables`
                  : undefined,
              ]
                .filter(Boolean)
                .join(" · "),
            })),
        );
        // A source's induction is STALE when the source was re-scanned and
        // re-approved AFTER its newest induction. We look at the newest
        // induction of ANY status (accepted OR pending):
        //   induction time = newest created_at of an "induction" proposal
        //     (status accepted or pending) whose metadata.datasource_ids
        //     includes the source.
        //   re-scan time   = newest scan-history REVIEW row with isRescan.
        // If the newest induction postdates the re-scan the source is current —
        // this is how a fresh re-induction, even one still pending review,
        // clears the banner. If the re-scan postdates it the source is stale,
        // and we route it by that newest induction's status: an accepted
        // induction is an out-of-date ontology; a pending one is a proposal
        // built on the older source. Best-effort — one proposals fetch plus one
        // scan-history fetch per approved source (no per-source re-scan marker
        // exists); failures are ignored.
        const approvedDbSources = (data.items || []).filter(
          (s) => s.status === "APPROVED",
        );
        if (approvedDbSources.length === 0) {
          if (!cancelled) {
            setStaleInducedSourceNames([]);
            setStalePendingSourceNames([]);
          }
        } else {
          Promise.all([
            // No status filter: we need accepted AND pending inductions.
            listProposals(apiClient, namespaceId ?? "").catch(
              (): Proposal[] => [],
            ),
            Promise.all(
              approvedDbSources.map((s) =>
                apiClient
                  .get<{
                    items?: Array<{
                      eventType?: string;
                      isRescan?: boolean;
                      at?: unknown;
                    }>;
                  }>(`/namespaces/${namespaceId}/sources/${s.sourceId}/scan`)
                  .then((h) => ({
                    sourceId: s.sourceId,
                    name: s.name,
                    rescanMs: latestRescanMs(h.items),
                  }))
                  .catch(() => ({
                    sourceId: s.sourceId,
                    name: s.name,
                    rescanMs: null,
                  })),
              ),
            ),
          ])
            .then(([proposals, rescans]) => {
              if (cancelled) return;
              // Newest induction per source id: its time (epoch ms) and the
              // status of that newest proposal (accepted or pending).
              const induction = new Map<
                string,
                { ms: number; status: string }
              >();
              for (const p of proposals) {
                if (p.proposal_type !== "induction") continue;
                if (p.status !== "accepted" && p.status !== "pending") continue;
                const ms = toEpochMs(p.created_at);
                if (ms === null) continue;
                const raw = p.metadata?.["datasource_ids"];
                const ids = Array.isArray(raw)
                  ? raw.filter((x): x is string => typeof x === "string")
                  : [];
                for (const id of ids) {
                  const prev = induction.get(id);
                  if (prev === undefined || ms > prev.ms)
                    induction.set(id, { ms, status: p.status });
                }
              }
              // A source is stale when its newest re-scan postdates its newest
              // induction. Route the name by that induction's status: accepted =>
              // out-of-date ontology; pending => proposal built on the old source.
              const acceptedStale: string[] = [];
              const pendingStale: string[] = [];
              for (const { sourceId, name, rescanMs } of rescans) {
                if (rescanMs === null) continue;
                const ind = induction.get(sourceId);
                if (ind === undefined || rescanMs <= ind.ms) continue;
                const label = name || sourceId;
                if (ind.status === "accepted") acceptedStale.push(label);
                else pendingStale.push(label);
              }
              setStaleInducedSourceNames(acceptedStale);
              setStalePendingSourceNames(pendingStale);
            })
            .catch((err) => {
              // Fail open if the proposals/scan fetches or the staleness
              // computation throw: clear the banners rather than leave the nested
              // promise's rejection unhandled. Log first (advisory feature, but
              // silence would make a vanished banner undebuggable), then clear.
              console.error("Failed to compute source staleness", {
                error: err,
              });
              if (cancelled) return;
              setStaleInducedSourceNames([]);
              setStalePendingSourceNames([]);
            });
        }
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setDatasourceError(e.message || "Failed to load data sources");
        // Reset the stale-source lists so a banner from a previous namespace
        // doesn't linger when this namespace's source fetch fails.
        setStaleInducedSourceNames([]);
        setStalePendingSourceNames([]);
      });
    return () => {
      cancelled = true;
    };
    // lockRefreshKey + pollingJobStatus mirror the induction-lock effect so the
    // staleness banners recompute after a trigger/accept (and the lock-key
    // bump), clearing in place once the user re-induces — not only on remount.
  }, [namespaceId, apiClient, lockRefreshKey, pollingJobStatus]);

  // Lazy-fetch the foundational ontology catalog and all loaded ontologies
  // when the Induce modal opens. The grounding multiselect shows only
  // ontologies that are already loaded (embeddings > 0).
  useEffect(() => {
    if (!showInductionModal || !namespaceId || foundationalCatalog !== null)
      return;
    let cancelled = false;
    setFoundationalError(null);
    Promise.all([
      listFoundationalOntologies(apiClient, namespaceId),
      listOntologies(apiClient, namespaceId).catch((): ListedOntology[] => []),
    ])
      .then(([catalogResp, allLoaded]) => {
        if (cancelled) return;
        setFoundationalCatalog(catalogResp.items ?? []);
        // Show ALL loaded ontologies — including ones that have not finished
        // embedding (embedding_count == 0/null) or failed to ingest. Hiding
        // them here made a still-embedding or failed-ingest ontology silently
        // vanish from Start Induction with no signal (issue #540). Readiness
        // (and thus selectability) is decided per row in the multiselect
        // options below via ontologyIngestState().
        const loaded = allLoaded ?? [];
        setLoadedOntologyRecords(loaded);
        // Pre-check the namespace's accepted induced ontologies by default so
        // new runs converge with prior ones (incremental induction). Only
        // READY induced ontologies are pre-selected — an un-embedded one is
        // shown disabled and cannot be grounded against. The user can deselect
        // them — the backend grounds against exactly the selected pool, with
        // no server-side auto-include.
        setSelectedFoundationalKeys(
          loaded
            .filter(
              (r) =>
                r.ontologyType === "induced" &&
                ontologyIngestState(r) === "ready",
            )
            .map((r) => r.uri ?? ""),
        );
      })
      .catch((e: Error) => {
        if (!cancelled)
          setFoundationalError(
            e.message || "Failed to load foundational ontologies",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [showInductionModal, namespaceId, apiClient, foundationalCatalog]);

  // Load DOCUMENTS sources for unstructured induction.
  useEffect(() => {
    let cancelled = false;
    apiClient
      .get<{
        items: Array<{
          sourceId: string;
          name?: string;
          sourceType?: string;
          status?: string;
        }>;
      }>(`/namespaces/${namespaceId}/sources?sourceType=DOCUMENTS`)
      .then((data) => {
        if (cancelled) return;
        const docs = (data.items || []).filter(
          (s) => s.status === "COMPLETED" || s.status === "ACTIVE",
        );
        setDocSourceOptions(
          docs.map((s) => ({
            value: s.sourceId,
            label: s.name || s.sourceId,
            description: s.sourceType,
          })),
        );
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setDatasourceError(e.message || "Failed to load data sources");
      });
    return () => {
      cancelled = true;
    };
  }, [namespaceId, apiClient]);
  // Poll induction job status. Cleanup ensures interval is cancelled on
  // unmount or when pollingJobId changes (e.g. user starts a second induction).
  // On completion the page navigates to the freshly-created proposal so
  // the user lands on actionable content instead of having to scan the
  // proposals tab. On failure the stages strip surfaces the error and
  // the user stays on the page to retry.
  useEffect(() => {
    if (!pollingJobId || !namespaceId) return;
    const interval = setInterval(async () => {
      try {
        const j = await getInductionJob(apiClient, namespaceId, pollingJobId);
        setPollingJobStatus(j.status);
        if (j.status === "completed") {
          setPollingJobId(null);
          setInducting(false);
          // Decide what to do (duplicate notice / navigate / refresh) via a pure
          // helper. Duplicate detection keys on the FULL combined table set
          // across every selected datasource, so it only fires for an exact
          // structural match already accepted — a superset selection (e.g.
          // adding a second datasource) has a different fingerprint and proceeds
          // as a normal new induction.
          const action = resolveCompletedInductionAction(j, pollingJobId);
          if (action.kind === "duplicate") {
            // Do NOT auto-navigate — surface a notice with a link to the
            // pre-existing proposal and let the user decide.
            setDuplicateProposalId(action.proposalId);
            setActiveTab("proposals");
          } else if (action.kind === "navigate") {
            navigate(
              `/namespaces/${namespaceId}/ontology/proposals/${action.proposalId}`,
            );
          } else {
            // Nothing to navigate to — refresh the proposals tab.
            setActiveTab("ontologies");
            setTimeout(() => setActiveTab("proposals"), 0);
          }
        } else if (j.status === "failed") {
          setPollingJobId(null);
          setInducting(false);
          setPollingJobError(j.error ?? "Induction failed");
        }
      } catch {
        /* keep polling */
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [pollingJobId, namespaceId, apiClient, navigate]);

  // Derive the induction lock from the server: an in-flight ("inducing") or a
  // completed-but-unreviewed ("pending"/"updated") proposal means a new
  // induction would 409. Recomputed on mount (so a mid-run refresh still shows
  // the lock), after a trigger, and after a 409. Fail-open: on a fetch error
  // we leave the button enabled and let the backend 409 be the source of truth.
  useEffect(() => {
    if (!namespaceId) return;
    let cancelled = false;
    listProposals(apiClient, namespaceId)
      .then((proposals) => {
        if (cancelled) return;
        const locked = proposals.some((p) =>
          proposalBlocksNewInduction(p.status),
        );
        setInductionLocked(locked);
      })
      .catch(() => {
        if (!cancelled) setInductionLocked(false);
      });
    return () => {
      cancelled = true;
    };
  }, [namespaceId, apiClient, lockRefreshKey, pollingJobStatus]);

  async function startInduction() {
    if (!namespaceId) return;
    if (selectedDatasources.length === 0) return;
    setInducting(true);
    setInductionError(null);
    setDuplicateProposalId(null);
    setPollingJobError(undefined);
    setPollingJobStatus("pending");
    setShowInductionModal(false);
    try {
      const dsIds = selectedDatasources.map((o) => o.value!);
      // Grounding applies to BOTH strategies: the structured
      // (table_to_ontology) path grounds tables and the unstructured
      // (lexical graph) path grounds its induced classes against the same
      // loaded foundational ontologies.
      // Combine curated catalog selection (key) + custom-uploaded ontologies
      // (ontology id). The backend resolves both via the same lookup —
      // curated keys map to canonical ontology IDs in the catalog, and
      // custom uploads went into the same ontologies table.
      const groundingIds = [...selectedFoundationalKeys];
      const job = await triggerInduction(apiClient, namespaceId, {
        datasource_ids: dsIds,
        ontology_uri_prefix: `http://coa.amazon.com/namespace/${namespaceId}/ontology/induced#`,
        namespace: namespaceId,
        strategy,
        confidence_threshold: confidenceThreshold,
        grounding_mode: groundingMode,
        grounding_ontology_ids:
          groundingIds.length > 0 ? groundingIds : undefined,
      });
      setActiveTab("proposals");
      setPollingJobId(job.job_id);
      // Re-derive the lock from the server now that a run is in flight.
      setLockRefreshKey((k) => k + 1);
    } catch (e) {
      // A 409 means an induction is already in flight or a prior proposal is
      // still awaiting review — surface a clear message (not the raw
      // "API POST … failed (409): …") and lock the button to match the server.
      if (e instanceof ApiError && e.status === 409) {
        setInductionError(
          "An induction is already in progress or a proposal is awaiting review for this namespace. " +
            "Finish reviewing it before starting a new one.",
        );
        setInductionLocked(true);
        setActiveTab("proposals");
      } else {
        setInductionError((e as Error).message);
      }
      setInducting(false);
    }
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Induce an ontology from your approved sources, review the proposal, then accept it to publish."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              onClick={() =>
                navigate(`/namespaces/${namespaceId}/ontology/graph`)
              }
            >
              Open Explorer
            </Button>
            <Button
              variant="primary"
              onClick={() => setShowInductionModal(true)}
              loading={inducting}
              disabled={inductionLocked}
              disabledReason="An induction is already in progress or a proposal is awaiting review. Finish reviewing it before starting a new one."
            >
              Start induction
            </Button>
          </SpaceBetween>
        }
      >
        Induction
      </Header>
      {staleInducedSourceNames.length > 0 && (
        <Alert
          type="warning"
          header="A re-scanned source has an out-of-date induction"
        >
          <SpaceBetween size="xs">
            <Box variant="p">
              Re-scanned and re-approved since their last induction:{" "}
              <b>{staleInducedSourceNames.join(", ")}</b>. That induction no
              longer matches the current source.
            </Box>
            <Box variant="p">
              Right now, re-inducing only merges into the existing ontology
              (append-only): it won&apos;t remove tables dropped at the source
              and can duplicate edited labels. For a clean rebuild today, delete
              the affected induction below and induce again. Editing an
              induction in place isn&apos;t available yet.
            </Box>
          </SpaceBetween>
        </Alert>
      )}
      {stalePendingSourceNames.length > 0 && (
        <Alert type="warning" header="A pending induction predates a re-scan">
          <SpaceBetween size="xs">
            <Box variant="p">
              You have a pending induction for:{" "}
              <b>{stalePendingSourceNames.join(", ")}</b>. The source was
              re-scanned and re-approved after you triggered that induction.
            </Box>
            <Box variant="p">
              The pending proposal was built on the older source, so accepting
              it won&apos;t include the re-scan&apos;s changes. Re-run the
              induction on the current source, then accept the new proposal.
            </Box>
          </SpaceBetween>
        </Alert>
      )}
      {(pollingJobId || pollingJobError) && (
        <Alert
          type={pollingJobError ? "error" : "info"}
          header={
            pollingJobError ? "Induction failed" : "Induction in progress"
          }
          dismissible={!!pollingJobError}
          onDismiss={() => {
            setPollingJobError(undefined);
            setPollingJobStatus("pending");
          }}
        >
          <SpaceBetween size="s">
            <InductionStages
              status={pollingJobError ? "failed" : pollingJobStatus}
              error={pollingJobError}
            />
            {!pollingJobError && (
              <Box variant="small" color="text-status-inactive">
                You'll be redirected to the new proposal as soon as it's ready.
              </Box>
            )}
          </SpaceBetween>
        </Alert>
      )}
      {inductionError && <Box color="text-status-error">{inductionError}</Box>}
      {duplicateProposalId && (
        <Alert
          type="warning"
          header="Datasource already inducted"
          dismissible
          onDismiss={() => setDuplicateProposalId(null)}
        >
          <SpaceBetween size="xs">
            <Box>
              This selection has the same structure as an existing accepted
              proposal, so no new proposal was created. Open the existing
              proposal to review or edit it.
            </Box>
            <Link
              onFollow={(e) => {
                e.preventDefault();
                navigate(
                  `/namespaces/${namespaceId}/ontology/proposals/${duplicateProposalId}`,
                );
              }}
              href={`/namespaces/${namespaceId}/ontology/proposals/${duplicateProposalId}`}
            >
              View existing proposal
            </Link>
          </SpaceBetween>
        </Alert>
      )}
      {datasourceError && (
        <Alert
          type="warning"
          dismissible
          onDismiss={() => setDatasourceError(null)}
          header="Could not load data sources"
        >
          {datasourceError}
        </Alert>
      )}
      {showInductionModal && (
        <Modal
          visible
          onDismiss={() => setShowInductionModal(false)}
          header="Induce Ontology"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setShowInductionModal(false)}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={startInduction}
                  disabled={selectedDatasources.length === 0 || inductionLocked}
                  disabledReason={
                    inductionLocked
                      ? "An induction is already in progress or a proposal is awaiting review. Finish reviewing it before starting a new one."
                      : undefined
                  }
                >
                  Start induction
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <FormField
              label="Strategy"
              info={
                <Popover
                  header="Induction strategies"
                  size="large"
                  triggerType="custom"
                  content={
                    <SpaceBetween size="xs">
                      <Box variant="p">
                        <strong>Table to Ontology</strong> — Maps each database
                        table to an OWL class and columns to properties. Fast,
                        deterministic, and cost-effective. Best for structured
                        relational schemas where the table/column names are
                        meaningful.
                      </Box>
                      <Box variant="p">
                        <strong>Unstructured (Lexical Graph)</strong> — Extracts
                        concepts and relationships from document sources (PDFs,
                        text) using NLP and builds a lexical knowledge graph.
                        Exclusively for document sources. Supports grounding
                        against loaded ontologies.
                      </Box>
                    </SpaceBetween>
                  }
                >
                  <Link variant="info">Info</Link>
                </Popover>
              }
              description="Determines how the ontology is constructed from your data."
            >
              <Select
                selectedOption={{
                  value: strategy,
                  label:
                    strategy === "unstructured_lexical_graph"
                      ? "Unstructured (Lexical Graph)"
                      : "Table to Ontology",
                }}
                onChange={({ detail }) => {
                  setStrategy(
                    detail.selectedOption.value ?? "table_to_ontology",
                  );
                  setSelectedDatasources([]);
                  setSelectedFoundationalKeys([]);
                }}
                options={[
                  {
                    value: "table_to_ontology",
                    label: "Table to Ontology",
                    description:
                      "Maps relational tables → OWL classes, columns → properties. Best for structured databases.",
                  },
                  {
                    value: "unstructured_lexical_graph",
                    label: "Unstructured (Lexical Graph)",
                    description:
                      "Extracts concepts from document sources via NLP and builds a lexical knowledge graph. Exclusively for document sources.",
                  },
                ]}
              />
            </FormField>
            <FormField
              label="Data sources"
              description={
                strategy === "unstructured_lexical_graph"
                  ? "Select one or more document sources to induce from."
                  : "Select one or more connected database sources to induce from."
              }
            >
              <Multiselect
                selectedOptions={selectedDatasources}
                onChange={({ detail }) =>
                  setSelectedDatasources([...detail.selectedOptions])
                }
                options={
                  strategy === "unstructured_lexical_graph"
                    ? docSourceOptions
                    : datasourceOptions
                }
                placeholder="Select data sources"
                filteringType="auto"
              />
            </FormField>

            {/* Grounding ontologies — available for BOTH strategies. The
                structured path grounds tables and the unstructured (lexical
                graph) path grounds its induced classes against the same
                loaded foundational ontologies. */}
            <FormField
              label="Grounding ontologies (optional)"
              info={
                <Popover
                  header="What is grounding?"
                  size="large"
                  triggerType="custom"
                  content={
                    <SpaceBetween size="xs">
                      <Box variant="p">
                        Grounding aligns your induced ontology against existing
                        well-known ontologies. Instead of creating novel classes
                        for every table or concept, the inducer checks if an
                        equivalent concept already exists in a loaded ontology
                        and reuses it.
                      </Box>
                      <Box variant="p">
                        This improves interoperability — your ontology shares
                        vocabulary with industry standards (Schema.org, FIBO,
                        Dublin Core, etc.) making it easier to integrate with
                        other systems.
                      </Box>
                      <Box variant="p">
                        This run grounds against EXACTLY the ontologies selected
                        here. Your namespace's accepted induced ontologies are
                        pre-selected by default (so new runs converge with prior
                        ones), but you can deselect them. With nothing selected,
                        every table/concept is treated as novel.
                      </Box>
                    </SpaceBetween>
                  }
                >
                  <Link variant="info">Info</Link>
                </Popover>
              }
              description="Pick the ontologies to ground against for this run. Accepted induced ontologies are pre-selected (deselect to skip). Load foundational/uploaded ontologies from the Reference ontologies tab first."
              constraintText="Ontologies still embedding or failed to ingest are shown but not selectable."
            >
              {foundationalError && (
                <Alert
                  type="warning"
                  dismissible
                  onDismiss={() => setFoundationalError(null)}
                >
                  Could not load ontologies: {foundationalError}
                </Alert>
              )}
              <Multiselect
                placeholder={
                  foundationalCatalog === null
                    ? "Loading…"
                    : "Pick loaded ontologies to ground against"
                }
                selectedOptions={selectedFoundationalKeys.map((uri) => {
                  const rec = loadedOntologyRecords.find((r) => r.uri === uri);
                  return {
                    value: uri,
                    label: rec?.title || uri.split("/").pop() || uri,
                  };
                })}
                onChange={({ detail }) =>
                  setSelectedFoundationalKeys(
                    detail.selectedOptions
                      .map((o) => o.value)
                      .filter((v): v is string => Boolean(v)),
                  )
                }
                // All loaded ontologies are SHOWN — induced, foundational, and
                // uploaded. Ready ones (finished embedding) are SELECTABLE; the
                // backend grounds against EXACTLY the selected pool (no
                // server-side auto-include), and ready induced ontologies are
                // pre-checked by default (see the load effect) so new runs
                // converge with prior ones. Ontologies that have not finished
                // embedding or failed to ingest are shown DISABLED with a
                // per-row status label so they are not silently hidden from
                // Start Induction (issue #540). Readiness is derived from
                // ontologyIngestState() — the single source of truth also used
                // by the Ontologies-list StatusIndicator — so the two views can
                // never disagree about whether a row is ready.
                options={loadedOntologyRecords.map((rec) => {
                  const ingestState = ontologyIngestState(rec);
                  const isReady = ingestState === "ready";
                  const statusLabel =
                    ingestState === "failed"
                      ? "⚠️ Ingest failed"
                      : ingestState === "ingesting"
                        ? "⏳ Embedding…"
                        : undefined;
                  return {
                    value: rec.uri,
                    label: rec.title || rec.ontologyId,
                    labelTag:
                      rec.ontologyType === "induced"
                        ? "Induced"
                        : rec.ontologyType === "foundational"
                          ? "Foundational"
                          : "Uploaded",
                    description: statusLabel ?? rec.uri,
                    disabled: !isReady,
                  };
                })}
                disabled={foundationalCatalog === null}
                filteringType="auto"
              />
            </FormField>

            {/* Grounding mode + confidence — shown whenever grounding will run,
                i.e. at least one ontology is selected (induced ontologies are
                pre-selected by default). If the user deselects everything, no
                grounding happens, so the mode controls are hidden. */}
            {selectedFoundationalKeys.length > 0 && (
              <>
                <FormField
                  label="Grounding mode"
                  info={
                    <Popover
                      header="Grounding modes"
                      size="large"
                      triggerType="custom"
                      content={
                        <SpaceBetween size="xs">
                          <Box variant="p">
                            <strong>Enhanced</strong> — Three-stage matching:
                            exact name match → embedding similarity recall → LLM
                            disambiguation for ambiguous candidates. Most
                            accurate but incurs ~1 LLM call per table. Use when
                            precision matters and cost is acceptable.
                          </Box>
                          <Box variant="p">
                            <strong>Standard</strong> — Two-stage matching:
                            exact name match → embedding similarity with a
                            confidence threshold. Fully deterministic, no LLM
                            cost. Use for large schemas where speed and cost are
                            priorities.
                          </Box>
                        </SpaceBetween>
                      }
                    >
                      <Link variant="info">Info</Link>
                    </Popover>
                  }
                  description="How to match source concepts against the selected ontologies."
                >
                  <RadioGroup
                    value={groundingMode}
                    onChange={({ detail }) => setGroundingMode(detail.value)}
                    items={[
                      {
                        value: "ENHANCED",
                        label: "Enhanced (recommended)",
                        description:
                          "Exact-name match + embedding recall + LLM disambiguation. Best accuracy, ~1 LLM call per table.",
                      },
                      {
                        value: "STANDARD",
                        label: "Standard",
                        description:
                          "Exact-name match + embedding recall only. Deterministic, faster, no LLM cost.",
                      },
                    ]}
                  />
                </FormField>
                {groundingMode === "STANDARD" && (
                  <FormField
                    label={`Confidence threshold: ${Math.round(confidenceThreshold * 100)}%`}
                    description="Only concepts above this threshold are auto-matched. Lower = more matches but more false positives."
                  >
                    <Slider
                      value={confidenceThreshold}
                      onChange={({ detail }) =>
                        setConfidenceThreshold(detail.value)
                      }
                      min={0.5}
                      max={0.95}
                      step={0.05}
                    />
                  </FormField>
                )}
              </>
            )}
          </SpaceBetween>
        </Modal>
      )}

      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          {
            id: "proposals",
            label: "Proposals",
            content: (
              <ProposalsTab apiClient={apiClient} namespace={namespaceId} />
            ),
          },
          {
            id: "ontologies",
            label: "Reference ontologies",
            content: (
              <OntologiesTab apiClient={apiClient} namespace={namespaceId} />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}

/** Friendly display metadata for each ontology_type the registry can
 *  return. Induced ontologies come from accepted induction proposals;
 *  foundational ones are curated public ontologies loaded/uploaded into
 *  the namespace; user_created/user_uploaded are custom uploads. Anything
 *  unmapped falls back to its raw type string with a neutral badge. */
/**
 * Ingest lifecycle of a registered (loaded) reference ontology, derived from
 * its ``parse_status`` (async parse/embed worker) with a legacy fallback for
 * rows persisted before ``parse_status`` existed.
 */
export type OntologyIngestState = "ingesting" | "ready" | "failed";

/** Terminal ingest states — polling stops once every registered row is here. */
export const ONTOLOGY_INGEST_TERMINAL: ReadonlySet<OntologyIngestState> =
  new Set(["ready", "failed"]);

/** StatusIndicator presentation per ingest state. */
export const ONTOLOGY_INGEST_STATUS: Record<
  OntologyIngestState,
  { type: "success" | "error" | "in-progress"; label: string }
> = {
  ready: { type: "success", label: "Ready" },
  failed: { type: "error", label: "Parse failed" },
  ingesting: { type: "in-progress", label: "Ingesting" },
};

/** Derive the ingest state of a registered ontology row.
 *
 *  A row that already has embeddings is DONE regardless of parse_status: the
 *  foundational-load path can leave parse_status stuck at "pending" even after
 *  embeddings land (backend append-path bug), so trusting "pending" over a
 *  positive embedding_count would show "Ingesting" forever AND never let the
 *  poll terminate. So: positive embeddings → ready first; then parse_status;
 *  then default to ingesting for a freshly-registered row with nothing yet. */
export function ontologyIngestState(row: OntologyRecord): OntologyIngestState {
  if (row.parseStatus === "parse_error") return "failed";
  if ((row.embeddingCount ?? 0) > 0) return "ready";
  if (row.parseStatus === "ok") return "ready";
  return "ingesting";
}

/** StatusIndicator type + label for a proposal's status, as shown in the
 * Proposals list. ``inducing`` is the trigger-time in-progress stub (issue
 * I-3f1f50cc); ``failed`` is a worker-failed induction; ``embeddings_sync`` is
 * the accept-phase embedding backfill. Exported so it can be unit-tested
 * directly (the full Tabs->table render is flaky under jsdom's fake timers). */
export function proposalStatusDisplay(status: string): {
  type: "success" | "error" | "in-progress" | "pending";
  label: string;
} {
  if (status === "accepted") return { type: "success", label: "accepted" };
  // ``accept_failed``: an accept whose pipeline step exhausted its retries.
  // Surfaced as an error (not pending) so it's visibly distinct from a fresh
  // proposal; ``accept_error`` on the proposal carries the failing step. The
  // proposal remains re-acceptable.
  if (status === "accept_failed")
    return { type: "error", label: "accept failed" };
  if (status === "rejected" || status === "failed")
    return { type: "error", label: status };
  if (
    status === "inducing" ||
    status === "accepting" ||
    status === "embeddings_sync"
  ) {
    const label =
      status === "inducing"
        ? "Inducing…"
        : status === "embeddings_sync"
          ? "Syncing embeddings…"
          : status;
    return { type: "in-progress", label };
  }
  return { type: "pending", label: status };
}

/** What the UI should do when an induction job reaches ``completed``.
 *
 *  - ``duplicate`` — the run was short-circuited by structural-fingerprint
 *    duplicate detection: NO new proposal was created, and ``proposalId`` is the
 *    pre-existing accepted proposal. The UI shows a notice + link (no redirect).
 *  - ``navigate`` — a normal run; ``proposalId`` is the freshly-created proposal
 *    to open. Prefer the report's ``induced_ontology_id`` (authoritative) and
 *    fall back to the polled job id (proposal_id == job_id on the normal path;
 *    resilient when induced_ontology_id isn't hydrated on the DDB-fallback poll).
 *  - ``refresh`` — nothing to navigate to (no id at all); just refresh the list.
 *
 *  Exported as a pure function so the duplicate-vs-navigate decision is unit
 *  tested directly (the 4s poll loop + Cloudscape Tabs render is flaky in jsdom).
 */
export function resolveCompletedInductionAction(
  job: { duplicate_of?: string; report?: Record<string, unknown> },
  polledJobId: string | null,
):
  | { kind: "duplicate" | "navigate"; proposalId: string }
  | { kind: "refresh" } {
  if (job.duplicate_of) {
    return { kind: "duplicate", proposalId: job.duplicate_of };
  }
  const proposalId =
    (job.report?.["induced_ontology_id"] as string | undefined) ??
    polledJobId ??
    null;
  if (proposalId) {
    return { kind: "navigate", proposalId };
  }
  return { kind: "refresh" };
}

/**
 * A catalog row. Curated foundational ontologies not yet loaded into the
 * namespace are surfaced as placeholder rows with `_available: true` so the
 * table can offer a "Load" action for them.
 */
type OntologyRow = ListedOntology & { _available?: boolean };

/** Columns the ontologies table sorts on; every one is an `OntologyRecord` field. */
const ONTOLOGY_SORT_FIELDS: ReadonlySet<string> = new Set([
  "title",
  "ontologyType",
  "classCount",
  "propertyCount",
  "createdAt",
]);

/** Narrows a Cloudscape `sortingField` to a real record key. */
function isOntologySortField(field?: string): field is keyof OntologyRecord {
  return !!field && ONTOLOGY_SORT_FIELDS.has(field);
}

function OntologiesTab({
  apiClient,
  namespace,
}: {
  apiClient: ApiClient;
  namespace?: string;
}) {
  const [items, setItems] = useState<OntologyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [typeFilter, setTypeFilter] = useState("all");
  const [sortingColumn, setSortingColumn] = useState<
    { sortingField: keyof OntologyRecord } | undefined
  >({ sortingField: "title" });
  const [isDescending, setIsDescending] = useState(false);
  // Upload modal state
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploadOntologyId, setUploadOntologyId] = useState("");
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // After a successful upload, ingestion (parse + embed) runs asynchronously;
  // this notice tells the user counts will populate once it finishes.
  const [ingestNotice, setIngestNotice] = useState<string | null>(null);
  // Set (not a single string) so concurrent Loads on different reference
  // ontologies each keep their own spinner — a scalar was overwritten by the
  // latest click, so starting a second load hid the first's spinner even
  // though its background load + poll was still running.
  const [loadingUris, setLoadingUris] = useState<Set<string>>(new Set());
  const [catalogByUri, setCatalogByUri] = useState<
    Map<string, FoundationalOntology>
  >(new Map());
  // Delete-ontology modal state (induced + user-uploaded). Type "delete" to confirm.
  const [ontologyToDelete, setOntologyToDelete] = useState<OntologyRow | null>(
    null,
  );
  const [deleteConfirmText, setDeleteConfirmText] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const navigate = useNavigate();

  const closeDeleteModal = () => {
    setOntologyToDelete(null);
    setDeleteConfirmText("");
    setDeleteError(null);
  };

  const confirmDeleteOntology = async () => {
    if (!namespace || !ontologyToDelete) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteOntology(apiClient, namespace, ontologyToDelete.ontologyId);
      closeDeleteModal();
      setRefreshKey((k) => k + 1);
    } catch (e) {
      // Surface the underlying API error (ApiError.message carries the HTTP
      // status + response body) so a 403 vs 500 vs network failure is
      // distinguishable, not collapsed into one generic string.
      setDeleteError(
        e instanceof Error
          ? `Failed to delete ontology: ${e.message}`
          : "Failed to delete ontology",
      );
    } finally {
      setDeleteBusy(false);
    }
  };

  useEffect(() => {
    if (!namespace) return;
    setLoading(true);
    setError(null);
    Promise.all([
      listOntologies(apiClient, namespace),
      listFoundationalOntologies(apiClient, namespace)
        .then((r) => r.items)
        .catch(() => [] as FoundationalOntology[]),
    ])
      .then(([registered, catalog]) => {
        // Merge: show curated foundationals not yet loaded as placeholder rows
        const loadedUris = new Set(registered.map((r) => r.uri));
        const byUri = new Map(catalog.map((c) => [c.uri, c]));
        setCatalogByUri(byUri);
        const placeholders: OntologyRow[] = catalog
          .filter((c) => !loadedUris.has(c.uri))
          .map((c) => ({
            ontologyId: c.uri,
            uri: c.uri,
            title: c.title,
            description: c.description,
            ontologyType: "foundational",
            format: c.format,
            domainTags: c.domain_tags,
            classCount: 0,
            propertyCount: 0,
            axiomCount: 0,
            embeddingCount: 0,
            createdAt: "",
            updatedAt: "",
            _available: true,
          }));
        setItems([...registered, ...placeholders]);
      })
      .catch((e: Error) => setError(e.message || "Failed to load ontologies"))
      .finally(() => setLoading(false));
  }, [apiClient, namespace, refreshKey]);

  // Poll while any registered (non-placeholder) row is still ingesting so
  // counts + the ingest StatusIndicator flip to their terminal state without a
  // manual refresh. Re-fetching bumps refreshKey (reusing the load effect).
  // Tears down once every registered row is terminal (ready/failed).
  const hasPendingIngest = items.some(
    (r) =>
      !r._available && !ONTOLOGY_INGEST_TERMINAL.has(ontologyIngestState(r)),
  );
  useEffect(() => {
    if (!namespace || !hasPendingIngest) return;
    const id = window.setInterval(() => {
      setRefreshKey((k) => k + 1);
    }, 4000);
    return () => window.clearInterval(id);
  }, [namespace, hasPendingIngest]);

  // Poll while any ontology is mid-delete (status === "deleting") so the card
  // disappears on its own once the backend finishes tearing down the graph +
  // embeddings and removes the registry row. Same pattern as the ingest poll.
  const hasDeletingOntology = items.some((r) => r.status === "deleting");
  useEffect(() => {
    if (!namespace || !hasDeletingOntology) return;
    const id = window.setInterval(() => {
      setRefreshKey((k) => k + 1);
    }, 4000);
    return () => window.clearInterval(id);
  }, [namespace, hasDeletingOntology]);

  // Induced ontologies are surfaced as top-level cards above the list;
  // the list below shows only uploaded + foundational ontologies.
  const inducedOntologies = items.filter(
    (e) => ontologyTypeGroup(e.ontologyType) === "induced",
  );
  const nonInducedItems = items.filter(
    (e) => ontologyTypeGroup(e.ontologyType) !== "induced",
  );
  const foundationalCount = nonInducedItems.filter(
    (e) => ontologyTypeGroup(e.ontologyType) === "foundational",
  ).length;
  const uploadedCount = nonInducedItems.filter(
    (e) => ontologyTypeGroup(e.ontologyType) === "uploaded",
  ).length;
  const filteredItems =
    typeFilter === "all"
      ? nonInducedItems
      : nonInducedItems.filter(
          (e) => ontologyTypeGroup(e.ontologyType) === typeFilter,
        );

  const sortedItems = [...filteredItems].sort((a, b) => {
    if (!sortingColumn) return 0;
    const field = sortingColumn.sortingField;
    const av = a[field] ?? "";
    const bv = b[field] ?? "";
    const cmp =
      typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv));
    return isDescending ? -cmp : cmp;
  });

  async function handleUpload() {
    if (!namespace || !uploadFiles[0] || !uploadOntologyId || !uploadTitle)
      return;
    setUploadBusy(true);
    setUploadError(null);
    try {
      const fmt = inferOntologyFormat(uploadFiles[0].name);
      const uploadedTitle = uploadTitle;
      await uploadOntologyFile(
        apiClient,
        namespace,
        uploadOntologyId,
        uploadTitle,
        uploadFiles[0],
        fmt,
      );
      setShowUploadModal(false);
      setUploadFiles([]);
      setUploadOntologyId("");
      setUploadTitle("");
      setRefreshKey((k) => k + 1);
      setIngestNotice(
        `"${uploadedTitle}" uploaded. Parsing and embedding run in the ` +
          `background — class and embedding counts will appear here once ` +
          `ingestion completes. Refresh in a moment to check progress.`,
      );
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploadBusy(false);
    }
  }

  return (
    <SpaceBetween size="s">
      {inducedOntologies.length > 0 && (
        <Box variant="small" color="text-status-inactive">
          {inducedOntologies.length} induced ontolog
          {inducedOntologies.length === 1 ? "y" : "ies"} in this namespace.{" "}
          <Link
            href={`/namespaces/${namespace}/ontology/graph?tab=ontologies`}
            onFollow={(ev) => {
              ev.preventDefault();
              navigate(
                `/namespaces/${namespace}/ontology/graph?tab=ontologies`,
              );
            }}
          >
            View in Explorer
          </Link>
        </Box>
      )}
      {ingestNotice && (
        <Alert
          type="info"
          dismissible
          onDismiss={() => setIngestNotice(null)}
          header="Ontology uploaded — ingestion in progress"
        >
          {ingestNotice}
        </Alert>
      )}
      {error && (
        <Alert
          type="warning"
          dismissible
          onDismiss={() => setError(null)}
          header="Could not load ontologies"
        >
          {error}
        </Alert>
      )}
      {ontologyToDelete &&
        (() => {
          return (
            <Modal
              visible
              onDismiss={closeDeleteModal}
              header={`Delete ontology "${
                ontologyToDelete.title || ontologyToDelete.ontologyId
              }"?`}
              footer={
                <Box float="right">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={closeDeleteModal}>
                      Cancel
                    </Button>
                    <Button
                      variant="primary"
                      loading={deleteBusy}
                      disabled={deleteBusy || deleteConfirmText !== "delete"}
                      onClick={confirmDeleteOntology}
                    >
                      Delete
                    </Button>
                  </SpaceBetween>
                </Box>
              }
            >
              <SpaceBetween size="m">
                <OntologyDeleteWarning
                  ontologyType={ontologyToDelete.ontologyType ?? ""}
                  name={ontologyToDelete.title || ontologyToDelete.ontologyId}
                />
                {deleteError && (
                  <Alert type="error" header="Delete failed">
                    {deleteError}
                  </Alert>
                )}
                <FormField label="Type delete to confirm">
                  <Input
                    value={deleteConfirmText}
                    onChange={({ detail }) =>
                      setDeleteConfirmText(detail.value)
                    }
                    placeholder="delete"
                    disabled={deleteBusy}
                  />
                </FormField>
              </SpaceBetween>
            </Modal>
          );
        })()}
      <Table
        loading={loading}
        items={sortedItems}
        sortingColumn={sortingColumn}
        sortingDescending={isDescending}
        onSortingChange={({ detail }) => {
          // Cloudscape types `sortingField` as an optional plain string, so it
          // is narrowed rather than asserted; an unrecognised field would sort
          // on `undefined` for every row (see table-trackby.test.ts).
          const field = detail.sortingColumn?.sortingField;
          if (isOntologySortField(field))
            setSortingColumn({ sortingField: field });
          setIsDescending(detail.isDescending ?? false);
        }}
        columnDefinitions={[
          {
            id: "title",
            header: "Title",
            sortingField: "title",
            cell: (e) => (
              <SpaceBetween size="xxxs">
                <Box>{e.title || e.ontologyId}</Box>
                <Box variant="small" color="text-status-inactive">
                  {e.uri}
                </Box>
              </SpaceBetween>
            ),
          },
          {
            id: "type",
            header: "Type",
            sortingField: "ontologyType",
            cell: (e) => {
              const isAvailable = e._available;
              if (isAvailable) {
                return <Badge color="green">Foundational</Badge>;
              }
              const { label, color } = ontologyTypeDisplay(e.ontologyType);
              return <Badge color={color}>{label}</Badge>;
            },
          },
          {
            id: "classes",
            header: "Classes",
            sortingField: "classCount",
            cell: (e) => e.classCount,
          },
          {
            id: "properties",
            header: "Properties",
            sortingField: "propertyCount",
            cell: (e) => e.propertyCount,
          },
          {
            id: "created",
            header: "Created",
            sortingField: "createdAt",
            cell: (e) => formatTimestamp(e.createdAt),
          },
          {
            id: "actions",
            header: "Actions",
            cell: (e) => {
              // Curated-but-not-yet-loaded rows offer a "Load" action; loaded /
              // registered rows show their ingest state inline (no separate
              // Status column). A row can't be both — _available is set only on
              // placeholder catalog entries that have no registry row.
              if (e._available) {
                const entry = catalogByUri.get(e.uri ?? "");
                if (!entry) return null;
                return (
                  <Button
                    variant="inline-link"
                    loading={loadingUris.has(e.uri ?? "")}
                    onClick={async () => {
                      if (!namespace) return;
                      setLoadingUris((s) => new Set(s).add(e.uri ?? ""));
                      try {
                        // POST returns 202 after synchronously creating an
                        // `ingesting` registry row (the ECS task fetches + embeds
                        // in the background). We only await the POST round-trip,
                        // then refresh so that row appears — the table's own
                        // ingest-state poll (`hasPendingIngest` effect) drives it
                        // to ready/failed. No separate button-side poll: it would
                        // duplicate that poll and, being client-local, wouldn't
                        // survive a refresh anyway (the row's "Ingesting" status
                        // does).
                        await loadFoundationalOntology(
                          apiClient,
                          namespace,
                          entry.key,
                        );
                      } catch (err) {
                        setError(
                          `Failed to load ${entry.title}: ${
                            err instanceof Error ? err.message : "unknown error"
                          }`,
                        );
                      } finally {
                        setLoadingUris((s) => {
                          const next = new Set(s);
                          next.delete(e.uri ?? "");
                          return next;
                        });
                        setRefreshKey((k) => k + 1);
                      }
                    }}
                  >
                    Load
                  </Button>
                );
              }
              const { type, label } =
                ONTOLOGY_INGEST_STATUS[ontologyIngestState(e)];
              const statusIndicator = (
                <StatusIndicator type={type}>{label}</StatusIndicator>
              );
              // Every LOADED reference ontology is deletable here — both
              // user-uploaded content and loaded foundational ontologies (the
              // backend teardown is type-agnostic, and a foundational can be
              // re-loaded from the catalog afterwards). Only the not-yet-loaded
              // catalog placeholders (``_available``, handled above) have no
              // registry row to delete and stay Load-only. The confirm modal is
              // type-aware (see closeDeleteModal / the modal body) so deleting a
              // re-loadable foundational reads differently from permanent
              // user/induced data.
              const isDeleting = e.status === "deleting";
              // Mirror the backend guard: a NON-induced ontology can't be deleted
              // while induced ontologies exist (they're grounded against it, so it
              // would leave them dangling — the API returns 409). Disable Delete
              // proactively + explain why, instead of letting the user click into
              // a guaranteed error. Induced ontologies aren't listed here at all
              // (this table is non-induced only) — they delete from their detail
              // page, reached via Explorer → Ontologies.
              const blockedByInduced =
                ontologyTypeGroup(e.ontologyType) !== "induced" &&
                inducedOntologies.length > 0;
              const deleteButton = (
                <Button
                  variant="inline-link"
                  disabled={isDeleting || blockedByInduced}
                  onClick={() => setOntologyToDelete(e)}
                >
                  Delete
                </Button>
              );
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  {isDeleting ? (
                    <StatusIndicator type="in-progress">
                      Delete in progress
                    </StatusIndicator>
                  ) : (
                    statusIndicator
                  )}
                  {blockedByInduced ? (
                    <Popover
                      dismissButton={false}
                      position="top"
                      triggerType="custom"
                      content="Delete the induced ontology first — it's grounded against this reference, so this can't be removed while it exists. Open it from Explorer → Ontologies and use Delete ontology there."
                    >
                      {deleteButton}
                    </Popover>
                  ) : (
                    deleteButton
                  )}
                </SpaceBetween>
              );
            },
          },
        ]}
        empty={
          <Box textAlign="center">
            {typeFilter === "induced"
              ? "No induced ontologies yet. Run an induction and accept its proposal to see one here."
              : typeFilter === "foundational"
                ? "No foundational ontologies loaded yet. Load one from the catalog below."
                : typeFilter === "uploaded"
                  ? "No uploaded ontologies yet. Use Upload your own ontology above."
                  : "No reference ontologies yet. Load one from the catalog, or upload your own."}
          </Box>
        }
        header={
          <Header
            counter={`(${sortedItems.length})`}
            description="Ontologies available for grounding. Load one from the catalog, or upload your own."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <SegmentedControl
                  selectedId={typeFilter}
                  onChange={({ detail }) => setTypeFilter(detail.selectedId)}
                  label="Filter by ontology type"
                  options={[
                    { id: "all", text: `All (${nonInducedItems.length})` },
                    {
                      id: "uploaded",
                      text: `Uploaded (${uploadedCount})`,
                    },
                    {
                      id: "foundational",
                      text: `Foundational (${foundationalCount})`,
                    },
                  ]}
                />
                <Button
                  iconName="refresh"
                  loading={loading}
                  onClick={() => setRefreshKey((k) => k + 1)}
                >
                  Refresh
                </Button>
                <Button
                  iconName="upload"
                  onClick={() => setShowUploadModal(true)}
                >
                  Upload your own ontology
                </Button>
              </SpaceBetween>
            }
          >
            Reference Ontologies
          </Header>
        }
      />
      {showUploadModal && (
        <Modal
          visible
          onDismiss={() => setShowUploadModal(false)}
          header="Upload your own ontology"
          size="medium"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setShowUploadModal(false)}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleUpload}
                  loading={uploadBusy}
                  disabled={
                    uploadFiles.length === 0 ||
                    !uploadOntologyId ||
                    !uploadTitle
                  }
                >
                  Upload
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box variant="small" color="text-status-inactive">
              Upload your own ontology file (Turtle, RDF/XML, or JSON-LD). For a
              curated public ontology (e.g. FIBO, Schema.org), use the{" "}
              <b>Load</b> action on its catalog row instead.
            </Box>
            {uploadError && (
              <Alert
                type="error"
                dismissible
                onDismiss={() => setUploadError(null)}
              >
                {uploadError}
              </Alert>
            )}
            <FormField
              label="Ontology IRI"
              description="Canonical IRI (e.g. http://example.org/myonto/). Used as the ontology_id."
            >
              <Input
                value={uploadOntologyId}
                onChange={({ detail }) => setUploadOntologyId(detail.value)}
                placeholder="https://example.org/my-ontology/"
              />
            </FormField>
            <FormField label="Display title">
              <Input
                value={uploadTitle}
                onChange={({ detail }) => setUploadTitle(detail.value)}
                placeholder="My internal ontology"
              />
            </FormField>
            <FormField
              label="File"
              description="Turtle (.ttl), RDF/XML (.rdf), or JSON-LD (.jsonld)."
            >
              <FileUpload
                value={uploadFiles}
                onChange={({ detail }) => setUploadFiles(detail.value)}
                accept=".ttl,.rdf,.jsonld,.json,application/turtle,application/rdf+xml,application/ld+json"
                multiple={false}
                showFileLastModified
                showFileSize
                i18nStrings={{
                  uploadButtonText: () => "Choose file",
                  dropzoneText: () => "Drop ontology file here",
                  removeFileAriaLabel: () => "Remove file",
                  errorIconAriaLabel: "Error",
                }}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}

function ProposalsTab({
  apiClient,
  namespace,
}: {
  apiClient: ApiClient;
  namespace?: string;
}) {
  const [items, setItems] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    if (!namespace) return;
    setLoading(true);
    listProposals(apiClient, namespace)
      .then(setItems)
      .finally(() => setLoading(false));
  }, [apiClient, namespace, refreshKey]);

  // Poll while any proposal is still being induced so the in-progress stub row
  // (written at induction trigger time, status "inducing") flips to the real
  // proposal ("pending") — or to "failed" — on its own, without a manual
  // Refresh. Same pattern as the OntologiesTab ingest poll. Tears down once no
  // row is inducing.
  const hasInducing = items.some((p) => p.status === "inducing");
  useEffect(() => {
    if (!namespace || !hasInducing) return;
    const id = window.setInterval(() => {
      setRefreshKey((k) => k + 1);
    }, 4000);
    return () => window.clearInterval(id);
  }, [namespace, hasInducing]);

  return (
    <SortableTable
      loading={loading}
      items={items}
      trackBy="proposal_id"
      defaultSortingColumnId="created"
      defaultSortingDescending
      columnDefinitions={[
        {
          id: "id",
          header: "Proposal ID",
          sortingField: "proposal_id",
          // Show the full UUID — it's the proposal's only unique identifier
          // (proposal_id == the induction job_id, minted as uuid4). A proposal
          // spans many tables, so no single label can identify it; truncating
          // the id risked ambiguous prefixes between runs.
          cell: (e) => (
            <Link
              href={`/namespaces/${namespace}/ontology/proposals/${e.proposal_id}`}
              onFollow={(ev) => {
                ev.preventDefault();
                navigate(
                  `/namespaces/${namespace}/ontology/proposals/${e.proposal_id}`,
                );
              }}
            >
              {e.proposal_id}
            </Link>
          ),
        },
        {
          id: "status",
          header: "Status",
          sortingField: "status",
          cell: (e) => {
            const { type, label } = proposalStatusDisplay(e.status);
            return <StatusIndicator type={type}>{label}</StatusIndicator>;
          },
        },
        {
          id: "type",
          header: "Type",
          // STRUCTURED vs UNSTRUCTURED induction. Makes the Scope unit
          // explicit (tables vs classes) and disambiguates same-ontology runs.
          sortingComparator: (a, b) =>
            proposalSourceTypeLabel(a.source_type).localeCompare(
              proposalSourceTypeLabel(b.source_type),
            ),
          cell: (e) => proposalSourceTypeLabel(e.source_type),
        },
        {
          id: "scope",
          header: "Scope",
          // A per-run size that means something for both source types:
          // tables processed for structured runs, classes induced for
          // unstructured. A single label can't disambiguate co-named runs
          // (they often share a generic default); Scope always can.
          // Blank ("—") only for in-flight rows that have no metadata yet.
          sortingComparator: (a, b) =>
            proposalScopeSortKey(a.metadata, a.source_type) -
            proposalScopeSortKey(b.metadata, b.source_type),
          cell: (e) => proposalScope(e.metadata, e.source_type) ?? "—",
        },
        {
          id: "created",
          header: "Created",
          sortingField: "created_at",
          cell: (e) => formatTimestamp(e.created_at),
        },
      ]}
      empty={<Box textAlign="center">No proposals yet.</Box>}
      header={
        <Header
          counter={`(${items.length})`}
          actions={
            <Button
              iconName="refresh"
              loading={loading}
              onClick={() => setRefreshKey((k) => k + 1)}
            >
              Refresh
            </Button>
          }
        >
          Proposals
        </Header>
      }
    />
  );
}
