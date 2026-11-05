# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A follow-up is answered in ITS language, not the language of earlier turns.

A Japanese question followed by a short English follow-up ("Which documents say
that?") was answered in Japanese: the standard Tier 3 path asked the model to work
out the question's language, and with a Japanese ``<conversation_history>`` it took
the history's language. The deep-reasoning path named the language but still did
not say that the history's language does not count.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock

import pytest
from coa_common.label_language import LabelPreference
from coa_serve import namespace_language
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier3 import knowledge_retriever as knowledge_retriever_module
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic import decompose as decompose_module
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.decompose import DecompositionPlanner
from coa_serve.tier3.agentic.models import PlannerDecision, SubQuestion
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.graph_traverser import GraphTraverser
from coa_serve.tier3.knowledge_retriever import KnowledgeRetriever
from coa_serve.tier3.synthesizer import (
    _DETECT_LANGUAGE_INSTRUCTION,
    _LANGUAGE_DIRECTIVE,
    _NEUTRAL_FOLLOWUP_CLAUSE,
    Synthesizer,
    _dominant_script_language,
)
from coa_serve.tier3.vector_retriever import VectorRetriever

NAMESPACE = "550e8400-e29b-41d4-a716-446655440000"
JA_QUESTION = "コンプレッサー CMP-3 のオイル交換の周期と定格出力を教えてください。"
EN_QUESTION = "What is the oil change interval and the rated output of compressor CMP-3?"
EN_FOLLOWUP = "Which documents say that?"
JA_FOLLOWUP = "その情報はどの文書に書かれていますか？"
# An English question that quotes a Japanese term: the term must not make it Japanese.
EN_KATAKANA = "What does the manual say about the アース terminal?"
# A follow-up that shows no language of its own.
NEUTRAL_FOLLOWUP = "CMP-3?"
KO_FOLLOWUP = "그 정보는 어느 문서에 있나요?"
JA_HISTORY = [
    {"role": "user", "content": JA_QUESTION},
    {"role": "assistant", "content": "CMP-3 のオイル交換周期は 3 か月、定格出力は 37 kW です。"},
]
EN_HISTORY = [
    {"role": "user", "content": EN_QUESTION},
    {"role": "assistant", "content": "The oil is changed every 3 months; the rated output is 37 kW."},
]
ENGLISH = "English (BCP 47 tag `en`)"
JAPANESE = "Japanese (BCP 47 tag `ja`)"
HISTORY_CLAUSE = "the language of earlier turns does NOT decide the language of this answer"


class FakeBedrock:
    """Answers the language-detection call with ``detected`` and every other call with prose."""

    def __init__(
        self, detected: str = "en", detect_raises: Exception | None = None, detect_delay_s: float = 0.0
    ) -> None:
        self._detected = detected
        self._detect_raises = detect_raises
        self._detect_delay_s = detect_delay_s
        self.calls: list[dict] = []

    def _is_detect(self, kwargs: dict) -> bool:
        return kwargs.get("system") == _DETECT_LANGUAGE_INSTRUCTION

    async def converse(self, prompt: str, **kwargs) -> ConverseResult:
        self.calls.append({"prompt": prompt, **kwargs})
        if self._is_detect(kwargs):
            if self._detect_delay_s:
                await asyncio.sleep(self._detect_delay_s)
            if self._detect_raises is not None:
                raise self._detect_raises
            return ConverseResult(text=self._detected)
        return ConverseResult(text="Policy v2.3 says so.\n\n[CONFIDENCE: 0.8]")

    async def converse_stream(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        yield "Policy v2.3 says so."

    @property
    def detect_calls(self) -> list[dict]:
        return [c for c in self.calls if self._is_detect(c)]

    @property
    def synthesis_system(self) -> str:
        systems = [c["system"] for c in self.calls if not self._is_detect(c)]
        assert len(systems) == 1
        return systems[0]


def _retriever(bedrock: FakeBedrock) -> KnowledgeRetriever:
    opensearch = AsyncMock()
    opensearch.search.return_value = [
        VectorHit(id="c1", text="CMP-3 oil change every 3 months", score=0.9, metadata={"source_doc": "m.txt"})
    ]
    neptune = AsyncMock()
    neptune.query.return_value = []
    return KnowledgeRetriever(
        VectorRetriever(opensearch),
        GraphTraverser(neptune, graph_uri_template="https://ontology-workbench.local/{namespace}"),
        Synthesizer(bedrock, guardrail_id="gr-test-123"),
    )


def _step(result, name: str) -> dict | None:
    return next((s for s in result.trace_steps if s["step"] == name), None)


@pytest.mark.unit
class TestStandardPathFollowUp:
    @pytest.mark.parametrize("stream", [False, True])
    async def test_english_followup_after_japanese_turn_names_english(self, stream):
        bedrock = FakeBedrock(detected="en")
        on_token = AsyncMock() if stream else None
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY, on_token=on_token
        )

        system = bedrock.synthesis_system
        assert ENGLISH in system
        assert _LANGUAGE_DIRECTIVE not in system
        assert HISTORY_CLAUSE in system
        assert system.endswith("Only the user's current question does.")
        # The language is detected from the CURRENT question only.
        assert [c["prompt"] for c in bedrock.detect_calls] == [EN_FOLLOWUP]
        assert _step(result, "answer_language") == {
            "step": "answer_language",
            "status": "success",
            "durationMs": 0,
            "detail": "en",
        }

    async def test_japanese_followup_after_english_turn_names_japanese_without_a_model_call(self):
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(
            JA_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=EN_HISTORY
        )

        system = bedrock.synthesis_system
        assert JAPANESE in system
        assert HISTORY_CLAUSE in system
        assert bedrock.detect_calls == []
        assert _step(result, "answer_language")["detail"] == "ja"

    async def test_single_turn_question_keeps_the_original_directive(self):
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(EN_QUESTION, NAMESPACE, embedding=[0.1] * 4)

        assert bedrock.synthesis_system.endswith(_LANGUAGE_DIRECTIVE)
        assert bedrock.detect_calls == []
        assert _step(result, "answer_language") is None

    async def test_detection_failure_keeps_the_guess_but_still_discounts_history(self):
        bedrock = FakeBedrock(detect_raises=RuntimeError("throttled"))
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY
        )

        system = bedrock.synthesis_system
        assert _LANGUAGE_DIRECTIVE in system
        assert HISTORY_CLAUSE in system
        assert result.synthesized_answer
        assert _step(result, "answer_language") is None

    @pytest.mark.parametrize("raw", ["English", "I think it is en", ""])
    async def test_unusable_tag_is_dropped(self, raw):
        bedrock = FakeBedrock(detected=raw)
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY
        )
        assert _LANGUAGE_DIRECTIVE in bedrock.synthesis_system
        assert _step(result, "answer_language") is None

    async def test_lexical_path_names_the_followup_language(self):
        from coa_serve.lexical.baseline_retriever import BaselineResult
        from coa_serve.lexical.strategies import RetrieverStrategy

        bedrock = FakeBedrock(detected="en")
        lexical = AsyncMock()
        lexical.retrieve.return_value = BaselineResult(chunks=(), entities=(), trace_steps=())
        retriever = KnowledgeRetriever(
            None, None, Synthesizer(bedrock, guardrail_id="gr-test-123"), lexical_retriever=lexical
        )
        result = await retriever.resolve(
            EN_FOLLOWUP,
            NAMESPACE,
            conversation_history=JA_HISTORY,
            retriever_strategy=RetrieverStrategy.CHUNK_BASED_SEMANTIC,
        )

        assert ENGLISH in bedrock.synthesis_system
        assert HISTORY_CLAUSE in bedrock.synthesis_system
        assert _step(result, "answer_language")["detail"] == "en"


