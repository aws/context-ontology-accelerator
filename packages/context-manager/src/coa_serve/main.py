# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Context Manager — Serve layer orchestration service.

Hosted on Bedrock AgentCore Runtime. The @app.entrypoint handler receives
queries from all consumer surfaces (Data Layer API Lambda, Playground SSE,
MCP) and coordinates downstream services through tiered resolution.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from coa_common.constants import validate_query_text
from coa_common.logging import setup_logging
from pydantic import ValidationError

from .clients.athena import AthenaQueryExecutor
from .clients.bedrock import BedrockLLMClient
from .clients.composite_executor import CompositeQueryExecutor
from .clients.neptune import NeptuneGraphClient
from .clients.opensearch import OpenSearchVectorClient
from .config import ServiceConfig, load_config
from .exceptions import (
    AccessDeniedError,
    DataSourceUnavailableError,
    NamespaceNotFoundError,
    NoResultError,
    QueryTranslationError,
    ServeError,
)
from .identity import extract_jwt_identity, resolve_principal, resolve_user_id
from .models import InvokeRequest, TraceStep
from .orchestrator import Orchestrator
from .role_resolver import ResolvedProfile, resolve_profile
from .session import SessionManager
from .session_actions import (
    handle_create_session,
    handle_delete_session,
    handle_get_recent_session,
    handle_get_session_history,
    handle_list_sessions,
)
from .session_metadata import SessionMetadataStore
from .sse_emitter import SSEEmitter
from .step_ids import StepId
from .tier1.metric_resolver import MetricResolver
from .tier2.nl_to_sql.agentic_strategy import AgenticStrategy
from .tier2.nl_to_sql.sql_generator import SQLGenerator
from .tier2.nl_to_sql.strategy import NLtoSQLStrategy
from .tier2.ontop.nl_to_sparql import NLtoSPARQL
from .tier2.ontop.sparql_example_loader import FewShotExampleLoader
from .tier2.ontop.strategy import OntopStrategy
from .tier2.ontop.vkg_translator import VKGTranslator
from .tier2.skip.aoss_provenance import AossProvenanceEvaluator
from .tier2.sql_firewall import SQLFirewall
from .tier2.strategy import StructuredQueryTier
from .tier3.graph_traverser import GraphTraverser
from .tier3.knowledge_retriever import KnowledgeRetriever
from .tier3.synthesizer import Synthesizer

if TYPE_CHECKING:
    from .clients.sources_registry import SourcesRegistry
    from .deadline import Deadline
from .tier3.vector_retriever import VectorRetriever
from .trace import TraceCollector

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

app = BedrockAgentCoreApp()

_config: ServiceConfig | None = None
_orchestrator: Orchestrator | None = None
_session_manager: SessionManager | None = None
_session_metadata: SessionMetadataStore | None = None
_sources_registry: SourcesRegistry | None = None
_nl_to_sparql: NLtoSPARQL | None = None
_init_lock = asyncio.Lock()

# Hard upper bound on the per-request transport budget: max(10, min(env, 300)).
# The 300s cap is a deliberate ceiling — even a misconfigured RESOLVE_TIMEOUT_S
# env cannot let a request run unbounded (e.g. toward a Lambda/runtime ceiling),
# and a caller's options.timeoutMs can only NARROW this via Deadline.from_budget,
# never widen it. Floor of 10s keeps a pathological low env from starving requests.
RESOLVE_TIMEOUT_S = max(10, min(int(os.environ.get("RESOLVE_TIMEOUT_S", "120")), 300))


def _build_deadline(request) -> Deadline:
    """Build the request-scoped deadline (A0) for a query.

    The transport budget is ``RESOLVE_TIMEOUT_S`` — set per-transport by the
    deploying stack (REST/data-layer path vs the longer AgentCore/Playground
    path). The caller may narrow it via ``options.timeoutMs`` but never widen it.

    ``options.timeoutMs`` is read here — this is what makes the long-declared but
    previously-inert Smithy field actually govern the request budget (closes the
    accept-and-ignore contract defect).
    """
    from .deadline import Deadline

    caller_timeout_ms = None
    options = getattr(request, "options", None) or {}
    if isinstance(options, dict):
        caller_timeout_ms = options.get("timeoutMs")
    return Deadline.from_budget(RESOLVE_TIMEOUT_S, caller_timeout_ms)


# Namespace format constraint — mirrors InvokeRequest.namespace so the 400/404
# split is consistent whether validation happens in the pre-gate below or the
# Pydantic model further down. Anchored so a partial match (e.g. an embedded
# valid substring inside "../../evil") does not accidentally pass.
_NAMESPACE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


