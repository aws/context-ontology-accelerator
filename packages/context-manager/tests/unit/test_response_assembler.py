# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ResponseAssembler."""

import pytest
from coa_serve.response_assembler import ResponseAssembler
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit


@pytest.fixture
def assembler():
    return ResponseAssembler()


@pytest.fixture
def trace():
    t = TraceCollector()
    t.record("test_step", "success", 10)
    return t


@pytest.mark.unit
class TestAssembleTier1:
    def test_tier1_has_confidence_1(self, assembler, trace):
        result = assembler.assemble_tier1(
            rows=[{"revenue": 100}],
            columns=["revenue"],
            metric_name="total_revenue",
            sql_used="SELECT sum(r) FROM t",
            trace=trace,
            namespace="demo",
        )
        assert result.tier == 1
        assert result.confidence.score == 1.0
        assert result.result_rows == [{"revenue": 100}]
        assert result.query_used == "SELECT sum(r) FROM t"
        assert result.metadata["namespace"] == "demo"

    def test_tier1_trace_is_typed(self, assembler, trace):
        result = assembler.assemble_tier1(
            rows=[],
            columns=[],
            metric_name="m",
            sql_used="",
            trace=trace,
            namespace="ns",
        )
        assert len(result.trace) == 1
        assert result.trace[0].step == "test_step"


@pytest.mark.unit
class TestOntologyVersionParity:
    """Every assembler can name the ontology version when the caller has one,
    and stays null-by-default when it does not (#986)."""

    def test_tier1_accepts_and_surfaces_ontology_version(self, assembler, trace):
        result = assembler.assemble_tier1(
            rows=[],
            columns=[],
            metric_name="m",
            sql_used="",
            trace=trace,
            namespace="ns",
            ontology_version="2026-09-12T08:15:00Z",
        )
        assert result.ontology_version == "2026-09-12T08:15:00Z"

    def test_nl_to_sql_accepts_and_surfaces_ontology_version(self, assembler, trace):
        result = assembler.assemble_nl_to_sql(
            rows=[],
            columns=[],
            sql_used="SELECT 1",
            confidence=0.7,
            retrieved_tables=[],
            expanded_tables=[],
            trace=trace,
            namespace="ns",
            ontology_version="2026-09-12T08:15:00Z",
        )
        assert result.ontology_version == "2026-09-12T08:15:00Z"

    def test_tier3_accepts_and_surfaces_ontology_version(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="answer",
            supporting_content=[],
            graph_context=[],
            confidence=0.6,
            trace=trace,
            namespace="ns",
            ontology_version="2026-09-12T08:15:00Z",
        )
        assert result.ontology_version == "2026-09-12T08:15:00Z"

    def test_all_assemblers_default_to_null(self, assembler, trace):
        tier1 = assembler.assemble_tier1(rows=[], columns=[], metric_name="m", sql_used="", trace=trace, namespace="ns")
        nl_to_sql = assembler.assemble_nl_to_sql(
            rows=[],
            columns=[],
            sql_used="",
            confidence=0.5,
            retrieved_tables=[],
            expanded_tables=[],
            trace=trace,
            namespace="ns",
        )
        tier3 = assembler.assemble_tier3(
            synthesized_answer="a",
            supporting_content=[],
            graph_context=[],
            confidence=0.5,
            trace=trace,
            namespace="ns",
        )
        assert tier1.ontology_version is None
        assert nl_to_sql.ontology_version is None
        assert tier3.ontology_version is None


@pytest.mark.unit
class TestAssembleTier2:
    def test_tier2_includes_sparql_and_sql(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=[{"id": "1"}],
            columns=["id"],
            sql_used="SELECT id FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.9,
            ontology_version="v2",
            data_sources=["table_a"],
            trace=trace,
            namespace="demo",
        )
        assert result.tier == 2
        assert result.confidence.score == 0.9
        assert result.sparql_generated == "SELECT ?x WHERE { ?x a :T }"
        assert result.query_used == "SELECT id FROM t"
        assert result.ontology_version == "v2"
        assert result.data_sources == ["table_a"]

    def test_tier2_handles_none_rows(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=None,
            columns=None,
            sql_used=None,
            sparql="SELECT ?x WHERE {}",
            confidence=0.5,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns",
        )
        assert result.result_rows is None


