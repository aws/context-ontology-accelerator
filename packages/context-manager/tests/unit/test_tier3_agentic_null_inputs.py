# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for null optional tool inputs in the deep-reasoning controller.

The planner sometimes sends ``null`` for an optional tool field (for example
``"revise": null``). That must count as leaving the field out: it passes the
input-contract check, and the field is not passed to the tool.
"""

from __future__ import annotations

import pytest
from coa_serve.tier3.agentic.controller import validate_inputs
from coa_serve.tier3.agentic.models import PlannerDecision, StopReason

from .test_tier3_agentic_controller import FakeTool, ScriptedPlanner, make_registry, run_loop

pytestmark = pytest.mark.unit


class TestValidateInputsNull:
    def test_null_declared_field_is_not_a_violation(self):
        assert validate_inputs({"query": "q", "revise": None}, {"query": "str", "revise": "str"}) == []

    def test_null_unknown_field_is_still_rejected(self):
        assert validate_inputs({"limit": None}, {"query": "str"}) == ["unknown field 'limit'"]

    def test_wrong_type_still_rejected(self):
        assert validate_inputs({"revise": 3}, {"revise": "str"}) == ["field 'revise' expected 'str', got int"]


class TestNullInputDroppedBeforeInvoke:
    async def test_tool_runs_without_the_null_field(self):
        tool = FakeTool("nl_to_sql", input_schema={"query": "str", "revise": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="nl_to_sql", inputs={"query": "q", "revise": None})]],
            sufficiency=[True],
        )

        stop, _ctx, _queue, trace = await run_loop(planner, make_registry(tool))

        assert len(tool.calls) == 1
        assert tool.calls[0]["query"] == "q"
        assert "revise" not in tool.calls[0]
        assert not [s for s in trace.steps if "input_contract_violation" in str(s.detail)]
        assert stop == StopReason.sufficiency_satisfied