async def _ensure_initialized():
    """Initialize config and orchestrator on first request."""
    global _config, _orchestrator, _session_manager, _session_metadata, _sources_registry, _nl_to_sparql
    if _orchestrator is not None:
        return
    async with _init_lock:
        if _orchestrator is not None:
            return
        config = await asyncio.to_thread(load_config)

        # Session manager (optional — requires MEMORY_ID)
        if config.memory_id:
            _session_manager = SessionManager(
                memory_id=config.memory_id,
                region=config.bedrock_region,
            )

        # Session metadata store (optional — requires SESSION_METADATA_TABLE)
        if config.session_metadata_table:
            _session_metadata = SessionMetadataStore(
                table_name=config.session_metadata_table,
                region=config.bedrock_region,
            )

        # Build client layer
        neptune_client = NeptuneGraphClient(endpoint=config.neptune_endpoint)
        opensearch_client = OpenSearchVectorClient(endpoint=config.opensearch_endpoint)
        bedrock_client = BedrockLLMClient(model_id=config.bedrock_model_id, region=config.bedrock_region)
        # VKG endpoint used as template: "://vkg." is replaced with "://vkg-{ns}." per request

        from .clients.sources_registry import SourcesRegistry

        sources_registry = SourcesRegistry(
            table_name=config.data_sources_table,
            namespaces_table=os.environ.get("NAMESPACES_TABLE", ""),
            region=config.bedrock_region,
        )
        _sources_registry = sources_registry
        athena_executor = AthenaQueryExecutor(region=config.bedrock_region, sources_registry=sources_registry)

        # single-vs-cross-source dispatcher (Serve LLD §2.2.1 Option D).
        # Direct JDBC is the DEFAULT fast path for single-source queries (one
        # catalog, all-qualified tables, a resolvable DATABASE source) because
        # direct asyncpg (~20-50ms p50) avoids Athena's federation cold-start;
        # complex/federated/cross-source queries use Athena. The composite only
        # routes to JDBC after confirming the source is a DATABASE source
        # (has_jdbc_endpoint gate) — Glue-native/S3 sources always use Athena.
        # Built by default when a data-sources table is configured; set
        # SCL_DISABLE_JDBC_DISPATCH=true as an operational off-switch (composite
        # then degrades to Athena-only = the prior behaviour).
        source_db_executor = None
        jdbc_disabled = os.environ.get("SCL_DISABLE_JDBC_DISPATCH", "").lower() == "true"
        if not jdbc_disabled and config.data_sources_table:
            try:
                from .clients.source_db import SourceDBQueryExecutor

                source_db_executor = SourceDBQueryExecutor(
                    data_sources_table=config.data_sources_table,
                    region=config.bedrock_region,
                    sources_registry=sources_registry,
                )
                logger.info("jdbc_dispatch_enabled")
            except Exception as exc:
                logger.warning("source_db_executor_unavailable", error=type(exc).__name__)

        # Redshift Serverless executor for Glue/Iceberg sources that opt
        # into Redshift execution (queryEngine=REDSHIFT). Built by default when a
        # data-sources table is configured; set SCL_DISABLE_REDSHIFT_DISPATCH=true
        # as an operational off-switch (composite then routes those sources to
        # Athena — the prior behaviour). Never regresses non-Redshift sources.
        redshift_executor = None
        redshift_disabled = os.environ.get("SCL_DISABLE_REDSHIFT_DISPATCH", "").lower() == "true"
        if not redshift_disabled and config.data_sources_table:
            try:
                from .clients.redshift_data import RedshiftDataAPIExecutor

                redshift_executor = RedshiftDataAPIExecutor(
                    sources_registry=sources_registry,
                    region=config.bedrock_region,
                )
                logger.info("redshift_dispatch_enabled")
            except Exception as exc:
                logger.warning("redshift_executor_unavailable", error=type(exc).__name__)

        query_executor = CompositeQueryExecutor(
            athena_executor=athena_executor,
            source_db_executor=source_db_executor,
            firewall=SQLFirewall(),
            sources_registry=sources_registry,
            redshift_executor=redshift_executor,
        )

        # Build tier resolvers
        metric_resolver = MetricResolver(neptune_client=neptune_client)
        # Load metrics from Neptune at startup (fire-and-forget — start()
        # spawns a background refresh task that keeps metrics fresh).
        # _ensure_initialized runs inside the running event loop, so use
        # create_task directly (get_event_loop is deprecated in this context).
        asyncio.create_task(metric_resolver.start())
        # few-shot example loader (S3). Inert unless ONTOLOGY_BUCKET is set,
        # in which case namespace-scoped examples.json replaces the generic prompt
        # examples; missing file falls back gracefully.
        few_shot_loader = FewShotExampleLoader(
            bucket=os.environ.get("ONTOLOGY_BUCKET", ""),
            region=config.bedrock_region,
        )
        nl_to_sparql = NLtoSPARQL(
            graph_client=neptune_client,
            llm_client=bedrock_client,
            guardrail_id=config.guardrail_id,
            # enrich Tier-2 metric context with full Neptune metric defs.
            metric_resolver=metric_resolver,
            few_shot_loader=few_shot_loader,
        )
        _nl_to_sparql = nl_to_sparql
        firewall = SQLFirewall()
        vkg_translator = VKGTranslator(
            vkg_endpoint=config.vkg_endpoint, firewall=firewall, query_executor=query_executor
        )
        vector_retriever = VectorRetriever(vector_client=opensearch_client)
        graph_traverser = GraphTraverser(graph_client=neptune_client)

        # Query-time chunk screening: evaluate retrieved content against retrieval
        # guardrail (MEDIUM PROMPT_ATTACK) before synthesis. Graceful degradation:
        # if retrieval guardrail not configured, screening is skipped.
        chunk_screener = None
        if config.retrieval_guardrail_id:
            from coa_common.guardrail_metrics import COMPONENT_SERVE_RETRIEVAL
            from coa_common.guardrail_screener import GuardrailScreener

            chunk_screener = GuardrailScreener(
                guardrail_id=config.retrieval_guardrail_id,
                guardrail_version=config.retrieval_guardrail_version,
                region=config.bedrock_region,
                # Serve emits guardrail decision metrics as stdout EMF — no
                # PutMetricData grant needed on the serve runtime role.
                component=COMPONENT_SERVE_RETRIEVAL,
                metrics_transport="emf",
            )
            logger.info("chunk_screener_enabled", guardrail_id=config.retrieval_guardrail_id)

        synthesizer = Synthesizer(
            bedrock_client=bedrock_client,
            guardrail_id=config.guardrail_id,
            chunk_screener=chunk_screener,
        )

        # Lexical baseline retriever — only when TIER3_STRATEGY="lexical-baseline"
        from .clients.factory import build_lexical_retriever

        lexical_retriever = build_lexical_retriever(config)

        knowledge_retriever = KnowledgeRetriever(
            vector_retriever=vector_retriever,
            graph_traverser=graph_traverser,
            synthesizer=synthesizer,
            lexical_retriever=lexical_retriever,
        )

        # NL-to-SQL: Direct SQL generation via LLM with ontology retrieval + FK expansion
        sql_generator = SQLGenerator(
            llm_client=bedrock_client,
            vector_client=opensearch_client,
            guardrail_id=config.guardrail_id,
        )

        # Tier 2 strategies
        oss_ontology_index = os.environ.get("OSS_ONTOLOGY_INDEX", "")
        ontop_strategy = OntopStrategy(
            nl_to_sparql=nl_to_sparql,
            vkg_translator=vkg_translator,
            vector_client=opensearch_client,
            oss_ontology_index=oss_ontology_index,
        )
        nl_to_sql_strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index=oss_ontology_index,
            # Backs the opt-in ontology-graph expansion of the retrieved tables
            # (SERVE_NL2SQL_GRAPH_EXPAND deployment-wide, options.flatGraphExpand per
            # request); with both off the client is simply never used.
            graph_client=neptune_client,
        )
        # Bounded tool-use agent (iterative schema discovery → generate → execute →
        # self-correct). OPT-IN only: it runs solely when a request pins
        # options.strategy="deep-reasoning" — never as a fallback (see
        # StructuredQueryTier._strategies_for) — so registering it here does not
        # change the default nl_to_sql_first resolution path.
        agentic_strategy = AgenticStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            vector_client=opensearch_client,
            oss_ontology_index=oss_ontology_index,
            # Backs the opt-in explore_graph tool (SERVE_DEEP_REASONING_GRAPH_TRAVERSAL);
            # with the flag off the client is simply never used.
            graph_client=neptune_client,
        )
        structured_query_tier = StructuredQueryTier(
            strategies=[ontop_strategy, nl_to_sql_strategy, agentic_strategy],
        )

        # Deep-reasoning Tier 3 path. Built so a deployment default
        # (TIER3_STRATEGY=deep-reasoning) OR a per-request
        # options.mode="deep-reasoning" can engage it; construction is
        # cheap and keeps the graphrag_toolkit import lazy (the registry's
        # strategy/graph tools defer toolkit access to first invoke). Imported here
        # (not at module load) so the serve module import stays graphrag-free. A
        # construction failure must NOT break startup — the orchestrator simply runs
        # the existing Tier 3 paths — so we degrade with a warning.
        #
        # Built AFTER structured_query_tier so the agentic registry can expose the
        # nl_to_sql tool over the same Tier-2 engine; per-namespace composition
        # gating withholds it at request time on document-only namespaces.
        agentic_retriever = None
        try:
            from .clients.base import ClientSet
            from .tier3.agentic.factory import build_agentic_retriever
            from .tier3.agentic.planner import BedrockStepPlanner

            agentic_clients = ClientSet(
                graph=neptune_client,
                vector=opensearch_client,
                llm=bedrock_client,
                query_executor=query_executor,
            )
            step_planner = BedrockStepPlanner(bedrock_client, guardrail_id=config.guardrail_id or None)
            agentic_retriever = build_agentic_retriever(
                config, agentic_clients, step_planner, structured_query_tier=structured_query_tier
            )
        except Exception as exc:
            logger.warning("agentic_retriever_unavailable", error=type(exc).__name__, error_msg=str(exc)[:200])

        # Per-query Tier-2 pruning for MIXED namespaces (which namespace-level
        # source-composition gating cannot rule out). On by default; set
        # SERVE_TIER2_SKIP_MODE=off to disable. Fail-open — see aoss_provenance.py.
        tier2_skip_evaluator = None
        if os.environ.get("SERVE_TIER2_SKIP_MODE", "aoss").lower() != "off" and oss_ontology_index:
            tier2_skip_evaluator = AossProvenanceEvaluator(
                vector_client=opensearch_client,
                sources_registry=sources_registry,
                oss_ontology_index=oss_ontology_index,
            )

        _orchestrator = Orchestrator(
            metric_resolver=metric_resolver,
            knowledge_retriever=knowledge_retriever,
            query_executor=query_executor,
            bedrock_client=bedrock_client,
            firewall=firewall,
            structured_query_tier=structured_query_tier,
            lexical_retriever_strategy=(
                config.lexical_retriever_strategy if config.tier3_strategy == "lexical-baseline" else None
            ),
            # Skip inapplicable tiers by namespace source composition (perf).
            # Reuses the same cached registry the executors use; fail-open.
            sources_registry=sources_registry,
            tier2_skip_evaluator=tier2_skip_evaluator,
            # Tier-3 URI-hop seeding: same vector client + ontology index the
            # Tier-2 strategies use, so a non-Latin query seeds graph traversal
            # from matched concept URIs instead of the ASCII keyword branch.
            vector_client=opensearch_client,
            oss_ontology_index=oss_ontology_index,
            agentic_retriever=agentic_retriever,
            tier3_deep_reasoning_default=(config.tier3_strategy == "deep-reasoning"),
        )
        _config = config
        logger.info(
            "context_manager_initialized",
            guardrail_configured=bool(config.guardrail_id),
            vkg_endpoint=config.vkg_endpoint,
        )


