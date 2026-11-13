# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology_Lookup_Tool: the determined ontology for the request namespace.

:class:`OntologyLookupTool` exposes the determined ontology — the complete set of
node types, the complete set of edge types, and the directional source→target
mapping per edge type — through the uniform :class:`Tool` contract, so
the reasoning controller invokes it the same way as every other Tool. It resolves
the ontology through a configurable :class:`OntologySource` (graph or file) and
returns the result in one source-agnostic shape regardless of which backend
resolved it.

The tool caches populated loads per namespace (the Tool instance lives for the
process). A load failure or an empty ontology is never cached, so a later request
re-queries once the ontology is induced or the backend recovers. A source that
raises is caught and returned as a degraded :class:`ToolResult` with no ontology
item; :func:`ontology_from_result` returns an :class:`Ontology` (possibly empty)
on success and ``None`` on a load failure.

Import discipline: module load imports only the dependency-free
``.base`` contract and the sibling ``..ontology`` abstraction (which itself imports
``rdflib`` lazily and never imports ``graphrag_toolkit``) plus ``structlog``, so
importing this module never pulls the toolkit into ``sys.modules``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..ontology import Ontology
from .base import ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ontology import OntologySource
    from .registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "ontology_lookup"

# Item ``type`` discriminator for the single ontology item a successful lookup
# emits; :func:`ontology_from_result` keys on it to reconstruct the Ontology.
_ONTOLOGY_ITEM_TYPE = "ontology"

_DESCRIPTION = (
    "Ontology lookup for the request namespace. Returns the determined ontology: "
    "the set of node types, the set of edge types, and the directional "
    "source->target mapping for each edge type. Consult this BEFORE choosing a "
    "graph traversal so you only traverse edge types that exist in the ontology. "
    "The ontology is loaded once per namespace and reused for the rest of the "
    "request, so a repeat lookup is free."
)

# The ontology tool takes no planner-supplied inputs: the namespace is supplied by
# the controller to every Tool's invoke. An empty input_schema is a valid, declared
# contract (the registry permits an empty dict — a tool that accepts no inputs).
_INPUT_SCHEMA: dict = {}
_OUTPUT_SCHEMA: dict = {
    "items": "list (one ontology item with node_types, edge_types, edge_map)",
}


def _ontology_item(ontology: Ontology) -> dict:
    """Normalize an :class:`Ontology` to a plain dict for ``ToolResult.items``.

    The ``edge_map`` triples are emitted as lists so the item is a plain,
    JSON-friendly dict; :func:`ontology_from_result` rebuilds the tuples.
    """
    return {
        "type": _ONTOLOGY_ITEM_TYPE,
        "node_types": list(ontology.node_types),
        "edge_types": list(ontology.edge_types),
        "edge_map": [list(triple) for triple in ontology.edge_map],
    }


def ontology_from_result(result: ToolResult) -> Ontology | None:
    """Reconstruct the :class:`Ontology` from an Ontology_Lookup_Tool result.

    Returns the :class:`Ontology` on success (possibly empty) and ``None`` on a
    load failure.
    """
    for item in result.items:
        if isinstance(item, dict) and item.get("type") == _ONTOLOGY_ITEM_TYPE:
            return Ontology(
                node_types=tuple(item.get("node_types", ()) or ()),
                edge_types=tuple(item.get("edge_types", ()) or ()),
                edge_map=tuple(tuple(triple) for triple in (item.get("edge_map", ()) or ())),
            )
    return None


class OntologyLookupTool:
    """Returns the ontology in one shape regardless of source."""

    def __init__(self, source: OntologySource, *, source_name: str | None = None) -> None:
        """Build the Tool over an :class:`OntologySource`.

        Args:
            source: The configured ontology backend (graph or file). Its ``load``
                returns an :class:`Ontology` and raises on a backend failure; this
                Tool turns that raise into a degraded result rather than
                propagating it.
            source_name: Human-readable label for the source in use, recorded in
                the failure ``detail`` so the trace can name the source that failed.
               Defaults to the source's class name.
        """
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._source = source
        self._source_name = source_name or type(source).__name__
        # Per-namespace cache of successful loads. Failures are not
        # cached so a later attempt can retry.
        self._cache: dict[str, Ontology] = {}

    async def invoke(self, *, namespace: str, **_: Any) -> ToolResult:
        """Resolve the ontology for ``namespace`` and return a :class:`ToolResult`.

        Reuses the cached ontology when this namespace has already been resolved
        during the Tool's lifetime, so a repeat lookup performs no source load.
        On a fresh namespace it loads through the source, caches a
        successful result, and returns it. Never raises for a load failure: the
        source's exception is caught and surfaced as a degraded result that names
        the source in use.
        """
        cached = self._cache.get(namespace)
        if cached is not None:
            return self._success_result(cached, from_cache=True)

        try:
            ontology = await self._source.load(namespace)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise
            logger.warning(
                "ontology_lookup_failed",
                tool=self.name,
                source=self._source_name,
                namespace=namespace,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Load FAILURE: no ontology item, detail names the source.
            return ToolResult(detail=f"{self.name} failed: {type(exc).__name__} (source={self._source_name})")

        # Cache ONLY a populated ontology: an empty one is usually not induced yet,
        # so caching it would pin 0/0 until restart.
        if ontology.node_types or ontology.edge_types:
            self._cache[namespace] = ontology
        return self._success_result(ontology, from_cache=False)

    def _success_result(self, ontology: Ontology, *, from_cache: bool) -> ToolResult:
        """Build the :class:`ToolResult` for a successful (possibly empty) load.

        An empty ontology (no node types and no edge types) is a valid success;
        its ``detail`` carries the ``empty`` marker so it is distinct
        from both a populated ontology and a load failure.
        """
        node_count = len(ontology.node_types)
        edge_count = len(ontology.edge_types)
        cache_note = " (cached)" if from_cache else ""
        if node_count == 0 and edge_count == 0:
            detail = f"{self.name}: empty ontology (0 node types, 0 edge types){cache_note}"
        else:
            detail = (
                f"{self.name}: {node_count} node types, {edge_count} edge types, "
                f"{len(ontology.edge_map)} directional edges{cache_note}"
            )
        return ToolResult(items=(_ontology_item(ontology),), detail=detail)


def build_ontology_tool(registry: ToolRegistry, source: OntologySource, *, source_name: str | None = None) -> str:
    """Register the single :class:`OntologyLookupTool` into ``registry``.

    Mirrors :func:`build_graph_tool` / :func:`build_vector_tool`: a thin builder
    the factory calls. Unlike the endpoint-gated tools the ontology tool
    is ALWAYS registered (the file source needs no endpoint, and the graph source
    reuses the already-required graph client), so the agent can always consult the
    ontology. Returns the registered Tool name.
    """
    tool = OntologyLookupTool(source, source_name=source_name)
    registry.register(tool)
    logger.info("ontology_tool_registered", tool_name=tool.name, source=tool._source_name)
    return tool.name
