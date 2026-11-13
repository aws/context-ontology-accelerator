# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Storage abstractions for the Ontology Workbench.

Two pluggable backends:

  - ``na_only``         (Neptune Analytics for BOTH graph and vectors)
  - ``opensearch_neptune`` (OpenSearch Serverless for vectors, Neptune DB
    for the graph). **This is the default.**

Select via the ``WORKBENCH_BACKEND`` environment variable. See
``app.stores.factory`` for construction.

The public surface is two Protocols — :class:`GraphStore` and
:class:`VectorStore` — plus :func:`build_stores` that returns a
``(GraphStore, VectorStore)`` pair.
"""

from coa_ontology.stores.base import EmbeddingHit, GraphStore, VectorStore
from coa_ontology.stores.factory import build_stores

__all__ = [
    "GraphStore",
    "VectorStore",
    "EmbeddingHit",
    "build_stores",
]
