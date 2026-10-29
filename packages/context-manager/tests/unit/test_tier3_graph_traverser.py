# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 3 graph traverser."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier3.graph_traverser import GraphTraverser


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = [
        {
            "entity": "http://ex.org/Claim",
            "label": "Claim",
            "type": "owl:Class",
        },
    ]
    return client


@pytest.fixture
def graph_traverser(mock_neptune):
    return GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")


@pytest.mark.unit
class TestGraphTraverserKeyword:
    async def test_extracts_entities_and_queries(self, graph_traverser, mock_neptune):
        await graph_traverser.traverse("What are the claims?", "insurance")
        mock_neptune.query.assert_awaited_once()
        query_arg = mock_neptune.query.call_args[0][0]
        assert "claims" in query_arg.lower()

    async def test_empty_query_returns_empty(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse("is the", "ns")
        assert results == []
        mock_neptune.query.assert_not_awaited()

    async def test_parses_relationships(self, mock_neptune):
        mock_neptune.query.return_value = [
            {
                "entity": "http://ex.org/Claim",
                "label": "Claim",
                "type": "owl:Class",
                "sourceUri": "http://ex.org/Claim",
                "sourceLabel": "Claim",
                "predicateUri": "http://ex.org/hasPolicy",
                "predicateLabel": "has policy",
                "targetUri": "http://ex.org/Policy",
                "targetLabel": "Policy",
            },
            {
                "entity": "http://ex.org/Claim",
                "label": "Claim",
                "type": "owl:Class",
                "sourceUri": "http://ex.org/Claim",
                "sourceLabel": "Claim",
                "predicateUri": "http://ex.org/hasCustomer",
                "predicateLabel": "has customer",
                "targetUri": "http://ex.org/Customer",
                "targetLabel": "Customer",
            },
        ]
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse("claims", "insurance", entities=["claim"])
        assert len(results) == 1
        assert len(results[0].relationships) == 2
        assert results[0].relationships[0] == {
            "source_uri": "http://ex.org/Claim",
            "source_label": "Claim",
            "predicate_uri": "http://ex.org/hasPolicy",
            "predicate_label": "has policy",
            "target_uri": "http://ex.org/Policy",
            "target_label": "Policy",
        }

    async def test_keyword_parser_dedupes_label_variants_by_directed_triple(self, mock_neptune):
        duplicate = {
            "entity": "http://ex.org/Claim",
            "label": "Claim",
            "type": "owl:Class",
            "sourceUri": "http://ex.org/Claim",
            "predicateUri": "http://ex.org/hasPolicy",
            "targetUri": "http://ex.org/Policy",
        }
        mock_neptune.query.return_value = [
            duplicate,
            {
                **duplicate,
                "sourceLabel": "Claim",
                "predicateLabel": "has policy",
                "targetLabel": "Policy",
            },
            *[
                {
                    **duplicate,
                    "predicateLabel": f"label variant {index}",
                }
                for index in range(20)
            ],
            {
                **duplicate,
                "predicateUri": "http://ex.org/hasCustomer",
                "targetUri": "http://ex.org/Customer",
                "predicateLabel": "has customer",
                "targetLabel": "Customer",
            },
        ]
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")

        results = await traverser.traverse("claims", "insurance", entities=["claim"])

        assert len(results[0].relationships) == 2
        assert results[0].relationships[0] == {
            "source_uri": "http://ex.org/Claim",
            "source_label": "Claim",
            "predicate_uri": "http://ex.org/hasPolicy",
            "predicate_label": "has policy",
            "target_uri": "http://ex.org/Policy",
            "target_label": "Policy",
        }

    async def test_rejects_invalid_namespace(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        with pytest.raises(ValueError, match="Invalid namespace"):
            await traverser.traverse("claims", "x:injected>}")

    async def test_sanitizes_external_entities(self, mock_neptune):
        mock_neptune.query.return_value = []
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        await traverser.traverse("claims", "insurance", entities=['foo"bar', "valid", "___"])

        query_arg = mock_neptune.query.call_args[0][0]
        assert r'CONTAINS(LCASE(?label), "foo\"bar")' in query_arg
        assert 'CONTAINS(LCASE(?label), "valid")' in query_arg
        assert "___" not in query_arg

    async def test_skips_empty_uri(self, mock_neptune):
        mock_neptune.query.return_value = [
            {"entity": "", "label": "Ghost", "type": "owl:Class"},
            {
                "entity": "http://ex.org/Real",
                "label": "Real",
                "type": "owl:Class",
            },
        ]
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse("real entity", "ns", entities=["real"])
        assert all(e.uri != "" for e in results)


@pytest.mark.unit
class TestGraphTraverserFromURIs:
    async def test_traverse_from_uris(self, mock_neptune):
        mock_neptune.query.return_value = [
            {
                "entity": "http://ex.org/Policy",
                "label": "Policy",
                "type": "owl:Class",
                "sourceUri": "http://ex.org/Policy",
                "sourceLabel": "Policy",
                "predicateUri": "http://ex.org/covers",
                "predicateLabel": "covers",
                "targetUri": "http://ex.org/Claim",
                "targetLabel": "Claim",
            },
        ]
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse_from_uris(["http://ex.org/Claim"], "insurance", max_hops=2)
        mock_neptune.query.assert_awaited_once()
        query_arg = mock_neptune.query.call_args[0][0]
        assert "VALUES ?seed" in query_arg
        assert "http://ex.org/Claim" in query_arg
        assert len(results) == 1
        assert results[0].label == "Policy"

    async def test_rejects_invalid_uris(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse_from_uris(['"><script>alert(1)</script>', "not a uri"], "insurance")
        assert results == []
        mock_neptune.query.assert_not_awaited()

    async def test_accepts_fragment_uris(self, mock_neptune):
        """Regression: induced-ontology class/property URIs carry a '#' fragment
        (e.g. .../pcinsurance#Claim). The URI safety gate must NOT reject them,
        or every seed URI is dropped and URI-hop silently returns 0 (the live
        Cause-B regression that took EN graph results from 24 to 0)."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        seed = "https://integ.coa/f5e4c967-2b4a-480c-b05f-8115faee3755/pcinsurance#Claim"
        await traverser.traverse_from_uris([seed], "insurance", max_hops=2)
        mock_neptune.query.assert_awaited_once()
        query_arg = mock_neptune.query.call_args[0][0]
        assert seed in query_arg  # the '#'-fragment URI reached the SPARQL VALUES clause

    async def test_single_hop_uses_union(self, mock_neptune):
        mock_neptune.query.return_value = []
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        await traverser.traverse_from_uris(["http://ex.org/Claim"], "insurance", max_hops=1)
        query_arg = mock_neptune.query.call_args[0][0]
        assert "UNION" in query_arg
        assert "hop2" not in query_arg

    async def test_parses_property_domain_range_relationships(self, mock_neptune):
        """A property node bridging the seed class surfaces as an entity whose
        relationships are its rdfs:domain / rdfs:range targets (the induced
        class↔property↔class structure). Duplicate domain/range triples (common
        in the induced graph) are de-duped."""
        mock_neptune.query.return_value = [
            {
                "entity": "http://ex.org/claim_catastropheIdentifier",
                "label": "catastrophe_identifier",
                "type": "http://www.w3.org/2002/07/owl#ObjectProperty",
                "sourceUri": "http://ex.org/claim_catastropheIdentifier",
                "sourceLabel": "catastrophe_identifier",
                "predicateUri": "http://www.w3.org/2000/01/rdf-schema#domain",
                "predicateLabel": "domain",
                "targetUri": "http://ex.org/Claim",
                "targetLabel": "claim",
            },
            {  # duplicate domain triple — must be de-duped
                "entity": "http://ex.org/claim_catastropheIdentifier",
                "label": "catastrophe_identifier",
                "type": "http://www.w3.org/2002/07/owl#ObjectProperty",
                "sourceUri": "http://ex.org/claim_catastropheIdentifier",
                "sourceLabel": "catastrophe_identifier",
                "predicateUri": "http://www.w3.org/2000/01/rdf-schema#domain",
                "predicateLabel": "domain",
                "targetUri": "http://ex.org/Claim",
                "targetLabel": "claim",
            },
            {  # range -> the bridged class
                "entity": "http://ex.org/claim_catastropheIdentifier",
                "label": "catastrophe_identifier",
                "type": "http://www.w3.org/2002/07/owl#ObjectProperty",
                "sourceUri": "http://ex.org/claim_catastropheIdentifier",
                "sourceLabel": "catastrophe_identifier",
                "predicateUri": "http://www.w3.org/2000/01/rdf-schema#range",
                "predicateLabel": "range",
                "targetUri": "http://ex.org/Catastrophe",
                "targetLabel": "catastrophe",
            },
        ]
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        results = await traverser.traverse_from_uris(["http://ex.org/Claim"], "insurance", max_hops=2)
        assert len(results) == 1
        entity = results[0]
        # 3 rows, but the duplicate domain triple collapses -> 2 unique relationships
        assert len(entity.relationships) == 2
        targets = {r["target_uri"] for r in entity.relationships}
        assert targets == {"http://ex.org/Claim", "http://ex.org/Catastrophe"}

    async def test_namespace_validation(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        with pytest.raises(ValueError, match="Invalid namespace"):
            await traverser.traverse_from_uris(["http://ex.org/Claim"], "bad>namespace")


@pytest.mark.unit
class TestGraphTraverserInducedHopQuery:
    """U4 (Change 2): the URI-hop SPARQL must traverse the induced-ontology edge
    layout — class↔class edges expressed through property nodes carrying
    rdfs:domain / rdfs:range (verified live on the combined namespace) — not
    only direct labelled ?seed→?entity triples. String-shape assertion on the
    emitted query for a seed URI."""

    def _hop_query(self, max_hops):
        t = GraphTraverser(AsyncMock(), graph_uri_template="https://ontology-workbench.local/{namespace}")
        return t._build_hop_query("insurance", ["http://ex.org/Claim"], max_hops)

    def test_two_hop_query_contains_domain_range_bridge(self):
        q = self._hop_query(max_hops=2)
        # induced-ontology property arm present (domain/range bridge)
        assert "rdfs:domain" in q
        assert "rdfs:range" in q
        assert "(rdfs:domain|rdfs:range)" in q
        # relationships surface as explicit source/predicate/target columns.
        assert "?sourceUri" in q and "?predicateUri" in q and "?targetUri" in q
        # seed-as-property arms (the common case: vector search returns property URIs)
        assert "BIND(?seed AS ?entity)" in q  # seed property emitted as entity
        assert "?sharedClass" in q  # sibling-property (2-hop) arm
        # bridged-class arm: the far-side class is emitted as its own ?entity
        assert "?bridge (rdfs:domain|rdfs:range) ?seed" in q
        # bridged-class PROPERTY arm: neighborhood classes' own properties emitted
        # too (entity-count parity with the keyword path)
        assert "?nbrClass" in q
        # seed still bound + graph-scoped + bounded
        assert "VALUES ?seed" in q
        assert "http://ex.org/Claim" in q
        assert "STRSTARTS(STR(?g)" in q

    def test_hop_query_has_induced_arm_all_hops(self):
        # The core induced domain/range arms are present regardless of hop count.
        for h in (1, 2):
            q = self._hop_query(max_hops=h)
            assert "(rdfs:domain|rdfs:range)" in q
            assert "UNION" in q
            # legacy hop2 columns must be gone
            assert "?hop2target" not in q and "hop2pred" not in q
            # seed's own neighborhood present at every hop count
            assert "BIND(?seed AS ?entity)" in q

    def test_one_hop_query_excludes_2hop_bridged_arms(self):
        # max_hops=1 must be a lighter query: the 2-hop bridged-class / sibling
        # arms are gated on max_hops>=2 to keep a 1-hop request cheap.
        q1 = self._hop_query(max_hops=1)
        assert "?bridge" not in q1
        assert "?nbrClass" not in q1
        assert "?sharedClass" not in q1
        # ...but the 2-hop query includes them.
        q2 = self._hop_query(max_hops=2)
        assert "?bridge" in q2 and "?nbrClass" in q2 and "?sharedClass" in q2
