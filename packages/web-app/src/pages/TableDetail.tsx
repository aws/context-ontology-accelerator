// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
import Alert from "@cloudscape-design/components/alert";
import AttributeEditor from "@cloudscape-design/components/attribute-editor";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Flashbar from "@cloudscape-design/components/flashbar";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Icon from "@cloudscape-design/components/icon";
import Input, { InputProps } from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Multiselect from "@cloudscape-design/components/multiselect";
import Pagination from "@cloudscape-design/components/pagination";
import Popover from "@cloudscape-design/components/popover";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Textarea from "@cloudscape-design/components/textarea";
import TextFilter from "@cloudscape-design/components/text-filter";
import TokenGroup from "@cloudscape-design/components/token-group";
import type { BadgeProps } from "@cloudscape-design/components/badge";
import type { SelectProps } from "@cloudscape-design/components/select";
import type { TableProps } from "@cloudscape-design/components/table";
import { useCollection } from "@cloudscape-design/collection-hooks";
import {
  useGetSourceTable,
  useGetSource,
  useReviewSourceTable,
  useReviewSourceColumn,
  useUpdateSourceTableMetadata,
  useUpdateSourceColumnMetadata,
  useUpdateSourceTableKeys,
  useKeepRescanRemoval,
} from "@api-hooks";
import { ReviewStatus, ReviewDecision } from "@coa/control-plane-client";
import type {
  ColumnMetadata,
  ForeignKeyOutput,
  RescanColumnChange,
  RescanFieldChange,
  RescanTableDiff,
} from "@coa/control-plane-client";
import { extractErrorMessage, runSequential } from "@utils";
import { InfoPopover } from "@components/InfoPopover";
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

// --- Constants ---

const PAGE_SIZE = 25;

const REVIEW_STATUS_TYPE: Record<string, "success" | "warning" | "error"> = {
  [ReviewStatus.APPROVED]: "success",
  [ReviewStatus.PENDING_REVIEW]: "warning",
  [ReviewStatus.REJECTED]: "error",
};

// --- Helpers ---

function getColumnStatus(col: ColumnMetadata): string {
  return col.businessMetadata?.reviewStatus ?? ReviewStatus.PENDING_REVIEW;
}

function statusCell(col: ColumnMetadata, t: AppTranslate) {
  const s = getColumnStatus(col);
  return (
    <StatusIndicator type={REVIEW_STATUS_TYPE[s] ?? "info"}>
      {s === ReviewStatus.PENDING_REVIEW
        ? t("Pending review")
        : s === ReviewStatus.APPROVED
          ? t("Approved")
          : t("Rejected")}
    </StatusIndicator>
  );
}

// --- Confidence / source display (shared) ---

/** Render an AI-inference confidence score as a colour-coded percentage.
 * Deterministic keys carry no score (confidence 0) and render as "—" — the
 * source badge already marks them as authoritative. */
function confidenceCell(confidence?: number) {
  if (typeof confidence !== "number" || confidence <= 0) return "—";
  const pct = Math.round(confidence * 100);
  const type =
    confidence >= 0.8 ? "success" : confidence >= 0.5 ? "warning" : "error";
  return <StatusIndicator type={type}>{pct}%</StatusIndicator>;
}

function sourceBadge(source?: string) {
  return source ? <Badge color="blue">{source}</Badge> : <>—</>;
}

/** Enrichment sources that mean the *description* came from an authoritative
 * record (source catalog, 3rd-party catalog, or a human steward) rather than
 * AI inference. Mirrors `_PROTECTED_DESCRIPTION_SOURCES` in
 * `table_enricher.py`. */
const DESCRIPTION_SOURCE_LABEL: Record<string, AppMessage> = {
  DETERMINISTIC: "Source catalog",
  CATALOG_EXISTING: "3rd-party catalog",
  STEWARD_EDITED: "Steward-edited",
  STEWARD_SPECIFIED: "Steward-specified",
  AI_GENERATED: "AI-generated",
};

const NON_AI_DESCRIPTION_SOURCES = new Set([
  "DETERMINISTIC",
  "CATALOG_EXISTING",
  "STEWARD_EDITED",
  "STEWARD_SPECIFIED",
]);

/** Render the description's provenance, plus an inline hint icon when tags/
 * synonyms/glossary terms may be AI-generated even though this description
 * came from a non-AI source. `enrichmentSource` on `businessMetadata` only
 * ever describes the *description* field — tags, synonyms, and glossary
 * terms can be AI-generated independently, so the hint sits directly next
 * to the description-source badge that would otherwise misrepresent them. */
function descriptionSourceBadge(
  meta:
    | {
        enrichmentSource?: string;
        tags?: string[];
        synonyms?: string[];
        glossaryTerms?: string[];
      }
    | undefined,
  metadataEnrichmentEnabled: boolean,
  t: AppTranslate,
) {
  const source = meta?.enrichmentSource;
  if (!source) return <>—</>;
  const label = DESCRIPTION_SOURCE_LABEL[source];
  return (
    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
      <Badge color="blue">{label ? t(label) : source}</Badge>
      {hasPossiblyAiEnrichedFields(meta, metadataEnrichmentEnabled) && (
        <AiEnrichedHint />
      )}
    </SpaceBetween>
  );
}

/** True when `businessMetadata` has non-empty tags/synonyms/glossary terms
 * that may be AI-generated even though the description's source is
 * non-AI (source catalog, 3rd-party catalog, or steward). The backend
 * stores provenance once per metadata block (see `table_enricher.py`), so
 * this can't be asserted with certainty — it's a hint, not a guarantee.
 *
 * Returns false when `metadataEnrichmentEnabled` is false — the scan
 * pipeline skips the enrichment step entirely for such sources (see
 * `metadataEnrichmentEnabled` on `DatabaseSourceDetail`), so there's no AI
 * involvement to warn about regardless of `enrichmentSource`. */
function hasPossiblyAiEnrichedFields(
  meta:
    | {
        enrichmentSource?: string;
        tags?: string[];
        synonyms?: string[];
        glossaryTerms?: string[];
      }
    | undefined,
  metadataEnrichmentEnabled: boolean,
): boolean {
  if (!metadataEnrichmentEnabled) return false;
  if (!meta?.enrichmentSource) return false;
  if (!NON_AI_DESCRIPTION_SOURCES.has(meta.enrichmentSource)) return false;
  return Boolean(
    meta.tags?.length || meta.synonyms?.length || meta.glossaryTerms?.length,
  );
}

