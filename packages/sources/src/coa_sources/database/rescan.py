# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Re-scan change detection for database sources.

Pure, side-effect-free diff between the tables a source held at its last
accepted scan (reconstructed from the stored DataZone assets) and the tables a
fresh scan just discovered. The result drives the re-scan review: which tables
are new, which disappeared, and — for tables present in both — every
source-owned field that changed.

"All changes" is the contract here: we compare every field a connector
populates from the source, not just the column schema shape. Concretely, per
table matched by ``table_id`` (``database.name``):

  * table-level: description (source comment), database/schema description,
    partition keys, format, location, primary key, foreign keys;
  * per column matched by name: added / removed, and for a match — data type,
    nullability, partition flag, and source description/comment.

Only *source-owned* fields are compared. Synonyms / glossary / tags are
AI- or steward-authored and never appear in a scan, so a re-scan cannot change
them — they are preserved by the writer, never diffed here. A description is
compared only when the accepted value is *source-derived* — a DB comment or a
3rd-party catalog (``enrichment_source`` DETERMINISTIC / CATALOG_EXISTING). An
AI- or steward-authored description is not re-derivable from a scan, so the
fresh scan value (usually empty) is not evidence of drift and is left alone;
diffing it would flag every enriched table as modified on a no-op re-scan.
Primary and foreign keys are gated the same way — only keys the source reported
(DETERMINISTIC / catalog) are compared; ``AI_INFERRED`` keys are guesses a scan
cannot reproduce, so they are not diffed.
``distinct_values`` is a sampled data value (not schema) that drifts as rows
change; it is refreshed on merge but deliberately NOT diffed, so sampled-data
churn does not drown — or manufacture — real schema/description/constraint
changes.

This module is pure — it never touches DataZone, DynamoDB, or the network. Its
only imports beyond the domain models are
:func:`coa_common.datazone_forms.deserialize_form`, itself a pure form-payload
parser, and two Smithy-generated enums, which are plain value types carrying no
behaviour. It reports what differs (:func:`diff_tables`),
produces the merged table a re-scan should write (:func:`merge_rescan_table`),
and reconstructs the last-approved baseline the diff must run against
(:func:`reconstruct_approved_baseline`). The actual I/O (reading the live
assets and the backup blob, writing the merged one, deleting removed assets on
approve) happens in the write path, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coa_common.datazone_forms import deserialize_form
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
)
from coa_control_plane_server.models.rescan_change_kind import RescanChangeKind
from coa_control_plane_server.models.rescan_column_change_status import RescanColumnChangeStatus

# ``RescanChangeKind`` and ``RescanColumnChangeStatus`` are imported above rather
# than restated here: both are returned on the wire by the table-diff endpoint, so
# a local copy would be two sources of truth for one contract. They are used under
# their generated names throughout, so a reader always knows where they come from.
#
# RescanChangeKind members: SCHEMA (data type, nullability, partition flag, format,
# location, partition keys), DESCRIPTION (table or column source comment,
# source-derived only), CONSTRAINT (primary key / foreign keys), and SAMPLED_VALUES.
#
# SAMPLED_VALUES is reserved for distinct-value drift and no producer emits it:
# sampled values are data churn, not a schema/metadata change, and treating them as
# drift floods a no-op re-scan. The member is kept for a planned re-induction signal
# that maps sampled-value drift to a context refresh.


@dataclass
class FieldChange:
    """One source-owned field that differs between accepted and fresh."""

    field: str
    kind: RescanChangeKind
    old: str
    new: str


@dataclass
class ColumnChange:
    """A column that was added, removed, or modified between scans.

    ``fields`` is populated only for ``MODIFIED`` and lists each differing
    source-owned column field.
    """

    name: str
    status: RescanColumnChangeStatus
    fields: list[FieldChange] = field(default_factory=list)


@dataclass
class TableChange:
    """A table present in both scans with at least one source-owned change."""

    table_id: str
    table_fields: list[FieldChange] = field(default_factory=list)
    columns: list[ColumnChange] = field(default_factory=list)


