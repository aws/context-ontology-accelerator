# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever subpackage skeleton and shared models.

Covers task 1 of the agentic-retriever spec:

- The shared, dependency-free data models in
  ``coa_serve.tier3.agentic.models`` (``SubQuestion``,
  ``ExplorationBranch``, ``PlannerDecision``, and the ``StopReason`` StrEnum).
- The lazy-import invariant (Requirement 12.3): importing
  ``coa_serve.tier3.agentic`` must NOT pull ``graphrag_toolkit``
  into ``sys.modules``. This mirrors the existing invariant test for the lexical
  strategies module — it runs in a fresh subprocess so that a ``graphrag_toolkit``
  already imported by a sibling test in the same process cannot mask a
  regression.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest
from coa_serve.tier3.agentic import (
    ExplorationBranch,
    PlannerDecision,
    StopReason,
    SubQuestion,
)


@pytest.mark.unit
class TestStopReason:
    def test_has_exactly_the_seven_enumerated_members(self):
        """StopReason carries the Req 6.6 / Req 7 stop-reason set plus answer_stable."""
        assert {r.value for r in StopReason} == {
            "sufficiency_satisfied",
            "no_new_information",
            "max_steps_reached",
            "sufficiency_check_timeout",
            "time_budget_expired",
            "queue_drained",
            "answer_stable",
        }

    def test_is_a_str_enum(self):
        """Members compare equal to their string value (StrEnum), so they trace
        and serialize as plain strings."""
        assert StopReason.time_budget_expired == "time_budget_expired"
        assert f"{StopReason.queue_drained}" == "queue_drained"


@pytest.mark.unit
class TestSubQuestion:
    def test_defaults(self):
        sq = SubQuestion(text="Amazon competitors' financial situations")
        assert sq.text == "Amazon competitors' financial situations"
        assert sq.multi_entity is False
        assert sq.origin == "decomposition"

    def test_is_frozen(self):
        sq = SubQuestion(text="q")
        with pytest.raises(FrozenInstanceError):
            sq.text = "other"  # type: ignore[misc]


@pytest.mark.unit
class TestExplorationBranch:
    def test_to_sub_question_marks_branch_origin(self):
        branch = ExplorationBranch(
            seed="Walmart financial situation",
            parent_question="Amazon competitors' financial situations",
            reason="competitor surfaced by COMPETES_WITH traversal",
        )
        sq = branch.to_sub_question()
        assert isinstance(sq, SubQuestion)
        assert sq.text == "Walmart financial situation"
        assert sq.origin == "branch"

    def test_is_frozen(self):
        branch = ExplorationBranch(seed="s")
        with pytest.raises(FrozenInstanceError):
            branch.seed = "other"  # type: ignore[misc]


@pytest.mark.unit
class TestPlannerDecision:
    def test_defaults(self):
        decision = PlannerDecision(tool_name="strategy:entity_based")
        assert decision.tool_name == "strategy:entity_based"
        assert decision.inputs == {}
        assert decision.reasoning == ""
        assert decision.is_ad_hoc is False

    def test_carries_tool_inputs_and_reasoning(self):
        decision = PlannerDecision(
            tool_name="graph_traversal",
            inputs={"mode": "edge_typed", "edge_types": ["COMPETES_WITH"]},
            reasoning="anchor entity found; traverse competitor edge",
        )
        assert decision.inputs["mode"] == "edge_typed"
        assert decision.reasoning

    def test_independent_inputs_default(self):
        """The default ``inputs`` is a fresh dict per instance (no shared state)."""
        a = PlannerDecision(tool_name="t")
        b = PlannerDecision(tool_name="t")
        assert a.inputs is not b.inputs


# ── Lazy-import invariant (Requirement 12.3) ───────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_agentic_subpackage_does_not_import_graphrag(self):
        """``import coa_serve.tier3.agentic`` must NOT import
        ``graphrag_toolkit`` (Req 12.3).

        Runs in a fresh subprocess — with the current ``sys.path`` propagated so
        imports resolve exactly as they do under pytest — so a ``graphrag_toolkit``
        already imported by a sibling test in this process cannot mask a
        regression. Asserts no ``graphrag_toolkit`` module is present in
        ``sys.modules`` after the import.
        """
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**__import__("os").environ, "PYTHONPATH": __import__("os").pathsep.join(sys.path)}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            "importing the agentic subpackage leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
