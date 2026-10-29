# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever session orchestration (task 13).

Covers ``coa_serve.tier3.agentic.retriever`` with a mocked
controller, decomposer, synthesizer, and an injected (manually advanced) budget
clock so the whole session is deterministic — no Bedrock, no tools, no graph, no
sleeping on the wall clock:

- Same result contract as ``KnowledgeRetriever`` (Req 1.4): a ``Tier3Result``
  whose ``synthesized_answer``/``supporting_content``/``graph_context``/
  ``confidence``/``guardrail_blocked``/``degraded_sources`` are all present.
- Aggregate budget stop → partial (Req 7.4-7.7): the wall-clock Time_Budget
  expiring mid-session ends the loop with unprocessed work, the terminal stop
  reason is ``time_budget_expired``, and the result is flagged partial.
- Queue-drained stop (Req 6.6 / session): the queue draining records a
  ``queue_drained`` terminal stop.
- Synthesis failure → partial, empty answer, confidence 0.0, traced (Req 8.6).
- Guardrail block → ``guardrail_blocked`` true with the blocked content excluded
  (Req 8.3-8.4).
- All-tools-fail with no context → partial, no Best_Effort_Answer, no source
  succeeded, synthesis skipped (Req 9.7); with context → partial best-effort
  (Req 9.6).
- ``supporting_content`` / ``graph_context`` populated from the accumulated
  context, empty when none (Req 8.2).
- Namespace scoping: every Sub_Question / branch loop is driven with the request
  Namespace (Req 1.5).
- The lazy-import invariant (Req 12.3).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.models import StopReason, SubQuestion
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.graph_traverser import GraphEntity
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

NS = "acme"


# ── deterministic test doubles ─────────────────────────────────────


