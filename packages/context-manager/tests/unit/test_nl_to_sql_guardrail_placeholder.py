# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 2 never runs SQL that filters on a value the PII guardrail masked.

The primary guardrail anonymizes NAME (and EMAIL, PHONE, ...) in the question, so
"total royalties for Saltmarsh Requiem under LA-450" reaches the SQL writer as
"... for {NAME} ...". SQL that filters on '{NAME}' matches no row and, for an
aggregate, answers 0.00 with high confidence. It must fail as a generation instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.step_ids import StepId
from coa_serve.tier2.guardrail_placeholders import GUARDRAIL_PII_PLACEHOLDERS, masked_literal_in_sql
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy
from coa_serve.tier2.sql_firewall import UnsafeSQLError
from coa_serve.tier2.strategy import StrategyContext
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

_QUESTION = "What were the total royalties reported for Saltmarsh Requiem under LA-450?"
_MASKED_SQL = (
    "SELECT SUM(rs.royalty_amount_usd) AS total FROM royalty_statement rs "
    "JOIN license_agreement la ON rs.license_agreement_id = la.license_agreement_id "
    "JOIN title t ON la.title_id = t.title_id "
    "WHERE la.agreement_code = 'LA-450' AND t.title_name = '{NAME}'"
)
_GUARDRAIL_STACK = Path(__file__).resolve().parents[4] / "infra/lib/stacks/foundation/guardrail-stack.ts"


def _generation(sql: str) -> MagicMock:
    result = MagicMock()
    result.sql = sql
    result.error = None
    result.confidence = 0.9
    result.retrieved_tables = ["royalty_statement", "license_agreement", "title"]
    result.expanded_tables = result.retrieved_tables
    result.trace_steps = []
    result.data_source_id = "ds-1"
    result.ddl_context = "CREATE TABLE title (title_id int, title_name varchar);"
    result.table_sources = {}
    result.table_candidates = {}
    return result


def _firewall_pass(sql: str, *_args, **_kwargs) -> MagicMock:
    result = MagicMock()
    result.denied = False
    result.authorized_sql = sql
    result.reason = None
    return result


def _strategy(
    sql_generator: AsyncMock, query_executor: AsyncMock, firewall: MagicMock | None = None
) -> NLtoSQLStrategy:
    if firewall is None:
        firewall = MagicMock()
        firewall.evaluate.side_effect = _firewall_pass
    return NLtoSQLStrategy(
        sql_generator=sql_generator,
        firewall=firewall,
        query_executor=query_executor,
        oss_ontology_index="test-index",
    )


def _context() -> StrategyContext:
    return StrategyContext(
        embedding=[0.1] * 10,
        profile={"userId": "user-1"},
        options={"maxResults": 100, "dataSourceId": "ds-1"},
        trace=TraceCollector(),
    )


def _failed_steps(context: StrategyContext) -> list[dict]:
    return [step for step in context.trace.steps_serializable if step.get("step") == StepId.T2_SQL_FAILED]


async def test_sql_filtering_on_a_masked_name_fails_instead_of_answering_zero():
    sql_generator = AsyncMock()
    sql_generator.generate.return_value = _generation(_MASKED_SQL)
    query_executor = AsyncMock()
    strategy_firewall = MagicMock()
    context = _context()

    result = await _strategy(sql_generator, query_executor, strategy_firewall).resolve(_QUESTION, "ns1", context)

    assert result is None
    query_executor.execute.assert_not_awaited()
    strategy_firewall.evaluate.assert_not_called()
    assert [step["detail"]["reason"] for step in _failed_steps(context)] == ["guardrail_masked_literal"]


async def test_a_correction_shot_that_filters_on_a_masked_value_is_not_executed():
    sql_generator = AsyncMock()
    sql_generator.generate.return_value = _generation("SELECT bad FROM title")
    sql_generator.correct.return_value = ("SELECT title_id FROM title WHERE title_name LIKE '%{NAME}%'", 0.9)
    query_executor = AsyncMock()
    query_executor.execute.side_effect = [Exception('column "bad" does not exist')]
    context = _context()

    result = await _strategy(sql_generator, query_executor).resolve(_QUESTION, "ns1", context)

    assert result is None
    assert query_executor.execute.await_count == 1
    assert len(_failed_steps(context)) == 1


