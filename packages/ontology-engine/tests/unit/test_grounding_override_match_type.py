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
from coa_ontology import proposals
from coa_ontology.inducer.services.grounding import GroundingCandidate, GroundingResult, GroundingService
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_PREFIX = "https://hcp360.example.org/onto#"
_PUBLICATION = "https://parallax.aws/commercial/ontology/Publication"
_PRODUCT = "https://parallax.aws/commercial/ontology/Product"


def _proposal_item():
    """A production-shaped ENHANCED match: only the chosen candidate has an
    LLM rerank score; the alternate retains its embedding score alone."""
    publication = GroundingCandidate(
        entity_uri=_PUBLICATION,
        ontology_id="http://example.org/myonto/",
        label="Publication",
        definition="A published work.",
        lexical_sim=0.725,
        rerank_score=0.95,
        rerank_relationship="exactMatch",
        rerank_reason="Publication is identical",
    )
    product = GroundingCandidate(
        entity_uri=_PRODUCT,
        ontology_id="http://example.org/myonto/",
        label="Product",
        definition="A commercial product.",
        lexical_sim=0.72,
    )
    result = GroundingResult(
        source_column="",
        source_table="publication",
        candidates=[publication, product],
        chosen=publication,
        match_type="exact",
        relationship="exactMatch",
        confidence=0.95,
        reason="Publication is identical",
        mode="ENHANCED",
    )
    match = GroundingService(ontology_catalog=None, embedding_generator=None).to_concept_match(result)
    assert match.candidates and match.candidates[0].fused_score == 0.95
    assert match.candidates[1].fused_score is None
    return {
        "proposal_id": "p-ground",
        "ontology_id": _PREFIX,
        "status": "pending",
        "metadata": {
            "datasource_ids": ["ds-1"],
            "ontology_uri_prefix": _PREFIX,
            "matches": [match.model_dump()],
        },
    }


def _patch_store_and_pipeline(monkeypatch):
    """Stub out catalog fetch + ontology build + persistence so the test
    exercises only the match-reclassification block. Returns a dict that
    captures the matches handed to S3 persistence."""
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

    def _capture_matches_s3(pid, matches, namespace="default", version="latest"):
        captured["matches"] = matches
        return f"proposals/{namespace}/{pid}/{version}/matches.json"

    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", _capture_matches_s3)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", lambda *a, **k: None)
    return proposals, captured


def _proposal_with_stale_semantics():
    return _proposal_item()


def _match_after_override(proposals, captured, item, override_uri):
    proposals._apply_grounding_overrides("p-ground", {"publication": override_uri}, item, namespace="ns")
    return next(m for m in captured["matches"] if m["source_table"] == "publication")


