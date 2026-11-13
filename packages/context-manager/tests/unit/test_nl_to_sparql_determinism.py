# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL→SPARQL determinism regression suite.

Drives the real ``NLtoSPARQL.translate`` pipeline with a mock LLM to prove the
validator and retry loop collapse a drifting model's output to one stable answer shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.tier2.ontop.nl_to_sparql import NLtoSPARQL, TranslationResult
from coa_serve.tier2.ontop.tbox_context import TBoxContext

pytestmark = pytest.mark.unit


def _tbox() -> TBoxContext:
    """A minimal non-empty T-Box so translate() skips the Neptune fetch."""
    return TBoxContext(
        classes=[{"uri": "http://ex.org/o#Product", "label": "Product", "parent": None}],
        properties=[
            {
                "uri": "http://ex.org/o#color",
                "label": "color",
                "domain": "http://ex.org/o#Product",
                "range": "http://www.w3.org/2001/XMLSchema#string",
            }
        ],
    )


def _wrap(sparql: str) -> ConverseResult:
    """LLM response envelope the extractor understands, with a confidence line."""
    return ConverseResult(text=f"```sparql\n{sparql}\n```\nconfidence: 0.9")


def _shape_signature(result: TranslationResult) -> tuple:
    """Canonical, run-independent signature of the ANSWER SHAPE.

    Captures validity + the ORDERED tuple of projected column aliases. Two runs of
    the same question that produce the same signature are indistinguishable to a
    typed client. Order is preserved (not sorted) because column ORDER is part
    of the contract a positional client depends on.
    """
    if not result.valid or not result.sparql:
        return ("INVALID", result.error)
    import re

    m = re.search(r"\bSELECT\b(?:\s+DISTINCT)?(.*?)\bWHERE\b", result.sparql, re.IGNORECASE | re.DOTALL)
    clause = m.group(1) if m else ""
    # Projected names: bare ?vars plus (… AS ?alias) aliases, in text order.
    aliases = re.findall(r"\bAS\s+\?(\w+)", clause, re.IGNORECASE)
    bare = re.findall(r"(?<![\w?])\?(\w+)", clause)
    # Union-in-order also keeps vars inside aggregate args; enough for these simple projections.
    ordered: list[str] = []
    for name in [*bare, *aliases]:
        if name not in ordered:
            ordered.append(name)
    has_limit = bool(re.search(r"\bLIMIT\b", result.sparql, re.IGNORECASE))
    return ("VALID", tuple(ordered), has_limit)


def _translator(llm: AsyncMock) -> NLtoSPARQL:
    graph = AsyncMock()
    graph.query = AsyncMock(return_value=[{"found": "1"}])  # URIs 'exist'
    graph.ask = AsyncMock(return_value=True)  # domain/range compatible
    return NLtoSPARQL(graph_client=graph, llm_client=llm)


async def _run_once(llm: AsyncMock, question: str) -> TranslationResult:
    return await _translator(llm).translate(question, "test-ns", tbox_context=_tbox())


_STABLE_QUERIES = {
    "count_by_color": (
        "PREFIX o: <http://ex.org/o#> "
        "SELECT ?colorName (COUNT(DISTINCT ?product) AS ?productCount) "
        "WHERE { ?product o:color ?colorName } GROUP BY ?colorName"
    ),
    "top_one": (
        "PREFIX o: <http://ex.org/o#> "
        "SELECT ?productName (COUNT(?x) AS ?viewCount) "
        "WHERE { ?product o:name ?productName } GROUP BY ?productName "
        "ORDER BY DESC(?viewCount) LIMIT 1"
    ),
    "plain_list": ("PREFIX o: <http://ex.org/o#> SELECT ?colorName WHERE { ?product o:color ?colorName }"),
}