def _format_validation_errors(e: ValidationError) -> list[dict]:
    """Return field-level errors without leaking constraint internals."""
    errors = []
    for err in e.errors():
        errors.append(
            {
                "field": ".".join(str(loc) for loc in err["loc"]),
                "type": err["type"],
            }
        )
    return errors


# ── Data Layer isolated action handlers ──────────────────────────────────────


def _error(status_code: int, message: str, request_id: str) -> dict:
    return {
        "message": message,
        "requestId": request_id,
        "statusCode": status_code,
    }


async def _handle_translate(payload: dict, request_id: str) -> dict:
    """NL-to-SPARQL translation only (no VKG execution)."""
    namespace = payload.get("namespace", "")
    try:
        query = validate_query_text(payload.get("query"))
    except ValueError:
        return _error(400, "invalid query", request_id)
    if not namespace:
        return _error(400, "namespace required", request_id)

    trace = TraceCollector()
    try:
        result = await asyncio.wait_for(
            _nl_to_sparql.translate(query, namespace),
            timeout=RESOLVE_TIMEOUT_S,
        )
    except TimeoutError as e:
        raise QueryTranslationError("Translation timed out") from e
    except Exception as e:
        logger.error("translate_error", error=str(e), request_id=request_id)
        raise QueryTranslationError("Translation failed") from e

    # Replay full pipeline steps (context_assembly, llm_call, validation) — E8
    if result.trace_steps:
        trace.add_dicts(result.trace_steps)

    if not result.sparql:
        raise QueryTranslationError("Could not generate SPARQL for this query")

    return {
        "sparqlQuery": result.sparql,
        "confidence": {"score": result.confidence, "rationale": ""},
        "trace": trace.steps_serializable,
        # Null BY DESIGN, not by omission (#986): translation never touches the
        # VKG, and no in-band artifact names the ontology snapshot it ran
        # against. A namespace-level "current version" would not be the version
        # THIS translation used, so reporting one here would be a false claim.
        "ontologyVersion": None,
        "requestId": request_id,
        "statusCode": 200,
    }


