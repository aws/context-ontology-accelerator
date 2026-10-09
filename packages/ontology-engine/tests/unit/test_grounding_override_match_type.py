# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grounding-override match_type must be derived from the chosen candidate's
score, not hardcoded.

Regression for the observed bug: overriding a match and then reverting to the
original grounding left the tier stuck at ``high_confidence``. Concretely —
``publication → Publication`` starts as an *exact* (0.95) match; the user
overrides it to ``Product`` and then back to ``Publication``; it must return to
``exact``, not stay ``high_confidence``. The old code hardcoded
``m.match_type = "high_confidence"`` on every override (proposals.py), which was
both lossy (destroyed the original tier) and wrong (asserted a tier unrelated to
the candidate's score). Each match keeps its candidates with scores, so the tier
is recoverable.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit

_PREFIX = "https://hcp360.example.org/onto#"
_PUBLICATION = "https://parallax.aws/commercial/ontology/Publication"
_PRODUCT = "https://parallax.aws/commercial/ontology/Product"


def _proposal_item():
    """A pending proposal whose ``publication`` table is an exact (0.95) match to
    Publication, with Product as a weaker (0.72) candidate."""
    return {
        "proposal_id": "p-ground",
        "ontology_id": _PREFIX,
        "status": "pending",
        "metadata": {
            "datasource_ids": ["ds-1"],
            "ontology_uri_prefix": _PREFIX,
            "matches": [
                {
                    "source_column": "",
                    "source_table": "publication",
                    "matched_class_uri": _PUBLICATION,
                    "matched_ontology_id": "http://example.org/myonto/",
                    "similarity": 0.95,
                    "match_type": "exact",
                    "candidates": [
                        {
                            "entity_uri": _PUBLICATION,
                            "ontology_id": "http://example.org/myonto/",
                            "lexical_sim": 0.725,
                            "fused_score": 0.95,
                        },
                        {
                            "entity_uri": _PRODUCT,
                            "ontology_id": "http://example.org/myonto/",
                            "lexical_sim": 0.60,
                            "fused_score": 0.72,
                        },
                    ],
                }
            ],
        },
    }


def _patch_store_and_pipeline(monkeypatch):
    """Stub out catalog fetch + ontology build + persistence so the test
    exercises only the match-reclassification block. Returns a dict that
    captures the matches handed to S3 persistence."""
    from coa_ontology import proposals

    captured: dict = {}

    monkeypatch.setattr(proposals, "_fetch_catalog_from_smus", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr("coa_ontology.induce_catalog._fetch_catalog", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(
        "coa_ontology.induce_catalog._catalog_to_tables",
        lambda catalog: [
            {
                "id": "t-publication",
                "name": "publication",
                "fullyQualifiedName": "db.publication",
                "columns": [{"name": "id", "dataType": "INT"}],
            }
        ],
        raising=False,
    )

    class _FakeGraph:
        def serialize(self, format="turtle"):
            return "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"

    class _FakeStrategy:
        def _build_proposal_ontology(self, uri_prefix, tables, matches):
            return _FakeGraph(), []

        def build_r2rml(self, **kwargs):
            return _FakeGraph()

    monkeypatch.setattr(proposals, "TableToOntologyStrategy", _FakeStrategy, raising=False)

    def _capture_matches_s3(pid, matches, namespace="default"):
        captured["matches"] = matches
        return f"proposals/{namespace}/{pid}/latest/matches.json"

    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", _capture_matches_s3)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            f"proposals/{namespace}/{pid}/latest/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", lambda *a, **k: None)
    return proposals, captured


def _match_type_after_override(proposals, captured, item, override_uri):
    proposals._apply_grounding_overrides("p-ground", {"publication": override_uri}, item, namespace="ns")
    pub = next(m for m in captured["matches"] if m["source_table"] == "publication")
    return pub["match_type"], pub.get("similarity")


def test_override_to_weaker_candidate_demotes_tier(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    mt, sim = _match_type_after_override(proposals, captured, _proposal_item(), _PRODUCT)
    # Product's candidate score is 0.72 → high_confidence (not exact).
    assert mt == "high_confidence"
    assert sim == 0.72


def test_revert_to_original_exact_restores_exact(monkeypatch):
    """The bug: this used to stay 'high_confidence'. Reverting to Publication
    (fused 0.95) must restore 'exact'."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    mt, sim = _match_type_after_override(proposals, captured, _proposal_item(), _PUBLICATION)
    assert mt == "exact"
    assert sim == 0.95


def test_override_to_unknown_uri_keeps_conservative_default(monkeypatch):
    """An override URI not among the stored candidates has no score to classify
    from → keep the conservative high_confidence default rather than asserting a
    tier we can't justify."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    mt, _sim = _match_type_after_override(
        proposals, captured, _proposal_item(), "https://parallax.aws/commercial/ontology/NotACandidate"
    )
    assert mt == "high_confidence"


def test_clear_override_sets_novel(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    proposals._apply_grounding_overrides("p-ground", {"publication": None}, _proposal_item(), namespace="ns")
    pub = next(m for m in captured["matches"] if m["source_table"] == "publication")
    assert pub["match_type"] == "novel"
    assert pub["matched_class_uri"] is None
    assert pub["similarity"] is None


def test_override_keyed_by_identity_updates_only_the_selected_same_named_table(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    finance = item["metadata"]["matches"][0]
    finance["source_table_identity"] = "DS#finance::finance.public.publication"
    identity = deepcopy(finance)
    identity["source_table_identity"] = "DS#identity::identity.public.publication"
    item["metadata"]["matches"].append(identity)

    proposals._apply_grounding_overrides(
        "p-ground",
        {"DS#finance::finance.public.publication": _PRODUCT},
        item,
        namespace="ns",
    )

    by_identity = {match["source_table_identity"]: match for match in captured["matches"]}
    assert by_identity["DS#finance::finance.public.publication"]["matched_class_uri"] == _PRODUCT
    assert by_identity["DS#identity::identity.public.publication"]["matched_class_uri"] == _PUBLICATION
    assert by_identity["DS#identity::identity.public.publication"]["similarity"] == 0.95


def test_legacy_bare_override_still_updates_a_unique_identity_bearing_match(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    item["metadata"]["matches"][0]["source_table_identity"] = "DS#one::db.publication"

    proposals._apply_grounding_overrides(
        "p-ground",
        {"publication": _PRODUCT},
        item,
        namespace="ns",
    )

    [publication] = captured["matches"]
    assert publication["matched_class_uri"] == _PRODUCT
    assert publication["similarity"] == 0.72


def test_identity_override_takes_precedence_over_legacy_bare_key(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    identity = "DS#one::db.publication"
    item["metadata"]["matches"][0]["source_table_identity"] = identity

    proposals._apply_grounding_overrides(
        "p-ground",
        {identity: _PRODUCT, "publication": _PUBLICATION},
        item,
        namespace="ns",
    )

    [publication] = captured["matches"]
    assert publication["matched_class_uri"] == _PRODUCT
    assert publication["similarity"] == 0.72


def test_ambiguous_legacy_bare_override_does_not_update_same_named_tables(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    item["metadata"]["matches"][0]["source_table_identity"] = "DS#finance::finance.public.publication"
    duplicate = deepcopy(item["metadata"]["matches"][0])
    duplicate["source_table_identity"] = "DS#identity::identity.public.publication"
    duplicate["matched_class_uri"] = _PRODUCT
    duplicate["similarity"] = 0.72
    duplicate["match_type"] = "high_confidence"
    item["metadata"]["matches"].append(duplicate)

    proposals._apply_grounding_overrides(
        "p-ground",
        {"publication": _PRODUCT},
        item,
        namespace="ns",
    )

    assert [match["matched_class_uri"] for match in captured["matches"]] == [
        _PUBLICATION,
        _PRODUCT,
    ]
    assert [match["similarity"] for match in captured["matches"]] == [0.95, 0.72]


def test_ambiguous_bare_override_does_not_update_a_mixed_legacy_match(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    identity_match = deepcopy(item["metadata"]["matches"][0])
    identity_match["source_table_identity"] = "DS#identity::identity.public.publication"
    identity_match["matched_class_uri"] = _PRODUCT
    identity_match["similarity"] = 0.72
    identity_match["match_type"] = "high_confidence"
    item["metadata"]["matches"].append(identity_match)

    proposals._apply_grounding_overrides(
        "p-ground",
        {"publication": None},
        item,
        namespace="ns",
    )

    assert [match["matched_class_uri"] for match in captured["matches"]] == [
        _PUBLICATION,
        _PRODUCT,
    ]
    assert [match["similarity"] for match in captured["matches"]] == [0.95, 0.72]


def test_identity_keyed_clear_updates_only_the_selected_same_named_table(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    finance = item["metadata"]["matches"][0]
    finance["source_table_identity"] = "DS#finance::finance.public.publication"
    identity = deepcopy(finance)
    identity["source_table_identity"] = "DS#identity::identity.public.publication"
    item["metadata"]["matches"].append(identity)

    proposals._apply_grounding_overrides(
        "p-ground",
        {"DS#finance::finance.public.publication": None},
        item,
        namespace="ns",
    )

    by_identity = {match["source_table_identity"]: match for match in captured["matches"]}
    assert by_identity["DS#finance::finance.public.publication"]["matched_class_uri"] is None
    assert by_identity["DS#finance::finance.public.publication"]["match_type"] == "novel"
    assert by_identity["DS#finance::finance.public.publication"]["similarity"] is None
    assert by_identity["DS#identity::identity.public.publication"]["matched_class_uri"] == _PUBLICATION
    assert by_identity["DS#identity::identity.public.publication"]["similarity"] == 0.95
