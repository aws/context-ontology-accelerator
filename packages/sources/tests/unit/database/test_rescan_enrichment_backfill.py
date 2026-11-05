# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A re-scan sends approved tables that have no metadata back through enrichment.

Without this, a table approved with no description, synonyms, glossary terms or
tags (approved while enrichment was off, or one an earlier enrichment run failed
on) stayed empty on every
re-scan, because a re-scan keeps approved items as they are and enrichment only
fills PENDING_REVIEW items. Curated tables and columns must not change at all.
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest
from coa_common.bedrock import BedrockInvocationResult
from coa_common.datazone_forms import deserialize_form
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    PrimaryKey,
    ReviewStatus,
    Table,
)
from coa_sources.database.connectors.base import ConnectionTestResult
from coa_sources.database.rescan import (
    diff_tables,
    mark_for_enrichment_backfill,
    needs_enrichment_backfill,
)
from coa_sources.database.rescan_backup import build_rescan_backup

MODULE = "coa_sources.database.pipeline.discovery_handler"
ENRICHER = "coa_sources.database.enrichment.table_enricher"

APPROVED = ReviewStatus.APPROVED
PENDING = ReviewStatus.PENDING_REVIEW


def _approved(**kwargs: object) -> BusinessMetadata:
    return BusinessMetadata(review_status=APPROVED, **kwargs)  # type: ignore[arg-type]


def _curated() -> BusinessMetadata:
    return _approved(description="Curated text", synonyms=["kept"], enrichment_source=EnrichmentSource.AI_GENERATED)


def _empty_table(name: str = "customers", *, columns: list[Column] | None = None) -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id="DS#ds-1",
        business_metadata=_approved(),
        columns=columns if columns is not None else [Column(name="id", data_type="int", business_metadata=_approved())],
    )


def _curated_table(name: str = "orders") -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id="DS#ds-1",
        business_metadata=_curated(),
        columns=[
            Column(name="id", data_type="int", business_metadata=_curated()),
            # An empty column inside a curated table stays as it is.
            Column(name="note", data_type="string", business_metadata=_approved()),
        ],
    )


def _scanned(table: Table) -> Table:
    """What a fresh scan returns for ``table``: same shape, no business metadata."""
    return Table(
        name=table.name,
        database=table.database,
        columns=[Column(name=c.name, data_type=c.data_type) for c in table.columns],
    )


# ── needs_enrichment_backfill ──────────────────────────────────────────


class TestNeedsEnrichmentBackfill:
    def test_approved_table_with_no_metadata_qualifies(self) -> None:
        assert needs_enrichment_backfill(_empty_table())

    @pytest.mark.parametrize(
        "bm",
        [
            _approved(description="Has a description"),
            _approved(synonyms=["client"]),
            _approved(tags=["pii"]),
            _approved(glossary_terms=["customer"]),
            # A steward saved it, even if empty: that is a decision, not a gap.
            _approved(enrichment_source=EnrichmentSource.STEWARD_EDITED),
            _approved(enrichment_source=EnrichmentSource.STEWARD_SPECIFIED),
        ],
    )
    def test_any_table_level_metadata_disqualifies(self, bm: BusinessMetadata) -> None:
        table = _empty_table()
        table.business_metadata = bm
        assert not needs_enrichment_backfill(table)

    @pytest.mark.parametrize("status", [ReviewStatus.REJECTED, ReviewStatus.PENDING_REVIEW])
    def test_only_approved_tables_qualify(self, status: str) -> None:
        table = _empty_table()
        table.business_metadata.review_status = status
        assert not needs_enrichment_backfill(table)

    @pytest.mark.parametrize(
        "source", [EnrichmentSource.AI_INFERRED, EnrichmentSource.STEWARD_SPECIFIED, EnrichmentSource.CATALOG_EXISTING]
    )
    def test_a_key_enrichment_would_replace_disqualifies(self, source: str) -> None:
        table = _empty_table()
        table.primary_key = PrimaryKey(columns=["id"], source=source)
        assert not needs_enrichment_backfill(table)

    def test_a_key_discovered_from_the_database_is_kept_by_enrichment_so_it_qualifies(self) -> None:
        table = _empty_table()
        table.primary_key = PrimaryKey(columns=["id"], source=EnrichmentSource.DETERMINISTIC)
        assert needs_enrichment_backfill(table)

    def test_a_curated_column_does_not_disqualify_an_empty_table(self) -> None:
        table = _empty_table(columns=[Column(name="id", data_type="int", business_metadata=_curated())])
        assert needs_enrichment_backfill(table)


