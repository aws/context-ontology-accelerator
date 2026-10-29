# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end + property verification for the Agentic Retriever (task 19).

This suite is the capstone verification for the agentic Tier 3 path. Where the
per-component suites (``test_tier3_agentic_controller`` /
``test_tier3_agentic_retriever`` / ``test_tier3_agentic_observability`` / …)
verify each piece in isolation, this suite drives the **real**
:class:`~coa_serve.tier3.agentic.retriever.AgenticRetriever` over the
**real** :class:`~coa_serve.tier3.agentic.controller.ReasoningController`,
the **real** :class:`~coa_serve.tier3.agentic.budget.BudgetClock`,
the **real** :class:`~coa_serve.tier3.agentic.context.AccumulatedContext`,
and the **real** :class:`~coa_serve.tier3.agentic.tools.registry.ToolRegistry`
— only the tools, the planner, the decomposer, the synthesizer, and the wall clock
are deterministic doubles. No live graph access happens here (that is task 20).

GROUNDING (real data, not fictional). The scenario is anchored in the deployed SCL
dev ``sec10qtesting`` tenant (``878519a5e0324c688d612709a``) — a SEC 10-Q filings
knowledge graph whose ``Company`` entities include NVIDIA, Microsoft, Amazon,
Intel, and Apple Inc. — and its induced ontology
``OntologyDevelopmentNotes/ndb-induced-878519a5e0324c688d612709a.ttl`` (57
classes), mirrored as the ``fixtures/agentic_sec10q_ontology.ttl`` excerpt this
suite loads through the real :class:`OntologyFileSource`. The edge types and
traversal targets below were verified live against the property graph (single
``__RELATION__`` type, edge label held in ``r.value``, node label
``__Entity__878519a5e0324c688d612709a__``):

  * NVIDIA ``-[SERVES]->`` {Data Center market, Gaming market, Professional
    Visualization market, Automotive market} (class ``Product``);
  * NVIDIA ``-[SELLS]->`` {Graphics Processing Units, A800, H800, networking
    products} (class ``Product``).

Crucially the induced ontology OMITS ``COMPETES WITH`` and ``ACQUIRED`` even
though both edges genuinely exist in the property graph (verified: ``COMPETES
WITH`` 11 edges / 6 company→company; ``ACQUIRED`` 19 / 14 incl. NVIDIA→Mellanox,
NVIDIA ``ACQUISITION OF``→Arm). That gap drives the ontology-constrained-traversal
property: the controller must refuse to traverse an edge type absent from the
ontology, even when the (mocked) graph tool holds data for it.

Two kinds of test live here:

1. **Integration-style worked example** — the "NVIDIA's market segments"
   walkthrough run end to end against the real controller/retriever: an ontology
   lookup that surfaces the investigation direction → entity anchoring of NVIDIA →
   an ontology-constrained ``SERVES`` edge-typed traversal that discovers the
   market segments and opens per-segment Exploration_Branches → per-segment
   retrieval (a ``SELLS`` traversal + a semantic chunk strategy) → one final
   synthesis. It asserts the reasoning shape, the branch fan-out, namespace
   scoping, the gathered supporting content / graph context, and the terminal stop
   reason.

2. **Property checks** — universal invariants the session must always uphold,
   each exercised at the layer that enforces it and across a spread of inputs
   (the codebase verifies "properties" with example sweeps rather than a
   property-testing library, which is not a dependency here):
     - bounded latency / steps (Req 4.4, 4.5): the session never runs more
       Reasoning_Steps than the configured maximum, in aggregate.
     - aggregate budgets (Req 13.9): the step cap and the fan-out cap are
       enforced across the WHOLE session, not per Sub_Question.
     - bounded fan-out (Req 13.4): the Exploration_Branches opened never exceed
       the configured maximum.
     - ontology-constrained traversal (Req 3.4, 3.9): traversal edge types are
       always a subset of the ontology's edge-type set — ``COMPETES WITH`` /
       ``ACQUIRED`` are never passed to the graph tool.
     - partial honesty (Req 7): the ``partial`` flag is set on Time_Budget
       expiry, on degraded sources, and when no context was gathered.
     - namespace isolation (Req 1.5): every Tool invocation is scoped to the
       request namespace.
     - the lazy-import invariant (Req 12.3): importing the agentic package — and
       the retriever module specifically — never pulls ``graphrag_toolkit`` into
       ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import (
    PlannerDecision,
    StopReason,
    SubQuestion,
)
from coa_serve.tier3.agentic.ontology.source import OntologyFileSource
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.trace import TraceCollector

NS = "sec10qtesting"

