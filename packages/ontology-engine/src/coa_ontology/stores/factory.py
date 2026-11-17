# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Backend factory.

Construct a ``(GraphStore, VectorStore)`` pair based on the
``WORKBENCH_BACKEND`` environment variable.

Valid values:
  - ``opensearch_neptune`` (default) — OpenSearch Serverless vector +
    Neptune DB graph.
  - ``na_only``                      — Neptune Analytics for both.

Callers should prefer :func:`build_stores` and accept whatever they get;
if a caller genuinely needs a specific backend it should not go through
this module.
"""

from __future__ import annotations

import logging
import os

from coa_ontology.stores.base import GraphStore, VectorStore

log = logging.getLogger(__name__)

DEFAULT_BACKEND = "opensearch_neptune"

_store_cache: dict[str | None, tuple[GraphStore, VectorStore]] = {}


def _selected_backend() -> str:
    return os.getenv("WORKBENCH_BACKEND", DEFAULT_BACKEND).strip().lower()


def build_stores(namespace: str | None = None) -> tuple[GraphStore, VectorStore]:
    """Construct the configured backend pair. Cached per namespace."""
    if namespace in _store_cache:
        return _store_cache[namespace]

    backend = _selected_backend()
    log.info("workbench backend = %s", backend)

    pair: tuple[GraphStore, VectorStore]
    if backend == "na_only":
        from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore
        from coa_ontology.stores.na_vector import NeptuneAnalyticsVectorStore

        pair = (
            NeptuneAnalyticsGraphStore(namespace=namespace),
            NeptuneAnalyticsVectorStore(namespace=namespace),
        )

    elif backend == "opensearch_neptune":
        from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore
        from coa_ontology.stores.opensearch_vector import OpenSearchVectorStore

        pair = (NeptuneDBGraphStore(namespace=namespace), OpenSearchVectorStore(namespace=namespace))

    else:
        raise ValueError(f"Unknown WORKBENCH_BACKEND {backend!r}. Expected one of: opensearch_neptune, na_only.")

    _store_cache[namespace] = pair
    return pair