async def _handle_kb_search(payload: dict, request_id: str) -> dict:
    """Vector search in OpenSearch — returns matching document chunks."""
    namespace = payload.get("namespace", "")
    options = payload.get("options", {})
    try:
        query = validate_query_text(payload.get("query"))
    except ValueError:
        return _error(400, "invalid query", request_id)
    if not namespace:
        return _error(400, "namespace required", request_id)

    top_k = min(options.get("topK", 10), 100)
    vector_retriever = _orchestrator._knowledge_retriever._vector
    bedrock_client = _orchestrator._bedrock_client

    if not vector_retriever or not bedrock_client:
        raise DataSourceUnavailableError("Vector search not configured")

    trace = TraceCollector()

    try:
        t0 = time.perf_counter()
        embedding = await bedrock_client.embed(query)
        embed_ms = int((time.perf_counter() - t0) * 1000)
        trace.record(StepId.QUERY_EMBED, "success", embed_ms, tool_used="bedrock")
    except Exception as e:
        logger.error("kb_search_embed_error", error=str(e), request_id=request_id)
        raise DataSourceUnavailableError("Embedding generation failed") from e

    try:
        t0 = time.perf_counter()
        chunks = await asyncio.wait_for(
            vector_retriever.search(embedding, namespace, top_k=top_k),
            timeout=RESOLVE_TIMEOUT_S,
        )
        search_ms = int((time.perf_counter() - t0) * 1000)
        trace.record(
            StepId.T3_VECTOR_SEARCH,
            "success",
            search_ms,
            detail=f"{len(chunks)} chunks",
            tool_used="opensearch",
        )
    except TimeoutError as e:
        raise DataSourceUnavailableError("Vector search timed out") from e
    except DataSourceUnavailableError:
        raise
    except Exception as e:
        logger.error("kb_search_error", error=str(e), request_id=request_id)
        raise DataSourceUnavailableError("Vector search failed") from e

    return {
        "chunks": [
            {
                "chunkId": c.chunk_id,
                "text": c.text,
                "sourceDocumentId": c.source_doc,
                "sourceDocumentName": c.source_doc_name,
                "relevanceScore": c.relevance_score,
            }
            for c in chunks
        ],
        "trace": trace.steps_serializable,
        "queryEmbeddingModel": bedrock_client._embed_model_id,
        "requestId": request_id,
        "statusCode": 200,
    }


async def _handle_graph_traverse(payload: dict, request_id: str) -> dict:
    """Graph traversal in Neptune — returns entities and relationships."""
    namespace = payload.get("namespace", "")
    options = payload.get("options", {})
    start_uri = options.get("startUri", "")
    if not namespace:
        return _error(400, "namespace required", request_id)
    if not start_uri:
        return _error(400, "startUri required in options", request_id)

    max_depth = min(options.get("maxDepth", 2), 5)
    graph_traverser = _orchestrator._knowledge_retriever._graph

    if not graph_traverser:
        raise DataSourceUnavailableError("Graph traversal not configured")

    trace = TraceCollector()

    try:
        t0 = time.perf_counter()
        entities = await asyncio.wait_for(
            graph_traverser.traverse_from_uris(
                [start_uri],
                namespace,
                max_hops=max_depth,
            ),
            timeout=RESOLVE_TIMEOUT_S,
        )
        traverse_ms = int((time.perf_counter() - t0) * 1000)
        trace.record(
            StepId.T3_GRAPH_TRAVERSE,
            "success",
            traverse_ms,
            detail=f"{len(entities)} entities",
            tool_used="neptune",
        )
    except TimeoutError as e:
        raise DataSourceUnavailableError("Graph traversal timed out") from e
    except DataSourceUnavailableError:
        raise
    except Exception as e:
        logger.error("graph_traverse_error", error=str(e), request_id=request_id)
        raise DataSourceUnavailableError("Graph traversal failed") from e

    return {
        "entities": [
            {
                "uri": e.uri,
                "label": e.label,
                "type": e.type,
                "properties": {},
            }
            for e in entities
        ],
        "relationships": [
            {
                "sourceUri": e.uri,
                "predicateUri": r.get("predicate", ""),
                "targetUri": r.get("target_uri", ""),
                "predicateLabel": r.get("target_label", ""),
            }
            for e in entities
            for r in e.relationships
        ],
        "trace": trace.steps_serializable,
        "requestId": request_id,
        "statusCode": 200,
    }


