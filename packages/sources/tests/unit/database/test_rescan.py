# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_sources.database.rescan.diff_tables (comprehensive diff)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
    TechnicalMetadata,
)
from coa_sources.database.connectors.glue_catalog import GlueCatalogConnector
from coa_sources.database.rescan import (
    RescanChangeKind,
    diff_tables,
    merge_rescan_table,
    merged_write_set,
    reconstruct_approved_baseline,
)
from coa_sources.database.rescan_backup import build_rescan_backup

pytestmark = pytest.mark.unit


def _col(
    name: str,
    data_type: str = "int",
    *,
    nullable: bool = True,
    is_partition_key: bool = False,
    description: str = "",
    synonyms: list[str] | None = None,
    distinct_values: list[str] | None = None,
    review_status: str = ReviewStatus.PENDING_REVIEW,
    enrichment_source: str = "",
) -> Column:
    return Column(
        name=name,
        data_type=data_type,
        nullable=nullable,
        is_partition_key=is_partition_key,
        business_metadata=BusinessMetadata(
            description=description,
            synonyms=list(synonyms or []),
            enrichment_source=enrichment_source,
            review_status=review_status,
        ),
        distinct_values=list(distinct_values or []),
    )


def _tbl(
    database: str = "db",
    name: str = "orders",
    *,
    description: str = "",
    enrichment_source: str = "",
    database_description: str = "",
    partition_keys: list[str] | None = None,
    fmt: str = "",
    location: str = "",
    pk: list[str] | None = None,
    pk_source: str = EnrichmentSource.DETERMINISTIC,
    fks: list[ForeignKey] | None = None,
    columns: list[Column] | None = None,
) -> Table:
    columns = columns if columns is not None else [_col("id")]
    return Table(
        name=name,
        database=database,
        database_description=database_description,
        technical_metadata=TechnicalMetadata(
            column_count=len(columns), partition_keys=list(partition_keys or []), format=fmt, location=location
        ),
        business_metadata=BusinessMetadata(description=description, enrichment_source=enrichment_source),
        primary_key=PrimaryKey(columns=list(pk or []), source=pk_source if pk else ""),
        foreign_keys=list(fks or []),
        columns=columns,
    )


def _only_modified(diff):
    assert diff.added == [] and diff.removed == [] and diff.unchanged == []
    assert len(diff.modified) == 1
    return diff.modified[0]


def test_identical_scan_has_no_changes():
    cols = [_col("id"), _col("total", "decimal", description="order total")]
    diff = diff_tables(
        [_tbl(columns=cols, description="orders", pk=["id"])], [_tbl(columns=cols, description="orders", pk=["id"])]
    )
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_added_and_removed_tables():
    diff = diff_tables([_tbl(name="old")], [_tbl(name="new")])
    assert diff.added == ["db.new"]
    assert diff.removed == ["db.old"]
    assert diff.has_changes


def test_column_type_change_is_schema():
    stored = [_tbl(columns=[_col("total", "decimal")])]
    fresh = [_tbl(columns=[_col("total", "varchar")])]
    tc = _only_modified(diff_tables(stored, fresh))
    (col,) = tc.columns
    assert col.status == "modified"
    (fc,) = [f for f in col.fields if f.field == "data_type"]
    assert fc.kind == RescanChangeKind.SCHEMA and fc.old == "decimal" and fc.new == "varchar"


def test_column_nullability_change_is_schema():
    stored = [_tbl(columns=[_col("id", nullable=True)])]
    fresh = [_tbl(columns=[_col("id", nullable=False)])]
    col = _only_modified(diff_tables(stored, fresh)).columns[0]
    assert [f.field for f in col.fields] == ["nullable"]
    assert col.fields[0].kind == RescanChangeKind.SCHEMA


def test_column_partition_flag_change_is_schema():
    stored = [_tbl(columns=[_col("dt", "date", is_partition_key=False)])]
    fresh = [_tbl(columns=[_col("dt", "date", is_partition_key=True)])]
    col = _only_modified(diff_tables(stored, fresh)).columns[0]
    assert [f.field for f in col.fields] == ["is_partition_key"]