@pytest.mark.unit
class TestAssembleTier3:
    def test_tier3_includes_synthesis(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="The answer is 42.",
            supporting_content=[{"chunkId": "c1", "text": "..."}],
            graph_context=[{"uri": "urn:x", "label": "X"}],
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )
        assert result.tier == 3
        assert result.synthesized_answer == "The answer is 42."
        assert result.supporting_content == [{"chunkId": "c1", "text": "..."}]
        assert result.graph_context == {"entities": [{"uri": "urn:x", "label": "X"}]}
        assert result.confidence.score == 0.8
        assert result.partial is False  # clean (no degraded sources)

    def test_tier3_partial_true_with_degraded_sources(self, assembler, trace):
        """a degraded retrieval source means partial-context synthesis → partial=true."""
        result = assembler.assemble_tier3(
            synthesized_answer="Partial answer.",
            supporting_content=[],
            graph_context=[],
            confidence=0.4,
            trace=trace,
            namespace="demo",
            degraded_sources=[{"source": "vector_search", "failureType": "timeout", "impact": "..."}],
        )
        assert result.partial is True
        assert result.metadata["degradedSources"][0]["source"] == "vector_search"


# ── confidence calibration + trace-completeness audit ───────────


@pytest.mark.unit
class TestTier2Calibration:
    """B1: Tier-2 confidence is calibrated from in-hand signals, not a blind
    passthrough of the LLM self-rating."""

    def test_empty_result_lowers_confidence_below_self_rating(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=[],
            columns=["id"],
            sql_used="SELECT id FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.9,
            ontology_version="v2",
            data_sources=["table_a"],
            trace=trace,
            namespace="demo",
            row_count=0,
        )
        # empty result must strictly lower the score vs the 0.9 self-rating
        assert result.confidence.score < 0.9
        assert "empty result" in result.confidence.rationale.lower()
        assert 0.0 <= result.confidence.score <= 1.0

    def test_non_empty_non_truncated_preserves_base_score(self, assembler, trace):
        # back-compat: rows present, not truncated → unchanged self-rating
        result = assembler.assemble_tier2(
            rows=[{"id": "1"}, {"id": "2"}],
            columns=["id"],
            sql_used="SELECT id FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.9,
            ontology_version="v2",
            data_sources=["table_a"],
            trace=trace,
            namespace="demo",
            row_count=5,
            truncated=False,
        )
        assert result.confidence.score == 0.9

    def test_empty_rows_without_explicit_row_count_still_penalized(self, assembler, trace):
        # codex hardening: a visible empty result must be penalized even if the
        # caller omits row_count (derive it from len(rows)). Guards the direct-call
        # footgun where rows=[] + omitted row_count would otherwise escape.
        result = assembler.assemble_tier2(
            rows=[],
            columns=["id"],
            sql_used="SELECT id FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.9,
            ontology_version="v2",
            data_sources=["table_a"],
            trace=trace,
            namespace="demo",
            # row_count intentionally omitted
        )
        assert result.confidence.score < 0.9
        assert "empty result" in result.confidence.rationale.lower()

    def test_none_rows_without_row_count_not_penalized(self, assembler, trace):
        # rows=None means "unknown / not applicable" (e.g. failed-rows path) — must
        # NOT be treated as an empty result. Preserves test_tier2_handles_none_rows.
        result = assembler.assemble_tier2(
            rows=None,
            columns=None,
            sql_used=None,
            sparql="SELECT ?x WHERE {}",
            confidence=0.7,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns",
            # row_count intentionally omitted
        )
        assert result.confidence.score == 0.7

    def test_explicit_row_count_zero_with_none_rows_is_confirmed_empty(self, assembler, trace):
        # Contract (MR !279 review): row_count=0 is the caller's ASSERTION of a
        # confirmed-empty executed result (the executor QueryResult requires
        # row_count, so 0 always means executed-and-empty) — penalized even when
        # rows aren't materialized. Unknown is expressed as row_count=None, never 0.
        result = assembler.assemble_tier2(
            rows=None,
            columns=None,
            sql_used="SELECT 1",
            sparql="SELECT ?x WHERE {}",
            confidence=0.8,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns",
            row_count=0,
        )
        assert result.confidence.score < 0.8
        assert "empty result" in result.confidence.rationale.lower()

    def test_truncated_annotates_rationale_within_bounds(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=[{"id": "1"}],
            columns=["id"],
            sql_used="SELECT id FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.8,
            ontology_version="v2",
            data_sources=["table_a"],
            trace=trace,
            namespace="demo",
            row_count=1000,
            truncated=True,
        )
        assert 0.0 <= result.confidence.score <= 1.0
        assert "truncat" in result.confidence.rationale.lower()


