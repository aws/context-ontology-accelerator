# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native SPARQL Tier-2 strategy — NL→SPARQL executed directly against a graph source.

The front end is the Ontop one (class retrieval → T-Box → NL→SPARQL → validator),
gated to ``RDF_SPARQL`` classes. Execution diverges: no VKG, no SQL, no SQL Firewall
— the generated query passes :class:`SPARQLFirewall` and runs against the data
endpoint of the SPECIFIC source backing the matched class, through a client chosen
by that source's ``configuration.store``.

Two endpoints are involved and never confused: the SHARED ontology endpoint (T-Box
and URI validation, via the translator) and the PER-SOURCE data endpoint (the A-Box,
only for execution).

Every way this strategy declines is a ``None`` with a distinct log event and trace
reason (LLD §5), so the runner can fall through while an operator can still tell a
graph namespace whose ontology was never imported (``sparql_strategy_misconfigured``)
from an unverified source, a rejected query or an endpoint failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import Any, Protocol

import structlog
from coa_common import ontology_vector_index_name
from coa_common.constants import GRAPH_BACKED_SUB_TYPES, StructuredBackend
from coa_common.neptune_endpoints import (
    GRAPH_CLUSTER_CLAIM_ATTR,
    PLATFORM_ENDPOINT_ENV,
    graph_cluster_refusal,
    is_graph_cluster_claim_pk,
    platform_refusal,
)
from coa_common.sparql_config import (
    SparqlConfigurationError,
    SparqlSourceConfig,
    SparqlStore,
    parse_sparql_configuration,
)

from ...clients.base import VectorClient
from ...clients.neptune import DEFAULT_MAX_RESULTS, server_timeout_ms
from ...clients.sources_registry import SourcesRegistry
from ...clients.sparql_clients import SparqlClientCache, SparqlEndpointClient, UnsupportedSparqlEngineError
from ...exceptions import AccessDeniedError
from ...step_ids import StepId
from ...trace import client_error_detail
from ..ontop.nl_to_sparql import PipelineStep
from ..ontop.types import VectorHit
from ..sql_firewall import FirewallResult, column_denylist_for, tables_visible_to
from ..strategy import StrategyContext, StrategyOption, StrategyResult, capped_max_rows
from .firewall import QueryForm, SafeQuery, SPARQLFirewall, UnsafeSPARQLError
from .nl_to_sparql import NativeNLtoSPARQL

logger = structlog.get_logger(__name__)

# k-NN depth for the retrieval feeding the T-Box (matches Ontop).
_VECTOR_TOP_K = 10
_DEFAULT_SOURCE = "default"
_ERROR_CHARS = 200

# The translator is the shared Ontop pipeline, so it records ``t2.vkg.*`` steps;
# this strategy owns its trace family and files them under ``t2.sparql.*``.
_TRANSLATOR_STEPS: dict[str, str] = {
    PipelineStep.CONTEXT_ASSEMBLY: StepId.T2_SPARQL_CONTEXT,
    PipelineStep.LLM_CALL: StepId.T2_SPARQL_TRANSLATE,
    PipelineStep.VALIDATION: StepId.T2_SPARQL_VALIDATE,
}


class NamespaceAuthorizer(Protocol):
    """Cedar's namespace admission gate (``SQLFirewall.authorize_namespace``)."""

    def authorize_namespace(self, namespace: str, profile: dict[str, Any] | None = None) -> FirewallResult:
        """Whether the principal may query ``namespace`` at all."""
        ...