def test_column_added_and_removed():
    stored = [_tbl(columns=[_col("id"), _col("legacy")])]
    fresh = [_tbl(columns=[_col("id"), _col("shipped_at", "timestamp")])]
    tc = _only_modified(diff_tables(stored, fresh))
    by_status = {c.status: c.name for c in tc.columns}
    assert by_status == {"added": "shipped_at", "removed": "legacy"}


def test_source_derived_description_change_is_description_kind():
    # A description diff counts only when the accepted value came from the source
    # (DB comment / catalog); then a changed fresh source comment is real drift.
    stored = [_tbl(columns=[_col("id", description="old", enrichment_source=EnrichmentSource.DETERMINISTIC)])]
    fresh = [_tbl(columns=[_col("id", description="new source comment")])]
    col = _only_modified(diff_tables(stored, fresh)).columns[0]
    assert [(f.field, f.kind) for f in col.fields] == [("description", RescanChangeKind.DESCRIPTION)]


def test_ai_description_change_is_not_flagged():
    # Accepted description is AI-authored and the fresh scan has no source comment
    # — not source drift, so the table stays unchanged. This is the no-op re-scan
    # flood the source-derived gate prevents (a re-scan of an approved source
    # whose descriptions are AI/steward text must not reset every column).
    stored = [_tbl(columns=[_col("id", description="AI text", enrichment_source=EnrichmentSource.AI_GENERATED)])]
    fresh = [_tbl(columns=[_col("id", description="")])]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_source_derived_table_description_change_is_description_kind():
    stored = [_tbl(description="old table doc", enrichment_source=EnrichmentSource.DETERMINISTIC)]
    fresh = [_tbl(description="new table doc")]
    tc = _only_modified(diff_tables(stored, fresh))
    assert tc.columns == []
    assert [(f.field, f.kind) for f in tc.table_fields] == [("description", RescanChangeKind.DESCRIPTION)]


def test_ai_table_description_change_is_not_flagged():
    # Table-level counterpart: an AI/steward table description is not diffed, so
    # a fresh empty scan comment does not mark the table modified.
    stored = [_tbl(description="AI: customer orders", enrichment_source=EnrichmentSource.AI_GENERATED)]
    fresh = [_tbl(description="")]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_source_comment_added_since_approval_is_flagged_on_a_column():
    # The other direction of the gate: the accepted description is AI-authored,
    # and the source has now GAINED a real comment. Gating on the accepted
    # provenance alone hid this forever — accepted provenance never changes by
    # itself, so no later re-scan could ever surface the comment either.
    stored = [_tbl(columns=[_col("id", description="AI text", enrichment_source=EnrichmentSource.AI_GENERATED)])]
    fresh = [
        _tbl(columns=[_col("id", description="Order identifier", enrichment_source=EnrichmentSource.DETERMINISTIC)])
    ]
    tc = _only_modified(diff_tables(stored, fresh))
    (col,) = tc.columns
    assert col.status == "modified"
    (fc,) = [f for f in col.fields if f.field == "description"]
    assert fc.kind == RescanChangeKind.DESCRIPTION and fc.old == "AI text" and fc.new == "Order identifier"


def test_source_comment_added_since_approval_is_flagged_on_a_table():
    stored = [_tbl(description="AI: customer orders", enrichment_source=EnrichmentSource.AI_GENERATED)]
    fresh = [_tbl(description="Customer order header", enrichment_source=EnrichmentSource.DETERMINISTIC)]
    tc = _only_modified(diff_tables(stored, fresh))
    assert tc.columns == []
    (fc,) = [f for f in tc.table_fields if f.field == "description"]
    assert fc.kind == RescanChangeKind.DESCRIPTION and fc.new == "Customer order header"


def test_primary_key_change_is_constraint():
    stored = [_tbl(pk=["id"])]
    fresh = [_tbl(pk=["id", "tenant_id"])]
    tc = _only_modified(diff_tables(stored, fresh))
    (fc,) = [f for f in tc.table_fields if f.field == "primary_key"]
    assert fc.kind == RescanChangeKind.CONSTRAINT


