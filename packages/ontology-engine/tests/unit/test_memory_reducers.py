# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for memory reducers: embed_concepts channel fusion and induce vector clearing."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import httpx
from coa_common.bedrock_metrics import CostTracker
from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable
from coa_ontology.inducer.services.pipeline import InductionPipeline, SourceConcept
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from opensearchpy.exceptions import OpenSearchException
from rdflib import OWL, RDF
from rdflib.compare import isomorphic

# Channel → per-column vector. NON-round values on purpose: with 1.0/0.0 the
# weighted sum is associativity-invariant and the byte-equality test is vacuous.
_NAME_VEC = [0.1, 0.7, 0.3, 0.9]
_DESC_VEC = [0.2, 0.4, 0.6, 0.8]
_DTYPE_VEC = [0.15, 0.35, 0.55, 0.75]
_CHANNEL_MODEL_IDS = {"name": "model-name", "desc": "model-desc", "dtype": "model-dtype"}


class _LiveListTracker:
    """Counts how many _TrackedList result lists are simultaneously alive."""

    def __init__(self):
        self.live = 0
        self.max_live = 0

    def acquire(self):
        self.live += 1
        self.max_live = max(self.max_live, self.live)

    def release(self):
        self.live -= 1


class _TrackedList(list):
    def __init__(self, items, tracker: _LiveListTracker | None):
        super().__init__(items)
        self._tracker = tracker
        if tracker:
            tracker.acquire()

    def __del__(self):
        if self._tracker:
            self._tracker.release()


class _ChannelDispatchGenerator:
    """Deterministic per-channel dispatch by call order: name, desc, dtype.

    ``embed_concepts`` fetches the three column channels in dict-insertion order
    (name → desc → dtype). We return the matching fixed vector + model_id by
    call index, independent of label heuristics.
    """

    _ORDER = [("name", _NAME_VEC), ("desc", _DESC_VEC), ("dtype", _DTYPE_VEC)]

    def __init__(self, tracker: _LiveListTracker | None = None):
        self._calls = 0
        self._tracker = tracker

    def generate_lexical(self, ontology_id, classes, backend=None, store=True):
        # Table-level embedding (single call before column channels) — the test
        # feeds only column concepts, so every call is a column channel.
        channel, vec = self._ORDER[self._calls]
        self._calls += 1
        results = _TrackedList([{"vector": list(vec)} for _ in classes], self._tracker)
        return results, _CHANNEL_MODEL_IDS[channel]


def _col_concepts(n: int = 3) -> list[SourceConcept]:
    return [
        SourceConcept(
            table_fqn="db.orders",
            table_name="orders",
            column_name=f"col_{i}",
            description=f"desc {i}",
            data_type="INT",
            embedding_text=f"col {i}",
        )
        for i in range(n)
    ]


def _reference_fused() -> list[float]:
    """The historical 3-channel computation for one column, name→desc→dtype."""
    fused = [0.0] * len(_NAME_VEC)
    for weight, vec in ((0.6, _NAME_VEC), (0.2, _DESC_VEC), (0.2, _DTYPE_VEC)):
        for j, v in enumerate(vec):
            fused[j] += weight * v
    return fused


def test_embed_concepts_fusion_is_byte_identical_to_reference():
    """Incremental fusion must produce byte-for-byte the same fused vector as
    the old dict-based 3-channel weighted sum (float add is not associative)."""
    concepts = _col_concepts(2)
    pipeline = InductionPipeline(None, _ChannelDispatchGenerator(), None)

    out, model_id = pipeline.embed_concepts(concepts)

    reference = _reference_fused()
    for c in out:
        assert c.vector == reference  # exact float equality, not approx
        # Guard against the vacuous case: the fused value must actually differ
        # from any single channel's contribution.
        assert c.vector != _NAME_VEC


def test_embed_concepts_returns_dtype_channel_model_id():
    """The returned model_id is the LAST (dtype) channel's — matches the prior
    behavior where model_id was reassigned on each channel call."""
    concepts = _col_concepts(1)
    pipeline = InductionPipeline(None, _ChannelDispatchGenerator(), None)

    _out, model_id = pipeline.embed_concepts(concepts)

    assert model_id == _CHANNEL_MODEL_IDS["dtype"]


def test_embed_concepts_holds_at_most_one_channel_list_alive():
    """The incremental path must ``del`` each channel's result list before
    fetching the next → never more than one channel list resident at once."""
    import gc

    tracker = _LiveListTracker()
    concepts = _col_concepts(3)
    pipeline = InductionPipeline(None, _ChannelDispatchGenerator(tracker), None)

    pipeline.embed_concepts(concepts)
    gc.collect()

    assert tracker.max_live == 1


