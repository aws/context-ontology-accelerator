# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Uniform Tool contract and result for the Agentic Retriever (Req 2, Req 11).

Every retrieval capability the reasoning agent can invoke — Retrieval_Strategy
tools, the Graph_Traversal tool, the Vector_Search tool, the Ontology_Lookup
tool — implements the single :class:`Tool` Protocol defined here and returns a
:class:`ToolResult`. The uniform contract is what lets the registry enumerate
tools for the planner and the controller invoke any tool the same way
(Req 11.1).

Import discipline (Requirement 12.3): this module is dependency-free — it uses
only the standard library and ``typing`` and never imports ``graphrag_toolkit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecutedQuery:
    """A query artifact a structured tool built and executed (#1166).

    ``kind`` is ``"sql"`` or ``"sparql"``; ``query`` is the statement text. The
    VKG path produces BOTH a SPARQL statement and the SQL it compiles to, so a
    single structured invocation can yield two ``ExecutedQuery`` records. Carries
    only the statement text — no rows — because it is surfaced (behind
    ``includeDebugInfo``) purely to show the query that ran, mirroring Standard
    mode's ``queryUsed`` / ``sparqlGenerated``.
    """

    kind: str
    query: str


@dataclass(frozen=True)
class ToolResult:
    """Uniform output of every Tool invocation (Req 11.1).

    A Tool never raises out of ``invoke`` for an expected retrieval failure
    (timeouts, backend errors); it returns a ``ToolResult`` whose ``detail``
    records the degradation so the controller can incorporate a degraded,
    non-fatal outcome and continue (Req 9.1-9.2). The passthrough ``chunks`` /
    ``entities`` carry the native retrieval objects forward for final synthesis,
    while ``items`` holds their normalized dict form for accumulation/dedupe.

    Attributes:
        items: Normalized retrieval items (chunks / entities / ontology facts)
            as plain dicts, used for accumulation and de-duplication.
        chunks: Native chunk objects (e.g. ``ChunkResult``) passed through
            untouched for the final Synthesizer.
        entities: Native entity objects (e.g. ``GraphEntity``) passed through
            untouched for the final Synthesizer.
        rows: Structured result rows (e.g. from NL→SQL / metric resolution) as
            plain dicts, passed through to the final Synthesizer. This is the
            channel structured tools produce on: chunks/entities are for
            document/graph retrieval, whereas a SQL or metric answer is tabular.
            ``AccumulatedContext.add`` counts new rows toward progress, so a
            successful structured tool is not mis-scored as no-progress.
        detail: Human-readable trace detail for this invocation, including any
            degradation reason.
        degraded_reason: Stable machine-readable reason when the Tool returned a
            degraded result while still completing normally. The controller
            records it in ``degraded_sources`` and marks the session partial.
        produced_directions: Candidate sub-question seeds this result surfaced
            (e.g. one per discovered competitor) that the controller may open as
            Exploration_Branches subject to the fan-out cap (Req 13.4).
        executed_queries: The query artifacts a structured tool built and ran to
            produce ``rows`` — one ``ExecutedQuery`` per generated statement (SQL
            from NL→SQL, SPARQL + its compiled SQL from the VKG path). Document /
            graph / ontology tools leave this empty. Surfaced (gated by
            ``includeDebugInfo``) on the final ``QueryResult`` so Deep Reasoning
            returns the same query artifacts Standard mode does (#1166).
    """

    items: tuple[dict, ...] = ()
    chunks: tuple[Any, ...] = ()
    entities: tuple[Any, ...] = ()
    rows: tuple[dict, ...] = ()
    detail: str = ""
    degraded_reason: str = ""
    produced_directions: tuple[str, ...] = ()
    executed_queries: tuple[ExecutedQuery, ...] = ()


@runtime_checkable
class Tool(Protocol):
    """Uniform tool contract (Req 2, Req 11).

    The four interface fields — ``name``, ``description``, ``input_schema``,
    ``output_schema`` — are all REQUIRED and must be present on every registered
    Tool (Req 11.1). ``input_schema`` declares the accepted input fields and
    their types; ``output_schema`` declares the produced output fields and their
    types. The async ``invoke`` performs the retrieval and returns a
    :class:`ToolResult`, always scoped to the request ``namespace``.

    ``@runtime_checkable`` lets the registry perform a structural presence check;
    the registry additionally validates the field values (non-empty name and
    description, dict schemas) because a Protocol ``isinstance`` check only
    confirms attribute presence, not that the values satisfy the contract.
    """

    name: str
    description: str
    input_schema: dict
    output_schema: dict

    async def invoke(self, *, namespace: str, **kwargs: Any) -> ToolResult:
        """Run the tool against ``namespace`` and return a :class:`ToolResult`."""
        ...