# ── mark_for_enrichment_backfill ───────────────────────────────────────


class TestMarkForEnrichmentBackfill:
    def test_resets_the_table_and_its_empty_columns_only(self) -> None:
        curated_col = Column(name="code", data_type="string", business_metadata=_curated())
        rejected_col = Column(
            name="legacy", data_type="string", business_metadata=BusinessMetadata(review_status=ReviewStatus.REJECTED)
        )
        table = _empty_table(
            columns=[Column(name="id", data_type="int", business_metadata=_approved()), curated_col, rejected_col]
        )
        before = copy.deepcopy(table)

        marked = mark_for_enrichment_backfill(table)

        assert marked.business_metadata.review_status == PENDING
        by_name = {c.name: c for c in marked.columns}
        assert by_name["id"].business_metadata.review_status == PENDING
        # Curated and rejected columns are carried over exactly.
        assert by_name["code"] == curated_col
        assert by_name["legacy"] == rejected_col
        # The input is untouched: discovery backs it up as the approved pre-image.
        assert table == before


# ── build_rescan_backup ────────────────────────────────────────────────


class TestBackupOfBackfilledTables:
    def test_backfilled_table_is_backed_up_as_its_approved_form(self) -> None:
        empty = _empty_table()
        diff = diff_tables([empty], [_scanned(empty)])
        assert diff.unchanged == [empty.table_id]

        backup = build_rescan_backup(
            diff, [empty], source_id="ds-1", scan_job_sk="sk", backfilled_tables=[empty.table_id]
        )

        restored = deserialize_form(backup["modified_backup"][empty.table_id], data_source_id="ds-1")
        assert restored.business_metadata.review_status == APPROVED
        assert restored.columns[0].business_metadata.review_status == APPROVED
        # Not a change the steward has to approve a deletion for.
        assert backup["added_tables"] == [] and backup["removed_tables"] == []

    def test_a_backfilled_table_with_no_approved_form_fails_instead_of_being_dropped(self) -> None:
        empty = _empty_table()
        diff = diff_tables([], [_scanned(empty)])

        # Its live asset is about to be rewritten as PENDING; without an approved
        # form in the backup a reject could not restore it.
        with pytest.raises(ValueError, match=empty.table_id):
            build_rescan_backup(diff, [], source_id="ds-1", scan_job_sk="sk", backfilled_tables=[empty.table_id])