@pytest.mark.unit
class TestResolveAnswerLanguage:
    async def test_detection_sees_only_the_current_question(self):
        bedrock = FakeBedrock(detected="en")
        tag = await Synthesizer(bedrock, guardrail_id="gr").resolve_answer_language(EN_FOLLOWUP, JA_HISTORY)
        assert tag == "en"
        (call,) = bedrock.detect_calls
        assert call["prompt"] == EN_FOLLOWUP
        assert JA_QUESTION not in str(call)

    async def test_kana_settles_japanese_even_with_english_history(self):
        bedrock = FakeBedrock(detected="en")
        assert await Synthesizer(bedrock, guardrail_id="gr").resolve_answer_language(JA_FOLLOWUP, EN_HISTORY) == "ja"
        assert bedrock.calls == []

    async def test_guardrail_blocked_detection_is_unresolved(self):
        bedrock = AsyncMock()
        bedrock.converse.return_value = ConverseResult(text="en", guardrail_blocked=True)
        assert await Synthesizer(bedrock, guardrail_id="gr").resolve_answer_language(EN_FOLLOWUP, JA_HISTORY) == ""


@pytest.mark.unit
class TestDeepPathFollowUp:
    """The agentic path resolves the tag from the question; synthesis must also discount the history."""

    @pytest.mark.parametrize(
        ("question", "history", "tag", "name"),
        [(EN_FOLLOWUP, JA_HISTORY, "en", ENGLISH), (JA_FOLLOWUP, EN_HISTORY, "ja", JAPANESE)],
    )
    @pytest.mark.parametrize("stream", [False, True])
    async def test_named_directive_discounts_history_language(self, question, history, tag, name, stream):
        bedrock = FakeBedrock()
        synthesizer = Synthesizer(bedrock, guardrail_id="gr-test-123")
        kwargs = {"conversation_history": history, "working_answer": "Draft.", "answer_language": tag}
        if stream:
            await synthesizer.synthesize_stream(question, [], [], on_token=AsyncMock(), **kwargs)
        else:
            await synthesizer.synthesize(question, [], [], **kwargs)

        system = bedrock.synthesis_system
        assert name in system
        assert HISTORY_CLAUSE in system
        assert _LANGUAGE_DIRECTIVE not in system
        # The agentic path already resolved the tag, so synthesis makes no detection call.
        assert bedrock.detect_calls == []

    async def test_named_directive_without_history_is_unchanged(self):
        from coa_serve.tier3.synthesizer import _named_language_directive

        system = Synthesizer._build_system_instruction(object(), "nonce", "m", "en")
        assert system.endswith(_named_language_directive("en"))
        assert HISTORY_CLAUSE not in system


