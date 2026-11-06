# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The native (RDF_SPARQL) translator: backend gating, prompt, calibration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_common.constants import StructuredBackend
from coa_serve.clients.base import ConverseResult
from coa_serve.tier2.ontop.nl_to_sparql import _SYSTEM_PROMPT, NLtoSPARQL
from coa_serve.tier2.ontop.sparql_validator import ValidationResult
from coa_serve.tier2.ontop.tbox_context import TBoxContext
from coa_serve.tier2.ontop.types import VectorHit
from coa_serve.tier2.sparql.nl_to_sparql import NATIVE_SPARQL_SYSTEM_PROMPT, NativeNLtoSPARQL

pytestmark = pytest.mark.unit

_RESPONSE = """```sparql
PREFIX ex: <http://example.org/o#>
SELECT ?ticket WHERE { ?ticket a ex:Ticket . }
```
Confidence: 0.8
"""


def _hit(score=0.9) -> VectorHit:
    uri = "http://example.org/o#Ticket"
    return VectorHit(type="ontology_class", score=score, entity_id=uri, uri=uri, metadata={})


def _wire(translator):
    """Replace the T-Box builder and validator so only the plumbing is exercised."""
    tbox = TBoxContext(
        classes=[{"uri": "http://example.org/o#Ticket", "label": "Ticket", "parent": None}], properties=[]
    )
    builder = MagicMock()
    builder.build = AsyncMock(return_value=tbox)
    builder.format_for_prompt = MagicMock(return_value="schema")
    translator._tbox_builder = builder
    translator._validator = MagicMock(validate=AsyncMock(return_value=ValidationResult(valid=True)))
    return builder


def _llm():
    llm = AsyncMock()
    llm.converse.return_value = ConverseResult(text=_RESPONSE)
    return llm


class TestBackendPlumbing:
    async def test_ontop_translator_defaults_to_the_sql_backend_and_prompt(self):
        llm = _llm()
        translator = NLtoSPARQL(AsyncMock(), llm)
        builder = _wire(translator)
        await translator.translate("tickets", "ns", vector_hits=[_hit()])
        assert builder.build.call_args.kwargs["backend"] is StructuredBackend.SQL
        assert llm.converse.call_args.kwargs["system"] == _SYSTEM_PROMPT

    async def test_native_translator_gates_to_rdf_sparql_with_the_full_prompt(self):
        llm = _llm()
        translator = NativeNLtoSPARQL(AsyncMock(), llm)
        builder = _wire(translator)
        result = await translator.translate("tickets", "ns", vector_hits=[_hit()])
        assert result.valid
        assert "SELECT ?ticket" in result.sparql
        assert builder.build.call_args.kwargs["backend"] is StructuredBackend.RDF_SPARQL
        assert llm.converse.call_args.kwargs["system"] == NATIVE_SPARQL_SYSTEM_PROMPT

    def test_native_translator_loads_no_ontop_few_shots(self):
        assert NativeNLtoSPARQL(AsyncMock(), AsyncMock())._few_shot_loader is None

    def test_native_translator_forwards_the_live_guardrail_provider(self):
        translator = NativeNLtoSPARQL(AsyncMock(), AsyncMock(), guardrail_id_provider=lambda: "gr-live")
        assert translator._effective_guardrail_id() == "gr-live"


class TestExcludedProperties:
    async def test_excluded_properties_never_reach_the_schema_or_the_result(self):
        llm = _llm()
        translator = NativeNLtoSPARQL(AsyncMock(), llm)
        builder = _wire(translator)
        title, name = "http://example.org/o#title", "http://example.org/o#name"
        builder.build.return_value = TBoxContext(
            classes=[{"uri": "http://example.org/o#Ticket", "label": "Ticket", "parent": None}],
            properties=[{"uri": title, "label": "title"}, {"uri": name, "label": "name"}],
            object_properties=[{"uri": title, "label": "title"}],
        )
        result = await translator.translate(
            "tickets", "ns", vector_hits=[_hit()], exclude_properties=frozenset({title})
        )
        (shown,) = builder.format_for_prompt.call_args.args[:1]
        assert [p["uri"] for p in shown.properties] == [name] and shown.object_properties == []
        assert [p["uri"] for p in result.tbox_context.properties] == [name]


class TestNativePrompt:
    def test_prompt_allows_graph_native_constructs_and_forbids_updates(self):
        assert "Property paths" in NATIVE_SPARQL_SYSTEM_PROMPT
        assert "NEVER emit INSERT/DELETE" in NATIVE_SPARQL_SYSTEM_PROMPT
        assert "NEVER use { SELECT" not in NATIVE_SPARQL_SYSTEM_PROMPT  # the Ontop-only rule

    def test_prompt_tells_the_model_not_to_name_a_graph(self):
        assert "NEVER use GRAPH, FROM or FROM NAMED" in NATIVE_SPARQL_SYSTEM_PROMPT

    def test_prompt_is_engine_agnostic(self):
        assert "Neptune" not in NATIVE_SPARQL_SYSTEM_PROMPT


class TestNativeCalibration:
    def _calibrate(self, sparql, score=0.9, base=0.8):
        return NativeNLtoSPARQL(AsyncMock(), AsyncMock())._calibrate_confidence(
            base, validation_passed=True, vector_hits=[_hit(score)], sparql=sparql
        )

    def test_a_moderately_complex_query_is_not_penalised(self):
        """Six triples would cost 0.05 on the Ontop path; a triple store runs them natively."""
        sparql = "SELECT * WHERE { ?a ?b ?c . ?a ?d ?e . ?e ?f ?g . ?g ?h ?i . ?i ?j ?k . ?k ?l ?m . }"
        assert self._calibrate(sparql) == pytest.approx(0.9)

    def test_a_very_complex_query_is_penalised(self):
        sparql = "SELECT * WHERE { " + " ".join(f"?a{i} ?p ?b{i} ." for i in range(14)) + " }"
        assert self._calibrate(sparql) == pytest.approx(0.85)

    def test_weak_retrieval_is_penalised(self):
        assert self._calibrate("SELECT * WHERE { ?a ?b ?c . }", score=0.3) == pytest.approx(0.75)