def test_source_reported_foreign_key_change_is_constraint():
    # A source-reported (DETERMINISTIC) FK that appears on a re-scan is real drift.
    stored = [_tbl(fks=[])]
    fresh = [
        _tbl(
            fks=[
                ForeignKey(
                    column="cust_id",
                    target_table="customers",
                    target_column="id",
                    source=EnrichmentSource.DETERMINISTIC,
                )
            ]
        )
    ]
    tc = _only_modified(diff_tables(stored, fresh))
    assert [f.field for f in tc.table_fields] == ["foreign_keys"]
    assert tc.table_fields[0].kind == RescanChangeKind.CONSTRAINT


def test_ai_inferred_foreign_key_is_not_diffed():
    # An AI-inferred FK is a guess, not a source-reported constraint; a fresh scan
    # can't reproduce it, so it must NOT register as drift — otherwise every
    # enriched table with an inferred FK flips to pending on a no-op re-scan.
    ai_fk = ForeignKey(column="setcode", target_table="sets", target_column="code", source=EnrichmentSource.AI_INFERRED)
    diff = diff_tables([_tbl(fks=[ai_fk])], [_tbl(fks=[])])
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_ai_inferred_primary_key_is_not_diffed():
    # Same for an AI-inferred PK: excluded on both sides, so it never drifts.
    stored = [_tbl(pk=["cdscode", "year"], pk_source=EnrichmentSource.AI_INFERRED)]
    fresh = [_tbl(pk=[])]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_format_location_partition_keys_are_schema():
    stored = [_tbl(fmt="parquet", location="s3://a", partition_keys=["dt"])]
    fresh = [_tbl(fmt="orc", location="s3://b", partition_keys=["dt", "region"])]
    tc = _only_modified(diff_tables(stored, fresh))
    fields = {f.field: f.kind for f in tc.table_fields}
    assert fields == {
        "format": RescanChangeKind.SCHEMA,
        "location": RescanChangeKind.SCHEMA,
        "partition_keys": RescanChangeKind.SCHEMA,
    }


def test_distinct_values_change_does_not_flag_modified():
    # Sampled distinct values are data churn, not schema/metadata drift, so a
    # change in them must NOT mark a column modified — otherwise a no-op re-scan
    # floods the review and resets curated columns to pending.
    stored = [_tbl(columns=[_col("region", "varchar", distinct_values=["us", "eu"])])]
    fresh = [_tbl(columns=[_col("region", "varchar", distinct_values=["us", "eu", "apac"])])]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


def test_partition_keys_order_independent():
    stored = [_tbl(partition_keys=["dt", "region"])]
    fresh = [_tbl(partition_keys=["region", "dt"])]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]


def test_ai_and_steward_only_fields_are_not_diffed():
    # Synonyms are AI/steward-authored — never in a scan — so a difference in
    # them must NOT register as a source change (only source-owned fields diff).
    stored = [_tbl(columns=[_col("id", description="d", synonyms=["identifier"])])]
    fresh = [_tbl(columns=[_col("id", description="d", synonyms=[])])]
    diff = diff_tables(stored, fresh)
    assert diff.unchanged == ["db.orders"]
    assert not diff.has_changes


# ── merge_rescan_table (re-scan merge onto the accepted/curated version) ──────


def test_merge_preserves_steward_description_and_refreshes_type():
    accepted = _tbl(
        columns=[
            _col(
                "total",
                "decimal",
                description="net order total, steward-authored",
                enrichment_source=EnrichmentSource.STEWARD_EDITED,
                review_status=ReviewStatus.APPROVED,
            )
        ]
    )
    fresh = _tbl(columns=[_col("total", "varchar")])
    (col,) = merge_rescan_table(accepted, fresh, changed_column_names={"total"}).columns
    assert col.data_type == "varchar"  # source shape refreshed
    assert col.business_metadata.description == "net order total, steward-authored"  # curated text kept
    assert col.business_metadata.enrichment_source == EnrichmentSource.STEWARD_EDITED
    assert col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW  # approval cleared for re-review


