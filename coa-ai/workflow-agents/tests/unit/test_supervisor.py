# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The real supervisor enforces the caller's ``confirm_mutations`` through its gate."""

from __future__ import annotations

from unittest.mock import MagicMock

import coa_workflow_agents.agents.supervisor as supervisor_module
import pytest
from coa_workflow_agents.config import WorkflowAgentsConfig

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
CONFIG = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")


def _status_call(monkeypatch: pytest.MonkeyPatch, confirmed: frozenset[str]) -> tuple[MagicMock, dict]:
    """Treat the status tool as mutating, build the real supervisor, and call that tool through it."""
    monkeypatch.setattr(supervisor_module, "ALL_MUTATING_TOOL_NAMES", frozenset({"get_namespace_status"}))
    client = MagicMock()
    client.list_sources.return_value = {"items": []}
    client.list_induction_jobs.return_value = {"items": []}
    client.list_proposals.return_value = {"items": []}
    agents = supervisor_module.build_supervisor_agent(client, NS, CONFIG, confirmed)
    result = agents.supervisor.tool.get_namespace_status(record_direct_tool_call=False)
    return client, result


def test_the_supervisor_denies_a_mutating_tool_the_caller_did_not_confirm(monkeypatch):
    client, result = _status_call(monkeypatch, frozenset())
    assert result["status"] == "error"
    assert "confirm_mutations" in str(result["content"])
    client.list_sources.assert_not_called()


@pytest.mark.parametrize("confirmed", [frozenset({"get_namespace_status"}), frozenset({"*"})], ids=["named", "all"])
def test_the_supervisor_runs_a_mutating_tool_the_caller_confirmed(monkeypatch, confirmed):
    client, result = _status_call(monkeypatch, confirmed)
    assert result["status"] == "success", result
    client.list_sources.assert_called()
