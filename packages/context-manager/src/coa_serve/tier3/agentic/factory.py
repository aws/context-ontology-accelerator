# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool registry factory and Agentic Retriever assembly (Req 12.4-12.6).

This module wires the internal-only :class:`ToolRegistry` and the composed
:class:`AgenticRetriever` from the deployment :class:`ServiceConfig` and the
shared :class:`ClientSet`, mirroring the existing client factory
(``clients/factory.py:build_lexical_retriever``) in structure and style and
reusing its endpoint-derivation helpers (``_graph_store_uri``) and lexical-
retriever builder.

Endpoint-gated registration (Req 12.5-12.6). A Tool is only registered when the
Knowledge_Graph / vector endpoints it needs (and, for the Vector_Search_Tool, an
embed client) are configured; otherwise the Tool is OMITTED rather than
registered in an unusable state (Req 12.5), and a ``structlog`` warning names the
omitted Tool and the missing endpoint/config (Req 12.6):

| Tool                       | Registered when                                |
|----------------------------|------------------------------------------------|
| ``strategy:<value>`` × 8   | ``neptune_endpoint`` AND ``opensearch_endpoint``|
| ``graph_traversal``        | ``neptune_endpoint``                            |
| ``vector_search``          | ``opensearch_endpoint`` AND an embed client     |
| ``nl_to_sql``              | a ``StructuredQueryTier`` is supplied           |
| ``nl_to_sparql``           | a ``StructuredQueryTier`` is supplied           |
| ``ontology_lookup``        | ALWAYS (the file source needs no endpoint)      |

``nl_to_sql`` / ``nl_to_sparql`` are additionally PER-NAMESPACE gated at request time: the retriever
withholds it from the planner on a namespace with no structured source (endpoint
gating registers it process-wide; composition gating hides it per request).

The Ontology_Lookup_Tool is ALWAYS registered (Req 12.4 / the design table): the
file source reads a local ``.ttl`` and needs no endpoint, and the graph source
reuses the already-required serve-layer graph client. Its backing
:class:`OntologySource` is selected from ``config.deep_reasoning_ontology_source``
(``file`` — the in-scope default for this increment — vs ``graph``, whose live
wiring is follow-up task 21), with ``config.deep_reasoning_ontology_file`` supplying the
file source path.

