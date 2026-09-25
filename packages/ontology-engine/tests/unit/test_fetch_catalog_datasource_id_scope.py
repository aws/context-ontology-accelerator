# SPDX-License-Identifier: Apache-2.0

"""Unit tests: catalog fetch matches a datasource regardless of DS#/SRC# key prefix.

The sources pipeline keys datasources as ``DS#{uuid}`` / ``SRC#{uuid}`` while the
``datasource_ids`` recorded on accepted proposals (and passed to the schema.sql
re-fetch) carry the bare uuid. An exact ``==`` match dropped the sources-pipeline
source, so its tables were absent from schema.sql, its ``rr:tableName`` literals
were uncoverable, and schema.sql generation failed closed — leaving a stale
schema.sql and breaking every Tier-2 query in the namespace (regression from the
sources cross-source work exposed by the schema.sql coverage guard).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.unit
def test_smus_fetch_matches_prefixed_source_for_bare_id() -> None:
    """A ``DS#``-keyed source in the reader result is matched when the caller passes the bare uuid."""
    from coa_ontology import induce_catalog

    bare = "11111111-2222-3333-4444-555555555555"
    reader_result = {
        "sources": [
            {"datasourceId": f"DS#{bare}", "databases": [{"name": "hcp360", "tables": [{"name": "trial_csv"}]}]},
        ]
    }

    with (
        patch.object(induce_catalog, "_resolve_datazone_project_id", return_value=("proj-1", "ns-uuid")),
        patch(
            "coa_common.metadata_store.catalog_reader.read_approved_catalog",
            return_value=reader_result,
        ) as mock_reader,
    ):
        config = {
            "smus_domain_id": "dom-1",
            "namespaces_table": "ns-table",
            "sources_table": "src-table",
            "smus_region": "us-east-1",
        }
        got = induce_catalog._fetch_catalog_from_smus(config, bare, namespace="ns")

    # Matched the DS#-prefixed source despite the caller passing the bare uuid.
    assert got.get("databases"), f"expected the DS#-keyed source to match the bare id, got {got!r}"
    assert got["databases"][0]["name"] == "hcp360"
    # The reader is called with the bare id, so DataZone resolution does not miss either.
    assert mock_reader.call_args.kwargs["data_source_ids"] == [bare]


@pytest.mark.unit
def test_smus_fetch_matches_bare_source_for_prefixed_id() -> None:
    """Symmetric case: a bare-keyed reader row matches when the caller passes a ``DS#`` id."""
    from coa_ontology import induce_catalog

    bare = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    reader_result = {"sources": [{"datasourceId": bare, "databases": [{"name": "public", "tables": []}]}]}

    with (
        patch.object(induce_catalog, "_resolve_datazone_project_id", return_value=("proj-1", "ns-uuid")),
        patch("coa_common.metadata_store.catalog_reader.read_approved_catalog", return_value=reader_result),
    ):
        got = induce_catalog._fetch_catalog_from_smus(
            {"smus_domain_id": "d", "namespaces_table": "n", "sources_table": "s", "smus_region": "us-east-1"},
            f"DS#{bare}",
            namespace="ns",
        )

    assert got.get("databases"), f"expected the bare source to match the DS# id, got {got!r}"


@pytest.mark.unit
def test_smus_fetch_no_match_returns_empty() -> None:
    """A genuinely different datasource still does not match (no over-broad collapse)."""
    from coa_ontology import induce_catalog

    reader_result = {"sources": [{"datasourceId": "DS#unrelated-id", "databases": [{"name": "x", "tables": []}]}]}

    with (
        patch.object(induce_catalog, "_resolve_datazone_project_id", return_value=("proj-1", "ns-uuid")),
        patch("coa_common.metadata_store.catalog_reader.read_approved_catalog", return_value=reader_result),
    ):
        got = induce_catalog._fetch_catalog_from_smus(
            {"smus_domain_id": "d", "namespaces_table": "n", "sources_table": "s", "smus_region": "us-east-1"},
            "some-other-uuid",
            namespace="ns",
        )

    assert got == {"databases": []}


@pytest.mark.unit
def test_smus_fetch_empty_id_does_not_match_empty_source() -> None:
    """An empty bare id must NOT match a source whose datasourceId is empty/absent."""
    from coa_ontology import induce_catalog

    reader_result = {"sources": [{"datasourceId": "", "databases": [{"name": "x", "tables": []}]}]}
    with (
        patch.object(induce_catalog, "_resolve_datazone_project_id", return_value=("proj-1", "ns-uuid")),
        patch("coa_common.metadata_store.catalog_reader.read_approved_catalog", return_value=reader_result),
    ):
        got = induce_catalog._fetch_catalog_from_smus(
            {"smus_domain_id": "d", "namespaces_table": "n", "sources_table": "s", "smus_region": "us-east-1"},
            "",
            namespace="ns",
        )
    assert got == {"databases": []}


@pytest.mark.unit
def test_bare_datasource_id_strips_single_prefix_only() -> None:
    """bare_datasource_id strips at most ONE key prefix (no double-strip of malformed input)."""
    from coa_ontology.datasource_ids import bare_datasource_id

    u = "11111111-2222-3333-4444-555555555555"
    assert bare_datasource_id(f"DS#{u}") == u
    assert bare_datasource_id(f"SRC#{u}") == u
    assert bare_datasource_id(u) == u
    assert bare_datasource_id("") == ""
    # malformed double prefix: only the leading one is removed, the rest is preserved
    assert bare_datasource_id(f"DS#SRC#{u}") == f"SRC#{u}"


@pytest.mark.unit
def test_is_valid_datasource_id() -> None:
    """Accepts ordinary ids (uuid AND ds-1 style); rejects path-traversal / empty / unsafe."""
    from coa_ontology.datasource_ids import is_valid_datasource_id

    assert is_valid_datasource_id("11111111-2222-3333-4444-555555555555")
    assert is_valid_datasource_id("ds-1")
    assert is_valid_datasource_id("ds-def789")
    assert not is_valid_datasource_id("")
    assert not is_valid_datasource_id("../../etc/passwd")
    assert not is_valid_datasource_id("11111111-2222-3333-4444-555555555555/extra")
    assert not is_valid_datasource_id("has space")
    assert not is_valid_datasource_id("has?query=1")


@pytest.mark.unit
def test_fetch_catalog_rejects_malformed_id() -> None:
    """_fetch_catalog validates the id before interpolating it into the URL path."""
    from coa_ontology import induce_catalog

    with pytest.raises(ValueError, match="invalid datasource id"):
        induce_catalog._fetch_catalog("http://catalog.local", "../../../etc/passwd")