def _make_table(name: str = "orders") -> CatalogTable:
    return CatalogTable(
        id=f"test.{name}",
        name=name,
        fullyQualifiedName=f"test_db.public.{name}",
        description=f"The {name} table",
        columns=[
            CatalogColumn(name="order_id", dataType="INT", constraint="PRIMARY_KEY"),
            CatalogColumn(name="total_amount", dataType="DECIMAL", description="Order total"),
        ],
        tableConstraints=None,
    )


class _FakePipeline:
    """Real extractor, stubbed embed/match — leaves vectors set on concepts so
    the test can prove induce() clears them."""

    def __init__(self, matches: list[ConceptMatch]):
        self._matches = matches
        self.seen_concepts: list[SourceConcept] | None = None

    def extract_concepts(self, tables):
        return InductionPipeline(None, None, None).extract_concepts(tables)

    def embed_concepts(self, concepts, backend=None):
        for c in concepts:
            c.vector = [0.1, 0.2, 0.3]  # non-empty, as production leaves them
        self.seen_concepts = concepts
        return concepts, "model-x"

    def match_concepts(self, concepts, *a, **k):
        # Vectors are still populated at match time (matcher reads them).
        assert all(c.vector for c in concepts)
        return self._matches


def test_induce_clears_vectors_after_match_without_affecting_output():
    """After match, every concept.vector is empty, yet matches, the proposal
    graph, and the novel-table set are unaffected."""
    table = _make_table()
    matches = [
        ConceptMatch(
            source_column="",
            source_table="orders",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        )
    ]
    fake = _FakePipeline(matches)

    graph, novel_tables, out_matches, dropped = TableToOntologyStrategy().induce(
        tables=[table],
        ontology_uri_prefix="http://ex.org/induced#",
        config={},
        pipeline=fake,
    )

    # Vectors freed on every concept the pipeline produced.
    assert fake.seen_concepts is not None
    assert all(c.vector == [] for c in fake.seen_concepts)

    # Downstream output is unaffected.
    assert out_matches == matches
    assert dropped == []  # table_to_ontology never drops tables
    assert "orders" in novel_tables
    next(graph.subjects(RDF.type, OWL.Class))


def test_induce_clears_vectors_on_all_novel_fallback():
    """Vector clearing must also hold on the all-novel fallback (match_concepts raised)."""

    class _RaisingPipeline(_FakePipeline):
        def match_concepts(self, concepts, *a, **k):
            raise RuntimeError("recall blew up")

    fake = _RaisingPipeline([])
    table = _make_table()

    _graph, novel_tables, matches, dropped = TableToOntologyStrategy().induce(
        tables=[table],
        ontology_uri_prefix="http://ex.org/induced#",
        config={},
        pipeline=fake,
    )

    assert all(m.match_type == "novel" for m in matches)
    assert "orders" in novel_tables
    assert fake.seen_concepts is not None
    assert all(c.vector == [] for c in fake.seen_concepts)
    assert dropped == []


def _make_tables(n: int) -> list[CatalogTable]:
    """n distinct tables, each 1 table-level + 2 column concepts = 3 concepts."""
    return [
        CatalogTable(
            id=f"test.table_{i}",
            name=f"table_{i}",
            fullyQualifiedName=f"test_db.public.table_{i}",
            description=f"Table {i}",
            columns=[
                CatalogColumn(name="pk_id", dataType="INT", constraint="PRIMARY_KEY"),
                CatalogColumn(name="value_col", dataType="VARCHAR", description="a value"),
            ],
            tableConstraints=None,
        )
        for i in range(n)
    ]


_CONCEPTS_PER_TABLE = 3  # 1 table-level + 2 columns (see _make_tables)