# The induced-ontology excerpt for the deployed dev ``sec10qtesting`` tenant.
FIXTURE_TTL = str(Path(__file__).parent / "fixtures" / "agentic_sec10q_ontology.ttl")


# ── deterministic test doubles (shapes mirror the agentic test suite) ──────


class FakeClockSource:
    """A manually advanced monotonic time source (seconds) for the BudgetClock."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _Chunk:
    """Minimal ChunkResult stand-in keyed by ``chunk_id`` (matches AccumulatedContext)."""

    def __init__(self, chunk_id: str, text: str | None = None) -> None:
        self.chunk_id = chunk_id
        self.text = text if text is not None else f"text-{chunk_id}"
        self.source_doc = "nvidia-10q.pdf"
        self.label = "L"
        self.relevance_score = 0.9
        self.uri = ""


class _Entity:
    """Minimal GraphEntity stand-in keyed by ``uri`` (matches AccumulatedContext).

    Mirrors the property-graph reality: entity nodes carry only ``value`` /
    ``search_str`` / ``class`` (there is no entityId property — node identity is
    the openCypher internal id). Here ``uri`` stands in for that stable node
    identity and ``type`` for the ``class`` property.
    """

    def __init__(self, uri: str, label: str, type_: str = "Product") -> None:
        self.uri = uri
        self.label = label
        self.type = type_
        self.relationships: tuple[dict, ...] = ()


class FakeTool:
    """A mock Tool implementing the four-field uniform contract.

    Records every invocation's kwargs (including ``namespace``) so namespace
    scoping and the edge types actually passed to a traversal can be asserted.
    A tool may return a single fixed ``result``, a per-call sequence of
    ``results``, or — in ``counting`` mode — a fresh unique chunk on every call
    (so ``AccumulatedContext.add`` always reports new information and the
    "no new information" stop never fires, letting step-cap tests drive the loop
    to the configured maximum). When BOTH ``results`` and ``counting`` are given,
    the queued ``results`` are returned first (e.g. a seed result that surfaces
    many directions) and ``counting`` unique chunks take over once the queue
    drains. ``raises`` makes ``invoke`` raise so the degraded path can be
    exercised. ``on_invoke`` fires inside ``invoke`` (used to advance the fake
    clock past the Time_Budget mid-loop).
    """

    def __init__(
        self,
        name: str,
        *,
        input_schema: dict | None = None,
        result: ToolResult | None = None,
        results: list[ToolResult] | None = None,
        counting: bool = False,
        raises: bool = False,
        on_invoke=None,
    ) -> None:
        self.name = name
        self.description = f"fake tool {name}"
        self.input_schema = input_schema if input_schema is not None else {}
        self.output_schema = {}
        self._result = result
        self._results = deque(results or [])
        self._counting = counting
        self._raises = raises
        self._on_invoke = on_invoke
        self._n = 0
        self.calls: list[dict] = []

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        self.calls.append({"namespace": namespace, **kwargs})
        if self._on_invoke is not None:
            self._on_invoke()
        if self._raises:
            raise RuntimeError(f"{self.name} boom")
        # Queued ``results`` are consumed first (a seed result that surfaces
        # directions), THEN ``counting`` unique chunks, THEN a fixed ``result``.
        if self._results:
            return self._results.popleft()
        if self._counting:
            self._n += 1
            return ToolResult(chunks=(_Chunk(f"{self.name}-{self._n}"),), detail=f"chunk {self._n}")
        if self._result is not None:
            return self._result
        return ToolResult(chunks=(_Chunk(f"{self.name}-c"),), detail="ok")


class ScriptedPlanner:
    """A :class:`StepPlanner` double driven by scripted candidate lists + booleans.

    ``candidates_script`` is consumed one list per ``propose_steps`` call; once
    exhausted an empty list (no Tool selectable, Req 4.7) is returned.
    ``sufficiency`` is consumed one bool per ``check_sufficiency`` call,
    defaulting to ``default_sufficiency`` thereafter.
    """

    def __init__(
        self,
        candidates_script: list[list[PlannerDecision]] | None = None,
        *,
        sufficiency: list[bool] | None = None,
        default_sufficiency: bool = False,
    ) -> None:
        self._script = deque(candidates_script or [])
        self._suff = deque(sufficiency or [])
        self._default_suff = default_sufficiency

    async def propose_steps(self, *, sub_question, context, tool_specs):
        if self._script:
            return self._script.popleft()
        return []

    async def check_sufficiency(self, *, sub_question, context):
        if self._suff:
            return self._suff.popleft()
        return self._default_suff


class AlwaysPlanner:
    """A planner that always proposes the same decision and never finds sufficiency.

    Used to drive the Reasoning_Loop hard against the aggregate step cap.
    """

    def __init__(self, decision: PlannerDecision, *, sufficient: bool = False) -> None:
        self._decision = decision
        self._sufficient = sufficient

    async def propose_steps(self, *, sub_question, context, tool_specs):
        return [self._decision]

    async def check_sufficiency(self, *, sub_question, context):
        return self._sufficient


class FakeDecomposer:
    """A DecompositionPlanner double returning scripted Sub_Questions.

    Records the ``decompose`` trace step itself (as the real planner does,
    Req 13.10) so the session-level ordered trace includes it.
    """

    def __init__(self, sub_questions: list[SubQuestion]) -> None:
        self._sub_questions = sub_questions

    async def decompose(self, query, trace=None):
        if trace is not None:
            trace.record("decompose", "success", 0, detail=f"{len(self._sub_questions)} sub-questions")
        return list(self._sub_questions)


class FakeSynthesizer:
    """A Synthesizer double; ``synthesize`` returns a scripted result."""

    def __init__(self, result: SynthesisResult | None = None) -> None:
        self._result = result or SynthesisResult(answer="final answer", confidence=0.8, duration_ms=7)
        self.calls = 0

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
        self.calls += 1
        return self._result


def make_retriever(
    *,
    decomposer: FakeDecomposer,
    planner,
    tools: list[FakeTool],
    synthesizer: FakeSynthesizer,
    clock_source: FakeClockSource,
    budget: AgenticBudgetConfig | None = None,
    per_tool_timeout_s: float = 30.0,
) -> AgenticRetriever:
    """Assemble a real AgenticRetriever over a real controller + real registry."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    controller = ReasoningController(planner, per_tool_timeout_s=per_tool_timeout_s)
    return AgenticRetriever(
        registry=registry,
        controller=controller,
        decomposer=decomposer,
        synthesizer=synthesizer,
        budget=budget or AgenticBudgetConfig(),
        clock_source=clock_source,
        # The worked-example / property tests assert the PLANNER-DRIVEN flow
        # step-for-step; the guaranteed final semantic search is verified
        # separately (test_tier3_agentic_retriever.TestFinalSemanticSearch), so
        # keep it off here.
        final_semantic_search=False,
    )


