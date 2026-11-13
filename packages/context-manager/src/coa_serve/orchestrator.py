# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core orchestrator — sequential Tier 1 → 2 (strategy-based) → 3 resolution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import structlog
from coa_common import ontology_vector_index_name
from coa_common.constants import StructuredBackend
from coa_common.sparql_config import graph_sources_enabled

from .clients.base import LLMClient, QueryExecutor, VectorClient
from .exceptions import AccessDeniedError, DataSourceUnavailableError, NamespaceScopeDeniedError, NoResultError
from .identity import display_principal
from .mode import Mode, resolve_mode
from .tier2.sql_firewall import NamespaceSQLScopeError, tables_visible_to

if TYPE_CHECKING:
    from .clients.sources_registry import SourceComposition, SourcesRegistry
    from .deadline import Deadline
    from .tier2.skip import Tier2SkipEvaluator
    from .tier3.agentic.retriever import AgenticRetriever
from .model_validation import validate_model_id as _validate_model_id
from .models import InvokeRequest, InvokeResponse
from .response_assembler import ResponseAssembler
from .schema_visibility import SchemaVisibility, request_schema_visibility, resolve_schema_visibility
from .step_ids import StepId
from .tier1.metric_resolver import DeclinedMetricContext, MetricMatch, MetricResolver
from .tier2.skip import Tier2SkipDecision
from .tier2.sql_firewall import FirewallResult, SQLFirewall
from .tier2.strategy import (
    DEFAULT_STRATEGY,
    EMPTY_RESULT_CONFIDENCE_FLOOR,
    NOT_ANSWERABLE_FIELD,
    StrategyContext,
    StrategyOption,
    StrategyResult,
    StructuredQueryTier,
    capped_max_rows,
    normalize_strategy_option,
    not_answerable_kind,
)
from .tier3.agentic.defaults import with_recommended_options
from .tier3.knowledge_retriever import VECTOR_SEARCH_TIMEOUT_S, KnowledgeRetriever
from .trace import TraceCollector, client_error_detail

logger = structlog.get_logger(__name__)

_VALID_TIER_OVERRIDES = {1, 2, 3}


@dataclass(frozen=True)
class TierGating:
    """Which tiers source-composition gating has ruled out for a request.

    The default (all False) is the fail-open "skip nothing" state used on the
    override / unknown / no-registry paths.

    Note the granularity difference: ``skip_tier1`` / ``skip_tier2`` gate a whole
    tier, whereas ``skip_tier3_vector_search`` suppresses only Tier 3's vector
    sub-step (graph traversal + synthesis still run) — hence the longer name.
    """

    skip_tier1: bool = False
    skip_tier2: bool = False
    skip_tier3_vector_search: bool = False
    # Not a skip: which structured backends the namespace has, for Tier-2 strategy
    # selection. ``None`` (no registry / lookup failed) means the SQL default.
    structured_backends: frozenset[StructuredBackend] | None = None

    @classmethod
    def from_composition(cls, composition: SourceComposition) -> TierGating:
        """Map a namespace's source composition to tier skips.

        Tier 2 needs a structured source of either backend. Tier 1 needs a SQL one
        specifically: a governed metric is a SQL formula, so a graph-only namespace
        has nothing for it to execute against. Tier 3's vector search needs a
        DOCUMENTS-derived chunk index.
        """
        backends = composition.structured_backends or frozenset()
        return cls(
            skip_tier1=StructuredBackend.SQL not in backends,
            skip_tier2=not backends,
            skip_tier3_vector_search=not composition.has_unstructured_source,
            structured_backends=backends,
        )

    @property
    def skipped(self) -> list[str]:
        """Tier labels that were gated out, for the ROUTING_SELECT trace detail."""
        return [
            label
            for label, skipped in (
                ("tier1", self.skip_tier1),
                ("tier2", self.skip_tier2),
                ("tier3_vector_search", self.skip_tier3_vector_search),
            )
            if skipped
        ]

    @property
    def any_skipped(self) -> bool:
        """Return whether any tier was gated out by source composition."""
        return bool(self.skipped)


