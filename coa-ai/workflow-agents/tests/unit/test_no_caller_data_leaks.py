# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End to end, no caller data reaches a log line.

Logging is configured as ``__main__`` configures it, then pointed at an in-memory stream
(structlog events and stdlib records from Strands, mcp and botocore alike). Each test drives
a real code path with one ``SENTINEL`` where caller data enters: the instruction, a tool
name the model invents, an exception message, and a ``CoaApiError``'s path and body. The
sentinel is identifier-shaped on purpose, so a value-shape check alone cannot hide it.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import coa_workflow_agents.agents.supervisor as supervisor_module
import coa_workflow_agents.server as server
import pytest
import structlog
from coa_workflow_agents.auth.caller import Caller
from coa_workflow_agents.clients.coa_api_client import CoaApiClient
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.log_policy import setup_service_logging
from strands.models.model import Model

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
CALLER = Caller(email="a@example.com", groups=(), id_token="t", expires_at=4102444800.0)
SENTINEL = "SENTINEL-caller-data-7f3a"
CONFIG = WorkflowAgentsConfig(bedrock_model_id="", bedrock_temperature=0.0, aws_region="us-east-1")


# --- capture -----------------------------------------------------------------


class _Capture:
    """Every rendered log line the service would write."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def text(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line.strip()]

    def assert_clean(self) -> None:
        assert SENTINEL not in self.text(), f"caller data in a log line:\n{self.text()}"


@pytest.fixture
def capture() -> Iterator[_Capture]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    # The production setup (as __main__ runs it), then only its sinks redirected
    # and the logger cache off, so later tests' capture_logs still works.
    setup_service_logging(CONFIG.log_level)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream), cache_logger_on_first_use=False)
    for handler in root.handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(stream)
    try:
        yield _Capture(stream)
    finally:
        structlog.reset_defaults()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# --- a scripted model --------------------------------------------------------


def _tool_turn(name: str, tool_input: dict[str, Any], tool_use_id: str) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _text_turn(text: str) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


class _ScriptedModel(Model):
    """A Strands model that plays scripted turns, chosen by the agent's system prompt.

    A turn is ``("tool", name, input)``, ``("raise", exception)`` or ``("text", text)``.
    Once an agent's script runs out it answers with text quoting the conversation's
    last user message, as a real model quoting the caller would.
    """

    def __init__(self, scripts: dict[str, list[tuple[Any, ...]]]) -> None:
        self._scripts = scripts

    def update_config(self, **model_config: Any) -> None:
        return None

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model: Any, prompt: Any, system_prompt: str | None = None, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(  # type: ignore[override]
        self, messages: Any, tool_specs: Any = None, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        key = next((k for k in self._scripts if system_prompt and system_prompt.startswith(k)), None)
        script = self._scripts.get(key, []) if key else []
        if script:
            turn = script.pop(0)
            if turn[0] == "raise":
                raise turn[1]
            events = (
                _tool_turn(turn[1], turn[2], f"tooluse_{len(messages)}") if turn[0] == "tool" else _text_turn(turn[1])
            )
        else:
            events = _text_turn(f"done: {json.dumps(messages)[-300:]} {SENTINEL}")
        for event in events:
            yield event


SUPERVISOR = "You are the COA Workflow supervisor"


async def _run(scripts: dict[str, list[tuple[Any, ...]]], instruction: str = f"please {SENTINEL}") -> str:
    model = _ScriptedModel(scripts)
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    with (
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(supervisor_module, "build_bedrock_model", return_value=model),
    ):
        return await server.run_workflow(ctx, instruction, NS)


# --- (a) the instruction -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_instruction_sentinel_reaches_no_log(capture: _Capture) -> None:
    reply = await _run({SUPERVISOR: []})
    assert SENTINEL in reply  # the model did see and quote it
    assert any(line.get("event") == "run_workflow_completed" for line in capture.lines())
    capture.assert_clean()


# --- (b) a tool name the model invents ---------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_unknown_tool_name_sentinel_reaches_no_log(capture: _Capture) -> None:
    await _run({SUPERVISOR: [("tool", SENTINEL, {"x": 1})]})
    completed = [line for line in capture.lines() if line.get("event") == "tool_call_completed"]
    assert completed and all(line["tool"] == "unknown" for line in completed)
    capture.assert_clean()


# --- (c) a plain exception reaching run_workflow_failed ----------------------


@pytest.mark.asyncio
async def test_run_workflow_failed_exception_message_sentinel_reaches_no_log(capture: _Capture) -> None:
    with pytest.raises(ValueError, match=r"Workflow failed \(RuntimeError\)"):
        await _run({SUPERVISOR: [("raise", RuntimeError(f"model failed on {SENTINEL}"))]})
    (failed,) = [line for line in capture.lines() if line.get("event") == "run_workflow_failed"]
    assert failed["error"] == "RuntimeError"
    capture.assert_clean()


# --- (d) a CoaApiError's path and body ---------------------------------------


class _TokenProvider:
    def get_id_token(self) -> str:
        return "fake-token"


@pytest.mark.asyncio
async def test_run_workflow_coa_api_error_path_and_body_sentinel_reaches_no_log(capture: _Capture) -> None:
    def worker(caller: Caller, namespace_id: str, instruction: str, confirmed_tools: frozenset[str]) -> str:
        with CoaApiClient(base_url="https://coa.example.com", token_provider=_TokenProvider()) as client:
            resp = MagicMock(status_code=404, content=b"x", text=f"no such {SENTINEL}", headers={})
            with patch.object(client._session, "request", return_value=resp):
                client.get(f"/namespaces/{namespace_id}/ontologies/{SENTINEL}")
        return "unreachable"

    with (
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server, "_run_supervisor_sync", side_effect=worker),
        pytest.raises(ValueError, match=r"Workflow failed \(CoaApiError\)"),
    ):
        await server.run_workflow(MagicMock(report_progress=AsyncMock()), "x", NS)
    events = [line["event"] for line in capture.lines()]
    assert "coa_api_error" in events and "run_workflow_failed" in events
    capture.assert_clean()