def step_names(trace_steps) -> list[str]:
    """Extract the ordered ``step`` names from trace steps.

    Accepts both the dict form attached to a result's ``trace_steps`` and the
    :class:`TraceStep` objects held on a ``TraceCollector.steps``.
    """
    names: list[str] = []
    for d in trace_steps:
        names.append(d["step"] if isinstance(d, dict) else d.step)
    return names


def tool_success_count(trace_steps, tool_name: str) -> int:
    """Count succeeded invocation steps for ``tool_name`` in a result's trace."""
    return sum(1 for d in trace_steps if d["step"] == tool_name and d["status"] == "succeeded")


# ── ontology fixture for the worked example ────────────────────────────────
#
# The Ontology_Lookup_Tool result the planner consults. Mirrors the real induced
# ontology subset (node + edge types), surfaced as a ToolResult ``items`` dict.
# Edge types include SERVES / SELLS / PRODUCES / … and deliberately OMIT
# COMPETES WITH and ACQUIRED.

_SEC10Q_ONTOLOGY_ITEM = {
    "type": "ontology",
    "node_types": ["Company", "Product", "Service", "Financial-Instrument", "Location"],
    "edge_types": [
        "SERVES",
        "SELLS",
        "PRODUCES",
        "ADDRESSES",
        "INVESTS IN",
        "OWNS",
        "CUSTOMER OF",
        "PART OF",
        "INCLUDES",
        "USES",
    ],
    "edge_map": [
        ["SERVES", "Company", "Product"],
        ["SELLS", "Company", "Product"],
        ["PRODUCES", "Company", "Product"],
    ],
}

# Real NVIDIA -[SERVES]-> market segments (class Product).
_NVIDIA_SEGMENTS = (
    ("urn:nvidia:segment:data-center", "Data Center market"),
    ("urn:nvidia:segment:gaming", "Gaming market"),
    ("urn:nvidia:segment:proviz", "Professional Visualization market"),
    ("urn:nvidia:segment:automotive", "Automotive market"),
)


# ── 1. integration-style worked example (Req 4, 5, 8, 13) ──────────────────