/** Info icon (not a Badge) clarifying that tags/synonyms/glossary terms may
 * be AI-generated even when the description's source is the source catalog,
 * a 3rd-party catalog, or a steward. Rendered inline next to
 * the "Description source" badge (via `descriptionSourceBadge`) since it
 * flags that *that field's* provenance is inconsistent with the provenance
 * of the other metadata fields in the same block. Deliberately visually
 * distinct from the tag/synonym/glossary Badges, so it doesn't read as just
 * another generated tag. */
function AiEnrichedHint() {
  const { t } = useAppI18n();
  return (
    <Popover
      triggerType="custom"
      size="small"
      dismissButton={false}
      content={t(
        "This description came from a non-AI source, but tags, synonyms, and glossary terms may still be AI-generated.",
      )}
    >
      <Icon
        name="status-info"
        variant="subtle"
        ariaLabel={t("AI-enriched hint")}
      />
    </Popover>
  );
}

/** One-line inference note with the full text on demand.
 *
 * A column can carry several inferred relationships and each is a yes/no call;
 * the note must be readable at a glance (0.3.3 feedback). The backend already
 * shortens it to a clause; this keeps the row to one line regardless and puts
 * the full text one click away rather than inline. */
const REVIEW_NOTE_INLINE_CHARS = 90;

function ReviewNote({ note }: { note: string }) {
  const short =
    note.length > REVIEW_NOTE_INLINE_CHARS
      ? `${note.slice(0, REVIEW_NOTE_INLINE_CHARS).trimEnd()}…`
      : note;
  const body = (
    <Box fontSize="body-s" color="text-status-inactive">
      {short}
    </Box>
  );
  if (short === note) return body;
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      content={note}
    >
      {body}
    </Popover>
  );
}

// --- Re-scan diff (old vs new) ---

/** Badge text + colour for a re-scan column change status. Added is additive
 * (green), removed is destructive (red), a modified column is a neutral change
 * (blue). Defaults to "Changed" for any unexpected status. */
function rescanColumnBadge(
  status: string,
  t: AppTranslate,
): {
  text: string;
  color: NonNullable<BadgeProps["color"]>;
} {
  if (status === "added") return { text: t("Added"), color: "green" };
  if (status === "removed") return { text: t("Removed"), color: "red" };
  return { text: t("Changed"), color: "blue" };
}

/** Display a re-scan field value; empty/absent renders as an em dash so an
 * added value (old empty) or a cleared value (new empty) reads cleanly. */
function rescanValue(value?: string): string {
  return value && value.length > 0 ? value : "—";
}

/** One changed source-owned field rendered as "field (KIND): old → new". */
function rescanFieldChangeRow(fc: RescanFieldChange) {
  return (
    <Box variant="small">
      <strong>{fc.field}</strong>
      {fc.kind ? ` (${fc.kind})` : ""}: {rescanValue(fc.old)} →{" "}
      {rescanValue(fc.new)}
    </Box>
  );
}

/** Vertical list of changed source-owned fields (each as old → new). Shared by
 * the top summary Alert and the per-column info popover. */
function rescanFieldChangeList(fields: RescanFieldChange[]) {
  return (
    <SpaceBetween size="xxs">
      {fields.map((fc, i) => (
        <div key={`${fc.field ?? "field"}-${i}`}>
          {rescanFieldChangeRow(fc)}
        </div>
      ))}
    </SpaceBetween>
  );
}

/** One column's re-scan change for the summary Alert: the column name + an
 * Added/Removed/Changed badge, and — for a modified column — its changed fields
 * listed (indented) as old → new. */
function rescanColumnChangeRow(change: RescanColumnChange, t: AppTranslate) {
  const { text, color } = rescanColumnBadge(change.status ?? "modified", t);
  const fields = change.fields ?? [];
  return (
    <SpaceBetween size="xxs">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="small" fontWeight="bold">
          {change.name}
        </Box>
        <Badge color={color}>{text}</Badge>
      </SpaceBetween>
      {fields.length > 0 && (
        <Box padding={{ left: "l" }}>{rescanFieldChangeList(fields)}</Box>
      )}
    </SpaceBetween>
  );
}

/** "Changes since last approved scan" summary, shown only while the source is in
 * RESCAN_REVIEW (when `rescanDiff` is present on the table). Two groups for an
 * at-a-glance read: table-level field changes and per-column changes, each as
 * old → new. The same per-column detail is also reachable inline on each column
 * row in the Columns table below (via the info icon next to its badge). */
function RescanChangesPanel({ diff }: { diff: RescanTableDiff }) {
  const { t } = useAppI18n();
  const tableFields = diff.tableFields ?? [];
  const columns = diff.columns ?? [];
  // Foldable so a table with many changed columns doesn't produce a huge panel
  // on larger graphs. Collapsed still shows the counts, so the steward sees the
  // magnitude at a glance; expand for the old → new detail.
  const tableCount = t(
    tableFields.length === 1
      ? "{count} table-level change"
      : "{count} table-level changes",
    { count: tableFields.length },
  );
  const columnCount = t(
    columns.length === 1 ? "{count} column change" : "{count} column changes",
    { count: columns.length },
  );
  const countLabel = t("{tableCount}, {columnCount}", {
    tableCount,
    columnCount,
  });
  return (
    <Alert type="info" header={t("Changes since last approved scan")}>
      <ExpandableSection variant="footer" headerText={countLabel}>
        <SpaceBetween size="m">
          <div>
            <Box variant="small" fontWeight="bold">
              {t("Table-level changes")}
            </Box>
            {tableFields.length > 0 ? (
              rescanFieldChangeList(tableFields)
            ) : (
              <Box variant="small" color="text-status-inactive">
                {t("None")}
              </Box>
            )}
          </div>
          <div>
            <Box variant="small" fontWeight="bold">
              {t("Column changes")}
            </Box>
            {columns.length > 0 ? (
              <SpaceBetween size="xs">
                {columns.map((cc, i) => (
                  <div key={`${cc.name ?? "col"}-${i}`}>
                    {rescanColumnChangeRow(cc, t)}
                  </div>
                ))}
              </SpaceBetween>
            ) : (
              <Box variant="small" color="text-status-inactive">
                {t("None")}
              </Box>
            )}
          </div>
        </SpaceBetween>
      </ExpandableSection>
    </Alert>
  );
}

/** Shown while the source is in RESCAN_REVIEW for a table the re-scan discovered
 * for the first time (`added`): it has no old-vs-new diff — the whole table is
 * new — so we say so instead of showing an empty changes panel. */