def test_merge_takes_a_newly_added_source_comment_over_ai_text():
    # Detection alone is not enough: if the merge still copied the accepted text,
    # the steward would see the source comment in the diff and lose it on approve.
    accepted = _tbl(
        columns=[
            _col(
                "id",
                description="AI text",
                enrichment_source=EnrichmentSource.AI_GENERATED,
                review_status=ReviewStatus.APPROVED,
            )
        ]
    )
    fresh = _tbl(columns=[_col("id", description="Order identifier", enrichment_source=EnrichmentSource.DETERMINISTIC)])
    (col,) = merge_rescan_table(accepted, fresh, changed_column_names={"id"}).columns
    assert col.business_metadata.description == "Order identifier"  # source comment lands
    assert col.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC  # provenance follows the text
    assert col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW  # back to review


def test_merge_takes_an_edited_source_comment_over_the_previously_scanned_one():
    # The plainest drift case: the accepted text came from the source's own
    # comment last scan and the owner has since reworded it. Approving must save
    # the new wording, not the one already on file.
    accepted = _tbl(
        columns=[
            _col(
                "id",
                description="order id",
                enrichment_source=EnrichmentSource.DETERMINISTIC,
                review_status=ReviewStatus.APPROVED,
            )
        ]
    )
    fresh = _tbl(
        columns=[_col("id", description="Tenant-scoped order id", enrichment_source=EnrichmentSource.DETERMINISTIC)]
    )
    (col,) = merge_rescan_table(accepted, fresh, changed_column_names={"id"}).columns
    assert col.business_metadata.description == "Tenant-scoped order id"


def test_merge_keeps_a_steward_description_when_the_source_gains_a_comment():
    # A human wrote this text, so it wins over the source. The change is still
    # flagged in the diff, so the steward can adopt the source wording by hand —
    # but the re-scan never overwrites their words for them.
    accepted = _tbl(
        columns=[
            _col(
                "id",
                description="Tenant-scoped order id, steward-authored",
                enrichment_source=EnrichmentSource.STEWARD_EDITED,
                review_status=ReviewStatus.APPROVED,
            )
        ]
    )
    fresh = _tbl(columns=[_col("id", description="Order identifier", enrichment_source=EnrichmentSource.DETERMINISTIC)])
    (col,) = merge_rescan_table(accepted, fresh, changed_column_names={"id"}).columns
    assert col.business_metadata.description == "Tenant-scoped order id, steward-authored"
    assert col.business_metadata.enrichment_source == EnrichmentSource.STEWARD_EDITED
    assert col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW


def test_merge_takes_a_newly_added_source_comment_at_table_level():
    accepted = _tbl(description="AI: customer orders", enrichment_source=EnrichmentSource.AI_GENERATED)
    fresh = _tbl(description="Customer order header", enrichment_source=EnrichmentSource.DETERMINISTIC)
    merged = merge_rescan_table(accepted, fresh, changed_column_names=set())
    assert merged.business_metadata.description == "Customer order header"
    assert merged.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC


def test_merge_keeps_accepted_description_when_the_source_has_no_comment():
    # The flood guard on the merge side: a scan with no comment is not
    # source-derived, so it must not blank out curated text.
    accepted = _tbl(
        columns=[_col("id", description="AI text", enrichment_source=EnrichmentSource.AI_GENERATED)],
        description="AI: customer orders",
        enrichment_source=EnrichmentSource.AI_GENERATED,
    )
    fresh = _tbl(columns=[_col("id")])
    merged = merge_rescan_table(accepted, fresh, changed_column_names={"id"})
    assert merged.business_metadata.description == "AI: customer orders"
    (col,) = merged.columns
    assert col.business_metadata.description == "AI text"
    assert col.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED


