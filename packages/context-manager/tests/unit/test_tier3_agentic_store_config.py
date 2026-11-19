# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The agentic toolkit-store tools apply serve's graphrag config before building a store.

``GraphRAGConfig`` is a process singleton and is now configured lazily (on first
toolkit use) rather than when the lexical retriever is constructed at startup, so a
graph or chunk tool that builds the process's first toolkit store must apply it.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool
from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool

_URI = "neptune-db://fake-host:8182"


@pytest.mark.unit
@pytest.mark.parametrize("tool_cls", [ChunkExpansionTool, GraphTraversalTool])
def test_get_store_configures_graphrag_before_building_store(tool_cls):
    order: list[str] = []
    tool = tool_cls(_URI)

    with (
        patch(
            "coa_serve.lexical.baseline_retriever.ensure_graphrag_configured",
            side_effect=lambda: order.append("configured"),
        ),
        patch(
            "graphrag_toolkit.lexical_graph.storage.GraphStoreFactory.for_graph_store",
            side_effect=lambda uri: order.append(f"store:{uri}") or object(),
        ),
    ):
        store = tool._get_store()
        assert tool._get_store() is store

    assert order == ["configured", f"store:{_URI}"]


@pytest.mark.unit
@pytest.mark.parametrize("tool_cls", [ChunkExpansionTool, GraphTraversalTool])
@pytest.mark.asyncio
async def test_ensure_store_builds_store_off_the_event_loop_thread(tool_cls):
    """The first-use toolkit import takes seconds; it must not block the event loop."""
    loop_thread = threading.get_ident()
    build_threads: list[int] = []
    tool = tool_cls(_URI)

    with (
        patch("coa_serve.lexical.baseline_retriever.ensure_graphrag_configured"),
        patch(
            "graphrag_toolkit.lexical_graph.storage.GraphStoreFactory.for_graph_store",
            side_effect=lambda uri: build_threads.append(threading.get_ident()) or object(),
        ),
    ):
        store = await tool._ensure_store()
        assert await tool._ensure_store() is store

    assert len(build_threads) == 1
    assert build_threads[0] != loop_thread