# ── discovery handler ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_TABLE", "test-datasources")
    monkeypatch.setenv("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
    monkeypatch.setenv("SMUS_DOMAIN_ID", "dz-test-domain")
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def reset_singletons() -> object:
    import coa_sources.database.pipeline.discovery_handler as mod

    mod._ds_dao = mod._scan_dao = mod._ns_dao = None
    yield
    mod._ds_dao = mod._scan_dao = mod._ns_dao = None


_EVENT = {
    "datasourceId": "DS#ds-1",
    "scanJobId": "SCAN#s",
    "scanJobSK": "2026-10-30T00:00:00.000000Z",
    "namespaceId": "ns-1",
    "scanType": "full",
    "isRescan": True,
}


def _run_rescan(
    live: list[Table],
    fresh: list[Table],
    *,
    enrichment_enabled: bool = True,
    open_review_backup: dict | None = None,
    tables_approved: int | None = None,
) -> dict:
    """Run a re-scan with everything outside the handler mocked.

    From APPROVED by default. With ``open_review_backup``, a re-scan while that
    earlier re-scan's review is still open: ``live`` is then what the earlier run
    wrote, and the backup is the approved pre-image.
    """
    from coa_sources.database.pipeline.discovery_handler import handler

    row = {
        "sourceSubType": "GLUE_DATABASE",
        "configuration": {"databaseName": "sales", "catalogId": "123456789012", "region": "us-east-1"},
        "metadataEnrichmentEnabled": enrichment_enabled,
        "tablesApproved": len(live) if tables_approved is None else tables_approved,
    }
    event = dict(_EVENT)
    if open_review_backup is not None:
        event["hadOpenRescan"] = "true"
    ds_dao = MagicMock()
    ds_dao.get.return_value = row
    connector = MagicMock()
    connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
    connector.discover_metadata.return_value = DiscoveredMetadata(tables=fresh)
    with (
        patch(f"{MODULE}.BUCKET_NAME", "test-bucket"),
        patch(f"{MODULE}.get_connector", return_value=connector),
        patch(f"{MODULE}._get_ds_dao", return_value=ds_dao),
        patch(f"{MODULE}._get_scan_dao", return_value=MagicMock()),
        patch(f"{MODULE}._get_ns_dao", return_value=MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "p"}))),
        patch(f"{MODULE}.read_assets_for_datasource", return_value=copy.deepcopy(live)),
        patch(f"{MODULE}.write_to_datazone", return_value={"assets_created": 0}) as write,
        patch(f"{MODULE}.get_s3_client"),
        patch(f"{MODULE}.upload_json") as upload,
        patch(f"{MODULE}._read_existing_backup", return_value=copy.deepcopy(open_review_backup)),
    ):
        result = handler(event, None)
    summary = [
        c.kwargs["update_fields"]
        for c in ds_dao.update.call_args_list
        if "tablesApproved" in c.kwargs.get("update_fields", {})
    ]
    return {
        "result": result,
        "written": {t.table_id: t for t in write.call_args.kwargs["metadata"].tables},
        "backup": upload.call_args.args[3] if upload.called else None,
        "tables_approved": summary[-1]["tablesApproved"],
    }


class TestDiscoveryBackfill:
    def test_unchanged_empty_approved_table_goes_back_to_review(self) -> None:
        empty, curated = _empty_table(), _curated_table()
        out = _run_rescan([empty, curated], [_scanned(empty), _scanned(curated)])

        written = out["written"]
        # The empty table is written with its items PENDING, so enrichment fills it.
        assert set(written) == {empty.table_id}
        assert written[empty.table_id].business_metadata.review_status == PENDING
        assert written[empty.table_id].columns[0].business_metadata.review_status == PENDING
        # The source ends in RESCAN_REVIEW, and a reject can restore the approved form.
        assert out["result"]["reviewNeeded"] == "true"
        assert empty.table_id in out["backup"]["modified_backup"]
        assert curated.table_id not in out["backup"]["modified_backup"]
        # Only the curated table still counts as approved.
        assert out["tables_approved"] == 1

    def test_curated_tables_are_left_alone_and_need_no_review(self) -> None:
        curated = _curated_table()
        out = _run_rescan([curated], [_scanned(curated)])

        # Not rewritten at all, even though one of its columns is empty.
        assert out["written"] == {}
        assert out["result"]["reviewNeeded"] == "false"
        assert out["backup"] is None
        assert out["tables_approved"] == 1

    def test_nothing_is_backfilled_when_enrichment_is_off(self) -> None:
        empty = _empty_table()
        out = _run_rescan([empty], [_scanned(empty)], enrichment_enabled=False)

        assert out["written"] == {}
        assert out["result"]["reviewNeeded"] == "false"

    def test_a_modified_empty_table_also_gets_its_unchanged_empty_columns_filled(self) -> None:
        empty = _empty_table(
            columns=[
                Column(name="id", data_type="int", business_metadata=_approved()),
                Column(name="code", data_type="string", business_metadata=_curated()),
            ]
        )
        fresh = _scanned(empty)
        fresh.columns.append(Column(name="added", data_type="string"))  # drift
        out = _run_rescan([empty], [fresh])

        table = out["written"][empty.table_id]
        by_name = {c.name: c for c in table.columns}
        assert table.business_metadata.review_status == PENDING
        # The source drift is kept: the backfill is applied on top of the merge.
        assert "added" in by_name
        assert by_name["added"].data_type == fresh.columns[-1].data_type
        assert by_name["added"].business_metadata.review_status == PENDING
        # Without the backfill this unchanged empty column stayed APPROVED and empty.
        assert by_name["id"].business_metadata.review_status == PENDING
        assert by_name["code"].business_metadata == _curated()
        # A reject restores the approved table as it was before the re-scan.
        pre_image = deserialize_form(out["backup"]["modified_backup"][empty.table_id], data_source_id="ds-1")
        assert [c.name for c in pre_image.columns] == ["id", "code"]
        assert pre_image.business_metadata.review_status == APPROVED
        assert pre_image.columns[0].business_metadata.review_status == APPROVED

    def test_a_second_rescan_during_the_open_review_backfills_the_table_again(self) -> None:
        empty, curated = _empty_table(), _curated_table()
        fresh = [_scanned(empty), _scanned(curated)]
        first = _run_rescan([empty, curated], fresh)
        # The live assets during that review: the backfilled table as written, PENDING.
        live = [first["written"][empty.table_id], copy.deepcopy(curated)]
        assert live[0].business_metadata.review_status == PENDING

        second = _run_rescan(live, fresh, open_review_backup=first["backup"], tables_approved=first["tables_approved"])

        # Rebuilt from the backup, the table is approved and empty again, so it is backfilled again.
        written = second["written"][empty.table_id]
        assert written.business_metadata.review_status == PENDING
        assert written.columns[0].business_metadata.review_status == PENDING
        assert second["result"]["reviewNeeded"] == "true"
        # The reject pre-image is still the approved form, not the PENDING one.
        pre_image = deserialize_form(second["backup"]["modified_backup"][empty.table_id], data_source_id="ds-1")
        assert pre_image.business_metadata.review_status == APPROVED
        assert pre_image.columns[0].business_metadata.review_status == APPROVED
        assert curated.table_id not in second["backup"]["modified_backup"]
        assert second["tables_approved"] == first["tables_approved"] == 1