def test_merge_unchanged_column_keeps_approval():
    accepted = _tbl(
        columns=[
            _col("id", "int", review_status=ReviewStatus.APPROVED, enrichment_source=EnrichmentSource.DETERMINISTIC),
            _col("total", "decimal", review_status=ReviewStatus.APPROVED),
        ]
    )
    fresh = _tbl(columns=[_col("id", "int"), _col("total", "varchar")])
    by_name = {c.name: c for c in merge_rescan_table(accepted, fresh, changed_column_names={"total"}).columns}
    assert by_name["id"].business_metadata.review_status == ReviewStatus.APPROVED  # unchanged column untouched
    assert by_name["total"].business_metadata.review_status == ReviewStatus.PENDING_REVIEW  # changed column reset


def test_merge_rejected_column_stays_rejected_even_when_changed():
    accepted = _tbl(columns=[_col("legacy", "int", review_status=ReviewStatus.REJECTED)])
    fresh = _tbl(columns=[_col("legacy", "bigint")])
    (col,) = merge_rescan_table(accepted, fresh, changed_column_names={"legacy"}).columns
    assert col.data_type == "bigint"
    assert col.business_metadata.review_status == ReviewStatus.REJECTED  # never resurrected


def test_merge_added_column_taken_as_scanned():
    accepted = _tbl(columns=[_col("id", "int", review_status=ReviewStatus.APPROVED)])
    fresh = _tbl(columns=[_col("id", "int"), _col("shipped_at", "timestamp")])
    by_name = {c.name: c for c in merge_rescan_table(accepted, fresh, changed_column_names=set()).columns}
    assert by_name["shipped_at"].data_type == "timestamp"
    assert by_name["shipped_at"].business_metadata.review_status == ReviewStatus.PENDING_REVIEW


def test_merge_removed_column_carried_over_untouched():
    accepted = _tbl(
        columns=[
            _col("id", "int", review_status=ReviewStatus.APPROVED),
            _col(
                "legacy",
                "int",
                description="steward note",
                enrichment_source=EnrichmentSource.STEWARD_EDITED,
                review_status=ReviewStatus.APPROVED,
            ),
        ]
    )
    fresh = _tbl(columns=[_col("id", "int")])
    by_name = {c.name: c for c in merge_rescan_table(accepted, fresh, changed_column_names=set()).columns}
    assert "legacy" in by_name  # discovery never deletes — carried over
    assert by_name["legacy"].business_metadata.description == "steward note"  # curation preserved
    assert by_name["legacy"].business_metadata.review_status == ReviewStatus.APPROVED  # untouched


def test_merge_table_business_metadata_preserved_technical_refreshed():
    accepted = Table(
        name="orders",
        database="db",
        database_description="old schema doc",
        technical_metadata=TechnicalMetadata(
            column_count=1, partition_keys=["dt"], format="parquet", location="s3://a"
        ),
        business_metadata=BusinessMetadata(
            description="AI: customer orders",
            synonyms=["sales"],
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.APPROVED,
        ),
        columns=[_col("id", "int", review_status=ReviewStatus.APPROVED)],
    )
    fresh = Table(
        name="orders",
        database="db",
        database_description="new schema doc",
        technical_metadata=TechnicalMetadata(
            column_count=1, partition_keys=["dt", "region"], format="orc", location="s3://b"
        ),
        columns=[_col("id", "int")],
    )
    merged = merge_rescan_table(accepted, fresh, changed_column_names=set())
    assert merged.business_metadata.description == "AI: customer orders"  # curated table description kept
    assert merged.business_metadata.synonyms == ["sales"]
    assert merged.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED
    assert merged.business_metadata.review_status == ReviewStatus.PENDING_REVIEW  # changed table always re-reviewed
    assert merged.technical_metadata.format == "orc"  # source shape refreshed
    assert merged.technical_metadata.location == "s3://b"
    assert merged.technical_metadata.partition_keys == ["dt", "region"]
    assert merged.database_description == "new schema doc"


