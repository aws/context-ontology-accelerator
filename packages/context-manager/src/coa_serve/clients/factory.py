# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client factory — constructs the default backend set.

Callers should use `build_clients()` and accept a `ClientSet`.
The resolution pipeline never imports concrete implementations directly.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog

from .base import ClientSet

if TYPE_CHECKING:
    from ..config import ServiceConfig
    from ..lexical.baseline_retriever import BaselineLexicalRetriever

logger = structlog.get_logger(__name__)


_NA_GRAPH_ID_RE = re.compile(r"g-[a-z0-9]{10,}")


def _extract_graph_id(endpoint: str) -> str | None:
    """Extract the Neptune Analytics graph-id (g-…) from an endpoint string.

    Recognises:
      - A bare graph-id: "g-abc1234567"
      - A neptune-graph:// URI: "neptune-graph://g-abc1234567"
      - An NA host: "g-abc1234567.us-east-1.neptune-graph.amazonaws.com"
    """
    m = _NA_GRAPH_ID_RE.search(endpoint)
    return m.group(0) if m else None


def _is_neptune_analytics(endpoint: str) -> bool:
    """Return True if *endpoint* refers to a Neptune Analytics graph.

    The service-host test is anchored to the end of the host so a value such as
    ``neptune-graph.amazonaws.com.other.test`` is not treated as Neptune
    Analytics. This picks which client to construct, not whether to trust the
    endpoint, but keeping the match host-anchored avoids routing traffic to the
    wrong backend on a malformed config value.
    """
    if endpoint.startswith("neptune-graph://"):
        return True
    try:
        host = (urlsplit(endpoint if "://" in endpoint else f"//{endpoint}").hostname or "").lower()
    except ValueError:
        # ``hostname`` raises on an unparseable netloc (stray brackets, or
        # characters that fail NFKC normalization). Such a value cannot be a real
        # NA host, so fall through to the graph-id heuristic rather than failing
        # client construction at startup.
        host = ""
    if host == "neptune-graph.amazonaws.com" or host.endswith(".neptune-graph.amazonaws.com"):
        return True
    # A bare or embedded graph-id (g-…) with no NDB port indicator
    return bool(_extract_graph_id(endpoint) and ":8182" not in endpoint)


def _graph_store_uri(neptune_endpoint: str) -> str:
    """Derive the graphrag-toolkit graph-store URI from the deployment endpoint.

    Neptune Analytics → ``neptune-graph://g-…``
    Neptune DB (default) → ``neptune-db://host:8182``
    """
    if _is_neptune_analytics(neptune_endpoint):
        graph_id = _extract_graph_id(neptune_endpoint)
        if graph_id:
            return f"neptune-graph://{graph_id}"
        # Fallback: strip protocol prefix and use as-is
        cleaned = neptune_endpoint.removeprefix("neptune-graph://")
        return f"neptune-graph://{cleaned}"

    # NDB: strip protocol prefixes, extract host, append port
    host = neptune_endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
    host = host.split(":")[0]
    return f"neptune-db://{host}:8182"


def build_clients() -> ClientSet:
    """Construct the client set for the serve component.

    Must be called outside the async event loop (e.g., during app startup).
    """
    logger.info("building_clients")
    return _build_default_clients()


def _build_default_clients() -> ClientSet:
    from .bedrock import BedrockLLMClient
    from .neptune import NeptuneGraphClient
    from .opensearch import OpenSearchVectorClient
    from .source_db import SourceDBQueryExecutor

    return ClientSet(
        graph=NeptuneGraphClient(),
        vector=OpenSearchVectorClient(),
        llm=BedrockLLMClient(),
        # Executes authorized SQL directly against customer relational databases.
        query_executor=SourceDBQueryExecutor(),
    )


def build_lexical_retriever(
    config: ServiceConfig, *, timeout_s: float | None = None
) -> BaselineLexicalRetriever | None:
    """Build the lexical baseline retriever (graphrag-toolkit adapter).

    Built on every deployment so a per-request ``options.retrieverStrategy`` can
    trigger graphrag retrieval on-demand, regardless of ``TIER3_STRATEGY``.
    Construction is cheap and keeps the graphrag import lazy.

    ``timeout_s`` overrides the per-retrieval timeout (the deep-reasoning path
    passes its per-tool budget). When None, it falls back to the
    ``LEXICAL_RETRIEVER_TIMEOUT_S`` env var, default 45s: the retriever's own 15s
    default is too low for graph-traversal strategies.

    Returns None only when the required store endpoints are unset.
    """
    if not config.neptune_endpoint or not config.opensearch_endpoint:
        return None

    if timeout_s is None:
        timeout_s = 45.0  # standard-mode default (see docstring); env var overrides below
        if env_timeout := os.environ.get("LEXICAL_RETRIEVER_TIMEOUT_S", "").strip():
            try:
                timeout_s = float(env_timeout)
            except ValueError:
                # Keep the 45s default rather than falling through to the
                # retriever's own 15s (the value this fallback exists to avoid).
                logger.warning(
                    "invalid_lexical_retriever_timeout",
                    value=env_timeout,
                    fallback_timeout_s=timeout_s,
                )

    from ..lexical import BaselineLexicalRetriever
    from ..lexical.strategies import DEFAULT_SPEC

    graph_uri = _graph_store_uri(config.neptune_endpoint)
    vector_store_uri = f"aoss://{config.opensearch_endpoint}"

    logger.info(
        "building_lexical_retriever",
        tier3_strategy=config.tier3_strategy,
        graph_store_uri=graph_uri,
        vector_store_uri=vector_store_uri,
    )

    # The instance-default StrategySpec is only a fallback; the orchestrator
    # passes the resolved per-request strategy into ``retrieve()`` whenever the
    # lexical path is engaged.
    return BaselineLexicalRetriever(
        graph_store_uri=graph_uri,
        vector_store_uri=vector_store_uri,
        strategy=DEFAULT_SPEC,
        timeout_s=timeout_s,
    )
