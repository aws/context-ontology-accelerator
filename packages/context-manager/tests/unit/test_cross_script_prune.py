# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the cross-script ``PruneStatements`` patch (#1263).

These run the toolkit's real ``PopulateStatementStrs`` -> ``RerankStatements`` (tfidf)
-> ``PruneStatements`` chain on statements taken from the #1263 reproduction: English
statements about circular trading, plus Japanese statements about other rules that
share ``取引先`` / ``疑いがある`` with the longer question.
"""

from __future__ import annotations

import pytest
from coa_serve.lexical import cross_script_prune
from coa_serve.lexical.cross_script_prune import (
    comparable_families,
    is_comparable,
    patch_cross_script_statement_prune,
)
from graphrag_toolkit.lexical_graph.metadata import FilterConfig
from graphrag_toolkit.lexical_graph.retrieval.model import (
    EntityContexts,
    SearchResult,
    SearchResultCollection,
    Source,
    Statement,
    Topic,
    Versioning,
)
from graphrag_toolkit.lexical_graph.retrieval.processors import (
    PopulateStatementStrs,
    ProcessorArgs,
    PruneStatements,
    RerankStatements,
)
from llama_index.core.schema import QueryBundle

pytestmark = pytest.mark.unit

CIRCULAR = "Prohibition of Circular Trading"
FIXTURE = {
    "code-of-conduct.txt": {
        CIRCULAR: [
            "Circular trading is a type of transaction in which the same product is bought and sold in "
            "sequence among multiple companies without substantive basis, inflating sales figures.",
            "Employees must not engage in circular trading.",
            "Employees who become aware of suspicion that circular trading is being conducted must "
            "immediately stop the transaction and report to the Compliance Department on the same day.",
        ],
        "Prohibition on Push-in Sales and Tie-in Sales": [
            "push-in sales とは、取引先が必要としない量の商品を期末に納入し、売上を前倒しで計上する行為をいう。",
            "抱き合わせ販売とは、取引先に対して、ある商品の購入を条件として別の商品の購入を強制する行為をいう。",
        ],
        "Reception and Reporting Framework Regulations": [
            "この規範に反する行為を見つけた場合、またはこの規範に反する行為について疑いがある場合は、"
            "コンプライアンス部の相談窓口に報告する。",
            "相談窓口は匿名での報告も受け付ける。",
        ],
        "Gift Acceptance Limits and Procedures": [
            "取引先からの贈答品は、一件あたり五千円を上限として受け取ることができる。",
        ],
    },
    "information-management.txt": {
        "Information Leakage Reporting Procedures": [
            "Employees who discover an information leak must report it to the Information Security Office.",
            "The information management policy took effect in 2026.",
        ],
    },
}
ALL_STATEMENTS = [s for topics in FIXTURE.values() for sts in topics.values() for s in sts]
BARE = "循環取引"
WRAPPED = "取引先との間で循環取引が行われている疑いがあります"
QUESTION = "循環取引が行われている疑いがあるとき、どうすればよいですか？"
ACRONYM = "AWSの取引先との間で循環取引が行われている疑いがあります"


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    """Run every test against the toolkit's own PruneStatements first.

    Other test modules call the real ``_get_engine``, which installs the patch for the
    rest of the process, so restore the upstream method whatever ran before.
    """
    current = PruneStatements._process_results
    original = getattr(current, "__wrapped__", current)
    monkeypatch.setattr(PruneStatements, "_process_results", original)
    monkeypatch.setattr(cross_script_prune, "_patched", False)
    return original


@pytest.fixture
def patched(upstream):
    patch_cross_script_statement_prune()
    assert PruneStatements._process_results is not upstream
    return upstream


def _collection(data: dict, extras: list[str] | None = None) -> SearchResultCollection:
    results = []
    for i, (name, topics) in enumerate(data.items()):
        results.append(
            SearchResult(
                source=Source(sourceId=f"src-{i}", metadata={"name": name}, versioning=Versioning()),
                topics=[
                    Topic(
                        topicId=f"{i}-{t}",
                        topic=t,
                        chunks=[],
                        statements=[Statement(statementId=f"{i}-{t}-{j}", statement=s) for j, s in enumerate(sts)],
                    )
                    for t, sts in topics.items()
                ],
            )
        )
    return SearchResultCollection(results=results, entity_contexts=EntityContexts(keywords=extras or []))


def _run(question: str, data: dict | None = None, extras: list[str] | None = None, **arg_overrides):
    kwargs = {"reranker": "tfidf", "max_search_results": 10, "max_statements_per_topic": 10, **arg_overrides}
    args = ProcessorArgs(**kwargs)
    filter_config = FilterConfig()
    query = QueryBundle(query_str=question)
    results = _collection(data or FIXTURE, extras)
    for processor in (PopulateStatementStrs, RerankStatements, PruneStatements):
        results = processor(args, filter_config)._process_results(results, query)
    return results


def _statements(results: SearchResultCollection, topic: str | None = None) -> list[str]:
    return [
        statement.statement
        for result in results.results
        for t in result.topics
        if topic is None or t.topic == topic
        for statement in t.statements
    ]


class TestComparableFamilies:
    def test_japanese_question_compares_only_japanese(self):
        assert comparable_families(QUESTION) == {"han"}

    def test_english_question_compares_only_latin(self):
        assert comparable_families("what should I do about circular trading?") == {"latin"}

    def test_question_mostly_in_two_scripts_compares_both(self):
        assert comparable_families("what is 循環取引") == {"latin", "han"}

    def test_one_acronym_in_a_japanese_question_does_not_make_latin_comparable(self):
        assert comparable_families(ACRONYM) == {"han"}
        assert comparable_families("S3バケットの暗号化") == {"han"}

    def test_tfidf_punctuation_stripping_is_applied(self):
        # tfidf drops "." before building trigrams, so "u.s." has no Latin trigram.
        assert comparable_families("U.S.の規制について教えてください") == {"han"}
        assert comparable_families("U.S.") == set()

    def test_mixed_script_trigrams_count_for_no_family(self):
        # Every trigram here spans two scripts, so none says which script a match is in.
        assert comparable_families("aバbバcバ") == set()

    def test_fullwidth_latin_is_its_own_family(self):
        assert "latin" not in comparable_families("ＡＷＳの料金")
        assert not is_comparable("Billing: AWS charges", comparable_families("ＡＷＳＡＷＳ"))

    def test_entity_context_extras_count_in_full(self):
        assert comparable_families(QUESTION, ["circular trading"]) == {"han", "latin"}

    def test_letterless_question_has_no_family(self):
        assert comparable_families("2026 / 10 / 11") == set()


class TestUpstreamPruneReproducesTheBug:
    """Guards the fixture: without the patch, the toolkit drops the right statements.

    If a toolkit upgrade makes this fail, upstream changed PruneStatements; re-check
    whether ``cross_script_prune`` is still needed.
    """

    def test_bare_term_keeps_the_circular_trading_statements(self):
        assert len(_statements(_run(BARE), topic=CIRCULAR)) == 3

    @pytest.mark.parametrize("question", [WRAPPED, QUESTION, ACRONYM])
    def test_wrapped_term_loses_them(self, question):
        assert _statements(_run(question), topic=CIRCULAR) == []


class TestPatchedPrune:
    @pytest.mark.parametrize("question", [BARE, WRAPPED, QUESTION, ACRONYM])
    def test_keeps_the_circular_trading_statements(self, patched, question):
        results = _run(question)
        assert len(_statements(results, topic=CIRCULAR)) == 3
        # The English-only source survives too, instead of disappearing with its statements.
        assert len(results.results) == 2

    def test_still_prunes_same_script_statements_below_the_threshold(self, patched):
        kept = _statements(_run(WRAPPED))
        # Scores above 0 through 取引先 / 疑いがある, so it stays.
        assert any("取引先からの贈答品" in s for s in kept)
        # Japanese, so comparable, and it shares nothing with the question: pruned as before.
        assert "相談窓口は匿名での報告も受け付ける。" not in kept

    def test_a_japanese_topic_title_makes_its_english_statements_comparable(self, patched):
        data = {
            "doc": {
                "禁止事項": ["Employees must not engage in circular trading."],
                "贈答品": ["取引先からの贈答品は、一件あたり五千円を上限とする。"],
            }
        }
        # The reranker scored "禁止事項: Employees ...", which it could compare.
        assert _statements(_run(WRAPPED, data), topic="禁止事項") == []

    def test_a_mixed_topic_is_pruned_as_upstream(self, patched, upstream):
        data = {
            "doc": {
                "Mixed": ["Employees must not engage in circular trading.", "取引先からの贈答品は五千円まで。"],
                "Other": ["相談窓口は匿名での報告も受け付ける。"],
            }
        }
        patched_result = _statements(_run(WRAPPED, data))
        PruneStatements._process_results = upstream
        assert patched_result == _statements(_run(WRAPPED, data)) == ["取引先からの贈答品は五千円まで。"]

    def test_same_script_question_behaves_as_upstream(self, patched, upstream):
        question = "Who must employees report an information leak to?"
        patched_result = _statements(_run(question))
        PruneStatements._process_results = upstream
        assert patched_result == _statements(_run(question))
        # Upstream does prune here, so the comparison is not vacuous.
        assert len(patched_result) < len(ALL_STATEMENTS)

    def test_english_entity_context_extras_make_english_statements_comparable(self, patched, upstream):
        extras = ["suspicion report"]
        patched_result = _statements(_run(WRAPPED, extras=extras))
        PruneStatements._process_results = upstream
        assert patched_result == _statements(_run(WRAPPED, extras=extras))

    def test_no_pruning_configured_is_left_alone(self, patched):
        assert len(_statements(_run(WRAPPED, statement_pruning_factor=0))) == len(ALL_STATEMENTS)

    def test_absolute_threshold_still_applies_to_comparable_topics(self, patched):
        # No Japanese statement reaches 0.99, so every comparable topic is emptied; the
        # English-only topics are kept because the reranker could not compare them.
        kept = _statements(_run(WRAPPED, statement_pruning_factor=0, statement_pruning_threshold=0.99))
        assert sorted(kept) == sorted(s for s in ALL_STATEMENTS if s.isascii())

    def test_letterless_question_falls_back_to_upstream(self, patched, upstream):
        patched_result = _statements(_run("2026"))
        PruneStatements._process_results = upstream
        assert patched_result == _statements(_run("2026")) == ["The information management policy took effect in 2026."]

    def test_other_rerankers_fall_back_to_upstream(self, patched, upstream):
        args = ProcessorArgs(reranker="none", statement_pruning_factor=0.05)
        results = _collection(FIXTURE)
        for topic in (t for r in results.results for t in r.topics):
            for i, statement in enumerate(topic.statements):
                statement.score = 1.0 if i == 0 and topic.topic.startswith("Gift") else 0.0
        upstream_copy = results.model_copy(deep=True)
        query = QueryBundle(query_str=WRAPPED)
        patched_result = PruneStatements(args, FilterConfig())._process_results(results, query)
        expected = upstream(PruneStatements(args, FilterConfig()), upstream_copy, query)
        assert _statements(patched_result) == _statements(expected) == [ALL_STATEMENTS[7]]

    def test_an_error_inside_the_patch_falls_back_to_upstream(self, patched, upstream, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("toolkit moved")

        monkeypatch.setattr(cross_script_prune, "_prune", boom)
        patched_result = _statements(_run(WRAPPED))
        PruneStatements._process_results = upstream
        assert patched_result == _statements(_run(WRAPPED))


def test_patch_is_idempotent_and_wraps_upstream(patched, upstream):
    applied = PruneStatements._process_results
    patch_cross_script_statement_prune()
    assert PruneStatements._process_results is applied
    cross_script_prune._patched = False
    patch_cross_script_statement_prune()
    assert PruneStatements._process_results.__wrapped__ is upstream
