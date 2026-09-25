// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import Textarea from "@cloudscape-design/components/textarea";

import {
  acceptProposal,
  cancelProposal,
  compileConstraints,
  getProposal,
  fetchTurtleFromUrl,
  fetchJsonFromUrl,
  getProposalUploadUrls,
  putToPresignedUrl,
  getProposalValidationJob,
  inferConstraints,
  getInferConstraintsJob,
  repairProposalDatatypes,
  updateProposal,
  validateProposal,
  type DatatypeOffender,
  type Proposal,
  type ProposalValidationFinding,
  type ProposalValidationJob,
} from "../../../services/ontology-engine";
import { useApiClient } from "@components/ApiClientProvider";
import {
  OntologyGraphFlow,
  type GraphAttribute,
  type GraphEdgeData,
  type GraphEdgeKind,
  type GraphNodeData,
} from "@components/graph/OntologyGraphFlow";
import { turtleToGraph } from "@components/graph/turtleToGraph";
import { ClassDetailCard } from "@components/ontology/ClassDetailCard";
import { useCurrentNamespace } from "@components/NamespaceProvider";
import {
  isAcceptTerminalFailure,
  isProposalAcceptInProgress,
  proposalStatusIndicator,
  resolveConstraintConfig,
} from "./helpers";
import {
  parseConceptMatches,
  parseConceptMatchesEnvelope,
  type ConceptMatch,
} from "./grounding";
import { ConstraintPanel } from "./ConstraintPanel";
import { TurtleHighlight } from "@components/TurtleHighlight";
import { parseTurtleEntities, type TurtleEntity } from "./turtle";
import { ProposalEntitiesPanel } from "./ProposalEntitiesPanel";
import { InfoPopover } from "@components/InfoPopover";
import { ButtonWithHint } from "@components/ButtonWithHint";

/**
 * Whether the proposal has a COMPUTED ontology to show constraints for.
 *
 * The SHACL / infer-constraints (beta) panel is gated on this. It must be
 * hidden ONLY while there is genuinely nothing computed yet:
 *   - ``inducing`` — the in-progress stub written at trigger time (worker still
 *     running; no ontology exists),
 *   - ``failed`` — the induction errored; no ontology was produced.
 *
 * Every other status HAS a computed ontology and should show the panel:
 * ``pending``/``updated`` (reviewable — editing enabled), and
 * ``accepting``/``accepted``/``rejected``/``cancelled`` (viewable read-only —
 * the panel's own ``isReadOnly`` disables editing on terminal statuses). The
 * earlier gate wrongly excluded ``accepted`` et al., hiding constraints that
 * exist and were previously viewable — this restores them.
 */
const _NO_ONTOLOGY_STATUSES = new Set(["inducing", "failed"]);

export function proposalHasComputedOntology(status: string): boolean {
  return !_NO_ONTOLOGY_STATUSES.has(status);
}

/**
 * Whether the proposal disables every edit + accept control on the page.
 *
 * True when the proposal has reached a terminal state (accepted/rejected/
 * cancelled/failed) OR when its ontology has not been produced yet
 * ("inducing" — the worker is still running). "failed" is genuinely terminal;
 * "inducing" is transient but has nothing to act on, so the same disable-all
 * applies. Named for what it gates rather than for the state machine — the
 * state-machine sense of "terminal" stays available for anyone else who needs
 * a stricter predicate.
 */
const _READ_ONLY_STATUSES = new Set([
  "accepted",
  "rejected",
  "cancelled",
  "failed",
  "inducing",
]);

export function proposalIsReadOnly(status: string): boolean {
  return _READ_ONLY_STATUSES.has(status);
}

/**
 * Origin-based node kind for graph color coding, derived from a
 * ``turtleToGraph`` node's ``data``. Grounding is read from the parsed
 * ``data.isGrounded`` flag (set from skos:exactMatch/closeMatch on the compact
 * prefixed subject) — NOT a string scan of the raw Turtle against the absolute
 * node IRI, which never matched the compact ``ind:Class skos:…`` serialization.
 */
export function nodeKindFromData(
  data: Record<string, unknown>,
): GraphNodeData["kind"] {
  if (data.typeIri === "http://www.w3.org/2002/07/owl#ObjectProperty") {
    return "object-property";
  }
  if (data.isGrounded === true) {
    return "grounded";
  }
  return "class";
}

/**
 * Narrow the untyped ``data.attributes`` from turtleToGraph into GraphAttribute[]
 * without a type assertion. Silently drops anything malformed.
 */
export function toGraphAttributes(
  value: unknown,
): GraphAttribute[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: GraphAttribute[] = [];
  for (const item of value) {
    if (item && typeof item === "object" && "label" in item) {
      const label = item.label;
      if (typeof label !== "string") continue;
      const range =
        "range" in item && typeof item.range === "string"
          ? item.range
          : undefined;
      out.push({ label, range });
    }
  }
  return out;
}

export function edgeKindFromData(
  data: Record<string, unknown> | undefined,
): GraphEdgeKind {
  const kind = data?.kind;
  if (kind === "subClassOf" || kind === "suggestedSubClassOf") return kind;
  return "objectProperty";
}

/** A single class entry inside an inferred ConstraintConfig. */
interface InferredClass {
  class_uri: string;
  class_name?: string;
  constraints?: unknown[];
}

/**
 * Narrow the ``unknown`` ``inferred_constraints`` payload from a completed
 * infer-constraints job into its ``classes`` array without ``as`` — the job
 * result is typed ``unknown`` on the wire, so guard the shape at runtime.
 */
function extractInferredClasses(payload: unknown): InferredClass[] {
  if (
    typeof payload !== "object" ||
    payload === null ||
    !("classes" in payload)
  ) {
    return [];
  }
  const { classes } = payload;
  if (!Array.isArray(classes)) return [];
  return classes.filter(
    (c): c is InferredClass =>
      typeof c === "object" &&
      c !== null &&
      "class_uri" in c &&
      typeof c.class_uri === "string",
  );
}

const DATATYPE_FINDING_CODE = "MALFORMED_DATATYPE_TOKENS";

function localName(iri: string): string {
  const hash = iri.lastIndexOf("#");
  const slash = iri.lastIndexOf("/");
  return iri.slice(Math.max(hash, slash) + 1) || iri;
}

/** Narrow a finding's untyped `details.offenders` to DatatypeOffender[] via runtime guards. */
function isDatatypeOffender(item: unknown): item is DatatypeOffender {
  if (typeof item !== "object" || item === null) return false;
  return (
    "subject" in item &&
    typeof item.subject === "string" &&
    "predicate" in item &&
    typeof item.predicate === "string" &&
    "found" in item &&
    typeof item.found === "string" &&
    "canonical" in item &&
    typeof item.canonical === "string"
  );
}

export function extractOffenders(
  finding: ProposalValidationFinding | undefined,
): DatatypeOffender[] {
  const raw = finding?.details?.offenders;
  if (!Array.isArray(raw)) return [];
  return raw.filter(isDatatypeOffender);
}

