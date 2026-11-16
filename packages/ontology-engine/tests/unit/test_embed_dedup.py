# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for per-channel embedding-text dedup.

``BedrockEmbeddingClient.generate_lexical`` embeds each unique channel text once
and scatters the vector back to every original position.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient
from coa_ontology.inducer.services.pipeline import InductionPipeline, SourceConcept


class _RecordingEmbedder:
    """Deterministic mock: vector derived from the text; records every batch.

    ``embed_documents`` returns a fresh, distinct vector per text (hash-derived so
    equal texts get equal vectors) and appends the exact input list it was called
    with to ``self.batches`` so tests can assert the dedup collapse.
    """

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        # Content-derived (sum of code points), NOT length-derived: distinct texts
        # of equal length must get distinct vectors, or the order/misalignment
        # guards below go vacuous (e.g. "alpha "/"gamma " both len 6 would collide).
        base = float(sum(ord(ch) for ch in text)) + 0.123
        return [base, base * 2.0, base * 3.0, base * 4.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [self._vec(t) for t in texts]


def _classes(labels: list[str]) -> list[dict]:
    return [{"class_uri": f"source:{i}", "labels": [lbl], "comments": []} for i, lbl in enumerate(labels)]


def _embed_text(label: str) -> str:
    """Reproduce generate_lexical's text formula for a single-label, no-comment
    class: ``" ".join(labels) + " " + " ".join(comments)`` → ``"<label> "``."""
    return f"{label} "


def _client_with(embedder: _RecordingEmbedder) -> BedrockEmbeddingClient:
    client = BedrockEmbeddingClient.__new__(BedrockEmbeddingClient)
    client.model_id = "mock-model"
    client.dimensions = 4
    client._embedder = embedder
    return client


def test_generate_lexical_duplicate_texts_returns_results_in_input_order():
    """Duplicate inputs → one result per class, each vector equal to the single
    embed of its own text, in original order (scatter must not misalign)."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)
    labels = ["alpha", "beta", "alpha", "gamma", "beta"]

    results, model_id = client.generate_lexical("", _classes(labels))

    assert model_id == "mock-model"
    assert len(results) == len(labels)
    for result, lbl in zip(results, labels, strict=True):
        assert result["vector"] == embedder._vec(_embed_text(lbl))


def test_generate_lexical_dispatches_only_distinct_texts():
    """embed_documents is called with exactly the distinct texts (the win)."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)
    labels = ["alpha", "beta", "alpha", "gamma", "beta"]

    client.generate_lexical("", _classes(labels))

    assert len(embedder.batches) == 1
    dispatched = embedder.batches[0]
    assert dispatched == [_embed_text("alpha"), _embed_text("beta"), _embed_text("gamma")]  # insertion-ordered
    assert len(dispatched) == len(set(dispatched))


def test_generate_lexical_all_identical_texts_embeds_once():
    """The real dtype-channel case: every column shares one text → embed_documents
    dispatched exactly one text, all results present and equal."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)
    labels = ["unknown"] * 500

    results, _ = client.generate_lexical("", _classes(labels))

    assert embedder.batches == [[_embed_text("unknown")]]  # dispatched a single text
    assert len(results) == 500
    expected = embedder._vec(_embed_text("unknown"))
    assert all(r["vector"] == expected for r in results)


def test_generate_lexical_single_element():
    """Single class is a no-op for dedup: one dispatch, one result."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)

    results, _ = client.generate_lexical("", _classes(["solo"]))

    assert embedder.batches == [[_embed_text("solo")]]
    assert len(results) == 1
    assert results[0]["vector"] == embedder._vec(_embed_text("solo"))


def test_generate_lexical_empty_classes():
    """No classes → no dispatch, empty results (guard against zip/scatter crash)."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)

    results, _ = client.generate_lexical("", [])

    assert results == []
    assert embedder.batches == [[]]


def test_generate_lexical_propagates_embedder_failure():
    """Embedder failure mid-dedup must propagate, not be swallowed or masked."""

    class _FailingEmbedder(_RecordingEmbedder):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("bedrock down")

    client = _client_with(_FailingEmbedder())

    with pytest.raises(RuntimeError, match="bedrock down"):
        client.generate_lexical("", _classes(["alpha", "beta"]))


def _reference_fused(name_vec: list[float], desc_vec: list[float], dtype_vec: list[float]) -> list[float]:
    """Historical name→desc→dtype weighted sum (float add is not associative)."""
    fused = [0.0] * len(name_vec)
    for weight, vec in ((0.6, name_vec), (0.2, desc_vec), (0.2, dtype_vec)):
        for j, v in enumerate(vec):
            fused[j] += weight * v
    return fused


def test_embed_concepts_with_dedup_matches_hand_computed_fused_reference():
    """Run the real dedup generate_lexical through the fusion loop on columns whose
    dtype channel is all-identical, and assert each fused concept.vector equals the
    hand-computed weighted sum."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)
    pipeline = InductionPipeline(None, client, None)
    concepts = [
        SourceConcept(
            table_fqn="db.orders",
            table_name="orders",
            column_name=col,
            # Distinct, non-empty description → desc is a genuine THIRD channel
            # vector (not a name fallback), so the 0.6/0.2/0.2 fold is
            # non-degenerate and a name/desc weight swap would change the result.
            description=f"description of {col}",
            data_type="INT",  # identical dtype across every column → single dispatch
            embedding_text=col,
        )
        for col in ("order_id", "customer_id", "order_id")
    ]

    out, _ = pipeline.embed_concepts(concepts)

    for concept in out:
        name_text = _embed_text(concept.column_name.replace("_", " "))
        desc_text = _embed_text(f"description of {concept.column_name}")
        name_vec = embedder._vec(name_text)
        expected = _reference_fused(
            name_vec=name_vec,
            desc_vec=embedder._vec(desc_text),
            dtype_vec=embedder._vec(_embed_text("INT")),
        )
        assert concept.vector == expected
        # Non-vacuity: the fused vector must differ from any single channel's raw
        # contribution (mirrors test_memory_reducers' `!= _NAME_VEC` guard).
        assert concept.vector != name_vec


def test_embed_concepts_dtype_channel_dispatched_single_text():
    """The dtype channel (all "INT") must dispatch exactly one text even though
    three columns feed it — proving the per-channel collapse under real fusion."""
    embedder = _RecordingEmbedder()
    client = _client_with(embedder)
    pipeline = InductionPipeline(None, client, None)
    concepts = [
        SourceConcept(
            table_fqn="db.orders",
            table_name="orders",
            column_name=col,
            description="",
            data_type="INT",
            embedding_text=col,
        )
        for col in ("a", "b", "c")
    ]

    pipeline.embed_concepts(concepts)

    # name, desc, dtype channels dispatched in order; dtype is all-identical.
    assert embedder.batches[-1] == [_embed_text("INT")]