async def _setup_streaming_session(
    payload: dict,
    session_user_id: str,
    namespace: str,
    query: str,
) -> tuple[str | None, list[dict[str, str]] | None]:
    """Create or resume a session for streaming mode.

    IMPORTANT: Memory API requires actor_id matching [a-zA-Z0-9][a-zA-Z0-9-_/]*
    Use the JWT sub (Cognito UUID) for Memory operations, NOT email.

    Returns (session_id, conversation_history).
    """
    session_id: str | None = payload.get("sessionId")
    conversation_history: list[dict[str, str]] | None = None

    if not (_session_manager and session_user_id):
        return session_id, conversation_history

    try:
        if not session_id and _session_metadata:
            meta_record = await _session_metadata.create(session_user_id, namespace_id=namespace, title=query[:60])
            session_id = meta_record.session_id
        elif session_id and _session_metadata:
            is_owner = await _session_metadata.validate_ownership(session_user_id, session_id)
            if not is_owner:
                try:
                    await _session_metadata.create(session_user_id, namespace_id=namespace, session_id=session_id)
                except Exception:
                    logger.warning("session_adopt_failed", session_id=session_id, exc_info=True)
    except Exception as e:
        logger.warning("session_metadata_op_failed", error=str(e), exc_info=True)

    sid, conversation_history = await _session_manager.create_or_resume(
        user_id=session_user_id,
        namespace_id=namespace,
        session_id=session_id,
    )
    return sid, conversation_history


async def _persist_turn(
    session_user_id: str,
    session_id: str,
    query: str,
    namespace: str,
    response,
    request_id: str,
) -> None:
    """Best-effort persistence of a completed query turn to session history."""
    try:
        answer = response.result.synthesized_answer or ""
        turn_metadata: dict | None = None
        if response.result:
            turn_metadata = response.result.model_dump(
                by_alias=True,
                exclude_none=True,
                exclude={"synthesized_answer", "supporting_content"},
            )
            turn_metadata["requestId"] = request_id
            turn_metadata["query"] = query
        await _session_manager.append_turn(
            user_id=session_user_id,
            session_id=session_id,
            query=query,
            answer_summary=answer,
            namespace_id=namespace,
            result_metadata=turn_metadata,
        )
        if _session_metadata:
            await _session_metadata.update_activity(session_user_id, session_id, namespace_id=namespace)
    except Exception:
        logger.warning("sse_persist_turn_failed", session_id=session_id, exc_info=True)


async def _handle_blocking_query(request, request_id: str):
    """Non-streaming query resolution. Yields a single result dict."""
    resolve_start = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            _orchestrator.resolve(request, deadline=_build_deadline(request)),
            timeout=RESOLVE_TIMEOUT_S,
        )
    except TimeoutError:
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        logger.error("orchestrator_timeout", namespace=request.namespace, request_id=request_id)
        yield {
            "error": "TimeoutError",
            "message": "Query resolution timed out",
            "requestId": request_id,
            "statusCode": 504,
            "metadata": {"totalMs": total_ms},
        }
        return
    except AccessDeniedError as e:
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        logger.warning("orchestrator_access_denied", request_id=request_id, reason=e.reason)
        d = e.to_dict(request_id)
        d["metadata"] = {"totalMs": total_ms}
        yield d
        return
    except NoResultError as e:
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        logger.info("orchestrator_no_result", request_id=request_id, tier=e.tier)
        d = e.to_dict(request_id)
        d["metadata"] = {"totalMs": total_ms}
        yield d
        return
    except Exception as e:
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        logger.error("orchestrator_error", error_type=type(e).__name__, error_msg=str(e), request_id=request_id)
        yield {
            "error": "InternalError",
            "message": "Query resolution failed",
            "requestId": request_id,
            "statusCode": 500,
            "metadata": {"totalMs": total_ms},
        }
        return

    total_ms = int((time.perf_counter() - resolve_start) * 1000)
    response.result.metadata = {**(response.result.metadata or {}), "totalMs": total_ms}
    response.requestId = request_id
    yield response.model_dump(by_alias=True, exclude_none=True, mode="json")


