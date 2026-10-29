# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ResponseAssembler."""

import json

import pytest
from coa_serve.response_assembler import (
    MAX_GRAPH_CONTEXT_ENTITIES,
    MAX_GRAPH_CONTEXT_RELATIONSHIPS,
    ResponseAssembler,
)
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
            include_debug_info=True,  # gate on; verify query_used populates
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
            include_debug_info=True,  # gate on; verify both fields populate
        )
        assert result.tier == 2
        assert result.confidence.score == 0.9
        assert result.sparql_generated == "SELECT ?x WHERE { ?x a :T }"
        assert result.query_used == "SELECT id FROM t"
        assert result.ontology_version == "v2"
        assert result.data_sources == ["table_a"]

    def test_debug_info_gate_defaults_off_and_withholds_query_and_sparql(self, assembler, trace):
        """query_used / sparql_generated must be None by default (info-disclosure gate).

        Regression test — the executed SQL / generated SPARQL
        expose internal schema and only reach the caller when the caller opts in
        via options.includeDebugInfo=true.
        """
        # Tier 1 default → no query_used
        t1 = assembler.assemble_tier1(
            rows=[],
            columns=[],
            metric_name="m",
            sql_used="SELECT * FROM sensitive_table",
            trace=trace,
            namespace="ns",
        )
        assert t1.query_used is None

        # Tier 2 default → no query_used and no sparql_generated
        t2 = assembler.assemble_tier2(
            rows=[],
            columns=[],
            sql_used="SELECT * FROM t",
            sparql="SELECT ?x WHERE { ?x a :T }",
            confidence=0.5,
            ontology_version=None,
            data_sources=None,
            trace=trace,
            namespace="ns",
        )
        assert t2.query_used is None
        assert t2.sparql_generated is None

        # NL-to-SQL default → no query_used
        nl = assembler.assemble_nl_to_sql(
            rows=[],
            columns=[],
            sql_used="SELECT * FROM t",
            confidence=0.5,
            retrieved_tables=[],
            expanded_tables=[],
            trace=trace,
            namespace="ns",
        )
        assert nl.query_used is None

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
        assert result.graph_context == {
            "entities": [{"uri": "urn:x", "label": "X"}],
            "relationships": [],
        }
        assert result.confidence.score == 0.8
        assert result.partial is False  # clean (no degraded sources)

    def test_tier3_graph_only_context_uses_canonical_smithy_shape(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="Amazon competes with Walmart.",
            supporting_content=[],
            graph_context=[
                {
                    "uri": "e:walmart",
                    "label": "Walmart",
                    "type": "Company",
                    "relationships": (
                        {
                            "source_uri": "e:amazon",
                            "predicate_uri": "urn:predicate:competes-with",
                            "predicate_label": "COMPETES_WITH",
                            "target_uri": "e:walmart",
                            "target_label": "Walmart",
                        },
                    ),
                }
            ],
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )

        assert result.supporting_content == []
        assert result.graph_context == {
            "entities": [{"uri": "e:walmart", "label": "Walmart", "type": "Company"}],
            "relationships": [
                {
                    "sourceUri": "e:amazon",
                    "predicateUri": "urn:predicate:competes-with",
                    "targetUri": "e:walmart",
                    "predicateLabel": "COMPETES_WITH",
                }
            ],
        }
        wire_result = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        assert wire_result["supportingContent"] == []
        assert wire_result["graphContext"] == result.graph_context

    def test_tier3_never_promotes_a_predicate_label_to_predicate_uri(self, assembler, trace):
        # An edge with NO predicate identity at all (only an untyped ``predicate``
        # key) still cannot go on the wire — ``predicateUri`` is @required and a
        # human-readable label is not an identifier. What changed is that the drop is
        # now OBSERVABLE: an empty ``relationships`` list must not be ambiguous
        # between "no edges found" and "every edge discarded", because the
        # synthesizer may already have asserted the relationship in the answer.
        result = assembler.assemble_tier3(
            synthesized_answer="Amazon competes with Walmart.",
            supporting_content=[],
            graph_context=[
                {
                    "uri": "e:walmart",
                    "label": "Walmart",
                    "relationships": (
                        {
                            "source_uri": "e:amazon",
                            "predicate": "COMPETES_WITH",
                            "target_uri": "e:walmart",
                        },
                    ),
                }
            ],
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )

        assert result.graph_context["relationships"] == []
        degraded = result.metadata["degradedSources"]
        assert [entry["reason"] for entry in degraded] == ["graph_context_evidence_dropped"]
        assert "1 relationship(s) with no predicate identity" in degraded[0]["detail"]
        assert result.partial is True
        degraded_steps = [step for step in result.trace if step.status == "degraded"]
        assert len(degraded_steps) == 1
        assert "no predicate identity" in degraded_steps[0].detail

    def test_tier3_reports_dropped_entities_without_a_uri(self, assembler, trace):
        result = assembler.assemble_tier3(
            synthesized_answer="Answer.",
            supporting_content=[],
            graph_context=[{"label": "No identity"}, {"uri": "e:kept", "label": "Kept"}],
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )

        assert [entity["uri"] for entity in result.graph_context["entities"]] == ["e:kept"]
        assert "1 entities with no uri" in result.metadata["degradedSources"][0]["detail"]

    def test_tier3_bounds_graph_context_entity_and_relationship_counts(self, assembler, trace):
        entities = [
            {
                "uri": f"e:{index}",
                "label": f"Entity {index}",
                "relationships": (
                    {
                        "source_uri": f"e:{index}",
                        "predicate_uri": "p:rel",
                        "target_uri": f"e:{index + 1}",
                    },
                ),
            }
            for index in range(MAX_GRAPH_CONTEXT_ENTITIES + 50)
        ]

        result = assembler.assemble_tier3(
            synthesized_answer="Answer.",
            supporting_content=[],
            graph_context=entities,
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )

        assert len(result.graph_context["entities"]) == MAX_GRAPH_CONTEXT_ENTITIES
        assert len(result.graph_context["relationships"]) <= MAX_GRAPH_CONTEXT_RELATIONSHIPS
        assert "50 entities over the" in result.metadata["degradedSources"][0]["detail"]

    def test_tier3_fairly_bounds_oversized_multibyte_graph_statements(self, assembler, trace):
        oversized = "界" * 5_000
        result = assembler.assemble_tier3(
            synthesized_answer="Graph-backed answer.",
            supporting_content=[],
            graph_context=[
                {
                    "uri": "urn:first",
                    "label": "First",
                    "properties": {"supportingStatements": json.dumps([oversized, "first-extra"])},
                },
                {
                    "uri": "urn:second",
                    "label": "Second",
                    "properties": {"supporting_statements": [oversized, "second-extra"]},
                },
            ],
            confidence=0.8,
            trace=trace,
            namespace="demo",
        )

        statement_groups = [
            json.loads(entity["properties"]["supportingStatements"]) for entity in result.graph_context["entities"]
        ]
        assert all(statement_groups)
        statements = [statement for group in statement_groups for statement in group]
        assert all(len(statement) <= 1_000 for statement in statements)
        assert all(len(statement.encode("utf-8")) <= 2_000 for statement in statements)
        assert sum(len(statement) for statement in statements) <= 4_000
        assert sum(len(statement.encode("utf-8")) for statement in statements) <= 6_000
        assert all("supporting_statements" not in entity["properties"] for entity in result.graph_context["entities"])

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

    # ── #1166: Deep Reasoning mode label + query artifacts ──────────────

    def test_tier3_execution_mode_surfaced(self, assembler, trace):
        """#1166 cut #2: the resolved execution mode is surfaced structurally so
        the console labels a Deep Reasoning answer by MODE, not by its tier=3
        resolution (which would mis-read as Standard mode's Tier-3 label)."""
        result = assembler.assemble_tier3(
            synthesized_answer="Deep answer.",
            supporting_content=[],
            graph_context=[],
            confidence=0.7,
            trace=trace,
            namespace="demo",
            execution_mode="deep-reasoning",
        )
        assert result.tier == 3
        assert result.execution_mode == "deep-reasoning"

    def test_tier3_execution_mode_defaults_none(self, assembler, trace):
        """Standard Tier-3 responses (no mode passed) leave execution_mode None;
        the console falls back to tier-based labelling for those."""
        result = assembler.assemble_tier3(
            synthesized_answer="Standard tier3 answer.",
            supporting_content=[],
            graph_context=[],
            confidence=0.7,
            trace=trace,
            namespace="demo",
        )
        assert result.execution_mode is None

    def test_tier3_query_artifacts_surfaced_when_debug_info_on(self, assembler, trace):
        """#1166 cut #3: Deep Reasoning returns the SQL/SPARQL it executed, same
        as Standard mode — but only when the caller opts into debug info."""
        result = assembler.assemble_tier3(
            synthesized_answer="Revenue was 100.",
            supporting_content=[],
            graph_context=[],
            confidence=0.8,
            trace=trace,
            namespace="demo",
            query_used="SELECT revenue FROM t",
            sparql_generated="SELECT ?r WHERE { ?x :revenue ?r }",
            include_debug_info=True,
        )
        assert result.query_used == "SELECT revenue FROM t"
        assert result.sparql_generated == "SELECT ?r WHERE { ?x :revenue ?r }"

    def test_tier3_query_artifacts_withheld_by_default(self, assembler, trace):
        """Negative control: without includeDebugInfo the Deep Reasoning query
        artifacts are withheld exactly like the Tier-2 gate — they expose internal
        schema and must not leak on a default request (#1166)."""
        result = assembler.assemble_tier3(
            synthesized_answer="Revenue was 100.",
            supporting_content=[],
            graph_context=[],
            confidence=0.8,
            trace=trace,
            namespace="demo",
            query_used="SELECT revenue FROM sensitive_t",
            sparql_generated="SELECT ?r WHERE { ?x :revenue ?r }",
            # include_debug_info defaults to False
        )
        assert result.query_used is None
        assert result.sparql_generated is None


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
