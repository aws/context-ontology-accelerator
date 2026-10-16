# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve-side graphrag-toolkit patch: don't prune topics the reranker can't compare.

Why this exists
---------------
Every lexical strategy runs the toolkit's ``RerankStatements`` (``reranker="tfidf"``)
and then ``PruneStatements``. The tfidf reranker scores each ``"{topic}: {statement}"``
against the question by shared character trigrams. ``PruneStatements`` then drops every
statement scoring below ``max_score * statement_pruning_factor`` (0.05 by default).

A statement written in a different script from the question shares no trigram with it,
so it scores 0 whatever it says. When no statement scores above 0, the threshold is 0
and nothing is pruned. As soon as one statement does, every 0 is dropped, and an empty
topic takes its topic and source with it. The semantic beam's best matches are lost
because a few unrelated statements share a word with the question.

#1263 is that case. Over English statements, ``循環取引`` scores every statement 0 and
gets the circular-trading topic. ``取引先との間で循環取引が行われている疑いがあります``
also contains ``取引先`` and ``疑いがある``, which appear in a handful of Japanese
statements about other rules. Those score above 0, the English circular-trading
statements are pruned, and the answer says the documents don't cover the term.

The fix
-------
``PruneStatements`` leaves a topic unpruned when the reranker could not compare any of
its statements with the question. A scored string is comparable when it has a letter in
a script the question is mainly written in (see :func:`comparable_families`), or in a
script of an entity-context match value. Every other topic is pruned exactly as before,
so a question and statements in the same script behave as they did.

Known limits: exempt statements keep their 0 score, so a source made only of them still
ranks below sources that scored, and later caps (``TruncateResults``, the synthesizer's
source limit) can drop it. An absolute ``statement_pruning_threshold`` does not apply to
an exempt topic. Applied lazily (never at import) to preserve the botocore sync-only
invariant documented in ``strategies.py``.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Any

import regex
import structlog

logger = structlog.get_logger(__name__)

# What tfidf_matcher does to a string before it builds trigrams (matcher.py lowercases,
# ngrams.py strips these characters, n=3).
_NGRAM_LENGTH = 3
_TFIDF_STRIP_RE = re.compile(r"[,-./]|\sBD")

# A script counts for the question when it holds at least this share of the question's
# single-script trigrams. One ASCII acronym in a Japanese question (``AWSの料金で…``)
# is a trigram or two out of a dozen or more and does not make every English statement
# comparable; a question written mostly in a script always makes that script count.
_MIN_QUESTION_SHARE = 0.25

# Han, kana and Bopomofo are one family: a Japanese statement mixes kanji and kana, so a
# kanji-only trigram in the question can match it. Every other script is its own family.
_HAN_FAMILY = "han"
_SCRIPT_FAMILIES: tuple[tuple[str, Any], ...] = tuple(
    (family, regex.compile(rf"\p{{scx={script}}}"))
    for family, script in (
        ("latin", "Latin"),
        (_HAN_FAMILY, "Han"),
        (_HAN_FAMILY, "Hiragana"),
        (_HAN_FAMILY, "Katakana"),
        (_HAN_FAMILY, "Bopomofo"),
        ("hangul", "Hangul"),
        ("cyrillic", "Cyrillic"),
        ("greek", "Greek"),
        ("armenian", "Armenian"),
        ("georgian", "Georgian"),
        ("hebrew", "Hebrew"),
        ("arabic", "Arabic"),
        ("syriac", "Syriac"),
        ("thaana", "Thaana"),
        ("devanagari", "Devanagari"),
        ("bengali", "Bengali"),
        ("gurmukhi", "Gurmukhi"),
        ("gujarati", "Gujarati"),
        ("oriya", "Oriya"),
        ("tamil", "Tamil"),
        ("telugu", "Telugu"),
        ("kannada", "Kannada"),
        ("malayalam", "Malayalam"),
        ("sinhala", "Sinhala"),
        ("thai", "Thai"),
        ("lao", "Lao"),
        ("tibetan", "Tibetan"),
        ("myanmar", "Myanmar"),
        ("khmer", "Khmer"),
        ("mongolian", "Mongolian"),
        ("ethiopic", "Ethiopic"),
    )
)

_patch_lock = threading.Lock()
_patched = False


def _letter_family(character: str) -> str | None:
    """The script family of a letter, or ``None`` for a non-letter or an unlisted script.

    Fullwidth and halfwidth forms are their own families: tfidf compares code points, so
    ``ＡＷＳ`` never matches ``AWS`` and ``ｶﾀｶﾅ`` never matches ``カタカナ``.
    """
    if not unicodedata.category(character).startswith("L"):
        return None
    for family, matcher in _SCRIPT_FAMILIES:
        if matcher.match(character):
            width = unicodedata.decomposition(character).split(" ", 1)[0]
            if width in ("<wide>", "<narrow>"):
                return f"{family}{width}"
            return family
    return None