@pytest.mark.unit
class TestNvidiaMarketSegmentsWorkedExample:
    """The "NVIDIA's market segments" walkthrough end to end.

    Driven against the REAL controller + REAL retriever with mocked tools and a
    scripted planner. The session shape:

      root sub-question →
        step 1  ontology_lookup              (resolves the ontology — edge types
                                              incl SERVES / SELLS; surfaces 1 branch)
        step 2  strategy:entity_based        (anchor the NVIDIA Company entity)
      "explore NVIDIA market segments" branch →
        step 3  graph_traversal edge_typed SERVES
                                              (discover the 4 market segments → 2 branches)
      "Data Center market products" branch →
        step 4  graph_traversal edge_typed SELLS  (GPUs / A800 / H800 / networking chunk)
      "Gaming market products" branch →
        step 5  strategy:chunk_based_semantic     (gaming product chunk)
      → queue drains with the last branch satisfied → one final synthesis.

    NOTE: resolving the ontology (step 1) is preparatory progress, not "no new
    information", so the root sub-question's loop CONTINUES to anchor NVIDIA
    (step 2) rather than ending at the lookup. The ontology is then cached on the
    accumulated context, which is what lets the controller constrain the later
    edge-typed traversals to the ontology's edge-type set.
    """

    def _build(self) -> tuple[AgenticRetriever, dict[str, FakeTool]]:
        ontology = FakeTool(
            "ontology_lookup",
            input_schema={},
            result=ToolResult(
                items=(_SEC10Q_ONTOLOGY_ITEM,),
                detail="ontology: 5 node types, 10 edge types",
                produced_directions=("explore NVIDIA's market segments",),
            ),
        )
        entity = FakeTool(
            "strategy:entity_based",
            input_schema={"query": "str"},
            result=ToolResult(
                entities=(_Entity("urn:nvidia", "NVIDIA", "Company"),),
                detail="anchored NVIDIA",
            ),
        )
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]", "edge_types": "list[str]"},
            results=[
                # SERVES → the four market segments + a branch per pursued segment.
                ToolResult(
                    entities=tuple(_Entity(uri, label) for uri, label in _NVIDIA_SEGMENTS),
                    detail="4 market segments via SERVES",
                    produced_directions=("Data Center market products", "Gaming market products"),
                ),
                # SELLS (Data Center branch) → the products sold into that market.
                ToolResult(
                    chunks=(
                        _Chunk(
                            "nvidia-datacenter-sells",
                            "NVIDIA sells Graphics Processing Units, A800, H800, and networking "
                            "products into the Data Center market",
                        ),
                    ),
                    detail="Data Center products via SELLS",
                ),
            ],
        )
        semantic = FakeTool(
            "strategy:chunk_based_semantic",
            input_schema={"query": "str"},
            result=ToolResult(
                chunks=(_Chunk("nvidia-gaming-products", "NVIDIA Gaming segment GPU products"),),
                detail="Gaming products",
            ),
        )

        planner = ScriptedPlanner(
            [
                # root.step1 → ontology lookup (surfaces the investigation direction)
                [PlannerDecision(tool_name="ontology_lookup", inputs={}, reasoning="learn the schema first")],
                # branch.step1 → anchor NVIDIA
                [
                    PlannerDecision(
                        tool_name="strategy:entity_based", inputs={"query": "NVIDIA"}, reasoning="anchor the entity"
                    )
                ],
                # branch.step2 → SERVES traversal
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={"mode": "edge_typed", "entity_ids": ["urn:nvidia"], "edge_types": ["SERVES"]},
                        reasoning="follow SERVES from NVIDIA to its market segments",
                    )
                ],
                # Data Center branch → SELLS traversal
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={"mode": "edge_typed", "entity_ids": ["urn:nvidia"], "edge_types": ["SELLS"]},
                        reasoning="pull the products NVIDIA sells into Data Center",
                    )
                ],
                # Gaming branch → semantic chunk strategy
                [
                    PlannerDecision(
                        tool_name="strategy:chunk_based_semantic",
                        inputs={"query": "NVIDIA gaming products"},
                        reasoning="semantic product chunks",
                    )
                ],
            ],
            # sufficiency consumed only after a step that added new info OR
            # resolved the ontology (resolving the ontology is progress, so the
            # loop continues and a sufficiency check runs):
            #   root.step1 (ontology resolved) → False (keep going)
            #   root.step2 (NVIDIA anchored)   → True  (root sub-question satisfied)
            #   branch.step3 (segments)        → True
            #   Data Center branch (SELLS)     → True
            #   Gaming branch (chunk_semantic) → True
            sufficiency=[False, True, True, True, True],
        )

        retriever = make_retriever(
            decomposer=FakeDecomposer(
                [
                    SubQuestion(
                        text="What are NVIDIA's market segments and what does it sell into them?", multi_entity=True
                    )
                ]
            ),
            planner=planner,
            tools=[ontology, entity, graph, semantic],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        return retriever, {"ontology": ontology, "entity": entity, "graph": graph, "semantic": semantic}

    async def test_full_multi_hop_session_synthesizes_a_grounded_answer(self):
        retriever, tools = self._build()
        trace = TraceCollector()

        result = await retriever.resolve(
            "What are NVIDIA's market segments and what does it sell into them?", NS, trace=trace
        )

        # A complete, non-partial answer was synthesized over gathered context.
        assert result.synthesized_answer == "final answer"
        assert 0.0 <= result.confidence <= 1.0
        assert result.guardrail_blocked is False
        assert result.partial is False
        assert result.degraded_sources == ()

        # Every worked-example beat appears in the trace, in dependency order.
        names = step_names(result.trace_steps)
        assert names[0] == "agentic_session_start"
        assert "decompose" in names
        for beat in (
            "ontology_lookup",
            "strategy:entity_based",
            "graph_traversal",
            "strategy:chunk_based_semantic",
            "exploration_branch",
            "agentic_retrieval_stop",
        ):
            assert beat in names, f"missing worked-example step {beat!r}"
        # Synthesis is always the final recorded step.
        assert names[-1] == "llm_synthesize"

    async def test_traversal_follows_serves_then_per_segment_sells(self):
        retriever, tools = self._build()

        await retriever.resolve("q", NS, trace=TraceCollector())

        # The ontology surfaced the edge types the planner then traversed, and it
        # does NOT include the off-ontology edges.
        assert "SERVES" in _SEC10Q_ONTOLOGY_ITEM["edge_types"]
        assert "SELLS" in _SEC10Q_ONTOLOGY_ITEM["edge_types"]
        assert "COMPETES WITH" not in _SEC10Q_ONTOLOGY_ITEM["edge_types"]
        assert "ACQUIRED" not in _SEC10Q_ONTOLOGY_ITEM["edge_types"]

        # The graph tool was invoked twice: first SERVES from NVIDIA to discover
        # the market segments, then SELLS for a segment — the multi-hop chain.
        graph_calls = tools["graph"].calls
        assert len(graph_calls) == 2
        assert graph_calls[0]["edge_types"] == ["SERVES"]
        assert graph_calls[0]["entity_ids"] == ["urn:nvidia"]
        assert graph_calls[1]["edge_types"] == ["SELLS"]

    async def test_one_exploration_branch_opened_per_discovered_segment(self):
        retriever, tools = self._build()
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)

        branch_seeds = [s.detail for s in trace.steps if s.step == "exploration_branch" and s.status == "success"]
        # 1 branch from the ontology direction + 1 per pursued segment.
        assert "explore NVIDIA's market segments" in branch_seeds
        assert "Data Center market products" in branch_seeds
        assert "Gaming market products" in branch_seeds

    async def test_gathered_context_populates_supporting_content_and_graph_context(self):
        retriever, tools = self._build()

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        # Product chunks from both pursued segments flow into supporting_content.
        chunk_ids = {c["chunkId"] for c in result.supporting_content}
        assert {"nvidia-datacenter-sells", "nvidia-gaming-products"} <= chunk_ids
        # The anchored NVIDIA entity + the discovered market segments flow into
        # graph_context.
        uris = {e["uri"] for e in result.graph_context}
        assert {"urn:nvidia", "urn:nvidia:segment:data-center", "urn:nvidia:segment:gaming"} <= uris

    async def test_terminal_stop_reason_is_recorded_once(self):
        retriever, tools = self._build()
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)

        stops = [s for s in trace.steps if s.step == "agentic_retrieval_stop"]
        assert len(stops) == 1
        # Queue drained within budgets with the last branch satisfied.
        assert str(StopReason.sufficiency_satisfied) in str(stops[0].detail)


