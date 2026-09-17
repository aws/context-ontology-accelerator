# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever AccumulatedContext store (task 5).

Covers ``coa_serve.tier3.agentic.context``:

- :meth:`AccumulatedContext.add` incorporates a ToolResult's chunks/entities
  before the next step (Req 4.2) and returns the count of genuinely NEW,
  de-duplicated retrieval results (Req 6.3) — so a step that surfaces only
  already-seen items returns ``0`` and drives the ``no_new_information`` stop.
- Duplicate chunks/entities (across calls and within one call) are never
  double-counted; arrival order is preserved.
- ``degraded_sources`` accumulates one record per failure (Req 9.1-9.3).
- ``ontology`` defaults to ``None`` and is settable (per-request cache, Req 3.8).

The chunk/entity stand-ins here are lightweight: ``add`` handles items
structurally (by attribute), so the tests need neither ``ChunkResult`` /
``GraphEntity`` nor any ``graphrag_toolkit`` access.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from coa_serve.tier3.agentic import AccumulatedContext
from coa_serve.tier3.agentic.context import (
    AccumulatedContext as DirectAccumulatedContext,
)
from coa_serve.tier3.agentic.tools.base import ToolResult


@dataclass(frozen=True)
class _Chunk:
    """Minimal stand-in matching the ``ChunkResult`` shape add() reads."""

    chunk_id: str = ""
    text: str = ""
    uri: str = ""


@dataclass(frozen=True)
class _Entity:
    """Minimal stand-in matching the ``GraphEntity`` shape add() reads."""

    uri: str = ""
    label: str = ""
    type: str = ""


@pytest.mark.unit
class TestReexport:
    def test_package_reexports_module_symbol_unchanged(self):
        assert AccumulatedContext is DirectAccumulatedContext


@pytest.mark.unit
class TestDefaults:
    def test_empty_defaults(self):
        ctx = AccumulatedContext()
        assert ctx.chunks == []
        assert ctx.entities == []
        assert ctx.ontology is None
        assert ctx.seen_keys == set()
        assert ctx.degraded_sources == []

    def test_independent_collection_defaults(self):
        """Each instance gets its own mutable collections (no shared state)."""
        a = AccumulatedContext()
        b = AccumulatedContext()
        assert a.chunks is not b.chunks
        assert a.entities is not b.entities
        assert a.seen_keys is not b.seen_keys
        assert a.degraded_sources is not b.degraded_sources

    def test_ontology_is_settable(self):
        """The per-request ontology cache (Req 3.8) is a plain settable field."""
        ctx = AccumulatedContext()
        sentinel = object()
        ctx.ontology = sentinel  # type: ignore[assignment]
        assert ctx.ontology is sentinel


