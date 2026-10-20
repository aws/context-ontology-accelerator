# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Answer language is resolved once per agentic session and named in later prompts.

Asking each model call to "match the question's language" let the planner's
JSON-only assess call re-guess the language, and it sometimes wrote an English
question's working answer in Spanish or Italian, which synthesis then copied.
"""

import json

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.decompose import DecompositionPlanner
from coa_serve.tier3.agentic.planner import _ASSESS_SYSTEM, _assess_system
from coa_serve.tier3.synthesizer import _LANGUAGE_DIRECTIVE, Synthesizer


class FakeClient:
    def __init__(self, text: str = "", raises: Exception | None = None) -> None:
        self._text = text
        self._raises = raises
        self.calls: list[dict] = []

    async def converse(self, prompt, **kwargs) -> ConverseResult:
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return ConverseResult(text=self._text, guardrail_blocked=False)


def _decomposition(language: str | None) -> str:
    payload = {"sub_questions": [{"text": "Which parts become constrained?", "multi_entity": False}]}
    if language is not None:
        payload["language"] = language
    return json.dumps(payload)


@pytest.mark.unit
class TestDecomposeReportsLanguage:
    async def test_one_call_returns_sub_questions_and_canonical_tag(self):
        client = FakeClient(text=_decomposition(" EN "))
        sub_questions, language = await DecompositionPlanner(client).decompose_with_language("Which parts?")
        assert [sq.text for sq in sub_questions] == ["Which parts become constrained?"]
        assert language == "en"
        assert len(client.calls) == 1

    async def test_labelled_tag_is_recovered(self):
        client = FakeClient(text=_decomposition("Language: es"))
        assert (await DecompositionPlanner(client).decompose_with_language("¿Qué partes?"))[1] == "es"

    @pytest.mark.parametrize("raw", ["English", "I think it is en", "", 7, None])
    async def test_unusable_tag_returns_empty(self, raw):
        client = FakeClient(text=_decomposition(raw))
        sub_questions, language = await DecompositionPlanner(client).decompose_with_language("Hi")
        assert language == ""
        assert len(sub_questions) == 1

    async def test_kana_wins_over_the_model_tag(self):
        client = FakeClient(text=_decomposition("en"))
        assert (await DecompositionPlanner(client).decompose_with_language("どの部品が制約を受けますか？"))[1] == "ja"

    async def test_hangul_settles_korean_even_when_planner_fails(self):
        client = FakeClient(raises=RuntimeError("boom"))
        sub_questions, language = await DecompositionPlanner(client).decompose_with_language("어떤 부품이 제한됩니까?")
        assert language == "ko"
        assert len(sub_questions) == 1

    async def test_planner_error_returns_empty_language(self):
        client = FakeClient(raises=RuntimeError("boom"))
        assert (await DecompositionPlanner(client).decompose_with_language("Hi"))[1] == ""

    async def test_decompose_keeps_its_list_return(self):
        client = FakeClient(text=_decomposition("en"))
        sub_questions = await DecompositionPlanner(client).decompose("Which parts?")
        assert [sq.text for sq in sub_questions] == ["Which parts become constrained?"]


@pytest.mark.unit
class TestNamedLanguagePrompts:
    def test_assess_names_the_resolved_language(self):
        system = _assess_system("es")
        assert "Spanish (BCP 47 tag `es`)" in system
        assert "SAME language as the sub-question" not in system
        assert system.count("LANGUAGE:") == 1

    def test_assess_unresolved_matches_the_sub_question(self):
        assert _assess_system("") == _ASSESS_SYSTEM
        assert "SAME language as the sub-question" in _ASSESS_SYSTEM
        assert "ignore their language and match the sub-question's language" in _ASSESS_SYSTEM

    def test_assess_resolved_clause_wording(self):
        assert (
            "LANGUAGE: Write your working_answer in Spanish (BCP 47 tag `es`), the language of "
            "the user's question. The gathered context may be in other languages; ignore their "
            "language.\n\n"
        ) in _assess_system("es")
        assert _ASSESS_SYSTEM.endswith('"working_answer": "<your grounded best answer>"}')

    def test_synthesizer_names_the_resolved_language(self):
        system = Synthesizer._build_system_instruction(object(), "nonce", "m", "ja")
        assert "Japanese (BCP 47 tag `ja`)" in system
        assert _LANGUAGE_DIRECTIVE not in system

    def test_synthesizer_unresolved_keeps_the_original_directive(self):
        assert Synthesizer._build_system_instruction(object(), "nonce", "m").endswith(_LANGUAGE_DIRECTIVE)

    def test_fork_carries_the_resolved_language(self):
        ctx = AccumulatedContext()
        ctx.answer_language = "ja"
        assert ctx.fork().answer_language == "ja"