def test_merge_takes_fresh_primary_key_when_present_else_keeps_accepted():
    accepted = _tbl(pk=["id"], fks=[ForeignKey(column="cust_id", target_table="customers", target_column="id")])
    fresh = _tbl(pk=["id", "tenant_id"], fks=[])
    merged = merge_rescan_table(accepted, fresh, changed_column_names=set())
    assert merged.primary_key.columns == ["id", "tenant_id"]  # fresh keys win when the scan reports them
    assert [fk.column for fk in merged.foreign_keys] == ["cust_id"]  # fresh empty -> keep accepted (curated)


# ── merged_write_set (diff → the tables a re-scan writes) ─────────────────────


def test_merged_write_set_writes_merged_modified_and_fresh_added_only():
    accepted = [
        _tbl(
            name="orders",
            columns=[
                _col(
                    "total",
                    "decimal",
                    description="steward note",
                    enrichment_source=EnrichmentSource.STEWARD_EDITED,
                    review_status=ReviewStatus.APPROVED,
                )
            ],
        ),
        _tbl(name="legacy"),  # removed from the source
    ]
    fresh = [
        _tbl(name="orders", columns=[_col("total", "varchar")]),  # modified (type change)
        _tbl(name="new_tbl"),  # added
    ]
    to_write = merged_write_set(diff_tables(accepted, fresh), accepted, fresh)
    by_id = {t.table_id: t for t in to_write}
    assert set(by_id) == {"db.orders", "db.new_tbl"}  # modified + added; unchanged/removed excluded
    (col,) = by_id["db.orders"].columns
    assert col.data_type == "varchar"  # source shape refreshed
    assert col.business_metadata.description == "steward note"  # curated text kept
    assert col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW  # changed -> pending


def test_merged_write_set_empty_when_nothing_changed():
    cols = [_col("id"), _col("total", "decimal", description="d")]
    accepted = [_tbl(columns=cols)]
    fresh = [_tbl(columns=cols)]
    assert merged_write_set(diff_tables(accepted, fresh), accepted, fresh) == []


# ── reconstruct_approved_baseline (baseline = last APPROVED, not last scan) ────
# A re-scan while a prior re-scan is still un-approved (RESCAN_REVIEW) must diff
# against the APPROVED state, not the interim live assets. The backup blob holds
# the approved pre-image; these tests build it with the REAL build_rescan_backup
# so the serialize/deserialize round-trip is exercised end-to-end.


def test_reconstruct_baseline_no_backup_returns_live_unchanged():
    # No backup == source is APPROVED (no open re-scan): live IS the approved
    # baseline, so it is returned unchanged. Both None and {} mean "no backup".
    live = [_tbl(name="orders"), _tbl(name="customers")]
    assert reconstruct_approved_baseline(live, None, source_id="s") == live
    assert reconstruct_approved_baseline(live, {}, source_id="s") == live


def test_reconstruct_baseline_uses_approved_pre_image_for_modified_table():
    # Approved: orders.total is decimal, steward-authored + APPROVED.
    approved = [
        _tbl(
            name="orders",
            columns=[
                _col(
                    "total",
                    "decimal",
                    description="net total (steward)",
                    enrichment_source=EnrichmentSource.STEWARD_EDITED,
                    review_status=ReviewStatus.APPROVED,
                )
            ],
        )
    ]
    # Prior un-approved re-scan changed the type decimal -> varchar. Its backup
    # captured the approved pre-image; its merge wrote the interim (varchar,
    # reset to PENDING) to the live assets.
    fresh1 = [_tbl(name="orders", columns=[_col("total", "varchar")])]
    diff = diff_tables(approved, fresh1)
    backup = build_rescan_backup(diff, approved, source_id="s", scan_job_sk="sk")
    live = merged_write_set(diff, approved, fresh1)

    (orders,) = reconstruct_approved_baseline(live, backup, source_id="s")
    (total,) = orders.columns
    # The baseline is the approved pre-image, NOT the live interim.
    assert total.data_type == "decimal"
    assert total.business_metadata.description == "net total (steward)"
    assert total.business_metadata.review_status == ReviewStatus.APPROVED