class _BatchFakePipeline:
    """Deterministic pipeline whose match result depends ONLY on each concept's
    own table (no cross-table state) — so batched matches must equal whole-catalog
    matches. Tracks peak resident vectors (concepts whose .vector is non-empty)
    across ALL batches seen, the true proxy for resident vector memory (the
    ~32 KB list[float], not the tiny SourceConcept object).

    ``raise_on_call``/``raise_exc`` inject a failure on the Nth match_concepts
    call (== the Nth batch, since induce processes batches in order) to exercise
    the per-batch fail-loud paths.
    """

    def __init__(self, raise_on_call: int | None = None, raise_exc: Exception | None = None):
        self.all_seen: list[SourceConcept] = []
        self.max_live_vectors = 0
        self.match_calls = 0
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc

    def extract_concepts(self, tables):
        return InductionPipeline(None, None, None).extract_concepts(tables)

    def embed_concepts(self, concepts, backend=None):
        for c in concepts:
            c.vector = [0.1, 0.2, 0.3]  # populate as production does
        self.all_seen.extend(concepts)
        # Peak = concepts with a live vector across every batch seen so far.
        # induce clears the prior batch (.vector = []) before this one embeds,
        # so under batching this equals ONE batch; unbatched it equals ALL.
        live = sum(1 for c in self.all_seen if c.vector)
        self.max_live_vectors = max(self.max_live_vectors, live)
        return concepts, "model-x"

    def match_concepts(self, concepts, *a, **k):
        self.match_calls += 1
        if self._raise_on_call is not None and self.match_calls == self._raise_on_call:
            assert self._raise_exc is not None
            raise self._raise_exc
        out: list[ConceptMatch] = []
        for c in concepts:
            if c.is_table_level:
                idx = int(c.table_name.split("_")[-1])
                if idx % 2 == 0:  # even tables ground, odd stay novel
                    out.append(
                        ConceptMatch(
                            source_column="",
                            source_table=c.table_name,
                            matched_class_uri=f"http://found.org/onto#Class{idx}",
                            matched_ontology_id="found",
                            similarity=0.9,
                            match_type="high_confidence",
                        )
                    )
                else:
                    out.append(
                        ConceptMatch(
                            source_column="",
                            source_table=c.table_name,
                            matched_class_uri=None,
                            matched_ontology_id=None,
                            similarity=None,
                            match_type="novel",
                        )
                    )
            else:
                out.append(
                    ConceptMatch(
                        source_column=c.column_name,
                        source_table=c.table_name,
                        matched_class_uri=None,
                        matched_ontology_id=None,
                        similarity=None,
                        match_type="novel",
                    )
                )
        return out


def _induce(tables, pipeline):
    graph, novel, matches, _dropped = TableToOntologyStrategy().induce(
        tables=tables,
        ontology_uri_prefix="http://ex.org/induced#",
        config={},
        pipeline=pipeline,
    )
    return graph, novel, matches


def test_batched_induce_peak_vectors_flat_in_table_count(monkeypatch):
    """CORE bound: peak resident vectors must be O(batch), independent of the
    table count. Runs the same batch size against 10 and 100 tables and asserts
    the peak is FLAT (constant). Fails against unmodified induce(), which embeds
    ALL concepts at once so the peak scales with N (30 vs 300, not flat)."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")

    fake_small = _BatchFakePipeline()
    _induce(_make_tables(10), fake_small)

    fake_large = _BatchFakePipeline()
    _induce(_make_tables(100), fake_large)

    # One batch = 5 tables × 3 concepts = 15 vectors resident at peak.
    assert fake_small.max_live_vectors == 5 * _CONCEPTS_PER_TABLE
    # FLAT: 10× the tables, identical peak — the whole point of the fix.
    assert fake_large.max_live_vectors == fake_small.max_live_vectors
    # And bounded far below the whole-catalog total (300 at 100 tables).
    assert fake_large.max_live_vectors < 100 * _CONCEPTS_PER_TABLE


def test_batched_induce_output_identical_to_single_batch(monkeypatch):
    """Behavior preservation: batched induce() yields identical matches (content,
    order-independent), the identical novel-table set, and an isomorphic proposal
    graph vs a single whole-catalog batch."""
    tables = _make_tables(10)

    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "3")
    g_batched, novel_batched, matches_batched = _induce(tables, _BatchFakePipeline())

    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "100000")  # one batch
    g_single, novel_single, matches_single = _induce(tables, _BatchFakePipeline())

    def _key(m: ConceptMatch):
        return (m.source_table, m.source_column)

    assert sorted(matches_batched, key=_key) == sorted(matches_single, key=_key)
    assert novel_batched == novel_single
    assert isomorphic(g_batched, g_single)
    # Non-trivial: some grounded, some novel — guards against a vacuous compare.
    assert any(m.match_type == "high_confidence" for m in matches_batched)
    assert {"table_1", "table_3", "table_5", "table_7", "table_9"} == novel_batched


def test_batched_induce_opensearch_error_propagates(monkeypatch):
    """Fail-loud: a batch whose match_concepts raises OpenSearchException must
    propagate (whole job fails) — an incomplete recall must NOT be silently
    dropped as one skipped batch."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    fake = _BatchFakePipeline(raise_on_call=2, raise_exc=OpenSearchException("recall truncated"))

    with pytest.raises(OpenSearchException):
        _induce(_make_tables(10), fake)


