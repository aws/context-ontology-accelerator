# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool registry factory and Agentic Retriever assembly.

Wires the internal-only :class:`ToolRegistry` and the composed
:class:`AgenticRetriever` from :class:`ServiceConfig` and the shared
:class:`ClientSet`. A Tool is registered only when the endpoints it needs are
configured; otherwise it is OMITTED and a warning names the missing config:

| Tool                       | Registered when                                |
|----------------------------|------------------------------------------------|
| ``strategy:<value>`` × 8   | ``neptune_endpoint`` AND ``opensearch_endpoint``|
| ``graph_traversal``        | ``neptune_endpoint``                            |
| ``vector_search``          | ``opensearch_endpoint`` AND an embed client     |
| ``nl_to_sql``              | a ``StructuredQueryTier`` is supplied           |
| ``nl_to_sparql``           | a ``StructuredQueryTier`` is supplied           |
| ``ontology_lookup``        | ALWAYS                                          |
| ``read_document``          | a ``SourcesRegistry`` is supplied (and          |
|                            | available); offered per request only            |
| ``find_documents``         | as ``read_document``, plus Neptune and/or       |
|                            | OpenSearch; offered per request only            |

``nl_to_sql`` / ``nl_to_sparql`` are additionally gated per namespace at request
time by the retriever. The ontology source is selected from
``config.deep_reasoning_ontology_source`` (``graph`` by default, or ``file`` with
``config.deep_reasoning_ontology_file``).

Import discipline: module load MUST NOT import ``graphrag_toolkit``. The
controller, decomposer, retriever and synthesizer are imported lazily inside
:func:`build_agentic_retriever`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ...clients.factory import _graph_store_uri, build_lexical_retriever
from ..vector_retriever import VectorRetriever
from .budget import AgenticBudgetConfig
from .models import StopSignalConfig
from .ontology import OntologyFileSource, OntologyGraphSource
from .tools.chunk_tool import build_chunk_expansion_tool
from .tools.document_tool import build_document_tool
from .tools.find_documents_tool import build_find_documents_tool
from .tools.graph_tool import TOOL_NAME as GRAPH_TOOL
from .tools.graph_tool import build_graph_tool
from .tools.ontology_tool import build_ontology_tool
from .tools.registry import ToolRegistry
from .tools.strategy_tool import build_strategy_tools
from .tools.vector_tool import build_vector_tool

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ...clients.base import ClientSet, LLMClient
    from ...config import ServiceConfig
    from .controller import StepPlanner
    from .ontology import OntologySource
    from .retriever import AgenticRetriever

logger = structlog.get_logger(__name__)


def build_budget_config(config: ServiceConfig) -> AgenticBudgetConfig:
    """Build the :class:`AgenticBudgetConfig` from the validated service config.

    Kept here rather than in ``config.py`` so the config loader never imports the
    agentic subpackage.
    """
    return AgenticBudgetConfig(
        time_budget_s=float(config.deep_reasoning_time_budget_s),
        max_steps=config.deep_reasoning_max_steps,
        per_tool_timeout_s=float(config.deep_reasoning_per_tool_timeout_s),
        max_fanout=config.deep_reasoning_max_fanout,
        synthesis_reserve_s=float(config.deep_reasoning_synthesis_reserve_s),
    )


def build_stop_signal_config(config: ServiceConfig) -> StopSignalConfig:
    """Build the :class:`StopSignalConfig` the planner and controller share.

    The fields are parsed and range-validated at config load; this only shapes
    them, so the planner (built at startup) and the controller (built here) read
    the same values.
    """
    return StopSignalConfig(
        min_confidence=config.deep_reasoning_sufficiency_min_confidence,
        similarity_threshold=config.deep_reasoning_progress_similarity_threshold,
        stability_steps=config.deep_reasoning_stability_steps,
        telemetry=config.deep_reasoning_stop_telemetry,
    )