@pytest.mark.unit
class TestTier3GuardrailClamp:
    """B2: a guardrail-blocked Tier-3 response is clamped to 0.0 confidence at
    the assembler (defense-in-depth — can only LOWER, never raise)."""

    def test_guardrail_blocked_forces_zero(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="",
            supporting_content=[],
            graph_context=[],
            confidence=0.8,
            guardrail_blocked=True,
            trace=trace,
            namespace="demo",
        )
        assert result.confidence.score == 0.0

    def test_guardrail_not_blocked_preserves_confidence(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="The answer is 42.",
            supporting_content=[{"chunkId": "c1", "text": "..."}],
            graph_context=[{"uri": "urn:x", "label": "X"}],
            confidence=0.8,
            guardrail_blocked=False,
            trace=trace,
            namespace="demo",
        )
        assert result.confidence.score == 0.8


@pytest.mark.unit
class TestTier1ConfidenceRegression:
    """A1-audit: Tier-1 confidence stays the deterministic 1.0 per LLD."""

    def test_tier1_confidence_unchanged(self, assembler, trace):
        result = assembler.assemble_tier1(
            rows=[{"revenue": 100}],
            columns=["revenue"],
            metric_name="total_revenue",
            sql_used="SELECT sum(r) FROM t",
            trace=trace,
            namespace="demo",
        )
        assert result.confidence.score == 1.0


class TestModelIdInMetadata:
    """Verify per-request model_id override propagates to response metadata."""

    @pytest.fixture
    def assembler(self):
        from coa_serve.response_assembler import ResponseAssembler

        return ResponseAssembler(model_id="default-model")

    @pytest.fixture
    def trace(self):
        from coa_serve.trace import TraceCollector

        return TraceCollector()

    def test_tier2_metadata_default_model(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=[{"x": 1}],
            columns=["x"],
            sql_used="SELECT 1",
            sparql="SELECT ?x WHERE { ?x a :Foo }",
            confidence=0.9,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns1",
        )
        assert result.metadata["modelId"] == "default-model"

    def test_tier2_metadata_with_override(self, assembler, trace):
        result = assembler.assemble_tier2(
            rows=[{"x": 1}],
            columns=["x"],
            sql_used="SELECT 1",
            sparql="SELECT ?x WHERE { ?x a :Foo }",
            confidence=0.9,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns1",
            model_id="us.anthropic.claude-opus-4-0",
        )
        assert result.metadata["modelId"] == "us.anthropic.claude-opus-4-0"

    def test_tier3_metadata_with_override(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="Answer",
            supporting_content=[],
            graph_context=[],
            confidence=0.8,
            trace=trace,
            namespace="ns1",
            model_id="us.anthropic.claude-haiku-4-0",
        )
        assert result.metadata["modelId"] == "us.anthropic.claude-haiku-4-0"

    def test_tier3_metadata_none_uses_default(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="Answer",
            supporting_content=[],
            graph_context=[],
            confidence=0.8,
            trace=trace,
            namespace="ns1",
            model_id=None,
        )
        assert result.metadata["modelId"] == "default-model"

    def test_tier1_metadata_with_override(self, assembler, trace):
        result = assembler.assemble_tier1(
            rows=[{"rev": 100}],
            columns=["rev"],
            metric_name="revenue",
            sql_used="SELECT 1",
            trace=trace,
            namespace="ns1",
            model_id="custom-model",
        )
        assert result.metadata["modelId"] == "custom-model"

    def test_nl_to_sql_metadata_with_override(self, assembler, trace):
        result = assembler.assemble_nl_to_sql(
            rows=[{"c": 1}],
            columns=["c"],
            sql_used="SELECT 1",
            confidence=0.85,
            retrieved_tables=["t1"],
            expanded_tables=["t1", "t2"],
            trace=trace,
            namespace="ns1",
            model_id="override-model",
        )
        assert result.metadata["modelId"] == "override-model"
