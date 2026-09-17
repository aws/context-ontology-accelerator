# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure re-scan backup blob builder."""

from __future__ import annotations

import pytest
from coa_common.datazone_forms import deserialize_form
from coa_common.domain_models import BusinessMetadata, Column, EnrichmentSource, ReviewStatus, Table
from coa_sources.database.rescan import diff_tables
from coa_sources.database.rescan_backup import BACKUP_SCHEMA_VERSION, backup_s3_key, build_rescan_backup

pytestmark = pytest.mark.unit


def _bm(desc: str = "", status: str = ReviewStatus.APPROVED) -> BusinessMetadata:
    return BusinessMetadata(description=desc, enrichment_source=EnrichmentSource.STEWARD_EDITED, review_status=status)


def _col(name: str, data_type: str = "varchar", *, status: str = ReviewStatus.APPROVED, desc: str = "") -> Column:
    return Column(name=name, data_type=data_type, business_metadata=_bm(desc, status))


def _tbl(database: str, name: str, columns: list[Column], *, desc: str = "") -> Table:
    return Table(database=database, name=name, business_metadata=_bm(desc, ReviewStatus.APPROVED), columns=columns)


def _scenario() -> tuple[list[Table], list[Table]]:
    """Accepted vs fresh covering modified / added / removed / unchanged."""
    accepted = [
        # modified: 'a' changes type, 'gone' removed, 'new' will be added by fresh
        _tbl("db", "mod", [_col("a", "varchar", desc="curated a"), _col("b"), _col("gone", desc="curated gone")]),
        _tbl("db", "removed", [_col("x")]),
        _tbl("db", "same", [_col("c")]),
    ]
    fresh = [
        _tbl("db", "mod", [_col("a", "int"), _col("b"), _col("new")]),
        _tbl("db", "added", [_col("z")]),
        _tbl("db", "same", [_col("c")]),
    ]
    return accepted, fresh


def test_build_rescan_backup_partitions_and_backs_up_only_modified() -> None:
    accepted, fresh = _scenario()
    diff = diff_tables(accepted, fresh)

    blob = build_rescan_backup(diff, accepted, source_id="src-1", scan_job_sk="2026-08-15T00:00:00Z")

    assert blob["version"] == BACKUP_SCHEMA_VERSION
    assert blob["source_id"] == "src-1"
    assert blob["scan_job_sk"] == "2026-08-15T00:00:00Z"
    assert blob["added_tables"] == ["db.added"]
    assert blob["removed_tables"] == ["db.removed"]
    assert blob["removed_columns"] == {"db.mod": ["gone"]}
    # Only the modified table is backed up — not added, removed, or unchanged.
    assert set(blob["modified_backup"]) == {"db.mod"}


def test_modified_backup_captures_pre_rescan_state() -> None:
    accepted, fresh = _scenario()
    diff = diff_tables(accepted, fresh)

    blob = build_rescan_backup(diff, accepted, source_id="src-1", scan_job_sk="sk")
    restored = deserialize_form(blob["modified_backup"]["db.mod"])

    # The backup is the ACCEPTED (pre-rescan) shape: 'gone' still present, 'a'
    # still the OLD type, 'new' absent, curated text and APPROVED status intact.
    assert restored.table_id == "db.mod"
    cols = {c.name: c for c in restored.columns}
    assert set(cols) == {"a", "b", "gone"}
    assert cols["a"].data_type == "varchar"
    assert cols["gone"].business_metadata.description == "curated gone"
    assert restored.business_metadata.review_status == ReviewStatus.APPROVED


def test_build_rescan_backup_no_changes_is_empty() -> None:
    accepted, _ = _scenario()
    diff = diff_tables(accepted, accepted)

    blob = build_rescan_backup(diff, accepted, source_id="s", scan_job_sk="sk")

    assert blob["added_tables"] == []
    assert blob["removed_tables"] == []
    assert blob["removed_columns"] == {}
    assert blob["modified_backup"] == {}


def test_backup_s3_key_is_per_source_and_stable() -> None:
    assert backup_s3_key("src-1") == "src-1/rescan-backup/current.json"
    assert backup_s3_key("src-1") == backup_s3_key("src-1")


def test_build_rescan_backup_captures_prior_summary() -> None:
    accepted, fresh = _scenario()
    diff = diff_tables(accepted, fresh)
    prior = {"tablesDiscovered": 3, "discoveredSchemas": ["db"], "lastScanAt": "t0", "tablesApproved": 3}

    blob = build_rescan_backup(diff, accepted, source_id="s", scan_job_sk="sk", prior_summary=prior)
    assert blob["source_summary"] == prior
    # Absent prior_summary is an empty dict, never a missing key.
    assert build_rescan_backup(diff, accepted, source_id="s", scan_job_sk="sk")["source_summary"] == {}


def test_orphaned_added_tables_land_in_both_lists() -> None:
    """A prior re-scan's now-vanished added table must be reaped by either outcome.

    It is absent from both the baseline and the fresh scan, so diff_tables files
    it nowhere; carrying it forward puts it in removed_tables (approve deletes it)
    AND added_tables (reject deletes it), while the normal diff entries stay.
    """
    accepted, fresh = _scenario()
    diff = diff_tables(accepted, fresh)

    blob = build_rescan_backup(diff, accepted, source_id="s", scan_job_sk="sk", orphaned_added_tables=["db.orphan"])

    assert "db.orphan" in blob["removed_tables"]
    assert "db.orphan" in blob["added_tables"]
    # The real diff's own entries survive alongside the carried-forward orphan.
    assert "db.added" in blob["added_tables"]
    assert "db.removed" in blob["removed_tables"]


def test_orphaned_added_tables_are_deduped() -> None:
    """An orphan already present in a diff list, or repeated, is not listed twice."""
    accepted, fresh = _scenario()
    diff = diff_tables(accepted, fresh)  # diff.added == ["db.added"], diff.removed == ["db.removed"]

    blob = build_rescan_backup(
        diff,
        accepted,
        source_id="s",
        scan_job_sk="sk",
        orphaned_added_tables=["db.added", "db.orphan", "db.orphan"],
    )

    assert blob["added_tables"].count("db.added") == 1
    assert blob["added_tables"].count("db.orphan") == 1
    assert blob["removed_tables"].count("db.orphan") == 1