class FakeClockSource:
    """A manually advanced monotonic time source (seconds) for the BudgetClock."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeDecomposer:
    """A mocked DecompositionPlanner returning scripted Sub_Questions."""

    def __init__(self, sub_questions: list[SubQuestion]) -> None:
        self._sub_questions = sub_questions
        self.calls: list[str] = []

    async def decompose(self, query, trace=None):
        self.calls.append(query)
        if trace is not None:
            trace.record("decompose", "success", 0, detail=f"{len(self._sub_questions)} sub-questions")
        return list(self._sub_questions)


class FakeController:
    """A mocked ReasoningController whose ``run_one_loop`` runs a scripted effect.

    Records the namespace and sub-question of every loop. ``on_loop`` (when given)
    is invoked with ``(ctx, clock, queue)`` so a test can populate the shared
    context, advance the injected clock past the budget, or enqueue branches.
    ``stops`` is consumed one StopReason per call (default ``None``).
    """

    def __init__(self, *, on_loop=None, stops: list[StopReason | None] | None = None) -> None:
        self._on_loop = on_loop
        self._stops = deque(stops or [])
        self.namespaces: list[str] = []
        self.sub_questions: list[SubQuestion] = []
        self.calls = 0

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **_):
        self.calls += 1
        self.namespaces.append(namespace)
        self.sub_questions.append(sub_question)
        if self._on_loop is not None:
            self._on_loop(ctx, clock, queue)
        return self._stops.popleft() if self._stops else None


class FakeSynthesizer:
    """A mocked Synthesizer; ``synthesize`` returns a scripted result or raises."""

    def __init__(self, *, result: SynthesisResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def synthesize(
        self,
        query,
        chunks,
        entities,
        *,
        structured_data=None,
        catalog_summary=None,
        conversation_history=None,
        working_answer=None,
    ):
        self.calls.append({"query": query, "chunks": list(chunks), "entities": list(entities)})
        if self._raises is not None:
            raise self._raises
        return self._result if self._result is not None else SynthesisResult(answer="ok", confidence=0.8, duration_ms=5)


def make_retriever(
    *,
    decomposer: FakeDecomposer,
    controller: FakeController,
    synthesizer: FakeSynthesizer,
    clock_source: FakeClockSource,
    budget: AgenticBudgetConfig | None = None,
) -> AgenticRetriever:
    return AgenticRetriever(
        registry=object(),  # the fake controller never touches the registry
        controller=controller,
        decomposer=decomposer,
        synthesizer=synthesizer,
        budget=budget or AgenticBudgetConfig(),
        clock_source=clock_source,
        # These tests isolate the session orchestration (decompose → loop →
        # synthesize) with a fake controller and a stub registry; the guaranteed
        # final semantic search needs a real registry and is covered by its own
        # tests (TestFinalSemanticSearch), so disable it here.
        final_semantic_search=False,
    )


def make_chunk(chunk_id: str) -> ChunkResult:
    return ChunkResult(
        chunk_id=chunk_id,
        text=f"text-{chunk_id}",
        source_doc=f"aws:tenant:source:{chunk_id}",
        source_doc_name="doc.pdf",
        relevance_score=0.9,
        label="L",
    )


def make_entity(uri: str) -> GraphEntity:
    return GraphEntity(
        uri=uri, label="Amazon", type="Company", relationships=({"predicate": "p", "target_label": "t"},)
    )


def stop_steps(trace: TraceCollector) -> list:
    return [s for s in trace.steps if s.step == "agentic_retrieval_stop"]


def synth_steps(trace: TraceCollector) -> list:
    return [s for s in trace.steps if s.step == "llm_synthesize"]


# ── guaranteed final semantic search (graph exploration + semantic) ─


class _FinalTool:
    """Minimal Tool implementing the four-field contract, for final-search tests."""

    def __init__(self, name: str, result=None, *, raises: Exception | None = None) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.input_schema = {"query": "str", "embedding": "list[float]"}
        self.output_schema = {}
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def invoke(self, *, namespace: str, **kwargs):
        self.calls.append({"namespace": namespace, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._result


def _final_result(chunk_id: str = "final-sem"):
    from coa_serve.tier3.agentic.tools.base import ToolResult

    chunk = make_chunk(chunk_id)
    return ToolResult(
        chunks=(chunk,), items=({"type": "chunk", "chunk_id": chunk_id},), detail="chunk_based_semantic: 1 chunk"
    )


def _registry(*tools: _FinalTool):
    from coa_serve.tier3.agentic.tools import ToolRegistry

    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


@pytest.mark.unit
class TestFinalSemanticSearch:
    """Every session ends with a standard semantic search of the original query.

    This is the guaranteed baseline that complements the planner-driven, ontology-
    guided GRAPH EXPLORATION: it runs once, AFTER the reasoning loop, before
    synthesis, so the answer is grounded in both graph and semantic retrieval.
    """

    def _retriever(self, registry, *, controller=None, synthesizer=None):
        return AgenticRetriever(
            registry=registry,
            controller=controller or FakeController(stops=[StopReason.queue_drained]),
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            synthesizer=synthesizer
            or FakeSynthesizer(result=SynthesisResult(answer="ans", confidence=0.8, duration_ms=5)),
            budget=AgenticBudgetConfig(),
            clock_source=FakeClockSource(),
            final_semantic_search=True,
        )

    async def test_runs_chunk_based_semantic_on_the_original_query_after_the_loop(self):
        sem = _FinalTool("strategy:chunk_based_semantic", _final_result())
        retriever = self._retriever(_registry(sem))
        trace = TraceCollector()

        result = await retriever.resolve("What are NVIDIA's market segments?", NS, trace=trace)

        # Invoked exactly once, scoped to the namespace, on the ORIGINAL query.
        assert len(sem.calls) == 1
        assert sem.calls[0]["namespace"] == NS
        assert sem.calls[0]["query"] == "What are NVIDIA's market segments?"
        # Recorded as the final step, AFTER the loop's terminal stop, before synth.
        names = [s.step for s in trace.steps]
        assert "agentic_final_semantic_search" in names
        assert names.index("agentic_retrieval_stop") < names.index("agentic_final_semantic_search")
        assert names.index("agentic_final_semantic_search") < names.index("llm_synthesize")
        # Its chunk flowed into the synthesized result's supporting content.
        assert any(c["chunkId"] == "final-sem" for c in result.supporting_content)

    async def test_falls_back_to_vector_tool_when_no_chunk_strategy(self):
        vec = _FinalTool("vector_search", _final_result("vec-sem"))
        retriever = self._retriever(_registry(vec))

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert len(vec.calls) == 1 and vec.calls[0]["query"] == "q"
        assert any(c["chunkId"] == "vec-sem" for c in result.supporting_content)

    async def test_failure_degrades_and_does_not_raise(self):
        sem = _FinalTool("strategy:chunk_based_semantic", None, raises=RuntimeError("boom"))
        retriever = self._retriever(_registry(sem))
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        assert {"source": "strategy:chunk_based_semantic", "reason": "error"} in list(result.degraded_sources)
        final_step = next(s for s in trace.steps if s.step == "agentic_final_semantic_search")
        assert final_step.status == "error"
        assert result.partial is True  # degraded source flags partial

    async def test_returned_final_search_degradation_marks_partial_and_trace(self):
        degraded_result = _final_result()
        degraded_result = type(degraded_result)(
            chunks=degraded_result.chunks,
            items=degraded_result.items,
            detail="1 chunk retained; dropped 2 anonymous chunks",
            degraded_reason="missing_document_provenance",
        )
        sem = _FinalTool("strategy:chunk_based_semantic", degraded_result)
        retriever = self._retriever(_registry(sem))
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        assert result.partial is True
        assert result.degraded_sources == (
            {
                "source": "strategy:chunk_based_semantic",
                "reason": "missing_document_provenance",
            },
        )
        assert result.supporting_content[0]["chunkId"] == "final-sem"
        final_step = next(step for step in trace.steps if step.step == "agentic_final_semantic_search")
        assert final_step.status == "degraded"

    async def test_skipped_cleanly_when_no_semantic_tool_registered(self):
        retriever = self._retriever(_registry())  # empty registry
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        final_step = next(s for s in trace.steps if s.step == "agentic_final_semantic_search")
        assert final_step.status == "skipped"
        # The session still synthesizes (no crash) even with no semantic tool.
        assert result.synthesized_answer == "ans"

    async def test_launched_before_loop_so_budget_expiry_does_not_waste_it(self):
        """P1: the final search is LAUNCHED before the loop and its I/O overlaps it,
        so a loop that later expires the budget does NOT discard it — its result is
        still incorporated (the whole point of overlapping). This supersedes the
        pre-P1 behavior where an expired session skipped the final search."""
        sem = _FinalTool("strategy:chunk_based_semantic", _final_result())
        src = FakeClockSource()
        # A controller loop that advances the injected clock past the time budget.
        retriever = AgenticRetriever(
            registry=_registry(sem),
            controller=FakeController(
                on_loop=lambda ctx, clock, queue: src.advance(31),
                stops=[StopReason.time_budget_expired],
            ),
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            synthesizer=FakeSynthesizer(result=SynthesisResult(answer="ans", confidence=0.8, duration_ms=5)),
            budget=AgenticBudgetConfig(time_budget_s=30),
            clock_source=src,
            final_semantic_search=True,
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        # Launched before the loop → invoked exactly once, and its chunk still
        # reaches the synthesized result even though the loop expired the budget.
        assert len(sem.calls) == 1
        final_step = next(s for s in trace.steps if s.step == "agentic_final_semantic_search")
        assert final_step.status == "succeeded"
        assert any(c["chunkId"] == "final-sem" for c in result.supporting_content)
        # Session still synthesizes and is flagged partial (budget expiry).
        assert result.synthesized_answer == "ans"
        assert result.partial is True

    async def test_disabled_flag_skips_the_final_search_entirely(self):
        sem = _FinalTool("strategy:chunk_based_semantic", _final_result())
        retriever = AgenticRetriever(
            registry=_registry(sem),
            controller=FakeController(stops=[StopReason.queue_drained]),
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            synthesizer=FakeSynthesizer(result=SynthesisResult(answer="ans", confidence=0.8, duration_ms=5)),
            budget=AgenticBudgetConfig(),
            clock_source=FakeClockSource(),
            final_semantic_search=False,
        )
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)

        assert sem.calls == []
        assert "agentic_final_semantic_search" not in [s.step for s in trace.steps]


# ── result contract (Req 1.4) ──────────────────────────────────────


@pytest.mark.unit
class TestSupportingContentProvenanceSignal:
    """Filtering provenance-less chunks must be observable, like the other paths.

    The hand-rolled and lexical Tier-3 paths both count the chunks they filter, mark
    the step degraded and add a ``degradedSources`` entry. This path filtered
    silently, so a chunk that should have carried a document id but did not simply
    disappeared from the citation list.
    """

    @staticmethod
    def _resolve(chunks):
        def populate(ctx, clock, queue):
            ctx.chunks.extend(chunks)
            ctx.entities.append(make_entity("urn:amazon"))

        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied]),
            synthesizer=FakeSynthesizer(result=SynthesisResult(answer="An answer", confidence=0.8, duration_ms=5)),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()
        return retriever, trace

    async def test_graph_only_statement_chunks_are_reported_but_not_degraded(self):
        # A graph statement legitimately has no document to cite and IS represented
        # in graphContext, so it is counted and traced without degrading the answer.
        graph_chunk = ChunkResult(
            chunk_id="urn:amazon#stmt-1",
            text="Amazon competes with Walmart.",
            source_doc="",
            relevance_score=1.0,
            uri="urn:amazon",
        )
        retriever, trace = self._resolve([make_chunk("c1"), graph_chunk])

        result = await retriever.resolve("q", NS, trace=trace)

        assert [item["chunkId"] for item in result.supporting_content] == ["c1"]
        step = next(s for s in trace.steps if s.step == "agentic_supporting_content")
        assert step.status == "succeeded"
        assert "1 graph-only statement chunk(s)" in step.detail
        assert result.degraded_sources == ()
        assert result.partial is False

    async def test_chunks_with_neither_document_nor_graph_provenance_degrade(self):
        anonymous = ChunkResult(
            chunk_id="orphan-1",
            text="Unattributable evidence.",
            source_doc="",
            relevance_score=0.5,
        )
        retriever, trace = self._resolve([make_chunk("c1"), anonymous])

        result = await retriever.resolve("q", NS, trace=trace)

        assert [item["chunkId"] for item in result.supporting_content] == ["c1"]
        step = next(s for s in trace.steps if s.step == "agentic_supporting_content")
        assert step.status == "degraded"
        assert "1 chunk(s) dropped with neither document nor graph provenance" in step.detail
        assert [entry["reason"] for entry in result.degraded_sources] == ["missing_document_provenance"]
        assert result.partial is True

    async def test_no_step_recorded_when_every_chunk_has_document_provenance(self):
        retriever, trace = self._resolve([make_chunk("c1"), make_chunk("c2")])

        result = await retriever.resolve("q", NS, trace=trace)

        assert len(result.supporting_content) == 2
        assert "agentic_supporting_content" not in [s.step for s in trace.steps]
        assert result.degraded_sources == ()


@pytest.mark.unit
class TestResultContract:
    async def test_happy_path_returns_full_tier3_result(self):
        def populate(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))
            ctx.entities.append(make_entity("urn:amazon"))

        controller = FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="An answer", confidence=0.77, duration_ms=12))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        result = await retriever.resolve("the question", NS, trace=trace)

        assert result.synthesized_answer == "An answer"
        assert 0.0 <= result.confidence <= 1.0 and result.confidence == 0.77
        assert result.guardrail_blocked is False
        assert result.partial is False  # context gathered, no degraded, no expiry
        # supporting_content / graph_context populated from accumulated context.
        assert result.supporting_content[0] == {
            "chunkId": "c1",
            "text": "text-c1",
            "sourceDocumentId": "aws:tenant:source:c1",
            "sourceDocumentName": "doc.pdf",
            "relevanceScore": 0.9,
            # Deprecated aliases kept for one release (see #984).
            "sourceDoc": "aws:tenant:source:c1",
            "label": "L",
        }
        assert result.graph_context[0]["uri"] == "urn:amazon"
        assert isinstance(result.degraded_sources, tuple)
        # trace flows through and is attached.
        assert any(s.step == "agentic_session_start" for s in trace.steps)
        assert len(synthesizer.calls) == 1

    async def test_relationship_working_set_cap_marks_result_partial(self):
        relationships = tuple(
            {
                "source_uri": "urn:amazon",
                "predicate_uri": f"urn:predicate:{index}",
                "target_uri": f"urn:target:{index}",
            }
            for index in range(11)
        )

        def populate(ctx, clock, queue):
            ctx.add(
                ToolResult(
                    entities=(
                        GraphEntity(
                            uri="urn:amazon",
                            label="Amazon",
                            type="Company",
                            relationships=relationships,
                        ),
                    )
                )
            )

        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied]),
            synthesizer=FakeSynthesizer(result=SynthesisResult(answer="An answer", confidence=0.7, duration_ms=4)),
            clock_source=FakeClockSource(),
        )

        result = await retriever.resolve("the question", NS)

        assert len(result.graph_context[0]["relationships"]) == 10
        assert result.degraded_sources == ({"source": "accumulated_context", "reason": "relationship_cap_reached"},)
        assert result.partial is True

    async def test_no_context_yields_empty_supporting_and_graph(self):
        # Nothing gathered, queue drains; synthesis still runs (Req 7.8 shape).
        controller = FakeController(stops=[None])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="best effort", confidence=0.3, duration_ms=3))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        result = await retriever.resolve("q", NS)

        assert result.supporting_content == ()
        assert result.graph_context == ()
        # No context gathered → partial best-effort answer (Req 7.8).
        assert result.partial is True
        assert result.synthesized_answer == "best effort"


# ── aggregate budget stop → partial (Req 7.4-7.7) ──────────────────


@pytest.mark.unit
class TestAggregateBudgetStop:
    async def test_time_budget_expiry_ends_loop_and_flags_partial(self):
        src = FakeClockSource()

        def expire_after_first(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))  # one loop did contribute context
            src.advance(99)  # blow the 30s budget mid-session

        controller = FakeController(on_loop=expire_after_first, stops=[StopReason.sufficiency_satisfied])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="partial", confidence=0.5, duration_ms=4))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q1"), SubQuestion(text="q2"), SubQuestion(text="q3")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=src,
            budget=AgenticBudgetConfig(time_budget_s=30),
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        # Only the first queued Sub_Question ran before the budget expired.
        assert controller.calls == 1
        assert result.partial is True
        steps = stop_steps(trace)
        assert len(steps) == 1
        assert str(StopReason.time_budget_expired) in str(steps[0].detail)

    async def test_max_steps_consumed_reports_max_steps_reached(self):
        src = FakeClockSource()

        def consume_all_steps(ctx, clock, queue):
            # Drain the aggregate step cap exactly as the real controller would.
            while clock.try_consume_step():
                pass
            ctx.chunks.append(make_chunk("c1"))

        controller = FakeController(on_loop=consume_all_steps, stops=[StopReason.max_steps_reached])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="a", confidence=0.6, duration_ms=2))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q1")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=src,
            budget=AgenticBudgetConfig(time_budget_s=300, max_steps=3),
        )
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)
        assert str(StopReason.max_steps_reached) in str(stop_steps(trace)[0].detail)


# ── queue-drained stop ─────────────────────────────────────────────


@pytest.mark.unit
class TestQueueDrainedStop:
    async def test_queue_drained_records_terminal_stop(self):
        # Controller returns None for each Sub_Question (nothing more selectable),
        # the queue drains without hitting a budget cap → queue_drained.
        controller = FakeController(stops=[None, None])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="done", confidence=0.7, duration_ms=2))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="a"), SubQuestion(text="b")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)

        assert controller.calls == 2  # both Sub_Questions processed
        assert str(StopReason.queue_drained) in str(stop_steps(trace)[0].detail)


# ── parallel sub-questions (P2) ────────────────────────────────────


@pytest.mark.unit
class TestParallelSubQuestions:
    """P2: independent Sub_Questions run in forked sub-contexts, merged back with
    dedup, under one shared aggregate BudgetClock."""

    async def test_forked_results_merge_deduped_into_final_context(self):
        # Each Sub_Question's loop writes to ITS OWN forked ctx; overlapping chunk
        # 'shared' must appear once in the merged result (dedup on merge).
        chunks_by_sq = {"a": ["a1", "shared"], "b": ["shared", "b1"]}

        def per_subq(ctx, clock, queue):
            # ``ctx`` here is the fork for the current Sub_Question. Which one?
            # Infer from call order via the controller's recorded sub_questions.
            sq_text = controller.sub_questions[-1].text
            for cid in chunks_by_sq.get(sq_text, ()):
                ctx.add(_final_result(cid))

        controller = FakeController(on_loop=per_subq, stops=[None, None])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="done", confidence=0.7, duration_ms=2))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="a"), SubQuestion(text="b")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert controller.calls == 2
        ids = sorted(c["chunkId"] for c in result.supporting_content)
        assert ids == ["a1", "b1", "shared"]  # 'shared' deduped to one

    async def test_shared_budget_step_cap_is_aggregate_across_subquestions(self):
        # A shared step cap of 1: the first Sub_Question consumes it; the second
        # gets no step. Proves the BudgetClock is shared (not per-fork).
        def consume_one_step(ctx, clock, queue):
            clock.try_consume_step()

        controller = FakeController(on_loop=consume_one_step, stops=[None, None])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="x", confidence=0.5, duration_ms=1))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="a"), SubQuestion(text="b")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
            budget=AgenticBudgetConfig(time_budget_s=300, max_steps=1),
        )
        clock_seen: list[int] = []
        orig = controller.run_one_loop

        async def spy(sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
            clock_seen.append(id(clock))
            return await orig(sub_question, ctx, registry, clock, queue, trace, namespace=namespace, **kw)

        controller.run_one_loop = spy  # type: ignore[assignment]

        await retriever.resolve("q", NS, trace=TraceCollector())

        # Both Sub_Questions received the SAME clock instance (shared aggregate).
        assert len(clock_seen) == 2
        assert clock_seen[0] == clock_seen[1]


# ── synthesis failure (Req 8.6) ────────────────────────────────────


@pytest.mark.unit
class TestSynthesisFailure:
    async def test_synthesizer_error_yields_partial_empty_answer_conf_zero(self):
        def populate(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))

        controller = FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied])
        synthesizer = FakeSynthesizer(raises=RuntimeError("bedrock exploded"))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        assert result.synthesized_answer == ""
        assert result.confidence == 0.0
        assert result.partial is True
        # supporting content is still surfaced even though synthesis failed.
        assert result.supporting_content[0]["chunkId"] == "c1"
        errors = [s for s in synth_steps(trace) if s.status == "error"]
        assert len(errors) == 1

    async def test_synthesizer_returns_no_answer_treated_as_failure(self):
        def populate(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))

        controller = FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="   ", confidence=0.9, duration_ms=1))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        result = await retriever.resolve("q", NS)

        assert result.synthesized_answer == ""
        assert result.confidence == 0.0
        assert result.partial is True


# ── guardrail block (Req 8.3-8.4) ──────────────────────────────────


@pytest.mark.unit
class TestGuardrailBlock:
    async def test_guardrail_block_sets_flag_and_excludes_content(self):
        def populate(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))

        controller = FakeController(on_loop=populate, stops=[StopReason.sufficiency_satisfied])
        synthesizer = FakeSynthesizer(
            result=SynthesisResult(answer="blocked text", confidence=0.0, duration_ms=2, guardrail_blocked=True)
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        result = await retriever.resolve("q", NS)

        assert result.guardrail_blocked is True
        assert result.synthesized_answer == ""  # blocked content excluded (Req 8.3)
        assert result.confidence == 0.0


# ── all-tools-fail tolerance (Req 9.6-9.7) ─────────────────────────


@pytest.mark.unit
class TestAllToolsFail:
    async def test_all_tools_fail_no_context_no_answer_no_synthesis(self):
        def all_fail(ctx, clock, queue):
            ctx.record_degraded("strategy:entity_based", "error")
            ctx.record_degraded("graph_traversal", "timeout")

        controller = FakeController(on_loop=all_fail, stops=[None])
        synthesizer = FakeSynthesizer(
            result=SynthesisResult(answer="should not be used", confidence=0.9, duration_ms=1)
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        # Req 9.7: no Best_Effort_Answer, partial, no source succeeded, synthesis skipped.
        assert result.synthesized_answer == ""
        assert result.confidence == 0.0
        assert result.partial is True
        assert len(result.degraded_sources) == 2
        assert synthesizer.calls == []  # synthesis was NOT invoked
        skipped = [s for s in synth_steps(trace) if s.status == "skipped"]
        assert len(skipped) == 1 and "no source succeeded" in str(skipped[0].detail)

    async def test_tools_fail_with_context_yields_partial_best_effort(self):
        def fail_but_have_context(ctx, clock, queue):
            ctx.chunks.append(make_chunk("c1"))
            ctx.record_degraded("graph_traversal", "timeout")

        controller = FakeController(on_loop=fail_but_have_context, stops=[StopReason.no_new_information])
        synthesizer = FakeSynthesizer(
            result=SynthesisResult(answer="best effort answer", confidence=0.4, duration_ms=2)
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )
        result = await retriever.resolve("q", NS)

        # Req 9.6: best-effort answer over the available context, flagged partial.
        assert result.synthesized_answer == "best effort answer"
        assert result.partial is True
        assert result.degraded_sources == ({"source": "graph_traversal", "reason": "timeout"},)
        assert result.supporting_content[0]["chunkId"] == "c1"


# ── namespace scoping (Req 1.5) ────────────────────────────────────


@pytest.mark.unit
class TestNamespaceScoping:
    async def test_every_loop_is_scoped_to_the_request_namespace(self):
        def enqueue_branch(ctx, clock, queue):
            # First loop surfaces a branch; both must run under the same namespace.
            if controller.calls == 1:
                queue.append(SubQuestion(text="branch", origin="branch"))
            ctx.chunks.append(make_chunk(f"c{controller.calls}"))

        controller = FakeController(on_loop=enqueue_branch, stops=[None, None])
        synthesizer = FakeSynthesizer(result=SynthesisResult(answer="a", confidence=0.5, duration_ms=1))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            controller=controller,
            synthesizer=synthesizer,
            clock_source=FakeClockSource(),
        )

        await retriever.resolve("q", "tenant-z")

        assert controller.calls == 2  # root + surfaced branch
        assert controller.namespaces == ["tenant-z", "tenant-z"]

    async def test_invalid_namespace_is_rejected(self):
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="q")]),
            controller=FakeController(),
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        with pytest.raises(ValueError):
            await retriever.resolve("q", "bad namespace!")


# ── lazy-import invariant (Req 12.3) ───────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_retriever_does_not_import_graphrag(self):
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.retriever; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing retriever leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
