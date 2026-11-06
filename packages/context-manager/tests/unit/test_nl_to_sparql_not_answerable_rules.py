# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL→SPARQL (Ontop) prompt carries the blank, out-of-range and missing-measure rules (new on the SPARQL path)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.nl_to_sparql import _SYSTEM_PROMPT
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator

pytestmark = pytest.mark.unit

NOT_ANSWERABLE_SPARQL = (
    'SELECT ?answer WHERE { BIND("NOT_ANSWERABLE_RANGE: March 2031 is outside the data; '
    'Plan covers 2026-01" AS ?answer) }',
    'SELECT ?answer WHERE { BIND("NOT_ANSWERABLE_FIELD: no cost field for service job" AS ?answer) }',
)


@pytest.mark.parametrize(
    "phrase",
    [
        "A blank (unbound) value means not recorded",
        "Never turn it into 0, 'N', 'No', 'None' or",
        "A SUM over no matching rows returns 0",
        'BIND("NOT_ANSWERABLE_RANGE: <what was asked> is outside the data; <class> covers <range>" AS ?answer)',
        'BIND("NOT_ANSWERABLE_FIELD: no <measure> field for <entity>" AS ?answer)',
    ],
)
def test_rule_is_in_the_sparql_system_prompt(phrase):
    assert phrase in " ".join(_SYSTEM_PROMPT.split())


def test_rules_stay_table_and_question_neutral():
    start = _SYSTEM_PROMPT.index("BLANKS, OUT-OF-RANGE PERIODS")
    end = _SYSTEM_PROMPT.index("REMEMBER: When comparing")
    assert not re.search(r"\b(19|20)\d\d\b|\b\d{5,}\b|\b[a-z]+_[a-z_]+\b", _SYSTEM_PROMPT[start:end])


def test_rules_sit_before_the_closing_reminder():
    assert _SYSTEM_PROMPT.index("BLANKS, OUT-OF-RANGE PERIODS") < _SYSTEM_PROMPT.index("REMEMBER: When comparing")


@pytest.mark.parametrize("sparql", NOT_ANSWERABLE_SPARQL)
def test_not_answerable_select_passes_the_validator_stages_that_run_without_a_graph(sparql):
    validator = SPARQLValidator(
        graph_client=AsyncMock(), graph_uri_template="https://ontology-workbench.local/{namespace}"
    )
    assert validator._validate_syntax(sparql).valid
    assert validator._validate_projection_aliases(sparql).valid
