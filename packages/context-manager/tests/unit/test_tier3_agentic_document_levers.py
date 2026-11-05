# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the opt-in deep-reasoning levers.

Covers:
- ``read_document`` (``tools/document_tool.py``): namespace scoping (keys outside
  every registered DOCUMENTS prefix, a foreign bucket, an unavailable inventory and
  unsafe keys are refused without touching S3), windowing / truncation / offsets,
  the per-request window option, the cache, and chunk-header key recovery.
- The retriever's per-request options: ``readDocument`` (tool withheld unless set),
  ``ontologyLookupMode=preload`` (ontology resolved up front, tool withheld, no
  step spent); and that a request with none of them sees exactly the pre-existing
  tool set and trace.
- The planner: the read-document instruction and the document listing appear only
  when the tool is offered; full-document windows reach the assess view.
- The controller: ``read_document`` ranks with the strategy tools, and a withheld
  tool named by the planner is not invoked.
- The request-model validator for the new option keys and the factory gating.
- Anchored reading (``readDocumentAnchor``): the anchor locator (exact /
  normalized / overlap, ties by question terms), the centred window with its
  previous/next offsets, auto-anchoring on the document's retrieved chunks, and
  the request-scoped swap of the anchored variant.
- Evidence prefetch (``prefetchMode="assess"``): the SQL writer on the whole
  question, reads of the best-supported documents (ranking, text budget, time
  guard, front-loading), and the single sufficiency check that skips the loop.
- ``document_ref`` ignores a record's own ``"source"`` field.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest
from coa_serve.exceptions import AccessDeniedError
from coa_serve.models import InvokeRequest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.budget import BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import PlannerDecision, StopReason, SubQuestion
from coa_serve.tier3.agentic.ontology import Ontology
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever, _RequestTools
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.document_anchor import anchor_segments, choose_center
from coa_serve.tier3.agentic.tools.document_tool import (
    DEFAULT_MAX_CHARS,
    HARD_MAX_CHARS,
    AnchoredReadDocumentTool,
    ReadDocumentTool,
    document_ref,
    document_topic,
    rank_source_documents,
)
from coa_serve.tier3.agentic.tools.document_tool import (
    TOOL_NAME as READ_TOOL,
)
from coa_serve.tier3.agentic.tools.ontology_tool import OntologyLookupTool
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

NS = "acme"
BUCKET = "corp-docs"
KEY = "reports/2024/q4-summary.txt"

_SOURCES = [
    {
        "sourceType": "DOCUMENTS",
        "sourceBucketArn": f"arn:aws:s3:::{BUCKET}",
        "s3Prefixes": ["reports/", "memos/"],
    }
]


# Upload sources: files in the shared platform bucket under <namespaceId>/raw/<uploadId>/.
_PLATFORM = "coa-dev-sources-data-123456789012"
_NS_ID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


def _upload_source(prefixes=None) -> dict:
    return {
        "sourceType": "DOCUMENTS",
        "sourceSubType": "LOCAL_UPLOAD",
        "docSourceType": "upload",
        "namespaceId": _NS_ID,
        "s3Prefixes": [f"{_NS_ID}/raw/up-1/"] if prefixes is None else prefixes,
    }


# ── fakes ──────────────────────────────────────────────────────────