@pytest.mark.unit
class TestFollowUpOnlyAndScriptShare:
    """Single-turn questions keep the old behaviour; a quoted term does not decide the language."""

    @pytest.mark.parametrize("question", [JA_QUESTION, EN_KATAKANA, KO_FOLLOWUP])
    async def test_single_turn_question_keeps_the_generic_directive_and_trace(self, question):
        bedrock = FakeBedrock(detected="ja")
        result = await _retriever(bedrock).resolve(question, NAMESPACE, embedding=[0.1] * 4)

        assert bedrock.synthesis_system.endswith(_LANGUAGE_DIRECTIVE)
        assert bedrock.detect_calls == []
        assert _step(result, "answer_language") is None

    async def test_english_followup_with_a_katakana_term_after_japanese_turn_is_english(self):
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(
            EN_KATAKANA, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY
        )

        system = bedrock.synthesis_system
        assert ENGLISH in system
        assert JAPANESE not in system
        # The katakana term does not settle the language, so the model is asked.
        assert [c["prompt"] for c in bedrock.detect_calls] == [EN_KATAKANA]
        assert _step(result, "answer_language")["detail"] == "en"

    async def test_hangul_followup_settles_korean_without_a_model_call(self):
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(
            KO_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=EN_HISTORY
        )
        assert "Korean" in bedrock.synthesis_system
        assert bedrock.detect_calls == []
        assert _step(result, "answer_language")["detail"] == "ko"

    @pytest.mark.parametrize(
        ("text", "tag"),
        [
            (JA_QUESTION, "ja"),
            (JA_FOLLOWUP, "ja"),
            ("CMP-3 の定格出力は？", "ja"),
            ("MTBFとは", "ja"),
            (EN_KATAKANA, ""),
            ("What is ソフトウェアアップデート?", ""),
            ("Which section of 「取扱説明書について」 covers grounding?", ""),
            ("定格出力？", ""),
            (NEUTRAL_FOLLOWUP, ""),
            (KO_FOLLOWUP, "ko"),
            ("What is 김치?", ""),
            (EN_FOLLOWUP, ""),
        ],
    )
    def test_script_share(self, text, tag):
        assert _dominant_script_language(text) == tag