def test_batched_induce_plain_exception_falls_back_that_batch_only(monkeypatch):
    """Fail-loud: a plain Exception in one batch falls back to all-novel FOR THAT
    BATCH ONLY; earlier/later batches keep their real matches."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    fake = _BatchFakePipeline(raise_on_call=2, raise_exc=RuntimeError("bug in batch 2"))

    _g, novel, matches = _induce(_make_tables(10), fake)
    mm = {(m.source_table, m.source_column): m for m in matches}

    # Batch 1 (tables 0-4) unaffected: even tables still grounded.
    assert mm[("table_0", "")].match_type == "high_confidence"
    assert mm[("table_0", "")].matched_class_uri is not None
    # Batch 2 (tables 5-9) all-novel — even table_6/table_8 that WOULD ground.
    for i in range(5, 10):
        assert mm[(f"table_{i}", "")].match_type == "novel"
    assert mm[("table_6", "")].matched_class_uri is None
    assert {f"table_{i}" for i in range(5, 10)} <= novel
    assert "table_0" not in novel  # grounded batch-1 table preserved


class _StageFailPipeline(_BatchFakePipeline):
    """Raises ``raise_exc`` from the given stage on the Nth batch.

    ``embed_populates`` mirrors production embed_concepts partially populating
    vectors before it blows up, so the fallback path's vector freeing is exercised.
    """

    def __init__(self, stage: str, batch: int, exc: Exception, embed_populates: bool = False):
        super().__init__()
        self._stage = stage
        self._batch = batch
        self._exc = exc
        self._embed_populates = embed_populates
        self.extract_calls = 0
        self.embed_calls = 0

    def extract_concepts(self, tables):
        self.extract_calls += 1
        if self._stage == "extract" and self.extract_calls == self._batch:
            raise self._exc
        return super().extract_concepts(tables)

    def embed_concepts(self, concepts, backend=None):
        self.embed_calls += 1
        if self._stage == "embed" and self.embed_calls == self._batch:
            if self._embed_populates:
                for c in concepts:
                    c.vector = [0.1, 0.2, 0.3]
                self.all_seen.extend(concepts)
            raise self._exc
        return super().embed_concepts(concepts, backend=backend)


@pytest.mark.parametrize("stage", ["extract", "embed"])
@pytest.mark.parametrize(
    "exc",
    [OpenSearchException("recall truncated"), httpx.HTTPError("bedrock transport died")],
    ids=["opensearch", "httpx"],
)
def test_batched_induce_transport_error_in_extract_or_embed_propagates(monkeypatch, stage, exc):
    """A transport error from extract/embed must fail the WHOLE job, not escape the
    loop leaving the caller to build an ontology from a partial all_matches. Fails
    if extract/embed are moved back outside the try (the exception then propagates
    as an *unhandled* escape rather than the deliberate re-raise — and, worse, any
    non-transport error would silently truncate the catalog)."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    fake = _StageFailPipeline(stage, batch=2, exc=exc)

    with pytest.raises(type(exc)):
        _induce(_make_tables(10), fake)


@pytest.mark.parametrize("stage", ["extract", "embed"])
def test_batched_induce_nontransport_error_in_extract_or_embed_falls_back(monkeypatch, stage):
    """A non-transport error in extract/embed degrades THAT batch to all-novel and
    the job completes with the other batches' real matches intact. The fallback is
    built from table_batch, so it is complete even when extract_concepts raised
    before any concept existed."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    fake = _StageFailPipeline(stage, batch=2, exc=RuntimeError(f"bug in {stage}"), embed_populates=True)

    _g, novel, matches = _induce(_make_tables(10), fake)
    mm = {(m.source_table, m.source_column): m for m in matches}

    # Batch 1 (tables 0-4) keeps its real matches — even tables still grounded.
    assert mm[("table_0", "")].match_type == "high_confidence"
    assert mm[("table_0", "")].matched_class_uri is not None
    assert "table_0" not in novel

    # Batch 2 (tables 5-9) all-novel, and COMPLETE: the table-level concept plus
    # both columns, same shape extract_concepts would have produced.
    for i in range(5, 10):
        for col in ("", "pk_id", "value_col"):
            assert mm[(f"table_{i}", col)].match_type == "novel"
            assert mm[(f"table_{i}", col)].matched_class_uri is None
    assert {f"table_{i}" for i in range(5, 10)} <= novel

    # Vectors the failing stage had already populated are still freed.
    assert all(c.vector == [] for c in fake.all_seen)


@pytest.mark.parametrize("stage", ["extract", "embed", "match"])
def test_batched_induce_memory_error_propagates(monkeypatch, stage):
    """MemoryError subclasses Exception, so the all-novel fallback would swallow it.
    Bounding peak memory is the whole point of batching — an OOM must fail loud."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    if stage == "match":
        fake = _BatchFakePipeline(raise_on_call=2, raise_exc=MemoryError("out of memory"))
    else:
        fake = _StageFailPipeline(stage, batch=2, exc=MemoryError("out of memory"))

    with pytest.raises(MemoryError):
        _induce(_make_tables(10), fake)