Import discipline (Requirement 12.3): module load of this module MUST NOT import
``graphrag_toolkit``. It imports only the dependency-free Tool builders, the
ontology-source abstraction, the budget value object, and the graphrag-free
client-factory helpers; the heavier composition pieces (controller, decomposer,
retriever, synthesizer) are imported LAZILY inside :func:`build_agentic_retriever`,
and ``build_lexical_retriever`` / ``build_strategy_tools`` keep their own toolkit
access lazy (the toolkit is only pulled in when the registry is actually built on
a configured deployment, never at import time).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ...clients.factory import _graph_store_uri, build_lexical_retriever
from ..vector_retriever import VectorRetriever
from .budget import AgenticBudgetConfig
from .ontology import OntologyFileSource, OntologyGraphSource
from .tools.chunk_tool import build_chunk_expansion_tool
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

    The four budgets are parsed and range-validated at config load (task 15), so
    here they are simply read off the flat ``ServiceConfig`` fields and shaped into
    the value object the session clock consumes. Keeping this mapping in the
    factory (rather than ``config.py``) preserves the config loader's import-light
    discipline (Req 12.3) — ``config.py`` never imports the agentic subpackage.

    Args:
        config: The loaded service configuration.

    Returns:
        An :class:`AgenticBudgetConfig` carrying the time budget, max steps,
        per-tool timeout, and fan-out cap.
    """
    return AgenticBudgetConfig(
        time_budget_s=float(config.deep_reasoning_time_budget_s),
        max_steps=config.deep_reasoning_max_steps,
        per_tool_timeout_s=float(config.deep_reasoning_per_tool_timeout_s),
        max_fanout=config.deep_reasoning_max_fanout,
        synthesis_reserve_s=float(config.deep_reasoning_synthesis_reserve_s),
    )


def build_ontology_source(config: ServiceConfig, clients: ClientSet) -> OntologySource:
    """Select the :class:`OntologySource` for the Ontology_Lookup_Tool (Req 3.2, 12.7).

    Honors ``config.deep_reasoning_ontology_source``: ``file`` (the in-scope default for
    this increment) builds an :class:`OntologyFileSource` over
    ``config.deep_reasoning_ontology_file``; ``graph`` builds an
    :class:`OntologyGraphSource` over the shared serve-layer graph client (its live
    use is wired/verified in follow-up task 21). The config value is already
    validated/normalized at load, so any unrecognized value would have fallen back
    to ``file`` there.

    Args:
        config: The loaded service configuration.
        clients: The shared client set (its ``graph`` client backs the graph
            source).

    Returns:
        The selected :class:`OntologySource`.
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
) -> ToolRegistry:
    """Build the internal :class:`ToolRegistry` with endpoint-gated tools (Req 12.4-12.6).

    Registers the seven Retrieval_Strategy_Tools, the Graph_Traversal_Tool, and the
    Vector_Search_Tool only when their required endpoints (and, for the vector tool,
    an embed client) are configured — omitting and warning (Req 12.5-12.6)
    otherwise — and ALWAYS registers the Ontology_Lookup_Tool (Req 12.4). Mirrors
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

    Returns:
        The populated :class:`ToolRegistry`, never exposed outside the Serve_Layer
        (Req 2.7).
    """
    registry = ToolRegistry()
    has_neptune = bool(config.neptune_endpoint)
    has_opensearch = bool(config.opensearch_endpoint)
    embed_client = clients.llm

    # ── Retrieval_Strategy_Tools × 7 — neptune + opensearch (Req 2.2) ──
    # build_lexical_retriever returns None unless BOTH store endpoints are set;
    # the strategy tools wrap that adapter, so they share its gating.
    #
    # Give the adapter the agentic PER-TOOL budget instead of its own 15s default.
    # A graphrag traversal strategy routinely needs more than 15s, so the default
    # made every strategy tool self-abort well inside the budget the controller had
    # granted it (live: "strategy:entity_based degraded ... TimeoutError after
    # 15.0s" → scored no-progress → wasted escalation step). The controller still
    # enforces the real deadline, so this only stops the adapter from cutting the
    # tool short first.
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

    # ── Graph_Traversal_Tool — neptune (Req 2.3) ───────────────────────
    if has_neptune:
        build_graph_tool(registry, _graph_store_uri(config.neptune_endpoint))
    else:
        logger.warning(
            "agentic_tool_omitted",
            tool="graph_traversal",
            missing_endpoints=["neptune_endpoint"],
            reason="required Knowledge_Graph endpoint unconfigured",
        )

    # ── Chunk_Expansion_Tool — neptune + opt-in flag (idea 6; DISABLED by default) ──
    # The idea-6 benchmark showed chunk-window expansion did not improve SEC10Q
    # accuracy (~58% with it vs ~62-64% without), so the tool is kept implemented
    # but NOT registered by default. Set AGENTIC_ENABLE_CHUNK_WINDOW=1 to re-enable
    # it for experiments.
    if has_neptune and os.getenv("AGENTIC_ENABLE_CHUNK_WINDOW", "") == "1":
        build_chunk_expansion_tool(registry, _graph_store_uri(config.neptune_endpoint))
    elif has_neptune:
        logger.info(
            "agentic_tool_disabled",
            tool="chunk_window",
            reason="AGENTIC_ENABLE_CHUNK_WINDOW not set (idea 6 disabled by default)",
        )

    # ── Vector_Search_Tool — opensearch + embed client (Req 2.3) ───────
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
    # Registered process-wide when the deployment has the Tier-2 structured engine
    # wired. Per-NAMESPACE gating (a namespace with no structured source) happens at
    # request time: the retriever withholds this tool from the planner's spec list
    # via ``excluded_tools`` so the LLM is never offered it on a document-only
    # namespace. Registered here only when the engine exists at all.
    if structured_query_tier is not None:
        from .tools.structured_tool import build_structured_tools

        # embed_client: both strategies use an embedding for ontology vector context
        # (Ontop skips its vector-hit lookup entirely without one).
        build_structured_tools(registry, structured_query_tier, embed_client=embed_client)
    else:
        logger.info(
            "agentic_tool_omitted",
            tool="nl_to_sql, nl_to_sparql",
            reason="no StructuredQueryTier supplied (structured retrieval unavailable)",
        )

    # ── Ontology_Lookup_Tool — ALWAYS (Req 12.4) ───────────────────────
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
) -> AgenticRetriever:
    """Compose the :class:`AgenticRetriever` from the registry and its collaborators.

    Assembles the endpoint-gated :class:`ToolRegistry`, the
    :class:`ReasoningController` (over the supplied ``step_planner``), the
    :class:`DecompositionPlanner`, the guardrailed :class:`Synthesizer`, and the
    :class:`AgenticBudgetConfig` into a single reusable, stateless
    :class:`AgenticRetriever` (per-request state lives on the clock/context created
    inside ``resolve``).

    The composition pieces (controller, decomposer, retriever, synthesizer) are
    imported LAZILY here so importing this module never pulls them — keeping the
    Req 12.3 lazy-import invariant trivially satisfied at module load.

    Args:
        config: The loaded service configuration (budgets, guardrail id, endpoints,
            ontology source).
        clients: The shared client set (its ``llm`` is used for the planner-side
            decomposition Converse call, synthesis, and embedding).
        step_planner: The :class:`StepPlanner` the reasoning controller consults for
            next-step selection and the Sufficiency_Check (the concrete Bedrock
            planner is supplied by startup wiring — task 18).
        registry: Optional pre-built registry; defaults to
            :func:`build_tool_registry`.
        vector_retriever: Optional pre-built :class:`VectorRetriever` forwarded to
            :func:`build_tool_registry`.
        embed_client: Optional embed client held by the retriever; defaults to
            ``clients.llm``.
        structured_query_tier: Optional Tier-2 structured-query tier forwarded to
            :func:`build_tool_registry` to enable the structured (SQL/SPARQL) tools.
        guardrail_id_provider: Optional callable returning the guardrail id LIVE on
            each call; forwarded to the :class:`Synthesizer` and
            :class:`DecompositionPlanner` so an operator's SSM guardrail change
            reaches the warm agentic path without a redeploy.

    Returns:
        The composed :class:`AgenticRetriever`.
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
            config, clients, vector_retriever=vector_retriever, structured_query_tier=structured_query_tier
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
    controller = ReasoningController(
        step_planner,
        per_tool_timeout_s=budget.per_tool_timeout_s,
        max_no_progress_steps=config.deep_reasoning_max_no_progress_steps,
        ontology_edge_mode=config.deep_reasoning_ontology_edge_mode,
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
        embed_client=embed_client if embed_client is not None else clients.llm,
    )


def _missing_store_endpoints(has_neptune: bool, has_opensearch: bool) -> list[str]:
    """Return the unconfigured store endpoints the strategy tools require (Req 12.6)."""
    missing: list[str] = []
    if not has_neptune:
        missing.append("neptune_endpoint")
    if not has_opensearch:
        missing.append("opensearch_endpoint")
    return missing
