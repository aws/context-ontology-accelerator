# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

r"""Query decomposition planner for the Agentic Retriever (Req 13.1-13.3, 13.10).

:class:`DecompositionPlanner` performs Query_Decomposition: it inspects the input
question and decides whether to split it into two or more independent
:class:`~coa_serve.tier3.agentic.models.SubQuestion`\\ s (when the
question refers to multiple distinct entities or contains multiple distinct asks,
Req 13.2) or to keep it as a single self-contained Sub_Question (Req 13.3). The
decision is delegated to the **planner LLM** — a Bedrock Converse concern that is
SEPARATE from final synthesis but runs under the same Bedrock client + region and
never bypasses the synthesis-time guardrail (mirroring the controller's planner).

The planner is asked for a small structured-JSON decision so decomposition is
deterministic to parse and easy to mock in unit tests. A self-contained ask that
still names an unresolved *set* of entities (e.g. "competitors", "subsidiaries")
is kept as a single Sub_Question flagged ``multi_entity=True`` so the reasoning
loop can anchor and branch on those entities later rather than guessing them up
front (see the worked example in the design).

Robustness: the planner LLM is best-effort. Any failure — the Converse call
raising, the guardrail blocking, or an unparseable / empty response — degrades
gracefully to treating the whole query as a single Sub_Question (Req 13.3), with
the outcome recorded in the trace. Decomposition never raises.

Import discipline (Requirement 12.3): this module imports only the dependency-free
sibling models and ``structlog``; the planner :class:`LLMClient` is referenced
structurally (only ``converse``) and typed under ``TYPE_CHECKING``, so importing
this module never pulls ``graphrag_toolkit`` (or the concrete Bedrock client) into
``sys.modules``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from coa_common.language_tags import LanguageTagError, canonicalize_language_tag, primary_language

from .models import SubQuestion

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ...clients.base import LLMClient
    from ...trace import TraceCollector

logger = structlog.get_logger(__name__)

# Hard cap on how much of the input question is sent to the planner LLM, matching
# the synthesizer's query budget so an oversized question cannot blow the prompt.
_MAX_QUERY_CHARS = 2000

# System instruction passed via the Converse API's ``system`` parameter. All static
# guidance lives here (not in the user message) to avoid tripping PROMPT_ATTACK
# guardrail filters, consistent with the Synthesizer.
_SYSTEM_INSTRUCTION = (
    "You are a query planner for a knowledge retrieval system. Your only job is to "
    "decide whether a user's question should be split into multiple independent "
    "sub-questions.\n\n"
    "Rules:\n"
    "- Decompose into TWO OR MORE sub-questions ONLY when the question refers to "
    "multiple DISTINCT named entities or contains multiple DISTINCT asks that can be "
    "pursued independently.\n"
    "- If the question is a single self-contained ask, return it UNCHANGED as a "
    "single sub-question.\n"
    "- If the question refers to an UNRESOLVED SET of entities (e.g. 'competitors', "
    "'subsidiaries', 'partners') rather than named ones, keep it as a SINGLE "
    'sub-question and set "multi_entity": true, so the agent can anchor and branch '
    "on those entities during retrieval rather than guessing them now.\n\n"
    "Also report the natural language the question is written in, as its BCP 47 "
    "language tag (for example en, ja, es, pt-BR).\n\n"
    "Respond with ONLY a JSON object of this exact shape, and no prose outside it:\n"
    '{"sub_questions": [{"text": "<question text>", "multi_entity": <true|false>}], '
    '"language": "<BCP 47 tag>"}\n'
    "Always return at least one sub-question."
)

# Scripts that settle the language without a model call. Kana is unique to
# Japanese and Hangul to Korean; Han characters alone are left to the model, since
# they could be Chinese or Japanese.
_SCRIPT_LANGUAGES = (
    (re.compile(r"[\u3040-\u30ff]"), "ja"),
    (re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"), "ko"),
)


def _script_language(text: str) -> str:
    """Return the tag a script settles (kana -> ja, Hangul -> ko), or ``""``."""
    for pattern, tag in _SCRIPT_LANGUAGES:
        if pattern.search(text):
            return tag
    return ""


def _parse_language_tag(raw: Any) -> str:
    """Return the canonical BCP 47 tag the model reported, or ``""``.

    Accepts a bare tag (``en``) or a labelled one (``Language: en``); the part
    after the last colon is used. Words in free prose are NOT scanned for a tag,
    because common English words are valid tags (``it`` is Italian, ``is`` is
    Icelandic). A primary subtag longer than three letters is rejected too: the
    tag syntax accepts words like ``english``, but no language in use has such a
    code. Anything that does not validate is dropped and never reaches a prompt.
    """
    if not isinstance(raw, str):
        return ""
    candidate = raw.rsplit(":", 1)[-1].strip().strip("`'\".")
    try:
        tag = canonicalize_language_tag(candidate)
    except LanguageTagError:
        tag = ""
    if tag and len(primary_language(tag)) <= 3:
        return tag
    logger.warning("decompose_language_unresolved", raw=raw[:40])
    return ""


class DecompositionPlanner:
    """Splits an input question into Sub_Questions via the planner LLM (Req 13.1-13.3).

    A single instance is reused across requests; it is stateless. The planner LLM
    is consulted once per :meth:`decompose` call and its structured-JSON decision
    is parsed into :class:`SubQuestion` objects. Unit tests inject a mocked
    :class:`LLMClient` whose ``converse`` returns scripted JSON, so decomposition
    is fully deterministic without a live Bedrock call.

    Args:
        planner_client: The Bedrock :class:`LLMClient` used for the planner
            Converse call (the same client class used for synthesis; a distinct
            *concern*, not necessarily a distinct instance).
        guardrail_id: Optional guardrail id applied to the user-supplied question
            via ``guard_content`` so the planner never bypasses the guardrail used
            at synthesis time. ``None``/empty disables guard tagging.
        max_query_chars: Upper bound on the question text sent to the planner.
    """

    def __init__(
        self,
        planner_client: LLMClient,
        *,
        guardrail_id: str | None = None,
        guardrail_id_provider: Callable[[], str] | None = None,
        max_query_chars: int = _MAX_QUERY_CHARS,
    ) -> None:
        """Bind the planner LLM client and optional guardrail / query-length bound."""
        self._planner = planner_client
        self._guardrail_id = guardrail_id or None
        self._guardrail_id_provider = guardrail_id_provider
        self._max_query_chars = max_query_chars

    def _effective_guardrail_id(self) -> str | None:
        """Resolve the guardrail id LIVE via the provider when injected.

        Falls back to the construction-time ``guardrail_id`` (already normalized
        to ``None`` when empty) otherwise. Returns ``None`` for an empty provider
        value to preserve the disabled-guardrail semantics of the call site.
        """
        if self._guardrail_id_provider is not None:
            gid = self._guardrail_id_provider()
            return gid or None
        return self._guardrail_id

    async def decompose(self, query: str, trace: TraceCollector | None = None) -> list[SubQuestion]:
        """Decompose ``query`` into Sub_Questions; see :meth:`decompose_with_language`."""
        sub_questions, _language = await self.decompose_with_language(query, trace)
        return sub_questions

    async def decompose_with_language(
        self, query: str, trace: TraceCollector | None = None
    ) -> tuple[list[SubQuestion], str]:
        """Decompose ``query`` into one or more Sub_Questions (Req 13.1-13.3).

        Consults the planner LLM for a structured decomposition decision. Returns
        two or more Sub_Questions when the planner determines the question refers
        to multiple distinct entities / asks (Req 13.2), otherwise a single
        Sub_Question (Req 13.3). Always returns at least one Sub_Question; on any
        planner failure it degrades to a single Sub_Question carrying the original
        query. The Query_Decomposition is recorded in the trace (Req 13.10).

        The same planner call also names the question's language, so the session
        resolves it once without a second model call. A script that settles the
        language (kana, Hangul) wins over the model's tag. When no tag can be
        resolved (including every fallback path) the language is ``""`` and
        callers keep matching the question's language as before.

        Args:
            query: The input question to evaluate for decomposition.
            trace: The session :class:`TraceCollector`; when provided, one
                ``decompose`` step is recorded describing the decision.

        Returns:
            A non-empty list of :class:`SubQuestion` with ``origin="decomposition"``,
            and the canonical BCP 47 tag of the question's language or ``""``.
        """
        start = time.perf_counter()
        prompt = query[: self._max_query_chars]
        script_language = _script_language(prompt)

        try:
            result = await self._planner.converse(
                prompt=prompt,
                system=_SYSTEM_INSTRUCTION,
                guardrail_id=self._effective_guardrail_id(),
                guard_content=f"Question: {prompt}",
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - decomposition must never raise.
            logger.warning(
                "decompose_planner_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._fallback(query, trace, start, detail=f"planner error: {type(exc).__name__}"), script_language

        if getattr(result, "guardrail_blocked", False):
            logger.warning("decompose_guardrail_blocked")
            return self._fallback(query, trace, start, detail="planner guardrail blocked"), script_language

        payload = self._extract_json_object(result.text)
        sub_questions = self._parse_payload(payload)
        if sub_questions is None:
            return self._fallback(query, trace, start, detail="unparseable planner response"), script_language
        language = script_language or _parse_language_tag(payload.get("language") if payload else None)

        ms = int((time.perf_counter() - start) * 1000)
        if len(sub_questions) > 1:
            detail = f"decomposed into {len(sub_questions)} sub-questions: " + " | ".join(
                sq.text[:60] for sq in sub_questions
            )
        else:
            multi = " (multi-entity)" if sub_questions[0].multi_entity else ""
            detail = f"single self-contained sub-question{multi}"
        self._record(trace, "success", ms, detail)
        return sub_questions, language

    # ── parsing ────────────────────────────────────────────────────────

    def _parse(self, text: str, query: str) -> list[SubQuestion] | None:
        """Parse the planner's JSON decision into Sub_Questions.

        Tolerates markdown code fences and surrounding prose by extracting the
        first ``{ … }`` JSON object. Returns ``None`` when no usable JSON object /
        ``sub_questions`` list can be recovered (caller falls back). An empty or
        all-blank ``sub_questions`` list also yields ``None``.
        """
        return self._parse_payload(self._extract_json_object(text))

    def _parse_payload(self, payload: dict[str, Any] | None) -> list[SubQuestion] | None:
        """Turn an extracted JSON object into Sub_Questions, or ``None`` if unusable."""
        if payload is None:
            return None
        raw = payload.get("sub_questions")
        if not isinstance(raw, list):
            return None

        sub_questions: list[SubQuestion] = []
        for item in raw:
            sq = self._coerce_sub_question(item)
            if sq is not None:
                sub_questions.append(sq)
        if not sub_questions:
            return None
        return sub_questions

    @staticmethod
    def _coerce_sub_question(item: Any) -> SubQuestion | None:
        """Coerce one planner list entry into a Sub_Question, or ``None`` if unusable."""
        text: str | None = None
        multi_entity = False
        if isinstance(item, dict):
            value = item.get("text")
            if isinstance(value, str):
                text = value
            multi_entity = bool(item.get("multi_entity", False))
        elif isinstance(item, str):
            text = item
        if text is None:
            return None
        text = text.strip()
        if not text:
            return None
        return SubQuestion(text=text, multi_entity=multi_entity, origin="decomposition")

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        """Extract and parse the first JSON object from ``text``.

        Handles a bare JSON object, one wrapped in ```` ```json ```` fences, or one
        embedded in surrounding prose by slicing from the first ``{`` to the last
        ``}``. Returns ``None`` on any parse failure.
        """
        if not text:
            return None
        candidate = text.strip()
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            pass
        # Fall back to slicing the outermost braces out of surrounding prose/fences.
        first = candidate.find("{")
        last = candidate.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None
        try:
            parsed = json.loads(candidate[first : last + 1])
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # ── fallback & tracing ──────────────────────────────────────────────

    def _fallback(
        self,
        query: str,
        trace: TraceCollector | None,
        start: float,
        *,
        detail: str,
    ) -> list[SubQuestion]:
        """Degrade to a single Sub_Question carrying the whole query (Req 13.3).

        Records the degraded decomposition outcome in the trace so the fallback is
        observable, then returns a one-element list. Never raises.
        """
        ms = int((time.perf_counter() - start) * 1000)
        self._record(trace, "degraded", ms, f"{detail}; treating query as a single sub-question")
        return [SubQuestion(text=query, multi_entity=False, origin="decomposition")]

    @staticmethod
    def _record(trace: TraceCollector | None, status: str, ms: int, detail: str) -> None:
        """Record the Query_Decomposition in the trace when a collector is present (Req 13.10)."""
        if trace is None:
            return
        trace.record("decompose", status, ms, detail=detail)
