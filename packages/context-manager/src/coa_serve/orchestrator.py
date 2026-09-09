# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core orchestrator — sequential Tier 1 → 2 (strategy-based) → 3 resolution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from coa_common import ontology_vector_index_name

from .clients.base import LLMClient, QueryExecutor, VectorClient
from .exceptions import AccessDeniedError, DataSourceUnavailableError, NamespaceScopeDeniedError, NoResultError
from .identity import display_principal
from .mode import Mode, resolve_mode
from .tier2.sql_firewall import NamespaceSQLScopeError

if TYPE_CHECKING:
    from .clients.sources_registry import SourceComposition, SourcesRegistry
    from .tier2.skip import Tier2SkipEvaluator
    from .tier3.agentic.retriever import AgenticRetriever
from .model_validation import validate_model_id as _validate_model_id
from .models import InvokeRequest, InvokeResponse
from .response_assembler import ResponseAssembler
from .step_ids import StepId
from .tier1.metric_resolver import MetricMatch, MetricResolver
from .tier2.skip import Tier2SkipDecision
from .tier2.sql_firewall import FirewallResult, SQLFirewall
from .tier2.strategy import (
    DEFAULT_STRATEGY,
    EMPTY_RESULT_CONFIDENCE_FLOOR,
    StrategyContext,
    StrategyOption,
    StrategyResult,
    StructuredQueryTier,
    capped_max_rows,
    normalize_strategy_option,
)
from .tier3.knowledge_retriever import VECTOR_SEARCH_TIMEOUT_S, KnowledgeRetriever
from .trace import TraceCollector

logger = structlog.get_logger(__name__)

_VALID_TIER_OVERRIDES = {1, 2, 3}