def build_ontology_source(config: ServiceConfig, clients: ClientSet) -> OntologySource:
    """Select the :class:`OntologySource` for the Ontology_Lookup_Tool.

    ``file`` builds an :class:`OntologyFileSource` over
    ``config.deep_reasoning_ontology_file``; ``graph`` (the default) builds an
    :class:`OntologyGraphSource` over the shared serve-layer graph client.
    """
    # Defense-in-depth: a "file" source with no path would make rdflib parse the
    # CWD (IsADirectoryError on every lookup), so fall back to the graph source.
    # config.load_config already normalizes this, but the factory must not build a
    # structurally-broken source if constructed with a raw config elsewhere.
    if config.deep_reasoning_ontology_source == "file" and config.deep_reasoning_ontology_file:
        logger.info("agentic_ontology_source_selected", source="file", path=config.deep_reasoning_ontology_file)
        return OntologyFileSource(config.deep_reasoning_ontology_file)
    if config.deep_reasoning_ontology_source == "file":
        logger.warning("agentic_ontology_file_unset_using_graph_source")
    logger.info("agentic_ontology_source_selected", source="graph")
    return OntologyGraphSource(clients.graph)


def build_tool_registry(
    config: ServiceConfig,
    clients: ClientSet,
    *,
    vector_retriever: VectorRetriever | None = None,
    ontology_source: OntologySource | None = None,
    structured_query_tier: Any | None = None,
    sources_registry: Any | None = None,
    chunk_screener: Any | None = None,
) -> ToolRegistry:
    """Build the internal :class:`ToolRegistry` with endpoint-gated tools.

    Registers the seven Retrieval_Strategy_Tools, the Graph_Traversal_Tool, and the
    Vector_Search_Tool only when their required endpoints (and, for the vector tool,
    an embed client) are configured — omitting and warning
    otherwise — and ALWAYS registers the Ontology_Lookup_Tool. Mirrors
    ``build_lexical_retriever`` in style and reuses its endpoint helpers.

    Args:
        config: The loaded service configuration (supplies the Neptune/OpenSearch
            endpoints and the ontology-source selection).
        clients: The shared client set (supplies the vector client and the embed
            client; the graph client backs the ``graph`` ontology source).
        vector_retriever: Optional pre-built :class:`VectorRetriever`. Defaults to
            one wrapping ``clients.vector``; injectable for testing.
        ontology_source: Optional pre-built :class:`OntologySource`. Defaults to the
            config-selected source; injectable for testing.
        structured_query_tier: Optional Tier-2 structured-query tier; when supplied
            the ``nl_to_sql`` / ``nl_to_sparql`` structured tools are registered so
            the planner can query databases (omitted when ``None``).
        sources_registry: Optional serve-layer ``SourcesRegistry``; when supplied
            and available the ``read_document`` tool is registered (the retriever
            still withholds it from the planner unless the request opts in).
        chunk_screener: Optional retrieval-guardrail screener (``None`` when no
            retrieval guardrail is configured); the SQL tools screen the document
            evidence they hand the SQL writer with it.

    Returns:
        The populated :class:`ToolRegistry`, never exposed outside the Serve_Layer.
    """
    registry = ToolRegistry()
    has_neptune = bool(config.neptune_endpoint)
    has_opensearch = bool(config.opensearch_endpoint)
    embed_client = clients.llm

    # ── Retrieval_Strategy_Tools × 7 — neptune + opensearch ──
    # build_lexical_retriever returns None unless BOTH store endpoints are set;
    # the strategy tools wrap that adapter, so they share its gating.
    #
    # The adapter's own 15s default is shorter than the per-tool budget, so pass the
    # per-tool budget; the controller still enforces the real deadline.
    lexical = build_lexical_retriever(config, timeout_s=float(config.deep_reasoning_per_tool_timeout_s))
    if lexical is not None:
        build_strategy_tools(registry, lexical)
    else:
        missing = _missing_store_endpoints(has_neptune, has_opensearch)
        logger.warning(
            "agentic_tool_omitted",
            tool="strategy:<value> (7 retrieval strategy tools)",
            missing_endpoints=missing,
            reason="required Knowledge_Graph endpoints unconfigured",
        )

    # ── Graph_Traversal_Tool — neptune ───────────────────────
    if has_neptune:
        build_graph_tool(registry, _graph_store_uri(config.neptune_endpoint))
    else:
        logger.warning(
            "agentic_tool_omitted",
            tool="graph_traversal",
            missing_endpoints=["neptune_endpoint"],
            reason="required Knowledge_Graph endpoint unconfigured",
        )

    # ── Chunk_Expansion_Tool — neptune + opt-in flag (DISABLED by default) ──
    # Chunk-window expansion did not improve accuracy in benchmarks, so it is off by
    # default. Set AGENTIC_ENABLE_CHUNK_WINDOW=1 to enable it.
    if has_neptune and os.getenv("AGENTIC_ENABLE_CHUNK_WINDOW", "") == "1":
        build_chunk_expansion_tool(registry, _graph_store_uri(config.neptune_endpoint))
    elif has_neptune:
        logger.info(
            "agentic_tool_disabled",
            tool="chunk_window",
            reason="AGENTIC_ENABLE_CHUNK_WINDOW not set (disabled by default)",
        )

    # ── Vector_Search_Tool — opensearch + embed client ───────
    if has_opensearch and embed_client is not None:
        vr = vector_retriever if vector_retriever is not None else VectorRetriever(clients.vector)
        build_vector_tool(registry, vr, embed_client)
    else:
        missing = [] if has_opensearch else ["opensearch_endpoint"]
        if embed_client is None:
            missing.append("embed_client")
        logger.warning(
            "agentic_tool_omitted",
            tool="vector_search",
            missing_endpoints=missing,
            reason="required vector endpoint or embed client unconfigured",
        )

    # ── NL→SQL structured tool — when a StructuredQueryTier is supplied ──
    # Per-namespace gating happens at request time in the retriever.
    if structured_query_tier is not None:
        from .tools.structured_tool import build_structured_tools

        # embed_client: both strategies use an embedding for ontology vector context
        # (Ontop skips its vector-hit lookup entirely without one).
        build_structured_tools(
            registry, structured_query_tier, embed_client=embed_client, chunk_screener=chunk_screener
        )
    else:
        logger.info(
            "agentic_tool_omitted",
            tool="nl_to_sql, nl_to_sparql",
            reason="no StructuredQueryTier supplied (structured retrieval unavailable)",
        )

    # ── Read_Document_Tool — when the sources registry is wired ────────
    # Reads a source document's full text, scoped to the request namespace's
    # registered DOCUMENTS sources. Registered process-wide; the retriever offers
    # it to the planner only on requests that set ``options.readDocument``.
    if sources_registry is not None and getattr(sources_registry, "available", False):
        # The graph tool's __Source__ lookup resolves a chunk's opaque document id
        # to its key for chunks whose text carries no source header.
        graph_tool = registry.get(GRAPH_TOOL)
        build_document_tool(
            registry,
            sources_registry,
            source_key_resolver=getattr(graph_tool, "resolve_source_key", None),
        )
        # Names -> source documents (offered per request via options.entityRouting):
        # graph entity mentions plus an exact-phrase search of the raw chunk text.
        from coa_common import graphrag_chunk_index_name

        build_find_documents_tool(
            registry,
            sources_registry,
            graph_tool=graph_tool,
            vector_client=clients.vector if has_opensearch else None,
            chunk_index_for=graphrag_chunk_index_name,
        )
    else:
        logger.info(
            "agentic_tool_omitted",
            tool="read_document",
            reason="no sources registry supplied (document sources cannot be scoped)",
        )

    # ── Ontology_Lookup_Tool — ALWAYS ───────────────────────
    source = ontology_source if ontology_source is not None else build_ontology_source(config, clients)
    build_ontology_tool(registry, source, source_name=type(source).__name__)

    logger.info(
        "agentic_tool_registry_built",
        tools=registry.names(),
        count=len(registry.names()),
        ontology_source=type(source).__name__,
    )
    return registry


