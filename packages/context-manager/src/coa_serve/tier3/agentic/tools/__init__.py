# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Agentic Retriever internal tool layer.

This subpackage defines the uniform :class:`Tool` contract
(:mod:`~coa_serve.tier3.agentic.tools.base`) and the internal-only
:class:`ToolRegistry` (:mod:`~coa_serve.tier3.agentic.tools.registry`)
the reasoning controller uses to discover and invoke Tools.

Import discipline (Requirement 12.3): module load of anything under
``tier3.agentic`` — including this tool layer — MUST NOT import
``graphrag_toolkit``. The base contract and the registry are dependency-free
(stdlib/typing + structlog only); concrete tools that touch the toolkit keep
that access lazy, inside their ``invoke`` methods.

The registry and the Tools it holds are never exposed outside the Serve_Layer
(Requirement 2.7): there is no transport or route that lists or invokes Tools.
"""

from __future__ import annotations

from .base import Tool, ToolResult
from .chunk_tool import ChunkExpansionTool, build_chunk_expansion_tool
from .document_tool import ReadDocumentTool, build_document_tool
from .find_documents_tool import FindDocumentsTool, build_find_documents_tool
from .graph_tool import GraphTraversalTool, build_graph_tool
from .ontology_tool import OntologyLookupTool, build_ontology_tool, ontology_from_result
from .registry import ToolRegistry
from .strategy_tool import RetrievalStrategyTool, build_strategy_tools
from .vector_tool import VectorSearchTool, build_vector_tool

__all__ = [
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "RetrievalStrategyTool",
    "build_strategy_tools",
    "GraphTraversalTool",
    "build_graph_tool",
    "ChunkExpansionTool",
    "build_chunk_expansion_tool",
    "ReadDocumentTool",
    "build_document_tool",
    "FindDocumentsTool",
    "build_find_documents_tool",
    "VectorSearchTool",
    "build_vector_tool",
    "OntologyLookupTool",
    "build_ontology_tool",
    "ontology_from_result",
]
