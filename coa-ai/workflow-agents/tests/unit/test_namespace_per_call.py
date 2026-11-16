# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""run_workflow's per-call ``namespace_id``: the run's namespace, checked, and used everywhere."""

from __future__ import annotations

import inspect
import time
from typing import Any
from unittest.mock import MagicMock, patch

import coa_workflow_agents.server as server
import pytest
from coa_workflow_agents.agents.supervisor import SupervisorAgents
from coa_workflow_agents.auth.caller import Caller

pytestmark = pytest.mark.unit

OTHER = "22222222-2222-4222-8222-222222222222"


def _caller() -> Caller:
    return Caller(email="a@example.com", groups=(), id_token="t", expires_at=time.time() + 3000)


@pytest.mark.parametrize("given", [OTHER, f"  {OTHER}  ", f"{OTHER}\n"])
def test_a_given_namespace_id_is_used(given):

    assert server._validate_namespace_id(given) == OTHER


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "22222222-2222-4222-8222-22222222222",  # too short
        "22222222-2222-4222-8222-2222222222222",  # too long
        "abcdef01-2345-4678-89ab-cdef01234567".upper(),  # COA ids are lowercase
        "22222222-2222-1222-8222-222222222222",  # not v4
        "22222222-2222-4222-c222-222222222222",  # bad variant nibble
        "22222222-2222-4222-8222-22222222222A",  # one uppercase character
        f"{OTHER}/../x",
        "../" + OTHER,
        f"{OTHER[:10]}\n{OTHER[10:]}",
    ],
)
def test_a_malformed_namespace_id_is_refused(bad):

    with pytest.raises(ValueError, match="namespace_id"):
        server._validate_namespace_id(bad)


@pytest.mark.asyncio
async def test_run_workflow_uses_the_given_namespace_for_every_agent():

    caller = _caller()
    per_call = MagicMock()
    built: list[Any] = []

    def fake_build(client, namespace_id, config, confirmed_tools, **kwargs):
        built.append((namespace_id, kwargs))
        supervisor = MagicMock(side_effect=lambda instr: "done")
        supervisor.event_loop_metrics.accumulated_usage = {}
        return SupervisorAgents(supervisor=supervisor, sub_agents=())

    with (
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        patch.object(server, "_caller_client", return_value=per_call) as make_client,
        patch("coa_workflow_agents.server.build_supervisor_agent", side_effect=fake_build),
    ):
        result = await server.run_workflow(MagicMock(), "list sources", namespace_id=OTHER)

    make_client.assert_called_once_with(caller)
    assert built == [(OTHER, {})]
    assert result == f"Namespace: {OTHER}\n\ndone"


@pytest.mark.asyncio
async def test_run_workflow_refuses_a_malformed_namespace_before_any_work():

    with (
        patch.object(server, "_verify_inbound_caller", return_value=_caller()),
        patch.object(server, "_caller_client") as make_client,
        pytest.raises(ValueError, match="namespace_id"),
    ):
        await server.run_workflow(MagicMock(), "list sources", namespace_id="not-a-uuid")
    make_client.assert_not_called()


@pytest.mark.asyncio
async def test_run_workflow_verifies_the_caller_before_checking_the_namespace():

    with (
        patch.object(server, "_verify_inbound_caller", side_effect=ValueError("Missing Authorization header")),
        pytest.raises(ValueError, match="Authorization"),
    ):
        await server.run_workflow(MagicMock(), "x", namespace_id="not-a-uuid")


@pytest.mark.asyncio
async def test_run_workflow_checks_confirm_mutations_before_the_namespace():

    with (
        patch.object(server, "_verify_inbound_caller", return_value=_caller()),
        pytest.raises(ValueError, match="confirm_mutations"),
    ):
        await server.run_workflow(MagicMock(), "x", confirm_mutations=["no_such_tool"], namespace_id="bad")


@pytest.mark.asyncio
async def test_run_workflow_logs_the_namespace_it_works_in():

    with (
        patch.object(server, "_verify_inbound_caller", return_value=_caller()),
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "logger") as log,
    ):
        await server.run_workflow(MagicMock(), "x", namespace_id=OTHER)

    log.info.assert_any_call("run_workflow_started", namespace_id=OTHER)


@pytest.mark.asyncio
async def test_a_failed_run_names_the_namespace():

    with (
        patch.object(server, "_verify_inbound_caller", return_value=_caller()),
        patch.object(server, "_run_supervisor_sync", side_effect=RuntimeError("boom")),
        pytest.raises(ValueError, match=rf"^Namespace: {OTHER}\. Workflow failed \(RuntimeError\)"),
    ):
        await server.run_workflow(MagicMock(), "x", namespace_id=OTHER)


def test_namespace_id_is_a_required_run_workflow_argument():

    param = inspect.signature(server.run_workflow).parameters["namespace_id"]
    assert param.default is inspect.Parameter.empty
