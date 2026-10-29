# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deep-reasoning synthesis keeps the agent's preliminary findings out of the answer.

The agentic path hands the synthesizer an ``<agent_preliminary_findings>`` block and
asks it to verify that interim answer. Without a rule saying the block is internal, the
model often closed its answer with "This confirms the agent's preliminary findings", and
Japanese answers carried the English term itself. These tests pin the rendered prompts
(both the single-shot and the streaming call) so the rule cannot silently disappear.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.tier3.synthesizer import Synthesizer
from coa_serve.tier3.vector_retriever import ChunkResult

pytestmark = pytest.mark.unit

# The rule, as it must read to the model. Literal on purpose: the test pins the wording
# the model sees, not a module constant that could be emptied without failing anything.
_NEVER_SEEN = "The preliminary findings are internal working notes that the user never sees."
_DIRECT = "Write your answer as a direct answer to the user's question."
_NEVER_MENTION = (
    "Never mention the preliminary findings, the reasoning agent that produced them, or the "
    "preliminary answer inside <agent_preliminary_findings>, and never say that you checked, "
    "verified, confirmed or corrected the preliminary findings — in any language."
)
_DOCUMENT_STATES_ALLOWED = (
    "Agents, earlier turns of this conversation, and verification states that come from the "
    "documents or that the user asked about may be discussed normally."
)
# The earlier, too-broad wording: it banned reporting any verification at all.
_BROAD_BAN = "checked, verified, confirmed or corrected anything"
# The earlier, too-broad nouns: they banned a document's own agents (e.g. an insurance
# agent) and earlier turns of the conversation.
_BROAD_NOUNS = "the agent or reasoning agent, or a draft or earlier answer"
_WORKING_ANSWER = "CMP-3 needs an oil change every three months and is rated at 37 kW."


class _RecordingClient:
    """Fake LLM client that records the system instruction and prompt of every call."""

    def __init__(self, text: str = "CMP-3: every 3 months, 37 kW.\n[CONFIDENCE: 0.8]") -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    async def converse(self, prompt: str, **kwargs: Any) -> ConverseResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return ConverseResult(text=self._text, guardrail_blocked=False)

    async def converse_stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[str]:
        self.calls.append({"prompt": prompt, **kwargs})
        for part in (self._text[:10], self._text[10:]):
            yield part


def _chunk() -> ChunkResult:
    return ChunkResult(
        chunk_id="c1",
        text="Compressor CMP-3 requires oil changes every three months.",
        source_doc="s3://bucket/compressor.txt",
        relevance_score=0.9,
        label="compressor.txt",
    )


def _assert_rule(text: str) -> None:
    assert _NEVER_SEEN in text
    assert _DIRECT in text
    assert _NEVER_MENTION in text
    assert _DOCUMENT_STATES_ALLOWED in text
    assert _BROAD_BAN not in text
    assert _BROAD_NOUNS not in text


def _assert_no_rule(text: str) -> None:
    assert _NEVER_SEEN not in text
    assert "never say that you checked" not in text
    assert _BROAD_BAN not in text


async def _synthesize(stream: bool, *, working_answer: str | None, answer_language: str | None) -> dict[str, Any]:
    client = _RecordingClient()
    synth = Synthesizer(client, guardrail_id="gr-test")
    question = "What is the oil interval for CMP-3?"
    kwargs: dict[str, Any] = {"working_answer": working_answer}
    if answer_language:
        kwargs["answer_language"] = answer_language
    if stream:

        async def _on_token(_t: str) -> None:
            return None

        await synth.synthesize_stream(question, [_chunk()], [], on_token=_on_token, **kwargs)
    else:
        await synth.synthesize(question, [_chunk()], [], **kwargs)
    assert len(client.calls) == 1
    return client.calls[0]


@pytest.mark.parametrize("stream", [False, True], ids=["converse", "converse_stream"])
@pytest.mark.parametrize("answer_language", [None, "ja", "en"])
class TestPreliminaryFindingsStayInternal:
    async def test_system_instruction_says_the_findings_are_internal(self, stream: bool, answer_language: str | None):
        call = await _synthesize(stream, working_answer=_WORKING_ANSWER, answer_language=answer_language)
        _assert_rule(call["system"])

    async def test_findings_block_repeats_the_rule(self, stream: bool, answer_language: str | None):
        call = await _synthesize(stream, working_answer=_WORKING_ANSWER, answer_language=answer_language)
        prompt = call["prompt"]
        start = prompt.index("<agent_preliminary_findings>")
        end = prompt.index("</agent_preliminary_findings>")
        block = prompt[start:end]
        _assert_rule(block)
        # The rule precedes the agent's text, so the model reads it before the content.
        assert block.index(_NEVER_MENTION) < block.index(_WORKING_ANSWER)

    async def test_language_directive_stays_last(self, stream: bool, answer_language: str | None):
        """The rule lives in the base instruction; the output-language directive keeps the last slot."""
        call = await _synthesize(stream, working_answer=_WORKING_ANSWER, answer_language=answer_language)
        system = call["system"]
        assert system.index(_NEVER_MENTION) < system.rindex("OUTPUT LANGUAGE — HIGHEST PRIORITY")


@pytest.mark.parametrize("stream", [False, True], ids=["converse", "converse_stream"])
@pytest.mark.parametrize("working_answer", [None, "", "   "], ids=["none", "empty", "blank"])
class TestStandardSynthesisWithoutWorkingAnswer:
    """Standard (non-agentic) synthesis has no working answer: no findings block, and no rule.

    The rule would otherwise stop the model from reporting a verification state that
    comes from the documents, or from answering "Can you verify that claim X was approved?".
    """

    async def test_no_findings_block(self, stream: bool, working_answer: str | None):
        call = await _synthesize(stream, working_answer=working_answer, answer_language=None)
        assert "<agent_preliminary_findings>" not in call["prompt"]

    async def test_system_instruction_has_no_rule(self, stream: bool, working_answer: str | None):
        call = await _synthesize(stream, working_answer=working_answer, answer_language=None)
        _assert_no_rule(call["system"])
        assert "[CONFIDENCE: X.X]" in call["system"]