def test_reconstruct_baseline_drops_table_added_by_prior_rescan():
    approved = [_tbl(name="orders")]
    # Prior un-approved re-scan added a brand-new table 'newt'.
    fresh1 = [_tbl(name="orders"), _tbl(name="newt")]
    diff = diff_tables(approved, fresh1)
    backup = build_rescan_backup(diff, approved, source_id="s", scan_job_sk="sk")
    assert backup["added_tables"] == ["db.newt"]
    # Live assets after that re-scan: orders (unchanged) + newt (added, un-approved).
    live = [_tbl(name="orders"), _tbl(name="newt")]

    baseline_ids = {t.table_id for t in reconstruct_approved_baseline(live, backup, source_id="s")}
    assert baseline_ids == {"db.orders"}  # 'newt' did not exist at approval -> dropped


def test_reconstruct_baseline_end_to_end_diffs_against_approved_not_interim():
    # Approved orders has only [id].
    approved = [_tbl(name="orders", columns=[_col("id")])]
    # Prior un-approved re-scan added column 'promo_code' (modifies orders).
    fresh1 = [_tbl(name="orders", columns=[_col("id"), _col("promo_code", "varchar")])]
    diff1 = diff_tables(approved, fresh1)
    backup = build_rescan_backup(diff1, approved, source_id="s", scan_job_sk="sk")
    live = merged_write_set(diff1, approved, fresh1)  # interim live: [id, promo_code]

    # Fresh scan #2: promo_code persists (prior change still in the source) AND a
    # genuinely new column 'region' appears.
    fresh2 = [_tbl(name="orders", columns=[_col("id"), _col("promo_code", "varchar"), _col("region", "varchar")])]

    # BUG (diff vs the interim live assets): promo_code is already present in live,
    # so the column added by the prior re-scan is NOT re-flagged — only 'region' is.
    buggy = diff_tables(live, fresh2)
    buggy_added = {c.name for tc in buggy.modified for c in tc.columns if c.status == "added"}
    assert buggy_added == {"region"}

    # FIX (diff vs the reconstructed APPROVED baseline of [id]): BOTH the prior
    # re-scan's promo_code AND the genuinely new region are flagged as changes
    # since approval.
    baseline = reconstruct_approved_baseline(live, backup, source_id="s")
    fixed = diff_tables(baseline, fresh2)
    fixed_added = {c.name for tc in fixed.modified for c in tc.columns if c.status == "added"}
    assert fixed_added == {"promo_code", "region"}


# ── diff over REAL GlueCatalogConnector output ────────────────────────────────
# The generic diff (data-type change, add/remove column, add/remove table,
# unchanged, PK/FK gating, nullability) is already covered above with hand-built
# _tbl/_col. These two add the missing wiring proof: the tables the *actual*
# GlueCatalogConnector emits (via its Glue→Table mapping) flow through
# diff_tables and produce the right added/removed/modified/unchanged partition —
# so a Glue re-scan genuinely detects drift and does not manufacture false
# "modified" from Glue's absent PK/FK or its constant hardcoded nullability.