@dataclass
class RescanDiff:
    """Full added / removed / modified / unchanged partition of a re-scan.

    ``added`` / ``removed`` / ``unchanged`` hold ``table_id`` values;
    ``modified`` carries the per-field / per-column breakdown for the review.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[TableChange] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """True when the fresh scan differs from the accepted state in any way."""
        return bool(self.added or self.removed or self.modified)


def _norm_list(values: list[str]) -> str:
    """Order-independent string form of a list, for stable field comparison."""
    return ",".join(sorted(values))


def _fk_key(fk: ForeignKey) -> tuple[str, str, str]:
    return (fk.column, fk.target_table, fk.target_column)


def _norm_fks(fks: list[ForeignKey]) -> str:
    return ";".join(f"{c}->{t}.{tc}" for (c, t, tc) in sorted(_fk_key(fk) for fk in fks))


# Only *source-derived* provenance is diffed on a re-scan: a value the scan read
# from the DB itself (DETERMINISTIC) or from a 3rd-party catalog (CATALOG_EXISTING).
# Anything AI- or steward-authored — descriptions, and AI_INFERRED primary/foreign
# keys — is not re-derivable from a scan, so a fresh scan won't reproduce it;
# diffing it would flag every enriched table as "modified" on a no-op re-scan and
# reset its curated review to PENDING. So a description or a key constraint is
# compared only when its provenance is one of these.
_SOURCE_DERIVED_PROVENANCE = frozenset({EnrichmentSource.DETERMINISTIC, EnrichmentSource.CATALOG_EXISTING})


def _source_derived_pk(pk: PrimaryKey) -> list[str]:
    """PK columns to diff — only when the source reported the key.

    An ``AI_INFERRED`` primary key is a guess a fresh scan cannot reproduce, so it
    is excluded (treated as "no PK") on both sides and never registers as drift.
    """
    return list(pk.columns) if pk.source in _SOURCE_DERIVED_PROVENANCE else []


def _source_derived_fks(fks: list[ForeignKey]) -> list[ForeignKey]:
    """Foreign keys to diff — only those the source reported (drop ``AI_INFERRED``)."""
    return [fk for fk in fks if fk.source in _SOURCE_DERIVED_PROVENANCE]


def _diff_description(stored: BusinessMetadata, fresh: BusinessMetadata) -> bool:
    """Whether a description is worth diffing, from either side's provenance.

    Two directions matter, and gating on ``stored`` alone only catches the first:

    * **stored is source-derived** — the accepted value came from the DB comment or
      catalog, so a differing fresh value is real drift in the source.
    * **fresh is source-derived** — the source has *gained* a comment since the last
      scan, on a table whose accepted description is AI- or steward-authored. Gating
      on stored alone skipped this forever: stored provenance never changes by
      itself, so the real comment could never surface on any later re-scan either.

    Neither side source-derived means neither value came from the source, so a
    difference is not drift — that is the no-op re-scan flood the gate prevents, and
    it stays prevented: a connector only sets a source-derived provenance when the
    source actually supplied a comment, so a fresh scan with no comment is not
    source-derived and does not open this branch.
    """
    return (
        stored.enrichment_source in _SOURCE_DERIVED_PROVENANCE or fresh.enrichment_source in _SOURCE_DERIVED_PROVENANCE
    )


def _diff_table_fields(stored: Table, fresh: Table) -> list[FieldChange]:
    """Compare every source-owned table-level field."""
    changes: list[FieldChange] = []

    def add(field_name: str, kind: RescanChangeKind, old: str, new: str) -> None:
        if old != new:
            changes.append(FieldChange(field=field_name, kind=kind, old=old, new=new))

    if _diff_description(stored.business_metadata, fresh.business_metadata):
        add(
            "description",
            RescanChangeKind.DESCRIPTION,
            stored.business_metadata.description,
            fresh.business_metadata.description,
        )
    add("database_description", RescanChangeKind.DESCRIPTION, stored.database_description, fresh.database_description)
    add("format", RescanChangeKind.SCHEMA, stored.technical_metadata.format, fresh.technical_metadata.format)
    add("location", RescanChangeKind.SCHEMA, stored.technical_metadata.location, fresh.technical_metadata.location)
    add(
        "partition_keys",
        RescanChangeKind.SCHEMA,
        _norm_list(stored.technical_metadata.partition_keys),
        _norm_list(fresh.technical_metadata.partition_keys),
    )
    add(
        "primary_key",
        RescanChangeKind.CONSTRAINT,
        _norm_list(_source_derived_pk(stored.primary_key)),
        _norm_list(_source_derived_pk(fresh.primary_key)),
    )
    add(
        "foreign_keys",
        RescanChangeKind.CONSTRAINT,
        _norm_fks(_source_derived_fks(stored.foreign_keys)),
        _norm_fks(_source_derived_fks(fresh.foreign_keys)),
    )
    return changes


def _diff_one_column(stored: Column, fresh: Column) -> list[FieldChange]:
    """Compare every source-owned field of a column present in both scans."""
    changes: list[FieldChange] = []

    def add(field_name: str, kind: RescanChangeKind, old: str, new: str) -> None:
        if old != new:
            changes.append(FieldChange(field=field_name, kind=kind, old=old, new=new))

    add("data_type", RescanChangeKind.SCHEMA, stored.data_type, fresh.data_type)
    add("nullable", RescanChangeKind.SCHEMA, str(stored.nullable), str(fresh.nullable))
    add("is_partition_key", RescanChangeKind.SCHEMA, str(stored.is_partition_key), str(fresh.is_partition_key))
    if _diff_description(stored.business_metadata, fresh.business_metadata):
        add(
            "description",
            RescanChangeKind.DESCRIPTION,
            stored.business_metadata.description,
            fresh.business_metadata.description,
        )
    # ``distinct_values`` is sampled data (not schema/metadata): it drifts as rows
    # change and is not evidence the source definition changed. It is refreshed on
    # merge but deliberately NOT diffed, so sampled-data churn never flags a column
    # modified (which would reset its curation to PENDING). See RescanChangeKind.SAMPLED_VALUES.
    return changes


def _diff_columns(stored: Table, fresh: Table) -> list[ColumnChange]:
    """Column-level add / remove / modify between two versions of a table."""
    stored_by_name = {c.name: c for c in stored.columns}
    fresh_by_name = {c.name: c for c in fresh.columns}
    stored_names = set(stored_by_name)
    fresh_names = set(fresh_by_name)

    changes: list[ColumnChange] = []
    for name in sorted(fresh_names - stored_names):
        changes.append(ColumnChange(name=name, status=RescanColumnChangeStatus.ADDED))
    for name in sorted(stored_names - fresh_names):
        changes.append(ColumnChange(name=name, status=RescanColumnChangeStatus.REMOVED))
    for name in sorted(stored_names & fresh_names):
        field_changes = _diff_one_column(stored_by_name[name], fresh_by_name[name])
        if field_changes:
            changes.append(ColumnChange(name=name, status=RescanColumnChangeStatus.MODIFIED, fields=field_changes))
    return changes


def diff_tables(stored: list[Table], fresh: list[Table]) -> RescanDiff:
    """Partition a re-scan into added / removed / modified / unchanged tables.

    Args:
        stored: tables reconstructed from the source's existing (accepted)
            DataZone assets — the last-scanned state.
        fresh: tables the current scan discovered.

    Matching is by ``table_id`` (``database.name``); a rename therefore shows as
    a removal plus an addition, never an in-place modification (a name-based
    diff cannot prove two differently named tables are the same one). A table is
    ``modified`` if ANY source-owned table-level field or ANY column changed.
    """
    stored_by_id = {t.table_id: t for t in stored}
    fresh_by_id = {t.table_id: t for t in fresh}
    stored_ids = set(stored_by_id)
    fresh_ids = set(fresh_by_id)

    modified: list[TableChange] = []
    unchanged: list[str] = []
    for table_id in sorted(stored_ids & fresh_ids):
        stored_table = stored_by_id[table_id]
        fresh_table = fresh_by_id[table_id]
        table_fields = _diff_table_fields(stored_table, fresh_table)
        column_changes = _diff_columns(stored_table, fresh_table)
        if table_fields or column_changes:
            modified.append(TableChange(table_id=table_id, table_fields=table_fields, columns=column_changes))
        else:
            unchanged.append(table_id)

    return RescanDiff(
        added=sorted(fresh_ids - stored_ids),
        removed=sorted(stored_ids - fresh_ids),
        modified=modified,
        unchanged=unchanged,
    )


def _carry_curated_status(bm: BusinessMetadata, fresh: BusinessMetadata, *, changed: bool) -> BusinessMetadata:
    """Carry curated business metadata over, clearing approval to PENDING iff changed.

    Synonyms / glossary / tags / confidence are always preserved exactly — a
    re-scan never drops steward or AI curation, it only decides whether the
    *approval* still stands. A REJECTED item stays REJECTED (never resurrected);
    an unchanged item keeps its status; a changed item drops back to
    PENDING_REVIEW so the steward re-confirms it.

    The description is the one field the fresh scan can win, when the source
    itself documents the item: a comment the source supplied — newly added, or
    edited since approval — becomes the value and carries its provenance. Without
    this the description would only ever appear in the review diff and then be
    discarded on approve, leaving the accepted asset stale.

    Two guards bound it:

    * **fresh must be source-derived.** A connector only stamps that provenance
      when the source actually supplied a comment, so a scan of an undocumented
      column cannot blank out accepted AI or steward text.
    * **a ``STEWARD_EDITED`` description is never overwritten.** A human wrote it,
      so it wins over the source. The difference is still detected and shown as
      old -> new in the review diff, which lets the steward take the source
      wording deliberately rather than having their own words replaced behind
      their back.
    """
    take_fresh = (
        fresh.enrichment_source in _SOURCE_DERIVED_PROVENANCE
        and bm.enrichment_source != EnrichmentSource.STEWARD_EDITED
    )
    if changed and bm.review_status != ReviewStatus.REJECTED:
        new_status: str = ReviewStatus.PENDING_REVIEW
    else:
        new_status = bm.review_status
    return BusinessMetadata(
        description=fresh.description if take_fresh else bm.description,
        synonyms=list(bm.synonyms),
        glossary_terms=list(bm.glossary_terms),
        tags=list(bm.tags),
        enrichment_source=fresh.enrichment_source if take_fresh else bm.enrichment_source,
        review_status=new_status,
        confidence=fresh.confidence if take_fresh else bm.confidence,
    )


def merge_rescan_table(accepted: Table, fresh: Table, *, changed_column_names: set[str]) -> Table:
    """Merge a freshly-scanned CHANGED table onto its accepted (curated) version.

    Used when re-scanning an approved source. The fresh scan is authoritative
    for the source-owned *shape* (column set, data types, nullability, partition
    flags, format / location / partition keys, database description); the
    accepted asset is authoritative for *curated* business metadata (steward
    edits, approved AI text, synonyms, glossary, tags). We refresh the shape,
    carry the curation over, and reset review status only for the items that
    actually changed — never dropping curated text, only clearing its approval
    until the steward re-confirms it against the new shape.

    Args:
        accepted: the table reconstructed from the current (approved) asset.
        fresh: the table the re-scan just discovered.
        changed_column_names: names of columns flagged ``modified`` by
            :func:`diff_tables` (added/removed are derived here from the two
            column sets, so only the *modified* set is needed).

    Rules:
      * table technical metadata and ``database_description`` come from ``fresh``;
      * table business metadata is carried from ``accepted`` with review status
        reset to PENDING_REVIEW (a changed table always needs re-review), unless
        it was REJECTED (stays REJECTED). The one exception is the description:
        a comment the source has newly supplied replaces AI-authored or absent
        text, but never a steward's — see :func:`_carry_curated_status`;
      * primary/foreign keys come from ``fresh`` when the scan reports them
        (source-owned), otherwise the accepted (curated) keys are kept;
      * a column present in both: fresh type / nullability / partition flag /
        sampled distinct values, accepted business metadata (same description
        rule), review status reset only if the column is in
        ``changed_column_names``;
      * a column only in ``fresh`` (added): taken as scanned (PENDING by default);
      * a column only in ``accepted`` (removed from the source): carried over
        unchanged. Discovery never deletes — flagging a removed column and
        deleting it live at the approve step, not here.
    """
    accepted_cols = {c.name: c for c in accepted.columns}
    fresh_names = {c.name for c in fresh.columns}
    merged_columns: list[Column] = []

    for fcol in fresh.columns:
        acc = accepted_cols.get(fcol.name)
        if acc is None:
            merged_columns.append(fcol)  # added column — take as scanned
            continue
        merged_columns.append(
            Column(
                name=fcol.name,
                data_type=fcol.data_type,
                nullable=fcol.nullable,
                is_partition_key=fcol.is_partition_key,
                business_metadata=_carry_curated_status(
                    acc.business_metadata,
                    fcol.business_metadata,
                    changed=fcol.name in changed_column_names,
                ),
                distinct_values=list(fcol.distinct_values),
            )
        )

    # Columns removed from the source: carry the accepted column over untouched
    # (deletion is deferred to approve). Preserves curated metadata meanwhile.
    for acc in accepted.columns:
        if acc.name not in fresh_names:
            merged_columns.append(acc)

    return Table(
        name=fresh.name,
        database=fresh.database,
        data_source_id=accepted.data_source_id or fresh.data_source_id,
        namespace_id=accepted.namespace_id or fresh.namespace_id,
        database_description=fresh.database_description,
        technical_metadata=fresh.technical_metadata,
        business_metadata=_carry_curated_status(accepted.business_metadata, fresh.business_metadata, changed=True),
        primary_key=fresh.primary_key if fresh.primary_key.columns else accepted.primary_key,
        foreign_keys=fresh.foreign_keys if fresh.foreign_keys else accepted.foreign_keys,
        columns=merged_columns,
    )


def merged_write_set(diff: RescanDiff, accepted: list[Table], fresh: list[Table]) -> list[Table]:
    """The tables a re-scan should WRITE, given a computed ``diff``.

    Returns the merged version of each *modified* table (curated metadata kept,
    source shape refreshed — see :func:`merge_rescan_table`) plus each *added*
    table as freshly scanned. *Unchanged* tables are omitted so their live asset
    is left exactly as-is, and *removed* tables are omitted too — a re-scan never
    deletes; a removed table's asset stays put until the steward approves the
    removal. Match is by ``table_id``.
    """
    accepted_by_id = {t.table_id: t for t in accepted}
    fresh_by_id = {t.table_id: t for t in fresh}

    to_write: list[Table] = []
    for change in diff.modified:
        changed_columns = {c.name for c in change.columns if c.status == RescanColumnChangeStatus.MODIFIED}
        to_write.append(
            merge_rescan_table(
                accepted_by_id[change.table_id],
                fresh_by_id[change.table_id],
                changed_column_names=changed_columns,
            )
        )
    for table_id in diff.added:
        to_write.append(fresh_by_id[table_id])
    return to_write


def reconstruct_approved_baseline(live: list[Table], backup: dict[str, Any] | None, *, source_id: str) -> list[Table]:
    """Reconstruct the last-APPROVED table set from the live assets + backup blob.

    WHY: a re-scan review must show "changes since the last APPROVED scan", not
    since the last *scan*. The live DataZone assets equal the approved state only
    when the source is APPROVED. While a prior re-scan sits un-approved in
    RESCAN_REVIEW, the live assets are that interim merge, so diffing a fresh scan
    against them hides every change the prior (still un-approved) re-scan
    introduced. The re-scan backup blob is the durable record of the approved
    pre-image (written by discovery before it overwrites anything, cleared by the
    worker on approve/reject), so its PRESENCE means an un-approved re-scan is
    open. This overlays that pre-image back onto the live set to recover the
    approved baseline the diff/merge/new-backup must all run against.

    Overlay rules:
      * ``modified_backup`` — restore each table to its approved (pre-rescan)
        form, undoing the interim merge;
      * ``added_tables`` — drop tables the prior re-scan created; they did not
        exist at approval;
      * every other live table is kept as-is — a table unchanged since approval
        is still approved, and a prior re-scan's ``removed_tables`` were left in
        place and remain approved until the removal is confirmed.

    Interim un-approved edits made during the open review are intentionally NOT
    preserved: the baseline is the last approval, and a fresh re-scan re-derives
    the change-set from there.

    Args:
        live: tables reconstructed from the source's current live DataZone assets.
        backup: the current re-scan backup blob (see ``rescan_backup``), or
            None/empty when the source is APPROVED (no open re-scan) — then
            ``live`` already IS the approved baseline and is returned unchanged.
        source_id: owning source id, threaded into ``deserialize_form``.

    Returns:
        The tables as they stood at the last approval. Pure — no I/O.
    """
    if not backup:
        return list(live)

    modified_backup: dict[str, Any] = backup.get("modified_backup") or {}
    added_tables = set(backup.get("added_tables") or [])

    baseline: dict[str, Table] = {t.table_id: t for t in live if t.table_id not in added_tables}
    for table_id, payload in modified_backup.items():
        baseline[table_id] = deserialize_form(payload, data_source_id=source_id)

    return list(baseline.values())
