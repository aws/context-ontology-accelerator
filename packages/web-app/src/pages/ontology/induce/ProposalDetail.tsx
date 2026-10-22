// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
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
import { ApiError, useApiClient } from "@components/ApiClientProvider";
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
import { useAppI18n } from "../../../i18n/LocaleProvider";

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

/**
 * Turn a failed save into a message that says whether to retry or to fix input.
 *
 * The update route distinguishes its failures deliberately: a 503 means the
 * attempt was abandoned before any write and is safe to repeat, while a 409 or
 * 400 means the request itself was refused and repeating it unchanged will fail
 * the same way. Rendering only `error.message` collapsed that distinction, so a
 * user who hit a transient DynamoDB throttle was told to fix input that was
 * never wrong. The server's own sentence is always kept — for grounding
 * overrides it names which class aborted the save.
 */
export function describeSaveFailure(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 503:
        return `${error.message} Nothing was saved, so you can retry this save as-is.`;
      case 409:
        return `The server refused this save: ${error.message} Retrying unchanged will fail the same way.`;
      case 400:
        return `This save was rejected as malformed: ${error.message}`;
      // 401/403 messages are already written for the reader, and a 5xx other
      // than 503 carries no retry promise, so neither gets a prefix.
      default:
        return error.message;
    }
  }
  // Local guards (the mixed grounding/Turtle check) and presigned-PUT failures
  // throw ordinary Errors whose messages are already the whole story.
  return error instanceof Error
    ? error.message
    : "Failed to save proposal changes";
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
  const { t } = useAppI18n();
  const groups = useMemo(() => groupOffenders(offenders), [offenders]);
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={t(
        offenders.length === 1
          ? groups.length === 1
            ? "Repair {issues} datatype issue across {names} datatype name"
            : "Repair {issues} datatype issue across {names} datatype names"
          : groups.length === 1
            ? "Repair {issues} datatype issues across {names} datatype name"
            : "Repair {issues} datatype issues across {names} datatype names",
        { issues: offenders.length, names: groups.length },
      )}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={repairing}>
              {t("Cancel")}
            </Button>
            <Button variant="primary" onClick={onApply} loading={repairing}>
              {t("Apply repair")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          {t(
            "These datatype names use non-standard spelling. Applying the repair rewrites them to the correct XSD datatype in your saved proposal. Your queries already work (the served copy is auto-corrected) — repairing keeps the saved ontology consistent with what gets served.",
          )}
        </Box>
        <Table
          variant="embedded"
          columnDefinitions={[
            {
              id: "change",
              header: t("Change"),
              cell: (g: DatatypeFixGroup) => (
                <span>
                  <code>{localName(g.found)}</code> →{" "}
                  <code>{localName(g.canonical)}</code>
                </span>
              ),
            },
            {
              id: "reason",
              header: t("Reason"),
              cell: (g: DatatypeFixGroup) =>
                g.reason === "non-standard capitalization"
                  ? t("non-standard capitalization")
                  : t("not a valid XSD datatype name"),
            },
            {
              id: "affected",
              header: t("Affected"),
              cell: (g: DatatypeFixGroup) =>
                t(
                  g.affected === 1 ? "{count} reference" : "{count} references",
                  { count: g.affected },
                ),
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
  const { t } = useAppI18n();
  if (job.status === "pending" || job.status === "running") {
    return (
      <Alert
        type="info"
        header={t("Validation {status}…", {
          status: job.status,
        })}
      >
        <SpaceBetween direction="horizontal" size="xs">
          <Spinner />
          <Box>
            {t(
              "Running HermiT reasoner + structural metrics + OoPS! (if enabled)",
            )}
          </Box>
        </SpaceBetween>
      </Alert>
    );
  }
  if (job.status === "failed") {
    return (
      <Alert type="error" header={t("Validation failed")} dismissible>
        {job.error ?? t("Unknown error")}
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
          ? t("Validation passed")
          : t(
              errors.length === 1
                ? "Validation failed: {count} error"
                : "Validation failed: {count} errors",
              { count: errors.length },
            )
      }
    >
      <SpaceBetween size="s">
        <Box>
          <Badge color={report.passed ? "green" : "red"}>
            {t("{errors} error · {warnings} warning · {infos} info", {
              errors: errors.length,
              warnings: warnings.length,
              infos: infos.length,
            })}
          </Badge>
        </Box>
        {report.metrics && (
          <KeyValuePairs
            columns={4}
            items={[
              {
                label: t("Classes"),
                value: String(report.metrics.class_count ?? 0),
              },
              {
                label: t("Object props"),
                value: String(report.metrics.object_property_count ?? 0),
              },
              {
                label: t("Datatype props"),
                value: String(report.metrics.datatype_property_count ?? 0),
              },
              {
                label: t("Max depth"),
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
                      {t(
                        offenders.length === 1
                          ? "Repair {count} datatype issue"
                          : "Repair {count} datatype issues",
                        { count: offenders.length },
                      )}
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
  const { t } = useAppI18n();
  const { namespaceId, proposalId } = useParams<{
    namespaceId: string;
    proposalId: string;
  }>();
  const navigate = useNavigate();
  const apiClient = useApiClient();
  const { current: currentNamespace } = useCurrentNamespace();

  const { name: nsName } = useNamespace(namespaceId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    { text: t("Ontology") },
    {
      text: t("Induction"),
      href: `/namespaces/${namespaceId}/ontology`,
      namespaceId,
    },
    { text: proposalId ?? "" },
  ]);

  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Save failures are recoverable editing errors. Keep them separate from the
  // initial-load error, which replaces the page with a retry screen.
  const [saveError, setSaveError] = useState<string | null>(null);
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
  // Staged grounding overrides (stable table identity, with a bare-table
  // fallback for legacy matches → chosen foundational URI, or ``null`` to keep
  // the table novel) that are NOT yet persisted. Picking a candidate / marking
  // novel stages here + marks dirty; sent on the next explicit Save.
  // The wire contract splits candidate URIs into ``grounding_overrides`` and
  // clears into ``novel_tables`` so generated OpenAPI clients never need null
  // map values. The staged UI state keeps the ergonomic nullable form.
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
    setSaveError(null);
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
          e instanceof Error ? e.message : t("Failed to parse ontology Turtle"),
        );
        setTurtleParse(null);
        setGraphData(null);
      } finally {
        setParsing(false);
      }
    }, 0);
    return () => window.clearTimeout(handle);
  }, [editedTurtle, t]);

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
        t(
          "Could not apply this edit: the entry's original text was not found in the Turtle (it may have changed since you opened it). Close and re-open the entry, then try again.",
        ),
      );
      return;
    }
    setEditError(null);
    setEditedTurtle(updated);
    setDirty(true);
    setEditingEntity(null);
  }

  const ontologyChanged =
    editedTurtle != null && editedTurtle !== originalTurtleRef.current;
  const r2rmlChanged =
    editedR2rml != null && editedR2rml !== originalR2rmlRef.current;
  const groundingChanged = Object.keys(pendingGroundingOverrides).length > 0;
  const hasMixedEdits = groundingChanged && (ontologyChanged || r2rmlChanged);

  function discardGroundingChanges() {
    setPendingGroundingOverrides({});
    setDirty(ontologyChanged || r2rmlChanged);
    setSaveError(null);
  }

  function discardTurtleChanges() {
    setEditedTurtle(originalTurtleRef.current);
    setEditedR2rml(originalR2rmlRef.current);
    setTurtleDraft(originalTurtleRef.current);
    setTurtleEditMode(false);
    setR2rmlEditMode(false);
    setEditingEntity(null);
    setEditError(null);
    setParseError(null);
    setDirty(groundingChanged);
    setSaveError(null);
  }

  async function saveChanges() {
    if (!proposalId || !namespaceId || !dirty) return;
    setSaveError(null);
    setUpdating(true);
    try {
      if (!ontologyChanged && !r2rmlChanged && !groundingChanged) {
        setUpdating(false);
        return;
      }
      if (groundingChanged && (ontologyChanged || r2rmlChanged)) {
        throw new Error(
          "Grounding overrides and Turtle edits must be saved separately",
        );
      }
      const fields: {
        ontology_uploaded?: boolean;
        r2rml_uploaded?: boolean;
        grounding_overrides?: Record<string, string>;
        novel_tables?: string[];
        upload_revision_id?: string;
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
        fields.upload_revision_id = urls.revisionId;
        if (ontologyChanged && editedTurtle) {
          await putToPresignedUrl(urls.ontologyPutUrl, editedTurtle);
          fields.ontology_uploaded = true;
        }
        if (r2rmlChanged && editedR2rml) {
          await putToPresignedUrl(urls.r2rmlPutUrl, editedR2rml);
          fields.r2rml_uploaded = true;
        }
      }
      // Grounding overrides are small and are sent inline only when no Turtle
      // artifact changed; the server rejects mixed requests as a second guard.
      if (groundingChanged) {
        const groundingOverrides: Record<string, string> = {};
        const novelTables: string[] = [];
        for (const [table, uri] of Object.entries(pendingGroundingOverrides)) {
          if (uri === null) {
            novelTables.push(table);
          } else {
            groundingOverrides[table] = uri;
          }
        }
        if (Object.keys(groundingOverrides).length > 0) {
          fields.grounding_overrides = groundingOverrides;
        }
        if (novelTables.length > 0) {
          fields.novel_tables = novelTables;
        }
      }
      await updateProposal(apiClient, namespaceId, proposalId, fields);
      setDirty(false);
      setPendingGroundingOverrides({});
      refresh();
    } catch (error: unknown) {
      setSaveError(describeSaveFailure(error));
    } finally {
      setUpdating(false);
    }
  }

  // Stage a chosen grounding candidate for a stable match key. Memoized so the
  // ProposalEntitiesPanel effect that mounts the split panel doesn't re-run on
  // every render (which would reopen the panel each keystroke). Selections are
  // ignored while a save is in flight — saveChanges → refresh() clears the
  // staged map, so a mid-save selection would be lost.
  const stageGroundingCandidate = useCallback(
    (matchKey: string, uri: string) => {
      if (updating) return;
      setPendingGroundingOverrides((prev) => ({ ...prev, [matchKey]: uri }));
      setDirty(true);
      setSaveError(null);
    },
    [updating],
  );
  // Stage "keep novel" (serialized as novel_tables by saveChanges).
  const stageGroundingNovel = useCallback(
    (matchKey: string) => {
      if (updating) return;
      setPendingGroundingOverrides((prev) => ({ ...prev, [matchKey]: null }));
      setDirty(true);
      setSaveError(null);
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
        message: t("Datatype repair failed: {error}", {
          error: (e as Error).message,
        }),
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
            message: t("Merged into ontology {id}", {
              id: fresh.ontology_id,
            }),
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
              t(
                "Accept failed. Re-accept to retry once the cause is resolved.",
              ),
          });
          return;
        }
      }
      setAcceptResult({
        ok: false,
        message: t(
          "Accept is still in progress after 5 minutes. The ingest may complete in the background — refresh the page to recheck.",
        ),
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
        error: t("Datatype repair failed: {error}", {
          error: (e as Error).message,
        }),
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
        header={t("Could not load proposal")}
        action={<Button onClick={refresh}>{t("Retry")}</Button>}
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
                header={t("Reviewing a proposal")}
                ariaLabel={t("About proposals")}
              >
                <SpaceBetween size="xs">
                  <Box variant="p">
                    {t(
                      "A proposal is a draft ontology generated from your data source. It is not live yet.",
                    )}
                  </Box>
                  <Box variant="p">
                    {t(
                      "Review and edit the classes, properties, and mappings, then Accept to merge it into this namespace's ontology.",
                    )}
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
                  {t("Back")}
                </Button>
                <Button iconName="refresh" onClick={refresh}>
                  {t("Refresh")}
                </Button>
                <ButtonWithHint
                  hint={t(
                    "Saves your edits (Turtle, R2RML, grounding choices) to the draft without accepting it.",
                  )}
                  onClick={saveChanges}
                  loading={updating}
                  disabled={!dirty || isReadOnly}
                >
                  {t("Save changes")}
                </ButtonWithHint>
                <Button
                  onClick={cancel}
                  loading={cancelling}
                  disabled={isReadOnly}
                >
                  {t("Cancel")}
                </Button>
                <ButtonWithHint
                  hint={t(
                    "Read-only quality checks: reasoner consistency (HermiT), structural metrics, and design pitfalls (OoPS!, if enabled). Doesn't change the proposal.",
                  )}
                  onClick={validate}
                  loading={validating}
                  disabled={!editedTurtle}
                >
                  {t("Validate")}
                </ButtonWithHint>
                <ButtonWithHint
                  hint={t(
                    "Merges this draft into the namespace's live ontology (graph, search index, storage). Runs in the background; can't be undone here.",
                  )}
                  variant="primary"
                  onClick={requestAccept}
                  loading={isAccepting || checkingAccept}
                  disabled={isReadOnly || isAccepting || checkingAccept}
                >
                  {proposal.status === "accepted"
                    ? t("Already accepted")
                    : isAccepting
                      ? t("Accepting…")
                      : t("Accept proposal")}
                </ButtonWithHint>
              </SpaceBetween>
            }
          >
            {(md.label as string) ||
              t("Proposal {id}…", { id: proposalId?.slice(0, 8) ?? "" })}
          </Header>
          {dirty && !hasMixedEdits && (
            <Alert type="info">
              {t("Unsaved edits. Click Save changes to upload.")}
            </Alert>
          )}
          {hasMixedEdits && (
            <Alert
              type="warning"
              header="Choose which edits to save"
              action={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={discardGroundingChanges}>
                    Discard grounding choices
                  </Button>
                  <Button onClick={discardTurtleChanges}>
                    Discard Turtle edits
                  </Button>
                </SpaceBetween>
              }
            >
              Grounding choices rebuild the ontology, so grounding and Turtle
              edits must be saved separately.
            </Alert>
          )}
          {saveError && (
            <Alert
              type="error"
              header="Could not save changes"
              dismissible
              onDismiss={() => setSaveError(null)}
            >
              {saveError}
            </Alert>
          )}
          {acceptResult && (
            <Alert
              type={acceptResult.ok ? "success" : "error"}
              header={acceptResult.ok ? t("Accepted") : t("Failed")}
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
                header={t("Previous accept attempt failed")}
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
                  ? t("Syncing embeddings…")
                  : t("Accepting proposal…")
              }
            >
              <SpaceBetween direction="horizontal" size="xs">
                <Spinner />
                <Box>
                  {proposal.status === "embeddings_sync"
                    ? t(
                        "Waiting for embeddings to become searchable in the vector index.",
                      )
                    : t(
                        "Merging into ontology graph (Neptune + AOSS + S3 + embeddings).",
                      )}
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
                dirty ? t("Save or discard your edits first") : undefined
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
              header={t("Accept with unrepaired datatype issues?")}
              footer={
                <Box float="right">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      variant="link"
                      onClick={() => setAcceptDatatypeWarning(null)}
                      disabled={repairing || accepting}
                    >
                      {t("Cancel")}
                    </Button>
                    <Button
                      onClick={doAccept}
                      disabled={repairing || accepting}
                    >
                      {t("Accept anyway")}
                    </Button>
                    <Button
                      variant="primary"
                      onClick={repairThenAccept}
                      loading={repairing}
                    >
                      {t("Repair & accept")}
                    </Button>
                  </SpaceBetween>
                </Box>
              }
            >
              <SpaceBetween size="s">
                <Box>
                  {t(
                    acceptDatatypeWarning.length === 1
                      ? "This proposal has {count} datatype reference that uses non-standard spelling. If you accept without repairing, it is stored as-is in your ontology and any downloads (queries still work — the served copy is auto-corrected). Repairing first keeps your saved ontology consistent everywhere."
                      : "This proposal has {count} datatype references that use non-standard spelling. If you accept without repairing, they are stored as-is in your ontology and any downloads (queries still work — the served copy is auto-corrected). Repairing first keeps your saved ontology consistent everywhere.",
                    { count: acceptDatatypeWarning.length },
                  )}
                </Box>
                <Table
                  variant="embedded"
                  columnDefinitions={[
                    {
                      id: "change",
                      header: t("Change"),
                      cell: (g: DatatypeFixGroup) => (
                        <span>
                          <code>{localName(g.found)}</code> →{" "}
                          <code>{localName(g.canonical)}</code>
                        </span>
                      ),
                    },
                    {
                      id: "reason",
                      header: t("Reason"),
                      cell: (g: DatatypeFixGroup) =>
                        g.reason === "non-standard capitalization"
                          ? t("non-standard capitalization")
                          : t("not a valid XSD datatype name"),
                    },
                    {
                      id: "affected",
                      header: t("Affected"),
                      cell: (g: DatatypeFixGroup) =>
                        t(
                          g.affected === 1
                            ? "{count} reference"
                            : "{count} references",
                          { count: g.affected },
                        ),
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
            headerText={t("Summary")}
          >
            <KeyValuePairs
              columns={3}
              items={[
                {
                  label: t("Proposal ID"),
                  value: <Box variant="code">{proposal.proposal_id}</Box>,
                },
                {
                  label: (
                    <SpaceBetween
                      direction="horizontal"
                      size="xxs"
                      alignItems="center"
                    >
                      <span>{t("Status")}</span>
                      <InfoPopover
                        header={t("Proposal status")}
                        ariaLabel={t("About proposal status")}
                      >
                        <SpaceBetween size="xs">
                          <Box variant="p">
                            {t("Pending: ready to review and edit.")}
                          </Box>
                          <Box variant="p">
                            {t(
                              "Accepting / Syncing embeddings: merging into the graph and search index (a few minutes).",
                            )}
                          </Box>
                          <Box variant="p">
                            {t("Accepted: now part of the live ontology.")}
                          </Box>
                          <Box variant="p">
                            {t("Updated: edits saved, not yet accepted.")}
                          </Box>
                          <Box variant="p">
                            {t(
                              "Rejected / Cancelled: the draft was discarded.",
                            )}
                          </Box>
                        </SpaceBetween>
                      </InfoPopover>
                    </SpaceBetween>
                  ),
                  value: proposalStatusIndicator(proposal.status, t),
                },
                {
                  label: t("Type"),
                  value:
                    proposal.source_type === "UNSTRUCTURED"
                      ? t("Unstructured Induction")
                      : t("Structured Induction"),
                },
                {
                  label: t("Namespace"),
                  value:
                    currentNamespace?.displayName ??
                    currentNamespace?.name ??
                    proposal.namespace,
                },
                {
                  label: t("Created"),
                  value: proposal.created_at ?? "—",
                },
                {
                  label: t("Datasource(s)"),
                  value:
                    proposal.source_type === "UNSTRUCTURED"
                      ? t("Lexical Graph")
                      : ((md.datasource_ids as string[]) ?? []).join(", ") ||
                        "—",
                },
                ...(proposal.source_type !== "UNSTRUCTURED"
                  ? [
                      {
                        label: t("Tables / columns"),
                        value: `${md.tables_processed ?? "?"} / ${md.columns_processed ?? "?"}`,
                      },
                    ]
                  : []),
                {
                  label:
                    proposal.source_type === "UNSTRUCTURED" ? (
                      t("Classes")
                    ) : (
                      <SpaceBetween
                        direction="horizontal"
                        size="xxs"
                        alignItems="center"
                      >
                        <span>{t("Novel classes")}</span>
                        <InfoPopover
                          header={t("Novel vs grounded")}
                          ariaLabel={t("About novel classes")}
                        >
                          <SpaceBetween size="xs">
                            <Box variant="p">
                              {t(
                                "New concepts created because they didn't match any grounding target.",
                              )}
                            </Box>
                            <Box variant="p">
                              {t(
                                "Classes that did match are reused (grounded) and not counted here. The graph colors grounded and novel classes differently.",
                              )}
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
                      ? t("Loading ontology from storage…")
                      : t("Rendering ontology…")}
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
              header={t("Ontology Turtle isn't valid")}
            >
              {t(
                "{error} The class list and graph won't update until the Turtle parses cleanly.",
                { error: parseError },
              )}
            </Alert>
          )}
          {flowNodes.length > 0 && (
            <ExpandableSection
              variant="container"
              defaultExpanded
              headerText={t("Proposal graph")}
              headerDescription={
                classCount > MAX_GRAPH_CLASSES
                  ? undefined
                  : t(
                      "Each cluster is a group of related classes; dashed purple arrows show subclass relationships. Click a class to inspect it, zoom in to read relationship names.",
                    )
              }
            >
              {classCount > MAX_GRAPH_CLASSES ? (
                <Alert type="info">
                  {t(
                    "This proposal has {count} classes — too large to render as a graph (hidden above {maximum} for performance). Use the table and Turtle editor below to review and edit.",
                    {
                      count: classCount,
                      maximum: MAX_GRAPH_CLASSES,
                    },
                  )}
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
              {t("Rebuilding ontology with updated grounding...")}
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
              headerText={t("R2RML Mapping")}
              headerInfo={
                <InfoPopover
                  header={t("R2RML mapping")}
                  ariaLabel={t("About R2RML mapping")}
                >
                  <Box variant="p">
                    {t(
                      "Maps each class and property back to its SQL table and columns, so questions about this ontology can be answered by querying the source database.",
                    )}
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
                    {r2rmlEditMode ? t("View") : t("Edit")}
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
                  ariaLabel={t("R2RML Turtle")}
                />
              ) : (
                <TurtleHighlight>{editedR2rml}</TurtleHighlight>
              )}
            </ExpandableSection>
          )}

          {editedTurtle && (
            <ExpandableSection
              variant="container"
              headerText={t("Ontology Source (Turtle)")}
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
                    {turtleEditMode ? t("View") : t("Edit")}
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
                    ariaLabel={t("Ontology Turtle")}
                  />
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      onClick={() => {
                        setTurtleEditMode(false);
                        setTurtleDraft(null);
                      }}
                    >
                      {t("Cancel")}
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
                      {t("Apply")}
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
                    throw new Error(
                      job.error || t("Constraint inference failed"),
                    );
                  }
                  if (job.status !== "completed") {
                    throw new Error(
                      t("Constraint inference timed out. Try again."),
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
              <Header variant="h3">
                {t("Edit: {name}", { name: editingEntity.entity.subject })}
              </Header>
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
                ariaLabel={t("Edit Turtle for {name}", {
                  name: editingEntity.entity.subject,
                })}
              />
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setEditingEntity(null)}>
                  {t("Cancel")}
                </Button>
                <Button variant="primary" onClick={applyEdit}>
                  {t("Apply")}
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
