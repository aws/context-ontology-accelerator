# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Translation & Query Execution — Tier 2 resolution pipeline.

Chains: SPARQL -> VKG translate -> SQL Firewall -> Query Execute.
Records trace steps for each stage with timing and status.

Executor dispatch (single-vs-cross-source): executor SELECTION is
delegated to the injected ``QueryExecutor``. Production (main.py) injects a
``CompositeQueryExecutor`` that routes single-source queries (one catalog, all
tables qualified, source confirmed JDBC-capable via the has_jdbc_endpoint gate)
to direct JDBC (SourceDBQueryExecutor) and everything else — cross-source,
Glue-native, ambiguous, or with JDBC disabled — to Athena federation.

Data-source RESOLUTION (Kun's 40cd2b8): ``_resolve_data_source`` /
``_infer_data_source`` resolve a ``data_source_id`` (explicit caller id -> VKG
``datasourceRouting`` -> table-prefix heuristic) that is passed into
``executor.execute(...)``; the composite/Athena use it for routing and
per-source catalog/schema lookup (athena.py ``_resolve_catalog_and_database``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from ...clients.base import QueryExecutor, QueryResult
from ...clients.vkg import SparqlProjection, VKGClient, VKGResult
from ...identity import display_principal
from ..sql_firewall import FirewallResult, SQLFirewall
from ..table_qualifier import SourceLookup, distinct_datasources, prepare_execution_sql

logger = structlog.get_logger(__name__)


class Tier2Step(StrEnum):
    """Trace step identifiers for the VKG resolution pipeline stages."""

    # SPARQL → SQL compilation by Ontop (distinct from the LLM "generate SPARQL"
    # step in nl_to_sparql.py, which keeps t2.vkg.translate). This is where a
    # syntactically-valid SPARQL can still fail (vkg_translation_error), which
    # is what triggers the retry loop.
    VKG_COMPILE = "t2.vkg.compile"
    VKG_AUTHORIZE = "t2.vkg.authorize"
    SQL_FIREWALL = "t2.vkg.firewall"
    QUERY_EXECUTE = "t2.vkg.execute"


class Tier2Status(StrEnum):
    """Outcome status codes recorded for each VKG resolution step."""

    OK = "success"
    ERROR = "error"
    DENIED = "denied"


@dataclass
class TraceStep:
    """One step in the VKG resolution trace."""

    step: str
    status: str
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tier2Result:
    """Full result of the Tier 2 VKG resolution pipeline."""

    query_result: QueryResult | None
    vkg_result: VKGResult | None = None
    firewall_result: FirewallResult | None = None
    trace_steps: list[TraceStep] = field(default_factory=list)
    error: str | None = None
    #: The statement actually handed to the executor — i.e. ``vkg_result.sql`` after
    #: the firewall's rewrite AND cross-source qualification. Distinct from
    #: ``vkg_result.sql``, which is Ontop's raw translate output. Reporting the raw
    #: form as ``queryUsed`` is actively misleading after the cross-source fix: a cross-source
    #: statement's bare table names cannot resolve under any single
    #: ``QueryExecutionContext``, so the SQL shown to an operator is one that
    #: provably does not run. ``None`` when the pipeline never reached execution.
    executed_sql: str | None = None


class VKGTranslator:
    """Orchestrates SPARQL -> SQL translation, firewall check, and execution.

    Dispatch is the injected executor's job (see module docstring): production
    injects a CompositeQueryExecutor that picks direct JDBC (single gated
    source) vs Athena federation (cross-source/Glue/ambiguous) per query.
    """

    def __init__(
        self,
        vkg_endpoint: str,
        firewall: SQLFirewall,
        query_executor: QueryExecutor,
        sources_registry: SourceLookup | None = None,
    ):
        """Configure the VKG endpoint, firewall, and query executor.

        Args:
            vkg_endpoint: Base URL of the VKG service (rewritten per namespace).
            firewall: SQLFirewall enforcing safety and authorization on compiled SQL.
            query_executor: Executor that runs the authorized SQL.
            sources_registry: Optional registry used ONLY to look up the Athena
                catalog name of each datasource when a query spans more than one
                (see :mod:`..table_qualifier`). With no registry wired, a
                cross-source query keeps its previous behaviour. Typed as the
                narrow :class:`~..table_qualifier.SourceLookup` Protocol rather
                than left bare, so a wiring mistake in ``dependencies.py`` fails
                type-check instead of surfacing as a runtime AttributeError on
                the first cross-source query.
        """
        self._vkg_endpoint = vkg_endpoint
        self._firewall = firewall
        self._executor = query_executor
        self._sources = sources_registry
        self._clients: dict[str, VKGClient] = {}

    _MAX_CACHED_CLIENTS = 64

    def _resolve_client(self, namespace: str) -> VKGClient:
        """Resolve a VKGClient for the given namespace.

        Rewrites the base VKG endpoint to target the per-namespace service:
          "http://vkg.coa-dev-services.local:8080"
            → "http://vkg-{namespace}.coa-dev-services.local:8080"

        Clients are cached per namespace (bounded to 64) to reuse HTTP connections.
        """
        if namespace not in self._clients:
            url = self._vkg_endpoint.replace("://vkg.", f"://vkg-{namespace}.")
            if url == self._vkg_endpoint:
                raise ValueError(
                    f"Cannot resolve per-namespace VKG: endpoint '{self._vkg_endpoint}' "
                    f"does not contain '://vkg.' to rewrite"
                )
            if len(self._clients) >= self._MAX_CACHED_CLIENTS:
                oldest = next(iter(self._clients))
                del self._clients[oldest]
            self._clients[namespace] = VKGClient(base_url=url)
        return self._clients[namespace]

    async def resolve(
        self,
        sparql: str,
        *,
        namespace: str,
        data_source_id: str = "",
        profile: dict[str, Any] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = 60,
    ) -> Tier2Result:
        """Run the full Tier 2 pipeline: translate -> firewall -> execute.

        Args:
            sparql: Valid SPARQL query to translate.
            namespace: Ontology namespace (validated for safe interpolation).
            data_source_id: Target data source (may be empty for cross-source).
            profile: Caller's authorization profile for firewall evaluation.
            max_rows: Maximum result rows.
            timeout_seconds: Query execution timeout.

        Returns:
            Tier2Result with query results or error information.
        """
        from ...query_utils import validate_namespace

        validate_namespace(namespace)
        trace: list[TraceStep] = []

        # Step 1: VKG Translation (SPARQL -> SQL)
        start = time.perf_counter()
        logger.info("vkg_translate_input", sparql_length=len(sparql))
        logger.debug("vkg_translate_input_full", sparql=sparql)
        try:
            vkg_client = self._resolve_client(namespace)
            vkg_result = await vkg_client.translate(sparql, namespace=namespace)
            routing = vkg_result.datasource_routing or {}
            routing_sources = sorted({v.get("datasourceId", "") for v in routing.values()} - {""})
            logger.info(
                "vkg_translate_output",
                sql_length=len(vkg_result.sql),
                sql_preview=vkg_result.sql[:300],
                routing_tables=len(vkg_result.datasource_routing),
                routing_sources=routing_sources,
            )
            trace.append(
                TraceStep(
                    step=Tier2Step.VKG_COMPILE,
                    status=Tier2Status.OK,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail={"dialect": vkg_result.dialect, "tables": vkg_result.source_table_refs},
                )
            )
        except Exception as e:
            trace.append(
                TraceStep(
                    step=Tier2Step.VKG_COMPILE,
                    status=Tier2Status.ERROR,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail={"error": type(e).__name__},
                )
            )
            return Tier2Result(query_result=None, trace_steps=trace, error="vkg_translation_error")

        if vkg_result.ontology_version == "unknown":
            logger.warning(
                "vkg_ontology_version_unknown",
                namespace=namespace,
                ontology_version=vkg_result.ontology_version,
            )

        # Step 2: Authorization + SQL Firewall
        principal_id = display_principal(profile) or "unknown"
        start = time.perf_counter()
        try:
            fw_result = self._firewall.evaluate(vkg_result.sql, profile, namespace=namespace)
            fw_ms = int((time.perf_counter() - start) * 1000)
            authorize_status = Tier2Status.DENIED if fw_result.denied else Tier2Status.OK
            trace.append(
                TraceStep(
                    step=Tier2Step.VKG_AUTHORIZE,
                    status=authorize_status,
                    duration_ms=fw_ms,
                    detail={"principal": principal_id, "decision": "deny" if fw_result.denied else "allow"},
                )
            )
            trace.append(
                TraceStep(
                    step=Tier2Step.SQL_FIREWALL,
                    status=Tier2Status.OK if not fw_result.denied else Tier2Status.DENIED,
                    duration_ms=fw_ms,
                    detail={"denied": fw_result.denied},
                )
            )
            if fw_result.denied:
                return Tier2Result(
                    query_result=None,
                    vkg_result=vkg_result,
                    firewall_result=fw_result,
                    trace_steps=trace,
                    error=f"Firewall denied: {fw_result.reason}",
                )
        except Exception as e:
            fw_ms = int((time.perf_counter() - start) * 1000)
            trace.append(
                TraceStep(
                    step=Tier2Step.SQL_FIREWALL,
                    status=Tier2Status.ERROR,
                    duration_ms=fw_ms,
                    detail={"error": type(e).__name__},
                )
            )
            return Tier2Result(query_result=None, vkg_result=vkg_result, trace_steps=trace, error="firewall_error")

        # Step 3: Query Execution
        # Resolve the target data source. An explicit caller-provided ID takes
        # precedence; otherwise we infer from VKG's datasourceRouting (R2RML
        # annotations) which is deterministic, falling back to table-prefix
        # heuristics only when routing metadata is unavailable.
        resolved_source = self._resolve_data_source(data_source_id, vkg_result)
        authorized_sql = fw_result.authorized_sql

        # Make the authorized SQL executable: restore any synthetic schema token
        # the mapping used to keep two sources' same-named tables apart, then
        # qualify to catalog.schema.table if the query spans sources.
        #
        # Runs AFTER authorization by design: the firewall's allowlist/denylist
        # keys are bare table names (unaffected by prefixes), but Cedar sees
        # extract_tables() output verbatim, so rewriting earlier would change the
        # strings a table-scoped policy matches on. A single-source query needing
        # no schema restoration is returned untouched.
        prepared = await prepare_execution_sql(
            authorized_sql,
            vkg_result.datasource_routing,
            self._sources,
            namespace=namespace,
        )
        if prepared.error:
            logger.warning("tier2_qualification_failed", namespace=namespace, error=prepared.error)
            trace.append(
                TraceStep(
                    step=Tier2Step.QUERY_EXECUTE,
                    status=Tier2Status.ERROR,
                    duration_ms=0,
                    detail={"error": prepared.error},
                )
            )
            return Tier2Result(
                query_result=None,
                vkg_result=vkg_result,
                firewall_result=fw_result,
                trace_steps=trace,
                error="query_qualification_error",
            )
        authorized_sql = prepared.sql

        start = time.perf_counter()
        try:
            query_result = await self._executor.execute(
                authorized_sql,
                namespace=namespace,
                data_source_id=resolved_source,
                max_rows=max_rows,
                timeout_seconds=timeout_seconds,
            )
            # Capture the executing engine BEFORE projection: _project_to_sparql may
            # return a fresh QueryResult that does not carry it forward. Surfaced on
            # the execute step (athena | redshift | jdbc) so the Redshift-dispatch
            # guard is observable from the trace on the ontop path too — the
            # nl_to_sql path already records it.
            executed_engine = getattr(query_result, "engine", "") or "unknown"
            # Project raw SQL result to SPARQL solution (if projection metadata available)
            if vkg_result.projection:
                query_result = self._project_to_sparql(query_result, vkg_result.projection)
            trace.append(
                TraceStep(
                    step=Tier2Step.QUERY_EXECUTE,
                    status=Tier2Status.OK,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail={
                        "row_count": query_result.row_count,
                        "truncated": query_result.truncated,
                        "engine": executed_engine,
                    },
                )
            )
        except Exception as e:
            logger.warning("tier2_query_execute_failed", error=str(e))
            trace.append(
                TraceStep(
                    step=Tier2Step.QUERY_EXECUTE,
                    status=Tier2Status.ERROR,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail={"error": f"{type(e).__name__}: {str(e)}"},
                )
            )
            return Tier2Result(
                query_result=None,
                vkg_result=vkg_result,
                firewall_result=fw_result,
                trace_steps=trace,
                error="query_execution_error",
            )

        return Tier2Result(
            query_result=query_result,
            vkg_result=vkg_result,
            firewall_result=fw_result,
            trace_steps=trace,
            executed_sql=authorized_sql,
        )

    def _resolve_data_source(self, explicit_id: str, vkg_result: VKGResult) -> str:
        """Determine which data source to execute against.

        Resolution order:
        1. Explicit caller-provided ID (not empty, not "default")
        2. VKG datasourceRouting — deterministic, from R2RML annotations
        3. Table-prefix heuristic — infers catalog from dotted table refs

        The "default" sentinel means "no preference" and triggers inference.
        """
        if explicit_id and explicit_id != "default":
            return explicit_id
        return self._infer_data_source(vkg_result)

    def _infer_data_source(self, vkg_result: VKGResult) -> str:
        """Infer data source from VKG response metadata.

        Tries datasourceRouting first (deterministic); falls back to
        parsing catalog prefixes from source_table_refs (heuristic).
        Returns empty string if inference is ambiguous or impossible —
        the Athena client then falls back to scanning DDB for any DATABASE
        source in the namespace (works for single-source only).

        The returned source ID is used by the Athena client to fetch the
        source record from DDB (athenaDataCatalogName, discoveredSchemas)
        for catalog/schema resolution.

        A MULTI-source query still returns "" here, but that no longer means
        "pick an arbitrary source": ``resolve()`` qualifies such SQL to
        ``catalog.schema.table`` first, after which the executor's catalog
        resolution is irrelevant (qualified names override the Athena
        ``QueryExecutionContext``). "" is now "no single source applies".
        """
        if vkg_result.datasource_routing:
            ds_ids = distinct_datasources(vkg_result.datasource_routing)
            if len(ds_ids) == 1:
                resolved = ds_ids.pop()
                logger.info(
                    "datasource_resolved_from_routing",
                    datasource_id=resolved,
                    table_count=len(vkg_result.datasource_routing),
                )
                return resolved
            logger.info("datasource_routing_ambiguous", distinct_sources=len(ds_ids))
            return ""

        if not vkg_result.source_table_refs:
            return ""
        catalogs = set()
        for ref in vkg_result.source_table_refs:
            parts = ref.split(".")
            if len(parts) >= 2:
                catalogs.add(parts[0])
        if len(catalogs) == 1:
            resolved = catalogs.pop()
            logger.info("datasource_resolved_from_prefix", datasource_id=resolved)
            return resolved
        return ""

    def _project_to_sparql(self, qr: QueryResult, proj: SparqlProjection) -> QueryResult:
        """Project raw SQL result to SPARQL solution.

        Maps SQL column aliases to SPARQL variable names and drops columns
        not in the SELECT. Handles DISTINCT deduplication when required.

        If any SPARQL variable's alias is absent from the result columns, the
        raw result is returned unchanged: reading a missing key would yield a
        correct-looking header over an all-NULL column, which is a worse
        failure than showing the SQL aliases. Alias lookup falls back to a
        case-insensitive match because some drivers normalise column labels.

        Args:
            qr: Raw QueryResult from SQL execution (keyed by SQL aliases).
            proj: Projection metadata from Ontop (SPARQL vars -> SQL aliases).

        Returns:
            QueryResult with columns renamed to SPARQL variables and projected,
            or ``qr`` unchanged when the projection cannot be applied.
        """
        columns = list(proj.select_vars)
        by_lower = {c.lower(): c for c in qr.columns}

        # Resolve every variable to a real result column up front.
        resolved: dict[str, str] = {}
        for var in columns:
            alias = proj.var_to_column.get(var, var)
            # Exact match must be tried first: by_lower collapses columns that
            # differ only in case (last wins), so a lower-only lookup against a
            # result set holding both "NAME" and "name" could read the wrong
            # one — a correct-looking header over wrong values.
            if alias in qr.columns:
                resolved[var] = alias
            elif alias.lower() in by_lower:
                resolved[var] = by_lower[alias.lower()]
            else:
                logger.warning(
                    "tier2_projection_alias_missing",
                    variable=var,
                    sql_alias=alias,
                    sql_columns=qr.columns,
                )
                return qr

        projected_rows = []
        seen: set[tuple[Any, ...]] = set()

        for row in qr.rows:
            # Read each SPARQL var's value via its SQL alias
            projected = {var: row.get(resolved[var]) for var in columns}

            # Dedupe only for SELECT DISTINCT; plain SELECT is a bag
            if proj.distinct:
                key = tuple(projected[c] for c in columns)
                if key in seen:
                    continue
                seen.add(key)

            projected_rows.append(projected)

        return QueryResult(
            rows=projected_rows,
            columns=columns,
            row_count=len(projected_rows),
            truncated=qr.truncated,
            duration_ms=qr.duration_ms,
        )