@pytest.mark.unit
class TestAddNewCount:
    def test_add_returns_count_of_new_chunks_and_entities(self):
        ctx = AccumulatedContext()
        result = ToolResult(
            chunks=(_Chunk(chunk_id="c1"), _Chunk(chunk_id="c2")),
            entities=(_Entity(uri="urn:e1"),),
        )
        assert ctx.add(result) == 3
        assert len(ctx.chunks) == 2
        assert len(ctx.entities) == 1

    def test_add_preserves_arrival_order(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"), _Chunk(chunk_id="c2"))))
        ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c3"),)))
        assert [c.chunk_id for c in ctx.chunks] == ["c1", "c2", "c3"]

    def test_empty_result_adds_nothing(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult()) == 0
        assert ctx.chunks == []
        assert ctx.entities == []


@pytest.mark.unit
class TestDeduplication:
    """Duplicate items are not double-counted — this drives the
    ``no_new_information`` stop (Req 6.3)."""

    def test_duplicate_chunk_across_calls_not_counted_again(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"),))) == 1
        # Same chunk id again -> no new information.
        assert ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"),))) == 0
        assert len(ctx.chunks) == 1

    def test_duplicate_entity_across_calls_not_counted_again(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(entities=(_Entity(uri="urn:e1"),))) == 1
        assert ctx.add(ToolResult(entities=(_Entity(uri="urn:e1"),))) == 0
        assert len(ctx.entities) == 1

    def test_duplicate_within_single_call_counted_once(self):
        ctx = AccumulatedContext()
        result = ToolResult(
            chunks=(_Chunk(chunk_id="c1"), _Chunk(chunk_id="c1")),
        )
        assert ctx.add(result) == 1
        assert len(ctx.chunks) == 1

    def test_partial_overlap_counts_only_new_items(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"),)))
        # c1 seen, c2 new -> exactly one new item.
        new = ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"), _Chunk(chunk_id="c2"))))
        assert new == 1
        assert [c.chunk_id for c in ctx.chunks] == ["c1", "c2"]

    def test_chunk_without_id_dedupes_by_text(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(chunks=(_Chunk(text="same body"),))) == 1
        assert ctx.add(ToolResult(chunks=(_Chunk(text="same body"),))) == 0
        assert len(ctx.chunks) == 1

    def test_chunk_dedupes_by_uri_when_no_id(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(chunks=(_Chunk(uri="urn:doc#1", text="a"),))) == 1
        # Same uri, different text -> still the same chunk, not new.
        assert ctx.add(ToolResult(chunks=(_Chunk(uri="urn:doc#1", text="b"),))) == 0

    def test_entity_dedupes_by_label_and_type_when_no_uri(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(entities=(_Entity(label="Amazon", type="Company"),))) == 1
        assert ctx.add(ToolResult(entities=(_Entity(label="Amazon", type="Company"),))) == 0
        # A different type is a distinct entity.
        assert ctx.add(ToolResult(entities=(_Entity(label="Amazon", type="Brand"),))) == 1

    def test_chunk_and_entity_sharing_a_uri_do_not_collide(self):
        """Chunk and entity keys are namespaced, so a chunk and an entity that
        happen to share a URI are both counted."""
        ctx = AccumulatedContext()
        new = ctx.add(
            ToolResult(
                chunks=(_Chunk(uri="urn:shared"),),
                entities=(_Entity(uri="urn:shared"),),
            )
        )
        assert new == 2
        assert len(ctx.chunks) == 1
        assert len(ctx.entities) == 1


@pytest.mark.unit
class TestRowsChannel:
    """Structured rows (NL→SQL / metric tools) accumulate and count toward progress
    like chunks/entities — the fix for a successful SQL tool being mis-scored as
    no-progress because its output arrives on ``rows``, not ``chunks``."""

    def test_rows_count_as_new_progress(self):
        ctx = AccumulatedContext()
        result = ToolResult(rows=({"revenue": 100}, {"revenue": 200}))
        assert ctx.add(result) == 2
        assert ctx.rows == [{"revenue": 100}, {"revenue": 200}]

    def test_duplicate_row_across_calls_not_counted_again(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(rows=({"a": 1},))) == 1
        # Same row again -> no new information (drives the no_new_information stop).
        assert ctx.add(ToolResult(rows=({"a": 1},))) == 0
        assert len(ctx.rows) == 1

    def test_duplicate_row_within_single_call_counted_once(self):
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(rows=({"a": 1}, {"a": 1}))) == 1
        assert len(ctx.rows) == 1

    def test_row_key_is_order_insensitive_on_dict_keys(self):
        """Two rows with the same key/value pairs in different insertion order are
        the same row (canonical sorted-key serialization)."""
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(rows=({"a": 1, "b": 2},))) == 1
        assert ctx.add(ToolResult(rows=({"b": 2, "a": 1},))) == 0

    def test_rows_mix_with_chunks_in_one_result(self):
        ctx = AccumulatedContext()
        result = ToolResult(chunks=(_Chunk(chunk_id="c1"),), rows=({"x": 1},))
        assert ctx.add(result) == 2
        assert len(ctx.chunks) == 1
        assert len(ctx.rows) == 1

    def test_row_with_non_json_value_does_not_raise(self):
        """Key derivation falls back to str() for non-JSON values."""
        ctx = AccumulatedContext()
        assert ctx.add(ToolResult(rows=({"when": object()},))) == 1

    def test_rows_default_is_independent_per_instance(self):
        a = AccumulatedContext()
        b = AccumulatedContext()
        assert a.rows == [] and b.rows == []
        assert a.rows is not b.rows


@pytest.mark.unit
class TestForkAndMerge:
    """P2 parallel sub-questions: fork gives a private sub-context; merge folds it
    back with the same dedup as sequential accumulation."""

    def test_fork_shares_ontology_but_not_mutable_state(self):
        parent = AccumulatedContext()
        onto = object()
        parent.ontology = onto  # type: ignore[assignment]
        parent.add(ToolResult(chunks=(_Chunk(chunk_id="p1"),)))

        child = parent.fork()

        # Shares the resolved ontology by reference (read-only during the loop)...
        assert child.ontology is onto
        # ...but starts empty with its own mutable collections (private seen_keys).
        assert child.chunks == []
        assert child.seen_keys == set()
        assert child.seen_keys is not parent.seen_keys
        assert child.chunks is not parent.chunks

    def test_private_seen_keys_prevents_false_no_progress(self):
        """The core P2 invariant: a sibling retrieving a chunk the PARENT already
        saw still counts it as new IN ITS OWN fork (own seen_keys), so its
        no-progress signal is judged against its own retrievals, not a sibling's."""
        parent = AccumulatedContext()
        parent.add(ToolResult(chunks=(_Chunk(chunk_id="shared"),)))

        child = parent.fork()
        # In the fork, 'shared' is unseen → counts as new (no false no-progress).
        assert child.add(ToolResult(chunks=(_Chunk(chunk_id="shared"),))) == 1

    def test_merge_dedups_against_parent_and_prior_siblings(self):
        parent = AccumulatedContext()
        a = parent.fork()
        a.add(ToolResult(chunks=(_Chunk(chunk_id="x"), _Chunk(chunk_id="y"))))
        b = parent.fork()
        b.add(ToolResult(chunks=(_Chunk(chunk_id="y"), _Chunk(chunk_id="z"))))  # y overlaps a

        assert parent.merge(a) == 2  # x, y
        assert parent.merge(b) == 1  # only z (y already merged from a)
        assert [c.chunk_id for c in parent.chunks] == ["x", "y", "z"]

    def test_merge_carries_and_dedups_rows(self):
        parent = AccumulatedContext()
        parent.add(ToolResult(rows=({"a": 1},)))
        child = parent.fork()
        child.add(ToolResult(rows=({"a": 1}, {"b": 2})))  # {"a":1} overlaps parent

        # Only the genuinely new row folds back (parent already has {"a":1}).
        assert parent.merge(child) == 1
        assert parent.rows == [{"a": 1}, {"b": 2}]

    def test_merge_concatenates_attempts_and_degraded(self):
        parent = AccumulatedContext()
        child = parent.fork()
        child.record_attempt("strategy:entity_based", {"query": "q"}, 2, "succeeded")
        child.record_degraded("graph_traversal", "timeout")

        parent.merge(child)

        assert len(parent.attempt_history) == 1
        assert parent.degraded_sources == [{"source": "graph_traversal", "reason": "timeout"}]

    def test_merge_combines_working_answers_by_question(self):
        parent = AccumulatedContext()
        a = parent.fork()
        a.set_working_answer("Q1", "answer one")
        b = parent.fork()
        b.set_working_answer("Q2", "answer two")

        parent.merge(a)
        parent.merge(b)

        questions = {e["question"] for e in parent.working_answers}
        assert questions == {"Q1", "Q2"}


@pytest.mark.unit
class TestDegradedSources:
    """Degraded sources accumulate across failures (Req 9.1-9.3, 9.5)."""

    def test_record_degraded_accumulates(self):
        ctx = AccumulatedContext()
        ctx.record_degraded("strategy:entity_based", "timeout")
        ctx.record_degraded("graph_traversal", "error")
        assert ctx.degraded_sources == [
            {"source": "strategy:entity_based", "reason": "timeout"},
            {"source": "graph_traversal", "reason": "error"},
        ]

    def test_repeated_failures_each_recorded(self):
        """One record per failure — repeated failures of the same Tool are not
        collapsed."""
        ctx = AccumulatedContext()
        ctx.record_degraded("vector_search", "timeout")
        ctx.record_degraded("vector_search", "timeout")
        assert len(ctx.degraded_sources) == 2

    def test_direct_append_also_accumulates(self):
        """The controller appends degraded records directly; that path
        accumulates too."""
        ctx = AccumulatedContext()
        ctx.degraded_sources.append({"source": "graph_traversal", "reason": "error"})
        ctx.degraded_sources.append({"source": "vector_search", "reason": "timeout"})
        assert len(ctx.degraded_sources) == 2

    def test_add_does_not_touch_degraded_sources(self):
        """``add`` only incorporates retrieval results; degraded tracking is a
        separate concern owned by the controller."""
        ctx = AccumulatedContext()
        ctx.add(ToolResult(chunks=(_Chunk(chunk_id="c1"),)))
        assert ctx.degraded_sources == []


@dataclass(frozen=True)
class _ScoredChunk:
    """Chunk stand-in carrying a relevance score (for idea-8 ranking tests)."""

    chunk_id: str = ""
    text: str = ""
    relevance_score: float = 0.0
    uri: str = ""


@pytest.mark.unit
class TestRelevanceRankedRefs:
    """Idea 8: relevance-ranked carried-context views (vs the recency views)."""

    def _ctx(self) -> AccumulatedContext:
        ctx = AccumulatedContext()
        # Arrival order: A(0.2), B(0.9), C(0.5), D(0.1 — newest).
        for c in (
            _ScoredChunk("A", "alpha", 0.2),
            _ScoredChunk("B", "bravo", 0.9),
            _ScoredChunk("C", "charlie", 0.5),
            _ScoredChunk("D", "delta", 0.1),
        ):
            ctx.add(ToolResult(chunks=(c,)))
        return ctx

    def test_recent_refs_are_by_arrival_order(self):
        ctx = self._ctx()
        ids = [cid for cid, _ in ctx.recent_chunk_refs(limit=2)]
        assert ids == ["C", "D"]  # last two added

    def test_relevant_refs_rank_by_score_with_guaranteed_newest(self):
        ctx = self._ctx()
        ids = [cid for cid, _ in ctx.relevant_chunk_refs(limit=2)]
        # Newest (D) guaranteed + highest score (B); order is newest-first then ranked.
        assert ids == ["D", "B"]

    def test_relevant_refs_fill_by_descending_score(self):
        ctx = self._ctx()
        ids = [cid for cid, _ in ctx.relevant_chunk_refs(limit=3)]
        assert ids == ["D", "B", "C"]  # newest, then 0.9, then 0.5

    def test_relevant_snippets_return_text(self):
        ctx = self._ctx()
        snips = ctx.relevant_chunk_snippets(limit=2)
        assert snips == ["delta", "bravo"]

    def test_relevant_refs_empty_when_no_chunks(self):
        assert AccumulatedContext().relevant_chunk_refs(limit=3) == []


@pytest.mark.unit
class TestRecentRowSnippets:
    """Structured rows must be renderable for the planner's content view — the
    blind spot where a rows-only answer was assessed as "no data gathered"."""

    def test_renders_rows_bounded_and_spread(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(rows=tuple({"d": f"2026-06-0{i}", "v": i} for i in range(1, 8))))
        snips = ctx.recent_row_snippets(limit=3)
        assert len(snips) == 3
        # Evenly-spaced sample spans the WHOLE result (first and last included),
        # so an ordered multi-day table doesn't read as a single-date slice.
        assert "2026-06-01" in snips[0]
        assert "2026-06-07" in snips[-1]

    def test_small_result_returned_whole_in_order(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(rows=({"d": "a"}, {"d": "b"})))
        snips = ctx.recent_row_snippets(limit=5)
        assert len(snips) == 2 and "'a'" in snips[0] and "'b'" in snips[1]

    def test_cell_values_clipped_to_width(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(rows=({"k": "x" * 500},)))
        (snip,) = ctx.recent_row_snippets(width=50)
        assert "x" * 50 in snip and "x" * 51 not in snip

    def test_non_mapping_rows_skipped(self):
        ctx = AccumulatedContext()
        ctx.rows.append("not-a-dict")
        assert ctx.recent_row_snippets() == []