def _glue_scan(table_list: list[dict]) -> list[Table]:
    """Discover ``table_list`` through the real GlueCatalogConnector (Glue client
    mocked, no AWS) and return the resulting Tables — so the diff runs against
    genuine connector output, not a hand-built stand-in."""
    with patch("coa_sources.database.connectors.glue_catalog.boto3") as mock_boto3:
        mock_client = MagicMock()
        # Return a real Glue-shaped GetDatabase response so ``database_description``
        # is a deterministic string (a bare MagicMock would differ per call and
        # falsely register as drift).
        mock_client.get_database.return_value = {"Database": {"Description": "sales database"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [{"TableList": table_list}]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client
        return GlueCatalogConnector().discover_metadata({"database_name": "sales_db", "region": "us-east-1"}).tables


def _glue_table(
    name: str,
    columns: list[tuple[str, str]],
    *,
    description: str = "",
    comments: dict[str, str] | None = None,
) -> dict:
    """A minimal Glue get_tables entry: (column_name, glue_type) pairs.

    ``description`` is the Glue table-level Description and ``comments`` maps a
    column name to its Glue Comment — the two places a database owner documents a
    table, and the only inputs that make the connector stamp a source-derived
    provenance.
    """
    comments = comments or {}
    return {
        "Name": name,
        "Description": description,
        "StorageDescriptor": {
            "Location": f"s3://bucket/{name}/",
            "Columns": [{"Name": c, "Type": t, "Comment": comments.get(c, "")} for c, t in columns],
        },
        "PartitionKeys": [],
    }


def test_glue_connector_output_drives_diff_added_removed_modified():
    stored = _glue_scan(
        [
            _glue_table("orders", [("order_id", "bigint"), ("amount", "decimal(10,2)")]),
            _glue_table("legacy", [("id", "int")]),
        ]
    )
    fresh = _glue_scan(
        [
            # orders: order_id type changed, region added, amount dropped
            _glue_table("orders", [("order_id", "string"), ("region", "varchar")]),
            _glue_table("new_tbl", [("id", "int")]),
        ]
    )

    diff = diff_tables(stored, fresh)
    assert diff.added == ["sales_db.new_tbl"]  # whole-table add
    assert diff.removed == ["sales_db.legacy"]  # whole-table remove
    assert diff.unchanged == []

    tc = _only_modified_by_id(diff, "sales_db.orders")
    assert {(c.name, c.status) for c in tc.columns} == {
        ("order_id", "modified"),  # data-type change
        ("region", "added"),  # column add
        ("amount", "removed"),  # column drop
    }
    (order_id,) = [c for c in tc.columns if c.name == "order_id"]
    (fc,) = [f for f in order_id.fields if f.field == "data_type"]
    assert fc.kind == RescanChangeKind.SCHEMA and fc.old == "bigint" and fc.new == "string"


def test_glue_connector_identical_rescan_is_unchanged():
    # Re-scanning the same Glue database twice must be a clean no-op: Glue's
    # absent PK/FK and its constant hardcoded nullability must NOT surface as drift.
    table = _glue_table("orders", [("order_id", "bigint"), ("region", "varchar")])
    diff = diff_tables(_glue_scan([table]), _glue_scan([table]))
    assert diff.unchanged == ["sales_db.orders"]
    assert not diff.has_changes


def _only_modified_by_id(diff, table_id: str):
    (tc,) = [t for t in diff.modified if t.table_id == table_id]
    return tc


def test_glue_comment_added_since_approval_flows_through_diff_and_merge():
    # End-to-end on the real connector: the database owner adds a Glue table
    # Description and a column Comment after the source was approved with
    # AI-authored text. Both must be detected AND land on approve.
    stored = _glue_scan([_glue_table("orders", [("order_id", "bigint")])])
    (accepted,) = stored
    accepted.business_metadata = BusinessMetadata(
        description="AI: orders", enrichment_source=EnrichmentSource.AI_GENERATED
    )
    accepted.columns[0].business_metadata = BusinessMetadata(
        description="AI: the order id",
        enrichment_source=EnrichmentSource.AI_GENERATED,
        review_status=ReviewStatus.APPROVED,
    )

    fresh = _glue_scan(
        [
            _glue_table(
                "orders",
                [("order_id", "bigint")],
                description="Customer order header",
                comments={"order_id": "Order identifier"},
            )
        ]
    )
    (fresh_tbl,) = fresh
    # The connector stamped a source-derived provenance because Glue supplied text.
    assert fresh_tbl.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
    assert fresh_tbl.columns[0].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC

    tc = _only_modified_by_id(diff_tables([accepted], fresh), "sales_db.orders")
    assert [f.field for f in tc.table_fields] == ["description"]
    (col,) = tc.columns
    (fc,) = [f for f in col.fields if f.field == "description"]
    assert fc.old == "AI: the order id" and fc.new == "Order identifier"

    merged = merge_rescan_table(accepted, fresh_tbl, changed_column_names={"order_id"})
    assert merged.business_metadata.description == "Customer order header"
    assert merged.columns[0].business_metadata.description == "Order identifier"
    assert merged.columns[0].business_metadata.review_status == ReviewStatus.PENDING_REVIEW