async def test_a_masked_correction_shot_keeps_the_clean_empty_first_result():
    empty = MagicMock(rows=[], columns=["title_id"], row_count=0, truncated=False)
    sql_generator = AsyncMock()
    sql_generator.generate.return_value = _generation(
        "SELECT title_id FROM title WHERE title_name = 'Requiem Saltmarsh'"
    )
    sql_generator.correct.return_value = ("SELECT title_id FROM title WHERE title_name LIKE '%{NAME}%'", 0.9)
    query_executor = AsyncMock()
    query_executor.execute.return_value = empty

    result = await _strategy(sql_generator, query_executor).resolve(
        "Which title is Saltmarsh Requiem?", "ns1", _context()
    )

    assert result is not None
    assert result.row_count == 0
    assert query_executor.execute.await_count == 1
    sql_generator.correct.assert_awaited_once()


async def test_an_unsafe_correction_shot_keeps_the_clean_empty_first_result():
    first_sql = "SELECT title_id FROM title WHERE title_name = 'Requiem Saltmarsh'"
    firewall = MagicMock()
    firewall.evaluate.side_effect = [_firewall_pass(first_sql), UnsafeSQLError("DROP is not allowed")]
    sql_generator = AsyncMock()
    sql_generator.generate.return_value = _generation(first_sql)
    sql_generator.correct.return_value = ("DROP TABLE title", 0.9)
    query_executor = AsyncMock()
    query_executor.execute.return_value = MagicMock(rows=[], columns=["title_id"], row_count=0, truncated=False)

    result = await _strategy(sql_generator, query_executor, firewall).resolve(
        "Which title is Saltmarsh Requiem?", "ns1", _context()
    )

    assert result is not None
    assert result.row_count == 0
    assert result.sql == first_sql
    assert query_executor.execute.await_count == 1
    assert firewall.evaluate.call_count == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE contact = '{EMAIL}'",
        "SELECT * FROM t WHERE phone IN ('555-0100', '{PHONE}')",
        "SELECT * FROM t WHERE owner LIKE '%{NAME}%'",
        "SELECT * FROM t WHERE ssn = '{US_SOCIAL_SECURITY_NUMBER}'",
        "SELECT * FROM t WHERE note = 'it''s {NAME} again'",
        "-- the title's total\nSELECT SUM(x) FROM t WHERE title_name = '{NAME}'",
        "SELECT * FROM t WHERE n = U&'{NAME}'",
        "SELECT * FROM t WHERE n = N'{NAME}'",
        "SELECT a /* /* */ FROM t WHERE n = '{NAME}' -- */",
        "SELECT * FROM t WHERE n = '{NAME}' AND note = 'unterminated",
        "SELECT \"owner's name\" FROM t WHERE owner = '{NAME}'",
    ],
)
def test_masked_literal_in_sql_detects_placeholders_in_string_literals(sql):
    assert masked_literal_in_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE regexp_like(code, '^[A-Z]{2}-[0-9]{3}$')",
        "SELECT * FROM t WHERE status = '{ACTIVE}'",
        "SELECT * FROM t WHERE name = '{name}'",
        "SELECT json_extract(doc, '$.items[0]') AS {NAME} FROM t",
        "SELECT * FROM t WHERE title_name = 'Saltmarsh Requiem'",
        "SELECT 1 -- '{NAME}' was masked\nFROM t",
    ],
)
def test_masked_literal_in_sql_ignores_other_braces(sql):
    assert not masked_literal_in_sql(sql)


@pytest.mark.skipif(not _GUARDRAIL_STACK.exists(), reason="infra sources not in this checkout")
def test_every_anonymized_guardrail_pii_entity_has_a_placeholder():
    stack = _GUARDRAIL_STACK.read_text()
    block = stack.split("const PII_ENTITIES_CONFIG", 1)[1].split("];", 1)[0]
    anonymized = set(re.findall(r"""type:\s*["']([A-Z_]+)["']""", block))
    assert anonymized
    assert {f"{{{entity}}}" for entity in anonymized} <= set(GUARDRAIL_PII_PLACEHOLDERS)