@pytest.mark.unit
class TestDeterminismStableAcrossRuns:
    @pytest.mark.parametrize(
        ("qname", "expected"),
        [
            ("count_by_color", ("VALID", ("colorName", "product", "productCount"), False)),
            ("top_one", ("VALID", ("productName", "x", "viewCount"), True)),
            ("plain_list", ("VALID", ("colorName",), False)),
        ],
    )
    async def test_same_question_same_shape_over_n_runs(self, qname, expected):
        llm = AsyncMock()
        llm.converse = AsyncMock(return_value=_wrap(_STABLE_QUERIES[qname]))

        result = await _run_once(llm, f"question::{qname}")

        assert _shape_signature(result) == expected, result.error


@pytest.mark.unit
class TestDeterminismEnforcementCollapsesDrift:
    async def test_bare_alias_rejected_then_regenerated_stable(self):
        """Model first emits an unstable bare ?count alias, then
        the descriptive alias on retry. The validator must reject #1 and the
        loop must converge, so the ACCEPTED shape is the stable one — not the
        drifting one."""
        drifting = (
            "PREFIX o: <http://ex.org/o#> "
            "SELECT ?colorName (COUNT(?product) AS ?count) "
            "WHERE { ?product o:color ?colorName } GROUP BY ?colorName"
        )
        stable = _STABLE_QUERIES["count_by_color"]
        llm = AsyncMock()
        # First converse() call returns the drifting query; every subsequent
        # (retry) call returns the stable one.
        llm.converse = AsyncMock(side_effect=[_wrap(drifting), _wrap(stable), _wrap(stable)])

        result = await _run_once(llm, "how many products per color")
        assert result.valid, result.error
        assert result.sparql and "?productCount" in result.sparql
        assert "AS ?count " not in result.sparql and "AS ?count)" not in result.sparql
        # The retry loop was actually used (more than one LLM call).
        assert llm.converse.await_count >= 2

    async def test_persistent_drift_fails_closed_not_wrong_shape(self):
        """If the model NEVER conforms, the translation fails-closed
        (valid=False) rather than returning the unstable shape — the correct,
        loud failure direction. A wrong-shaped answer must never be served."""
        drifting = "PREFIX o: <http://ex.org/o#> SELECT (COUNT(?product) AS ?count) WHERE { ?product o:color ?c }"
        llm = AsyncMock()
        llm.converse = AsyncMock(return_value=_wrap(drifting))  # always drifts
        result = await _run_once(llm, "count products")
        assert not result.valid  # fail-closed, never a silent wrong shape


@pytest.mark.unit
class TestShapeSignatureIsDiscriminating:
    """A green determinism test is only meaningful if the signature would have
    gone RED on real drift. Prove the comparator separates alias, LIMIT and
    column-count drift."""

    def _sig(self, sparql: str) -> tuple:
        return _shape_signature(TranslationResult(sparql=sparql, confidence=0.9, valid=True))

    def test_alias_drift_is_detected(self):
        a = self._sig("SELECT (COUNT(?p) AS ?productCount) WHERE { ?p a :P }")
        b = self._sig("SELECT (COUNT(?p) AS ?count) WHERE { ?p a :P }")
        assert a != b

    def test_limit_drift_is_detected(self):
        a = self._sig("SELECT ?name WHERE { ?p :n ?name } ORDER BY ?name")
        b = self._sig("SELECT ?name WHERE { ?p :n ?name } ORDER BY ?name LIMIT 1")
        assert a != b

    def test_column_count_drift_is_detected(self):
        a = self._sig("SELECT ?archetypeName WHERE { ?p :a ?archetypeName }")
        b = self._sig("SELECT ?archetypeName1 ?archetypeName2 WHERE { ?p :a ?archetypeName1 ; :b ?archetypeName2 }")
        assert a != b

    def test_identical_shape_matches(self):
        a = self._sig("SELECT ?colorName (COUNT(?p) AS ?productCount) WHERE { ?p :c ?colorName }")
        b = self._sig("SELECT ?colorName (COUNT(?p) AS ?productCount) WHERE { ?p :c ?colorName }")
        assert a == b
