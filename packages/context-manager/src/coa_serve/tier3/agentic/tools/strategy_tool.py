# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Retrieval_Strategy_Tool: one Tool per graphrag-toolkit RetrieverStrategy.

Each :class:`RetrievalStrategyTool` wraps the existing, already-lazy
``BaselineLexicalRetriever`` adapter and invokes exactly one of the seven
registered graphrag-toolkit ``RetrieverStrategy`` options (Req 2.2, 5.1). The
builder :func:`build_strategy_tools` registers one Tool per enum member, named
``strategy:<value>`` (e.g. ``strategy:entity_network``).

Import discipline (Requirement 12.3): module load of this module MUST NOT import
``graphrag_toolkit`` — nor anything from the ``coa_serve.lexical``
package, whose ``__init__`` imports the toolkit eagerly. Accordingly:

- the per-strategy descriptions are keyed by the strategy's **string value**, so
  no enum import is needed at module load;
- ``BaselineLexicalRetriever`` / ``RetrieverStrategy`` / ``StrategySpec`` are
  referenced only under ``TYPE_CHECKING`` for typing;
- the only access to ``STRATEGY_REGISTRY`` is a **lazy import inside**
  :meth:`RetrievalStrategyTool.invoke`; the enum is imported lazily inside
  :func:`build_strategy_tools`.

Per-Tool failure tolerance (Req 9.1-9.2): the wrapped adapter already degrades to
an empty result with an error trace step on timeout/error and never raises, so a
strategy Tool invocation likewise never raises — it returns a (possibly empty)
:class:`ToolResult` whose ``detail`` records any degradation for the controller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .base import ToolResult
from .registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ....lexical.baseline_retriever import BaselineLexicalRetriever, BaselineResult
    from ....lexical.strategies import RetrieverStrategy

logger = structlog.get_logger(__name__)


# Static, graphrag-free descriptions keyed by the RetrieverStrategy *value*
# string (the ``StrEnum`` value). Keying by string lets this module load without
# importing the enum (and therefore without importing graphrag_toolkit). The
# seven keys are the seven non-deprecated graphrag-toolkit retrievers exposed by
# ``coa_serve.lexical.strategies.RetrieverStrategy``.
_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "chunk_based": (
        "Chunk-based retrieval: ranks document chunks by vector similarity to "
        "the query and returns the most relevant chunk text. Best for direct "
        "fact lookup where the answer is stated verbatim in a document."
    ),
    "chunk_based_semantic": (
        "Semantic chunk retrieval: expands chunk vector-similarity hits along "
        "semantically related chunks before ranking. Best general-purpose "
        "strategy for natural-language questions over document text."
    ),
    "entity_based": (
        "Entity-anchored retrieval: locates the entities named in the query and "
        "returns the chunks and entities directly associated with them. Best for "
        "anchoring a question on a specific named entity (e.g. a company)."
    ),
    "entity_context": (
        "Entity-context retrieval: returns an entity together with its "
        "surrounding contextual statements and chunks. Best for gathering "
        "background about a single entity."
    ),
    "entity_network": (
        "Entity-network retrieval: returns an entity together with its connected "
        "entities and the relationships among them. Best for questions about how "
        "entities relate to one another."
    ),
    "topic_based": (
        "Topic-based retrieval: groups and returns chunks by the topics they "
        "discuss. Best for broad, thematic questions spanning many documents."
    ),
    "traversal": (
        "Weighted traversal retrieval: a composite of chunk, entity-network, and "
        "topic retrievers combined by weight. Best as a broad multi-signal sweep "
        "when no single strategy clearly fits."
    ),
    "topic_beam": (
        "Topic-beam retrieval (TopicGraphRAG): seeds from topic vector similarity, "
        "then beam-searches the topic graph following chunk co-occurrence to gather "
        "the statements under the most relevant topics. Best for synthesis questions "
        "that span multiple sections or documents of one filing."
    ),
}

# The uniform input/output contracts every Retrieval_Strategy_Tool declares
# (Req 11.1). A strategy tool takes the natural-language sub-question text and
# returns native chunks + entities (passed through to synthesis) plus their
# normalized dict form.
_INPUT_SCHEMA: dict = {"query": "str"}
_OUTPUT_SCHEMA: dict = {"chunks": "list", "entities": "list", "items": "list"}


def _chunk_item(chunk: Any) -> dict:
    """Normalize a retrieved chunk to a plain dict for ``ToolResult.items``."""
    return {
        "type": "chunk",
        "chunk_id": getattr(chunk, "chunk_id", "") or "",
        "text": getattr(chunk, "text", "") or "",
        "source_doc": getattr(chunk, "source_doc", "") or "",
    }


def _entity_item(entity: Any) -> dict:
    """Normalize a retrieved entity to a plain dict for ``ToolResult.items``."""
    return {
        "type": "entity",
        "uri": getattr(entity, "uri", "") or "",
        "label": getattr(entity, "label", "") or "",
        "entity_type": getattr(entity, "type", "") or "",
    }


def _degradation(result: BaselineResult) -> tuple[str, str] | None:
    """Return machine-readable reason and detail for an adapter degradation.

    The adapter records a ``lexical_baseline_retrieve`` trace step with
    ``status="error"`` when it times out or the toolkit raises and
    ``status="degraded"`` when malformed anonymous chunks were dropped. Both
    must survive the Tool boundary so the controller marks the final response
    partial rather than treating the invocation as fully successful.
    """
    for step in getattr(result, "trace_steps", ()) or ():
        if not isinstance(step, dict) or step.get("status") not in {"error", "degraded"}:
            continue
        detail = str(step.get("detail") or "retrieval degraded")
        if step.get("status") == "degraded" and getattr(result, "dropped_anonymous_chunks", 0):
            return "missing_document_provenance", detail
        if "TimeoutError" in detail:
            return "timeout", detail
        return "error", detail
    return None