# ── 2a. property: bounded latency / steps (Req 4.4, 4.5) ───────────────────


@pytest.mark.unit
class TestBoundedStepsProperty:
    @pytest.mark.parametrize("max_steps", [1, 2, 3, 5, 10])
    async def test_session_never_exceeds_max_steps_in_aggregate(self, max_steps):
        # A planner that always proposes a productive tool and never finds
        # sufficiency would loop forever but for the aggregate step cap. Several
        # Sub_Questions ensure the cap is GLOBAL, not per-Sub_Question.
        probe = FakeTool("strategy:probe", input_schema={"query": "str"}, counting=True)
        planner = AlwaysPlanner(PlannerDecision(tool_name="strategy:probe", inputs={"query": "go"}), sufficient=False)
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="a"), SubQuestion(text="b"), SubQuestion(text="c")]),
            planner=planner,
            tools=[probe],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
            budget=AgenticBudgetConfig(max_steps=max_steps),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        # Exactly ``max_steps`` Tool invocations ran across the whole session —
        # never more, regardless of the number of Sub_Questions (Req 4.4/4.5).
        assert tool_success_count(result.trace_steps, "strategy:probe") == max_steps


# ── 2b. property: aggregate budgets across the whole session (Req 13.9) ─────


@pytest.mark.unit
class TestAggregateBudgetsProperty:
    async def test_step_cap_is_aggregate_not_per_subquestion(self):
        # 4 Sub_Questions, a step cap of 3: a per-Sub_Question cap would allow 12
        # steps; the aggregate cap allows exactly 3 (Req 13.9).
        probe = FakeTool("strategy:probe", input_schema={"query": "str"}, counting=True)
        planner = AlwaysPlanner(PlannerDecision(tool_name="strategy:probe", inputs={"query": "go"}))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text=t) for t in ("a", "b", "c", "d")]),
            planner=planner,
            tools=[probe],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
            budget=AgenticBudgetConfig(max_steps=3),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert tool_success_count(result.trace_steps, "strategy:probe") == 3

    async def test_time_budget_is_aggregate_and_stops_remaining_subquestions(self):
        # The first Sub_Question's single step blows the 30s Time_Budget; the
        # remaining Sub_Questions are never started (Req 7.4, 13.9).
        src = FakeClockSource()
        probe = FakeTool(
            "strategy:probe",
            input_schema={"query": "str"},
            counting=True,
            on_invoke=lambda: src.advance(99.0),
        )
        planner = AlwaysPlanner(PlannerDecision(tool_name="strategy:probe", inputs={"query": "go"}))
        retriever = make_retriever(
            decomposer=FakeDecomposer(
                [SubQuestion(text="first"), SubQuestion(text="second"), SubQuestion(text="third")]
            ),
            planner=planner,
            tools=[probe],
            synthesizer=FakeSynthesizer(),
            clock_source=src,
            budget=AgenticBudgetConfig(time_budget_s=30),
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        # Only the first Sub_Question ever started; time expiry ended the session.
        assert step_names(trace.steps).count("subquestion_start") == 1
        stops = [s for s in trace.steps if s.step == "agentic_retrieval_stop"]
        assert str(StopReason.time_budget_expired) in str(stops[0].detail)
        assert result.partial is True


# ── 2c. property: bounded fan-out (Req 13.4, 13.7-13.8) ─────────────────────


@pytest.mark.unit
class TestBoundedFanoutProperty:
    @pytest.mark.parametrize("max_fanout", [1, 2, 3, 5])
    async def test_branches_opened_never_exceed_max_fanout(self, max_fanout):
        # The first step surfaces far more directions than the fan-out cap; the
        # session must open at most ``max_fanout`` Exploration_Branches in
        # aggregate, no matter how many directions are produced.
        directions = tuple(f"segment-{i}" for i in range(12))
        # First call surfaces many directions (the seed result); later calls (the
        # opened branches) just return a fresh chunk so they terminate without
        # surfacing more.
        seeder = FakeTool(
            "strategy:seed",
            input_schema={"query": "str"},
            results=[ToolResult(chunks=(_Chunk("seed"),), detail="seeded", produced_directions=directions)],
            counting=True,  # subsequent calls return unique chunks, no new directions
        )
        planner = AlwaysPlanner(PlannerDecision(tool_name="strategy:seed", inputs={"query": "go"}))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[seeder],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
            # Generous step cap so branch processing is not what bounds fan-out.
            budget=AgenticBudgetConfig(max_steps=50, max_fanout=max_fanout),
        )
        trace = TraceCollector()

        await retriever.resolve("q", NS, trace=trace)

        opened = [s for s in trace.steps if s.step == "exploration_branch" and s.status == "success"]
        assert len(opened) == max_fanout
        # The cap was actually hit (more directions were available than allowed),
        # recorded as a skipped branch.
        skipped = [s for s in trace.steps if s.step == "exploration_branch" and s.status == "skipped"]
        assert skipped, "expected the fan-out cap to be hit and recorded as skipped"


