# SPDX-License-Identifier: Apache-2.0

"""Local repro of the schema.sql merge failure (no AWS).

Reproduces ``test_vkg_schema_sql_declares_every_logical_table`` from
``packages/ontology-engine/tests/integ`` without a deployed environment by
driving the real ``_generate_schema_sql`` code path with mocked I/O
(``dynamo_store.list_proposals`` and the DataZone catalog reader).

Root cause (established live): ``_generate_schema_sql`` gathers datasource ids
from ``list_proposals(status="accepted")`` — an eventually-consistent DynamoDB
scan. On a merge, schema.sql generation runs inside the very accept that flipped
its own proposal to ``accepted`` moments earlier, so the scan can miss that
proposal and drop its source's tables from the DDL. The merged mapping (built
from Neptune) still names them, so ``_tables_to_h2_ddl`` fails closed with
``SchemaSqlGenerationError`` and a stale schema.sql is served — breaking every
Tier-2 query in the namespace.

Fix: the accept threads the in-flight proposal's own ``datasource_ids`` in via
``extra_datasource_ids``, unioned with the scan, closing the read-after-write
gap. These tests model the scan MISSING source 2 (the race) and assert the fix
still covers it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

_DS1 = "11111111-1111-1111-1111-111111111111"  # source 1: already visible to the scan
_DS2 = "22222222-2222-2222-2222-222222222222"  # source 2: the in-flight accept the scan misses

_TABLES = {_DS1: ["brands", "products"], _DS2: ["customers", "orders"]}
_ALL_TABLES = [t for tbls in _TABLES.values() for t in tbls]

# The merged mapping (accumulated from Neptune) names BOTH sources' tables.
_MAPPING_TURTLE = "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n" + "".join(
    f'<#tm_{t}> rr:logicalTable [ rr:tableName "\\"{t}\\"" ] .\n' for t in _ALL_TABLES
)


def _reader_for(ds_id: str) -> dict:
    """DataZone reader result for one datasource id (mirrors read_approved_catalog)."""
    tbls = _TABLES.get(ds_id, [])
    return {
        "sources": [
            {
                "datasourceId": ds_id,
                "databases": [
                    {
                        "name": "db",
                        "tables": [
                            {"name": t, "fullyQualifiedName": f"db.{t}", "columns": [{"name": "id", "type": "INT"}]}
                            for t in tbls
                        ],
                    }
                ],
            }
        ]
    }


def _run(monkeypatch: pytest.MonkeyPatch, *, extra_datasource_ids: list[str] | None):
    """Run _generate_schema_sql with the scan returning ONLY source 1 (the race)."""
    from coa_ontology import proposals

    monkeypatch.setenv("CATALOG_SOURCE", "smus")
    monkeypatch.setenv("SMUS_DOMAIN_ID", "dom-1")
    monkeypatch.setenv("NAMESPACES_TABLE", "ns-table")
    monkeypatch.setenv("SOURCES_TABLE", "src-table")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    # The eventually-consistent scan has NOT yet caught up to source 2's accept.
    scan_result = [{"metadata": {"datasource_ids": [_DS1]}}]

    def _fake_read(*_a, data_source_ids, **_k):
        return _reader_for(data_source_ids[0])

    with (
        patch.object(proposals.dynamo_store, "list_proposals", return_value=scan_result),
        patch("coa_ontology.induce_catalog._resolve_datazone_project_id", return_value=("proj-1", "ns-uuid")),
        patch("coa_common.metadata_store.catalog_reader.read_approved_catalog", side_effect=_fake_read),
    ):
        return proposals._generate_schema_sql("ns", "ont-1", _MAPPING_TURTLE, extra_datasource_ids=extra_datasource_ids)


@pytest.mark.unit
def test_schema_sql_drops_source_when_scan_misses_it_and_no_extra_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the fix (no extra_datasource_ids), source 2 is dropped and generation fails closed."""
    from coa_ontology.proposals import SchemaSqlGenerationError

    with pytest.raises(SchemaSqlGenerationError):
        _run(monkeypatch, extra_datasource_ids=None)


@pytest.mark.unit
def test_schema_sql_covers_in_flight_source_via_extra_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the fix, threading source 2's own id makes schema.sql declare every logical table."""
    ddl = _run(monkeypatch, extra_datasource_ids=[_DS2])
    assert ddl, "schema.sql was empty/None"
    missing = [t for t in _ALL_TABLES if f'CREATE TABLE IF NOT EXISTS "{t}" (' not in ddl]
    assert not missing, f"logical table(s) absent from schema.sql: {missing}"


@pytest.mark.unit
def test_extra_ids_union_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing an id the scan already returned does not duplicate or break anything."""
    ddl = _run(monkeypatch, extra_datasource_ids=[_DS1, _DS2])
    assert ddl
    # Each table declared exactly once despite _DS1 appearing in both scan and extra.
    for t in _ALL_TABLES:
        assert ddl.count(f'CREATE TABLE IF NOT EXISTS "{t}" (') == 1, f"{t} not declared exactly once"