# ── enrichment over what discovery wrote ───────────────────────────────


def _bedrock_result() -> BedrockInvocationResult:
    return BedrockInvocationResult(
        result={
            "table": {"description": "AI table", "synonyms": ["ai"], "glossary_terms": [], "tags": ["ai"]},
            "primary_key": {"columns": [], "confidence": 0.0},
            "columns": [
                {"name": "id", "description": "AI id", "synonyms": [], "glossary_terms": [], "tags": []},
                {"name": "code", "description": "AI code", "synonyms": [], "glossary_terms": [], "tags": []},
                {"name": "note", "description": "AI note", "synonyms": [], "glossary_terms": [], "tags": []},
            ],
        },
        input_tokens=1,
        output_tokens=1,
        latency_ms=1.0,
    )


class TestEnrichmentFillsBackfilledTables:
    @patch(f"{ENRICHER}._write_enriched_assets")
    @patch(f"{ENRICHER}.read_assets_for_datasource")
    @patch(f"{ENRICHER}.BedrockClient")
    def test_empty_items_are_filled_for_review_and_curated_items_are_untouched(
        self, client_cls: MagicMock, read: MagicMock, write: MagicMock
    ) -> None:
        from coa_sources.database.enrichment.table_enricher import run

        empty = _empty_table(
            columns=[
                Column(name="id", data_type="int", business_metadata=_approved()),
                Column(name="code", data_type="string", business_metadata=_curated()),
            ]
        )
        curated = _curated_table()
        # The live assets after the re-scan's discovery step.
        read.return_value = [mark_for_enrichment_backfill(empty), copy.deepcopy(curated)]
        client_cls.return_value.invoke.return_value = _bedrock_result()

        result = run("DS#ds-1", "proj", "dom", "full", emitter=MagicMock(), namespace_language="en")

        (enriched,) = write.call_args.args[0]
        assert result["tables_enriched"] == 1 and result["tables_skipped_unchanged"] == 1
        assert enriched.table_id == empty.table_id
        # New AI metadata, waiting for the steward like after a first scan.
        assert enriched.business_metadata.description == "AI table"
        assert enriched.business_metadata.review_status == PENDING
        by_name = {c.name: c for c in enriched.columns}
        assert by_name["id"].business_metadata.description == "AI id"
        assert by_name["id"].business_metadata.review_status == PENDING
        # The curated column inside the backfilled table is byte-for-byte the same.
        assert by_name["code"].business_metadata == _curated()
        # The curated table was skipped: neither rewritten nor changed in memory.
        assert read.return_value[1] == curated