def _tfidf_text(value: str) -> str:
    return _TFIDF_STRIP_RE.sub("", value.lower())


def _single_family_trigrams(value: str) -> Iterable[str]:
    """The family of each trigram of ``value`` whose letters all belong to one family.

    Trigrams with no letter (digits, spaces) or with letters from several families are
    skipped: they say nothing reliable about which script a matching statement is in.
    """
    text = _tfidf_text(value)
    for start in range(max(len(text) - _NGRAM_LENGTH + 1, 0)):
        families = {_letter_family(c) for c in text[start : start + _NGRAM_LENGTH]}
        families.discard(None)
        if len(families) == 1:
            yield families.pop()


def comparable_families(question: str, extras: Iterable[str] = ()) -> set[str]:
    """Script families the tfidf reranker meaningfully compares statements through.

    The question counts for each family holding at least ``_MIN_QUESTION_SHARE`` of its
    single-script trigrams. Entity-context extras are match values in their own right
    (``RerankStatements`` scores against them too), so every family they use counts.
    """
    counts = Counter(_single_family_trigrams(question))
    total = sum(counts.values())
    comparable = {family for family, n in counts.items() if n >= _MIN_QUESTION_SHARE * total}
    for extra in extras:
        comparable.update(_single_family_trigrams(extra))
    return comparable


def is_comparable(scored_text: str, families: set[str]) -> bool:
    """Whether ``scored_text`` has a letter in one of ``families``."""
    return any(_letter_family(c) in families for c in _tfidf_text(scored_text))


def patch_cross_script_statement_prune() -> None:
    """Make ``PruneStatements`` leave topics the reranker could not compare unpruned.

    Idempotent. If the toolkit's internals have moved, install logs and returns, and a
    query-time error falls back to the upstream prune, so retrieval never gets worse
    than upstream because of this patch.
    """
    global _patched
    with _patch_lock:
        if _patched:
            return
        try:
            from graphrag_toolkit.lexical_graph.retrieval.processors import PruneStatements
        except ImportError:
            logger.debug("graphrag_cross_script_prune_patch_skipped", reason="module not importable")
            return

        original = getattr(PruneStatements, "_process_results", None)
        if original is None:
            logger.warning("graphrag_cross_script_prune_patch_skipped", reason="_process_results missing")
            return
        # Patched twice (a reload): wrap the upstream method, not our own wrapper.
        original = getattr(original, "__wrapped__", original)

        def _process_results(self, search_results, query):
            try:
                return _prune(self, search_results, query)
            except _Fallback:
                return original(self, search_results, query)
            except Exception as exc:  # noqa: BLE001 - never worse than upstream
                logger.warning("lexical_cross_script_prune_failed", error=repr(exc))
                return original(self, search_results, query)

        _process_results.__wrapped__ = original
        PruneStatements._process_results = _process_results
        _patched = True
        logger.info("graphrag_cross_script_prune_patch_applied")


class _Fallback(Exception):
    """Use the upstream prune unchanged."""


def _prune(processor, search_results, query):
    args = processor.args
    if (
        (args.reranker or "").lower() != "tfidf"
        or not search_results.results
        or (not args.statement_pruning_factor and not args.statement_pruning_threshold)
    ):
        raise _Fallback

    entity_contexts = getattr(search_results, "entity_contexts", None)
    extras = entity_contexts.all_context_strs if entity_contexts is not None else []
    families = comparable_families(query.query_str, extras)
    if not families:
        # No letter trigram to reason from (a digits-only question, or an unlisted
        # script): keep the upstream behavior.
        raise _Fallback

    scores = [
        statement.score
        for search_result in search_results.results
        for topic in search_result.topics
        for statement in topic.statements
    ]
    if not scores:
        raise _Fallback  # upstream's own behavior for this shape
    min_threshold = max(scores) * args.statement_pruning_factor
    if args.statement_pruning_threshold:
        min_threshold = max(min_threshold, args.statement_pruning_threshold)

    exempt_topics = 0

    def prune_statements(topic):
        nonlocal exempt_topics
        # The exact string RerankStatements scored (ProcessorBase._format_statement_context).
        if not any(
            is_comparable(processor._format_statement_context("", topic.topic, s.statement_str), families)
            for s in topic.statements
        ):
            exempt_topics += 1
            return topic
        topic.statements = [s for s in topic.statements if s.score >= min_threshold]
        return topic

    def prune_search_result(index, search_result):
        return processor._apply_to_topics(search_result, prune_statements)

    pruned = processor._apply_to_search_results(search_results, prune_search_result)
    if exempt_topics:
        logger.debug(
            "lexical_cross_script_topics_kept",
            topics=exempt_topics,
            question_families=sorted(families),
            threshold=min_threshold,
        )
    return pruned