@pytest.mark.unit
class TestDetectionCallCost:
    """The detection call runs only when it can change the outcome, unguarded and bounded."""

    async def test_latin_followup_after_latin_history_makes_no_model_call(self):
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=EN_HISTORY
        )

        system = bedrock.synthesis_system
        assert bedrock.detect_calls == []
        assert _LANGUAGE_DIRECTIVE in system
        assert HISTORY_CLAUSE in system
        assert _step(result, "answer_language") is None

    async def test_katakana_followup_after_latin_history_makes_no_model_call(self):
        bedrock = FakeBedrock(detected="ja")
        await _retriever(bedrock).resolve(EN_KATAKANA, NAMESPACE, embedding=[0.1] * 4, conversation_history=EN_HISTORY)
        assert bedrock.detect_calls == []
        assert JAPANESE not in bedrock.synthesis_system

    async def test_english_answer_quoting_a_japanese_title_makes_no_model_call(self):
        # Only the user's own questions decide whether detection can matter: an
        # English answer that cites a Japanese document title is not a Japanese turn.
        history = [
            {"role": "user", "content": EN_QUESTION},
            {
                "role": "assistant",
                "content": "Per Document 2 (コンプレッサー保守手順書), the oil is changed every 3 months.",
            },
        ]
        bedrock = FakeBedrock(detected="en")
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=history
        )
        assert bedrock.detect_calls == []
        assert _LANGUAGE_DIRECTIVE in bedrock.synthesis_system
        assert _step(result, "answer_language") is None

    async def test_japanese_user_turn_still_triggers_detection(self):
        bedrock = FakeBedrock(detected="en")
        await Synthesizer(bedrock, guardrail_id=None).resolve_answer_language(EN_FOLLOWUP, JA_HISTORY)
        assert len(bedrock.detect_calls) == 1

    async def test_detection_call_has_no_guardrail(self):
        bedrock = FakeBedrock(detected="en")
        await Synthesizer(bedrock, guardrail_id="gr").resolve_answer_language(EN_FOLLOWUP, JA_HISTORY)
        (call,) = bedrock.detect_calls
        assert call["guardrail_id"] is None
        assert call["guard_content"] is None
        # Room for a reasoning model to think before the tag: a reply cut off before
        # any text leaves the language unresolved.
        assert call["max_tokens"] >= 256

    async def test_slow_detection_times_out_and_falls_back(self, monkeypatch):
        monkeypatch.setattr(knowledge_retriever_module, "ANSWER_LANGUAGE_TIMEOUT_S", 0.01)
        bedrock = FakeBedrock(detected="en", detect_delay_s=1.0)
        result = await _retriever(bedrock).resolve(
            EN_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY
        )

        assert _LANGUAGE_DIRECTIVE in bedrock.synthesis_system
        step = _step(result, "answer_language")
        assert step["status"] == "timeout"
        assert result.synthesized_answer


