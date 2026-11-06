# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Ontop path turns the prompt's NOT_ANSWERABLE SPARQL into the refusal row the Tier-2 gate reads.

The row has one ``answer`` cell and a bare ``SELECT '...' AS answer`` as its SQL, the same shape the
NL->SQL writer's refusal has, so both paths are treated alike wherever that shape is checked.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.tier2.ontop.strategy import OntopStrategy, _not_answerable_reason
from coa_serve.tier2.strategy import StrategyContext, StrategyOption
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

REFUSAL_SPARQL = 'SELECT ?answer WHERE { BIND("NOT_ANSWERABLE_FIELD: no cost field for service job" AS ?answer) }'
BARE_LITERAL_SELECT = re.compile(
    r"^select\s+'not_answerable_(?:range|field):(?:[^']|'')*'\s+as\s+answer$", re.IGNORECASE
)


def _translation(sparql: str, confidence: float = 0.3):
    result = MagicMock()
    result.valid = True
    result.sparql = sparql
    result.confidence = confidence
    result.trace_steps = []
    return result


def _context() -> StrategyContext:
    return StrategyContext(
        embedding=None,
        profile={"userId": "user-1"},
        options={"maxResults": 100, "dataSourceId": "ds-1"},
        trace=TraceCollector(),
    )


@pytest.mark.parametrize(
    "sparql, expected",
    [
        (REFUSAL_SPARQL, "NOT_ANSWERABLE_FIELD: no cost field for service job"),
        (
            'select ?answer where {bind("NOT_ANSWERABLE_RANGE: it\'s  outside\ndata" as ?answer)} LIMIT 10',
            "NOT_ANSWERABLE_RANGE: it's outside data",
        ),
        # a real query, or one that only mentions the prefix, is not a refusal
        ("SELECT ?x WHERE { ?x a :Foo }", None),
        ('SELECT ?answer WHERE { ?x :note ?answer . FILTER(STRSTARTS(?answer, "NOT_ANSWERABLE:")) }', None),
        ('SELECT ?answer WHERE { ?x :p ?v . BIND("NOT_ANSWERABLE_RANGE: x" AS ?answer) }', None),
        ('SELECT ?answer WHERE { BIND("42" AS ?answer) }', None),
        ('SELECT ?answer WHERE { BIND("not_answerable: x" AS ?answer) }', None),
    ],
)
def test_sentinel_is_recognised_only_as_the_bare_refusal_query(sparql, expected):
    assert _not_answerable_reason(sparql) == expected


@pytest.mark.asyncio
async def test_refusal_sparql_becomes_the_refusal_row_without_running_the_vkg():
    nl_to_sparql, vkg = AsyncMock(), AsyncMock()
    nl_to_sparql.translate.return_value = _translation(REFUSAL_SPARQL)
    strategy = OntopStrategy(nl_to_sparql=nl_to_sparql, vkg_translator=vkg)

    result = await strategy.resolve("What did service job 1234 cost?", "ns", _context())

    assert result is not None
    assert result.strategy_name == StrategyOption.ONTOP
    assert result.rows == [{"answer": "NOT_ANSWERABLE_FIELD: no cost field for service job"}]
    assert result.columns == ["answer"]
    assert result.row_count == 1
    assert BARE_LITERAL_SELECT.match(result.sql)
    vkg.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_reason_with_a_quote_keeps_the_sql_a_single_literal():
    sparql = 'SELECT ?answer WHERE { BIND("NOT_ANSWERABLE_RANGE: the plan\'s year is outside the data" AS ?answer) }'
    nl_to_sparql = AsyncMock()
    nl_to_sparql.translate.return_value = _translation(sparql)
    strategy = OntopStrategy(nl_to_sparql=nl_to_sparql, vkg_translator=AsyncMock())

    result = await strategy.resolve("q", "ns", _context())

    assert result.rows[0]["answer"] == "NOT_ANSWERABLE_RANGE: the plan's year is outside the data"
    assert result.sql == "SELECT 'NOT_ANSWERABLE_RANGE: the plan''s year is outside the data' AS answer"
    assert BARE_LITERAL_SELECT.match(result.sql)


@pytest.mark.asyncio
async def test_ordinary_sparql_still_goes_through_the_vkg():
    nl_to_sparql, vkg = AsyncMock(), AsyncMock()
    nl_to_sparql.translate.return_value = _translation("SELECT ?x WHERE { ?x a :Foo }")
    strategy = OntopStrategy(nl_to_sparql=nl_to_sparql, vkg_translator=vkg)
    strategy._resolve_with_retry = AsyncMock(return_value=None)

    await strategy.resolve("q", "ns", _context())

    strategy._resolve_with_retry.assert_awaited_once()