function NewTablePanel() {
  const { t } = useAppI18n();
  return (
    <Alert type="info" header={t("New table")}>
      {t(
        "This table was discovered during the latest re-scan and did not exist in the last approved scan. There is no before/after to compare — review its generated metadata below and approve to add it.",
      )}
    </Alert>
  );
}

/** Shown while the source is in RESCAN_REVIEW for a table the re-scan found
 * gone from the source (`pendingDeletion`): it is dropped and will be deleted
 * when the re-scan is approved, unless the steward keeps it. */
function DroppedTablePanel() {
  const { t } = useAppI18n();
  return (
    <Alert type="warning" header={t("Dropped table")}>
      {t(
        'This table was not found in the source during the latest re-scan. It will be deleted when you approve the re-scan — use "Keep table" above to retain it if the source removal was unintended (e.g. a partial scan or a changed filter).',
      )}
    </Alert>
  );
}

/** Per-column re-scan indicator for a column row: an Added/Removed/Changed
 * badge. For a modified column, an info icon next to the badge opens a popover
 * listing each changed field as old → new — the icon is the discoverable
 * trigger (matching the `InfoPopover` pattern used across the app), since a
 * plain badge gives no signal that it is interactive. */
function rescanColumnChangeCell(change: RescanColumnChange, t: AppTranslate) {
  const { text, color } = rescanColumnBadge(change.status ?? "modified", t);
  const fields = change.fields ?? [];
  const badge = <Badge color={color}>{text}</Badge>;
  if (change.status === "modified" && fields.length > 0) {
    return (
      <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
        {badge}
        <InfoPopover
          header={t("Changes since last approved scan")}
          ariaLabel={t("Column changes since last approved scan")}
        >
          {rescanFieldChangeList(fields)}
        </InfoPopover>
      </SpaceBetween>
    );
  }
  return badge;
}

// --- Component ---

