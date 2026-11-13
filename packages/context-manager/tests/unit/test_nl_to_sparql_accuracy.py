# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL-to-SPARQL glossary injection, validation hardening and confidence calibration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_common.constants import URN_PREFIX
from coa_serve.tier2.ontop.nl_to_sparql import NLtoSPARQL
from coa_serve.tier2.ontop.sparql_validator import _GOVERNED_METRIC_URI_RE, SPARQLValidator
from coa_serve.tier2.ontop.tbox_context import AiContextTerm, TBoxContext, TBoxContextBuilder
from coa_serve.tier2.ontop.types import VectorHit


def _hit(uri: str, score: float = 0.9) -> VectorHit:
    return VectorHit(type="ontology_class", score=score, entity_id="e1", uri=uri)


@pytest.mark.unit
class TestAiContextGlossary:
    def test_parse_ai_context_extracts_synonyms_and_instructions(self):
        rows = [
            {
                "s": "http://ex.org/o#Churn",
                "label": "Churn",
                "aiContext": '{"synonyms": ["attrition", "customer loss"], "instructions": "use net rate"}',
            }
        ]
        terms = TBoxContextBuilder._parse_ai_context(rows)
        assert len(terms) == 1
        assert terms[0].synonyms == ["attrition", "customer loss"]
        assert terms[0].instructions == "use net rate"

    def test_parse_ai_context_skips_malformed_json(self):
        rows = [{"s": "u", "aiContext": "{not json"}, {"s": "u2", "aiContext": ""}]
        assert TBoxContextBuilder._parse_ai_context(rows) == []

    def test_parse_ai_context_skips_entries_without_synonyms_or_instructions(self):
        rows = [{"s": "u", "aiContext": '{"other": "field"}'}]
        assert TBoxContextBuilder._parse_ai_context(rows) == []

    async def test_fetch_ai_context_fail_open_on_query_error(self):
        client = AsyncMock()
        client.query.side_effect = RuntimeError("neptune down")
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        terms = await builder._fetch_ai_context([_hit("https://ex.org/o#A")], "ns")
        assert terms == []

    def test_metric_hit_enriched_from_resolver(self):
        """a metric vector hit is enriched with the full resolver definition."""
        from coa_serve.tier1.metric_resolver import MetricResolver

        resolver = MetricResolver(
            seed=[
                {
                    "metric_id": "m-rev",
                    "name": "total_revenue",
                    "description": "Sum of net revenue",
                    "sql_template": "SELECT SUM(amount) FROM rev",
                    "dimensions": ["region", "quarter"],
                    "namespace": "finance",
                }
            ]
        )
        builder = TBoxContextBuilder(AsyncMock(), graph_uri_template="https://t/{namespace}", metric_resolver=resolver)
        # Hit carries only a sparse name; resolver supplies the full definition.
        hit = VectorHit(type="metric", score=0.95, entity_id="m-rev", uri="", metadata={"name": "total_revenue"})
        metrics = builder._build_metric_context([hit], "finance")
        assert len(metrics) == 1
        assert metrics[0].description == "Sum of net revenue"
        assert metrics[0].dimensions == ["region", "quarter"]
        assert metrics[0].formula == "SELECT SUM(amount) FROM rev"

    def test_metric_hit_falls_back_to_metadata_without_resolver(self):
        builder = TBoxContextBuilder(AsyncMock(), graph_uri_template="https://t/{namespace}")
        hit = VectorHit(
            type="metric", score=0.9, entity_id="x", uri="", metadata={"name": "m", "description": "from-meta"}
        )
        metrics = builder._build_metric_context([hit], "ns")
        assert metrics[0].description == "from-meta"

    def test_glossary_rendered_in_prompt(self):
        client = AsyncMock()
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        ctx = TBoxContext(
            classes=[{"uri": "https://ex.org/o#Churn", "label": "Churn", "parent": None}],
            properties=[],
            glossary=[AiContextTerm(label="Churn", synonyms=["attrition"], instructions="use net rate")],
        )
        text = builder.format_for_prompt(ctx, "ns")
        assert "Glossary" in text
        assert "attrition" in text
        assert "use net rate" in text


@pytest.mark.unit
class TestGovernedMetricUriPattern:
    def test_recognizes_metric_urn(self):
        assert _GOVERNED_METRIC_URI_RE.match(f"urn:{URN_PREFIX}:insurance:metric:total_premium")

    def test_rejects_non_metric_urn(self):
        assert not _GOVERNED_METRIC_URI_RE.match(f"urn:{URN_PREFIX}:insurance:published")

    async def test_wellformed_metric_urn_accepted_without_existence_check(self):
        """A well-formed GovernedMetric URN validates by pattern (not as an http subject)."""
        graph = AsyncMock()
        graph.query.return_value = [{"found": "0"}]  # would fail an http existence check
        graph.ask.return_value = False
        v = SPARQLValidator(graph, graph_uri_template="https://test.local/{namespace}")
        sparql = "SELECT ?m WHERE { ?m a <urn:coa:insurance:metric:total_premium> }"
        result = await v.validate(sparql, "insurance")
        assert result.valid is True

    async def test_malformed_metric_urn_rejected(self):
        """A urn:coa:...:metric token that doesn't match the strict pattern is rejected."""
        graph = AsyncMock()
        v = SPARQLValidator(graph, graph_uri_template="https://test.local/{namespace}")
        # ':metric' present but not the strict :metric:{name} shape
        sparql = "SELECT ?m WHERE { ?m a <urn:coa:insurance:metric:> }"
        result = await v.validate(sparql, "insurance")
        assert result.valid is False
        assert "Malformed" in (result.error or "")