@dataclass(frozen=True)
class TierGating:
    """Which tiers source-composition gating has ruled out for a request.

    Named fields (not a positional bool tuple) so callers read the decision by
    intent — ``gating.skip_tier1`` — and cannot silently transpose two skips.
    The default (all False) is the fail-open "skip nothing" state used on the
    override / unknown / no-registry paths.

    Note the granularity difference: ``skip_tier1`` / ``skip_tier2`` gate a whole
    tier, whereas ``skip_tier3_vector_search`` suppresses only Tier 3's vector
    sub-step (graph traversal + synthesis still run) — hence the longer name.
    """

    skip_tier1: bool = False
    skip_tier2: bool = False
    skip_tier3_vector_search: bool = False

    @classmethod
    def from_composition(cls, composition: SourceComposition) -> TierGating:
        """Map a namespace's source composition to tier skips.

        Tier 1 (metric resolver) and Tier 2 (VKG/Ontop NL→SQL) can only produce a
        result over a DATABASE source, so both are gated on the same
        no-structured-source signal — expressed once here so the two can never
        drift apart. Tier 3's vector search needs a DOCUMENTS-derived chunk index.
        """
        no_structured = not composition.has_structured_source
        return cls(
            skip_tier1=no_structured,
            skip_tier2=no_structured,
            skip_tier3_vector_search=not composition.has_unstructured_source,
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
        # Optional: gate inapplicable tiers by the namespace's source composition
        # (structured-only vs document-only). When None, no gating happens and
        # every tier runs as before — the feature is purely additive and fail-open.
        self._sources_registry = sources_registry
        # Optional: prune Tier 2 for a document-flavored query in a MIXED namespace
        # (which namespace-level source-composition gating cannot rule out). When
        # None the feature is absent — no extra work, no behavior change. Fail-open:
        # only a SKIP decision prunes Tier 2; RUN/ABSTAIN run it as normal.
        self._tier2_skip_evaluator = tier2_skip_evaluator
        # Tier-3 URI-hop seeding: a multilingual ontology-concept vector search
        # (mirrors the Tier-2 pattern in tier2/ontop/strategy.py) resolves seed
        # class/property URIs from the query embedding so graph traversal takes
        # the language-agnostic traverse_from_uris() branch instead of the
        # ASCII-only keyword branch. Both are optional/fail-open: without them
        # _resolve_seed_uris() returns [] and Tier 3 behaves exactly as before.
        self._vector_client = vector_client
        self._oss_ontology_index = oss_ontology_index
        # Optional deep-reasoning Tier-3 path. When present it can be engaged
        # per-request (options.mode=="deep-reasoning") or as the deployment default
        # (tier3_deep_reasoning_default, set when
        # config.tier3_strategy=="deep-reasoning"). When None deep reasoning is simply
        # unavailable and Tier-3 always runs the
        # existing hand-rolled / lexical KnowledgeRetriever — purely additive.
        self._agentic_retriever = agentic_retriever
        self._tier3_deep_reasoning_default = tier3_deep_reasoning_default

    async def resolve(
        self,
        request: InvokeRequest,
        *,
        trace: TraceCollector | None = None,
        on_token: Callable[[str], Awaitable[None]] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
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
        # If the namespace has ONLY structured (DATABASE) or ONLY unstructured
        # (DOCUMENTS) sources, skip the tier(s) that cannot possibly contribute:
        # • no DATABASE → skip Tier 1 (metric resolver) AND Tier 2 (Ontop/NL→SQL).
        # Both can only produce a result over a relational DATABASE source; on a
        # document-only namespace they would otherwise pay a metric-resolver call
        # and a full NL→SPARQL LLM attempt before no-op'ing straight to Tier 3.
        # • no DOCUMENTS → skip Tier 3's vector search (no chunk_ index exists);
        # graph-traversal + synthesis over the ontology graph still run.
        # Only applied on the automatic path (no explicit tierOverride) and only on
        # a positive zero-count signal; unknown/errors fail OPEN (run everything).
        # The composition lookup shares the cached SRC# query with Tier-2 routing,
        # so when both run (mixed namespace) only one DDB round-trip occurs per TTL.
        gating = await self._resolve_source_gating(namespace, tier_override, trace)

        tier2_sparql: str | None = None
        metric_definitions: list[dict] | None = None

        # ── Deep-reasoning mode owns the whole request ───────────────
        # Mode is an execution policy, not a tier: an explicit
        # options.mode="deep-reasoning"
        # hands the request to the reasoning loop directly instead of running the
        # T1 -> T2 cascade first. Without this, a question the cascade can answer
        # returns at Tier 1/2 and the loop never runs, so the mode selector appears
        # to do nothing on any namespace with a structured source.
        #
        # The loop is not losing the structured tiers — it has nl_to_sql and
        # nl_to_sparql as tools (composition-gated), so it decides per sub-question
        # whether to query the database, and can combine that with document
        # retrieval in one answer, which the cascade cannot.
        #
        # An explicit tierOverride still wins: it is a direct instruction about which
        # tier to run, so it is honored over the mode's routing.
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

        # ── Tier 1: Metric Resolver ──────────────────────────────────
        if self._should_run_tier1(tier_override, start_tier, gating):
            result = await self._run_tier1(query, namespace, profile, options, trace, model_id=model_id_override)
            if result:
                return result
            metric_definitions = self._metric_resolver.list_all(namespace) or None

        # Query embedding — shared by Tier 2 (class retrieval) and Tier 3 (vector search).
        query_embedding = await self._embed_query(query, tier_override, trace)

        # ── Tier 2: strategy-based structured query ────────────────
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
            )
            if strategy_result:
                return strategy_result

        # ── Tier 3: Knowledge Retriever (fallback) ───────────────────
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

        # / Req 2: a pinned tier that exhausts its permitted fall-through
        # and yields nothing is a "no data found" outcome, NOT a server fault. Raise
        # a typed NoResultError (→ 404 at the handler) instead of a bare ValueError
        # that the WS handler would map to InternalError 500. Reached for a pinned
        # tierOverride=1 (Tier-1 miss) or tierOverride=2 (Tier-2 strategy miss);
        # unpinned and tierOverride=3 always resolve via Tier 3 above.
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
        without any namespace authorization (F-2, CWE-862). The Tier-1/2 SQL path
        keeps its own firewall.evaluate() gate; this runs upstream of dispatch.
        """
        return self._firewall.authorize_namespace(namespace, profile)

    # ── Tier run/skip decisions ──────────────────────────────────────
    # Each tier's guard is a named predicate so resolve() reads as a plain
    # sequence. "should run" folds in three orthogonal inputs: caller intent
    # (tierOverride / startTier / explicit strategy), namespace source-composition
    # gating, and per-query capability pruning.

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

        Tier-1 matches a metric by name/synonym anywhere in the question and then
        executes that metric's SQL **verbatim** — it parses no filter, grouping, or
        time window out of the natural language. Anything the match did not consume
        is therefore a qualifier that would be silently dropped, so Tier-1 must
        decline the question rather than answer a different one.

        Two cases are NOT unhandled:

        * **Caller-supplied dimensions.** ``options.dimensions`` is the documented
          out-of-band way to filter a metric, so a request that carries them has
          expressed its qualifier through the supported channel; the residual words
          are that same intent restated in prose. Substitution still validates them
          downstream (undeclared/missing values fail closed there).
        * **A Tier-1 pin.** ``tierOverride=1`` is an explicit instruction to answer
          from the metric path, and bypassing would turn it into a 404. The caller
          asked for the deterministic aggregate; the trace still carries the match
          detail. Only the AUTOMATIC path re-routes.

        Returns:
            The unconsumed question text, or ``""`` when Tier-1 fully consumed the
            question (or one of the exemptions above applies).
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
            # detail to the client (security fix 57b9ec08); full error stays in logs.
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

        Runs the same multilingual ontology-concept vector search Tier 2 uses
        (tier2/ontop/strategy.py::_fetch_vector_hits) so a non-Latin (e.g.
        Japanese) query — which the ASCII-only keyword traverser drops entirely
        — still seeds graph traversal from the class/property URIs its embedding
        matches. Fail-open: returns [] when the ontology index or vector client
        is unconfigured, the embedding is missing, or the search errors, in which
        case Tier 3 falls back to the keyword branch exactly as before.

        The vector client returns the base VectorHit (clients/base.py), whose
        entity type and URI live in ``metadata`` under ``entity_type`` /
        ``entity_uri`` (stamped at ingest) — NOT the typed Tier-2 VectorHit.
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

        Tier 1 (metric resolver) and Tier 2 (VKG/Ontop NL→SQL) both require a
        DATABASE source to produce a result, so they are gated together on the
        no-structured-source signal (see :meth:`TierGating.from_composition`).

        Guardrails (conservative + fail-open):
          • No registry configured → skip nothing (feature is additive).
          • An explicit ``tierOverride`` is user intent — never override it here.
          • Unknown composition (registry error/timeout) → skip nothing.
          • Only skip on a *positive zero-count* signal (``has_* == False``); a
            MIXED namespace runs every tier.

        The result is traced (ROUTING_SELECT) only when something is actually
        skipped, so normal requests are not cluttered.
        """
        if self._sources_registry is None or tier_override is not None:
            return TierGating()

        try:
            composition: SourceComposition = await self._sources_registry.get_source_composition(namespace)
        except Exception as e:  # defensive: any unexpected error must fail open
            logger.warning("source_gating_lookup_failed", namespace=namespace, error=type(e).__name__)
            return TierGating()

        if composition.unknown:
            return TierGating()

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
        }
    )

    @classmethod
    def _has_explicit_strategy(cls, options: dict) -> bool:
        """True when the caller pinned a specific Tier-2 strategy."""
        return normalize_strategy_option(options.get("strategy")) in cls._EXPLICIT_STRATEGY_OPTIONS

    @classmethod
    def _resolve_strategy_selection(cls, options: dict, tier_override: int | None) -> StrategyOption | None:
        """Which Tier-2 strategy to run, or ``None`` if Tier 2 should not run.

        ``None`` means "no structured strategy applies" (a Tier-1/3 pin, or
        startTier > 2) — the caller then skips Tier 2. This never encodes a
        capability skip (gating / per-query pruning); those are decided in
        ``_should_run_tier2``.
        """
        # An explicit strategy pin takes highest precedence (caller intent).
        # Normalized so the pre-rename "agentic" spelling still counts as a pin —
        # otherwise it drops through to DEFAULT_STRATEGY and silently runs the cheap
        # fallback chain instead of the agent the caller asked for.
        strategy = normalize_strategy_option(options.get("strategy"))
        if strategy in cls._EXPLICIT_STRATEGY_OPTIONS:
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
    ) -> tuple[InvokeResponse | None, str | None]:
        """Run Tier 2 strategies and return (response, sparql_for_tier3_grounding).

        Called only when a strategy applies, so ``strategy_selection`` is always a
        real StrategyOption (never a skip sentinel).
        """
        context = StrategyContext(
            embedding=embedding,
            profile=profile,
            options=options,
            trace=trace,
            model_id=model_id,
        )

        result = await self._structured_query_tier.resolve(query, namespace, context, option=strategy_selection)

        if result is not None:
            max_rows = capped_max_rows(options)
            if len(result.rows) > max_rows:
                from dataclasses import replace as _replace

                result = _replace(result, rows=result.rows[:max_rows], row_count=max_rows, truncated=True)

            # Confidence gate: a Tier-2 result below the floor is not a usable
            # structured answer even when it returned rows — the strategy scraped
            # together a query it wasn't confident in (e.g. a knowledge question
            # forced through NL→SQL/VKG). Unless the caller pinned Tier 2, drop it
            # to None so we fall through to Tier 3. Mirrors is_low_confidence_empty
            # but also covers non-empty low-confidence results.
            if tier_override != 2 and result.confidence < EMPTY_RESULT_CONFIDENCE_FLOOR:
                logger.info(
                    "tier2_low_confidence_fallthrough",
                    tier2_confidence=result.confidence,
                    row_count=result.row_count,
                    strategy=result.strategy_name,
                    namespace=namespace,
                )
                result = None

        if result is None:
            if tier_override == 2:
                # / Req 2: pinned Tier-2 miss is "no data found", not a
                # server fault — typed NoResultError (→ 404), not a bare ValueError
                # that the WS handler maps to InternalError 500.
                #
                # Note: a Tier-2 result that executed but returned zero rows with
                # LOW confidence (is_low_confidence_empty) also yields result=None
                # here, so a pinned tierOverride=2 low-confidence empty is reported
                # as 404 NoResult (there is no Tier-3 fallthrough when pinned). This
                # is intentional — a low-confidence empty is treated as "no usable
                # answer", consistent with the unpinned fall-through behavior.
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

        response = self._strategy_result_to_response(result, trace, namespace, profile, model_id=model_id)
        return response, result.sparql or None

    def _strategy_result_to_response(
        self,
        result: StrategyResult,
        trace: TraceCollector,
        namespace: str,
        profile: dict,
        model_id: str | None = None,
    ) -> InvokeResponse:
        """Convert a StrategyResult into the appropriate InvokeResponse."""
        if result.ontop_assembly:
            response = InvokeResponse(
                result=self._assembler.assemble_tier2(
                    rows=result.rows,
                    columns=result.columns,
                    sql_used=result.sql,
                    sparql=result.sparql,
                    confidence=result.confidence,
                    ontology_version=None,
                    data_sources=None,
                    trace=trace,
                    namespace=namespace,
                    principal=display_principal(profile),
                    row_count=result.row_count,
                    truncated=result.truncated,
                    strategy=result.strategy_name,
                    model_id=model_id,
                )
            )
        else:
            response = InvokeResponse(
                result=self._assembler.assemble_nl_to_sql(
                    rows=result.rows,
                    columns=result.columns,
                    sql_used=result.sql,
                    confidence=result.confidence,
                    retrieved_tables=result.retrieved_tables,
                    expanded_tables=result.expanded_tables,
                    trace=trace,
                    namespace=namespace,
                    principal=display_principal(profile),
                    strategy=result.strategy_name,
                    model_id=model_id,
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
    ) -> InvokeResponse | None:
        t1_start = time.perf_counter()
        metric_match = await self._metric_resolver.match(query, namespace)
        t1_ms = int((time.perf_counter() - t1_start) * 1000)

        # a query referencing MORE THAN ONE governed metric is ambiguous
        # for the single-metric Tier-1 path — bypass Tier-1 and let Tier-2/3
        # handle it (DoD: "match_count > 1 → bypasses Tier 1, routes to Tier 2/3").
        # Checked before .found so we never execute just the first of several.
        if metric_match.match_count > 1:
            trace.record(
                StepId.T1_METRIC_MATCH,
                "multi_metric_bypass",
                t1_ms,
                detail=f"{metric_match.match_count} metrics",
            )
            return None

        # a match that leaves part of the question unconsumed is a
        # PARTIAL match: Tier-1 executes the metric's SQL verbatim and cannot
        # express a filter, grouping, or time window, so answering would return the
        # unfiltered aggregate as though it were the answer to the narrower question
        # — at confidence 1.0, with nothing marking the drop. Bypass to Tier 2,
        # which can express predicates. Checked before .found's success trace so a
        # declined question never records a Tier-1 hit.
        unhandled = self._unhandled_qualifier(metric_match, options) if metric_match.found else ""
        if unhandled:
            trace.record(
                StepId.T1_METRIC_MATCH,
                "residual_qualifier_bypass",
                t1_ms,
                detail={
                    "metricName": metric_match.metric_name,
                    "matchSource": metric_match.match_source,
                    # The span of the question Tier-1 could not honour — the evidence
                    # that was previously missing: a caller can now see exactly what
                    # was unhandled instead of inferring it from identical results.
                    "unhandledQualifier": unhandled,
                    # …paired with the span that DID match, so a surprising bypass is
                    # diagnosable from the trace alone (e.g. an over-broad synonym
                    # consuming less of the question than its author expected).
                    "matchedText": metric_match.matched_text,
                },
            )
            logger.info(
                "tier1_residual_qualifier_bypass",
                namespace=namespace,
                metric=metric_match.metric_name,
                match_source=metric_match.match_source,
                matched_text=metric_match.matched_text,
                unhandled=unhandled,
            )
            return None

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

            # enforce allowedMetrics grant-profile restriction before
            # execution. Fail closed: if a profile DECLARES allowedMetrics, the
            # matched metric must be in that list (by metric_id or metric_name).
            #
            # ``is not None``, not truthiness: an ABSENT allowedMetrics means "no
            # metric constraint" (sparse opt-in), but an explicitly EMPTY one means
            # "no metrics permitted". Collapsing the two would make a deny-all
            # grant fail OPEN — the same empty-vs-absent bug fixed for
            # tableAllowlist in sql_firewall.py.
            allowed_metrics = profile.get("allowedMetrics")
            if allowed_metrics is not None and (
                metric_match.metric_id not in allowed_metrics and metric_match.metric_name not in allowed_metrics
            ):
                principal_id = profile.get("userId", "unknown")
                # Trace details are rendered verbatim in the rationale panel
                # ("Access denied for principal X"), so they carry the display
                # label; the log below keeps the sub, which is the stable
                # correlation key across requests.
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

            if not self._query_executor or not metric_match.sql_template:
                trace.record(StepId.T1_EXECUTE, "skipped", 0, detail="no executor or sql_template")
                return None

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
                return None
            if dimensions or "{" in metric_sql or ":" in metric_sql:
                try:
                    metric_sql = self._metric_resolver.substitute_dimensions(
                        metric_match.sql_template, dimensions, metric_match.dimensions
                    )
                except ValueError as e:
                    trace.record(StepId.T1_METRIC_MATCH, "dimension_substitution_failed", t1_ms, detail=str(e)[:120])
                    logger.info("tier1_dimension_substitution_failed", error=str(e))
                    return None

            # Log the pre-substitution template (placeholders, not values) to
            # avoid leaking caller-supplied dimension values (potential PII) into
            # logs. Truncated, consistent with other SQL log sites.
            logger.debug("t1_sql_template", sql=metric_match.sql_template)
            # Tier-1 data-level authorization gate. Tier-1 previously called
            # the executor directly without firewall.evaluate(), bypassing the grant
            # profile (executors only call .validate(), never .evaluate()). Evaluate
            # AFTER the no-executor/no-sql_template guard above so the clean "skipped"
            # path is preserved and an empty sql_template never hits a firewall parse
            # failure. Execute the authorized (possibly rewritten) SQL, not the raw
            # template, so any future row-filter rewrite cannot be bypassed.
            #
            # Empty profile = no data-level restriction by design (sql_firewall.py
            # "sparse opt-in"); this terminal deny only fires when a restrictive
            # profile is supplied. The profile is caller-supplied today; wiring the
            # authorizer-RESOLVED profile is the Cedar follow-up (see MR notes).
            fw_start = time.perf_counter()
            try:
                fw_result = self._firewall.evaluate(metric_sql, profile, namespace=namespace)
            except Exception as e:
                fw_ms = int((time.perf_counter() - fw_start) * 1000)
                trace.record(
                    StepId.T1_FIREWALL,
                    "error",
                    fw_ms,
                    detail={"error": type(e).__name__, "message": str(e)[:120]},
                    tool_used="sql-firewall",
                )
                logger.warning(
                    "tier1_firewall_error", namespace=namespace, metric=metric_match.metric_name, error=str(e)[:200]
                )
                return None

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
            try:
                result = await self._query_executor.execute(
                    fw_result.authorized_sql, data_source_id=metric_match.data_source_id, namespace=namespace
                )
            except Exception as e:
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                trace.record(
                    StepId.T1_EXECUTE,
                    "error",
                    exec_ms,
                    detail={"error": type(e).__name__, "message": str(e)[:120]},
                    tool_used="sql-engine",
                )
                logger.warning(
                    "tier1_execute_failed",
                    namespace=namespace,
                    metric=metric_match.metric_name,
                    error=type(e).__name__,
                    detail=str(e)[:200],
                )
                # A cross-namespace SQL reference is a policy denial, not a data-source
                # outage: surface it as 403 with its (non-sensitive) policy reason.
                # Detected via the wrapped firewall cause so the executor keeps raising
                # its own {Athena,Redshift}QueryError (unit-test contract) and generic
                # executor failures stay masked as DataSourceUnavailableError.
                if isinstance(e.__cause__, NamespaceSQLScopeError):
                    raise NamespaceScopeDeniedError(str(e)) from e
                raise DataSourceUnavailableError(
                    f"Metric '{metric_match.metric_name}' execution failed: {type(e).__name__}"
                ) from e

            exec_ms = int((time.perf_counter() - exec_start) * 1000)
            # enrich with row_count/truncated already in-hand on the
            # executor QueryResult; (A2) record the executing component.
            trace.record(
                StepId.T1_EXECUTE,
                "success",
                exec_ms,
                detail={
                    "rowCount": result.row_count,
                    "table": metric_match.data_source_id or "unknown",
                    "truncated": result.truncated,
                    # (A2) executing engine — athena | redshift | jdbc.
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
                )
            )

        trace.record(StepId.T1_METRIC_MATCH, "miss", t1_ms, detail={"query_length": len(query)})
        return None

    def _collect_metric_context(self, trace: TraceCollector) -> list[dict] | None:
        # Placeholder: intended to harvest metric definitions surfaced during a
        # Tier-1 miss and pass them to Tier 3 as catalog_summary. Not yet
        # implemented — always None today (Tier 3 simply gets no catalog summary).
        # TODO(serve): populate from the Tier-1 trace when catalog grounding lands.
        return None

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
                # Out-of-band request context the structured tools need. Without the
                # profile, SQLFirewall/Cedar fail CLOSED (no resolved roles) and every
                # nl_to_sql / nl_to_sparql call was an AccessDeniedError silently
                # rendered as "0 rows". options carries the row cap; model_id the
                # per-request model override the standard Tier-2 path already honors.
                profile=profile,
                options=options,
                model_id=model_id,
            )
            result = self._assembler.assemble_tier3(
                synthesized_answer=tier3_result.synthesized_answer,
                supporting_content=tier3_result.supporting_content,
                graph_context=tier3_result.graph_context,
                confidence=tier3_result.confidence,
                guardrail_blocked=tier3_result.guardrail_blocked,
                trace=trace,
                namespace=namespace,
                principal=display_principal(profile),
                degraded_sources=tier3_result.degraded_sources or None,
                model_id=model_id,
                partial=tier3_result.partial,
            )
            result.metadata = {**(result.metadata or {}), "mode": Mode.DEEP_REASONING.value}
            return InvokeResponse(result=result)

        # Resolve the graphrag retriever strategy. The lexical retriever is built
        # on every deployment, so a per-request options.retrieverStrategy engages
        # graphrag on-demand. self._lexical_retriever_strategy is the deployment
        # default: a concrete strategy under TIER3_STRATEGY=lexical-baseline, or
        # None under hand-rolled (so absence of a request value resolves to None
        # and Tier 3 runs the hand-rolled path). resolve_strategy lives in a
        # graphrag-free module, so importing it does not break the sync-only
        # invariant on the hand-rolled path.
        retriever_strategy = None
        resolved_metadata: dict[str, str] | None = None
        if self._knowledge_retriever.lexical_enabled:
            from .lexical.strategies import resolve_strategy

            request_value = (options or {}).get("retrieverStrategy")
            retriever_strategy = resolve_strategy(request_value, self._lexical_retriever_strategy)
            if retriever_strategy is not None:
                resolved_metadata = {"retrieverStrategy": str(retriever_strategy)}

        # Seed graph traversal from ontology-concept URIs matched to the query
        # embedding. This must run inside the Tier-3 path (not rely on Tier 2):
        # on tierOverride=3 Tier 2 is skipped, and for a structured-only namespace
        # the `embedding` arg above is None (vector-search self-skip) — but the
        # seed search needs the *query* embedding regardless, so it uses the
        # explicitly-threaded seed_embedding. Empty result → resolve() falls back
        # to the keyword branch (trace strategy=="keyword"), so this is fail-open.
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
        )
        # Trace steps are recorded progressively inside knowledge_retriever.resolve()
        # via the trace collector callback. toolUsed annotation lives
        # at the retriever's record sites (opensearch/neptune/bedrock).

        result = self._assembler.assemble_tier3(
            synthesized_answer=tier3_result.synthesized_answer,
            supporting_content=tier3_result.supporting_content,
            graph_context=tier3_result.graph_context,
            confidence=tier3_result.confidence,
            guardrail_blocked=tier3_result.guardrail_blocked,
            trace=trace,
            namespace=namespace,
            principal=display_principal(profile),
            degraded_sources=tier3_result.degraded_sources or None,
            model_id=model_id,
        )

        # Surface the resolved strategy in response metadata so request logs and
        # eval runs attribute results to the strategy that produced them (2.18).
        if resolved_metadata is not None:
            result.metadata = {**(result.metadata or {}), **resolved_metadata}

        return InvokeResponse(result=result)
