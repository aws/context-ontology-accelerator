# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OSI import applies the CreateMetric contract bounds per metric.

A metric stored outside ``metric-service.smithy``'s bounds cannot be fetched,
updated or deleted through the API, so import must reject it — as that metric's
error, never by failing the whole import.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from coa_metrics.api import contract
from coa_metrics.api.import_job_store import MetricDisposition
from coa_metrics.api.import_osi import _osi_metric_to_definition
from coa_metrics.api.import_worker import _build_offset_plan
from coa_metrics.osi_parser import OsiAiContext, OsiCustomExtension, OsiDialectExpression, OsiDocument, OsiMetric
from coa_metrics.source_status import PERMISSIVE_ENV

pytestmark = pytest.mark.unit

_NS = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PERMISSIVE_ENV, "true")


def _metric(name: str = "total_revenue", **overrides: Any) -> OsiMetric:
    ext = {"data_source_id": "ds-1", "source_table": "orders", **overrides.pop("ext", {})}
    return OsiMetric(
        name=name,
        description=overrides.pop("description", "Total revenue"),
        expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT SUM(amount) FROM orders")],
        ai_context=overrides.pop("ai_context", None),
        custom_extensions=OsiCustomExtension(**ext),
    )


def _too_long(limit: int) -> str:
    return "x" * (limit + 1)


_OUT_OF_CONTRACT = {
    "name": (_metric(name="m" * (contract.METRIC_NAME_MAX_LENGTH + 1)), "name"),
    "description": (_metric(description=_too_long(contract.METRIC_DESCRIPTION_MAX_LENGTH)), "description"),
    "unit": (_metric(ext={"unit": _too_long(contract.METRIC_UNIT_MAX_LENGTH)}), "unit"),
    "returnType": (_metric(ext={"return_type": _too_long(contract.METRIC_RETURN_TYPE_MAX_LENGTH)}), "returnType"),
    "sourceTable": (_metric(ext={"source_table": _too_long(contract.SOURCE_TABLE_MAX_LENGTH)}), "sourceTable"),
    "ontologyConcepts items": (
        _metric(ext={"ontology_concepts": ["c"] * (contract.ONTOLOGY_CONCEPTS_MAX_ITEMS + 1)}),
        "ontologyConcepts",
    ),
    "ontologyConcepts member": (
        _metric(ext={"ontology_concepts": [_too_long(contract.ONTOLOGY_CONCEPT_MAX_LENGTH)]}),
        "ontologyConcepts entries",
    ),
    "synonyms items": (
        _metric(ai_context=OsiAiContext(synonyms=["s"] * (contract.AI_CONTEXT_SYNONYMS_MAX_ITEMS + 1))),
        "aiContext.synonyms",
    ),
    "synonyms member": (
        _metric(ai_context=OsiAiContext(synonyms=[_too_long(contract.AI_CONTEXT_SYNONYM_MAX_LENGTH)])),
        "aiContext.synonyms entries",
    ),
    "instructions": (
        _metric(ai_context=OsiAiContext(instructions=_too_long(contract.AI_CONTEXT_INSTRUCTIONS_MAX_LENGTH))),
        "aiContext.instructions",
    ),
    "examples items": (
        _metric(ai_context=OsiAiContext(examples=["e"] * (contract.AI_CONTEXT_EXAMPLES_MAX_ITEMS + 1))),
        "aiContext.examples",
    ),
    "examples member": (
        _metric(ai_context=OsiAiContext(examples=[_too_long(contract.AI_CONTEXT_EXAMPLE_MAX_LENGTH)])),
        "aiContext.examples entries",
    ),
}


@pytest.mark.parametrize("case", sorted(_OUT_OF_CONTRACT))
def test_out_of_contract_metric_is_rejected_by_the_converter(case: str) -> None:
    metric, field = _OUT_OF_CONTRACT[case]
    with pytest.raises(ValueError, match=rf"^{field} must"):
        _osi_metric_to_definition(metric, OsiDocument(metrics=[metric]), "test")


def test_metric_at_every_bound_is_accepted() -> None:
    metric = _metric(
        name="m" * contract.METRIC_NAME_MAX_LENGTH,
        description="d" * contract.METRIC_DESCRIPTION_MAX_LENGTH,
        ext={"unit": "u" * contract.METRIC_UNIT_MAX_LENGTH, "ontology_concepts": ["c"]},
        ai_context=OsiAiContext(
            synonyms=["s" * contract.AI_CONTEXT_SYNONYM_MAX_LENGTH],
            instructions="i" * contract.AI_CONTEXT_INSTRUCTIONS_MAX_LENGTH,
            examples=["e" * contract.AI_CONTEXT_EXAMPLE_MAX_LENGTH],
        ),
    )
    definition = _osi_metric_to_definition(metric, OsiDocument(metrics=[metric]), "test")
    assert definition.name == metric.name


def test_worker_records_out_of_contract_metric_as_its_error_and_plans_the_rest() -> None:
    bad, _ = _OUT_OF_CONTRACT["description"]
    good = _metric(name="order_count")
    neptune = MagicMock()
    neptune.get_metric.return_value = None
    neptune.resolve_class_uris.side_effect = lambda _ns, concepts: concepts

    plan = _build_offset_plan(_NS, 0, [bad, good], OsiDocument(metrics=[bad, good]), neptune)

    assert [e.disposition for e in plan.entries] == [MetricDisposition.ERROR, MetricDisposition.CREATE]
    assert plan.entries[0].error == (
        f"total_revenue: description must be at most {contract.METRIC_DESCRIPTION_MAX_LENGTH} characters"
    )
    assert plan.entries[1].name == "order_count"
