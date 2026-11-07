# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The installed strands-agents still offers what SqlAgent uses.

The SqlAgent tests replace ``strands`` with ``strands_fake``, so they cover our
orchestration and not the library. A strands release that renames a constructor
argument, changes the ``@tool`` schema, stops honouring an outer
``asyncio.timeout`` or renames the usage keys would pass them and fail only in
the serve image. This test runs the real library against a scripted model:
one turn that calls a tool, then a turn that hangs until the deadline. No model
endpoint is contacted.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from .strands_fake import uninstall_fake_strands

pytestmark = pytest.mark.unit

# Backstop only: the test expires the deadline itself once turn 2 starts.
_CEILING_S = 30.0


@pytest.fixture
def real_strands(monkeypatch: pytest.MonkeyPatch) -> Any:
    uninstall_fake_strands()
    for name in ("strands", "strands.models"):
        if sys.modules.get(name, ...) is None:
            sys.modules.pop(name)
    # BedrockModel builds a boto3 client at construction; give it static dummy
    # credentials instead of a profile so nothing is resolved from the host.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # strands-agents is a runtime dependency: an import failure must fail here,
    # not skip, since SqlAgent turns the same ImportError into a silent fallback.
    return importlib.import_module("strands")


def _scripted_model() -> Any:
    from strands.models.model import Model

    class ScriptedModel(Model):
        """Turn 1 calls ``probe``; turn 2 records the conversation and never finishes."""

        def __init__(self) -> None:
            self.turns = 0
            self.turn2_messages: list[Any] = []
            self.on_turn2: Any = lambda: None

        def update_config(self, **model_config: Any) -> None:
            pass

        def get_config(self) -> dict[str, Any]:
            return {}

        async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            raise NotImplementedError
            yield

        async def stream(self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any):
            self.turns += 1
            yield {"messageStart": {"role": "assistant"}}
            if self.turns == 1:
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "probe"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"query": "orders"}'}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                yield {
                    "metadata": {
                        "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
                        "metrics": {"latencyMs": 1},
                    }
                }
            else:
                self.turn2_messages = list(messages)
                self.on_turn2()
                await asyncio.sleep(_CEILING_S * 2)

    return ScriptedModel()


def _tool_results(messages: list[Any]) -> list[dict[str, Any]]:
    return [block["toolResult"] for m in messages for block in m.get("content", []) if "toolResult" in block]


@pytest.mark.asyncio
async def test_agent_surface_matches_sql_agent_usage(real_strands: Any) -> None:
    from strands import Agent, tool
    from strands.models import BedrockModel

    calls: list[str] = []

    @tool
    async def probe(query: str, limit: int = 5) -> str:
        """Search tables.

        Args:
            query: What to look for.
            limit: How many results.
        """
        calls.append(query)
        return "[]"

    properties = probe.tool_spec["inputSchema"]["json"]["properties"]
    assert properties["query"]["description"] == "What to look for."
    assert probe.tool_spec["inputSchema"]["json"]["required"] == ["query"]

    bedrock = BedrockModel(region_name="us-west-2", max_tokens=4096, model_id="example-model-id")
    assert bedrock.get_config()["model_id"] == "example-model-id"
    assert bedrock.get_config()["max_tokens"] == 4096
    # SqlAgent reads the model id back from the agent's model config.
    assert Agent(model=bedrock, tools=[]).model.config["model_id"] == "example-model-id"

    model = _scripted_model()
    agent = Agent(model=model, tools=[probe], system_prompt="system")
    agent.callback_handler = lambda **kw: None
    assert agent.tool_names == ["probe"]

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(_CEILING_S) as budget:
            model.on_turn2 = lambda: budget.reschedule(asyncio.get_running_loop().time())
            await agent.invoke_async("Question: how many orders?")
    assert budget.expired()
    assert calls == ["orders"]
    # The tool's return value reaches the model's next turn as a successful result.
    results = _tool_results(model.turn2_messages)
    assert [r["status"] for r in results] == ["success"]
    assert results[0]["content"] == [{"text": "[]"}]

    usage = agent.event_loop_metrics.accumulated_usage
    assert {"inputTokens", "outputTokens", "totalTokens"} <= set(usage)
    assert usage["totalTokens"] == 7
