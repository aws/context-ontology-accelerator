# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The mutating-tool gate, against the real Strands intervention types.

Using Strands' own ``BeforeToolCallEvent``/``Deny``/``Proceed`` means a change to that API
breaks these tests instead of silently disabling the gate.
"""

from __future__ import annotations

import dataclasses

import pytest
from coa_workflow_agents.intervention import (
    CONFIRM_ALL,
    MutatingToolGate,
    normalize_confirm_mutations,
)
from strands.interventions.actions import Confirm, Deny, Proceed
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

# A stand-in for the tool families' mutating names (the agent changes add the real ones).
_MUTATING = frozenset({"approve_source", "accept_proposal", "start_induction"})


class _FakeToolCallEvent:
    """Stands in for ``BeforeToolCallEvent`` — only ``tool_use`` is read by the gate."""

    def __init__(self, tool_name: str) -> None:
        self.tool_use = {"name": tool_name, "toolUseId": "fake-id", "input": {}}


# ── normalize_confirm_mutations ──────────────────────────────────────────


def test_none_authorizes_nothing():
    assert normalize_confirm_mutations(None, _MUTATING) == frozenset()


def test_empty_list_authorizes_nothing():
    assert normalize_confirm_mutations([], _MUTATING) == frozenset()


def test_named_tools_pass_through_verbatim():
    result = normalize_confirm_mutations(["approve_source", "accept_proposal"], _MUTATING)
    assert result == frozenset({"approve_source", "accept_proposal"})


def test_whitespace_around_names_is_trimmed():
    result = normalize_confirm_mutations(["  approve_source  "], _MUTATING)
    assert result == frozenset({"approve_source"})


def test_sentinel_normalizes_to_confirm_all_only():
    # The '*' sentinel collapses to just {CONFIRM_ALL} even when mixed with names.
    assert normalize_confirm_mutations(["*"], _MUTATING) == frozenset({CONFIRM_ALL})
    assert normalize_confirm_mutations(["approve_source", "*"], _MUTATING) == frozenset({CONFIRM_ALL})


def test_unknown_tool_name_raises_rather_than_being_silently_ignored():
    """A typo/hallucinated name must fail loud — silently dropping it would let
    the caller believe they authorized a tool they did not."""
    with pytest.raises(ValueError) as exc:
        normalize_confirm_mutations(["approve_sauce"], _MUTATING)
    assert "approve_sauce" in str(exc.value)


def test_unknown_name_alongside_valid_names_still_raises():
    with pytest.raises(ValueError):
        normalize_confirm_mutations(["approve_source", "definitely_not_a_tool"], _MUTATING)


# ── MutatingToolGate.before_tool_call ────────────────────────────────────


def test_denies_mutating_tool_when_not_authorized():
    gate = MutatingToolGate(mutating_tool_names=frozenset({"approve_source"}), confirmed_tools=frozenset())
    result = gate.before_tool_call(_FakeToolCallEvent("approve_source"))
    assert isinstance(result, Deny)
    assert "approve_source" in result.reason
    assert "confirm_mutations" in result.reason


def test_proceeds_with_mutating_tool_when_that_tool_is_authorized():
    gate = MutatingToolGate(
        mutating_tool_names=frozenset({"approve_source"}), confirmed_tools=frozenset({"approve_source"})
    )
    result = gate.before_tool_call(_FakeToolCallEvent("approve_source"))
    assert isinstance(result, Proceed)


def test_authorizing_one_tool_does_not_authorize_another():
    """Authorization is per tool: naming approve_source does not authorize accept_proposal."""
    gate = MutatingToolGate(
        mutating_tool_names=frozenset({"approve_source", "accept_proposal"}),
        confirmed_tools=frozenset({"approve_source"}),
    )
    assert isinstance(gate.before_tool_call(_FakeToolCallEvent("approve_source")), Proceed)
    assert isinstance(gate.before_tool_call(_FakeToolCallEvent("accept_proposal")), Deny)


def test_sentinel_authorizes_every_mutating_tool():
    gate = MutatingToolGate(
        mutating_tool_names=frozenset({"approve_source", "accept_proposal"}),
        confirmed_tools=frozenset({CONFIRM_ALL}),
    )
    assert isinstance(gate.before_tool_call(_FakeToolCallEvent("approve_source")), Proceed)
    assert isinstance(gate.before_tool_call(_FakeToolCallEvent("accept_proposal")), Proceed)


def test_proceeds_with_non_mutating_tool_regardless_of_authorization():
    gate = MutatingToolGate(mutating_tool_names=frozenset({"approve_source"}), confirmed_tools=frozenset())
    result = gate.before_tool_call(_FakeToolCallEvent("get_source_status"))
    assert isinstance(result, Proceed)


def test_deny_by_default_is_the_only_behavior_no_pause_semantics():
    """Never Confirm, on any input: the server runs unattended, with nobody to answer a pause."""

    for tool_in_set in (True, False):
        for confirmed in (frozenset(), frozenset({"approve_source"}), frozenset({CONFIRM_ALL})):
            gate = MutatingToolGate(
                mutating_tool_names=frozenset({"approve_source"} if tool_in_set else set()),
                confirmed_tools=confirmed,
            )
            result = gate.before_tool_call(_FakeToolCallEvent("approve_source"))
            assert isinstance(result, (Deny, Proceed))
            assert not isinstance(result, Confirm)


def test_deny_logs_mutating_tool_denied_with_tool_name_only():
    """The alarm metric filter matches ``$.event = "mutating_tool_denied"``; args are never logged."""
    gate = MutatingToolGate(mutating_tool_names=frozenset({"approve_source"}), confirmed_tools=frozenset())
    event = _FakeToolCallEvent("approve_source")
    event.tool_use["input"] = {"secret": "do-not-log"}
    with capture_logs() as logs:
        gate.before_tool_call(event)  # type: ignore[arg-type]
    denied = [entry for entry in logs if entry["event"] == "mutating_tool_denied"]
    assert denied == [{"event": "mutating_tool_denied", "tool_name": "approve_source", "log_level": "warning"}]


def test_proceed_logs_nothing():
    gate = MutatingToolGate(mutating_tool_names=frozenset({"approve_source"}), confirmed_tools=frozenset({"*"}))
    with capture_logs() as logs:
        gate.before_tool_call(_FakeToolCallEvent("approve_source"))  # type: ignore[arg-type]
    assert logs == []


def test_gate_authorization_cannot_be_reassigned_after_construction():
    gate = MutatingToolGate(mutating_tool_names=frozenset({"delete_source"}), confirmed_tools=frozenset())
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.confirmed_tools = frozenset({"*"})  # type: ignore[misc]


def test_an_unknown_name_with_no_mutating_tools_says_there_are_none():
    with pytest.raises(ValueError, match="none in this release"):
        normalize_confirm_mutations(["approve_source"], frozenset())
    assert normalize_confirm_mutations(["*"], frozenset()) == frozenset({CONFIRM_ALL})