class Orchestrator:
    """Coordinates tiered query resolution (metric → structured → knowledge)."""

    def __init__(
        self,
        metric_resolver: MetricResolver,
        knowledge_retriever: KnowledgeRetriever,
        structured_query_tier: StructuredQueryTier,
        query_executor: QueryExecutor | None = None,
        bedrock_client: LLMClient | None = None,
        firewall: SQLFirewall | None = None,
        lexical_retriever_strategy: str | None = None,
        sources_registry: SourcesRegistry | None = None,
        tier2_skip_evaluator: Tier2SkipEvaluator | None = None,
        vector_client: VectorClient | None = None,
        oss_ontology_index: str | None = None,
        agentic_retriever: AgenticRetriever | None = None,
        tier3_deep_reasoning_default: bool = False,
        tier1_metric_timeout_s: int = 35,
    ):
        """Wire the per-tier resolvers and optional gating/skip helpers.

        Args:
            metric_resolver: Tier-1 pre-defined metric resolver.
            knowledge_retriever: Tier-3 knowledge retriever/synthesizer.
            structured_query_tier: Tier-2 structured-query (VKG / NL-to-SQL) tier.
            query_executor: Optional SQL executor shared across tiers.
            bedrock_client: Optional LLM client; supplies the default model id.
            firewall: SQLFirewall instance; defaults to a fresh one.
            lexical_retriever_strategy: Optional default lexical retriever strategy.
            sources_registry: Optional registry enabling source-composition tier gating.
            tier2_skip_evaluator: Optional evaluator that prunes Tier 2 for
                document-flavored queries in mixed namespaces.
            vector_client: Optional vector client for Tier-3 URI-hop seeding via
                multilingual ontology-concept vector search.
            oss_ontology_index: Optional AOSS ontology index name backing the
                Tier-3 seed-URI vector search.
            agentic_retriever: Optional deep-reasoning Tier-3 retriever. When present
                it can be engaged per-request (options.mode=="deep-reasoning") or as
                the deployment default; when None deep reasoning is unavailable.
            tier3_deep_reasoning_default: Whether deep reasoning is the deployment
                default for Tier 3 (set when
                config.tier3_strategy=="deep-reasoning").
            tier1_metric_timeout_s: Configured Tier-1 statement timeout (1..300).
        """
        self._metric_resolver = metric_resolver
        self._knowledge_retriever = knowledge_retriever
        self._structured_query_tier = structured_query_tier
        self._query_executor = query_executor
        self._bedrock_client = bedrock_client
        model_id = bedrock_client.model_id if bedrock_client else "unknown"
        self._assembler = ResponseAssembler(model_id=model_id)
        self._firewall = firewall or SQLFirewall()
        self._lexical_retriever_strategy = lexical_retriever_strategy
        self._sources_registry = sources_registry
        # Optional: prune Tier 2 for a document-flavored query in a MIXED namespace.
        # Only a SKIP decision prunes Tier 2; RUN/ABSTAIN run it as normal.
        self._tier2_skip_evaluator = tier2_skip_evaluator
        # Optional Tier-3 URI-hop seeding: an ontology-concept vector search resolves
        # seed URIs so graph traversal is not limited to the ASCII-only keyword branch.
        self._vector_client = vector_client
        self._oss_ontology_index = oss_ontology_index
        self._agentic_retriever = agentic_retriever
        self._tier3_deep_reasoning_default = tier3_deep_reasoning_default
        if not 1 <= tier1_metric_timeout_s <= 300:
            raise ValueError("tier1_metric_timeout_s must be in the range 1..300")
        self._tier1_metric_timeout_s = tier1_metric_timeout_s

    async def resolve(
        self,
        request: InvokeRequest,
        *,
        trace: TraceCollector | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        deadline: Deadline | None = None,
    ) -> InvokeResponse:
        """Resolve a query through the tier cascade and return the first hit.

        Applies source-composition gating and optional tier overrides, then tries
        Tier 1 (metrics), Tier 2 (structured query), and Tier 3 (knowledge
        synthesis) in order until one produces a result.

        Args:
            request: The inbound query request (question, namespace, options).
            trace: Optional trace collector; a new one is created when omitted.
            on_token: Optional async callback for streaming Tier-3 tokens.
            conversation_history: Optional prior turns for conversational context.
            deadline: Optional request-scoped time budget. When supplied, it is
                threaded into the Tier-2 StrategyContext so the NL→SQL correction
                shot can skip itself rather than overrun the transport ceiling. None
                means strategies use fixed internal timeouts.

        Returns:
            The assembled InvokeResponse from the first tier that resolves.

        Raises:
            ValueError: If ``tierOverride`` is not 1, 2, or 3.
            NoResultError: If a pinned tier exhausts its fall-through with no result.
        """
        if trace is None:
            trace = TraceCollector()
        query = request.query
        namespace = request.namespace
        profile = request.profile
        options = request.options

        tier_override = options.get("tierOverride")
        if tier_override is not None and tier_override not in _VALID_TIER_OVERRIDES:
            raise ValueError(f"Invalid tierOverride: {tier_override!r}. Must be 1, 2, or 3.")

        model_id_override = options.get("modelId")
        if model_id_override:
            _validate_model_id(model_id_override)
            logger.info("model_override_requested", model_id=model_id_override)

        start_tier = options.get("startTier", 1)

        # ── Source-composition gating (perf optimization) ────────────
        # Skip tiers that cannot contribute given the namespace's source composition
        # (see TierGating.from_composition). Applied only on the automatic path and
        # only on a positive zero-count signal; unknown/errors fail open.
        gating = await self._resolve_source_gating(namespace, tier_override, trace)

        tier2_sparql: str | None = None
        metric_definitions: list[dict] | None = None
        # The governed definition Tier-1 matched but declined, if its
        # residual-qualifier gate fired. Threaded into Tier 2 so the authored formula
        # survives the decline instead of being discarded with the match.
        declined_metric: DeclinedMetricContext | None = None

        # ── Deep-reasoning mode owns the whole request ───────────────
        # An explicit options.mode="deep-reasoning" hands the request to the
        # reasoning loop directly instead of running the T1 -> T2 cascade first; the
        # loop has nl_to_sql and nl_to_sparql as tools. An explicit tierOverride
        # still wins.
        if tier_override is None and self._is_deep_reasoning_engaged(options):
            return await self._run_tier3(
                query,
                namespace,
                trace,
                profile=profile,
                tier2_sparql=None,
                metric_definitions=None,
                embedding=None if gating.skip_tier3_vector_search else await self._embed_query(query, None, trace),
                on_token=on_token,
                conversation_history=conversation_history,
                options=options,
                model_id=model_id_override,
                has_structured_source=not gating.skip_tier1,
                has_unstructured_source=not gating.skip_tier3_vector_search,
            )

        if self._should_run_tier1(tier_override, start_tier, gating):
            result, declined_metric = await self._run_tier1(
                query,
                namespace,
                profile,
                options,
                trace,
                model_id=model_id_override,
                deadline=deadline,
            )
            if result:
                return result
            metric_definitions = self._metric_resolver.list_all(namespace) or None

        # Query embedding — shared by Tier 2 (class retrieval) and Tier 3 (vector search).
        query_embedding = await self._embed_query(query, tier_override, trace)

        strategy = self._resolve_strategy_selection(options, tier_override)
        if await self._should_run_tier2(
            strategy, gating, tier_override, options, query, namespace, query_embedding, trace
        ):
            strategy_result, tier2_sparql = await self._run_tier2(
                query,
                namespace,
                profile,
                options,
                trace,
                embedding=query_embedding,
                strategy_selection=strategy,
                tier_override=tier_override,
                model_id=model_id_override,
                deadline=deadline,
                declined_metric=declined_metric,
                has_unstructured_source=not gating.skip_tier3_vector_search,
                structured_backends=gating.structured_backends,
            )
            if strategy_result:
                return strategy_result

        if self._should_run_tier3(tier_override):
            # For a structured-only namespace there is no chunk_ index, so pass
            # embedding=None: Tier 3's vector-search sub-step self-skips (via the
            # `embedding is not None` gate in KnowledgeRetriever.resolve) while graph
            # traversal + synthesis still run. tier2_sparql grounding used the real one.
            return await self._run_tier3(
                query,
                namespace,
                trace,
                profile=profile,
                tier2_sparql=tier2_sparql,
                metric_definitions=metric_definitions,
                embedding=None if gating.skip_tier3_vector_search else query_embedding,
                on_token=on_token,
                conversation_history=conversation_history,
                options=options,
                model_id=model_id_override,
                # Seed URIs need the query embedding regardless of the vector-search
                # skip gate above, so pass it explicitly (not the possibly-None
                # `embedding` param). None only when Tier-1-pinned (no embed ran).
                seed_embedding=query_embedding,
                # gating.skip_tier1 is the no-structured-source signal and
                # skip_tier3_vector_search the no-unstructured-source one. The agentic
                # path uses both to withhold inapplicable tools from the planner:
                # structured tools on a document-only namespace, and the chunk/topic
                # retrievers on a structured-only one.
                has_structured_source=not gating.skip_tier1,
                has_unstructured_source=not gating.skip_tier3_vector_search,
            )

        # A pinned tier that yields nothing is "no data found" (NoResultError, 404),
        # not a server fault.
        raise NoResultError(
            f"No result for the requested tier (tierOverride={tier_override})",
            tier=tier_override if isinstance(tier_override, int) else None,
        )

    def authorize_namespace(self, namespace: str, profile: dict) -> FirewallResult:
        """Coarse "may this principal query this namespace?" admission decision.

        Delegates to the SQL firewall's Cedar gate so there is exactly one policy
        source/cache. This is the single authorization point for the retrieval
        surfaces that never build SQL — the isolated kbSearch/graphTraverse/
        translate actions and the Tier-3 path — which otherwise reach a data sink
        without any namespace authorization (CWE-862). The Tier-1/2 SQL path
        keeps its own firewall.evaluate() gate; this runs upstream of dispatch.
        """
        return self._firewall.authorize_namespace(namespace, profile)

    @staticmethod
    def _debug_info_requested(options: dict | None) -> bool:
        """Whether the caller opted in to schema artifacts (``includeDebugInfo=true``)."""
        return (options or {}).get("includeDebugInfo", False) is True

    def schema_visibility(self, namespace: str, profile: dict | None) -> SchemaVisibility:
        """The caller's schema rights (``viewSource`` / ``viewOntology``), fail-closed.

        Used by the serve entrypoint to scrub every response that can carry
        schema (see :mod:`coa_serve.schema_visibility`).
        """
        return resolve_schema_visibility(self._firewall, namespace, profile or {})

    def _resolve_include_sql(self, options: dict | None, namespace: str, profile: dict | None) -> bool:
        """Whether the generated SQL artifact may be returned (opt-in AND ``viewSource``).

        Reuses the visibility the serve entrypoint already resolved for this
        request (no second Cedar evaluation on the event loop); without one it
        asks the firewall, which fails closed.
        """
        if not self._debug_info_requested(options):
            return False
        vis = request_schema_visibility(namespace)
        return vis.source if vis is not None else self._firewall.can_view_source(namespace, profile)

    def _resolve_include_sparql(self, options: dict | None, namespace: str, profile: dict | None) -> bool:
        """Whether the generated SPARQL artifact may be returned (opt-in AND ``viewOntology``)."""
        if not self._debug_info_requested(options):
            return False
        vis = request_schema_visibility(namespace)
        return vis.ontology if vis is not None else self._firewall.can_view_ontology(namespace, profile)

    @staticmethod
    def _should_run_tier1(tier_override: int | None, start_tier: int, gating: TierGating) -> bool:
        """Tier 1 runs on the automatic path or a Tier-1 pin, unless gated out."""
        if gating.skip_tier1:
            return False
        return (tier_override is None or tier_override == 1) and start_tier <= 1

    @staticmethod
    def _should_run_tier3(tier_override: int | None) -> bool:
        """Tier 3 is the fallback: it runs on the automatic path or a Tier-3 pin."""
        return tier_override is None or tier_override == 3

    @staticmethod
    def _unhandled_qualifier(metric_match: MetricMatch, options: dict) -> str:
        """Return the part of the question Tier-1 cannot honour, or ``""``.

        Tier-1 executes the matched metric's SQL verbatim and parses no filter,
        grouping, or time window from the question, so any unconsumed text is a
        qualifier it would silently drop.

        Not unhandled: a request carrying ``options.dimensions`` (the supported
        filter channel), or a ``tierOverride=1`` pin (only the automatic path
        re-routes).
        """
        if not metric_match.residual:
            return ""
        if options.get("dimensions"):
            return ""
        if options.get("tierOverride") == 1:
            return ""
        return metric_match.residual

    async def _should_run_tier2(
        self,
        strategy: StrategyOption | None,
        gating: TierGating,
        tier_override: int | None,
        options: dict,
        query: str,
        namespace: str,
        query_embedding: list[float] | None,
        trace: TraceCollector,
    ) -> bool:
        """Single Tier-2 run/skip decision, folding every skip reason into one bool.

        Skips Tier 2 when:
          • no strategy was selected (caller pinned Tier 1/3 or startTier > 2), or
          • namespace source-composition gating ruled it out (no DATABASE source), or
          • per-query pruning proves it useless (this query's ontology classes are
            all unmapped, so NL→SQL/SPARQL cannot be generated).

        The per-query pruner runs ONLY on the automatic path — never when a
        tierOverride or an explicit strategy is set (both are caller intent the
        optimization must not override).
        """
        if strategy is None or gating.skip_tier2:
            return False

        if self._tier2_skip_evaluator is None or tier_override is not None or self._has_explicit_strategy(options):
            return True

        decision = await self._tier2_skip_evaluator.should_skip_tier2(query, namespace, query_embedding, trace=trace)
        return decision is not Tier2SkipDecision.SKIP

    async def _embed_query(
        self,
        query: str,
        tier_override: int | None,
        trace: TraceCollector,
    ) -> list[float] | None:
        """Embed the query for Tier 2/3, tracing the call.

        Returns None when no embedding is needed (a Tier-1-only pin), the Bedrock
        client is absent, or embedding fails.
        """
        # Only Tier 2 and Tier 3 consume the embedding; a Tier-1 pin needs none.
        if self._bedrock_client is None or tier_override == 1:
            return None

        embed_start = time.perf_counter()
        try:
            embedding = await self._bedrock_client.embed(query)
            trace.record(
                StepId.QUERY_EMBED,
                "success",
                int((time.perf_counter() - embed_start) * 1000),
                detail={"dimensions": len(embedding)},
                tool_used="bedrock",
            )
            return embedding
        except Exception as e:
            # Trace carries only the exception type — raw str(e) can leak internal
            # detail to the client; full error stays in logs.
            trace.record(
                StepId.QUERY_EMBED,
                "error",
                int((time.perf_counter() - embed_start) * 1000),
                detail={"error": type(e).__name__},
                tool_used="bedrock",
            )
            logger.warning("embedding_generation_failed", error=str(e))
            return None

    async def _resolve_seed_uris(self, embedding: list[float] | None, namespace: str) -> list[str]:
        """Resolve ontology-concept seed URIs for Tier-3 URI-hop graph traversal.

        Runs the same multilingual ontology-concept vector search Tier 2 uses, so a
        non-Latin query, which the ASCII-only keyword traverser drops, still seeds
        graph traversal. Fail-open: returns [] when the ontology index or vector
        client is unconfigured, the embedding is missing, or the search errors.

        The vector client returns the base VectorHit, whose entity type and URI
        live in ``metadata`` under ``entity_type`` / ``entity_uri`` (stamped at
        ingest).
        """
        if not self._vector_client or not self._oss_ontology_index or embedding is None:
            return []
        try:
            index_name = ontology_vector_index_name(self._oss_ontology_index, namespace)
            # Bound the seed search by the same timeout Tier 3's other retrieval
            # sub-steps use, so a slow AOSS collection cannot stall the request on
            # the 60s OpenSearch client default. Timeout fails open like any other
            # error → keyword-branch fallback.
            hits = await asyncio.wait_for(
                self._vector_client.search(embedding, namespace=namespace, top_k=10, index=index_name),
                timeout=VECTOR_SEARCH_TIMEOUT_S,
            )
        except Exception as e:  # service boundary (incl. TimeoutError): never let seeding break Tier 3
            logger.warning("tier3_seed_uri_search_failed", error=str(e))
            return []
        uris: list[str] = []
        for h in hits:
            if h.metadata.get("entity_type") in ("class", "property"):
                uri = h.metadata.get("entity_uri", "")
                if uri:
                    uris.append(uri)
        logger.info("tier3_seed_uris_resolved", count=len(uris), namespace=namespace, seed_uris=uris[:10])
        return uris

    async def _resolve_source_gating(
        self,
        namespace: str,
        tier_override: int | None,
        trace: TraceCollector,
    ) -> TierGating:
        """Decide which tiers to skip based on the namespace's source composition.

        Returns a :class:`TierGating` (all-False = skip nothing).

        Tier 1 needs a SQL source and Tier 2 any structured source (see
        :meth:`TierGating.from_composition`).

        Fail-open: no registry configured, an explicit ``tierOverride``, or an
        unknown composition skips nothing; only a positive zero-count signal
        (``has_* == False``) skips. The result is traced (ROUTING_SELECT) only when
        something is skipped.
        """
        # With graph sources off, graph backends are ignored: a pin skips the lookup,
        # a graph-only namespace gates as one without structured sources, and a
        # mixed one keeps only SQL.
        graph_sources = graph_sources_enabled()
        if self._sources_registry is None or (tier_override is not None and not graph_sources):
            return TierGating()

        try:
            composition: SourceComposition = await self._sources_registry.get_source_composition(namespace)
        except Exception as e:  # defensive: any unexpected error must fail open
            logger.warning("source_gating_lookup_failed", namespace=namespace, error=type(e).__name__)
            return TierGating()

        if not graph_sources:
            backends = (composition.structured_backends or frozenset()) - {StructuredBackend.RDF_SPARQL}
            composition = replace(composition, has_structured_source=bool(backends), structured_backends=backends)

        # A pin or an uncertain read skips nothing, but strategy selection still
        # needs the backends — a graph-only namespace pinned to Tier 2 must get the
        # SPARQL strategy. (An unknown composition reports {SQL}: fail safe.)
        if tier_override is not None or composition.unknown:
            return TierGating(structured_backends=composition.structured_backends or None)

        gating = TierGating.from_composition(composition)

        if gating.any_skipped:
            logger.info(
                "source_composition_gating",
                namespace=namespace,
                has_structured_source=composition.has_structured_source,
                has_unstructured_source=composition.has_unstructured_source,
                skipped=gating.skipped,
            )
            trace.record(
                StepId.ROUTING_SELECT,
                "success",
                0,
                detail={
                    "gating": "source_composition",
                    "hasStructuredSource": composition.has_structured_source,
                    "hasUnstructuredSource": composition.has_unstructured_source,
                    "skipped": gating.skipped,
                },
            )

        return gating

    # Strategy values a caller can explicitly pin via options["strategy"]. An
    # explicit pin is user intent and must never be silently overridden by an
    # automatic optimization (tier gating / per-query Tier-2 pruning).
    _EXPLICIT_STRATEGY_OPTIONS = frozenset(
        {
            StrategyOption.BEST,
            StrategyOption.ONTOP,
            StrategyOption.NL_TO_SQL,
            StrategyOption.ONTOP_FIRST,
            StrategyOption.NL_TO_SQL_FIRST,
            StrategyOption.DEEP_REASONING,
            StrategyOption.NL_TO_SPARQL,
        }
    )

    @classmethod
    def _explicit_strategy(cls, options: dict) -> StrategyOption | None:
        """The Tier-2 strategy the caller pinned, if any.

        With graph sources off ``nl_to_sparql`` is not a pin, so the request runs
        the default chain.
        """
        strategy = normalize_strategy_option(options.get("strategy"))
        if strategy not in cls._EXPLICIT_STRATEGY_OPTIONS:
            return None
        if strategy == StrategyOption.NL_TO_SPARQL and not graph_sources_enabled():
            return None
        return strategy

    @classmethod
    def _has_explicit_strategy(cls, options: dict) -> bool:
        """True when the caller pinned a specific Tier-2 strategy."""
        return cls._explicit_strategy(options) is not None

    @classmethod
    def _resolve_strategy_selection(cls, options: dict, tier_override: int | None) -> StrategyOption | None:
        """Which Tier-2 strategy to run, or ``None`` if Tier 2 should not run.

        ``None`` means "no structured strategy applies" (a Tier-1/3 pin, or
        startTier > 2) — the caller then skips Tier 2. This never encodes a
        capability skip (gating / per-query pruning); those are decided in
        ``_should_run_tier2``.
        """
        # An explicit strategy pin takes highest precedence (caller intent).
        strategy = cls._explicit_strategy(options)
        if strategy is not None:
            return strategy

        # tierOverride=2 pins Tier 2 with the default strategy (no Tier-3 fallback).
        if tier_override == 2:
            return DEFAULT_STRATEGY

        # tierOverride=1/3, or startTier past Tier 2 → Tier 2 does not run.
        if tier_override in (1, 3) or options.get("startTier", 1) > 2:
            return None

        return DEFAULT_STRATEGY

    async def _run_tier2(
        self,
        query: str,
        namespace: str,
        profile: dict,
        options: dict,
        trace: TraceCollector,
        *,
        embedding: list[float] | None = None,
        strategy_selection: StrategyOption,
        tier_override: int | None,
        model_id: str | None = None,
        deadline: Deadline | None = None,
        declined_metric: DeclinedMetricContext | None = None,
        has_unstructured_source: bool = True,
        structured_backends: frozenset[StructuredBackend] | None = None,
    ) -> tuple[InvokeResponse | None, str | None]:
        """Run Tier 2 strategies and return (response, sparql_for_tier3_grounding).

        Called only when a strategy applies, so ``strategy_selection`` is always a
        real StrategyOption (never a skip sentinel).

        ``declined_metric`` carries the governed definition Tier-1 matched but
        declined, so the SQL writer extends the authored formula rather than
        composing a replacement for it.
        """
        context = StrategyContext(
            embedding=embedding,
            profile=profile,
            options=options,
            trace=trace,
            model_id=model_id,
            deadline=deadline,
            declined_metric=declined_metric,
        )

        result = await self._structured_query_tier.resolve(
            query, namespace, context, option=strategy_selection, structured_backends=structured_backends
        )

        if result is not None:
            max_rows = capped_max_rows(options)
            if len(result.rows) > max_rows:
                result = replace(result, rows=result.rows[:max_rows], row_count=max_rows, truncated=True)

            # Confidence gate: unless the caller pinned Tier 2, a result below the
            # floor falls through to Tier 3 even when it returned rows.
            # A NOT_ANSWERABLE_RANGE row is final whatever the confidence: the tables
            # are the authority. A NOT_ANSWERABLE_FIELD row is final only when the
            # namespace has no unstructured source; otherwise a document may hold the
            # answer, so it falls through to Tier 3.
            refusal_kind = not_answerable_kind(result)
            field_to_tier3 = refusal_kind == NOT_ANSWERABLE_FIELD and has_unstructured_source
            keep_refusal = refusal_kind is not None and not field_to_tier3
            low_confidence = result.confidence < EMPTY_RESULT_CONFIDENCE_FLOOR
            if keep_refusal and low_confidence:
                logger.info(
                    "tier2_not_answerable_final",
                    kind=refusal_kind,
                    tier2_confidence=result.confidence,
                    strategy=result.strategy_name,
                    namespace=namespace,
                )
            if tier_override != 2 and not keep_refusal and (field_to_tier3 or low_confidence):
                logger.info(
                    "tier2_not_answerable_field_fallthrough" if field_to_tier3 else "tier2_low_confidence_fallthrough",
                    tier2_confidence=result.confidence,
                    row_count=result.row_count,
                    strategy=result.strategy_name,
                    namespace=namespace,
                )
                result = None

        if result is None:
            if tier_override == 2:
                # A pinned Tier-2 miss, including a low-confidence empty result, is
                # "no data found" (404), not a server fault.
                raise NoResultError("No result for the requested tier (tierOverride=2)", tier=2)
            logger.info(
                "tier2_fallback_decision",
                query_length=len(query),
                namespace=namespace,
                failure_reason="all_strategies_failed",
            )
            return None, None

        logger.info(
            "tier2_accepted",
            query_length=len(query),
            tier2_confidence=result.confidence,
            namespace=namespace,
            strategy=result.strategy_name,
        )

        response = self._strategy_result_to_response(
            result,
            trace,
            namespace,
            profile,
            model_id=model_id,
            include_sql=self._resolve_include_sql(options, namespace, profile),
            include_sparql=self._resolve_include_sparql(options, namespace, profile),
        )
        return response, result.sparql or None

    def _strategy_result_to_response(
        self,
        result: StrategyResult,
        trace: TraceCollector,
        namespace: str,
        profile: dict,
        model_id: str | None = None,
        include_sql: bool = False,
        include_sparql: bool = False,
    ) -> InvokeResponse:
        """Convert a StrategyResult into the appropriate InvokeResponse."""
        # A native SPARQL answer is shaped like an Ontop one (SPARQL, no SQL leg), but
        # it names the graph source it ran against — Ontop's VKG spans its mappings.
        native_sparql = result.strategy_name == StrategyOption.NL_TO_SPARQL
        if result.ontop_assembly or native_sparql:
            response = InvokeResponse(
                result=self._assembler.assemble_tier2(
                    rows=result.rows,
                    columns=result.columns,
                    sql_used=result.sql or None,
                    sparql=result.sparql,
                    confidence=result.confidence,
                    # Only the executing VKG's translate response knows the snapshot
                    # version this query used; other paths report null.
                    ontology_version=result.ontology_version or None,
                    data_sources=[result.data_source_id] if native_sparql and result.data_source_id else None,
                    trace=trace,
                    namespace=namespace,
                    principal=display_principal(profile),
                    row_count=result.row_count,
                    truncated=result.truncated,
                    strategy=result.strategy_name,
                    model_id=model_id,
                    include_sql=include_sql,
                    include_sparql=include_sparql,
                )
            )
        else:
            response = InvokeResponse(
                result=self._assembler.assemble_nl_to_sql(
                    rows=result.rows,
                    columns=result.columns,
                    sql_used=result.sql,
                    confidence=result.confidence,
                    # Response metadata reaches the caller: list only the tables
                    # the grant's ``tableAllowlist`` permits (the trace steps are
                    # filtered the same way where they are recorded).
                    retrieved_tables=tables_visible_to(result.retrieved_tables, profile),
                    expanded_tables=tables_visible_to(result.expanded_tables, profile),
                    trace=trace,
                    namespace=namespace,
                    principal=display_principal(profile),
                    strategy=result.strategy_name,
                    model_id=model_id,
                    include_sql=include_sql,
                )
            )

        return response

    async def _run_tier1(
        self,
        query: str,
        namespace: str,
        profile: dict,
        options: dict,
        trace: TraceCollector,
        model_id: str | None = None,
        deadline: Deadline | None = None,
    ) -> tuple[InvokeResponse | None, DeclinedMetricContext | None]:
        """Run Tier 1 and return ``(response, declined_metric_context)``.

        The second element is populated ONLY when the residual-qualifier gate
        declined an otherwise-good match. It carries the authored formula onward so
        Tier 2 extends the governed definition instead of reinventing it; every
        other outcome — a clean hit, a miss, a multi-metric bypass, an execution
        failure — returns ``None`` for it.
        """
        t1_start = time.perf_counter()
        metric_match = await self._metric_resolver.match(query, namespace)
        t1_ms = int((time.perf_counter() - t1_start) * 1000)

        # A query referencing more than one governed metric is ambiguous for the
        # single-metric Tier-1 path, so bypass Tier 1. Checked before .found so we
        # never execute just the first of several.
        if metric_match.match_count > 1:
            trace.record(
                StepId.T1_METRIC_MATCH,
                "multi_metric_bypass",
                t1_ms,
                detail=f"{metric_match.match_count} metrics",
            )
            return None, None

        # Authorize before the residual-qualifier gate; see _enforce_allowed_metrics.
        if metric_match.found:
            self._enforce_allowed_metrics(metric_match, profile, namespace, trace, t1_ms)

        # A partial match bypasses to Tier 2 (see _unhandled_qualifier); checked
        # before .found's success trace so a declined question never records a hit.
        unhandled = self._unhandled_qualifier(metric_match, options) if metric_match.found else ""
        if unhandled:
            declined = DeclinedMetricContext.from_match(metric_match, unhandled)
            trace.record(
                StepId.T1_METRIC_MATCH,
                "residual_qualifier_bypass",
                t1_ms,
                detail={
                    "metricName": metric_match.metric_name,
                    "matchSource": metric_match.match_source,
                    # The span of the question Tier-1 could not honour.
                    "unhandledQualifier": unhandled,
                    # The span that did match.
                    "matchedText": metric_match.matched_text,
                    # Whether the authored formula was forwarded to Tier 2; False
                    # exactly when ``prompt_block`` is empty.
                    "governedDefinitionForwarded": bool(declined.prompt_block),
                },
            )
            logger.info(
                "tier1_residual_qualifier_bypass",
                namespace=namespace,
                metric=metric_match.metric_name,
                match_source=metric_match.match_source,
                matched_text=metric_match.matched_text,
                unhandled=unhandled,
                governed_definition_forwarded=bool(declined.prompt_block),
            )
            return None, declined

        if metric_match.found:
            trace.record(
                StepId.T1_METRIC_MATCH,
                "success",
                t1_ms,
                detail={
                    "metricName": metric_match.metric_name,
                    "matchSource": metric_match.match_source,
                    "confidence": metric_match.match_confidence,
                },
            )

            if not self._query_executor or not metric_match.sql_template:
                trace.record(StepId.T1_EXECUTE, "skipped", 0, detail="no executor or sql_template")
                return None, None

            # substitute caller-supplied dimension values into the metric
            # SQL template via parameterized (sqlglot-literal) binding — NOT string
            # interpolation. Done BEFORE the firewall so authz applies to the FINAL
            # executed SQL. A substitution error (unknown/missing/undeclared
            # dimension) fails closed → bypass Tier-1 to Tier-2/3 rather than run a
            # half-bound template.
            metric_sql = metric_match.sql_template
            dimensions = options.get("dimensions") or {}
            # Defense in depth: the wire shape (a LIST of DimensionFilter objects)
            # is converted to this name->value mapping by
            # ``InvokeRequest._normalize_dimensions``, which is the only supported
            # entry point. Anything else here means an unvalidated internal caller;
            # fail CLOSED to Tier-2/3 rather than let substitute_dimensions raise
            # AttributeError on .items() and surface as a blanket 500.
            if not isinstance(dimensions, dict):
                trace.record(
                    StepId.T1_METRIC_MATCH,
                    "dimension_substitution_failed",
                    t1_ms,
                    detail=f"dimensions must be a mapping, got {type(dimensions).__name__}",
                )
                logger.warning("tier1_dimensions_malformed", dimensions_type=type(dimensions).__name__)
                return None, None
            if dimensions or "{" in metric_sql or ":" in metric_sql:
                try:
                    metric_sql = self._metric_resolver.substitute_dimensions(
                        metric_match.sql_template, dimensions, metric_match.dimensions
                    )
                except ValueError as e:
                    # Trace detail carries exception type only — raw str(e) can
                    # leak SQL fragments / dimension values (potential PII).
                    # Server-side logging (below) still captures the full
                    # exception for operators.
                    trace.record(
                        StepId.T1_METRIC_MATCH,
                        "dimension_substitution_failed",
                        t1_ms,
                        detail={"error": type(e).__name__},
                    )
                    logger.info("tier1_dimension_substitution_failed", error=str(e))
                    return None, None

            # Log the pre-substitution template (placeholders, not values) to
            # avoid leaking caller-supplied dimension values (potential PII) into
            # logs.
            logger.debug("t1_sql_template", sql=metric_match.sql_template)
            # Tier-1 data-level authorization gate: executors only call .validate(),
            # so evaluate the final substituted SQL here and execute the authorized
            # (possibly rewritten) SQL, not the raw template. An empty profile means
            # no data-level restriction (sparse opt-in).
            fw_start = time.perf_counter()
            try:
                fw_result = self._firewall.evaluate(metric_sql, profile, namespace=namespace)
            except Exception as e:
                fw_ms = int((time.perf_counter() - fw_start) * 1000)
                trace.record(
                    StepId.T1_FIREWALL,
                    "error",
                    fw_ms,
                    detail=client_error_detail(e),
                    tool_used="sql-firewall",
                )
                logger.warning(
                    "tier1_firewall_error", namespace=namespace, metric=metric_match.metric_name, error=str(e)[:200]
                )
                return None, None

            fw_ms = int((time.perf_counter() - fw_start) * 1000)
            # Display label — these details are rendered in the rationale panel.
            principal_label = display_principal(profile) or "unknown"
            if fw_result.denied:
                trace.record(
                    StepId.T1_AUTHORIZE,
                    "denied",
                    fw_ms,
                    detail={"principal": principal_label, "decision": "deny"},
                    tool_used="cedar",
                )
                trace.record(
                    StepId.T1_FIREWALL,
                    "denied",
                    fw_ms,
                    detail={"reason": fw_result.reason},
                    tool_used="sql-firewall",
                )
                logger.warning("tier1_firewall_denied", namespace=namespace, reason=fw_result.reason)
                raise AccessDeniedError(fw_result.reason)
            trace.record(
                StepId.T1_AUTHORIZE,
                "allow",
                fw_ms,
                detail={"principal": principal_label, "decision": "allow"},
                tool_used="cedar",
            )
            trace.record(
                StepId.T1_FIREWALL,
                "success",
                fw_ms,
                detail={"decision": "authorized"},
                tool_used="sql-firewall",
            )

            exec_start = time.perf_counter()
            remaining_s = deadline.remaining_s() if deadline is not None else None
            if remaining_s is not None and remaining_s < 1:
                trace.record(
                    StepId.T1_EXECUTE,
                    "skipped",
                    0,
                    detail={
                        "reason": "request deadline exhausted",
                        "remainingSeconds": round(remaining_s, 3),
                    },
                    tool_used="sql-engine",
                )
                logger.info(
                    "tier1_execute_skipped_deadline",
                    namespace=namespace,
                    remaining_seconds=round(remaining_s, 3),
                )
                return None, None
            timeout_seconds = self._tier1_metric_timeout_s
            if remaining_s is not None:
                timeout_seconds = min(
                    timeout_seconds,
                    # All concrete executors require a positive integer. The
                    # <1s branch above skips execution, and max(1, ...) keeps
                    # that invariant explicit if the boundary logic changes.
                    max(1, int(remaining_s)),
                )
            try:
                result = await self._query_executor.execute(
                    fw_result.authorized_sql,
                    data_source_id=metric_match.data_source_id,
                    namespace=namespace,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as e:
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                trace.record(
                    StepId.T1_EXECUTE,
                    "error",
                    exec_ms,
                    detail=client_error_detail(e, timeoutSeconds=timeout_seconds),
                    tool_used="sql-engine",
                )
                logger.warning(
                    "tier1_execute_failed",
                    namespace=namespace,
                    metric=metric_match.metric_name,
                    error=type(e).__name__,
                    detail=str(e)[:200],
                    timeout_seconds=timeout_seconds,
                )
                # A cross-namespace SQL reference is a policy denial, not a data-source
                # outage: surface it as 403 with a fixed policy reason (never the
                # executor's message, which is not ours to vouch for).
                # Detected via the wrapped firewall cause so the executor keeps raising
                # its own {Athena,Redshift}QueryError (unit-test contract) and generic
                # executor failures stay masked as DataSourceUnavailableError.
                if isinstance(e.__cause__, NamespaceSQLScopeError):
                    raise NamespaceScopeDeniedError(
                        "Access denied: SQL reference is outside the requested namespace"
                    ) from e
                raise DataSourceUnavailableError(
                    f"Metric '{metric_match.metric_name}' execution failed: {type(e).__name__}"
                ) from e

            exec_ms = int((time.perf_counter() - exec_start) * 1000)
            # Enrich with row_count/truncated and the executing component.
            trace.record(
                StepId.T1_EXECUTE,
                "success",
                exec_ms,
                detail={
                    "rowCount": result.row_count,
                    "table": metric_match.data_source_id or "unknown",
                    "truncated": result.truncated,
                    "timeoutSeconds": timeout_seconds,
                    # Executing engine — athena | redshift | jdbc.
                    "engine": result.engine or "unknown",
                },
                tool_used="sql-engine",
            )

            return InvokeResponse(
                result=self._assembler.assemble_tier1(
                    rows=result.rows,
                    columns=metric_match.columns,
                    metric_name=metric_match.metric_name,
                    sql_used=metric_sql,
                    trace=trace,
                    namespace=namespace,
                    principal=display_principal(profile),
                    # fuzzy near-miss reports its similarity as confidence (<1.0);
                    # exact name/synonym stays deterministic 1.0.
                    match_confidence=metric_match.match_confidence,
                    match_source=metric_match.match_source,
                    model_id=model_id,
                    include_sql=self._resolve_include_sql(options, namespace, profile),
                )
            ), None

        trace.record(StepId.T1_METRIC_MATCH, "miss", t1_ms, detail={"query_length": len(query)})
        return None, None

    def _enforce_allowed_metrics(
        self,
        metric_match: MetricMatch,
        profile: dict,
        namespace: str,
        trace: TraceCollector,
        t1_ms: int,
    ) -> None:
        """Enforce the principal's ``allowedMetrics`` grant, raising on a deny.

        Runs for BOTH Tier-1 exits — an executed match and a residual-declined one
        — so a metric the principal may not use is denied uniformly. If it only
        guarded execution, the declined path would forward a forbidden metric's
        formula/description into the Tier-2 prompt (an authorization bypass a caller
        could trigger simply by appending a qualifier).

        Fail closed: ``is not None`` (not truthiness) — an ABSENT ``allowedMetrics``
        means "no metric constraint" (sparse opt-in), but an explicitly EMPTY one
        means "no metrics permitted". Collapsing the two would make a deny-all grant
        fail OPEN.
        """
        allowed_metrics = profile.get("allowedMetrics")
        if allowed_metrics is not None and (
            metric_match.metric_id not in allowed_metrics and metric_match.metric_name not in allowed_metrics
        ):
            principal_id = profile.get("userId", "unknown")
            # Trace details are rendered verbatim in the rationale panel ("Access
            # denied for principal X"), so they carry the display label; the log
            # below keeps the sub, which is the stable correlation key across requests.
            principal_label = display_principal(profile) or "unknown"
            reason = f"Metric '{metric_match.metric_name}' not in principal's allowedMetrics"
            trace.record(
                StepId.T1_AUTHORIZE,
                "denied",
                t1_ms,
                detail={"principal": principal_label, "decision": "deny", "reason": reason},
                tool_used="grant-profile",
            )
            logger.warning(
                "tier1_allowed_metrics_denied",
                namespace=namespace,
                metric_name=metric_match.metric_name,
                metric_id=metric_match.metric_id,
                principal=principal_id,
            )
            raise AccessDeniedError(reason)

    def _is_deep_reasoning_engaged(self, options: dict) -> bool:
        """Whether the deep-reasoning execution path should handle this request.

        Mode is the per-request execution policy, orthogonal to the tier cascade.
        The request field is ``options.mode``. Precedence: request wins over the
        deployment default (``config.tier3_strategy``).

        * ``"deep-reasoning"`` → engage the loop (unless no retriever was built).
        * ``"standard"`` → explicit OPT-OUT, even under a deep-reasoning default.
        * absent        → follow the deployment default, which ships as standard
          (``TIER3_STRATEGY=lexical-baseline``); deep reasoning is opt-in.

        ``False`` when no deep-reasoning retriever is configured, regardless of mode.
        """
        request_mode = options.get("mode")
        deployment_default = Mode.DEEP_REASONING if self._tier3_deep_reasoning_default else Mode.STANDARD
        resolved, _ = resolve_mode(
            request_mode,
            deployment_default=deployment_default,
            deep_reasoning_available=self._agentic_retriever is not None,
        )
        return resolved is Mode.DEEP_REASONING

    async def _run_tier3(
        self,
        query: str,
        namespace: str,
        trace: TraceCollector,
        *,
        profile: dict | None = None,
        tier2_sparql: str | None = None,
        metric_definitions: list[dict] | None = None,
        embedding: list[float] | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        options: dict | None = None,
        model_id: str | None = None,
        seed_embedding: list[float] | None = None,
        has_structured_source: bool = True,
        has_unstructured_source: bool = True,
    ) -> InvokeResponse:
        # options.includeSupporting=False omits supportingContent from the response
        # (default True). Tier 3 still retrieves and synthesizes over the chunks;
        # only the response payload drops them. Checked `is not False` so any other
        # value (absent, None, True) keeps the chunks — the Smithy/REST default.
        include_supporting = (options or {}).get("includeSupporting") is not False

        # A caller whose grant declares allowedMetrics sees only the metrics it permits in the catalog, and no
        # governed metric through graph traversal.
        allowed_metrics = (profile or {}).get("allowedMetrics")
        hidden: dict[str, bool] = {}
        if allowed_metrics is not None:
            hidden = {"hide_governed": True}
            metric_definitions = [
                m
                for m in metric_definitions or []
                if m.get("metric_id") in allowed_metrics or m.get("name") in allowed_metrics
            ] or None

        # Deep-reasoning Tier-3 path (opt-in). Engaged per-request via
        # options.mode=="deep-reasoning" or as the deployment default. Returns the
        # same Tier3Result contract, so the assembler tail below is shared. When
        # not engaged, fall through to the existing hand-rolled / lexical path.
        if self._is_deep_reasoning_engaged(options or {}):
            tier3_result = await self._agentic_retriever.resolve(
                query,
                namespace,
                embedding=embedding,
                tier2_sparql_hint=tier2_sparql,
                catalog_summary=metric_definitions,
                on_token=on_token,
                conversation_history=conversation_history,
                trace=trace,
                has_structured_source=has_structured_source,
                has_unstructured_source=has_unstructured_source,
                # Per-request tool ablation (benchmark/diagnostic lever): withhold
                # named tools from the planner on top of composition gating.
                exclude_tools=frozenset((options or {}).get("excludeTools") or ()),
                # Without the profile, SQLFirewall/Cedar fail closed (no resolved
                # roles). options carries the row cap; model_id the per-request model.
                profile=profile,
                # Unset Deep Reasoning options take the measured recommended values.
                options=with_recommended_options(options),
                model_id=model_id,
                # The answer may name tables and columns only for a caller who may
                # see the generated SQL itself (includeDebugInfo + viewSource, the
                # query_used gate below).
                schema_visible=self._resolve_include_sql(options, namespace, profile),
            )
            result = self._assembler.assemble_tier3(
                synthesized_answer=tier3_result.synthesized_answer,
                supporting_content=tier3_result.supporting_content if include_supporting else None,
                graph_context=tier3_result.graph_context,
                confidence=tier3_result.confidence,
                guardrail_blocked=tier3_result.guardrail_blocked,
                trace=trace,
                namespace=namespace,
                principal=display_principal(profile),
                degraded_sources=tier3_result.degraded_sources or None,
                model_id=model_id,
                partial=tier3_result.partial,
                # Label by mode, not tier: Deep Reasoning resolves at tier=3 but is
                # a distinct execution mode.
                execution_mode=Mode.DEEP_REASONING.value,
                # Return the SQL/SPARQL the agentic loop ran, same as Standard mode
                # does — gated by includeDebugInfo opt-in AND viewSource (SQL) /
                # viewOntology (SPARQL); see _resolve_include_sql/_resolve_include_sparql.
                query_used=tier3_result.query_used,
                sparql_generated=tier3_result.sparql_generated,
                include_sql=self._resolve_include_sql(options, namespace, profile),
                include_sparql=self._resolve_include_sparql(options, namespace, profile),
            )
            # Keep the free-form metadata["mode"] stamp for request logs / eval
            # attribution; execution_mode above is the field the console reads.
            result.metadata = {**(result.metadata or {}), "mode": Mode.DEEP_REASONING.value}
            return InvokeResponse(result=result)

        # A per-request options.retrieverStrategy overrides the deployment default
        # (None under hand-rolled). resolve_strategy lives in a graphrag-free module,
        # so importing it keeps the hand-rolled path sync-only.
        retriever_strategy = None
        resolved_metadata: dict[str, str] | None = None
        if self._knowledge_retriever.lexical_enabled:
            from .lexical.strategies import resolve_strategy

            request_value = (options or {}).get("retrieverStrategy")
            retriever_strategy = resolve_strategy(request_value, self._lexical_retriever_strategy)
            if retriever_strategy is not None:
                resolved_metadata = {"retrieverStrategy": str(retriever_strategy)}

        # The seed search needs the query embedding even when `embedding` is None
        # (structured-only namespace), so it uses seed_embedding.
        entity_uris = await self._resolve_seed_uris(seed_embedding, namespace)

        tier3_result = await self._knowledge_retriever.resolve(
            query,
            namespace,
            embedding=embedding,
            entity_uris=entity_uris or None,
            tier2_sparql_hint=tier2_sparql,
            structured_data=None,
            catalog_summary=metric_definitions,
            on_token=on_token,
            conversation_history=conversation_history,
            trace=trace,
            retriever_strategy=retriever_strategy,
            model_id=model_id,
            **hidden,
        )
        result = self._assembler.assemble_tier3(
            synthesized_answer=tier3_result.synthesized_answer,
            supporting_content=tier3_result.supporting_content if include_supporting else None,
            graph_context=tier3_result.graph_context,
            confidence=tier3_result.confidence,
            guardrail_blocked=tier3_result.guardrail_blocked,
            trace=trace,
            namespace=namespace,
            principal=display_principal(profile),
            degraded_sources=tier3_result.degraded_sources or None,
            model_id=model_id,
        )
        # The Tier-2 SPARQL handed to synthesis as a hint can surface in the
        # answer; record it so a caller without viewOntology gets its IRIs redacted.
        result.with_raw_artifacts(query_used=None, sparql=tier2_sparql)

        # Surface the resolved strategy in response metadata so request logs and
        # eval runs attribute results to the strategy that produced them.
        if resolved_metadata is not None:
            result.metadata = {**(result.metadata or {}), **resolved_metadata}

        return InvokeResponse(result=result)