export const TableDetail: React.FC = () => {
  const { t } = useAppI18n();
  const navigate = useNavigate();
  const { namespaceId, dataSourceId, tableId } = useParams<{
    namespaceId: string;
    dataSourceId: string;
    tableId: string;
  }>();
  const { data, isLoading, error, refetch, isFetching } = useGetSourceTable(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );
  // Fetched only to gate the AI-enriched hint icon: when
  // metadataEnrichmentEnabled is false, the scan pipeline never ran the
  // enrichment step, so tags/synonyms/glossary terms can't be AI-generated
  // even if the description's source looks non-AI.
  const { data: sourceData } = useGetSource(
    namespaceId ?? "",
    dataSourceId ?? "",
  );
  const metadataEnrichmentEnabled =
    sourceData?.body?.databaseDetails?.metadataEnrichmentEnabled !== false;
  const reviewTableMutation = useReviewSourceTable(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );
  const reviewColumnMutation = useReviewSourceColumn(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );
  const updateTableMetaMutation = useUpdateSourceTableMetadata(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );
  const updateColumnMetaMutation = useUpdateSourceColumnMetadata(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );
  const updateKeysMutation = useUpdateSourceTableKeys(
    namespaceId ?? "",
    dataSourceId ?? "",
    tableId ?? "",
  );

  const [flash, setFlash] = useState<
    { id: string; type: "success" | "error"; content: string }[]
  >([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const [editing, setEditing] = useState<ColumnMetadata | null>(null);
  const [editingTable, setEditingTable] = useState(false);
  const [editingKeys, setEditingKeys] = useState(false);
  const [bulkLoading, setBulkLoading] = useState(false);

  const { name: nsName } = useNamespace(namespaceId);
  const sourceName = sourceData?.body?.name ?? dataSourceId ?? "";
  const tableName = data?.name ?? tableId ?? "";
  const isEditing = editing !== null || editingTable || editingKeys;
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    {
      text: t("Sources"),
      href: `/namespaces/${namespaceId}/sources`,
      namespaceId,
    },
    {
      text: sourceName,
      href: `/namespaces/${namespaceId}/sources/${dataSourceId}`,
      namespaceId,
    },
    { text: tableName },
    ...(isEditing ? [{ text: t("Edit") }] : []),
  ]);

  // Re-scan review: keep (decline) a table or column the re-scan flagged for
  // deletion, so approving the re-scan no longer removes it.
  const { mutate: keepMutate, isPending: keepPending } = useKeepRescanRemoval(
    namespaceId ?? "",
    dataSourceId ?? "",
  );
  const [keepingColumn, setKeepingColumn] = useState<string | null>(null);
  const [keepingTable, setKeepingTable] = useState(false);
  // useCallback so the column definitions' useMemo stays stable — keepMutate is
  // a stable react-query reference, so handleKeepColumn only changes with tableId.
  const handleKeepColumn = useCallback(
    (columnName: string) => {
      setKeepingColumn(columnName);
      keepMutate(
        { tableId: tableId ?? "", columnName },
        {
          onSuccess: () =>
            setFlash((prev) => [
              ...prev,
              {
                id: String(Date.now()),
                type: "success",
                content: t(
                  'Keeping column "{name}". Approving the re-scan will no longer drop it.',
                  { name: columnName },
                ),
              },
            ]),
          onError: (e) =>
            setFlash((prev) => [
              ...prev,
              { id: String(Date.now()), type: "error", content: e.message },
            ]),
          onSettled: () => setKeepingColumn(null),
        },
      );
    },
    [keepMutate, tableId, t],
  );
  const handleKeepTable = () => {
    setKeepingTable(true);
    keepMutate(
      { tableId: tableId ?? "" },
      {
        onSuccess: () =>
          setFlash((prev) => [
            ...prev,
            {
              id: String(Date.now()),
              type: "success",
              content: t(
                "Keeping this table. Approving the re-scan will no longer delete it.",
              ),
            },
          ]),
        onError: (e) =>
          setFlash((prev) => [
            ...prev,
            { id: String(Date.now()), type: "error", content: e.message },
          ]),
        onSettled: () => setKeepingTable(false),
      },
    );
  };

  // Filter columns by review status
  const filteredColumns = useMemo(() => {
    const cols = data?.columns ?? [];
    if (statusFilter === "all") return cols;
    return cols.filter((c) => getColumnStatus(c) === statusFilter);
  }, [data?.columns, statusFilter]);

  const reviewFilterOptions = useMemo<SelectProps.Option[]>(
    () => [
      { value: "all", label: t("All statuses") },
      {
        value: ReviewStatus.PENDING_REVIEW,
        label: t("Pending review"),
      },
      { value: ReviewStatus.APPROVED, label: t("Approved") },
      { value: ReviewStatus.REJECTED, label: t("Rejected") },
    ],
    [t],
  );

  const foreignKeyColumnDefs = useMemo<
    TableProps.ColumnDefinition<ForeignKeyOutput>[]
  >(
    () => [
      {
        id: "column",
        header: t("Column"),
        isRowHeader: true,
        cell: (fk) => <Box fontWeight="bold">{fk.column ?? "—"}</Box>,
      },
      {
        id: "references",
        header: t("References"),
        cell: (fk) =>
          fk.targetColumn
            ? `${fk.targetTable ?? ""}.${fk.targetColumn}`
            : (fk.targetTable ?? "—"),
      },
      {
        id: "source",
        header: t("Source"),
        cell: (fk) => sourceBadge(fk.source),
      },
      {
        id: "review",
        header: t("Review"),
        // Inferred relationships (#1088) carry a review state; authoritative keys
        // (deterministic/steward) do not and render "—". Cross-source relationships
        // also show a chip and their inference provenance so a steward can judge.
        cell: (fk) => {
          if (fk.source !== "AI_INFERRED" || !fk.reviewStatus) return "—";
          const type = REVIEW_STATUS_TYPE[fk.reviewStatus] ?? "warning";
          return (
            <SpaceBetween size="xxs">
              <StatusIndicator type={type}>{fk.reviewStatus}</StatusIndicator>
              {fk.targetDatasourceId ? (
                <Badge color="blue">{t("cross-source")}</Badge>
              ) : null}
              {fk.provenance ? <ReviewNote note={fk.provenance} /> : null}
            </SpaceBetween>
          );
        },
      },
      {
        id: "confidence",
        header: t("Confidence"),
        cell: (fk) => confidenceCell(fk.confidence),
      },
    ],
    [t],
  );

  // Re-scan review: per-column change breakdown keyed by column name, so each
  // row can show an Added/Removed/Changed badge (and, for a modified column,
  // its changed fields). Empty outside RESCAN_REVIEW — rescanDiff is absent then.
  const rescanColumnChanges = useMemo(() => {
    const map = new Map<string, RescanColumnChange>();
    for (const change of data?.rescanDiff?.columns ?? []) {
      if (change.name) map.set(change.name, change);
    }
    return map;
  }, [data?.rescanDiff?.columns]);

  // Column definitions matching coa-ui style
  const columnDefs = useMemo<TableProps.ColumnDefinition<ColumnMetadata>[]>(
    () => [
      {
        id: "name",
        header: t("Column"),
        sortingField: "name",
        isRowHeader: true,
        cell: (col) => <Box fontWeight="bold">{col.name}</Box>,
      },
      {
        id: "dataType",
        header: t("Data type"),
        sortingField: "dataType",
        cell: (col) => <Box variant="code">{col.dataType}</Box>,
      },
      {
        id: "description",
        header: t("Description"),
        cell: (col) => col.businessMetadata?.description ?? "—",
        minWidth: 200,
      },
      {
        id: "synonyms",
        header: t("Synonyms"),
        cell: (col) => {
          const syns = col.businessMetadata?.synonyms;
          return syns && syns.length > 0 ? syns.join(", ") : "—";
        },
      },
      {
        id: "glossaryTerms",
        header: t("Glossary"),
        cell: (col) => {
          const terms = col.businessMetadata?.glossaryTerms;
          if (!terms || terms.length === 0) return "—";
          return (
            <SpaceBetween direction="horizontal" size="xxs">
              {terms.map((t) => (
                <Badge key={t}>{t}</Badge>
              ))}
            </SpaceBetween>
          );
        },
      },
      {
        id: "tags",
        header: t("Tags"),
        cell: (col) => {
          const tags = col.businessMetadata?.tags;
          if (!tags || tags.length === 0) return "—";
          return (
            <SpaceBetween direction="horizontal" size="xxs">
              {tags.map((t) => (
                <Badge key={t} color="grey">
                  {t}
                </Badge>
              ))}
            </SpaceBetween>
          );
        },
      },
      {
        id: "distinctValues",
        header: t("Sample values"),
        cell: (col) => {
          const vals = col.distinctValues;
          if (!vals || vals.length === 0) return "—";
          return (
            <SpaceBetween direction="horizontal" size="xxs">
              {vals.map((v, i) => (
                <Badge key={`${v}-${i}`} color="grey">
                  {v}
                </Badge>
              ))}
            </SpaceBetween>
          );
        },
      },
      {
        id: "nullable",
        header: t("Nullable"),
        cell: (col) => (col.nullable ? t("Yes") : t("No")),
      },
      {
        id: "enrichmentSource",
        header: t("Description source"),
        cell: (col) =>
          descriptionSourceBadge(
            col.businessMetadata,
            metadataEnrichmentEnabled,
            t,
          ),
      },
      {
        id: "confidence",
        header: t("Confidence"),
        cell: (col) => confidenceCell(col.businessMetadata?.confidence),
      },
      {
        id: "status",
        header: t("Review status"),
        sortingComparator: (a, b) =>
          getColumnStatus(a).localeCompare(getColumnStatus(b)),
        cell: (col) => {
          const rescanChange = col.name
            ? rescanColumnChanges.get(col.name)
            : undefined;
          // A re-scan-dropped column is surfaced below as the actionable
          // "Pending deletion" badge (with a Keep button). Showing the diff's
          // red "Removed" badge too is a duplicate of the same fact — and reads
          // as already-gone when it is only pending — so suppress it in that one
          // case. Added/modified columns still show their diff badge.
          const showRescanChange =
            rescanChange &&
            !(rescanChange.status === "removed" && col.pendingDeletion);
          return (
            <SpaceBetween size="xxs">
              {statusCell(col, t)}
              {showRescanChange && rescanColumnChangeCell(rescanChange, t)}
              {col.pendingDeletion && (
                <SpaceBetween
                  direction="horizontal"
                  size="xs"
                  alignItems="center"
                >
                  <Badge color="red">{t("Pending deletion")}</Badge>
                  <Button
                    variant="inline-link"
                    loading={keepingColumn === col.name}
                    disabled={keepPending}
                    onClick={() => handleKeepColumn(col.name ?? "")}
                  >
                    {t("Keep")}
                  </Button>
                </SpaceBetween>
              )}
            </SpaceBetween>
          );
        },
      },
    ],
    [
      metadataEnrichmentEnabled,
      keepingColumn,
      keepPending,
      handleKeepColumn,
      rescanColumnChanges,
      t,
    ],
  );

  const { items, collectionProps, filterProps, paginationProps, actions } =
    useCollection(filteredColumns, {
      filtering: {
        empty: (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {t("No columns found.")}
          </Box>
        ),
        noMatch: (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {t("No columns match the filter.")}
          </Box>
        ),
      },
      pagination: { pageSize: PAGE_SIZE },
      sorting: { defaultState: { sortingColumn: columnDefs[0]! } },
      selection: {},
    });

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error) {
    return (
      <Alert type="error" header={t("Failed to load table")}>
        {error.message}
      </Alert>
    );
  }

  if (!data) return null;

  const selected = collectionProps.selectedItems ?? [];
  const tableStatus = data.reviewStatus as string;
  const pendingCount = (data.columns ?? []).filter(
    (c) => getColumnStatus(c) === ReviewStatus.PENDING_REVIEW,
  ).length;

  // --- Actions ---

  function notify(type: "success" | "error", content: string) {
    setFlash((prev) => [...prev, { id: String(Date.now()), type, content }]);
  }

  async function handleBulkColumns(decision: ReviewDecision) {
    if (!tableId) return;
    const snapshot = [...selected];
    const total = snapshot.length;
    setBulkLoading(true);
    // Review SEQUENTIALLY, not concurrently. Each per-column review is a full
    // read-modify-write of the entire table asset on the backend (load asset →
    // flip one column → write a new revision). Firing them in parallel makes
    // those writes race off the same baseline revision and clobber each other,
    // so only the last writer's column survives and the rest are silently lost
    // (even though every call returns 200). runSequential awaits each call so
    // the next one reads the revision the previous one just wrote.
    let failed: ColumnMetadata[] = [];
    try {
      ({ failed } = await runSequential(snapshot, (col) =>
        reviewColumnMutation.mutateAsync({ columnName: col.name!, decision }),
      ));
    } finally {
      // Deselect everything that was successfully reviewed; keep only the
      // failed columns selected so the user can retry just those.
      actions.setSelectedItems(failed);
      setBulkLoading(false);
    }

    const approved = decision === ReviewDecision.APPROVED;
    if (failed.length === 0) {
      notify(
        "success",
        t(
          approved
            ? total === 1
              ? "Approved {count} column."
              : "Approved {count} columns."
            : total === 1
              ? "Rejected {count} column."
              : "Rejected {count} columns.",
          { count: total },
        ),
      );
    } else {
      const succeeded = total - failed.length;
      notify(
        "error",
        t(
          approved
            ? total === 1
              ? "Approved {succeeded} of {total} column; {failed} failed."
              : "Approved {succeeded} of {total} columns; {failed} failed."
            : total === 1
              ? "Rejected {succeeded} of {total} column; {failed} failed."
              : "Rejected {succeeded} of {total} columns; {failed} failed.",
          { succeeded, total, failed: failed.length },
        ),
      );
    }
  }

  async function handleApproveAll() {
    if (!tableId) return;
    try {
      await reviewTableMutation.mutateAsync({
        decision: ReviewDecision.APPROVED,
      });
      notify("success", t("Table and all columns approved."));
    } catch (e) {
      notify("error", e instanceof Error ? e.message : String(e));
    }
  }

  async function handleRejectTable() {
    if (!tableId) return;
    try {
      await reviewTableMutation.mutateAsync({
        decision: ReviewDecision.REJECTED,
      });
      notify("success", t("Table and all columns rejected."));
    } catch (e) {
      notify("error", e instanceof Error ? e.message : String(e));
    }
  }

  function handleSaveEdit(overrides: {
    description: string;
    synonyms: string[];
    glossaryTerms: string[];
    tags: string[];
  }) {
    if (!editing || !tableId) return;
    updateColumnMetaMutation.mutate(
      { columnName: editing.name!, overrides },
      {
        onSuccess: () => {
          notify(
            "success",
            t('Updated column "{name}".', { name: editing.name ?? "" }),
          );
          setEditing(null);
        },
        onError: (e) => notify("error", e.message),
      },
    );
  }

  function handleSaveTable(overrides: {
    description: string;
    synonyms: string[];
    glossaryTerms: string[];
    tags: string[];
  }) {
    if (!tableId) return;
    updateTableMetaMutation.mutate(
      { overrides },
      {
        onSuccess: () => {
          notify("success", t("Table metadata updated."));
          setEditingTable(false);
        },
        onError: (e) => notify("error", e.message),
      },
    );
  }

  function handleSaveKeys(payload: {
    primaryKey: { columns: string[] };
    foreignKeys: {
      column: string;
      targetTable: string;
      targetColumn?: string;
    }[];
  }) {
    if (!tableId) return;
    updateKeysMutation.mutate(payload, {
      onSuccess: () => {
        notify("success", t("Keys & relationships updated."));
        setEditingKeys(false);
      },
    });
  }

  // Approve/reject one inferred relationship (#1088). Re-sends the full FK list
  // (so unrelated keys are preserved) with only the target's reviewStatus
  // changed; the backend keeps its source/targetDatasourceId/provenance.
  function reviewRelationship(target: ForeignKeyOutput, decision: string) {
    if (!tableId) return;
    const foreignKeys = (data?.foreignKeys ?? []).map((fk) => ({
      column: fk.column ?? "",
      targetTable: fk.targetTable ?? "",
      targetColumn: fk.targetColumn ?? undefined,
      ...(fk.column === target.column &&
      fk.targetTable === target.targetTable &&
      (fk.targetColumn ?? "") === (target.targetColumn ?? "")
        ? { reviewStatus: decision }
        : {}),
    }));
    updateKeysMutation.mutate(
      { foreignKeys },
      {
        onSuccess: () =>
          notify(
            "success",
            decision === ReviewStatus.APPROVED
              ? "Relationship approved."
              : "Relationship rejected.",
          ),
        onError: (e) => notify("error", extractErrorMessage(e)),
      },
    );
  }

  // Base FK columns plus a per-row Approve/Reject action for pending inferred
  // relationships (authoritative and already-decided keys show no action).
  const fkColumnDefs: TableProps.ColumnDefinition<ForeignKeyOutput>[] = [
    ...foreignKeyColumnDefs,
    {
      id: "actions",
      header: t("Actions"),
      cell: (fk) =>
        fk.source === "AI_INFERRED" &&
        fk.reviewStatus === ReviewStatus.PENDING_REVIEW ? (
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="inline-link"
              loading={updateKeysMutation.isPending}
              onClick={() => reviewRelationship(fk, ReviewStatus.APPROVED)}
            >
              {t("Approve")}
            </Button>
            <Button
              variant="inline-link"
              disabled={updateKeysMutation.isPending}
              onClick={() => reviewRelationship(fk, ReviewStatus.REJECTED)}
            >
              {t("Reject")}
            </Button>
          </SpaceBetween>
        ) : null,
    },
  ];

  return (
    <SpaceBetween size="m">
      {flash.length > 0 && (
        <Flashbar
          items={flash.map((f) => ({
            ...f,
            dismissible: true,
            onDismiss: () =>
              setFlash((prev) => prev.filter((x) => x.id !== f.id)),
          }))}
        />
      )}

      {/* Table-level metadata header */}
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              iconName="refresh"
              loading={isFetching}
              onClick={() => refetch()}
            >
              {t("Refresh")}
            </Button>
            <Button onClick={() => navigate(-1)}>{t("Back")}</Button>
            {data.pendingDeletion && (
              <Button
                loading={keepingTable}
                disabled={keepPending}
                onClick={handleKeepTable}
              >
                {t("Keep table")}
              </Button>
            )}
            <Button
              onClick={handleRejectTable}
              loading={reviewTableMutation.isPending}
              disabled={tableStatus === ReviewStatus.REJECTED}
            >
              {t("Reject table & all columns")}
            </Button>
            <Button
              variant="primary"
              onClick={handleApproveAll}
              loading={reviewTableMutation.isPending}
              disabled={tableStatus === ReviewStatus.APPROVED}
            >
              {t("Approve table & all columns")}
            </Button>
          </SpaceBetween>
        }
      >
        {data.name} <Badge color="grey">{data.database}</Badge>
      </Header>

      {/* Re-scan review: old-vs-new changes since the last approved scan.
          Present only while the source is in RESCAN_REVIEW. */}
      {data.rescanDiff && <RescanChangesPanel diff={data.rescanDiff} />}
      {data.added && !data.rescanDiff && <NewTablePanel />}
      {data.pendingDeletion && <DroppedTablePanel />}

      {/* Table metadata summary */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <Button onClick={() => setEditingTable(true)}>{t("Edit")}</Button>
            }
          >
            {t("Table metadata")}
          </Header>
        }
      >
        <KeyValuePairs
          columns={3}
          items={[
            { label: t("Table ID"), value: data.tableId },
            {
              label: t("Review status"),
              value: (
                <SpaceBetween
                  direction="horizontal"
                  size="xs"
                  alignItems="center"
                >
                  <StatusIndicator
                    type={REVIEW_STATUS_TYPE[tableStatus] ?? "info"}
                  >
                    {tableStatus === ReviewStatus.PENDING_REVIEW
                      ? t("Pending review")
                      : tableStatus}
                  </StatusIndicator>
                  {data.pendingDeletion && (
                    <Badge color="red">{t("Pending deletion")}</Badge>
                  )}
                </SpaceBetween>
              ),
            },
            {
              label: t("Description source"),
              value: descriptionSourceBadge(
                data.businessMetadata,
                metadataEnrichmentEnabled,
                t,
              ),
            },
            {
              label: t("Description"),
              value: data.businessMetadata?.description ?? "—",
            },
            {
              label: t("Synonyms"),
              value: data.businessMetadata?.synonyms?.join(", ") || "—",
            },
            {
              label: t("Tags"),
              value: data.businessMetadata?.tags?.join(", ") || "—",
            },
            {
              label: t("Glossary terms"),
              value: data.businessMetadata?.glossaryTerms?.join(", ") || "—",
            },
            {
              label: t("Format"),
              value: data.technicalMetadata?.format || "—",
            },
            {
              label: t("Location"),
              value: data.technicalMetadata?.location || "—",
            },
          ]}
        />
      </Container>

      {/* Keys & relationships */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <Button
                onClick={() => {
                  updateKeysMutation.reset();
                  setEditingKeys(true);
                }}
              >
                {t("Edit keys")}
              </Button>
            }
          >
            {t("Keys & relationships")}
          </Header>
        }
      >
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={3}
            items={[
              {
                label: t("Primary key"),
                value:
                  data.primaryKey?.columns && data.primaryKey.columns.length > 0
                    ? data.primaryKey.columns.join(", ")
                    : "—",
              },
              {
                label: t("Source"),
                value: sourceBadge(data.primaryKey?.source),
              },
              {
                label: t("Confidence"),
                value: confidenceCell(data.primaryKey?.confidence),
              },
            ]}
          />
          <Table<ForeignKeyOutput>
            variant="embedded"
            items={data.foreignKeys ?? []}
            trackBy="column"
            columnDefinitions={fkColumnDefs}
            header={
              <Header
                variant="h3"
                counter={`(${data.foreignKeys?.length ?? 0})`}
              >
                {t("Foreign keys & relationships")}
              </Header>
            }
            empty={
              <Box textAlign="center" color="text-status-inactive" padding="s">
                {t("No foreign key relationships detected.")}
              </Box>
            }
          />
        </SpaceBetween>
      </Container>

      {/* Pending review alert */}
      {pendingCount > 0 && (
        <Alert
          type="info"
          header={t(
            pendingCount === 1
              ? "{count} column pending review"
              : "{count} columns pending review",
            { count: pendingCount },
          )}
        >
          {t(
            "AI enrichment produced descriptions, synonyms, and glossary mappings for these columns. Review or edit them before approving.",
          )}
        </Alert>
      )}

      {/* Columns table with selection, filtering, pagination, edit */}
      <Table<ColumnMetadata>
        {...collectionProps}
        items={items}
        trackBy="name"
        variant="container"
        stickyHeader
        resizableColumns
        selectionType="multi"
        columnDefinitions={columnDefs}
        header={
          <Header
            variant="h2"
            counter={`(${filteredColumns.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  disabled={selected.length === 0 || bulkLoading}
                  loading={bulkLoading}
                  onClick={() => handleBulkColumns(ReviewDecision.REJECTED)}
                >
                  {t("Reject selected")}
                </Button>
                <Button
                  disabled={selected.length === 0 || bulkLoading}
                  loading={bulkLoading}
                  onClick={() => handleBulkColumns(ReviewDecision.APPROVED)}
                >
                  {t("Approve selected")}
                </Button>
                <Button
                  disabled={selected.length !== 1 || bulkLoading}
                  onClick={() => setEditing(selected[0] ?? null)}
                >
                  {t("Edit")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("Columns")}
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              {...filterProps}
              filteringPlaceholder={t("Find columns")}
              filteringAriaLabel={t("Filter columns")}
            />
            <Select
              selectedOption={
                reviewFilterOptions.find(
                  (option) => option.value === statusFilter,
                ) ?? null
              }
              onChange={({ detail }) =>
                setStatusFilter(detail.selectedOption.value ?? "all")
              }
              options={reviewFilterOptions}
              ariaLabel={t("Filter by review status")}
            />
          </SpaceBetween>
        }
        pagination={<Pagination {...paginationProps} />}
        empty={
          <Box textAlign="center" color="text-status-inactive" padding="l">
            {t("No columns discovered yet.")}
          </Box>
        }
        ariaLabels={{
          tableLabel: t("Column metadata"),
          selectionGroupLabel: t("Column selection"),
          allItemsSelectionLabel: ({ selectedItems }) =>
            t("{count} columns selected", {
              count: selectedItems.length,
            }),
          itemSelectionLabel: (_s, row) =>
            t("Select {name}", { name: row.name ?? "" }),
        }}
      />

      {/* Edit modal */}
      {editing && (
        <EditColumnModal
          column={editing}
          onDismiss={() => setEditing(null)}
          onSave={handleSaveEdit}
          loading={updateColumnMetaMutation.isPending}
        />
      )}

      {/* Table edit modal */}
      {editingTable && (
        <EditTableModal
          businessMetadata={data.businessMetadata}
          tableName={data.name ?? ""}
          onDismiss={() => setEditingTable(false)}
          onSave={handleSaveTable}
          loading={updateTableMetaMutation.isPending}
        />
      )}

      {/* Keys edit modal */}
      {editingKeys && (
        <EditKeysModal
          tableName={data.name ?? ""}
          columnNames={(data.columns ?? []).map((c) => c.name!)}
          primaryKey={data.primaryKey?.columns ?? []}
          foreignKeys={data.foreignKeys ?? []}
          onDismiss={() => setEditingKeys(false)}
          onSave={handleSaveKeys}
          loading={updateKeysMutation.isPending}
          error={updateKeysMutation.error}
        />
      )}
    </SpaceBetween>
  );
};

// --- Shared helpers ---

function toTokens(
  values: string[],
  t: AppTranslate,
): { label: string; dismissLabel: string }[] {
  return values.map((value) => ({
    label: value,
    dismissLabel: t("Remove {value}", { value }),
  }));
}

function useTokenField(initial: string[]) {
  const { t } = useAppI18n();
  const [values, setValues] = useState(initial);
  const [input, setInput] = useState("");
  const items = useMemo(() => toTokens(values, t), [t, values]);

  function add() {
    const trimmed = input.trim();
    if (trimmed && !values.includes(trimmed)) {
      setValues([...values, trimmed]);
    }
    setInput("");
  }

  function remove(index: number) {
    setValues(values.filter((_, i) => i !== index));
  }

  function clear() {
    setValues([]);
  }

  return {
    items,
    input,
    setInput,
    add,
    remove,
    clear,
    values,
  };
}

/** Synonyms field shared by the table and column editors.
 *
 * The list is pre-filled with whatever is stored, which for an enriched source
 * is the AI-generated guesses (steward-curation supersede: `enrichmentSource` only describes the
 * description, so we cannot tell an AI synonym from an authored one per item).
 * A steward who only adds terms would otherwise re-submit the AI ones too and
 * the ontology would keep them as live `skos:altLabel`s. "Clear all" lets them
 * start from an empty, authored list; PATCH replaces the whole list, and the
 * next re-induction + accept supersedes the previous generation in the graph. */
function SynonymsField({
  field,
  placeholder,
}: {
  field: ReturnType<typeof useTokenField>;
  placeholder: string;
}) {
  // `Clear all` unmounts itself when it clears the list (the button is
  // conditional on `items.length > 0`). Without moving focus, keyboard users
  // lose their place. After clear() runs, transfer focus to the synonyms
  // input — the natural next thing to interact with, and it stays mounted.
  const { t } = useAppI18n();
  const inputRef = useRef<InputProps.Ref>(null);
  const handleClear = () => {
    field.clear();
    // Defer so the render that drops the button has settled before focus moves.
    requestAnimationFrame(() => inputRef.current?.focus());
  };
  return (
    <FormField
      label={t("Synonyms")}
      description={t(
        "Press Enter to add. Pre-filled terms may be AI-generated; saving replaces the whole list.",
      )}
      secondaryControl={
        field.items.length > 0 ? (
          <Button
            variant="link"
            iconName="remove"
            onClick={handleClear}
            ariaLabel={t("Clear all synonyms")}
          >
            {t("Clear all")}
          </Button>
        ) : undefined
      }
    >
      <SpaceBetween size="xs">
        <Input
          ref={inputRef}
          value={field.input}
          onChange={({ detail }) => field.setInput(detail.value)}
          onKeyDown={({ detail }) => {
            if (detail.key === "Enter") field.add();
          }}
          placeholder={placeholder}
        />
        <TokenGroup
          items={field.items}
          onDismiss={({ detail }) => field.remove(detail.itemIndex)}
        />
      </SpaceBetween>
    </FormField>
  );
}

// --- Edit Table Modal ---

function EditTableModal({
  businessMetadata,
  tableName,
  onDismiss,
  onSave,
  loading,
}: {
  businessMetadata?: {
    description?: string;
    synonyms?: string[];
    glossaryTerms?: string[];
    tags?: string[];
  };
  tableName: string;
  onDismiss: () => void;
  onSave: (overrides: {
    description: string;
    synonyms: string[];
    glossaryTerms: string[];
    tags: string[];
  }) => void;
  loading: boolean;
}) {
  const { t } = useAppI18n();
  const bm = businessMetadata;
  const [description, setDescription] = useState(bm?.description ?? "");
  const synonyms = useTokenField(bm?.synonyms ?? []);
  const glossary = useTokenField(bm?.glossaryTerms ?? []);
  const tags = useTokenField(bm?.tags ?? []);

  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={t("Edit table: {name}", { name: tableName })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              loading={loading}
              onClick={() =>
                onSave({
                  description,
                  synonyms: synonyms.values,
                  glossaryTerms: glossary.values,
                  tags: tags.values,
                })
              }
            >
              {t("Save")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <FormField label={t("Description")}>
          <Textarea
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            rows={3}
          />
        </FormField>
        <SynonymsField
          field={synonyms}
          placeholder={t("e.g. customers_table")}
        />
        <FormField
          label={t("Glossary terms")}
          description={t("Press Enter to add.")}
        >
          <SpaceBetween size="xs">
            <Input
              value={glossary.input}
              onChange={({ detail }) => glossary.setInput(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === "Enter") glossary.add();
              }}
              placeholder={t("e.g. Customer")}
            />
            <TokenGroup
              items={glossary.items}
              onDismiss={({ detail }) => glossary.remove(detail.itemIndex)}
            />
          </SpaceBetween>
        </FormField>
        <FormField label={t("Tags")} description={t("Press Enter to add.")}>
          <SpaceBetween size="xs">
            <Input
              value={tags.input}
              onChange={({ detail }) => tags.setInput(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === "Enter") tags.add();
              }}
              placeholder={t("e.g. pii")}
            />
            <TokenGroup
              items={tags.items}
              onDismiss={({ detail }) => tags.remove(detail.itemIndex)}
            />
          </SpaceBetween>
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

// --- Edit Column Modal ---

function EditColumnModal({
  column,
  onDismiss,
  onSave,
  loading,
}: {
  column: ColumnMetadata;
  onDismiss: () => void;
  onSave: (overrides: {
    description: string;
    synonyms: string[];
    glossaryTerms: string[];
    tags: string[];
  }) => void;
  loading: boolean;
}) {
  const { t } = useAppI18n();
  const bm = column.businessMetadata;
  const [description, setDescription] = useState(bm?.description ?? "");
  const synonyms = useTokenField(bm?.synonyms ?? []);
  const glossary = useTokenField(bm?.glossaryTerms ?? []);
  const tags = useTokenField(bm?.tags ?? []);

  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={t("Edit column: {name}", { name: column.name ?? "" })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              loading={loading}
              onClick={() =>
                onSave({
                  description,
                  synonyms: synonyms.values,
                  glossaryTerms: glossary.values,
                  tags: tags.values,
                })
              }
            >
              {t("Save & approve")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <FormField label={t("Data type")}>
          <Input value={column.dataType ?? ""} disabled />
        </FormField>
        <FormField label={t("Description")}>
          <Textarea
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            rows={3}
          />
        </FormField>
        <SynonymsField field={synonyms} placeholder={t("e.g. customer_id")} />
        <FormField
          label={t("Glossary terms")}
          description={t("Press Enter to add.")}
        >
          <SpaceBetween size="xs">
            <Input
              value={glossary.input}
              onChange={({ detail }) => glossary.setInput(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === "Enter") glossary.add();
              }}
              placeholder={t("e.g. Customer")}
            />
            <TokenGroup
              items={glossary.items}
              onDismiss={({ detail }) => glossary.remove(detail.itemIndex)}
            />
          </SpaceBetween>
        </FormField>
        <FormField label={t("Tags")} description={t("Press Enter to add.")}>
          <SpaceBetween size="xs">
            <Input
              value={tags.input}
              onChange={({ detail }) => tags.setInput(detail.value)}
              onKeyDown={({ detail }) => {
                if (detail.key === "Enter") tags.add();
              }}
              placeholder={t("e.g. pii")}
            />
            <TokenGroup
              items={tags.items}
              onDismiss={({ detail }) => tags.remove(detail.itemIndex)}
            />
          </SpaceBetween>
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

// --- Edit Keys Modal ---

function EditKeysModal({
  tableName,
  columnNames,
  primaryKey,
  foreignKeys,
  onDismiss,
  onSave,
  loading,
  error,
}: {
  tableName: string;
  columnNames: string[];
  primaryKey: string[];
  foreignKeys: ForeignKeyOutput[];
  onDismiss: () => void;
  onSave: (payload: {
    primaryKey: { columns: string[] };
    foreignKeys: {
      column: string;
      targetTable: string;
      targetColumn?: string;
    }[];
  }) => void;
  loading: boolean;
  error?: Error | null;
}) {
  const { t } = useAppI18n();
  const columnOptions = columnNames.map((c) => ({ label: c, value: c }));
  const [pkSelected, setPkSelected] = useState<readonly SelectProps.Option[]>(
    primaryKey.map((c) => ({ label: c, value: c })),
  );
  const [fkRows, setFkRows] = useState(
    foreignKeys.map((fk) => ({
      column: fk.column ?? "",
      targetTable: fk.targetTable ?? "",
      targetColumn: fk.targetColumn ?? "",
    })),
  );

  function updateRow(index: number, patch: Partial<(typeof fkRows)[number]>) {
    setFkRows(fkRows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={t("Edit keys: {name}", { name: tableName })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              loading={loading}
              onClick={() =>
                onSave({
                  primaryKey: {
                    columns: pkSelected
                      .map((o) => o.value ?? "")
                      .filter(Boolean),
                  },
                  foreignKeys: fkRows
                    .filter((r) => r.column && r.targetTable)
                    .map((r) => ({
                      column: r.column,
                      targetTable: r.targetTable,
                      targetColumn: r.targetColumn || undefined,
                    })),
                })
              }
            >
              {t("Save")}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" header={t("Failed to save keys")}>
            {extractErrorMessage(error, t("An unexpected error occurred."))}
          </Alert>
        )}
        <FormField
          label={t("Primary key columns")}
          description={t("Steward-specified keys override discovered ones.")}
        >
          <Multiselect
            selectedOptions={pkSelected}
            onChange={({ detail }) => setPkSelected(detail.selectedOptions)}
            options={columnOptions}
            placeholder={t("Select primary key columns")}
          />
        </FormField>
        <AttributeEditor
          items={fkRows}
          addButtonText={t("Add foreign key")}
          removeButtonText={t("Remove")}
          empty={t("No foreign keys.")}
          onAddButtonClick={() =>
            setFkRows([
              ...fkRows,
              { column: "", targetTable: "", targetColumn: "" },
            ])
          }
          onRemoveButtonClick={({ detail }) =>
            setFkRows(fkRows.filter((_, i) => i !== detail.itemIndex))
          }
          definition={[
            {
              label: t("Column"),
              control: (item, i) => (
                <Input
                  value={item.column}
                  onChange={({ detail }) =>
                    updateRow(i, { column: detail.value })
                  }
                  placeholder="customer_id"
                />
              ),
            },
            {
              label: t("Target table"),
              control: (item, i) => (
                <Input
                  value={item.targetTable}
                  onChange={({ detail }) =>
                    updateRow(i, { targetTable: detail.value })
                  }
                  placeholder="customers"
                />
              ),
            },
            {
              label: t("Target column"),
              control: (item, i) => (
                <Input
                  value={item.targetColumn}
                  onChange={({ detail }) =>
                    updateRow(i, { targetColumn: detail.value })
                  }
                  placeholder="id"
                />
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Modal>
  );
}