# ── 2d. property: ontology-constrained traversal (Req 3.4, 3.9) ─────────────


@pytest.mark.unit
class TestOntologyConstrainedTraversalProperty:
    async def test_fixture_ontology_omits_off_ontology_edges(self):
        # Ground the property in the real induced ontology: the fixture (a subset
        # of the deployed dev tenant's induced .ttl) declares SERVES / SELLS but
        # NOT COMPETES WITH / ACQUIRED — even though both genuinely exist in the
        # property graph. This gap is what the controller enforces below.
        onto = await OntologyFileSource(FIXTURE_TTL).load(NS)
        assert "SERVES" in onto.edge_types
        assert "SELLS" in onto.edge_types
        assert "COMPETES WITH" not in onto.edge_types
        assert "ACQUIRED" not in onto.edge_types

    async def _run_with_edge_types(self, requested, mode):
        """Drive one edge_typed traversal under the given ontology edge mode against
        the real induced fixture ontology; return the edge_types the tool received."""
        ontology = await OntologyFileSource(FIXTURE_TTL).load(NS)
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]", "edge_types": "list[str]"},
        )
        registry = ToolRegistry()
        registry.register(graph)
        ctx = AccumulatedContext()
        ctx.ontology = ontology
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={"mode": "edge_typed", "entity_ids": ["urn:nvidia"], "edge_types": list(requested)},
                    )
                ]
            ],
            sufficiency=[True],
        )
        controller = ReasoningController(planner, ontology_edge_mode=mode)
        await controller.run_one_loop(
            SubQuestion(text="q"), ctx, registry, _started_clock(), deque(), TraceCollector(), namespace=NS
        )
        return graph.calls[0]["edge_types"] if graph.calls else None

    @pytest.mark.parametrize(
        "requested,expected",
        [
            # DEFAULT soft_prior: NOTHING dropped. Off-ontology edges (COMPETES WITH /
            # ACQUIRED — real in the graph, absent from the pruned ontology) survive;
            # ontology edges (SERVES / SELLS) are simply ordered first.
            (["SERVES", "COMPETES WITH", "SELLS"], ["SERVES", "SELLS", "COMPETES WITH"]),
            (["ACQUIRED"], ["ACQUIRED"]),
            (["COMPETES WITH", "ACQUIRED"], ["COMPETES WITH", "ACQUIRED"]),
            (["SELLS"], ["SELLS"]),
            (["SERVES", "ACQUIRED", "SELLS", "COMPETES WITH"], ["SERVES", "SELLS", "ACQUIRED", "COMPETES WITH"]),
        ],
    )
    async def test_soft_prior_retains_off_ontology_edges_reordered(self, requested, expected):
        # This is the idea-5 fix: the pruned ontology no longer HIDES the graph's
        # real predicates from the traversal — it only PREFERS its own (ordered first).
        passed = await self._run_with_edge_types(requested, mode="soft_prior")
        assert passed == expected

    @pytest.mark.parametrize(
        "requested,expected",
        [
            # strict mode preserves the legacy allowlist: off-ontology edges dropped...
            (["SERVES", "COMPETES WITH", "SELLS"], ["SERVES", "SELLS"]),
            (["SELLS"], ["SELLS"]),
            (["SERVES", "ACQUIRED", "SELLS", "COMPETES WITH"], ["SERVES", "SELLS"]),
            # ...EXCEPT the anti-starvation guard: all-off-ontology keeps the request
            # (an empty edge_types would trip the tool's MIN_EDGE_TYPES bound).
            (["ACQUIRED"], ["ACQUIRED"]),
            (["COMPETES WITH", "ACQUIRED"], ["COMPETES WITH", "ACQUIRED"]),
        ],
    )
    async def test_strict_mode_allowlists_but_never_starves(self, requested, expected):
        passed = await self._run_with_edge_types(requested, mode="strict")
        assert passed == expected


