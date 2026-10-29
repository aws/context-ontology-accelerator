# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured-retrieval tools for the Agentic Retriever (NL→SQL and NL→SPARQL).

Wraps the existing :class:`StructuredQueryTier` — the same engine the standard
Tier-2 path uses — and exposes its TWO strategies to the reasoning loop as separate
Tools that produce on the ``rows`` channel of :class:`ToolResult`:

  * ``nl_to_sql``    → ``NLtoSQLStrategy``: NL→SQL directly over the mapped tables.
  * ``nl_to_sparql`` → ``OntopStrategy``: NL→SPARQL over the ontology/VKG, which
    Ontop rewrites to SQL.

They are separate tools because they are genuinely different capabilities and the
planner should be able to pick the ontology path on purpose. ``StructuredQueryTier``
already supports this via its ``option`` kwarg, so each tool just pins its own.
This is what lets the agentic loop answer a structured sub-question (e.g. "what was
revenue in Q3?") against a DATABASE source instead of only retrieving document chunks.

Composition gating (Req: "don't present structured tools when there is no
structured source") is PER-REQUEST, not at registration: the factory registers
these tools process-wide whenever a ``StructuredQueryTier`` exists, and the
retriever withholds them from the planner's spec list on a namespace with no
structured source (``_STRUCTURED_TOOL_NAMES`` -> ``excluded_tools`` ->
``registry.specs(exclude=...)``). The tools themselves never see composition; they
always attempt the query they are given.

The per-request ``profile`` / ``options`` / ``model_id`` reach ``invoke`` OUT-OF-BAND
from the controller, NOT through the planner's tool inputs: identity is not the
LLM's to supply, and ``validate_inputs`` rejects any key outside a tool's
``input_schema`` anyway. The profile is load-bearing — SQLFirewall/Cedar fail
CLOSED when no roles resolve, so an empty profile turns every invocation into an
``AccessDeniedError`` that the blanket ``except`` renders as a plain retrieval miss.

Per-Tool failure tolerance (Req 9.1-9.2): like every Tool, ``invoke`` never
raises for a retrieval failure. ``StructuredQueryTier.resolve`` returns ``None``
when no strategy produced a confident answer; the tool maps that to an empty
:class:`ToolResult` whose ``detail`` records the miss, so the controller scores it
as no-progress and can escalate, exactly as an empty document retrieval would.

Import discipline (Requirement 12.3): this module imports only the dependency-free
Tool base and, lazily inside ``invoke``, the Tier-2 strategy context — never
``graphrag_toolkit``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ....exceptions import AccessDeniedError
from .base import ExecutedQuery, ToolResult
from .registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ....tier2.strategy import StructuredQueryTier

logger = structlog.get_logger(__name__)

NL_TO_SQL_TOOL = "nl_to_sql"

NL_TO_SPARQL_TOOL = "nl_to_sparql"

_SQL_DESCRIPTION = (
    "NL→SQL structured query over the namespace's relational (DATABASE) sources. "
    "Translates a natural-language question into SQL, executes it, and returns the "
    "result rows. Best for precise, tabular questions — specific figures, counts, "
    "aggregations, filters over columns (e.g. 'total revenue for 2023', 'how many "
    "orders shipped in Q2'). Not for open-ended prose or document passages."
)

_SPARQL_DESCRIPTION = (
    "NL→SPARQL structured query over the namespace's ONTOLOGY (virtual knowledge "
    "graph): translates the question into SPARQL, which Ontop rewrites to SQL over "
    "the mapped DATABASE sources, and returns the result rows. Prefer this over "
    "nl_to_sql when the question follows ONTOLOGY relationships — traversing between "
    "classes, or using a modelled concept/property name rather than raw column names. "
    "Returns the same tabular rows."
)

_INPUT_SCHEMA: dict = {"query": "str"}
# Rows are the structured channel; chunks/entities stay empty for these tools.
_OUTPUT_SCHEMA: dict = {"rows": "list"}


class StructuredQueryTool:
    """Exposes one :class:`StructuredQueryTier` strategy as an agentic Tool (rows).

    One class, two registrations: the SQL and SPARQL tools differ only by the
    ``option`` handed to ``StructuredQueryTier.resolve`` (and their name/description),
    so they share this wrapper rather than duplicating the result mapping.
    """

    def __init__(
        self,
        structured_query_tier: StructuredQueryTier,
        *,
        name: str,
        description: str,
        option: str,
        embed_client: Any | None = None,
    ) -> None:
        """Build a structured-query Tool bound to one ``StructuredQueryTier`` option."""
        self.name = name
        self.description = description
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._tier = structured_query_tier
        self._option = option
        self._embed_client = embed_client

    async def _embed(self, text: str) -> list[float] | None:
        """Embed the sub-question, or ``None`` if unavailable (never raises).

        What the embedding actually buys (measured against the code, not assumed):
        Ontop skips its vector-hit lookup entirely without one, which costs the
        metric context, the ``:aiContext`` glossary, and the confidence-calibration
        signal. It does NOT change the class/property T-Box for namespaces at or
        under the 200-mapped-class full-context threshold — that path is
        vector-independent. Note the calibration loss cuts both ways: with no hits
        neither the strong-hit boost nor the weak-hit penalty applies, so an
        ungrounded SPARQL can score HIGHER than it deserves.

        For ``nl_to_sql`` this relocates an embed rather than saving one
        (``SQLGenerator`` self-embeds otherwise) — but relocating it is still worth
        it, because an embed failure inside ``SQLGenerator`` is terminal for the
        strategy, whereas here it degrades to ``None``.

        Embeds the SUB-QUESTION, not the original request query: the request-level
        embedding is never plumbed into the reasoning loop at all, so there is no
        precomputed vector available mid-loop. This matches ``vector_tool``, which
        re-embeds its own sub-question the same way.
        """
        if self._embed_client is None or not text:
            return None
        try:
            return await self._embed_client.embed(text)
        except Exception as exc:  # noqa: BLE001 - degrade to no vector context
            logger.warning("structured_tool_embed_failed", tool=self.name, error=str(exc))
            return None

    async def invoke(
        self,
        *,
        namespace: str,
        query: str = "",
        profile: dict | None = None,
        options: dict | None = None,
        model_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        """Run the strategy for ``query`` scoped to ``namespace``; return rows.

        Embeds the sub-question so the strategy gets its ontology vector context,
        then delegates to ``StructuredQueryTier.resolve`` with this tool's ``option``.
        A ``None`` result (no confident answer) or an exception both map to an empty
        :class:`ToolResult`; the tool never raises (Req 9.1-9.2).
        """
        from ....tier2.strategy import StrategyContext

        # profile/options/model_id arrive OUT-OF-BAND from the controller (never via
        # planner inputs — see the module docstring). profile is load-bearing:
        # SQLFirewall/Cedar fail CLOSED on an empty one.
        context = StrategyContext(
            profile=profile or {},
            options=options or {},
            model_id=model_id,
            embedding=await self._embed(query),
        )
        try:
            result = await self._tier.resolve(query, namespace, context, option=self._option)
        except AccessDeniedError:
            # An authorization denial is NOT a retrieval failure and must not be
            # degraded into "0 rows". AccessDeniedError is the firewall/Cedar 403
            # (exceptions.py:33) and every other consumer of this engine propagates
            # it — the standard Tier-2 path raises it straight out of the
            # orchestrator. Swallowing it here would turn a 403 into a 200 with an
            # empty answer, hiding a misconfigured grant behind a plausible
            # "no data found". Re-raise so the request fails loudly with 403.
            raise
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise into the loop
            logger.warning(
                "structured_tool_invoke_error", tool=self.name, error=str(exc), error_type=type(exc).__name__
            )
            return ToolResult(detail=f"{self.name} error: {type(exc).__name__}")

        if result is None or not result.rows:
            detail = f"{self.name}: no rows" if result is None else f"{self.name}: 0 rows"
            return ToolResult(detail=detail)

        rows = tuple(result.rows)
        # Mirror rows into items so accumulation/observability see them under the
        # generic item view too (chunks/entities stay empty for a structured tool).
        items = tuple({"type": "row", **row} for row in result.rows)
        detail = f"{self.name}: {len(rows)} rows via {result.strategy_name} (conf {result.confidence:.2f})"
        # Capture the query artifacts this tool built + executed so Deep Reasoning
        # can return them the same way Standard mode does (#1166). The VKG path
        # yields both a SPARQL statement and the SQL it compiled to; NL→SQL yields
        # SQL only (result.sparql is empty). Surfaced behind includeDebugInfo at
        # assembly time — capturing here does not itself expose them.
        executed: list[ExecutedQuery] = []
        if result.sparql:
            executed.append(ExecutedQuery(kind="sparql", query=result.sparql))
        if result.sql:
            executed.append(ExecutedQuery(kind="sql", query=result.sql))
        return ToolResult(rows=rows, items=items, detail=detail, executed_queries=tuple(executed))


def build_structured_tools(
    registry: ToolRegistry, structured_query_tier: StructuredQueryTier, *, embed_client: Any | None = None
) -> list[str]:
    """Register the NL→SQL and NL→SPARQL Tools; return their names.

    Both are registered together (they need the same engine) and both are withheld
    per-request by composition gating on a namespace with no structured source.
    Exposing them SEPARATELY lets the planner deliberately choose the ontology/VKG
    path — previously only ``nl_to_sql`` existed and it silently fell back to Ontop
    via the tier's default option, so the SPARQL capability was reachable but
    mislabeled and never selectable on purpose.
    """
    from ....tier2.strategy import StrategyOption

    specs = (
        (NL_TO_SQL_TOOL, _SQL_DESCRIPTION, StrategyOption.NL_TO_SQL),
        (NL_TO_SPARQL_TOOL, _SPARQL_DESCRIPTION, StrategyOption.ONTOP),
    )
    names: list[str] = []
    for name, description, option in specs:
        registry.register(
            StructuredQueryTool(
                structured_query_tier,
                name=name,
                description=description,
                option=option,
                embed_client=embed_client,
            )
        )
        names.append(name)

    logger.info("structured_tools_registered", tools=names)
    return names