async def _handle_streaming_query(payload: dict, request, request_id: str, session_user_id: str):
    """Streaming SSE query resolution. Yields SSE event strings."""
    session_id, conversation_history = await _setup_streaming_session(
        payload=payload,
        session_user_id=session_user_id,
        namespace=request.namespace,
        query=request.query,
    )

    emitter = SSEEmitter(request_id, session_id)

    async def on_step(step: TraceStep) -> None:
        emitter.queue_step(step)

    async def on_token(text: str) -> None:
        emitter.queue_token(text)

    trace = TraceCollector(on_record=on_step)
    resolve_start = time.perf_counter()

    async def _run_resolve():
        try:
            response = await asyncio.wait_for(
                _orchestrator.resolve(
                    request,
                    trace=trace,
                    on_token=on_token,
                    conversation_history=conversation_history,
                    deadline=_build_deadline(request),
                ),
                timeout=RESOLVE_TIMEOUT_S,
            )
            await trace.flush()
            emitter.done()
            return response
        except Exception:
            await trace.flush()
            emitter.close()
            raise

    resolve_task = asyncio.create_task(_run_resolve())

    try:
        async for event_str in emitter.stream():
            yield event_str
    except (asyncio.CancelledError, GeneratorExit):
        resolve_task.cancel()
        emitter.close()
        logger.info("sse_client_disconnected", request_id=request_id)
        return

    try:
        await resolve_task
        response = resolve_task.result()
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        response.result.metadata = {**(response.result.metadata or {}), "totalMs": total_ms}
        response.requestId = request_id
        response.sessionId = session_id
        yield emitter.format_done(response.result)

        if _session_manager and session_user_id and session_id:
            await _persist_turn(
                session_user_id=session_user_id,
                session_id=session_id,
                query=request.query,
                namespace=request.namespace,
                response=response,
                request_id=request_id,
            )
    except TimeoutError:
        total_ms = int((time.perf_counter() - resolve_start) * 1000)
        logger.error("sse_timeout", request_id=request_id, total_ms=total_ms)
        yield emitter.format_error("TimeoutError", "Query resolution timed out", 504)
    except AccessDeniedError as e:
        logger.warning("sse_access_denied", request_id=request_id, reason=e.reason)
        yield emitter.format_error("AccessDenied", "Access denied", 403)
    except NoResultError as e:
        logger.info("sse_no_result", request_id=request_id, tier=e.tier)
        yield emitter.format_error("NoResult", e.message, e.status_code)
    except ServeError as e:
        logger.warning("sse_serve_error", request_id=request_id, error_type=e.error_type, message=e.message)
        yield emitter.format_error(e.error_type, e.message, e.status_code)
    except asyncio.CancelledError:
        logger.info("sse_resolve_cancelled", request_id=request_id)
    except Exception as exc:
        logger.error("sse_resolve_error", request_id=request_id, error_type=type(exc).__name__, exc_info=True)
        yield emitter.format_error("InternalError", "Query resolution failed", 500)


async def _authorize_namespace_access(
    namespace: str,
    jwt_user_id: str,
    jwt_email: str,
    jwt_groups: list[str],
    payload_profile: dict,
    request_id: str,
) -> tuple[ResolvedProfile | None, dict | None]:
    """Resolve the caller's roles and run the Cedar namespace-admission gate.

    The SINGLE namespace authorization point for every namespace-scoped surface —
    the isolated ``translate`` / ``kbSearch`` / ``graphTraverse`` actions AND the
    Tier-1/2/3 query path. Namespace authorization previously lived only inside
    ``SQLFirewall`` (the Tier-1/2 SQL execution sites), so the isolated retrieval
    actions and the whole Tier-3 retrieval/synthesis path reached their data sinks
    ungated: any authenticated IdP user could read any namespace's document
    chunks, graph entities and synthesized answers (F-2, CWE-862).

    JWT identity is authoritative; ``payload_profile`` userId/groups are a
    fallback used only when no JWT was forwarded (local/dev). Fails CLOSED: a
    grant-lookup error rejects the request, and Cedar itself denies a caller with
    no resolved roles in prod (``SCL_CEDAR_FAIL_OPEN_NO_ROLES`` governs the dev
    bypass).

    Both the grant lookup (DynamoDB) and the Cedar evaluation (``cedarpy`` plus a
    possible policy read) are synchronous/blocking, and this gate now runs on
    EVERY namespace-scoped request — so both are dispatched via
    ``asyncio.to_thread`` to keep them off the event loop.

    CANONICAL-ID CONSTRAINT: the decision is evaluated on the namespace string the
    caller sent, verbatim. The namespace-scoped seed policies match
    ``resourceRoles.contains({role, resourceUID: resource.id})`` and the control
    plane writes grants with ``resourceId`` = the namespace **UUID** (see
    ``grants/create_handler.py``), so a caller must pass the canonical id to be
    authorized. A namespace *name* — which ``namespace_exists`` does resolve, and
    which ``_NAMESPACE_PATTERN`` permits — therefore DENIES even for a legitimately
    granted principal. Deliberate for now: fail-closed is the safe side, and
    canonicalizing here WITHOUT also canonicalizing the retrieval layer's tenant
    derivation would admit the caller and then read under a tenant derived from the
    unresolved name (the separate tenant-scoping issue). All three shipped surfaces
    send the id — Playground route param, data-layer ``{namespaceId}``, MCP
    ``namespaceId`` — so no supported caller is affected today.

    Returns:
        ``(resolved_profile, None)`` when allowed — the resolved profile is handed
        back so the query path can reuse it instead of resolving a second time.
        ``(None, error_dict)`` when rejected — the caller must yield ``error_dict``
        and stop. A policy deny is a 403; an inability to evaluate the policy
        (grant lookup failed) is a retryable 502, so a transient DynamoDB fault
        is not reported to every caller as a permissions change.
    """
    upstream_user_id, upstream_groups = resolve_principal(payload_profile, jwt_user_id, jwt_email, jwt_groups)

    try:
        resolved = await asyncio.to_thread(
            resolve_profile, upstream_user_id, upstream_groups, namespace=namespace, email=jwt_email
        )
    except Exception:
        # A grant-lookup failure must never widen access — reject rather than
        # dispatch retrieval with an unresolved (empty) profile. Reported as a
        # retryable 502 (not 403) because the caller's permissions are unknown,
        # not denied: a DynamoDB blip must not look like mass access revocation.
        logger.warning("namespace_authz_resolve_failed", namespace=namespace, request_id=request_id, exc_info=True)
        return None, DataSourceUnavailableError("Unable to verify access").to_dict(request_id)

    gate_profile: dict[str, Any] = {}
    resolved.inject_into(gate_profile)
    decision = await asyncio.to_thread(_orchestrator.authorize_namespace, namespace, gate_profile)
    if decision.denied:
        logger.warning(
            "namespace_authz_denied",
            namespace=namespace,
            user_id=upstream_user_id or "unknown",
            request_id=request_id,
        )
        return None, AccessDeniedError(decision.reason or "not permitted to query this namespace").to_dict(request_id)

    return resolved, None