class SparqlStrategy:
    """``StructuredQueryStrategy`` for ``RDF_SPARQL`` sources (name ``nl_to_sparql``).

    Vendor-agnostic: the engine lives in the client the cache builds from the
    source's configuration, so a second SPARQL engine needs no change here.
    """

    name: str = StrategyOption.NL_TO_SPARQL
    supported_backends: frozenset[StructuredBackend] = frozenset({StructuredBackend.RDF_SPARQL})

    def __init__(
        self,
        translator: NativeNLtoSPARQL,
        sources_registry: SourcesRegistry,
        vector_client: VectorClient | None = None,
        oss_ontology_index: str = "",
        client_cache: SparqlClientCache | None = None,
        firewall: SPARQLFirewall | None = None,
        namespace_authorizer: NamespaceAuthorizer | None = None,
    ):
        """Wire the translator, source registry, retrieval index, clients and firewall.

        Args:
            translator: Full-SPARQL-1.1 translator gated to ``RDF_SPARQL`` classes.
            sources_registry: Resolves the source backing the matched class.
            vector_client: Ontology-class retrieval (optional; without it the T-Box
                falls back to label matching).
            oss_ontology_index: OpenSearch ontology index base name.
            client_cache: Per-source data-endpoint clients; one is created if absent.
            firewall: Safety gate; the default one if absent.
            namespace_authorizer: Cedar's namespace "query" gate (serve passes the SQL
                strategies' SQLFirewall); a deny is the SQL path's terminal 403.
        """
        self._translator = translator
        self._sources = sources_registry
        self._vector_client = vector_client
        self._oss_ontology_index = oss_ontology_index
        # ``is None``, not ``or``: the cache defines ``__len__``, so an empty one is falsy.
        self._clients = client_cache if client_cache is not None else SparqlClientCache()
        self._firewall = firewall if firewall is not None else SPARQLFirewall()
        self._namespace_authorizer = namespace_authorizer

    @property
    def client_cache(self) -> SparqlClientCache:
        """The data-endpoint client cache, for closing at shutdown."""
        return self._clients

    async def resolve(self, query: str, namespace: str, context: StrategyContext) -> StrategyResult | None:
        """Translate, gate and execute; ``None`` on any miss so the runner falls through.

        Raises:
            AccessDeniedError: Cedar denies the principal "query" on the namespace.
        """
        self._authorize(namespace, context)
        hits = await self._fetch_vector_hits(context.embedding, namespace) if context.embedding else None

        # The T-Box never offers a predicate the caller's grant denies on any source.
        excluded = _denied_iris(context.profile)
        extra = {"exclude_properties": excluded} if excluded else {}
        translation = await self._translator.translate(
            query, namespace, vector_hits=hits, model_id=context.model_id, **extra
        )
        if translation.trace_steps:
            context.trace.add_dicts(_as_sparql_steps(translation.trace_steps))

        tbox = translation.tbox_context
        if tbox is not None and not tbox.classes:
            # The strategy only runs for a namespace with a graph source, so an empty
            # RDF_SPARQL T-Box means no imported ontology binds a class to one.
            logger.error(
                "sparql_strategy_misconfigured",
                namespace=namespace,
                detail=(
                    "the namespace has a graph source but no RDF_SPARQL classes; import an ontology whose "
                    "classes name the source with coa_import:dataSourceName"
                ),
            )
            self._fail(context, "sparql_strategy_misconfigured")
            return None
        if not (translation.valid and translation.sparql):
            return None

        resolved = await self._resolve_source(namespace, self._pick_source_id(context.options, hits))
        if not isinstance(resolved, tuple):
            self._fail(context, resolved or "sparql_strategy_no_data_endpoint")
            return None
        data_source_id, config, source_name = resolved
        if not tables_visible_to([data_source_id], context.profile):
            # Mirrors the SQL firewall: a declared allowlist (even an empty one) must name the source.
            logger.warning("sparql_grant_restricted", namespace=namespace, data_source_id=data_source_id)
            self._fail(context, "sparql_grant_restricted")
            return None
        denied = column_denylist_for(data_source_id, context.profile)
        if (
            denied is None
            or not all(_is_absolute_iri(name) for name in denied)
            or _misaddressed_denylist(context.profile, data_source_id, source_name)
        ):
            # A denylist this source cannot enforce refuses it: a denied name that is not a full
            # predicate IRI, or an entry meant for it but keyed by its name or "*" (keys are source ids).
            logger.warning("sparql_grant_restricted", namespace=namespace, data_source_id=data_source_id)
            self._fail(context, "sparql_grant_restricted")
            return None

        safe = self._gate(translation.sparql, config, context, namespace, denied)
        if safe is None:
            return None

        try:
            lease = self._clients.lease(namespace, data_source_id, config)
        except UnsupportedSparqlEngineError as e:
            logger.error("sparql_client_build_failed", namespace=namespace, data_source_id=data_source_id, error=str(e))
            self._fail(context, "sparql_client_build_failed")
            return None

        with lease as client:
            return await self._execute(client, safe, translation.confidence, data_source_id, namespace, context)

    def _authorize(self, namespace: str, context: StrategyContext) -> None:
        if self._namespace_authorizer is None:
            return
        decision = self._namespace_authorizer.authorize_namespace(namespace, context.profile)
        if decision.denied:
            self._fail(context, "sparql_access_denied")
            raise AccessDeniedError(decision.reason or "Access denied by policy")

    # ── routing ──────────────────────────────────────────────────────────

    @staticmethod
    def _pick_source_id(options: dict[str, Any], hits: list[VectorHit] | None) -> str:
        """Which source to execute against: pin > top graph class hit > ``"default"``.

        The class hits were retrieved with the RDF_SPARQL backend filter, so each
        carries the id of the graph source that backs it; the top-scoring one is the
        class the question is most about. ``"default"`` means "the namespace's only
        graph source" and is resolved (or refused) by :meth:`_resolve_source`.
        """
        pinned = options.get("dataSourceId")
        if pinned:
            return str(pinned)
        for hit in hits or []:
            if (
                hit.type == "ontology_class"
                and hit.metadata.get("query_backend") == StructuredBackend.RDF_SPARQL.value
                and hit.metadata.get("data_source_id")
            ):
                return str(hit.metadata["data_source_id"])
        for hit in hits or []:
            if hit.type == "ontology_class" and hit.metadata.get("data_source_id"):
                return str(hit.metadata["data_source_id"])
        return _DEFAULT_SOURCE

    async def _resolve_source(
        self, namespace: str, data_source_id: str
    ) -> tuple[str, SparqlSourceConfig, str] | str | None:
        """Load the source record: its id, configuration and name; ``None`` if it cannot serve.

        A registry that cannot be read answers its trace reason, ``sparql_registry_unavailable``, instead.

        Refuses anything but a verified (``queryable``) graph source: an unverified
        endpoint would only time out, and a pinned id naming a SQL source must not
        reach a SPARQL client.
        """
        try:
            if data_source_id == _DEFAULT_SOURCE:
                item = await self._sources.find_sole_graph_source(namespace)
            else:
                item = await self._sources.get_source(namespace, data_source_id)
        except Exception as e:  # registry boundary: no endpoint to query, traced as the outage it is
            logger.warning(
                "sparql_endpoint_resolve_failed",
                namespace=namespace,
                data_source_id=data_source_id,
                error=type(e).__name__,
                message=str(e)[:_ERROR_CHARS],
            )
            return "sparql_registry_unavailable"

        reason = None
        if not item:
            reason = (
                "no graph source with this id"
                if data_source_id != _DEFAULT_SOURCE
                else "no class names its source and the namespace does not have exactly one graph source"
            )
        elif item.get("sourceSubType") not in GRAPH_BACKED_SUB_TYPES:
            reason = "source is not an RDF_SPARQL source"
        elif item.get("status") in _LEAVING_STATUSES:
            reason = "source is being deleted (status " + str(item.get("status")) + ")"
        elif item.get("queryable") is not True:
            reason = "source endpoint has not been verified (status " + str(item.get("status", "?")) + ")"
        if reason is not None:
            logger.error(
                "sparql_strategy_no_data_endpoint", namespace=namespace, data_source_id=data_source_id, detail=reason
            )
            return None

        try:
            config = parse_sparql_configuration(item.get("configuration"))
        except SparqlConfigurationError as e:
            logger.error(
                "sparql_strategy_no_data_endpoint",
                namespace=namespace,
                data_source_id=data_source_id,
                detail=f"invalid source configuration: {e}",
            )
            return None
        refusal = await self._cluster_refusal(item, config)
        if refusal:
            logger.error(
                "sparql_strategy_no_data_endpoint", namespace=namespace, data_source_id=data_source_id, detail=refusal
            )
            return None
        return str(item.get("sourceId") or data_source_id), config, str(item.get("name") or "")

    async def _cluster_refusal(self, item: dict[str, Any], config: SparqlSourceConfig) -> str | None:
        """Never COA's own catalog cluster, nor one its namespace holds no claim on, whatever the record says."""
        claim_pk = item.get(GRAPH_CLUSTER_CLAIM_ATTR)
        noted = claim_pk if is_graph_cluster_claim_pk(claim_pk) else None
        refusal = platform_refusal(config.host, os.environ.get(PLATFORM_ENDPOINT_ENV, ""), noted)
        if refusal:
            return refusal
        if noted is None:
            return "the source notes no claim on its cluster; verify the source again"
        try:
            claim = await self._sources.get_graph_cluster_claim(noted)
        except Exception as e:  # registry boundary: a claim that cannot be read refuses
            return f"the cluster's claim could not be read ({type(e).__name__})"
        return graph_cluster_refusal(claim, str(item.get("namespaceId", "")))

    # ── safety ───────────────────────────────────────────────────────────

    def _gate(
        self,
        sparql: str,
        config: SparqlSourceConfig,
        context: StrategyContext,
        namespace: str,
        denied: frozenset[str] = frozenset(),
    ) -> SafeQuery | None:
        start = time.perf_counter()
        try:
            safe = self._firewall.check(
                sparql,
                named_graph=config.data_named_graph,
                max_results=capped_max_rows(context.options),
                query_timeout_ms=server_timeout_ms() if config.store == SparqlStore.NEPTUNE else None,
                denied_predicates=denied,
            )
        except UnsafeSPARQLError as e:
            logger.warning("sparql_strategy_firewall_rejected", namespace=namespace, rule=e.rule.value, detail=str(e))
            context.trace.record(
                StepId.T2_SPARQL_FIREWALL,
                "error",
                _ms(start),
                detail={"strategy": self.name, "reason": "sparql_firewall_rejected", "rule": e.rule.value},
            )
            return None
        context.trace.record(
            StepId.T2_SPARQL_FIREWALL,
            "success",
            _ms(start),
            detail={"strategy": self.name, "form": safe.form.name, "limit": safe.limit},
        )
        return safe

    # ── execution ────────────────────────────────────────────────────────

    async def _execute(
        self,
        client: SparqlEndpointClient,
        safe: SafeQuery,
        confidence: float,
        data_source_id: str,
        namespace: str,
        context: StrategyContext,
    ) -> StrategyResult | None:
        start = time.perf_counter()
        timeout = context.deadline.remaining_s() if context.deadline is not None else None
        try:
            rows, columns = await asyncio.wait_for(_run(client, safe), timeout=timeout)
        except Exception as e:  # endpoint boundary: connectivity, auth, SPARQL errors, deadline
            # MC-1: the error text names the endpoint (and can carry more), so it goes to the log only; the
            # caller-visible trace step gets the type and a correlation id that finds this log line (CWE-209).
            error_ref = uuid.uuid4().hex[:12]
            logger.warning(
                "sparql_strategy_exec_failed",
                namespace=namespace,
                data_source_id=data_source_id,
                error=type(e).__name__,
                message=str(e)[:_ERROR_CHARS],
                error_ref=error_ref,
            )
            context.trace.record(
                StepId.T2_SPARQL_EXECUTE,
                "error",
                _ms(start),
                detail=client_error_detail(
                    e, strategy=self.name, reason="sparql_strategy_exec_failed", errorRef=error_ref
                ),
            )
            return None

        duration_ms = _ms(start)
        truncated = safe.limit is not None and len(rows) >= safe.limit
        logger.info(
            "sparql_strategy_resolved",
            namespace=namespace,
            data_source_id=data_source_id,
            form=safe.form.name,
            row_count=len(rows),
            duration_ms=duration_ms,
        )
        context.trace.record(
            StepId.T2_SPARQL_EXECUTE,
            "success",
            duration_ms,
            detail={"strategy": self.name, "dataSourceId": data_source_id, "rowCount": len(rows)},
        )
        return StrategyResult(
            # Bindings are not SQL: the query travels in ``sparql`` and ``sql`` stays
            # empty, which is what the response assembler keys the graph answer on.
            sql="",
            rows=rows,
            columns=columns,
            confidence=confidence,
            strategy_name=self.name,
            trace_steps=[],
            data_source_id=data_source_id,
            row_count=len(rows),
            truncated=truncated,
            sparql=safe.sparql,
        )

    def _fail(self, context: StrategyContext, reason: str) -> None:
        context.trace.record(StepId.T2_SPARQL_FAILED, "error", 0, detail={"strategy": self.name, "reason": reason})

    # ── retrieval ────────────────────────────────────────────────────────

    async def _fetch_vector_hits(self, embedding: list[float], namespace: str) -> list[VectorHit] | None:
        """Graph-class hits (backend-filtered) plus property hits.

        Classes are retrieved with the RDF_SPARQL filter so the top hits are graph
        classes and carry their source id for routing. Properties carry no backend
        marker and are fetched unfiltered; the T-Box gate drops any whose class is
        not a graph class. Metric hits are dropped: governed metrics are SQL
        formulas, meaningless to a SPARQL prompt.
        """
        if not self._vector_client or not self._oss_ontology_index:
            return None
        try:
            index = ontology_vector_index_name(self._oss_ontology_index, namespace)
            class_hits = await self._vector_client.search(
                embedding,
                namespace=namespace,
                top_k=_VECTOR_TOP_K,
                index=index,
                entity_type="class",
                query_backend=StructuredBackend.RDF_SPARQL,
            )
            property_hits = await self._vector_client.search(
                embedding, namespace=namespace, top_k=_VECTOR_TOP_K, index=index, entity_type="property"
            )
        except Exception as e:  # retrieval boundary: degrade to label matching
            logger.warning("sparql_strategy_vector_search_failed", namespace=namespace, error=type(e).__name__)
            return None
        return [
            VectorHit(
                type=f"ontology_{hit.metadata.get('entity_type')}",
                score=hit.score,
                entity_id=hit.metadata.get("text", hit.id),
                uri=hit.metadata.get("entity_uri", ""),
                metadata={
                    "data_source_id": hit.metadata.get("data_source_id", ""),
                    "query_backend": hit.metadata.get("query_backend", ""),
                },
            )
            for hit in [*class_hits, *property_hits]
            if hit.metadata.get("entity_type") in ("class", "property") and hit.metadata.get("entity_uri")
        ]