@pytest.mark.unit
class TestNeutralFollowUp:
    """A follow-up with no language of its own keeps the generic directive and leans on the history."""

    async def test_neutral_followup_after_japanese_turn_is_left_to_the_history(self):
        bedrock = FakeBedrock(detected="und")
        result = await _retriever(bedrock).resolve(
            NEUTRAL_FOLLOWUP, NAMESPACE, embedding=[0.1] * 4, conversation_history=JA_HISTORY
        )

        system = bedrock.synthesis_system
        assert _LANGUAGE_DIRECTIVE in system
        assert HISTORY_CLAUSE in system
        assert system.endswith(_NEUTRAL_FOLLOWUP_CLAUSE)
        assert "BCP 47 tag `und`" not in system
        assert _step(result, "answer_language") is None

    @pytest.mark.parametrize("raw", ["und", "zxx", "mul", "UND"])
    async def test_tags_that_name_no_language_are_unresolved(self, raw):
        bedrock = FakeBedrock(detected=raw)
        assert await Synthesizer(bedrock, guardrail_id="gr").resolve_answer_language(NEUTRAL_FOLLOWUP, JA_HISTORY) == ""
        assert len(bedrock.detect_calls) == 1

    def test_detection_prompt_offers_und(self):
        assert "respond with und" in _DETECT_LANGUAGE_INSTRUCTION

    def test_detection_prompt_reads_a_quoted_term_as_part_of_the_question(self):
        # "What do the documents say about the オイル ...?" is an English question.
        assert "quotes a word, name or title in another language" in _DETECT_LANGUAGE_INSTRUCTION

    def test_named_directive_gets_no_neutral_clause(self):
        system = Synthesizer._build_system_instruction(object(), "nonce", "m", "en", has_history=True)
        assert _NEUTRAL_FOLLOWUP_CLAUSE not in system
        assert HISTORY_CLAUSE in system


# ── deep path: a namespace-language fallback is not the question's language ──


class _Chunk:
    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id
        self.text = f"text-{chunk_id}"
        self.source_doc = "doc.pdf"
        self.label = "L"
        self.relevance_score = 0.9
        self.uri = ""
        self.statements: tuple[str, ...] = ()


class _Tool:
    name = "strategy:entity_based"
    description = "fake tool"
    input_schema = {"query": "str"}
    output_schema: dict = {}

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        return ToolResult(chunks=(_Chunk("c1"),), detail="ok")


class _Planner:
    def __init__(self) -> None:
        self._script = deque([[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]])

    async def propose_steps(self, *, sub_question, context, tool_specs):
        return self._script.popleft() if self._script else []

    async def check_sufficiency(self, *, sub_question, context):
        return True


class _Decomposer:
    def __init__(self, detected: str) -> None:
        self._detected = detected

    async def decompose_with_language(self, query, trace=None):
        return [SubQuestion(text=query)], self._detected


def _agentic(bedrock: FakeBedrock, detected: str) -> AgenticRetriever:
    registry = ToolRegistry()
    registry.register(_Tool())
    return AgenticRetriever(
        registry=registry,
        controller=ReasoningController(_Planner(), per_tool_timeout_s=30.0),
        decomposer=_Decomposer(detected),
        synthesizer=Synthesizer(bedrock, guardrail_id="gr-test-123"),
        budget=AgenticBudgetConfig(),
        final_semantic_search=False,
    )


@pytest.mark.unit
class TestDeepPathNamespaceFallback:
    """``answer_language`` defaulted from a ``ja`` namespace must not be named for a follow-up."""

    @pytest.fixture(autouse=True)
    def _unbind(self):
        yield
        namespace_language.bind_label_preference(LabelPreference.untagged_first())

    async def test_defaulted_tag_with_history_uses_generic_directive(self):
        namespace_language.bind_label_preference(LabelPreference.for_namespace("ja"))
        bedrock = FakeBedrock()
        await _agentic(bedrock, detected="").resolve(EN_FOLLOWUP, NAMESPACE, conversation_history=JA_HISTORY)

        system = bedrock.synthesis_system
        assert JAPANESE not in system
        assert _LANGUAGE_DIRECTIVE in system
        assert HISTORY_CLAUSE in system

    async def test_detected_tag_with_history_is_still_named(self):
        namespace_language.bind_label_preference(LabelPreference.for_namespace("ja"))
        bedrock = FakeBedrock()
        await _agentic(bedrock, detected="en").resolve(EN_FOLLOWUP, NAMESPACE, conversation_history=JA_HISTORY)

        system = bedrock.synthesis_system
        assert ENGLISH in system
        assert HISTORY_CLAUSE in system

    async def test_defaulted_tag_without_history_keeps_the_namespace_language(self):
        namespace_language.bind_label_preference(LabelPreference.for_namespace("ja"))
        bedrock = FakeBedrock()
        await _agentic(bedrock, detected="").resolve(EN_QUESTION, NAMESPACE)

        assert JAPANESE in bedrock.synthesis_system