def test_batched_induce_frees_all_vectors(monkeypatch):
    """Under batching, every concept's vector is empty after induce, across
    all batches (including a final short batch)."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "4")
    fake = _BatchFakePipeline()

    _induce(_make_tables(9), fake)  # 9 = 2 full batches + 1 short

    assert fake.all_seen
    assert all(c.vector == [] for c in fake.all_seen)


class _ColumnMatchTimingPipeline:
    """Fake whose match_concepts records a KNOWN per-batch ColumnMatch duration
    into a real CostTracker exactly as production does (one record_stage_duration
    call per invocation, last-writer-wins). Batch k records 100*k ms, so the true
    SUM is distinguishable from the last-batch value and from any partial sum."""

    def __init__(self):
        self.match_calls = 0

    def extract_concepts(self, tables):
        return InductionPipeline(None, None, None).extract_concepts(tables)

    def embed_concepts(self, concepts, backend=None):
        for c in concepts:
            c.vector = [0.1, 0.2, 0.3]
        return concepts, "model-x"

    def match_concepts(self, concepts, *a, cost_tracker=None, **k):
        self.match_calls += 1
        # Mirror production match_concepts: record ColumnMatch once per call.
        if cost_tracker is not None:
            cost_tracker.record_stage_duration("ColumnMatch", 100.0 * self.match_calls)
        return [
            ConceptMatch(
                source_column=c.column_name,
                source_table=c.table_name,
                matched_class_uri=None,
                matched_ontology_id=None,
                similarity=None,
                match_type="novel",
            )
            for c in concepts
        ]


def test_batched_induce_aggregates_column_match_duration_across_batches(monkeypatch):
    """Observability: the emitted ColumnMatch stage duration must be the SUM
    across fusion batches, not the last batch. 3 batches record 100+200+300ms →
    the CostTracker must hold 600, not 300 (last), nor 500 (last two)."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    tracker = CostTracker()
    fake = _ColumnMatchTimingPipeline()

    TableToOntologyStrategy().induce(
        tables=_make_tables(15),  # 15 tables / batch 5 = 3 batches
        ontology_uri_prefix="http://ex.org/induced#",
        config={},
        pipeline=fake,
        cost_tracker=tracker,
    )

    assert fake.match_calls == 3  # guard: the test actually exercised ≥2 batches
    # SUM (100+200+300), NOT 300 (last-writer-wins) nor 500 (last two).
    assert tracker.stage_durations_ms["ColumnMatch"] == 600.0


def test_batched_induce_column_match_duration_no_double_count_on_fallback(monkeypatch):
    """A batch whose match_concepts raises (all-novel fallback) records no
    ColumnMatch, so its contribution is 0 — the prior batch's value must NOT be
    counted twice. Pins the per-batch reset: without it the stale last-batch value
    carries over and the aggregate reads 200 instead of the true 100."""
    monkeypatch.setenv("INDUCER_TABLE_BATCH_SIZE", "5")
    tracker = CostTracker()

    class _FallbackOnSecondBatch(_ColumnMatchTimingPipeline):
        def match_concepts(self, concepts, *a, cost_tracker=None, **k):
            self.match_calls += 1
            if self.match_calls == 1:
                # Real column-match work recorded (100ms) as production's finally does.
                if cost_tracker is not None:
                    cost_tracker.record_stage_duration("ColumnMatch", 100.0)
                return [
                    ConceptMatch(
                        source_column=c.column_name,
                        source_table=c.table_name,
                        matched_class_uri=None,
                        matched_ontology_id=None,
                        similarity=None,
                        match_type="novel",
                    )
                    for c in concepts
                ]
            # Second batch blows up before recording → induce() falls back all-novel.
            raise RuntimeError("bug in batch 2")

    fake = _FallbackOnSecondBatch()
    TableToOntologyStrategy().induce(
        tables=_make_tables(10),  # 2 batches of 5
        ontology_uri_prefix="http://ex.org/induced#",
        config={},
        pipeline=fake,
        cost_tracker=tracker,
    )

    assert fake.match_calls == 2  # guard: both batches ran, second fell back
    # Only batch 1 recorded (100ms); batch 2 recorded nothing → total is 100, not 200.
    assert tracker.stage_durations_ms["ColumnMatch"] == 100.0