def build_agentic_retriever(
    config: ServiceConfig,
    clients: ClientSet,
    step_planner: StepPlanner,
    *,
    registry: ToolRegistry | None = None,
    vector_retriever: VectorRetriever | None = None,
    embed_client: LLMClient | None = None,
    structured_query_tier: Any | None = None,
    guardrail_id_provider: Callable[[], str] | None = None,
    sources_registry: Any | None = None,
    chunk_screener: Any | None = None,
) -> AgenticRetriever:
    """Compose the :class:`AgenticRetriever` from the registry and its collaborators.

    Assembles the endpoint-gated :class:`ToolRegistry`, the
    :class:`ReasoningController` (over the supplied ``step_planner``), the
    :class:`DecompositionPlanner`, the guardrailed :class:`Synthesizer`, and the
    :class:`AgenticBudgetConfig` into a single reusable, stateless
    :class:`AgenticRetriever` (per-request state lives on the clock/context created
    inside ``resolve``).

    ``guardrail_id_provider`` returns the guardrail id LIVE on each call; it is
    forwarded to the :class:`Synthesizer` and :class:`DecompositionPlanner` so an
    operator's SSM guardrail change reaches the warm agentic path without a redeploy.
    """
    from ..synthesizer import Synthesizer
    from .controller import ReasoningController
    from .decompose import DecompositionPlanner
    from .retriever import AgenticRetriever

    budget = build_budget_config(config)
    reg = (
        registry
        if registry is not None
        else build_tool_registry(
            config,
            clients,
            vector_retriever=vector_retriever,
            structured_query_tier=structured_query_tier,
            sources_registry=sources_registry,
            chunk_screener=chunk_screener,
        )
    )
    # The agentic path synthesizes over EVERY chunk its multi-step session gathered
    # (no MAX_PROMPT_CHUNKS truncation), so the synthesizer sees the full context —
    # graph traversals + the final semantic search — not just the first few chunks.
    synthesizer = Synthesizer(
        clients.llm,
        config.guardrail_id,
        guardrail_id_provider=guardrail_id_provider,
        max_prompt_chunks=None,
    )
    decomposer = DecompositionPlanner(
        clients.llm,
        guardrail_id=config.guardrail_id or None,
        guardrail_id_provider=guardrail_id_provider,
    )
    # The same embed backend the retriever (and the vector tool) uses.
    embed = embed_client if embed_client is not None else clients.llm
    stop_signals = build_stop_signal_config(config)
    planner_signals = getattr(step_planner, "_stop_signals", stop_signals)
    if planner_signals != stop_signals:
        # Fail-safe (empty replies are held back, the prefetch never skips) but silent
        # otherwise: the planner must be built with the same config to elicit signals.
        logger.warning(
            "deep_reasoning_stop_signal_config_mismatch", planner=str(planner_signals), controller=str(stop_signals)
        )
    controller = ReasoningController(
        step_planner,
        per_tool_timeout_s=budget.per_tool_timeout_s,
        max_no_progress_steps=config.deep_reasoning_max_no_progress_steps,
        ontology_edge_mode=config.deep_reasoning_ontology_edge_mode,
        # The answer-stability signal embeds working answers with it.
        embed_fn=embed,
        stop_signals=stop_signals,
    )

    logger.info(
        "agentic_retriever_built",
        tier3_strategy=config.tier3_strategy,
        tools=reg.names(),
        time_budget_s=budget.time_budget_s,
        max_steps=budget.max_steps,
    )
    return AgenticRetriever(
        reg,
        controller,
        decomposer,
        synthesizer,
        budget,
        embed_client=embed,
    )


def _missing_store_endpoints(has_neptune: bool, has_opensearch: bool) -> list[str]:
    """Return the unconfigured store endpoints the strategy tools require."""
    missing: list[str] = []
    if not has_neptune:
        missing.append("neptune_endpoint")
    if not has_opensearch:
        missing.append("opensearch_endpoint")
    return missing
