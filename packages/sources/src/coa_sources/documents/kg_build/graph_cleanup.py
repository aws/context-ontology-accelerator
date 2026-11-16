# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion Pipeline — Stage 2: Graph Cleanup ECS Task.

Invoked by the deletion Step Functions state machine via EcsRunTask.
Removes the document subgraph and vector embeddings for a specific doc source
from Neptune and OpenSearch.

Uses DeleteSources directly with a small batch_size to avoid Neptune's
per-query memory limit. The default batch_size=1000 causes MemoryLimitExceeded
on large documents because delete_statements() passes all 1000 IDs in one query.

Opens the vector store over the SAME ``EMBEDDING_INDEXES`` the build task writes
(imported rather than re-derived). The toolkit's default index set includes
``statement``, which the build path does not create, and deletion would wait out
a 70-second timeout per batch for documents that were never indexed.

Applies the same graphrag-toolkit monkey-patches as the build task, because
deletion reads the vector indexes and hits the same AOSS failure modes. They must
run BEFORE graphrag is imported (``LexicalGraphIndex``/``VectorStoreFactory`` bind
their own references at import time), so the graphrag imports live inside main().
"""

from __future__ import annotations

import os

import structlog
from coa_common import resolve_region
from coa_common.logging import setup_logging
from coa_common.opensearch import AossVectorClient

from .graph_build import (
    EMBEDDING_INDEXES,
    _patch_graphrag_bulk_ingest_retry,
    _patch_graphrag_paginated_search_retry,
    _patch_graphrag_toolkit_for_aoss_nextgen,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

NAMESPACE_ID: str = os.environ.get("NAMESPACE_ID", "")
DOC_SOURCE_ID: str = os.environ.get("DOC_SOURCE_ID", "")
TENANT_ID: str = os.environ.get("TENANT_ID", "")
NEPTUNE_ENDPOINT: str = os.environ.get("NEPTUNE_ENDPOINT", "")
OPENSEARCH_ENDPOINT: str = os.environ.get("OPENSEARCH_ENDPOINT", "")

_BATCH_SIZE = 200


def _existing_embedding_indexes(prefixes: list[str], tenant_id: str) -> list[str]:
    """Return only the ``prefixes`` whose AOSS index actually exists.

    Deletion must not be handed a missing index name: graphrag's
    ``delete_embeddings`` sleeps in a 70-second retry loop per batch when it finds
    no documents. A missing index is a normal condition, since namespace teardown
    drops ``chunk_*``/``topic_*`` once the last source is deleted.

    Best-effort: if the existence probe itself fails the prefix is kept, so a
    transient AOSS fault does not skip a delete that was needed.
    """
    if not prefixes:
        return []

    client = AossVectorClient(endpoint=OPENSEARCH_ENDPOINT, region=resolve_region()).raw_client()
    kept: list[str] = []
    for prefix in prefixes:
        index_name = f"{prefix}_{tenant_id}"
        try:
            if client.indices.exists(index=index_name):
                kept.append(prefix)
            else:
                logger.info("embedding_index_absent_skipping", index=index_name)
        except Exception:  # noqa: BLE001 — probe only; keep the prefix on failure
            logger.warning("embedding_index_probe_failed_keeping", index=index_name, exc_info=True)
            kept.append(prefix)
    return kept


def main() -> None:
    """Run the graph-cleanup task (ECS Fargate entrypoint).

    Reads its inputs from environment variables (namespace, doc source, tenant,
    and the Neptune/OpenSearch endpoints), locates every graph source for the
    doc source, and deletes their subgraph and vector embeddings in small
    batches to stay under Neptune's per-query memory limit.

    Raises:
        RuntimeError: If any required environment variable is missing.
    """
    missing = [
        name
        for name, val in [
            ("NAMESPACE_ID", NAMESPACE_ID),
            ("DOC_SOURCE_ID", DOC_SOURCE_ID),
            ("TENANT_ID", TENANT_ID),
            ("NEPTUNE_ENDPOINT", NEPTUNE_ENDPOINT),
            ("OPENSEARCH_ENDPOINT", OPENSEARCH_ENDPOINT),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    # Patch BEFORE importing graphrag (see module docstring). allow_create=False
    # because this is the deletion path and must never create an index.
    _patch_graphrag_toolkit_for_aoss_nextgen(allow_create=False)
    _patch_graphrag_paginated_search_retry()
    _patch_graphrag_bulk_ingest_retry()

    from graphrag_toolkit.lexical_graph import LexicalGraphIndex
    from graphrag_toolkit.lexical_graph.indexing.build.delete_sources import DeleteSources
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory

    neptune_host = NEPTUNE_ENDPOINT.replace("wss://", "").replace("https://", "").rstrip("/")
    opensearch_host = OPENSEARCH_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    graph_store_uri = f"neptune-db://{neptune_host}:8182"
    vector_store_uri = f"aoss://{opensearch_host}:443"

    logger.info(
        "graph_cleanup_started",
        namespace_id=NAMESPACE_ID,
        doc_source_id=DOC_SOURCE_ID,
        tenant_id=TENANT_ID,
    )

    # Hand graphrag ONLY the indexes that still exist. A name that is already
    # gone would cost 70s of futile retry per batch (see
    # _existing_embedding_indexes), and namespace teardown legitimately drops
    # these while this task may still be running.
    existing_indexes = _existing_embedding_indexes(EMBEDDING_INDEXES, TENANT_ID)
    if existing_indexes:
        vector_index_names = existing_indexes
    else:
        # No AOSS indexes remain, so there are no embeddings to delete. But
        # VectorStoreFactory.for_vector_store raises "Unrecognized vector store info"
        # for an EMPTY index list under any scheme, so point at the dummy store with
        # a NON-EMPTY set of names (irrelevant to a dummy store; default to the
        # standard doc-KG set). The Neptune subgraph deletion still proceeds.
        logger.info(
            "no_embedding_indexes_present_skipping_vector_deletes",
            tenant_id=TENANT_ID,
            configured=EMBEDDING_INDEXES,
        )
        vector_store_uri = "dummy://"
        vector_index_names = EMBEDDING_INDEXES or ["chunk", "topic"]

    with (
        GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store,
        VectorStoreFactory.for_vector_store(vector_store_uri, index_names=vector_index_names) as vector_store,
    ):
        graph_index = LexicalGraphIndex(graph_store, vector_store, tenant_id=TENANT_ID)

        # Find source IDs for this doc source
        sources = graph_index.get_sources(filter={"doc_source_id": DOC_SOURCE_ID})
        if not sources:
            logger.info("no_sources_found", doc_source_id=DOC_SOURCE_ID)
        else:
            source_ids = [s["sourceId"] for s in sources]
            logger.info("sources_found", count=len(source_ids), doc_source_id=DOC_SOURCE_ID)

            # Use num_workers=1 to serialize deletions — parallel workers cause
            # ConcurrentModificationException in Neptune when multiple transactions
            # modify the same nodes simultaneously.
            delete_sources = DeleteSources(
                graph_store=graph_index.graph_store,
                vector_store=graph_index.vector_store,
                batch_size=_BATCH_SIZE,
                num_workers=1,
            )
            deleted = delete_sources.delete_source_documents(source_ids)
            logger.info(
                "neptune_cleanup_complete",
                doc_source_id=DOC_SOURCE_ID,
                deleted_count=len(deleted),
            )

    logger.info(
        "graph_cleanup_complete",
        namespace_id=NAMESPACE_ID,
        doc_source_id=DOC_SOURCE_ID,
    )


if __name__ == "__main__":
    main()
