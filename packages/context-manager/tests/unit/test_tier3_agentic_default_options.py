# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The recommended Deep Reasoning options apply when a request leaves them unset.

Covers ``coa_serve.tier3.agentic.defaults`` and its use on the orchestrator's
deep-reasoning path: unset options take the recommended values, a value the
request sets (``false`` included) is kept, ``tokenCaps`` is filled per role, and
``DEEP_REASONING_DEFAULT_OPTIONS=off`` restores the previous all-off behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.models import InvokeRequest
from coa_serve.tier3.agentic.defaults import RECOMMENDED_OPTIONS, with_recommended_options

from .test_orchestrator import _deep_reasoning_orchestrator

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _defaults_on(monkeypatch):
    monkeypatch.delenv("DEEP_REASONING_DEFAULT_OPTIONS", raising=False)


class TestWithRecommendedOptions:
    def test_unset_options_take_the_recommended_values(self):
        merged = with_recommended_options({"mode": "deep-reasoning"})
        assert merged == {"mode": "deep-reasoning", **RECOMMENDED_OPTIONS}

    def test_a_value_the_request_sets_is_kept(self):
        merged = with_recommended_options({"readDocument": False, "prefetchMode": "off", "readDocumentMaxChars": 8000})
        assert merged["readDocument"] is False
        assert merged["prefetchMode"] == "off"
        assert merged["readDocumentMaxChars"] == 8000
        assert merged["entityRouting"] is True  # still defaulted

    def test_token_caps_are_filled_per_role(self):
        assert with_recommended_options({"tokenCaps": {"planner": 2048}})["tokenCaps"] == {
            "planner": 2048,
            "synthesizer": 12000,
            "nl2sql": 8000,
        }

    def test_input_is_not_mutated(self):
        options = {"tokenCaps": {"planner": 2048}}
        with_recommended_options(options)
        assert options == {"tokenCaps": {"planner": 2048}}

    @pytest.mark.parametrize("value", ["off", "OFF", "false", "0", "no"])
    def test_deployment_switch_off_restores_all_off(self, monkeypatch, value):
        monkeypatch.setenv("DEEP_REASONING_DEFAULT_OPTIONS", value)
        assert with_recommended_options({"mode": "deep-reasoning"}) == {"mode": "deep-reasoning"}

    def test_prefetch_mode_off_is_accepted(self):
        assert (
            InvokeRequest(query="q", namespace="demo", options={"prefetchMode": "off"}).options["prefetchMode"] == "off"
        )
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace="demo", options={"prefetchMode": "sometimes"})


class TestOrchestratorAppliesDefaults:
    @pytest.mark.asyncio
    async def test_deep_reasoning_request_reaches_the_retriever_with_the_defaults(self):
        orch = _deep_reasoning_orchestrator()
        await orch.resolve(
            InvokeRequest(query="q", namespace="demo", options={"mode": "deep-reasoning", "readDocument": False})
        )
        options = orch._agentic_retriever.resolve.call_args.kwargs["options"]
        assert options["readDocument"] is False
        assert options["prefetchMode"] == "assess"
        assert options["tokenCaps"] == {"planner": 4000, "synthesizer": 12000, "nl2sql": 8000}
        assert options["sqlWithEvidence"] is True and options["sqlWriterEffort"] == "medium"

    @pytest.mark.asyncio
    async def test_a_direct_tier2_request_gets_no_sql_writer_defaults(self):
        # The defaults are applied only on the deep-reasoning path; a standard request never reaches it.
        orch = _deep_reasoning_orchestrator()
        tier2 = orch._structured_query_tier.resolve
        orch._structured_query_tier.resolve = AsyncMock(wraps=tier2)
        await orch.resolve(InvokeRequest(query="q", namespace="demo", options={"mode": "standard"}))
        orch._agentic_retriever.resolve.assert_not_called()
        # The options that reach the Tier-2 SQL writer are only the ones the request sent.
        context = orch._structured_query_tier.resolve.call_args.args[2]
        assert not set(RECOMMENDED_OPTIONS) & set(context.options or {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("options", "may_view", "expected"),
        [
            ({}, True, False),  # not requested
            ({"includeDebugInfo": True}, False, False),  # requested, no viewSource
            ({"includeDebugInfo": True}, True, True),
        ],
    )
    async def test_schema_visibility_follows_the_query_used_gate(self, options, may_view, expected):
        orch = _deep_reasoning_orchestrator()
        orch._firewall.can_view_source = MagicMock(return_value=may_view)
        # viewOntology alone must not unlock table and column names.
        orch._firewall.can_view_ontology = MagicMock(return_value=True)
        await orch.resolve(InvokeRequest(query="q", namespace="demo", options={"mode": "deep-reasoning", **options}))
        assert orch._agentic_retriever.resolve.call_args.kwargs["schema_visible"] is expected