def test_override_to_weaker_candidate_demotes_tier(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    pub = _match_after_override(proposals, captured, _proposal_with_stale_semantics(), _PRODUCT)
    # Product's 0.72 is embedding-only, so it uses the STANDARD ladder and is
    # ambiguous rather than being promoted by the rerank ladder. The old
    # Publication relationship/rationale must not follow the new target.
    assert pub["match_type"] == "ambiguous"
    assert pub["similarity"] == 0.72
    assert pub["relationship"] is None
    assert pub["reason"] is None


def test_revert_to_original_exact_restores_exact(monkeypatch):
    """The bug: this used to stay 'high_confidence'. Reverting to Publication
    (fused 0.95) must restore 'exact'."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    pub = _match_after_override(proposals, captured, _proposal_with_stale_semantics(), _PUBLICATION)
    assert pub["match_type"] == "exact"
    assert pub["similarity"] == 0.95
    assert pub["relationship"] is None
    assert pub["reason"] is None


def test_override_to_unknown_uri_is_rejected(monkeypatch):
    """A URI absent from recall has no evidence and must not become an axiom."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    with pytest.raises(proposals.HTTPException, match="stored candidates"):
        _match_after_override(
            proposals,
            captured,
            _proposal_with_stale_semantics(),
            "https://parallax.aws/commercial/ontology/NotACandidate",
        )
    assert "matches" not in captured


def test_clear_override_sets_novel(monkeypatch):
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    pub = _match_after_override(proposals, captured, _proposal_with_stale_semantics(), None)
    assert pub["match_type"] == "novel"
    assert pub["matched_class_uri"] is None
    assert pub["matched_ontology_id"] is None
    assert pub["similarity"] is None
    assert pub["relationship"] is None
    assert pub["reason"] is None


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


def test_ambiguous_legacy_bare_override_is_rejected_before_any_write(monkeypatch):
    """A bare name two identity-bearing tables share cannot say which one to update.

    Applying it to neither table and returning 200 would tell the caller the
    choice was saved when it was not, so the request is a 409 and nothing is
    persisted.
    """
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    item["metadata"]["matches"][0]["source_table_identity"] = "DS#finance::finance.public.publication"
    duplicate = deepcopy(item["metadata"]["matches"][0])
    duplicate["source_table_identity"] = "DS#identity::identity.public.publication"
    duplicate["matched_class_uri"] = _PRODUCT
    duplicate["similarity"] = 0.72
    duplicate["match_type"] = "high_confidence"
    item["metadata"]["matches"].append(duplicate)
    before = deepcopy(item)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides(
            "p-ground",
            {"publication": _PRODUCT},
            item,
            namespace="ns",
        )

    assert exc.value.status_code == 409
    assert "'publication' is ambiguous" in str(exc.value.detail)
    assert "matches" not in captured
    assert item == before


def test_ambiguous_bare_override_is_rejected_for_a_mixed_legacy_match(monkeypatch):
    """One legacy match without an identity still shares the bare name."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    identity_match = deepcopy(item["metadata"]["matches"][0])
    identity_match["source_table_identity"] = "DS#identity::identity.public.publication"
    identity_match["matched_class_uri"] = _PRODUCT
    identity_match["similarity"] = 0.72
    identity_match["match_type"] = "high_confidence"
    item["metadata"]["matches"].append(identity_match)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides(
            "p-ground",
            {"publication": None},
            item,
            namespace="ns",
        )

    assert exc.value.status_code == 409
    assert "'publication' is ambiguous" in str(exc.value.detail)
    assert "matches" not in captured


def test_ambiguous_bare_key_rejects_the_whole_request_alongside_a_valid_identity_key(monkeypatch):
    """A valid identity key in the same request is not applied either."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    finance = item["metadata"]["matches"][0]
    finance["source_table_identity"] = "DS#finance::finance.public.publication"
    identity = deepcopy(finance)
    identity["source_table_identity"] = "DS#identity::identity.public.publication"
    item["metadata"]["matches"].append(identity)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides(
            "p-ground",
            {"DS#finance::finance.public.publication": _PRODUCT, "publication": None},
            item,
            namespace="ns",
        )

    assert exc.value.status_code == 409
    assert "matches" not in captured


def test_identity_key_is_not_ambiguous_when_it_equals_a_shared_bare_name(monkeypatch):
    """The ambiguity check applies to bare names, never to an exact identity."""
    proposals, captured = _patch_store_and_pipeline(monkeypatch)
    item = _proposal_item()
    item["metadata"]["matches"][0]["source_table_identity"] = "publication"
    other = deepcopy(item["metadata"]["matches"][0])
    other["source_table_identity"] = "DS#identity::identity.public.publication"
    item["metadata"]["matches"].append(other)

    proposals._apply_grounding_overrides(
        "p-ground",
        {"publication": _PRODUCT},
        item,
        namespace="ns",
    )

    by_identity = {match["source_table_identity"]: match for match in captured["matches"]}
    assert by_identity["publication"]["matched_class_uri"] == _PRODUCT
    assert by_identity["DS#identity::identity.public.publication"]["matched_class_uri"] == _PUBLICATION


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
