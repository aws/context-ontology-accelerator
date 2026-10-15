# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Induction and the structured proposal update resolve a datasource the same way.

Regression for the deployed ``test_mark_novel_round_trips_through_public_update_api``:
a proposal induced from the ``ds-pc-insurance-mock`` datasource (read from the bundled
fixture) could not be edited, because the update's rebuild asked SMUS for the mock ID,
got no tables, and answered ``409 Could not fetch catalog tables``.
"""

from __future__ import annotations

import pytest
from coa_ontology import induce_catalog, proposals

pytestmark = pytest.mark.unit

_FIXTURE_CATALOG = {
    "databases": [
        {
            "name": "pc_insurance",
            "tables": [
                {
                    "id": "t-policy",
                    "name": "policy",
                    "fullyQualifiedName": "pc_insurance.policy",
                    "columns": [{"name": "policy_id", "dataType": "INT"}],
                }
            ],
        }
    ]
}


@pytest.fixture
def sources(monkeypatch):
    """Record which catalog source each lookup used."""
    calls: list[tuple[str, str]] = []

    def fixture(ds_id):
        calls.append(("fixture", ds_id))
        return _FIXTURE_CATALOG

    def smus(config, ds_id, namespace):
        calls.append(("smus", ds_id))
        return {"databases": []}

    def http(url, ds_id):
        calls.append(("http", ds_id))
        return {"databases": []}

    monkeypatch.setattr(induce_catalog, "_fetch_catalog_from_fixture", fixture)
    monkeypatch.setattr(induce_catalog, "_fetch_catalog_from_smus", smus)
    monkeypatch.setattr(induce_catalog, "_fetch_catalog", http)
    return calls


@pytest.mark.parametrize(
    ("ds_id", "catalog_source", "expected"),
    [
        ("ds-pc-insurance-mock", "smus", "fixture"),
        ("ds-pc-insurance-mock", "http", "fixture"),
        ("ds-real", "smus", "smus"),
        ("ds-real", "http", "http"),
    ],
)
def test_routing(sources, ds_id, catalog_source, expected):
    config = {"catalog_source": catalog_source, "data_catalog_url": "http://catalog"}
    induce_catalog.fetch_datasource_catalog(config, ds_id, namespace="ns")
    assert sources == [(expected, ds_id)]


def test_update_rebuild_reads_a_mock_datasource_from_the_fixture(sources, monkeypatch):
    """The update gets past the catalog step for a proposal induced from a mock source."""
    monkeypatch.setenv("CATALOG_SOURCE", "smus")
    # The SMUS path reads the namespace language from the namespaces table, which
    # this unit test has no stand-in for; an English namespace is the legacy case.
    monkeypatch.setattr("coa_ontology.induce_catalog.resolve_namespace_language", lambda *a, **k: "en")
    built: dict = {}

    class _Built(Exception):
        pass

    class _Strategy:
        def _build_proposal_ontology(self, uri_prefix, tables, matches, namespace_language="en"):
            built["tables"] = [t.name for t in tables]
            raise _Built

    # _apply_grounding_overrides imports the strategy inside the function.
    monkeypatch.setattr("coa_ontology.inducer.strategies.table_to_ontology.TableToOntologyStrategy", _Strategy)
    item = {
        "proposal_id": "p-mock",
        "metadata": {
            "datasource_ids": ["ds-pc-insurance-mock"],
            "ontology_uri_prefix": "https://example.org/onto#",
            "matches": [
                {
                    "source_table": "policy",
                    "source_column": "",
                    "matched_class_uri": "",
                    "matched_ontology_id": "",
                    "similarity": 0.0,
                    "match_type": "novel",
                }
            ],
        },
    }
    with pytest.raises(_Built):
        proposals._apply_grounding_overrides("p-mock", {}, item, namespace="ns")
    assert sources == [("fixture", "ds-pc-insurance-mock")]
    assert built["tables"] == ["policy"]