@pytest.mark.unit
class TestDomainRangeValidation:
    def _validator(self, ask_results):
        client = AsyncMock()
        client.ask.side_effect = ask_results
        # query() (URI-existence stage) — no domain-specific URIs to check here.
        client.query.return_value = [{"found": "0"}]
        return SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

    def _typed_sparql(self):
        return "SELECT ?c ?n WHERE { ?c a <http://ex.org/o#Claim> . ?c <http://ex.org/o#policyNumber> ?n . }"

    def test_extracts_typed_property_use_pairs(self):
        v = SPARQLValidator(AsyncMock(), graph_uri_template="https://test.local/{namespace}")
        pairs = v._extract_typed_property_uses(self._typed_sparql())
        assert ("http://ex.org/o#Claim", "http://ex.org/o#policyNumber") in pairs

    def test_extracts_prefixed_name_pairs(self):
        """prefixed names (ind:Foo) are expanded via PREFIX decls and checked."""
        v = SPARQLValidator(AsyncMock(), graph_uri_template="https://test.local/{namespace}")
        sparql = "PREFIX ind: <http://ex.org/o#>\nSELECT ?c ?n WHERE { ?c a ind:Claim . ?c ind:policyNumber ?n . }"
        pairs = v._extract_typed_property_uses(sparql)
        assert ("http://ex.org/o#Claim", "http://ex.org/o#policyNumber") in pairs

    async def test_prefixed_name_domain_mismatch_fails(self):
        """End-to-end: a prefixed-name domain mismatch is caught."""
        client = AsyncMock()
        client.ask.side_effect = [False, True]  # not-compatible, but declares a domain → mismatch
        client.query.return_value = [{"found": "0"}]
        v = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")
        sparql = "PREFIX ind: <http://ex.org/o#>\nSELECT ?c ?n WHERE { ?c a ind:Claim . ?c ind:policyNumber ?n . }"
        result = await v._validate_domain_range(sparql, "ns")
        assert result.valid is False

    async def test_clear_mismatch_fails(self):
        # ask #1 (compatible domain?) -> False; ask #2 (declares any domain?) -> True => mismatch
        v = self._validator([False, True])
        result = await v._validate_domain_range(self._typed_sparql(), "ns")
        assert result.valid is False

    async def test_compatible_domain_passes(self):
        # ask #1 (compatible domain?) -> True
        v = self._validator([True])
        result = await v._validate_domain_range(self._typed_sparql(), "ns")
        assert result.valid is True

    async def test_no_declared_domain_fails_open(self):
        # ask #1 -> False, ask #2 -> False (no declared domain) => None => pass
        v = self._validator([False, False])
        result = await v._validate_domain_range(self._typed_sparql(), "ns")
        assert result.valid is True

    async def test_neptune_error_fails_open(self):
        client = AsyncMock()
        client.ask.side_effect = RuntimeError("neptune down")
        v = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")
        result = await v._validate_domain_range(self._typed_sparql(), "ns")
        assert result.valid is True


@pytest.mark.unit
class TestConfidenceCalibration:
    def _translator(self):
        return NLtoSPARQL(graph_client=AsyncMock(), llm_client=AsyncMock())

    def test_validation_and_strong_grounding_boosts(self):
        t = self._translator()
        out = t._calibrate_confidence(
            0.7, validation_passed=True, vector_hits=[_hit("https://x", 0.9)], sparql="SELECT ?x WHERE { ?x a ?y . }"
        )
        assert out > 0.7

    def test_weak_grounding_penalizes(self):
        t = self._translator()
        out = t._calibrate_confidence(
            0.7, validation_passed=True, vector_hits=[_hit("https://x", 0.3)], sparql="SELECT ?x WHERE { ?x a ?y . }"
        )
        # +0.05 validation, -0.1 weak grounding => net below base
        assert out < 0.7

    def test_high_complexity_penalizes(self):
        t = self._translator()
        complex_sparql = "SELECT ?a WHERE { " + " ".join(f"?x{i} ?p{i} ?o{i} ." for i in range(10)) + " }"
        out = t._calibrate_confidence(0.9, validation_passed=True, vector_hits=None, sparql=complex_sparql)
        assert out < 0.95  # complexity penalty applied

    def test_bounded_to_unit_interval(self):
        t = self._translator()
        out = t._calibrate_confidence(
            1.0, validation_passed=True, vector_hits=[_hit("https://x", 0.99)], sparql="SELECT ?x WHERE { ?x a ?y }"
        )
        assert 0.0 <= out <= 1.0