class FakeRegistry:
    """A SourcesRegistry double returning a scripted DOCUMENTS inventory."""

    def __init__(self, sources=_SOURCES, *, raises: Exception | None = None) -> None:
        self._sources = sources
        self._raises = raises
        self.namespaces: list[str] = []
        self.available = True

    async def document_sources(self, namespace):
        self.namespaces.append(namespace)
        if self._raises is not None:
            raise self._raises
        return self._sources


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """Records get_object calls; serves objects from a dict keyed by (bucket, key)."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self._objects = objects
        self.calls: list[dict] = []

    def get_object(self, *, Bucket, Key, Range=None):  # noqa: N803 - boto3 casing
        self.calls.append({"Bucket": Bucket, "Key": Key, "Range": Range})
        return {"Body": FakeBody(self._objects[(Bucket, Key)])}


def _tool(text: str = "x" * 30_000, *, registry: FakeRegistry | None = None) -> tuple[ReadDocumentTool, FakeS3]:
    s3 = FakeS3({(BUCKET, KEY): text.encode("utf-8")})
    return ReadDocumentTool(registry or FakeRegistry(), s3_client=s3), s3


# ── read_document: namespace scoping ───────────────────────────────


class TestReadDocumentScoping:
    @pytest.mark.asyncio
    async def test_reads_key_under_registered_prefix(self):
        registry = FakeRegistry()
        tool, s3 = _tool("hello world", registry=registry)
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert registry.namespaces == [NS]
        assert s3.calls and s3.calls[0]["Bucket"] == BUCKET and s3.calls[0]["Key"] == KEY
        assert len(result.chunks) == 1
        assert result.chunks[0].text.endswith("hello world")
        assert result.chunks[0].source_doc == KEY

    @pytest.mark.asyncio
    async def test_refuses_key_outside_every_prefix(self):
        tool, s3 = _tool()
        result = await tool.invoke(namespace=NS, source_key="secrets/payroll.txt")
        assert not result.chunks
        assert "refused" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_refuses_foreign_bucket(self):
        tool, s3 = _tool()
        result = await tool.invoke(namespace=NS, source_key=f"s3://other-bucket/{KEY}")
        assert not result.chunks and "refused" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_accepts_matching_s3_uri(self):
        tool, s3 = _tool("abc")
        result = await tool.invoke(namespace=NS, source_key=f"s3://{BUCKET}/{KEY}")
        assert len(result.chunks) == 1 and s3.calls[0]["Key"] == KEY

    @pytest.mark.parametrize(
        ("prefix", "key", "allowed"),
        [
            # A plain string prefix, as S3 ListObjects applied it at ingestion: a
            # prefix without a trailing slash also covers sibling keys.
            ("reports", "reports-private/x.txt", True),
            ("reports/", "reports-private/x.txt", False),
            # An empty prefix ingested the whole bucket.
            ("", "anything/x.txt", True),
        ],
    )
    def test_prefix_matching_follows_ingestion(self, prefix, key, allowed):
        sources = [{"sourceType": "DOCUMENTS", "sourceBucketArn": f"arn:aws:s3:::{BUCKET}", "s3Prefixes": [prefix]}]
        assert ReadDocumentTool.authorize(sources, "", key) == (BUCKET if allowed else "")

    @pytest.mark.asyncio
    async def test_reads_an_upload_source_document_from_the_platform_bucket(self, monkeypatch):
        monkeypatch.setenv("SOURCES_DATA_BUCKET", _PLATFORM)
        key = f"{_NS_ID}/raw/up-1/notes.txt"
        s3 = FakeS3({(_PLATFORM, key): b"upload text"})
        tool = ReadDocumentTool(FakeRegistry(sources=[_upload_source()]), s3_client=s3)
        result = await tool.invoke(namespace=_NS_ID, source_key=f"s3://{_PLATFORM}/{key}")
        assert "upload text" in result.chunks[0].text
        assert s3.calls[0]["Bucket"] == _PLATFORM

    @pytest.mark.parametrize(
        ("key", "allowed"),
        [
            (f"{_NS_ID}/raw/up-1/notes.txt", True),
            # another namespace's upload folder in the same shared bucket
            ("11111111-2222-3333-4444-555555555555/raw/up-9/notes.txt", False),
            # this namespace, but not under one of its upload prefixes
            (f"{_NS_ID}/staging/x/notes.txt", False),
        ],
    )
    def test_upload_source_is_scoped_to_its_own_raw_prefix(self, monkeypatch, key, allowed):
        monkeypatch.setenv("SOURCES_DATA_BUCKET", _PLATFORM)
        assert ReadDocumentTool.authorize([_upload_source()], "", key) == (_PLATFORM if allowed else "")

    def test_upload_source_without_its_own_prefix_never_opens_the_shared_bucket(self, monkeypatch):
        monkeypatch.setenv("SOURCES_DATA_BUCKET", _PLATFORM)
        no_prefix = _upload_source(prefixes=[])
        foreign = _upload_source(prefixes=["11111111-2222-3333-4444-555555555555/raw/up-9/"])
        # Shares the "<namespace>/raw" text but is not under the "<namespace>/raw/" folder.
        lookalike = _upload_source(prefixes=[f"{_NS_ID}/raw-other/"])
        for source in (no_prefix, foreign, lookalike):
            assert ReadDocumentTool.authorize([source], "", f"{_NS_ID}/raw/up-1/notes.txt") == ""
            assert ReadDocumentTool.authorize([source], "", "11111111-2222-3333-4444-555555555555/raw/up-9/x.txt") == ""
            assert ReadDocumentTool.authorize([source], "", f"{_NS_ID}/raw-other/x.txt") == ""

    def test_upload_source_unreadable_when_the_platform_bucket_is_not_configured(self, monkeypatch):
        monkeypatch.delenv("SOURCES_DATA_BUCKET", raising=False)
        assert ReadDocumentTool.authorize([_upload_source()], "", f"{_NS_ID}/raw/up-1/notes.txt") == ""

    @pytest.mark.asyncio
    async def test_refuses_when_namespace_has_no_document_sources(self):
        tool, s3 = _tool(registry=FakeRegistry(sources=[]))
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert not result.chunks and "refused" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_fails_closed_when_inventory_unavailable(self):
        tool, s3 = _tool(registry=FakeRegistry(sources=None))
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert not result.chunks and "unavailable" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_fails_closed_when_registry_raises(self):
        tool, s3 = _tool(registry=FakeRegistry(raises=RuntimeError("ddb down")))
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert not result.chunks and "refused" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "   ", "reports/../secrets/x.txt", "reports/a\x00b.txt", "r" * 2000])
    async def test_refuses_unsafe_keys(self, bad):
        tool, s3 = _tool()
        result = await tool.invoke(namespace=NS, source_key=bad)
        assert not result.chunks
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_source_without_prefixes_covers_whole_bucket(self):
        registry = FakeRegistry(sources=[{"sourceType": "DOCUMENTS", "sourceBucketArn": f"arn:aws:s3:::{BUCKET}"}])
        tool, _ = _tool("abc", registry=registry)
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert len(result.chunks) == 1

    @pytest.mark.asyncio
    async def test_refuses_non_text_document(self):
        tool, s3 = _tool()
        result = await tool.invoke(namespace=NS, source_key="reports/annual.pdf")
        assert not result.chunks and "plain-text" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_s3_error_degrades_without_raising(self):
        tool = ReadDocumentTool(FakeRegistry(), s3_client=FakeS3({}))  # KeyError on any get
        result = await tool.invoke(namespace=NS, source_key=KEY)
        assert not result.chunks and "error" in result.detail


# ── read_document: windowing ───────────────────────────────────────


class TestReadDocumentWindowing:
    @pytest.mark.asyncio
    async def test_default_window_truncates_and_reports_next_offset(self):
        text = "".join(chr(ord("a") + i % 26) for i in range(30_000))
        tool, _ = _tool(text)
        result = await tool.invoke(namespace=NS, source_key=KEY)
        chunk = result.chunks[0]
        body = chunk.text.split("\n", 1)[1]
        assert body == text[:DEFAULT_MAX_CHARS]
        assert f"offset={DEFAULT_MAX_CHARS}" in chunk.text
        assert result.items[0]["next_offset"] == DEFAULT_MAX_CHARS
        assert result.items[0]["total_chars"] == 30_000

    @pytest.mark.asyncio
    async def test_window_header_parses_back(self):
        from coa_serve.tier3.agentic.tools.document_tool import WINDOW_HEADER_RE

        tool, _ = _tool("y" * 30_000)
        result = await tool.invoke(namespace=NS, source_key=KEY, offset=1000, max_chars=2000)
        match = WINDOW_HEADER_RE.match(result.chunks[0].text)
        assert match and match.group("key") == KEY
        assert (match.group("start"), match.group("end"), match.group("total")) == ("1000", "3000", "30000")

    @pytest.mark.asyncio
    async def test_cache_bounded_by_total_characters(self, monkeypatch):
        from coa_serve.tier3.agentic.tools import document_tool as dt

        monkeypatch.setattr(dt, "_CACHE_MAX_CHARS", 25_000)
        tool, _ = _tool("z" * 10_000)
        for i in range(4):
            tool._cache[("b", f"k{i}")] = ("z" * 10_000, False, tool._clock())
        await tool.invoke(namespace=NS, source_key=KEY)
        assert sum(len(v[0]) for v in tool._cache.values()) <= 25_000
        assert next(reversed(tool._cache))[1] == KEY  # the document just read stays

    @pytest.mark.asyncio
    async def test_offset_reads_later_window_from_cache(self):
        text = "0123456789" * 3000
        tool, s3 = _tool(text)
        first = await tool.invoke(namespace=NS, source_key=KEY, max_chars=1000)
        second = await tool.invoke(namespace=NS, source_key=KEY, offset=1000, max_chars=1000)
        assert first.chunks[0].chunk_id != second.chunks[0].chunk_id
        assert second.chunks[0].text.split("\n", 1)[1] == text[1000:2000]
        assert len(s3.calls) == 1  # second window served from the cache

    @pytest.mark.asyncio
    async def test_last_window_has_no_next_offset(self):
        tool, _ = _tool("a" * 1500)
        result = await tool.invoke(namespace=NS, source_key=KEY, offset=1000, max_chars=1000)
        assert result.items[0]["next_offset"] is None
        assert "more text follows" not in result.chunks[0].text

    @pytest.mark.asyncio
    async def test_offset_past_end_returns_no_chunk(self):
        tool, _ = _tool("short")
        result = await tool.invoke(namespace=NS, source_key=KEY, offset=999)
        assert not result.chunks and "past the end" in result.detail

    @pytest.mark.asyncio
    async def test_max_chars_is_clamped_to_hard_cap(self):
        tool, _ = _tool("b" * (HARD_MAX_CHARS + 5000))
        result = await tool.invoke(namespace=NS, source_key=KEY, max_chars=10**6)
        assert result.items[0]["end"] == HARD_MAX_CHARS

    @pytest.mark.asyncio
    async def test_request_option_sets_default_window(self):
        tool, _ = _tool("c" * 10_000)
        result = await tool.invoke(namespace=NS, source_key=KEY, options={"readDocumentMaxChars": 2000})
        assert result.items[0]["end"] == 2000

    @pytest.mark.asyncio
    async def test_ignores_out_of_band_context(self):
        tool, _ = _tool("d")
        result = await tool.invoke(namespace=NS, source_key=KEY, profile={"sub": "u"}, model_id="m", options={})
        assert len(result.chunks) == 1


class TestDocumentRef:
    def test_recovers_key_from_graphrag_header(self):
        text = (
            '{\n  "source": "corpus/documents/71/abc.txt (abc.txt, 22117ed6, ns, passthrough, .txt, 3667)",\n'
            '  "topic": "account applications schema",\n  "statements": []\n}'
        )
        chunk = ChunkResult(chunk_id="c1", text=text, source_doc="aws:opaque", relevance_score=0.0)
        assert document_ref(chunk) == "corpus/documents/71/abc.txt"
        assert document_topic(chunk) == "account applications schema"

    def test_unknown_when_no_header(self):
        chunk = ChunkResult(chunk_id="c1", text="plain text", source_doc="aws:opaque", relevance_score=0.0)
        assert document_ref(chunk) == ""

    def test_window_chunk_maps_to_its_key(self):
        chunk = ChunkResult(
            chunk_id=f"doc:{KEY}@0", text="[Source document ...]\nbody", source_doc=KEY, relevance_score=1
        )
        assert document_ref(chunk) == KEY

    def test_record_source_system_is_not_a_key(self):
        chunk = ChunkResult(
            chunk_id="c1",
            text='{"object_id":"x","source":"github","source_id":"y"} body',
            source_doc="aws:t:c:1",
            relevance_score=1,
        )
        assert document_ref(chunk) == ""

    def test_later_path_header_still_found(self):
        chunk = ChunkResult(
            chunk_id="c2",
            text='{"source":"slack"} {"source":"docs/a.txt"}',
            source_doc="aws:t:c:2",
            relevance_score=1,
        )
        assert document_ref(chunk) == "docs/a.txt"


# ── retriever: per-request options ─────────────────────────────────


class _SpecTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"
        self.input_schema: dict = {}
        self.output_schema: dict = {}

    async def invoke(self, *, namespace, **_):
        return ToolResult()


class _FakeOntologySource:
    def __init__(self) -> None:
        self.loads = 0

    async def load(self, namespace):
        self.loads += 1
        return Ontology(node_types=("Account",), edge_types=("HAS_OWNER",), edge_map=(("HAS_OWNER", "Account", "P"),))


class _RecordingController:
    """Records what each loop was offered and whether the ontology was resolved."""

    def __init__(self) -> None:
        self.excluded: list[frozenset] = []
        self.ontology_at_start: list = []
        self.steps_allowed: list[int] = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.excluded.append(kw.get("excluded_tools") or frozenset())
        self.ontology_at_start.append(ctx.ontology)
        n = 0
        while clock.try_consume_step():
            n += 1
        self.steps_allowed.append(n)
        return StopReason.max_steps_reached


class _Decomposer:
    async def decompose(self, query, trace=None):
        return [SubQuestion(text=query)]


class _Synth:
    async def synthesize(self, query, chunks, entities, **_):
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


def _retriever(registry: ToolRegistry, controller, *, budget: AgenticBudgetConfig | None = None) -> AgenticRetriever:
    return AgenticRetriever(
        registry,
        controller,
        _Decomposer(),
        _Synth(),
        budget or AgenticBudgetConfig(max_steps=5, time_budget_s=60.0),
        final_semantic_search=False,
    )


def _registry_with(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


class TestRetrieverOptions:
    @pytest.mark.asyncio
    async def test_read_document_withheld_by_default_and_untraced(self):
        reg = _registry_with(_SpecTool("strategy:entity_based"), _SpecTool("read_document"))
        ctl = _RecordingController()
        trace = TraceCollector()
        await _retriever(reg, ctl).resolve("q", NS, trace=trace)
        assert "read_document" in ctl.excluded[0]
        # Default requests record no gating step and no budget override.
        assert not [s for s in trace.steps if s.step in ("tools_gated", "ontology_preload")]

    @pytest.mark.asyncio
    async def test_read_document_offered_when_opted_in(self):
        reg = _registry_with(_SpecTool("strategy:entity_based"), _SpecTool("read_document"))
        ctl = _RecordingController()
        await _retriever(reg, ctl).resolve("q", NS, options={"readDocument": True})
        assert "read_document" not in ctl.excluded[0]

    @pytest.mark.asyncio
    async def test_read_document_withheld_without_document_sources(self):
        reg = _registry_with(_SpecTool("read_document"))
        ctl = _RecordingController()
        await _retriever(reg, ctl).resolve("q", NS, options={"readDocument": True}, has_unstructured_source=False)
        assert "read_document" in ctl.excluded[0]

    @pytest.mark.asyncio
    async def test_truthy_non_bool_does_not_opt_in(self):
        reg = _registry_with(_SpecTool("read_document"))
        ctl = _RecordingController()
        await _retriever(reg, ctl).resolve("q", NS, options={"readDocument": "yes"})
        assert "read_document" in ctl.excluded[0]

    @pytest.mark.asyncio
    async def test_gating_trace_detail_unchanged_by_opt_in_tools(self):
        reg = _registry_with(_SpecTool("read_document"), _SpecTool("vector_search"))
        trace = TraceCollector()
        await _retriever(reg, _RecordingController()).resolve(
            "q", NS, trace=trace, exclude_tools=frozenset({"vector_search"})
        )
        gated = [s for s in trace.steps if s.step == "tools_gated"]
        assert len(gated) == 1 and "read_document" not in gated[0].detail

    @pytest.mark.asyncio
    async def test_ontology_planner_mode_by_default(self):
        source = _FakeOntologySource()
        reg = _registry_with(OntologyLookupTool(source))
        ctl = _RecordingController()
        await _retriever(reg, ctl).resolve("q", NS)
        assert "ontology_lookup" not in ctl.excluded[0]
        assert ctl.ontology_at_start == [None]
        assert source.loads == 0

    @pytest.mark.asyncio
    async def test_ontology_preload_resolves_up_front_and_withholds_tool(self):
        source = _FakeOntologySource()
        reg = _registry_with(OntologyLookupTool(source))
        ctl = _RecordingController()
        trace = TraceCollector()
        await _retriever(reg, ctl).resolve("q", NS, trace=trace, options={"ontologyLookupMode": "preload"})
        assert "ontology_lookup" in ctl.excluded[0]
        assert ctl.ontology_at_start[0] is not None
        assert ctl.ontology_at_start[0].edge_types == ("HAS_OWNER",)
        assert source.loads == 1
        # The preload spends no reasoning step: the loop still gets the full cap.
        assert ctl.steps_allowed == [5]
        assert [s.status for s in trace.steps if s.step == "ontology_preload"] == ["success"]

    @pytest.mark.asyncio
    async def test_ontology_preload_skipped_when_tool_excluded(self):
        source = _FakeOntologySource()
        reg = _registry_with(OntologyLookupTool(source))
        ctl = _RecordingController()
        await _retriever(reg, ctl).resolve(
            "q", NS, options={"ontologyLookupMode": "preload"}, exclude_tools=frozenset({"ontology_lookup"})
        )
        assert source.loads == 0 and ctl.ontology_at_start == [None]

    @pytest.mark.asyncio
    async def test_ontology_preload_failure_runs_without_ontology(self):
        class _Boom:
            async def load(self, namespace):
                raise RuntimeError("down")

        reg = _registry_with(OntologyLookupTool(_Boom()))
        ctl = _RecordingController()
        trace = TraceCollector()
        result = await _retriever(reg, ctl).resolve("q", NS, trace=trace, options={"ontologyLookupMode": "preload"})
        assert ctl.ontology_at_start == [None]
        assert [s.status for s in trace.steps if s.step == "ontology_preload"] == ["error"]
        assert result.synthesized_answer == "ok"

    @pytest.mark.asyncio
    async def test_ontology_preload_cancelled_with_the_request(self):
        import asyncio

        started, cancelled = asyncio.Event(), asyncio.Event()

        class _Slow:
            async def load(self, namespace):
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        class _SlowDecomposer:
            async def decompose(self, query, trace):
                await asyncio.sleep(30)

        reg = _registry_with(OntologyLookupTool(_Slow()))
        retriever = AgenticRetriever(
            reg,
            _RecordingController(),
            _SlowDecomposer(),
            _Synth(),
            AgenticBudgetConfig(max_steps=5, time_budget_s=60.0),
            final_semantic_search=False,
        )
        task = asyncio.ensure_future(retriever.resolve("q", NS, options={"ontologyLookupMode": "preload"}))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), 5)


# ── planner prompt ─────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, text: str = '{"candidates": []}') -> None:
        self._text = text
        self.calls: list[dict] = []

    async def converse(self, *, prompt, system=None, **_):
        self.calls.append({"prompt": prompt, "system": system})
        return SimpleNamespace(text=self._text, guardrail_blocked=False)


_BASE_SPECS = [{"name": "strategy:entity_based", "description": "d", "input_schema": {}, "output_schema": {}}]
_DOC_SPEC = {"name": "read_document", "description": "d", "input_schema": {}, "output_schema": {}}


def _graphrag_chunk(cid: str, key: str, topic: str) -> ChunkResult:
    text = f'{{\n  "source": "{key} ({key.rsplit("/", 1)[-1]}, ds, ns)",\n  "topic": "{topic}",\n  "statements": []\n}}'
    return ChunkResult(chunk_id=cid, text=text, source_doc="aws:opaque", relevance_score=0.0)


class TestPlannerDocumentPrompt:
    @pytest.mark.asyncio
    async def test_prompt_unchanged_when_tool_not_offered(self):
        ctx = AccumulatedContext()
        ctx.chunks.append(_graphrag_chunk("c1", "docs/a.txt", "Alpha"))
        llm = _FakeLLM()
        await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=_BASE_SPECS
        )
        assert "READ THE SOURCE DOCUMENT" not in llm.calls[0]["system"]
        assert "Source documents seen so far" not in llm.calls[0]["prompt"]

    @pytest.mark.asyncio
    async def test_prompt_lists_documents_when_offered(self):
        ctx = AccumulatedContext()
        ctx.chunks.extend(
            [
                _graphrag_chunk("c1", "docs/a.txt", "Alpha"),
                _graphrag_chunk("c2", "docs/b.txt", "Beta"),
                _graphrag_chunk("c3", "docs/b.txt", "Beta 2"),
            ]
        )
        ctx.chunks.append(
            ChunkResult(
                chunk_id="doc:docs/b.txt@0",
                text=(
                    "[Source document docs/b.txt: characters 0-100 of 300; "
                    "more text follows, read it with offset=100]\nB"
                ),
                source_doc="docs/b.txt",
                relevance_score=1.0,
            )
        )
        llm = _FakeLLM()
        await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=[*_BASE_SPECS, _DOC_SPEC]
        )
        system, prompt = llm.calls[0]["system"], llm.calls[0]["prompt"]
        assert "READ THE SOURCE DOCUMENT" in system
        listing = prompt.split("Source documents seen so far", 1)[1]
        # Most-supported document first, with its topic and the window already read.
        assert listing.index("source_key=docs/b.txt") < listing.index("source_key=docs/a.txt")
        assert "(topic: Beta)" in listing
        assert "ALREADY READ: characters 0-100 of 300" in listing

    @pytest.mark.asyncio
    async def test_assess_view_includes_document_window(self):
        ctx = AccumulatedContext()
        body = "Z" * 5000
        ctx.chunks.append(
            ChunkResult(chunk_id="doc:docs/b.txt@0", text=f"[hdr]\n{body}", source_doc="docs/b.txt", relevance_score=1)
        )
        llm = _FakeLLM('{"sufficient": true, "working_answer": "a"}')
        await BedrockStepPlanner(llm).assess(sub_question=SubQuestion(text="q"), context=ctx)
        assert body in llm.calls[0]["prompt"]


# ── controller ─────────────────────────────────────────────────────


class _ScriptedPlanner:
    def __init__(self, rounds: list[list[PlannerDecision]]) -> None:
        self._rounds = deque(rounds)

    async def propose_steps(self, *, sub_question, context, tool_specs):
        return self._rounds.popleft() if self._rounds else []

    async def check_sufficiency(self, *, sub_question, context):
        return True


class _CountingTool(_SpecTool):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls = 0

    async def invoke(self, *, namespace, **_):
        self.calls += 1
        return ToolResult(
            chunks=(ChunkResult(chunk_id=f"{self.name}-{self.calls}", text="t", source_doc="", relevance_score=1),)
        )


class TestControllerDocumentRules:
    @pytest.mark.asyncio
    async def test_read_document_not_overridden_by_strategy_alternative(self):
        read, strat = _CountingTool("read_document"), _CountingTool("strategy:entity_based")
        reg = _registry_with(read, strat)
        planner = _ScriptedPlanner(
            [[PlannerDecision(tool_name="read_document"), PlannerDecision(tool_name="strategy:entity_based")]]
        )
        clock = BudgetClock(AgenticBudgetConfig(max_steps=3, time_budget_s=60))
        clock.started()
        await ReasoningController(planner).run_one_loop(
            SubQuestion(text="q"), AccumulatedContext(), reg, clock, deque(), TraceCollector(), namespace=NS
        )
        assert read.calls == 1 and strat.calls == 0

    @pytest.mark.asyncio
    async def test_withheld_tool_named_by_planner_is_not_invoked(self):
        read = _CountingTool("read_document")
        reg = _registry_with(read)
        planner = _ScriptedPlanner([[PlannerDecision(tool_name="read_document")]])
        clock = BudgetClock(AgenticBudgetConfig(max_steps=2, time_budget_s=60))
        clock.started()
        trace = TraceCollector()
        await ReasoningController(planner).run_one_loop(
            SubQuestion(text="q"),
            AccumulatedContext(),
            reg,
            clock,
            deque(),
            trace,
            namespace=NS,
            excluded_tools=frozenset({"read_document"}),
        )
        assert read.calls == 0
        assert any(s.detail == "unknown_tool:read_document" for s in trace.steps)


# ── request validation + factory ───────────────────────────────────


class TestOptionValidation:
    @pytest.mark.parametrize(
        "options",
        [
            {"readDocument": True, "readDocumentMaxChars": 8000},
            {"ontologyLookupMode": "preload"},
            {"ontologyLookupMode": "planner"},
            {"prefetchMode": "assess"},
            {"loopFixes": True, "plannerParseRetry": False, "entityRouting": True},
        ],
    )
    def test_accepts_valid(self, options):
        assert InvokeRequest(query="q", namespace=NS, options=options).options == options

    @pytest.mark.parametrize(
        "options",
        [
            {"readDocument": "true"},
            {"readDocumentMaxChars": "8000"},
            {"ontologyLookupMode": "off"},
            {"prefetchMode": "loop"},
            {"prefetchMode": "answer"},
            {"prefetchMode": True},
            {"loopFixes": "yes"},
        ],
    )
    def test_rejects_malformed(self, options):
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options=options)


class TestFactoryGating:
    def _config(self):
        return SimpleNamespace(
            neptune_endpoint="",
            opensearch_endpoint="",
            deep_reasoning_per_tool_timeout_s=30,
            deep_reasoning_ontology_source="graph",
            deep_reasoning_ontology_file="",
        )

    def test_registered_only_with_available_sources_registry(self):
        from coa_serve.tier3.agentic.factory import build_tool_registry

        clients = SimpleNamespace(llm=None, vector=None, graph=None)
        source = _FakeOntologySource()
        without = build_tool_registry(self._config(), clients, ontology_source=source)
        assert "read_document" not in without.names()
        unavailable = FakeRegistry()
        unavailable.available = False
        assert (
            "read_document"
            not in build_tool_registry(
                self._config(), clients, ontology_source=source, sources_registry=unavailable
            ).names()
        )
        with_reg = build_tool_registry(self._config(), clients, ontology_source=source, sources_registry=FakeRegistry())
        assert "read_document" in with_reg.names()


# ── doc_id resolution ──────────────────────────────────────────────


class TestDocIdResolution:
    @pytest.mark.asyncio
    async def test_doc_id_resolved_then_authorized(self):
        seen: list[tuple[str, str]] = []

        async def resolver(namespace, doc_id):
            seen.append((namespace, doc_id))
            return KEY

        s3 = FakeS3({(BUCKET, KEY): b"resolved body"})
        tool = ReadDocumentTool(FakeRegistry(), s3_client=s3, source_key_resolver=resolver)
        result = await tool.invoke(namespace=NS, doc_id="aws:tenant:abc:01")
        assert seen == [(NS, "aws:tenant:abc:01")]
        assert result.chunks and result.chunks[0].text.endswith("resolved body")

    @pytest.mark.asyncio
    async def test_resolved_key_outside_namespace_is_refused(self):
        async def resolver(namespace, doc_id):
            return "secrets/payroll.txt"

        s3 = FakeS3({})
        tool = ReadDocumentTool(FakeRegistry(), s3_client=s3, source_key_resolver=resolver)
        result = await tool.invoke(namespace=NS, doc_id="aws:tenant:abc:01")
        assert not result.chunks and "refused" in result.detail
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_doc_id_without_resolver_or_key(self):
        async def unknown(namespace, doc_id):
            return ""

        for resolver in (None, unknown):
            tool = ReadDocumentTool(FakeRegistry(), s3_client=FakeS3({}), source_key_resolver=resolver)
            result = await tool.invoke(namespace=NS, doc_id="aws:tenant:abc:01")
            assert not result.chunks and "no readable source key" in result.detail

    @pytest.mark.asyncio
    async def test_doc_id_lookup_failure_is_not_reported_as_a_miss(self):
        async def failing(namespace, doc_id):
            raise TimeoutError

        tool = ReadDocumentTool(FakeRegistry(), s3_client=FakeS3({}), source_key_resolver=failing)
        result = await tool.invoke(namespace=NS, doc_id="aws:tenant:abc:01")
        assert not result.chunks
        assert "TimeoutError resolving doc_id" in result.detail and "no readable source key" not in result.detail

    @pytest.mark.asyncio
    async def test_graph_tool_source_lookup(self, monkeypatch):
        from coa_serve.tier3.agentic.tools import graph_tool as gt

        tool = gt.GraphTraversalTool("neptune-db://host:8182")
        captured: dict = {}

        async def fake_run(cypher, params):
            captured.update(cypher=cypher, params=params)
            return [{"key": KEY}]

        monkeypatch.setattr(tool, "_run_cypher", fake_run)
        monkeypatch.setattr(gt.GraphTraversalTool, "_format_label", staticmethod(lambda ns, label: f"`{label}{ns}`"))
        assert await tool.resolve_source_key(NS, "aws:tenant:abc:01") == KEY
        assert "`__Source__acme`" in captured["cypher"] and captured["params"] == {"source_id": "aws:tenant:abc:01"}
        # A malformed id never reaches the graph.
        captured.clear()
        assert await tool.resolve_source_key(NS, "x' OR 1=1 //") == ""
        assert captured == {}

    @pytest.mark.asyncio
    async def test_planner_lists_doc_id_for_headerless_chunk(self):
        ctx = AccumulatedContext()
        ctx.chunks.append(ChunkResult(chunk_id="c1", text="raw text", source_doc="aws:t:abc:01", relevance_score=0.0))
        llm = _FakeLLM()
        await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=[*_BASE_SPECS, _DOC_SPEC]
        )
        assert "doc_id=aws:t:abc:01" in llm.calls[0]["prompt"]


# ── anchored reading (options.readDocumentAnchor) ──────────────────


def _filler(n: int, word: str = "lorem") -> str:
    return " ".join(f"{word}{i % 7}" for i in range(n // 7))


def _long_doc() -> str:
    # ~30k chars; the November section sits around 20k.
    head = _filler(18_000)
    section = (
        "\n## November 2024 month-end\nThe retained invoice count at November month-end was 4,217 "
        "and the cancellation share was 3.1 percent.\n"
    )
    return head + section + _filler(12_000, "ipsum")


class TestAnchorLocator:
    def test_exact_match(self):
        doc = _long_doc()
        m = choose_center(doc, ["retained invoice count at November month-end"], "", 12000)
        assert m.method == "exact"
        assert abs(m.center - doc.index("retained invoice count")) < 100

    def test_normalized_match_ignores_case_and_punctuation(self):
        doc = _long_doc()
        m = choose_center(doc, ["RETAINED invoice-count, at november MONTH END"], "", 12000)
        assert m.method == "normalized"
        assert abs(m.center - doc.index("retained invoice count")) < 200

    def test_graphrag_chunk_statements_are_located(self):
        doc = _long_doc()
        chunk = (
            '{\n  "source": "reports/x.txt (x.txt, ds, ns)",\n  "topic": "Invoice metrics",\n'
            '  "statements": [\n    "The cancellation share was 3.1 percent at the reported month end."\n  ]\n}'
        )
        assert "Invoice metrics" in anchor_segments(chunk)
        m = choose_center(doc, [chunk], "", 12000)
        assert m.method == "normalized"
        assert abs(m.center - doc.index("cancellation share")) < 300

    def test_overlap_fallback_for_paraphrase(self):
        doc = _long_doc()
        m = choose_center(doc, ["how many retained invoices in november and cancellation share"], "", 12000)
        assert m.method == "overlap"
        assert abs(m.center - doc.index("November 2024 month-end")) < 2500

    def test_none_when_nothing_matches(self):
        assert choose_center("alpha beta gamma", ["zzzz qqqq"], "", 1000).method == "none"

    def test_repeated_anchor_prefers_question_terms(self):
        block_a = "Status report. The metric value was recorded. October figures follow. " * 3
        block_b = "Status report. The metric value was recorded. November figures follow. " * 3
        doc = block_a + _filler(20_000) + block_b
        m = choose_center(doc, ["The metric value was recorded"], "What was the November value?", 2000)
        assert m.center > len(block_a) + 10_000

    def test_window_covering_most_anchors_wins(self):
        doc = (
            "alpha unique sentence number one here. "
            + _filler(20_000)
            + ("bravo unique sentence number two here. charlie unique sentence number three here.")
        )
        m = choose_center(
            doc,
            ["alpha unique sentence number one", "bravo unique sentence number two", "charlie unique sentence three"],
            "",
            4000,
        )
        assert m.center > 10_000 and m.anchors_in_window >= 2


def _anchored(text: str):
    s3 = FakeS3({(BUCKET, KEY): text.encode("utf-8")})
    return ReadDocumentTool(FakeRegistry(), s3_client=s3).anchored_variant(), s3


class TestAnchoredReadDocument:
    def test_variant_is_its_own_anchored_variant(self):
        base, _ = _tool()
        variant = base.anchored_variant()
        assert base.anchored_variant() is variant and variant.anchored_variant() is variant

    @pytest.mark.asyncio
    async def test_explicit_anchor_centres_window(self):
        doc = _long_doc()
        tool, _ = _anchored(doc)
        result = await tool.invoke(namespace=NS, source_key=KEY, anchor="retained invoice count", max_chars=4000)
        item = result.items[0]
        pos = doc.index("retained invoice count")
        assert item["offset"] < pos < item["end"]
        assert item["end"] - item["offset"] == 4000
        assert item["prev_offset"] == max(0, item["offset"] - 4000)
        assert item["next_offset"] == item["end"]
        assert item["anchor_method"] == "exact" and item["anchor_source"] == "anchor"
        assert "centred on the anchored passage" in result.chunks[0].text

    @pytest.mark.asyncio
    async def test_auto_anchors_on_retrieved_chunk_of_same_document(self):
        doc = _long_doc()
        tool, _ = _anchored(doc)
        ctx = AccumulatedContext()
        ctx.chunks.append(_graphrag_chunk("c1", KEY, "The retained invoice count at November month-end was 4,217"))
        ctx.chunks.append(_graphrag_chunk("c2", "other/doc.txt", "lorem3 lorem4 lorem5 lorem6"))
        result = await tool.invoke(
            namespace=NS, source_key=KEY, offset=0, max_chars=4000, accumulated_context=ctx, sub_question="q"
        )
        item = result.items[0]
        assert item["anchor_source"] == "retrieved_chunks"
        assert item["offset"] < doc.index("retained invoice count") < item["end"]

    @pytest.mark.asyncio
    async def test_anchor_chunk_id_resolves_from_context(self):
        doc = _long_doc()
        tool, _ = _anchored(doc)
        ctx = AccumulatedContext()
        ctx.chunks.append(
            ChunkResult(chunk_id="raw-1", text="cancellation share was 3.1 percent", source_doc="", relevance_score=0)
        )
        result = await tool.invoke(
            namespace=NS, source_key=KEY, anchor_chunk_id="raw-1", max_chars=3000, accumulated_context=ctx
        )
        assert result.items[0]["anchor_source"] == "anchor_chunk_id"
        assert result.items[0]["offset"] < doc.index("cancellation share") < result.items[0]["end"]

    @pytest.mark.asyncio
    async def test_explicit_offset_pages_without_anchor(self):
        doc = _long_doc()
        tool, _ = _anchored(doc)
        ctx = AccumulatedContext()
        ctx.chunks.append(_graphrag_chunk("c1", KEY, "retained invoice count at November month-end"))
        result = await tool.invoke(namespace=NS, source_key=KEY, offset=6000, max_chars=2000, accumulated_context=ctx)
        assert result.items[0]["offset"] == 6000 and "anchor_method" not in result.items[0]

    @pytest.mark.asyncio
    async def test_offset_zero_pages_once_document_was_read(self):
        doc = _long_doc()
        tool, _ = _anchored(doc)
        ctx = AccumulatedContext()
        ctx.chunks.append(_graphrag_chunk("c1", KEY, "retained invoice count at November month-end"))
        ctx.chunks.append(ChunkResult(chunk_id=f"doc:{KEY}@18000", text="[hdr]\nx", source_doc=KEY, relevance_score=1))
        result = await tool.invoke(namespace=NS, source_key=KEY, offset=0, max_chars=2000, accumulated_context=ctx)
        assert result.items[0]["offset"] == 0 and "anchor_method" not in result.items[0]

    @pytest.mark.asyncio
    async def test_unlocated_anchor_falls_back_to_offset(self):
        tool, _ = _anchored(_long_doc())
        result = await tool.invoke(namespace=NS, source_key=KEY, anchor="zzzz qqqq xxxx", max_chars=2000)
        assert result.items[0]["offset"] == 0 and result.items[0]["anchor_method"] == "none"

    @pytest.mark.asyncio
    async def test_anchored_variant_keeps_namespace_scoping(self):
        tool, s3 = _anchored(_long_doc())
        result = await tool.invoke(namespace=NS, source_key="secrets/payroll.txt", anchor="invoice")
        assert not result.chunks and "refused" in result.detail and s3.calls == []

    def test_variant_shares_state_and_differs_only_in_spec(self):
        base = ReadDocumentTool(FakeRegistry(), s3_client=FakeS3({}))
        variant = base.anchored_variant()
        assert isinstance(variant, AnchoredReadDocumentTool) and variant is base.anchored_variant()
        assert variant.name == base.name and variant._cache is base._cache
        assert "anchor" in variant.input_schema and "anchor" not in base.input_schema
        assert variant.wants_context is True and not getattr(base, "wants_context", False)


class TestAnchoredWiring:
    @pytest.mark.asyncio
    async def test_retriever_swaps_variant_only_when_both_options_set(self):
        base = ReadDocumentTool(FakeRegistry(), s3_client=FakeS3({}))
        reg = _registry_with(base)
        seen = []

        class _Ctl(_RecordingController):
            async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
                seen.append((registry.get("read_document"), registry.specs(exclude=kw.get("excluded_tools"))))
                return None

        for opts in (
            {"readDocument": True},
            {"readDocumentAnchor": True},
            {"readDocument": True, "readDocumentAnchor": True},
        ):
            await _retriever(reg, _Ctl()).resolve("q", NS, options=opts)
        (t1, s1), (t2, s2), (t3, s3) = seen
        assert t1 is base and "anchor" not in s1[0]["input_schema"]
        assert s2 == []  # readDocument off: tool withheld regardless of the anchor option
        assert isinstance(t3, AnchoredReadDocumentTool) and "anchor" in s3[0]["input_schema"]
        assert "anchor" not in base.input_schema  # the registry's tool is untouched

    @pytest.mark.asyncio
    async def test_controller_passes_context_only_to_context_tools(self):
        received: dict = {}

        class _CtxTool(_SpecTool):
            wants_context = True

            async def invoke(self, *, namespace, **kw):
                received.update(kw)
                return ToolResult()

        plain_kwargs: dict = {}

        class _PlainTool(_SpecTool):
            async def invoke(self, *, namespace, **kw):
                plain_kwargs.update(kw)
                return ToolResult()

        reg = _registry_with(_CtxTool("read_document"), _PlainTool("strategy:entity_based"))
        planner = _ScriptedPlanner(
            [[PlannerDecision(tool_name="read_document")], [PlannerDecision(tool_name="strategy:entity_based")]]
        )
        clock = BudgetClock(AgenticBudgetConfig(max_steps=2, time_budget_s=60))
        clock.started()
        ctx = AccumulatedContext()
        await ReasoningController(planner, max_no_progress_steps=5).run_one_loop(
            SubQuestion(text="the sub q"), ctx, reg, clock, deque(), TraceCollector(), namespace=NS
        )
        assert received["accumulated_context"] is ctx and received["sub_question"] == "the sub q"
        assert "accumulated_context" not in plain_kwargs

    @pytest.mark.asyncio
    async def test_planner_anchor_guidance_only_with_anchored_spec(self):
        ctx = AccumulatedContext()
        ctx.chunks.append(_graphrag_chunk("c1", "docs/a.txt", "Alpha"))
        anchored_spec = {**_DOC_SPEC, "input_schema": {"anchor": "str"}}
        plain, anchored = _FakeLLM(), _FakeLLM()
        await BedrockStepPlanner(plain).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=[*_BASE_SPECS, _DOC_SPEC]
        )
        await BedrockStepPlanner(anchored).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=[*_BASE_SPECS, anchored_spec]
        )
        assert "READ AROUND THE PASSAGE" not in plain.calls[0]["system"]
        assert "chunk ids:" not in plain.calls[0]["prompt"]
        assert "READ AROUND THE PASSAGE" in anchored.calls[0]["system"]
        assert "chunk ids: c1" in anchored.calls[0]["prompt"]

    def test_option_validation(self):
        assert InvokeRequest(query="q", namespace=NS, options={"readDocumentAnchor": True}).options
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"readDocumentAnchor": "yes"})


# ── document ranking + prefetch reads (options.prefetchMode) ────────


class TestRankSourceDocuments:
    def test_pools_chunks_per_document(self):
        chunks = [
            _graphrag_chunk("a1", "docs/a.txt", "A"),
            _graphrag_chunk("b1", "docs/b.txt", "B"),
            _graphrag_chunk("b2", "docs/b.txt", "B"),
            ChunkResult(chunk_id="r1", text="raw", source_doc="aws:t:c:1", relevance_score=0.9),
            ChunkResult(chunk_id="doc:docs/a.txt@0", text="[hdr]\nx", source_doc="docs/a.txt", relevance_score=1),
        ]
        ranked = rank_source_documents(chunks)
        assert [(d.source_key or d.doc_id, d.chunk_count) for d in ranked] == [
            ("docs/b.txt", 2),
            ("aws:t:c:1", 1),  # same count as a.txt, higher best score
            ("docs/a.txt", 1),
        ]
        assert ranked[2].read is True and ranked[0].read is False

    def test_tie_breaks_on_earliest_rank(self):
        ranked = rank_source_documents(
            [_graphrag_chunk("x", "docs/x.txt", "X"), _graphrag_chunk("y", "docs/y.txt", "Y")]
        )
        assert [d.source_key for d in ranked] == ["docs/x.txt", "docs/y.txt"]


def _doc_registry(objects: dict[str, str]) -> tuple[ToolRegistry, FakeS3]:
    s3 = FakeS3({(BUCKET, k): v.encode("utf-8") for k, v in objects.items()})
    reg = _registry_with(
        ReadDocumentTool(
            FakeRegistry(sources=[{"sourceType": "DOCUMENTS", "sourceBucketArn": f"arn:aws:s3:::{BUCKET}"}]),
            s3_client=s3,
        )
    )
    return reg, s3


def _read_retriever(reg, clock_source=None) -> AgenticRetriever:
    kwargs = {"clock_source": clock_source} if clock_source else {}
    return AgenticRetriever(
        reg,
        _RecordingController(),
        _Decomposer(),
        _Synth(),
        AgenticBudgetConfig(max_steps=5, time_budget_s=60.0, synthesis_reserve_s=5.0),
        final_semantic_search=False,
        **kwargs,
    )


def _gathered():
    return [
        _graphrag_chunk("a1", "docs/a.txt", "A"),
        _graphrag_chunk("b1", "docs/b.txt", "B"),
        _graphrag_chunk("b2", "docs/b.txt", "B"),
        _graphrag_chunk("c1", "docs/c.txt", "C"),
    ]


async def _read_top(reg, chunks, *, k=2, text_budget=60_000, anchored=False, options=None, clock_source=None):
    """Run the prefetch document reader over a context holding ``chunks``."""
    retriever = _read_retriever(reg, clock_source=clock_source)
    ctx = AccumulatedContext()
    ctx.chunks.extend(chunks)
    clock = BudgetClock(retriever._budget, clock=clock_source or time.monotonic)
    clock.started()
    trace = TraceCollector()
    await retriever._read_top_documents(
        ctx,
        clock,
        trace,
        namespace=NS,
        query="q",
        k=k,
        text_budget=text_budget,
        tools=_RequestTools(reg, {READ_TOOL: reg.get(READ_TOOL).anchored_variant()}) if anchored else reg,
        tool_context={"options": options or {}},
    )
    return ctx, trace


class TestPrefetchReads:
    @pytest.mark.asyncio
    async def test_reads_top_k_and_front_loads_windows(self):
        reg, s3 = _doc_registry({"docs/a.txt": "AAA text", "docs/b.txt": "BBB text", "docs/c.txt": "CCC"})
        ctx, trace = await _read_top(reg, _gathered(), k=2)
        assert sorted(c["Key"] for c in s3.calls) == ["docs/a.txt", "docs/b.txt"]
        # Windows first, in document-rank order (b has 2 chunks), then the chunks.
        assert [c.chunk_id for c in ctx.chunks[:2]] == ["doc:docs/b.txt@0", "doc:docs/a.txt@0"]
        assert ctx.chunks[0].text.endswith("BBB text")
        assert len(ctx.chunks) == 6
        assert [s.status for s in trace.steps if s.step == "prefetch_read_documents"] == ["success"]

    @pytest.mark.asyncio
    async def test_skips_documents_already_read(self):
        reg, s3 = _doc_registry({"docs/a.txt": "A", "docs/b.txt": "B", "docs/c.txt": "C"})
        chunks = _gathered() + [
            ChunkResult(chunk_id="doc:docs/b.txt@0", text="[hdr]\nB", source_doc="docs/b.txt", relevance_score=1)
        ]
        await _read_top(reg, chunks, k=1)
        assert [c["Key"] for c in s3.calls] == ["docs/a.txt"]

    @pytest.mark.asyncio
    async def test_skips_cleanly_when_no_time_left(self):
        reg, s3 = _doc_registry({"docs/a.txt": "A", "docs/b.txt": "B"})
        clock = _FakeClock()
        retriever = _read_retriever(reg, clock_source=clock)
        budget_clock = BudgetClock(retriever._budget, clock=clock)
        budget_clock.started()
        clock.advance(57.0)  # 3s left < 5s reserve
        ctx = AccumulatedContext()
        ctx.chunks.extend(_gathered())
        trace = TraceCollector()
        await retriever._read_top_documents(
            ctx, budget_clock, trace, namespace=NS, query="q", k=3, text_budget=60_000, tools=reg, tool_context={}
        )
        assert s3.calls == []
        steps = [s for s in trace.steps if s.step == "prefetch_read_documents"]
        assert steps[0].status == "skipped" and "no time left" in steps[0].detail

    @pytest.mark.asyncio
    async def test_text_budget_bounds_reads(self):
        long = "z" * 50_000
        reg, s3 = _doc_registry({"docs/a.txt": long, "docs/b.txt": long, "docs/c.txt": long})
        ctx, _ = await _read_top(reg, _gathered(), k=3, text_budget=20_000, options={"readDocumentMaxChars": 12_000})
        windows = [c for c in ctx.chunks if c.chunk_id.startswith("doc:")]
        # 12k for the top document, the remaining 8k for the second, nothing left for the third.
        assert len(windows) == 2 and len(s3.calls) == 2
        assert sum(len(c.text.split("\n", 1)[1]) for c in windows) == 20_000

    @pytest.mark.asyncio
    async def test_anchored_read_centres_on_retrieved_chunk(self):
        doc = _long_doc()
        reg, _ = _doc_registry({"docs/a.txt": doc})
        chunks = [_graphrag_chunk("a1", "docs/a.txt", "The retained invoice count at November month-end was 4,217")]
        ctx, _ = await _read_top(reg, chunks, k=1, anchored=True, options={"readDocumentMaxChars": 4000})
        window = ctx.chunks[0]
        start = int(window.chunk_id.rsplit("@", 1)[1])
        assert start < doc.index("retained invoice count") < start + 4000

    @pytest.mark.asyncio
    async def test_refused_reads_are_reported_not_raised(self):
        reg, s3 = _doc_registry({})
        _, trace = await _read_top(reg, [_graphrag_chunk("x1", "docs/x.pdf", "X")], k=1)
        step = [s for s in trace.steps if s.step == "prefetch_read_documents"][0]
        assert step.status == "error" and "not read" in step.detail
        assert s3.calls == []


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ── evidence prefetch (options.prefetchMode) ───────────────────────────


class _SqlTool(_SpecTool):
    def __init__(self) -> None:
        super().__init__("nl_to_sql")
        self.input_schema = {"query": "str"}
        self.queries: list[str] = []

    async def invoke(self, *, namespace, query="", **_):
        self.queries.append(query)
        return ToolResult(items=({"row": 1},), detail="1 row")


class _AssessingController(_RecordingController):
    def __init__(self, sufficient: bool) -> None:
        super().__init__()
        self.sufficient = sufficient
        self.assessed: list[str] = []

    async def assess(self, sub_question, ctx):
        self.assessed.append(sub_question.text)
        return self.sufficient, "working answer"


class TestPrefetch:
    @pytest.mark.asyncio
    async def test_off_by_default(self):
        sql = _SqlTool()
        ctl = _AssessingController(True)
        trace = TraceCollector()
        await _retriever(_registry_with(sql), ctl).resolve("q", NS, trace=trace)
        assert sql.queries == [] and ctl.assessed == [] and len(ctl.steps_allowed) == 1
        assert not [s for s in trace.steps if s.step.startswith("prefetch")]

    @pytest.mark.asyncio
    async def test_assess_runs_sql_on_whole_question_and_skips_loop_when_sufficient(self):
        sql = _SqlTool()
        ctl = _AssessingController(True)
        trace = TraceCollector()
        await _retriever(_registry_with(sql), ctl).resolve(
            "the whole question", NS, trace=trace, options={"prefetchMode": "assess"}
        )
        assert sql.queries == ["the whole question"]
        assert ctl.assessed == ["the whole question"] and ctl.steps_allowed == []
        assert [s.status for s in trace.steps if s.step == "prefetch_sql"] == ["success"]
        stop = [s for s in trace.steps if s.step == "agentic_retrieval_stop"]
        assert stop[0].detail == "sufficiency_satisfied"

    @pytest.mark.asyncio
    async def test_assess_mode_runs_loop_with_full_budget_when_not_sufficient(self):
        ctl = _AssessingController(False)
        await _retriever(_registry_with(_SqlTool()), ctl).resolve("q", NS, options={"prefetchMode": "assess"})
        assert ctl.assessed == ["q"] and ctl.steps_allowed == [5]

    @pytest.mark.asyncio
    async def test_no_sql_without_structured_source(self):
        sql = _SqlTool()
        await _retriever(_registry_with(sql), _AssessingController(False)).resolve(
            "q", NS, options={"prefetchMode": "assess"}, has_structured_source=False
        )
        assert sql.queries == []

    @pytest.mark.asyncio
    async def test_reads_top_documents_found_by_the_baseline_search(self, monkeypatch):
        reg, s3 = _doc_registry({"docs/a.txt": "AAA", "docs/b.txt": "BBB", "docs/c.txt": "CCC"})
        ctl = _AssessingController(True)
        retriever = _read_retriever(reg)
        retriever._controller = ctl

        async def _search_results(final_search, ctx, trace, clock):
            if not ctx.chunks:
                ctx.chunks.extend(_gathered())

        monkeypatch.setattr(retriever, "_finish_final_semantic_search", _search_results)
        trace = TraceCollector()
        await retriever.resolve("q", NS, trace=trace, options={"prefetchMode": "assess", "readDocument": True})
        # The two best-supported documents are read before the single assess call.
        assert sorted(c["Key"] for c in s3.calls) == ["docs/a.txt", "docs/b.txt"]
        assert ctl.assessed == ["q"] and ctl.steps_allowed == []

    @pytest.mark.asyncio
    async def test_no_reads_without_read_document_opt_in(self, monkeypatch):
        reg, s3 = _doc_registry({"docs/a.txt": "AAA"})
        retriever = _read_retriever(reg)
        retriever._controller = _AssessingController(True)

        async def _search_results(final_search, ctx, trace, clock):
            ctx.chunks.extend(_gathered())

        monkeypatch.setattr(retriever, "_finish_final_semantic_search", _search_results)
        await retriever.resolve("q", NS, options={"prefetchMode": "assess"})
        assert s3.calls == []

    @pytest.mark.asyncio
    async def test_sql_access_denied_cancels_the_reads_and_propagates(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        class _DeniedSql(_SqlTool):
            async def invoke(self, **_):
                await started.wait()
                raise AccessDeniedError("denied")

        class _SlowRead(_SpecTool):
            def __init__(self) -> None:
                super().__init__("read_document")

            async def invoke(self, **_):
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return ToolResult()

        reg = _registry_with(_DeniedSql(), _SlowRead())
        retriever = _read_retriever(reg)
        ctx = AccumulatedContext()
        ctx.chunks.extend(_gathered())
        clock = BudgetClock(retriever._budget)
        clock.started()
        trace = TraceCollector()
        with pytest.raises(AccessDeniedError):
            await retriever._prefetch(
                "q",
                ctx,
                clock,
                trace,
                namespace=NS,
                tools=reg,
                sql_allowed=True,
                read_allowed=True,
                tool_context={},
            )
        assert cancelled.is_set()
        assert [s.detail for s in trace.steps if s.step == "prefetch_sql"] == ["AccessDeniedError"]


# ── options.fastPathSpeculation ─────────────────────────────────────


class _CountingSynth:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, query, chunks, entities, **_):
        self.calls += 1
        return SynthesisResult(answer="spec answer", confidence=0.8, duration_ms=1)


def _spec_retriever(registry, controller, synth):
    return AgenticRetriever(
        registry,
        controller,
        _Decomposer(),
        synth,
        AgenticBudgetConfig(max_steps=5, time_budget_s=60.0),
        final_semantic_search=False,
    )


class TestFastPathSpeculation:
    OPTS = {"prefetchMode": "assess", "fastPathSpeculation": True}

    @pytest.mark.asyncio
    async def test_kept_when_check_passes(self):
        synth = _CountingSynth()
        ctl = _AssessingController(True)
        result = await _spec_retriever(_registry_with(_SqlTool()), ctl, synth).resolve("q", NS, options=self.OPTS)
        assert result.synthesized_answer == "spec answer" and synth.calls == 1 and ctl.steps_allowed == []

    @pytest.mark.asyncio
    async def test_discarded_when_check_fails(self):
        synth = _CountingSynth()
        ctl = _AssessingController(False)
        await _spec_retriever(_registry_with(_SqlTool()), ctl, synth).resolve("q", NS, options=self.OPTS)
        # The loop ran, and the answer was written once more after it.
        assert ctl.steps_allowed == [5] and synth.calls >= 1

    @pytest.mark.asyncio
    async def test_not_used_for_streamed_answers(self):
        synth = _CountingSynth()
        ctl = _AssessingController(True)

        async def on_token(_t):
            return None

        await _spec_retriever(_registry_with(_SqlTool()), ctl, synth).resolve(
            "q", NS, options=self.OPTS, on_token=on_token
        )
        assert synth.calls == 1 and ctl.assessed == ["q"]

    @pytest.mark.asyncio
    async def test_guard_already_holding_skips_the_check(self, monkeypatch):
        import coa_serve.tier3.agentic.retriever as retriever_module

        monkeypatch.setattr(retriever_module, "insufficiency_reason", lambda ctx: "read the rest")
        synth = _CountingSynth()
        ctl = _AssessingController(True)
        trace = TraceCollector()
        await _spec_retriever(_registry_with(_SqlTool()), ctl, synth).resolve("q", NS, options=self.OPTS, trace=trace)
        assert ctl.assessed == [] and ctl.steps_allowed == [5] and synth.calls == 1
        assert [s.detail for s in trace.steps if s.step == "prefetch_done"] == ["guard held; running the loop"]

    @pytest.mark.asyncio
    async def test_off_without_the_option(self):
        synth = _CountingSynth()
        ctl = _AssessingController(False)
        await _spec_retriever(_registry_with(_SqlTool()), ctl, synth).resolve(
            "q", NS, options={"prefetchMode": "assess"}
        )
        assert synth.calls == 1

    def test_option_validated(self):
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"fastPathSpeculation": "yes"})