class RetrievalStrategyTool:
    """Wraps one graphrag ``RetrieverStrategy`` via the lexical adapter (Req 2.2).

    The four uniform interface fields (``name``, ``description``,
    ``input_schema``, ``output_schema``) are set at construction from static,
    graphrag-free data so the Tool can be registered and described to the planner
    without importing the toolkit. The strategy's ``StrategySpec`` is resolved
    lazily inside :meth:`invoke`.
    """

    def __init__(self, strategy: RetrieverStrategy, lexical: BaselineLexicalRetriever) -> None:
        """Build a Tool for ``strategy`` backed by the ``lexical`` adapter.

        Args:
            strategy: The :class:`RetrieverStrategy` enum member this Tool runs.
                Only ``strategy.value`` is read at construction (for the name and
                description); the member itself is stored for the lazy
                ``STRATEGY_REGISTRY`` lookup in :meth:`invoke`.
            lexical: The shared ``BaselineLexicalRetriever`` adapter that performs
                the retrieval (already namespace/tenant aware, self-timing, and
                non-raising).
        """
        value = strategy.value
        self.name = f"strategy:{value}"
        self.description = _STRATEGY_DESCRIPTIONS.get(
            value,
            f"graphrag-toolkit '{value}' retrieval strategy over the knowledge graph.",
        )
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._strategy = strategy
        self._lexical = lexical

    async def invoke(self, *, namespace: str, query: str = "", **_: Any) -> ToolResult:
        """Run the wrapped strategy for ``query`` scoped to ``namespace``.

        Resolves the strategy's ``StrategySpec`` from the registry (lazy import,
        Req 12.3) and delegates to ``BaselineLexicalRetriever.retrieve``. The
        adapter degrades to an empty result with an error trace step on
        timeout/error and never raises; this method mirrors that contract — it
        never raises for a retrieval failure, returning a (possibly empty)
        :class:`ToolResult` whose ``detail`` records any degradation
        (Req 9.1-9.2).

        Args:
            namespace: The originating request namespace; every retrieval is
                scoped to it (Req 1.5).
            query: The natural-language sub-question text to retrieve for.

        Returns:
            A :class:`ToolResult` carrying the native ``chunks`` / ``entities``
            (passed through to synthesis), their normalized dict ``items``, and a
            human-readable ``detail`` for the trace.
        """
        # Lazy import — keeps graphrag_toolkit (pulled in transitively by the
        # lexical package) out of module-load (Req 12.3).
        from ....lexical.strategies import STRATEGY_REGISTRY

        spec = STRATEGY_REGISTRY[self._strategy]

        try:
            result = await self._lexical.retrieve(query, namespace, strategy=spec)
        except Exception as exc:  # noqa: BLE001 - defensive: never raise out of a Tool
            # The adapter is documented not to raise, but a Tool must never
            # propagate a retrieval failure to the reasoning loop (Req 9.1).
            logger.warning(
                "strategy_tool_invoke_error",
                tool=self.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ToolResult(
                detail=f"{self.name} error: {type(exc).__name__}",
                degraded_reason="error",
            )

        chunks = tuple(result.chunks)
        entities = tuple(result.entities)
        items = tuple(_chunk_item(c) for c in chunks) + tuple(_entity_item(e) for e in entities)

        degradation = _degradation(result)
        if degradation is not None:
            degraded_reason, degraded_detail = degradation
            detail = f"{self.name} degraded: {degraded_detail}"
        else:
            degraded_reason = ""
            detail = f"{self.name}: {len(chunks)} chunks, {len(entities)} entities"

        return ToolResult(
            chunks=chunks,
            entities=entities,
            items=items,
            detail=detail,
            degraded_reason=degraded_reason,
        )


def build_strategy_tools(
    registry: ToolRegistry,
    lexical: BaselineLexicalRetriever,
) -> list[str]:
    """Register one Retrieval_Strategy_Tool per exposed ``RetrieverStrategy`` (Req 2.2).

    Enumerates the :class:`RetrieverStrategy` enum (lazy import, Req 12.3) and
    registers one :class:`RetrievalStrategyTool` per member into ``registry``,
    each named ``strategy:<value>``.

    ``topic_beam`` (TopicGraphRAG) is a first-class member of this set. It was
    gated behind an A/B flag while its accuracy contribution was measured on
    SEC10Q; that measurement is done and it now ships as a permanent tool. It
    still requires a populated ``topic`` vector index (present on any graph
    ingested after the kg-build ``EMBEDDING_INDEXES`` change) — on an older graph
    the tool seeds empty and returns nothing, which the controller tolerates like
    any other empty result.

    Args:
        registry: The :class:`ToolRegistry` to register the tools into.
        lexical: The shared ``BaselineLexicalRetriever`` adapter the tools wrap.

    Returns:
        The list of registered Tool names, in enum order.
    """
    # Lazy import — enumerating the enum pulls in the lexical package (and thus
    # graphrag_toolkit), so it must not happen at module load (Req 12.3).
    from ....lexical.strategies import RetrieverStrategy

    registered: list[str] = []
    for strategy in RetrieverStrategy:
        tool = RetrievalStrategyTool(strategy, lexical)
        registry.register(tool)
        registered.append(tool.name)

    logger.info(
        "strategy_tools_registered",
        count=len(registered),
        names=registered,
    )
    return registered
