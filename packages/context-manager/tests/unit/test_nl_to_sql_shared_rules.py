# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL→SQL writer rules: no COALESCE-to-zero outside the stated range, no blank turned into a value,
identifier / two-column-key / lookup / exact-value / snapshot rules, and the NOT_ANSWERABLE row."""

from __future__ import annotations

import re

import pytest
from coa_serve.tier2.nl_to_sql.sql_generator import _SHARED_RULES, _build_system_prompt
from coa_serve.tier2.sql_firewall import SQLFirewall

pytestmark = pytest.mark.unit

NOT_ANSWERABLE_SQL = (
    "SELECT 'NOT_ANSWERABLE_FIELD: no cost field for service job' AS answer",
    "SELECT 'NOT_ANSWERABLE_RANGE: March 2031 is outside the data; plan covers 2026-01 to 2029-12' AS answer",
)


def test_unconditional_coalesce_rule_is_gone():
    assert "Handle potential NULLs with COALESCE" not in _SHARED_RULES


@pytest.mark.parametrize(
    "phrase",
    [
        # blanks and zeros
        "only when the period or entity asked about lies inside the data range",
        "a blank means not recorded",
        # identifiers, keys, lookups, listed values, snapshots
        "Identifiers of different kinds are not interchangeable",
        "A row identified by two columns",
        "held in another table, join that table by the code",
        "lists exact values, use them in full and in the same case",
        "pick by the date the question names",
        # out-of-range periods and missing measures
        "SELECT 'NOT_ANSWERABLE_RANGE: <what was asked> is outside the data; <table> covers <range>' AS answer",
        "SELECT 'NOT_ANSWERABLE_FIELD: no <measure> field for <entity>' AS answer",
    ],
)
def test_rule_is_in_the_system_prompt_for_every_dialect(phrase):
    flat = " ".join(_SHARED_RULES.split())
    assert phrase in flat
    for dialect in ("athena", "postgres", "redshift"):
        prompt = " ".join(_build_system_prompt(dialect).split())
        assert phrase in prompt


def test_rules_stay_table_and_question_neutral():
    # The rules must hold for any column of that kind, so none names a table, date or value.
    start = _SHARED_RULES.index("- Use COALESCE(SUM(...), 0) only when")
    end = _SHARED_RULES.index("no <measure> field for <entity>")
    block = _SHARED_RULES[start:end]
    assert not re.search(r"\b(19|20)\d\d\b|\b\d{5,}\b|\b[a-z]+_[a-z_]+\b", block)


@pytest.mark.parametrize("sql", NOT_ANSWERABLE_SQL)
def test_not_answerable_row_passes_the_firewall(sql):
    firewall = SQLFirewall()
    firewall.validate(sql)
    assert SQLFirewall.referenced_tables(sql) == []
    assert firewall.evaluate(sql, {"tableAllowlist": ["orders"]}).denied is False


@pytest.mark.parametrize("dialect", ["athena", "postgres", "redshift"])
def test_worked_example_does_not_coalesce_to_zero(dialect):
    # The example's table states no data range, so by the rule it must not turn an empty
    # period into 0; models copy the example closely.
    prompt = _build_system_prompt(dialect)
    start = prompt.index("```sql\nSELECT")
    example = prompt[start : prompt.index("```", start + 6)]
    assert "SUM(o.total)" in example
    assert "COALESCE" not in example.upper()