@app.entrypoint
async def invoke(payload: dict, context=None):
    """Main entrypoint — AgentCore @app.entrypoint streaming handler.

    All consumer surfaces (Lambda handlers, Playground SSE, MCP) route here.
    Payload follows the InvokeRequest schema.

    This is an async generator:
    - Non-streaming responses (session actions, data-layer): yield single dict, return
    - Streaming queries (Playground SSE): yield multiple SSE event strings

    Health checks use the built-in GET /ping endpoint per AgentCore protocol.
    """
    logger.info("invoke_entry", has_context=context is not None, payload_keys=list(payload.keys()) if payload else [])

    try:
        await _ensure_initialized()
    except Exception as e:
        logger.error("invoke_init_failed", error=str(e))
        yield {"error": "InternalError", "message": "Initialization failed", "statusCode": 500}
        return

    if _orchestrator is None:
        yield {"error": "InternalError", "message": "Service not ready", "statusCode": 503}
        return

    request_id = payload.get("requestId") or str(uuid.uuid4())

    # ── Extract authenticated user identity ───────────────────────────────
    # See identity.py for the full trust model explanation.
    _jwt_user_id, _jwt_email, _jwt_groups = extract_jwt_identity(context)
    if not context:
        logger.info("invoke_no_context", payload_keys=list(payload.keys()))

    # ── Session CRUD actions (delegated to session_actions module) ──────
    action = payload.get("action")
    if action == "getRecentSession" and _session_metadata:
        user_id = resolve_user_id(payload, _jwt_user_id, _jwt_email)
        if not user_id:
            yield {
                "error": "ValidationError",
                "message": "userId required in profile",
                "requestId": request_id,
                "statusCode": 400,
            }
            return
        yield await handle_get_recent_session(payload, request_id, user_id, _session_metadata, _session_manager)
        return

    if action == "getSessionHistory" and _session_manager:
        user_id = resolve_user_id(payload, _jwt_user_id, _jwt_email)
        yield await handle_get_session_history(payload, request_id, user_id, _session_manager, _session_metadata)
        return

    if action == "createSession" and _session_metadata:
        user_id = resolve_user_id(payload, _jwt_user_id, _jwt_email)
        yield await handle_create_session(payload, request_id, user_id, _session_metadata)
        return

    if action == "listSessions" and _session_metadata:
        user_id = resolve_user_id(payload, _jwt_user_id, _jwt_email)
        yield await handle_list_sessions(payload, request_id, user_id, _session_metadata)
        return

    if action == "deleteSession" and _session_metadata:
        user_id = resolve_user_id(payload, _jwt_user_id, _jwt_email)
        yield await handle_delete_session(payload, request_id, user_id, _session_metadata)
        return

    # ── Data Layer isolated actions ────────────────────────────────────────
    # Strip injectable ROLE fields only. Keep identity fields (userId, groups,
    # email) — the full-query path below needs them for resolve_profile().
    payload.setdefault("profile", {})
    for reserved in ("globalRoles", "resourceRoles", "sub", "tableAllowlist", "columnDenylist", "allowedMetrics"):
        payload["profile"].pop(reserved, None)

    # Query-bearing actions are validated before namespace lookup or any other
    # backend call. The full query path validates again through InvokeRequest;
    # this early gate protects isolated actions and prevents oversized input from
    # reaching synchronous Unicode segmentation.
    if action in (None, "translate", "kbSearch"):
        try:
            payload["query"] = validate_query_text(payload.get("query"))
        except ValueError:
            yield {
                "error": "ValidationError",
                "message": "Invalid request payload",
                "details": [{"field": "query", "type": "value_error"}],
                "requestId": request_id,
                "statusCode": 400,
            }
            return

    # ── Namespace format validation ────────────────────────────────────────
    # Reject malformed namespaces (path traversal, spaces, special chars) with a
    # 400 BEFORE any downstream lookup, so callers get a validation error instead
    # of a 404 masking their input mistake. The regex matches the constraint on
    # InvokeRequest.namespace so behavior is identical between paths.
    namespace = payload.get("namespace", "")
    if namespace and not _NAMESPACE_PATTERN.match(namespace):
        yield {
            "error": "ValidationError",
            "message": "Invalid namespace format",
            "details": [{"field": "namespace", "type": "pattern_mismatch"}],
            "requestId": request_id,
            "statusCode": 400,
        }
        return

    # ── Namespace existence gate (query + retrieval actions) ───────────────
    # translate / kbSearch / graphTraverse and the query path below all operate
    # on a specific namespace. A request for a namespace that does not exist must
    # return 404 — otherwise the query path silently falls through to a Tier-3
    # "no information available" 200, masking a caller's typo'd/stale namespace.
    # Session CRUD actions returned above and are intentionally NOT gated here.
    # namespace_exists() returns None when it cannot check. We fail CLOSED when the
    # namespaces table IS configured (so None means a genuine lookup error — do not
    # serve a namespace we could not verify, F-2). When the table is NOT configured
    # (feature absent, e.g. a minimal/dev deployment) None means "cannot check" and
    # we proceed, so an unconfigured deployment stays usable.
    if namespace and _sources_registry is not None:
        namespace_present = await _sources_registry.namespace_exists(namespace)
        if namespace_present is False:
            yield NamespaceNotFoundError(f"Namespace '{namespace}' not found").to_dict(request_id)
            return
        if namespace_present is None and _sources_registry.namespaces_configured:
            logger.warning("namespace_exists_indeterminate_fail_closed", namespace=namespace, request_id=request_id)
            yield DataSourceUnavailableError("Unable to verify namespace").to_dict(request_id)
            return

    # ── Namespace admission gate (Cedar) ───────────────────────────────────
    # Run the coarse "may this principal query this namespace at all?" decision
    # ONCE, here, before dispatching either the isolated retrieval actions
    # (translate/kbSearch/graphTraverse) or the Tier-1/2/3 query path. Previously
    # this gate existed only inside SQLFirewall at the Tier-1/2 SQL-execution
    # sites, so the isolated actions and the entire Tier-3 retrieval/synthesis
    # path were ungated — any authenticated IdP user could read any namespace's
    # chunks, graph and synthesized answers (F-2, CWE-862). Session CRUD actions
    # returned above and are intentionally NOT gated (they are user-scoped, not
    # namespace-data reads). The resolved profile is reused by the query path
    # below to avoid a second grant lookup.
    resolved_profile: ResolvedProfile | None = None
    if namespace:
        resolved_profile, authz_error = await _authorize_namespace_access(
            namespace, _jwt_user_id, _jwt_email, _jwt_groups, payload.get("profile", {}), request_id
        )
        if authz_error is not None:
            yield authz_error
            return

    try:
        if action == "translate":
            yield await _handle_translate(payload, request_id)
            return
        if action == "kbSearch":
            yield await _handle_kb_search(payload, request_id)
            return
        if action == "graphTraverse":
            yield await _handle_graph_traverse(payload, request_id)
            return
    except ServeError as e:
        yield e.to_dict(request_id)
        return

    # ── Query resolution (streaming SSE) ──────────────────────────────────
    try:
        request = InvokeRequest(
            query=payload.get("query", ""),
            namespace=payload.get("namespace", ""),
            profile=payload.get("profile", {}),
            options=payload.get("options", {}),
        )
    except ValidationError as e:
        logger.info("invoke_validation_error", errors=e.error_count(), request_id=request_id)
        yield {
            "error": "ValidationError",
            "message": "Invalid request payload",
            "details": _format_validation_errors(e),
            "requestId": request_id,
            "statusCode": 400,
        }
        return

    # JWT identity is authoritative when present. The request
    # body is attacker-controlled for direct callers (Playground SSE), so we must
    # NOT trust profile.userId/groups over the cryptographically-validated JWT.
    # profile.* values are only used as fallback when no JWT is forwarded.
    upstream_user_id, upstream_groups = resolve_principal(request.profile, _jwt_user_id, _jwt_email, _jwt_groups)
    _reserved_keys = (
        "userId",
        "groups",
        "globalRoles",
        "resourceRoles",
        "sub",
        "email",
        "tableAllowlist",
        "columnDenylist",
        "allowedMetrics",
    )
    injected = [k for k in _reserved_keys if k in request.profile and k not in ("userId", "groups", "email")]
    if injected:
        logger.warning(
            "client_role_injection_stripped",
            stripped_keys=injected,
            user_id=upstream_user_id or "unknown",
            request_id=request_id,
        )
    for reserved in _reserved_keys:
        request.profile.pop(reserved, None)
    if resolved_profile is not None:
        # Reuse the profile resolved for the admission gate above — same principal,
        # same namespace — so the query path does not repeat the grant lookup.
        resolved_profile.inject_into(request.profile)
    elif upstream_user_id:
        # Reached only when the admission gate did not run (no namespace on the
        # request), so this is the non-namespace-scoped fallback. Threaded for the
        # same reason as the gate's own lookup: resolve_profile does blocking
        # DynamoDB I/O and must not run on the event loop.
        resolved = await asyncio.to_thread(
            resolve_profile, upstream_user_id, upstream_groups, namespace=request.namespace, email=_jwt_email
        )
        resolved.inject_into(request.profile)

    logger.info(
        "invoke",
        namespace=request.namespace,
        query_length=len(request.query),
        request_id=request_id,
    )

    # ── Determine streaming mode ──────────────────────────────────────────
    # If "stream": true in payload, yield SSE events progressively.
    # Otherwise yield a single dict (backward compat for MCP/data-layer callers).
    stream_mode = payload.get("stream", False)

    if not stream_mode:
        async for event in _handle_blocking_query(request, request_id):
            yield event
        return

    # ── Streaming SSE mode ────────────────────────────────────────────────
    session_user_id = _jwt_user_id or upstream_user_id
    async for event in _handle_streaming_query(payload, request, request_id, session_user_id):
        yield event


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
