# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared OpenSearch Serverless (AOSS) vector client for the Context Ontology Accelerator.

Every subsystem that reads or writes vector embeddings — ontology construction
(induction + grounding), metric-service, and the serve NL→SQL / lexical
retrieval tiers — talks to the SAME AOSS collection. This package is the single
place that knows how to do that correctly, so the AOSS quirks and the k-NN
engine contract are learned once, not re-discovered per package.

Engine & filtering contract
---------------------------
The collection is a NEXTGEN ``VECTORSEARCH`` collection. Filtered k-NN requires
an ANN engine that supports filters — **Faiss** (HNSW) or Lucene do; **NMSLIB
does not** (``Engine [NMSLIB] does not support filters``). Which engine a
method-less ``knn_vector`` field resolves to is a SERVER-SIDE default that has
NOT been stable: it has been observed to resolve to NMSLIB in one account/date
and to Faiss in another, and an explicit ``method.engine`` block has been both
accepted and rejected at index creation across those same accounts (see #174).

We therefore do NOT assume the default. :func:`build_index_mapping` requests an
explicit Faiss/HNSW ``method`` by default; :meth:`AossVectorClient.ensure_index`
tries that, falls back to a method-less mapping if the service rejects the
explicit block, and then READS BACK the resolved engine via ``get_mapping`` and
raises if it is not filter-capable — so an index that cannot serve a filtered
query is detected at creation, not at first query.

Because the resolved engine is verified to be filter-capable, **filtering is
done SERVER-SIDE inside the k-NN query** via ``knn.embedding.filter`` (a
``bool``/``filter`` of ``term`` / ``exists`` clauses on keyword fields). The
engine returns the true top-``k`` *within* the filtered subset in one
round-trip — there is no need to over-fetch and post-filter in Python. All
filterable fields (``entity_type``, ``ontology_id``, ``data_source_id`` …) are
mapped as ``keyword`` so ``term`` / ``exists`` match exactly.

AOSS quirks this client encapsulates
------------------------------------
- **No client-supplied ``_id``** on index/bulk (AOSS auto-assigns). Upserts
  therefore delete-then-insert rather than overwrite by id.
- **No ``_delete_by_query``** on VECTORSEARCH — delete by searching for matching
  docs and deleting each by its auto-assigned ``_id``.
- **No scroll API** — paginate with ``search_after`` on a stable sort.
- **Root ``/`` returns 404** — ``client.info()`` is unusable as a liveness
  probe; probe by ensuring an index exists instead.
- **Engine not assumed** — a method-less ``knn_vector`` can resolve to a
  non-filtering engine (NMSLIB); ``ensure_index`` requests explicit Faiss/HNSW,
  falls back to method-less on rejection, then verifies the resolved engine is
  filter-capable via ``get_mapping`` (see the engine contract above and #174).
- **Transient 429/5xx** under OCU pressure / node faults — every op is wrapped
  in capped exponential backoff (:func:`oss_retry`), and bulk writes re-submit
  only the transiently-failed per-item docs and RAISE :class:`PartialIndexError`
  if any doc fails to land after retries (:func:`bulk_with_retry`) — a partial
  write is never reported as success (see #173).

Auth
----
SigV4 for the ``aoss`` service (NOT ``es``), using ``AWSV4SignerAuth`` with the
**live** boto3 credentials object — it re-signs each request with the current
auto-refreshed credentials, so a long-lived cached client survives Fargate
task-role token rotation (a frozen-snapshot signer 403s after ~6–12h).

Transport
---------
Direct AOSS by default. The serve path (AgentCore containers that cannot reach
AOSS directly) injects a search-only proxy-Lambda transport — see
``AossVectorClient(transport=...)``.
"""

from __future__ import annotations

from coa_common.opensearch.client import (
    AossVectorClient,
    IncompatibleEngineError,
    build_index_mapping,
)
from coa_common.opensearch.retry import (
    OSS_MAX_BACKOFF_S,
    OSS_MAX_RETRIES,
    OSS_RETRY_STATUS,
    PartialIndexError,
    bulk_with_retry,
    is_transient,
    oss_retry,
    retry_call,
)

__all__ = [
    "AossVectorClient",
    "IncompatibleEngineError",
    "build_index_mapping",
    "OSS_RETRY_STATUS",
    "OSS_MAX_RETRIES",
    "OSS_MAX_BACKOFF_S",
    "PartialIndexError",
    "is_transient",
    "oss_retry",
    "retry_call",
    "bulk_with_retry",
]