# ── 2e. property: partial honesty (Req 7.5-7.8, 9.6-9.7) ────────────────────


@pytest.mark.unit
class TestPartialHonestyProperty:
    async def test_partial_set_on_time_budget_expiry(self):
        src = FakeClockSource()
        probe = FakeTool(
            "strategy:probe",
            input_schema={"query": "str"},
            counting=True,
            on_invoke=lambda: src.advance(99.0),
        )
        planner = AlwaysPlanner(PlannerDecision(tool_name="strategy:probe", inputs={"query": "go"}))
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[probe],
            synthesizer=FakeSynthesizer(),
            clock_source=src,
            budget=AgenticBudgetConfig(time_budget_s=30),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert result.partial is True

    async def test_partial_set_when_a_tool_degrades(self):
        # A succeeding tool gathers context; a second, failing tool degrades. The
        # result carries the degraded source AND is flagged partial (Req 9.6).
        good = FakeTool("strategy:good", input_schema={"query": "str"}, counting=True)
        bad = FakeTool("strategy:bad", input_schema={"query": "str"}, raises=True)
        planner = ScriptedPlanner(
            [
                [PlannerDecision(tool_name="strategy:good", inputs={"query": "a"})],
                [PlannerDecision(tool_name="strategy:bad", inputs={"query": "b"})],
            ],
            default_sufficiency=False,
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[good, bad],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
            budget=AgenticBudgetConfig(max_steps=2),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert result.partial is True
        assert any(d["source"] == "strategy:bad" and d["reason"] == "error" for d in result.degraded_sources)

    async def test_real_chunk_window_mixed_provenance_marks_partial_and_retains_valid_evidence(self, monkeypatch):
        monkeypatch.setattr(
            ChunkExpansionTool,
            "_format_label",
            staticmethod(lambda namespace, label: f"`{label}{namespace}__`"),
        )
        chunk_window = ChunkExpansionTool("neptune-db://fake-host:8182")
        chunk_window._store = type(
            "_Store",
            (),
            {
                "execute_query_with_retry": staticmethod(
                    lambda cypher, params: [
                        {
                            "chunkId": "valid-neighbor",
                            "text": "Grounded adjacent evidence.",
                            "sourceDocumentId": "source-42",
                            "sourceFilename": "annual-report.pdf",
                        },
                        {
                            "chunkId": "anonymous-neighbor",
                            "text": "This row has no source document.",
                        },
                    ]
                )
            },
        )()
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="chunk_window",
                        inputs={"chunk_ids": ["seed"], "hops": 1, "limit": 10},
                    )
                ]
            ],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[chunk_window],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert result.partial is True
        assert [item["chunkId"] for item in result.supporting_content] == ["valid-neighbor"]
        assert result.degraded_sources == ({"source": "chunk_window", "reason": "missing_document_provenance"},)
        chunk_steps = [step for step in result.trace_steps if step["step"] == "chunk_window"]
        assert chunk_steps[0]["status"] == "degraded"
        assert "dropped 1 anonymous or malformed document chunk(s)" in chunk_steps[0]["detail"]

    async def test_partial_set_when_no_context_was_gathered(self):
        # The planner can select no Tool, so no context is ever gathered; the
        # best-effort answer must be flagged partial (Req 7.8).
        planner = ScriptedPlanner([], default_sufficiency=False)  # nothing selectable
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[FakeTool("strategy:unused", input_schema={"query": "str"})],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )

        result = await retriever.resolve("q", NS, trace=TraceCollector())

        assert result.partial is True
        assert result.supporting_content == ()
        assert result.graph_context == ()