# ── deep path: a follow-up with no language of its own ──


class _PlannerLLM:
    """The decomposition planner's model: one sub-question, tagged ``language``."""

    def __init__(self, language: str) -> None:
        self._language = language

    async def converse(self, prompt: str, **kwargs) -> ConverseResult:
        payload = {"sub_questions": [{"text": prompt, "multi_entity": False}], "language": self._language}
        return ConverseResult(text=json.dumps(payload))


def _agentic_with_planner(bedrock: FakeBedrock, language: str) -> AgenticRetriever:
    """A deep-path retriever whose real DecompositionPlanner gets ``language`` from its model."""
    registry = ToolRegistry()
    registry.register(_Tool())
    return AgenticRetriever(
        registry=registry,
        controller=ReasoningController(_Planner(), per_tool_timeout_s=30.0),
        decomposer=DecompositionPlanner(_PlannerLLM(language)),
        synthesizer=Synthesizer(bedrock, guardrail_id="gr-test-123"),
        budget=AgenticBudgetConfig(),
        final_semantic_search=False,
    )


@pytest.mark.unit
class TestDeepPathNeutralFollowUp:
    """A bare "CMP-3?" after a Japanese turn: a tag naming no language is not named."""

    @pytest.fixture(autouse=True)
    def _unbind(self):
        yield
        namespace_language.bind_label_preference(LabelPreference.untagged_first())

    @pytest.mark.parametrize("namespace_tag", [None, "ja"])
    @pytest.mark.parametrize("raw", ["und", "UND", "zxx", "mul"])
    async def test_neutral_followup_gets_the_generic_directive_and_the_neutral_clause(self, raw, namespace_tag):
        if namespace_tag:
            namespace_language.bind_label_preference(LabelPreference.for_namespace(namespace_tag))
        bedrock = FakeBedrock()
        await _agentic_with_planner(bedrock, raw).resolve(NEUTRAL_FOLLOWUP, NAMESPACE, conversation_history=JA_HISTORY)

        system = bedrock.synthesis_system
        assert "BCP 47 tag" not in system
        assert JAPANESE not in system
        assert _LANGUAGE_DIRECTIVE in system
        assert HISTORY_CLAUSE in system
        assert system.endswith(_NEUTRAL_FOLLOWUP_CLAUSE)

    async def test_single_turn_question_tagged_und_keeps_the_generic_directive(self):
        bedrock = FakeBedrock()
        await _agentic_with_planner(bedrock, "und").resolve(NEUTRAL_FOLLOWUP, NAMESPACE)

        system = bedrock.synthesis_system
        assert "BCP 47 tag" not in system
        assert system.endswith(_LANGUAGE_DIRECTIVE)

    async def test_a_followup_the_planner_tags_with_a_language_is_still_named(self):
        bedrock = FakeBedrock()
        await _agentic_with_planner(bedrock, "en").resolve(EN_FOLLOWUP, NAMESPACE, conversation_history=JA_HISTORY)

        system = bedrock.synthesis_system
        assert ENGLISH in system
        assert _NEUTRAL_FOLLOWUP_CLAUSE not in system

    def test_planner_prompt_offers_und(self):
        assert "report und" in decompose_module._SYSTEM_INSTRUCTION
        assert "report und" in decompose_module._SYSTEM_INSTRUCTION_WITH_TERMS