/** Group offenders by (found → canonical) so the preview reads "N affected", not N near-identical rows. */
interface DatatypeFixGroup {
  found: string;
  canonical: string;
  reason: string;
  affected: number;
}

export function groupOffenders(
  offenders: ReadonlyArray<{ found: string; canonical: string }>,
): DatatypeFixGroup[] {
  const map = new Map<string, DatatypeFixGroup>();
  for (const o of offenders) {
    const key = `${o.found} ${o.canonical}`;
    const existing = map.get(key);
    if (existing) {
      existing.affected += 1;
    } else {
      // Non-standard capitalization vs SQL alias: if the found token differs
      // from its canonical only by case, it's a casing fix; otherwise it's an
      // alias/type substitution (e.g. xsd:varchar → xsd:string).
      const foundLocal = localName(o.found);
      const canonicalLocal = localName(o.canonical);
      const reason =
        foundLocal.toLowerCase() === canonicalLocal.toLowerCase()
          ? "non-standard capitalization"
          : "not a valid XSD datatype name";
      map.set(key, {
        found: o.found,
        canonical: o.canonical,
        reason,
        affected: 1,
      });
    }
  }
  return [...map.values()];
}

function DatatypeRepairModal({
  visible,
  offenders,
  repairing,
  onApply,
  onDismiss,
}: {
  visible: boolean;
  offenders: DatatypeOffender[];
  repairing: boolean;
  onApply: () => void;
  onDismiss: () => void;
}) {
  const groups = useMemo(() => groupOffenders(offenders), [offenders]);
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={`Repair ${offenders.length} datatype issue${offenders.length === 1 ? "" : "s"} across ${groups.length} datatype name${groups.length === 1 ? "" : "s"}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={repairing}>
              Cancel
            </Button>
            <Button variant="primary" onClick={onApply} loading={repairing}>
              Apply repair
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          These datatype names use non-standard spelling. Applying the repair
          rewrites them to the correct XSD datatype in your saved proposal. Your
          queries already work (the served copy is auto-corrected) — repairing
          keeps the saved ontology consistent with what gets served.
        </Box>
        <Table
          variant="embedded"
          columnDefinitions={[
            {
              id: "change",
              header: "Change",
              cell: (g: DatatypeFixGroup) => (
                <span>
                  <code>{localName(g.found)}</code> →{" "}
                  <code>{localName(g.canonical)}</code>
                </span>
              ),
            },
            {
              id: "reason",
              header: "Reason",
              cell: (g: DatatypeFixGroup) => g.reason,
            },
            {
              id: "affected",
              header: "Affected",
              cell: (g: DatatypeFixGroup) =>
                `${g.affected} reference${g.affected === 1 ? "" : "s"}`,
            },
          ]}
          items={groups}
        />
      </SpaceBetween>
    </Modal>
  );
}

function ValidationResults({
  job,
  onRepair,
  repairing,
  repairDisabledReason,
}: {
  job: ProposalValidationJob;
  onRepair?: (offenders: DatatypeOffender[]) => void;
  repairing?: boolean;
  repairDisabledReason?: string;
}) {
  if (job.status === "pending" || job.status === "running") {
    return (
      <Alert type="info" header={`Validation ${job.status}…`}>
        <SpaceBetween direction="horizontal" size="xs">
          <Spinner />
          <Box>Running HermiT reasoner + structural metrics + OoPS!</Box>
        </SpaceBetween>
      </Alert>
    );
  }
  if (job.status === "failed") {
    return (
      <Alert type="error" header="Validation failed" dismissible>
        {job.error ?? "Unknown error"}
      </Alert>
    );
  }
  const report = job.report;
  if (!report) return null;

  const errors = report.findings.filter((f) => f.severity === "error");
  const warnings = report.findings.filter((f) => f.severity === "warning");
  const infos = report.findings.filter((f) => f.severity === "info");

  return (
    <Alert
      type={report.passed ? "success" : "error"}
      header={
        report.passed
          ? "Validation passed"
          : `Validation failed: ${errors.length} error${errors.length === 1 ? "" : "s"}`
      }
    >
      <SpaceBetween size="s">
        <Box>
          <Badge color={report.passed ? "green" : "red"}>
            {errors.length} error · {warnings.length} warning · {infos.length}{" "}
            info
          </Badge>
        </Box>
        {report.metrics && (
          <KeyValuePairs
            columns={4}
            items={[
              {
                label: "Classes",
                value: String(report.metrics.class_count ?? 0),
              },
              {
                label: "Object props",
                value: String(report.metrics.object_property_count ?? 0),
              },
              {
                label: "Datatype props",
                value: String(report.metrics.datatype_property_count ?? 0),
              },
              {
                label: "Max depth",
                value: String(report.metrics.max_depth ?? 0),
              },
            ]}
          />
        )}
        {/* Findings list — scrollable. Errors first, then warnings, then info.
            We keep the full list (no truncation) but cap visible height so a
            proposal with 200 info-level findings doesn't push the page into
            useless scroll-the-world territory. */}
        <div
          style={{
            maxHeight: 320,
            overflowY: "auto",
            paddingRight: 8,
            display: "flex",
            flexDirection: "column",
            gap: 6,
          }}
        >
          {[...errors, ...warnings, ...infos].map((f, i) => {
            const offenders =
              f.code === DATATYPE_FINDING_CODE ? extractOffenders(f) : [];
            const canRepair = offenders.length > 0 && !!onRepair;
            return (
              <Box key={`${f.code}-${i}`}>
                <Badge
                  color={
                    f.severity === "error"
                      ? "red"
                      : f.severity === "warning"
                        ? "severity-medium"
                        : "blue"
                  }
                >
                  {f.severity}
                </Badge>{" "}
                <strong>{f.code}</strong>: {f.message}
                {canRepair && (
                  <Box margin={{ top: "xxs" }}>
                    <Button
                      variant="normal"
                      iconName="edit"
                      loading={repairing}
                      disabled={!!repairDisabledReason}
                      onClick={() => onRepair?.(offenders)}
                    >
                      {`Repair ${offenders.length} datatype issue${offenders.length === 1 ? "" : "s"}`}
                    </Button>
                    {repairDisabledReason && (
                      <Box
                        color="text-status-inactive"
                        fontSize="body-s"
                        display="inline-block"
                        margin={{ left: "xs" }}
                      >
                        {repairDisabledReason}
                      </Box>
                    )}
                  </Box>
                )}
              </Box>
            );
          })}
        </div>
      </SpaceBetween>
    </Alert>
  );
}

function CodeBlock({ children }: { children: string }) {
  // Darker slate background + near-black text — the previous
  // light-grey-on-white combo was effectively unreadable. Matches the
  // contrast level of the new R2RML viewer's pretty-printed Turtle pane.
  return (
    <pre
      style={{
        whiteSpace: "pre-wrap",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 12,
        lineHeight: 1.5,
        background: "#f1f5f9",
        color: "#0f172a",
        padding: 12,
        borderRadius: 4,
        border: "1px solid #e2e8f0",
        overflow: "auto",
        maxHeight: 400,
      }}
    >
      {children}
    </pre>
  );
}

export function ProposalDetailPage() {
  const { namespaceId, proposalId } = useParams<{
    namespaceId: string;
    proposalId: string;
  }>();
  const navigate = useNavigate();
  const apiClient = useApiClient();
  const { current: currentNamespace } = useCurrentNamespace();

  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Inline error for the entity-edit panel. Kept separate from `error` so an
  // apply failure surfaces inside the editor rather than replacing the whole
  // page (the page-level `error` triggers a full-page "Could not load proposal").
  const [editError, setEditError] = useState<string | null>(null);
  // Inline, non-destructive parse error for the live Turtle editor — kept off
  // the page-level `error` so a transiently-invalid Turtle while editing does
  // not replace the whole page with the "Could not load proposal" screen.
  const [parseError, setParseError] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<ProposalValidationJob | null>(
    null,
  );
  const [repairing, setRepairing] = useState(false);
  const [repairOffenders, setRepairOffenders] = useState<
    DatatypeOffender[] | null
  >(null);
  // Accept-time datatype guard: the (found→canonical) changes a dry-run repair
  // found when the user clicked Accept. null = no pending confirm; a non-empty
  // array opens the "you have unrepaired datatype issues" modal, which shows the
  // same grouped preview as the Repair flow so the user can investigate in place.
  const [acceptDatatypeWarning, setAcceptDatatypeWarning] = useState<Array<{
    found: string;
    canonical: string;
  }> | null>(null);
  const [checkingAccept, setCheckingAccept] = useState(false);
  const [shaclValidation, setShaclValidation] =
    useState<ProposalValidationJob | null>(null);
  const [acceptResult, setAcceptResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);
  const [editedTurtle, setEditedTurtle] = useState<string | null>(null);
  const [editedR2rml, setEditedR2rml] = useState<string | null>(null);
  // Parsed grounding matches (one per grounded table). Held in state rather than
  // read inline off ``proposal.metadata.matches`` because the detail endpoint
  // now serves them out-of-band via ``matches_url`` (a presigned S3 GET) to stay
  // under the 6 MB response cap, so they arrive asynchronously in refresh().
  // Legacy proposals still carry them inline; refresh() prefers that when present.
  const [groundingMatches, setGroundingMatches] = useState<ConceptMatch[]>([]);
  const [dirty, setDirty] = useState(false);
  // Staged grounding overrides (table → chosen foundational URI, or ``null``
  // to keep the table novel) that are NOT yet persisted. Picking a candidate /
  // marking novel stages here + marks dirty; sent on the next explicit Save
  // (not auto-committed on every selection). ``null`` clears grounding — the
  // backend ``grounding_overrides`` contract is ``dict[str, str | None]``.
  const [pendingGroundingOverrides, setPendingGroundingOverrides] = useState<
    Record<string, string | null>
  >({});
  // Snapshot of the Turtle as loaded, so save can detect which artifact
  // actually changed. The read path omits inline ``ontology_turtle`` for
  // large proposals, so we cannot compare against ``proposal`` directly.
  const originalTurtleRef = useRef<string | null>(null);
  const originalR2rmlRef = useRef<string | null>(null);
  const [editingEntity, setEditingEntity] = useState<{
    entity: TurtleEntity;
    index: number;
  } | null>(null);
  const [editValue, setEditValue] = useState("");
  const [selectedClass, setSelectedClass] = useState<string | null>(null);
  const [r2rmlEditMode, setR2rmlEditMode] = useState(false);
  const [turtleEditMode, setTurtleEditMode] = useState(false);
  // Draft buffer for the full-Turtle editor: edits accumulate here and are only
  // committed into editedTurtle (+ marked dirty) on Apply — mirroring the
  // class/entity editors. The top-bar "Save changes" then persists.
  const [turtleDraft, setTurtleDraft] = useState<string | null>(null);
  const [shaclShapes, setShaclShapes] = useState("");
  const [inferring, setInferring] = useState(false);
  // Cached compiled SHACL shapes (Turtle) from the most recent Validate/compile
  // in the ConstraintPanel. Held here (not in the panel) so it survives the
  // panel's expand/collapse and so the "Export SHACL" button can download it.
  const [compiledShacl, setCompiledShacl] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!proposalId || !namespaceId) return;
    setError(null);
    setLoading(true);
    getProposal(apiClient, namespaceId, proposalId)
      .then(async (p) => {
        // Prefer inline content (present for normal-size proposals). Large
        // artifacts are omitted from the response to stay under the 6 MB limit,
        // so fall back to fetching each directly from its presigned S3 URL:
        //   - ontology / r2rml Turtle -> ontology_url / r2rml_url
        //   - grounding matches       -> matches_url (JSON envelope)
        //   - constraint config       -> constraints_url (bare config object)
        const inlineMatches = p.metadata?.matches;
        const [ontologyTtl, r2rmlTtl, matches, constraintConfig] =
          await Promise.all([
            p.ontology_turtle != null
              ? Promise.resolve(p.ontology_turtle)
              : p.ontology_url
                ? fetchTurtleFromUrl(p.ontology_url)
                : Promise.resolve(null),
            p.r2rml_turtle != null
              ? Promise.resolve(p.r2rml_turtle)
              : p.r2rml_url
                ? fetchTurtleFromUrl(p.r2rml_url)
                : Promise.resolve(null),
            inlineMatches !== undefined
              ? Promise.resolve(parseConceptMatches(inlineMatches))
              : p.matches_url
                ? fetchJsonFromUrl(p.matches_url)
                    .then(parseConceptMatchesEnvelope)
                    // Grounding matches are secondary review content. A transient
                    // matches fetch/parse failure must NOT reject the whole
                    // Promise.all and blank the page (discarding the ontology
                    // Turtle that loaded fine) — degrade to "no grounding shown".
                    // Retrying refresh re-presigns and recovers.
                    .catch((e: unknown) => {
                      console.warn("Failed to load grounding matches:", e);
                      return [] as ConceptMatch[];
                    })
                : Promise.resolve([]),
            resolveConstraintConfig(p, fetchJsonFromUrl),
          ]);
        // Seed the fetched config back onto metadata rather than holding it in
        // separate state: every consumer (ConstraintPanel, the Infer merge, the
        // save path) already reads and writes metadata.constraint_config, and the
        // detail response no longer carries it inline once it is offloaded.
        setProposal(
          constraintConfig === undefined
            ? p
            : {
                ...p,
                metadata: {
                  ...p.metadata,
                  constraint_config: constraintConfig,
                },
              },
        );
        setEditedTurtle(ontologyTtl);
        setEditedR2rml(r2rmlTtl);
        setGroundingMatches(matches);
        originalTurtleRef.current = ontologyTtl;
        originalR2rmlRef.current = r2rmlTtl;
        setDirty(false);
        setPendingGroundingOverrides({});
        setEditingEntity(null);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [apiClient, proposalId, namespaceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Auto-poll while the proposal is mid-accept so a user who navigates
  // away and back still sees the terminal status without manually
  // refreshing. The interval clears when the proposal leaves the
  // in-progress states (``accepting`` → ``embeddings_sync``) or the page
  // unmounts.
  useEffect(() => {
    if (!isProposalAcceptInProgress(proposal?.status ?? "")) return;
    if (!proposalId || !namespaceId) return;
    const id = window.setInterval(() => {
      getProposal(apiClient, namespaceId, proposalId)
        .then(setProposal)
        .catch(() => {
          /* Transient errors during polling are non-fatal. */
        });
    }, 3000);
    return () => window.clearInterval(id);
  }, [apiClient, proposal?.status, proposalId, namespaceId]);

  // Parsing the (potentially multi-MB) Turtle and building the graph are
  // synchronous and block the main thread for ~1-2s on large ontologies — on
  // top of the S3 fetch in refresh(). Compute them off the render in a deferred
  // effect (setTimeout 0) so the loading placeholder paints first; ``parsing``
  // gates that placeholder. Recompute whenever the (edited) Turtle changes.
  // (Previously ``turtleParse`` was recomputed on every render — unmemoized.)
  const [turtleParse, setTurtleParse] = useState<ReturnType<
    typeof parseTurtleEntities
  > | null>(null);
  const [graphData, setGraphData] = useState<ReturnType<
    typeof turtleToGraph
  > | null>(null);
  const [parsing, setParsing] = useState(false);

  useEffect(() => {
    if (editedTurtle == null) {
      setTurtleParse(null);
      setGraphData(null);
      setParsing(false);
      return;
    }
    setParsing(true);
    const handle = window.setTimeout(() => {
      try {
        setTurtleParse(parseTurtleEntities(editedTurtle));
        setGraphData(turtleToGraph(editedTurtle));
        setParseError(null);
      } catch (e) {
        // A malformed Turtle (common mid-edit) must not strand the spinner or
        // replace the page. Surface it inline via parseError and clear the
        // now-stale parse results; the editor stays intact.
        setParseError(
          e instanceof Error ? e.message : "Failed to parse ontology Turtle",
        );
        setTurtleParse(null);
        setGraphData(null);
      } finally {
        setParsing(false);
      }
    }, 0);
    return () => window.clearTimeout(handle);
  }, [editedTurtle]);

  const flowNodes: GraphNodeData[] = useMemo(() => {
    if (!graphData) return [];
    return graphData.nodes.map((n) => {
      const data = n.data;
      return {
        id: n.id,
        label:
          typeof data.prefLabel === "string"
            ? data.prefLabel
            : (n.id.split(/[#/]/).pop() ?? n.id),
        kind: nodeKindFromData(data),
        uri: n.id,
        attributes: toGraphAttributes(data.attributes),
      };
    });
    // ``graphData`` is derived from ``editedTurtle`` upstream, so it is the only
    // dependency (isGrounded already lives on each node's data).
  }, [graphData]);

  const flowEdges: GraphEdgeData[] = useMemo(() => {
    if (!graphData) return [];
    return graphData.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.label,
      kind: edgeKindFromData(e.data),
    }));
  }, [graphData]);

  // Large proposed ontologies (100+ classes) are unreadable and slow to lay
  // out, so we skip the graph entirely and point users at the table / Turtle.
  const MAX_GRAPH_CLASSES = 100;
  const classCount = flowNodes.filter(
    (n) => n.kind === "class" || n.kind === "grounded",
  ).length;

  function handleEditEntity(entity: TurtleEntity, index: number) {
    setEditingEntity({ entity, index });
    setEditValue(entity.body);
    setEditError(null);
  }

  function applyEdit() {
    if (!editingEntity || !editedTurtle) return;
    const newBody = editValue.trim();
    // Unchanged — nothing to apply; just close the editor.
    if (newBody === editingEntity.entity.body) {
      setEditingEntity(null);
      return;
    }
    const updated = editedTurtle.replace(editingEntity.entity.body, newBody);
    // String.replace is a silent no-op when the original body isn't found
    // verbatim (e.g. the Turtle changed since the entry was opened). Surface
    // that instead of dropping the edit and leaving the user to discover it
    // later, and keep the editor open so the in-progress value isn't lost.
    if (updated === editedTurtle) {
      setEditError(
        `Could not apply this edit: the entry's original text was not found in the Turtle (it may have changed since you opened it). Close and re-open the entry, then try again.`,
      );
      return;
    }
    setEditError(null);
    setEditedTurtle(updated);
    setDirty(true);
    setEditingEntity(null);
  }

  async function saveChanges() {
    if (!proposalId || !namespaceId || !dirty) return;
    setUpdating(true);
    try {
      const ontologyChanged =
        editedTurtle != null && editedTurtle !== originalTurtleRef.current;
      const r2rmlChanged =
        editedR2rml != null && editedR2rml !== originalR2rmlRef.current;
      const groundingChanged =
        Object.keys(pendingGroundingOverrides).length > 0;
      if (!ontologyChanged && !r2rmlChanged && !groundingChanged) {
        setUpdating(false);
        return;
      }
      const fields: {
        ontology_uploaded?: boolean;
        r2rml_uploaded?: boolean;
        grounding_overrides?: Record<string, string | null>;
      } = {};
      // Upload edited Turtle straight to S3 via presigned PUT, bypassing the
      // 6 MB API Gateway / Lambda request limit (mirror of the presigned-GET
      // read path), then commit with the *_uploaded flags rather than sending
      // the (potentially multi-MB) Turtle inline. Only fetch upload URLs when a
      // Turtle artifact actually changed.
      if (ontologyChanged || r2rmlChanged) {
        const urls = await getProposalUploadUrls(
          apiClient,
          namespaceId,
          proposalId,
        );
        if (ontologyChanged && editedTurtle) {
          await putToPresignedUrl(urls.ontology_put_url, editedTurtle);
          fields.ontology_uploaded = true;
        }
        if (r2rmlChanged && editedR2rml) {
          await putToPresignedUrl(urls.r2rml_put_url, editedR2rml);
          fields.r2rml_uploaded = true;
        }
      }
      // Grounding overrides are small; sent inline alongside the upload flags.
      if (groundingChanged) {
        fields.grounding_overrides = pendingGroundingOverrides;
      }
      await updateProposal(apiClient, namespaceId, proposalId, fields);
      setDirty(false);
      setPendingGroundingOverrides({});
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUpdating(false);
    }
  }

  // Stage a chosen grounding candidate for a source table. Memoized so the
  // ProposalEntitiesPanel effect that mounts the split panel doesn't re-run on
  // every render (which would reopen the panel each keystroke). Selections are
  // ignored while a save is in flight — saveChanges → refresh() clears the
  // staged map, so a mid-save selection would be lost.
  const stageGroundingCandidate = useCallback(
    (table: string, uri: string) => {
      if (updating) return;
      setPendingGroundingOverrides((prev) => ({ ...prev, [table]: uri }));
      setDirty(true);
    },
    [updating],
  );
  // Stage "keep novel" (null clears grounding — see grounding_overrides type).
  const stageGroundingNovel = useCallback(
    (table: string) => {
      if (updating) return;
      setPendingGroundingOverrides((prev) => ({ ...prev, [table]: null }));
      setDirty(true);
    },
    [updating],
  );

  async function requestAccept() {
    // Accept-time datatype guard: before accepting, cheaply check (dry-run, no
    // reasoner) whether the proposal still has malformed datatype tokens. If it
    // does, surface a confirm — the tokens will be stored as-is in the ontology
    // and downloads (only the served copy is auto-corrected). If clean, proceed.
    if (!proposalId || !namespaceId) return;
    setCheckingAccept(true);
    try {
      const preview = await repairProposalDatatypes(
        apiClient,
        namespaceId,
        proposalId,
        {
          dryRun: true,
        },
      );
      if (preview.repaired_count > 0) {
        setAcceptDatatypeWarning(preview.changes);
        return; // wait for the user's choice in the confirm modal
      }
    } catch {
      // A guard failure must never block accept — fall through and accept.
    } finally {
      setCheckingAccept(false);
    }
    await doAccept();
  }

  async function repairThenAccept() {
    // "Repair & accept" path from the confirm modal: apply the repair, then accept.
    if (!proposalId || !namespaceId) return;
    setAcceptDatatypeWarning(null);
    setRepairing(true);
    try {
      await repairProposalDatatypes(apiClient, namespaceId, proposalId);
      refresh();
    } catch (e) {
      setAcceptResult({
        ok: false,
        message: `Datatype repair failed: ${(e as Error).message}`,
      });
      return;
    } finally {
      setRepairing(false);
    }
    await doAccept();
  }

  async function doAccept() {
    // POST /accept returns 202 immediately; the worker runs ingestion
    // in the background. We poll the proposal until ``status`` leaves the
    // in-progress states (terminal: ``accepted``, or ``accept_failed`` with
    // ``accept_error`` naming the failed step; legacy: ``pending`` +
    // ``accept_error``). Time out after 5 minutes.
    if (!proposalId || !namespaceId) return;
    setAcceptDatatypeWarning(null);
    setAcceptResult(null);
    setAccepting(true);
    try {
      await acceptProposal(apiClient, namespaceId, proposalId);
      const start = Date.now();
      const POLL_INTERVAL_MS = 2000;
      const POLL_TIMEOUT_MS = 5 * 60 * 1000;
      while (Date.now() - start < POLL_TIMEOUT_MS) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        const fresh = await getProposal(apiClient, namespaceId, proposalId);
        setProposal(fresh);
        if (fresh.status === "accepted") {
          setAcceptResult({
            ok: true,
            message: `Merged into ontology ${fresh.ontology_id}`,
          });
          return;
        }
        // Terminal failure (``accept_failed``, or the legacy ``pending`` +
        // ``accept_error``) — stop polling and surface the reason. The proposal
        // stays re-acceptable, so the Accept button re-enables and the user can
        // retry once the cause is resolved. See isAcceptTerminalFailure.
        if (isAcceptTerminalFailure(fresh)) {
          setAcceptResult({
            ok: false,
            message:
              fresh.accept_error ??
              "Accept failed. Re-accept to retry once the cause is resolved.",
          });
          return;
        }
      }
      setAcceptResult({
        ok: false,
        message:
          "Accept is still in progress after 5 minutes. The ingest may complete in the background — refresh the page to recheck.",
      });
    } catch (e) {
      setAcceptResult({ ok: false, message: (e as Error).message });
    } finally {
      setAccepting(false);
    }
  }

  async function cancel() {
    if (!proposalId || !namespaceId) return;
    setCancelling(true);
    try {
      await cancelProposal(apiClient, namespaceId, proposalId);
      navigate(`/namespaces/${namespaceId}/ontology`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCancelling(false);
    }
  }

  async function validate() {
    if (!proposalId || !namespaceId) return;
    setValidation(null);
    setValidating(true);
    try {
      const initial = await validateProposal(
        apiClient,
        namespaceId,
        proposalId,
        {
          tiers: ["tier1_blocking", "tier2_scoring", "tier3_review"],
          ...(shaclShapes.trim() && { shacl_shapes_turtle: shaclShapes }),
        },
      );
      setValidation(initial);

      // Poll until terminal
      const start = Date.now();
      while (Date.now() - start < 90_000) {
        await new Promise((r) => setTimeout(r, 2000));
        const job = await getProposalValidationJob(
          apiClient,
          namespaceId,
          proposalId,
          initial.job_id,
        );
        setValidation(job);
        if (job.status === "completed" || job.status === "failed") return;
      }
    } catch (e) {
      setValidation({
        job_id: "",
        proposal_id: proposalId,
        status: "failed",
        created_at: new Date().toISOString(),
        error: (e as Error).message,
      });
    } finally {
      setValidating(false);
    }
  }

  async function applyDatatypeRepair() {
    if (!proposalId || !namespaceId) return;
    setRepairing(true);
    try {
      await repairProposalDatatypes(apiClient, namespaceId, proposalId);
      setRepairOffenders(null);
      // Reload the (now-repaired) proposal and re-run validation so the
      // MALFORMED_DATATYPE_TOKENS finding clears.
      refresh();
      await validate();
    } catch (e) {
      setValidation({
        job_id: "",
        proposal_id: proposalId,
        status: "failed",
        created_at: new Date().toISOString(),
        error: `Datatype repair failed: ${(e as Error).message}`,
      });
    } finally {
      setRepairing(false);
    }
  }

  if (loading && !proposal)
    return (
      <Box padding="l" textAlign="center">
        <Spinner size="big" />
      </Box>
    );
  if (error)
    return (
      <Alert
        type="error"
        header="Could not load proposal"
        action={<Button onClick={refresh}>Retry</Button>}
      >
        {error}
      </Alert>
    );
  if (!proposal) return null;

  const md = (proposal.metadata ?? {}) as Record<string, unknown>;
  // Gates every edit + accept control on this page. See proposalIsReadOnly.
  const isReadOnly = proposalIsReadOnly(proposal.status);
  const isAccepting = isProposalAcceptInProgress(proposal.status) || accepting;
  // Show the SHACL / infer-constraints panel whenever the proposal has a
  // computed ontology (see proposalHasComputedOntology) — hidden only for the
  // in-progress "inducing" stub and "failed" runs where nothing exists yet.
  // Accepted/rejected/cancelled still show it read-only (isReadOnly disables
  // editing inside the panel).
  const hasComputedOntology = proposalHasComputedOntology(proposal.status);

  return (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <SpaceBetween size="m">
          <Header
            variant="h1"
            info={
              <InfoPopover
                header="Reviewing a proposal"
                ariaLabel="About proposals"
              >
                <SpaceBetween size="xs">
                  <Box variant="p">
                    A proposal is a draft ontology generated from your data
                    source. It is not live yet.
                  </Box>
                  <Box variant="p">
                    Review and edit the classes, properties, and mappings, then
                    Accept to merge it into this namespace's ontology.
                  </Box>
                </SpaceBetween>
              </InfoPopover>
            }
            actions={
              <SpaceBetween
                direction="horizontal"
                size="xs"
                alignItems="center"
              >
                <Button
                  onClick={() =>
                    navigate(`/namespaces/${namespaceId}/ontology`)
                  }
                >
                  Back
                </Button>
                <Button iconName="refresh" onClick={refresh}>
                  Refresh
                </Button>
                <ButtonWithHint
                  hint="Saves your edits (Turtle, R2RML, grounding choices) to the draft without accepting it."
                  onClick={saveChanges}
                  loading={updating}
                  disabled={!dirty || isReadOnly}
                >
                  Save changes
                </ButtonWithHint>
                <Button
                  onClick={cancel}
                  loading={cancelling}
                  disabled={isReadOnly}
                >
                  Cancel
                </Button>
                <ButtonWithHint
                  hint="Read-only quality checks: reasoner consistency (HermiT), structural metrics, and design pitfalls (OoPS!). Doesn't change the proposal."
                  onClick={validate}
                  loading={validating}
                  disabled={!editedTurtle}
                >
                  Validate
                </ButtonWithHint>
                <ButtonWithHint
                  hint="Merges this draft into the namespace's live ontology (graph, search index, storage). Runs in the background; can't be undone here."
                  variant="primary"
                  onClick={requestAccept}
                  loading={isAccepting || checkingAccept}
                  disabled={isReadOnly || isAccepting || checkingAccept}
                >
                  {proposal.status === "accepted"
                    ? "Already accepted"
                    : isAccepting
                      ? "Accepting…"
                      : "Accept proposal"}
                </ButtonWithHint>
              </SpaceBetween>
            }
          >
            {(md.label as string) || `Proposal ${proposalId?.slice(0, 8)}…`}
          </Header>
          {dirty && (
            <Alert type="info">
              Unsaved edits. Click <strong>Save changes</strong> to upload.
            </Alert>
          )}
          {acceptResult && (
            <Alert
              type={acceptResult.ok ? "success" : "error"}
              header={acceptResult.ok ? "Accepted" : "Failed"}
              dismissible
              onDismiss={() => setAcceptResult(null)}
            >
              <CodeBlock>{acceptResult.message}</CodeBlock>
            </Alert>
          )}

          {/* Survives a page reload: `acceptResult` only exists while this tab
              ran the accept, so a returning user sees the stored error instead.
              Reuses the same predicate the poll loop stops on (it already covers
              accept_failed AND the legacy pending+error shape) so the two can't
              disagree about what counts as a failed accept. */}
          {!acceptResult &&
            isAcceptTerminalFailure(proposal) &&
            proposal.accept_error && (
              <Alert
                type="error"
                header="Previous accept attempt failed"
                dismissible
              >
                <CodeBlock>{proposal.accept_error}</CodeBlock>
              </Alert>
            )}

          {isAccepting && !acceptResult && (
            <Alert
              type="info"
              header={
                proposal.status === "embeddings_sync"
                  ? "Syncing embeddings…"
                  : "Accepting proposal…"
              }
            >
              <SpaceBetween direction="horizontal" size="xs">
                <Spinner />
                <Box>
                  {proposal.status === "embeddings_sync"
                    ? "Waiting for embeddings to become searchable in the vector index."
                    : "Merging into ontology graph (Neptune + AOSS + S3 + embeddings)."}
                </Box>
              </SpaceBetween>
            </Alert>
          )}

          {validation && (
            <ValidationResults
              job={validation}
              onRepair={setRepairOffenders}
              repairing={repairing}
              repairDisabledReason={
                dirty ? "Save or discard your edits first" : undefined
              }
            />
          )}
          {repairOffenders !== null && (
            <DatatypeRepairModal
              visible
              offenders={repairOffenders}
              repairing={repairing}
              onApply={applyDatatypeRepair}
              onDismiss={() => setRepairOffenders(null)}
            />
          )}
          {acceptDatatypeWarning !== null && (
            <Modal
              visible
              onDismiss={() => setAcceptDatatypeWarning(null)}
              header="Accept with unrepaired datatype issues?"
              footer={
                <Box float="right">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      variant="link"
                      onClick={() => setAcceptDatatypeWarning(null)}
                      disabled={repairing || accepting}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={doAccept}
                      disabled={repairing || accepting}
                    >
                      Accept anyway
                    </Button>
                    <Button
                      variant="primary"
                      onClick={repairThenAccept}
                      loading={repairing}
                    >
                      Repair &amp; accept
                    </Button>
                  </SpaceBetween>
                </Box>
              }
            >
              <SpaceBetween size="s">
                <Box>
                  This proposal has {acceptDatatypeWarning.length} datatype
                  reference
                  {acceptDatatypeWarning.length === 1 ? "" : "s"} that use
                  non-standard spelling. If you accept without repairing, they
                  are stored as-is in your ontology and any downloads (queries
                  still work — the served copy is auto-corrected). Repairing
                  first keeps your saved ontology consistent everywhere.
                </Box>
                <Table
                  variant="embedded"
                  columnDefinitions={[
                    {
                      id: "change",
                      header: "Change",
                      cell: (g: DatatypeFixGroup) => (
                        <span>
                          <code>{localName(g.found)}</code> →{" "}
                          <code>{localName(g.canonical)}</code>
                        </span>
                      ),
                    },
                    {
                      id: "reason",
                      header: "Reason",
                      cell: (g: DatatypeFixGroup) => g.reason,
                    },
                    {
                      id: "affected",
                      header: "Affected",
                      cell: (g: DatatypeFixGroup) =>
                        `${g.affected} reference${g.affected === 1 ? "" : "s"}`,
                    },
                  ]}
                  items={groupOffenders(acceptDatatypeWarning)}
                />
              </SpaceBetween>
            </Modal>
          )}

          <ExpandableSection
            variant="container"
            defaultExpanded
            headerText="Summary"
          >
            <KeyValuePairs
              columns={3}
              items={[
                {
                  label: "Proposal ID",
                  value: <Box variant="code">{proposal.proposal_id}</Box>,
                },
                {
                  label: (
                    <SpaceBetween
                      direction="horizontal"
                      size="xxs"
                      alignItems="center"
                    >
                      <span>Status</span>
                      <InfoPopover
                        header="Proposal status"
                        ariaLabel="About proposal status"
                      >
                        <SpaceBetween size="xs">
                          <Box variant="p">
                            <strong>Pending</strong>: ready to review and edit.
                          </Box>
                          <Box variant="p">
                            <strong>Accepting / Syncing embeddings</strong>:
                            merging into the graph and search index (a few
                            minutes).
                          </Box>
                          <Box variant="p">
                            <strong>Accepted</strong>: now part of the live
                            ontology.
                          </Box>
                          <Box variant="p">
                            <strong>Updated</strong>: edits saved, not yet
                            accepted.
                          </Box>
                          <Box variant="p">
                            <strong>Rejected / Cancelled</strong>: the draft was
                            discarded.
                          </Box>
                        </SpaceBetween>
                      </InfoPopover>
                    </SpaceBetween>
                  ),
                  value: proposalStatusIndicator(proposal.status),
                },
                {
                  label: "Type",
                  value:
                    proposal.source_type === "UNSTRUCTURED"
                      ? "Unstructured Induction"
                      : "Structured Induction",
                },
                {
                  label: "Namespace",
                  value:
                    currentNamespace?.displayName ??
                    currentNamespace?.name ??
                    proposal.namespace,
                },
                { label: "Created", value: proposal.created_at ?? "—" },
                {
                  label: "Datasource(s)",
                  value:
                    proposal.source_type === "UNSTRUCTURED"
                      ? "Lexical Graph"
                      : ((md.datasource_ids as string[]) ?? []).join(", ") ||
                        "—",
                },
                ...(proposal.source_type !== "UNSTRUCTURED"
                  ? [
                      {
                        label: "Tables / columns",
                        value: `${md.tables_processed ?? "?"} / ${md.columns_processed ?? "?"}`,
                      },
                    ]
                  : []),
                {
                  label:
                    proposal.source_type === "UNSTRUCTURED" ? (
                      "Classes"
                    ) : (
                      <SpaceBetween
                        direction="horizontal"
                        size="xxs"
                        alignItems="center"
                      >
                        <span>Novel classes</span>
                        <InfoPopover
                          header="Novel vs grounded"
                          ariaLabel="About novel classes"
                        >
                          <SpaceBetween size="xs">
                            <Box variant="p">
                              New concepts created because they didn't match any
                              grounding target.
                            </Box>
                            <Box variant="p">
                              Classes that did match are reused (grounded) and
                              not counted here. The graph colors grounded and
                              novel classes differently.
                            </Box>
                          </SpaceBetween>
                        </InfoPopover>
                      </SpaceBetween>
                    ),
                  value: String(
                    md.novel_classes_created ?? md.class_count ?? 0,
                  ),
                },
              ]}
            />
          </ExpandableSection>

          {(loading || parsing) && (
            <Container>
              <Box
                padding="xxl"
                textAlign="center"
                color="text-status-inactive"
              >
                <SpaceBetween size="s">
                  <Spinner size="big" />
                  <span>
                    {loading
                      ? "Loading ontology from storage…"
                      : "Rendering ontology…"}
                  </span>
                </SpaceBetween>
              </Box>
            </Container>
          )}

          {parseError && (
            <Alert
              type="warning"
              dismissible
              onDismiss={() => setParseError(null)}
              header="Ontology Turtle isn't valid"
            >
              {parseError} The class list and graph won't update until the
              Turtle parses cleanly.
            </Alert>
          )}
          {flowNodes.length > 0 && (
            <ExpandableSection
              variant="container"
              defaultExpanded
              headerText="Proposal graph"
              headerDescription={
                classCount > MAX_GRAPH_CLASSES
                  ? undefined
                  : "Each cluster is a group of related classes; dashed purple arrows show subclass relationships. Click a class to inspect it, zoom in to read relationship names."
              }
            >
              {classCount > MAX_GRAPH_CLASSES ? (
                <Alert type="info">
                  This proposal has {classCount} classes — too large to render
                  as a graph (hidden above {MAX_GRAPH_CLASSES} for performance).
                  Use the table and Turtle editor below to review and edit.
                </Alert>
              ) : (
                <div style={{ height: 560, position: "relative" }}>
                  <OntologyGraphFlow
                    nodes={flowNodes}
                    edges={flowEdges}
                    layout="force"
                    onNodeSelect={(d) => setSelectedClass(d?.id ?? null)}
                  />
                </div>
              )}
            </ExpandableSection>
          )}

          {/* Foundational grounding is no longer a standalone card — it is
              folded into each class-card's Matches tab (Item 1). The
              ConceptMatch records + staged overrides thread down through
              ProposalEntitiesPanel into ClassDetailPanel. */}
          {updating && groundingMatches.length > 0 && (
            <Box variant="small" color="text-status-info">
              Rebuilding ontology with updated grounding...
            </Box>
          )}

          {turtleParse && (
            <ProposalEntitiesPanel
              entities={turtleParse.entities}
              prelude={turtleParse.prelude}
              fullTurtle={editedTurtle}
              r2rmlTurtle={editedR2rml}
              isReadOnly={isReadOnly}
              onEditEntity={handleEditEntity}
              groundingMatches={groundingMatches}
              pendingGroundingOverrides={pendingGroundingOverrides}
              onSelectGroundingCandidate={stageGroundingCandidate}
              onMarkGroundingNovel={stageGroundingNovel}
              onUpdateEntityTurtle={(entity, updatedBody) => {
                if (!editedTurtle) return;
                const updated = editedTurtle.replace(entity.body, updatedBody);
                setEditedTurtle(updated);
                setDirty(true);
              }}
              onUpdateTurtle={(updatedTurtle) => {
                setEditedTurtle(updatedTurtle);
                setDirty(true);
              }}
            />
          )}

          {editedR2rml && (
            <ExpandableSection
              variant="container"
              headerText="R2RML Mapping"
              headerInfo={
                <InfoPopover
                  header="R2RML mapping"
                  ariaLabel="About R2RML mapping"
                >
                  <Box variant="p">
                    Maps each class and property back to its SQL table and
                    columns, so questions about this ontology can be answered by
                    querying the source database.
                  </Box>
                </InfoPopover>
              }
              defaultExpanded={false}
              headerActions={
                !isReadOnly && (
                  <Button
                    variant="inline-link"
                    onClick={() => setR2rmlEditMode((m) => !m)}
                  >
                    {r2rmlEditMode ? "View" : "Edit"}
                  </Button>
                )
              }
            >
              {r2rmlEditMode ? (
                <Textarea
                  value={editedR2rml}
                  onChange={({ detail }) => {
                    setEditedR2rml(detail.value);
                    setDirty(true);
                  }}
                  rows={20}
                  disabled={isReadOnly}
                  ariaLabel="R2RML Turtle"
                />
              ) : (
                <TurtleHighlight>{editedR2rml}</TurtleHighlight>
              )}
            </ExpandableSection>
          )}

          {editedTurtle && (
            <ExpandableSection
              variant="container"
              headerText="Ontology Source (Turtle)"
              defaultExpanded={false}
              headerActions={
                !isReadOnly && (
                  <Button
                    variant="inline-link"
                    onClick={() => {
                      if (turtleEditMode) {
                        setTurtleEditMode(false);
                        setTurtleDraft(null);
                      } else {
                        setTurtleDraft(editedTurtle);
                        setTurtleEditMode(true);
                      }
                    }}
                  >
                    {turtleEditMode ? "View" : "Edit"}
                  </Button>
                )
              }
            >
              {turtleEditMode ? (
                <SpaceBetween size="s">
                  <Textarea
                    value={turtleDraft ?? ""}
                    onChange={({ detail }) => setTurtleDraft(detail.value)}
                    rows={25}
                    disabled={isReadOnly}
                    ariaLabel="Ontology Turtle"
                  />
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      onClick={() => {
                        setTurtleEditMode(false);
                        setTurtleDraft(null);
                      }}
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="primary"
                      disabled={isReadOnly || turtleDraft === editedTurtle}
                      onClick={() => {
                        if (turtleDraft != null) {
                          setEditedTurtle(turtleDraft);
                          setDirty(true);
                        }
                        setTurtleEditMode(false);
                        setTurtleDraft(null);
                      }}
                    >
                      Apply
                    </Button>
                  </SpaceBetween>
                </SpaceBetween>
              ) : (
                <TurtleHighlight>{editedTurtle}</TurtleHighlight>
              )}
            </ExpandableSection>
          )}

          {/* SHACL / infer-constraints (beta): shown whenever a computed
              ontology exists. Hidden ONLY for the in-progress "inducing" stub
              and "failed" runs (nothing to show yet). Accepted/rejected/
              cancelled show it read-only — isReadOnly disables editing. */}
          {hasComputedOntology && (
            <ConstraintPanel
              isReadOnly={isReadOnly}
              config={md?.constraint_config as never}
              onConfigChange={(updated) => {
                if (!proposal) return;
                setProposal({
                  ...proposal,
                  metadata: { ...md, constraint_config: updated as never },
                });
              }}
              onInfer={async () => {
                if (!proposalId || !namespaceId) return;
                setInferring(true);
                try {
                  // POST returns 202 + a job; the Bedrock inference runs in the
                  // background. Poll the job until terminal (mirrors onValidate:
                  // 2s interval, 30s cap) then apply the inferred constraints.
                  const initial = await inferConstraints(
                    apiClient,
                    namespaceId,
                    proposalId,
                  );
                  let job = initial;
                  const start = Date.now();
                  while (
                    job.status !== "completed" &&
                    job.status !== "failed" &&
                    Date.now() - start < 30_000
                  ) {
                    await new Promise((r) => setTimeout(r, 2000));
                    job = await getInferConstraintsJob(
                      apiClient,
                      namespaceId,
                      proposalId,
                      initial.job_id,
                    );
                  }
                  if (job.status === "failed") {
                    throw new Error(job.error || "Constraint inference failed");
                  }
                  if (job.status !== "completed") {
                    throw new Error(
                      "Constraint inference timed out. Try again.",
                    );
                  }
                  const inferred = extractInferredClasses(
                    job.result?.inferred_constraints,
                  );
                  const existing = (md?.constraint_config as {
                    classes?: Array<{ class_uri?: string }>;
                  }) || { classes: [] };
                  const existingClasses = existing.classes ?? [];
                  const seen = new Set(existingClasses.map((c) => c.class_uri));
                  // Skip inferred classes we already have so re-running Infer
                  // doesn't append duplicate class entries.
                  const newClasses = inferred.filter(
                    (c) => !seen.has(c.class_uri),
                  );
                  setProposal({
                    ...proposal!,
                    metadata: {
                      ...md,
                      constraint_config: {
                        classes: [
                          ...(existingClasses as never[]),
                          ...(newClasses as never[]),
                        ],
                      },
                    },
                  });
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setInferring(false);
                }
              }}
              onValidate={async (config, custom) => {
                if (!proposalId || !namespaceId) return;
                setValidating(true);
                try {
                  const compiled = await compileConstraints(
                    apiClient,
                    namespaceId,
                    proposalId,
                    config,
                    custom || undefined,
                  );
                  setCompiledShacl(compiled.shapes_turtle);
                  const initial = await validateProposal(
                    apiClient,
                    namespaceId,
                    proposalId,
                    {
                      tiers: ["tier1_blocking"],
                      shacl_shapes_turtle: compiled.shapes_turtle,
                    },
                  );
                  setShaclValidation(initial);
                  // Poll until terminal
                  const start = Date.now();
                  const jobId = initial.job_id;
                  while (Date.now() - start < 30_000) {
                    await new Promise((r) => setTimeout(r, 2000));
                    const job = await getProposalValidationJob(
                      apiClient,
                      namespaceId,
                      proposalId,
                      jobId,
                    );
                    setShaclValidation(job);
                    if (job.status === "completed" || job.status === "failed")
                      break;
                  }
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setValidating(false);
                }
              }}
              inferring={inferring}
              validating={validating}
              validationResult={
                shaclValidation?.status === "completed"
                  ? {
                      status: "completed",
                      passed: shaclValidation.report?.passed,
                      findings: shaclValidation.report?.findings,
                    }
                  : null
              }
              onDismissValidation={() => setShaclValidation(null)}
              customTurtle={shaclShapes}
              onCustomTurtleChange={setShaclShapes}
              compiledShapes={compiledShacl}
              proposalName={
                typeof md.label === "string" && md.label ? md.label : undefined
              }
            />
          )}
        </SpaceBetween>
      </div>

      {/* Right edit panel */}
      {editingEntity && (
        <div
          style={{
            width: 420,
            flexShrink: 0,
            overflowY: "auto",
            borderLeft:
              "1px solid var(--color-border-divider-default, #e9ebed)",
            paddingLeft: 16,
          }}
        >
          <Container
            header={
              <Header variant="h3">Edit: {editingEntity.entity.subject}</Header>
            }
          >
            <SpaceBetween size="m">
              {editError && (
                <Alert
                  type="error"
                  dismissible
                  onDismiss={() => setEditError(null)}
                >
                  {editError}
                </Alert>
              )}
              <Box variant="small" color="text-status-inactive">
                {editingEntity.entity.kind}
              </Box>
              <Textarea
                value={editValue}
                onChange={({ detail }) => setEditValue(detail.value)}
                rows={20}
                ariaLabel={`Edit Turtle for ${editingEntity.entity.subject}`}
              />
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setEditingEntity(null)}>Cancel</Button>
                <Button variant="primary" onClick={applyEdit}>
                  Apply
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        </div>
      )}

      {!editingEntity && selectedClass && editedTurtle && (
        <div
          style={{
            width: 420,
            flexShrink: 0,
            overflowY: "auto",
            borderLeft:
              "1px solid var(--color-border-divider-default, #e9ebed)",
            paddingLeft: 16,
          }}
        >
          <ClassDetailCard
            iri={selectedClass}
            turtle={editedTurtle}
            onSelectClass={setSelectedClass}
            onTurtleEdit={(t) => {
              setEditedTurtle(t);
              setDirty(true);
            }}
            onClose={() => setSelectedClass(null)}
          />
        </div>
      )}
    </div>
  );
}