# ── 2f. property: namespace isolation (Req 1.5) ─────────────────────────────


@pytest.mark.unit
class TestNamespaceIsolationProperty:
    @pytest.mark.parametrize("namespace", ["sec10qtesting", "tenant-x", "ns-42"])
    async def test_every_tool_invocation_is_scoped_to_the_request_namespace(self, namespace):
        # Run the full multi-hop worked-example session and assert EVERY tool
        # invocation across every Sub_Question and Exploration_Branch carried the
        # originating request namespace — never any other.
        retriever, tools = TestNvidiaMarketSegmentsWorkedExample()._build()

        await retriever.resolve("q", namespace, trace=TraceCollector())

        all_calls = [call for tool in tools.values() for call in tool.calls]
        assert all_calls, "expected the session to invoke at least one tool"
        assert all(call["namespace"] == namespace for call in all_calls)


# ── 2g. property: the lazy-import invariant (Req 12.3) ──────────────────────


@pytest.mark.unit
class TestLazyImportInvariantProperty:
    def test_importing_agentic_and_retriever_does_not_import_graphrag(self):
        # Importing the agentic package AND the retriever module specifically must
        # not pull ``graphrag_toolkit`` into ``sys.modules``. Runs in a fresh
        # subprocess (with the current ``sys.path``) so a sibling test that already
        # imported the toolkit cannot mask a regression.
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic; "
            "import coa_serve.tier3.agentic.retriever; "
            "import coa_serve.tier3.agentic.controller; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        import os

        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            "importing the agentic retriever leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout


# ── shared helper ──────────────────────────────────────────────────────────


def _started_clock():
    """A real BudgetClock anchored on a non-advancing source (no time pressure)."""
    from coa_serve.tier3.agentic.budget import BudgetClock

    clock = BudgetClock(AgenticBudgetConfig(max_steps=10, max_fanout=5), clock=FakeClockSource())
    clock.started()
    return clock