async def _run(client: SparqlEndpointClient, safe: SafeQuery) -> tuple[list[dict], list[str]]:
    """Execute by query form and shape the answer as rows + ordered columns."""
    if safe.form == QueryForm.ASK:
        return [{"result": await client.ask(safe.sparql)}], ["result"]
    if safe.form in (QueryForm.CONSTRUCT, QueryForm.DESCRIBE):
        # A LIMIT bounds solutions, not triples, so the client caps the triples at the same bound.
        triples = await client.construct(safe.sparql, max_triples=safe.limit or DEFAULT_MAX_RESULTS)
        return triples, ["subject", "predicate", "object"]
    # The firewall always bounds a SELECT; the fallback only satisfies the signature.
    bindings = await client.query(safe.sparql, max_results=safe.limit or DEFAULT_MAX_RESULTS)
    return _bindings_to_rows(bindings, safe.variables)


def _bindings_to_rows(bindings: list[dict[str, Any]], variables: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """Rows carrying every column, in projection order; unbound variables become ``""``.

    Columns follow the SELECT projection; a variable a binding carries that the
    projection did not name (``SELECT *``) is appended in first-seen order.
    """
    columns = list(variables)
    seen = set(columns)
    for row in bindings:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return [{col: row.get(col, "") for col in columns} for row in bindings], columns


def _as_sparql_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renamed = []
    for step in steps:
        step = dict(step)
        step["step"] = _TRANSLATOR_STEPS.get(step.get("step", ""), step.get("step"))
        renamed.append(step)
    return renamed


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


# A delete moves the row here without clearing queryable; such a source never answers again.
_LEAVING_STATUSES = frozenset({"DELETING", "DELETE_FAILED"})

# A full predicate IRI as an expanded query term spells it: http(s) or urn, no CURIEs.
_ABSOLUTE_IRI_RE = re.compile(r"(?:https?://|urn:)[^\s<>\"{}|\\^`]+")


def _is_absolute_iri(name: str) -> bool:
    """A grant's denied name a predicate can be matched against: a full http(s) or urn IRI, exactly."""
    return bool(_ABSOLUTE_IRI_RE.fullmatch(name))


def _misaddressed_denylist(profile: dict[str, Any] | None, source_id: str, source_name: str) -> bool:
    """A columnDenylist entry keyed by this source's name or by ``"*"`` rather than its id.

    Entries are keyed by source id; such an entry would otherwise deny nothing here.
    """
    raw = (profile or {}).get("columnDenylist")
    if not isinstance(raw, dict):
        return False
    name = source_name.strip().casefold()
    for key, names in raw.items():
        key_cf = str(key).strip().casefold()
        if key_cf == source_id.casefold() or not names:
            continue
        if key_cf == "*" or (name and key_cf == name):
            return True
    return False


def _denied_iris(profile: dict[str, Any] | None) -> frozenset[str]:
    """Every predicate IRI any columnDenylist entry of the grant denies, to keep out of the LLM's T-Box."""
    raw = (profile or {}).get("columnDenylist")
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(
        str(name)
        for names in raw.values()
        if isinstance(names, (list, tuple))
        for name in names
        if _is_absolute_iri(str(name))
    )
