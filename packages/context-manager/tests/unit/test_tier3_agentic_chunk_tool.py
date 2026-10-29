# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for source-provenanced agentic document-window evidence."""

from __future__ import annotations

import pytest
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.templates import CHUNK_WINDOW_EXPANSION
from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool


class _FakeStore:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute_query_with_retry(self, cypher: str, params: dict) -> list[dict]:
        self.calls.append((cypher, params))
        return self.rows


@pytest.mark.unit
async def test_document_window_evidence_retains_source_and_becomes_supporting_content(monkeypatch):
    monkeypatch.setattr(
        ChunkExpansionTool,
        "_format_label",
        staticmethod(lambda namespace, label: f"`{label}{namespace}__`"),
    )
    tool = ChunkExpansionTool("neptune-db://fake-host:8182")
    store = _FakeStore(
        [
            {
                "chunkId": "chunk-adjacent",
                "text": "The adjacent page contains the requested figure.",
                "sourceDocumentId": "aws:tenant:source:document-42",
                "sourceS3Key": "tenant/raw/source/annual-report.pdf",
                "sourceFilename": "annual-report.md",
            }
        ]
    )
    tool._store = store

    result = await tool.invoke(namespace="retail", chunk_ids=["chunk-seed"])

    cypher, params = store.calls[0]
    assert "`__Source__retail__`" in cypher
    assert "__EXTRACTED_FROM__" in cypher
    assert params == {"chunk_ids": ["chunk-seed"]}
    assert result.chunks[0].source_doc == "aws:tenant:source:document-42"
    assert result.chunks[0].source_doc_name == "annual-report.pdf"

    context = AccumulatedContext()
    context.add(result)
    assert AgenticRetriever._supporting_content(context) == (
        {
            "chunkId": "chunk-adjacent",
            "text": "The adjacent page contains the requested figure.",
            "sourceDocumentId": "aws:tenant:source:document-42",
            "sourceDocumentName": "annual-report.pdf",
            "relevanceScore": 1.0,
            # Deprecated aliases kept for one release (see #984).
            "sourceDoc": "aws:tenant:source:document-42",
            "label": "",
        },
    )


@pytest.mark.unit
async def test_document_window_row_without_source_is_not_used_for_synthesis(monkeypatch):
    monkeypatch.setattr(
        ChunkExpansionTool,
        "_format_label",
        staticmethod(lambda namespace, label: f"`{label}{namespace}__`"),
    )
    tool = ChunkExpansionTool("neptune-db://fake-host:8182")
    tool._store = _FakeStore([{"chunkId": "chunk-adjacent", "text": "Anonymous text"}])

    result = await tool.invoke(namespace="retail", chunk_ids=["chunk-seed"])

    assert result.chunks == ()
    assert result.degraded_reason == "missing_document_provenance"
    assert "dropped 1 anonymous or malformed document chunk(s)" in result.detail


@pytest.mark.unit
def test_chunk_window_template_joins_the_source_optionally():
    # The join itself, not a monkeypatched row set. As a mandatory MATCH this turned
    # a tenant whose graph lacks `__EXTRACTED_FROM__`/`__Source__` into zero rows —
    # `dropped_chunks` 0, `degraded_reason` empty, trace status "succeeded" — so
    # chunk-window expansion silently contributed nothing for that namespace.
    cypher = CHUNK_WINDOW_EXPANSION.format(
        chunk_label="`__Chunk__retail__`",
        source_label="`__Source__retail__`",
        hops=1,
        limit=50,
    )

    assert "OPTIONAL MATCH (adj)-[:`__EXTRACTED_FROM__`]->(source:`__Source__retail__`)" in cypher
    # The adjacency hop stays mandatory: no neighbours genuinely means no neighbours.
    assert "MATCH (c)-[:`__NEXT__`|`__PREVIOUS__`*1..1]-(adj:`__Chunk__retail__`)" in cypher
    # Exactly one non-optional source join must not survive anywhere in the query.
    assert "\nMATCH (adj)-[:`__EXTRACTED_FROM__`]" not in cypher


@pytest.mark.unit
async def test_namespace_graph_without_the_source_shape_degrades_instead_of_succeeding_empty(monkeypatch):
    # What the OPTIONAL join buys: the adjacency rows still arrive, every one of them
    # with a null source (the shape Neptune returns for an unmatched OPTIONAL MATCH),
    # so the schema mismatch is counted and named instead of looking like a healthy
    # "0 adjacent chunks" result.
    monkeypatch.setattr(
        ChunkExpansionTool,
        "_format_label",
        staticmethod(lambda namespace, label: f"`{label}{namespace}__`"),
    )
    tool = ChunkExpansionTool("neptune-db://fake-host:8182")
    tool._store = _FakeStore(
        [
            {
                "chunkId": "chunk-before",
                "text": "Text before the seed.",
                "sourceDocumentId": None,
                "sourceS3Key": None,
                "sourceFilename": None,
            },
            {
                "chunkId": "chunk-after",
                "text": "Text after the seed.",
                "sourceDocumentId": None,
                "sourceS3Key": None,
                "sourceFilename": None,
            },
        ]
    )

    result = await tool.invoke(namespace="retail", chunk_ids=["chunk-seed"])

    assert result.chunks == ()
    assert result.degraded_reason == "missing_document_provenance"
    assert "dropped 2 anonymous or malformed document chunk(s)" in result.detail
    assert "__EXTRACTED_FROM__" in result.detail


@pytest.mark.unit
async def test_document_window_keeps_valid_chunks_and_reports_deterministic_drop_count(monkeypatch):
    monkeypatch.setattr(
        ChunkExpansionTool,
        "_format_label",
        staticmethod(lambda namespace, label: f"`{label}{namespace}__`"),
    )
    tool = ChunkExpansionTool("neptune-db://fake-host:8182")
    tool._store = _FakeStore(
        [
            {
                "chunkId": "valid",
                "text": "Document-backed evidence.",
                "sourceDocumentId": "source-1",
                "sourceFilename": "report.pdf",
            },
            {"chunkId": "anonymous", "text": "Missing source provenance."},
            {"chunkId": "", "text": "Missing identifier.", "sourceDocumentId": "source-1"},
        ]
    )

    result = await tool.invoke(namespace="retail", chunk_ids=["chunk-seed"])

    assert [chunk.chunk_id for chunk in result.chunks] == ["valid"]
    assert result.degraded_reason == "missing_document_provenance"
    assert result.detail.endswith("dropped 2 anonymous or malformed document chunk(s)")
