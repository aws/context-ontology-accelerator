# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenSearch Serverless client — VectorClient implementation.

Thin async wrapper over the shared
:class:`coa_common.opensearch.AossVectorClient`. This module owns
only the serve-specific concerns:

  - async surface (the shared client is sync — calls run in a thread executor);
  - the proxy-Lambda transport for AgentCore containers that cannot reach AOSS
    directly (see aoss-troubleshooting.md);
  - ``VectorHit`` shaping and the ``_SOURCE_FIELDS`` projection;
  - hybrid BM25 + k-NN search;
  - the serve health-check contract.

Server-side filtering (Faiss/HNSW ``knn.embedding.filter``), SigV4 auth,
transient-fault retry, and the canonical mapping live in the shared library —
see its package docstring.

TODO(aoss-proxy): When AgentCore natively supports AOSS, remove the proxy path
by unsetting OPENSEARCH_PROXY_LAMBDA_ARN.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import structlog
from coa_common import AossVectorClient, resolve_region, sync_boto_config

from .base import VectorClient, VectorHit

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opensearch")


_SOURCE_FIELDS = [
    "text",
    "context_text",
    "value",
    "source_doc",
    "chunk_index",
    "namespace",
    "uri",
    "type",
    "label",
    "description",
    "entity_type",
    "entity_uri",
    "data_source_id",
    # GraphRAG chunk indexes (``chunk_{tenant}``) carry the document identity
    # nested inside the toolkit's node metadata, not as a flat ``source_doc``
    # key. Project only the identity fields — pulling ``metadata`` whole would
    # drag the serialized ``_node_content`` node (chunk text duplicated as
    # JSON) into every hit.
    "metadata.source.sourceId",
    "metadata.source.metadata.source_s3_key",
    "metadata.source.metadata.filename",
]


def _source_document_id(source: dict) -> str:
    """Resolve the unique document identity of a GraphRAG chunk hit (#985).

    A flat ``source_doc`` field wins when present. GraphRAG chunk documents
    written by the ingest pipeline have no such field; their unique identity is
    the toolkit's ``sourceId`` at ``metadata.source.sourceId``. Path-shaped
    metadata (``source_s3_key``, ``filename``) is deliberately NOT used as the
    identity: it is not unique — two document sources can hold the same
    ``reports/annual.pdf`` key in different buckets — so it would fuse distinct
    documents under one id. The human-readable name is a separate field (see
    :func:`_source_document_name`), matching the API's
    ``sourceDocumentId`` / ``sourceDocumentName`` split.
    """
    flat = source.get("source_doc", "")
    if isinstance(flat, str) and flat:
        return flat
    nested_source = _nested_source(source)
    source_id = nested_source.get("sourceId")
    return source_id if isinstance(source_id, str) else ""


def _source_document_name(source: dict) -> str:
    """Resolve the human-readable document name of a chunk hit (#985).

    The name users recognize is the UPLOADED document's — the basename of the
    raw ``source_s3_key``. The ingest ``filename`` is the STAGING artifact's
    name and changes extension when preprocessing converts the document
    (``report.pdf`` is staged as ``report.md``), so it is only the fallback.
    A display name, not an identity — distinct documents may share it.
    """
    nested_metadata = _nested_source(source).get("metadata")
    if not isinstance(nested_metadata, dict):
        return ""
    s3_key = nested_metadata.get("source_s3_key")
    if isinstance(s3_key, str) and s3_key:
        return s3_key.rsplit("/", 1)[-1]
    filename = nested_metadata.get("filename")
    return filename if isinstance(filename, str) else ""


def _nested_source(source: dict) -> dict:
    """The toolkit's nested ``metadata.source`` dict, or ``{}`` off-shape."""
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        nested_source = metadata.get("source")
        if isinstance(nested_source, dict):
            return nested_source
    return {}


class OpenSearchVectorClient(VectorClient):
    """OpenSearch Serverless k-NN + hybrid search client (async)."""

    def __init__(
        self,
        endpoint: str | None = None,
        region: str | None = None,
        index: str = "document-chunks",
        timeout: int = 60,
    ):
        """Configure the AOSS endpoint, region, index, and optional search proxy.

        Args:
            endpoint: AOSS collection host. Defaults to the ``OPENSEARCH_ENDPOINT`` env var.
            region: AWS region for SigV4 signing. Defaults to the resolved region.
            index: Default index name for searches and counts.
            timeout: Request timeout in seconds.

        Raises:
            ValueError: If no endpoint is provided via arg or env var.
        """
        self._endpoint = endpoint or os.environ.get("OPENSEARCH_ENDPOINT", "")
        self._region = region or resolve_region()
        self._index = index
        self._timeout = timeout
        if not self._endpoint:
            raise ValueError("OpenSearch endpoint must be provided via constructor arg or OPENSEARCH_ENDPOINT env var")
        # TODO(aoss-proxy): Remove proxy fields when AgentCore supports AOSS directly
        self._proxy_lambda_arn = os.environ.get("OPENSEARCH_PROXY_LAMBDA_ARN", "")
        self._lambda_client: Any = None
        if self._proxy_lambda_arn:
            self._lambda_client = boto3.client("lambda", region_name=self._region, config=sync_boto_config())
        # Shared AOSS client. When a proxy is configured it becomes search-only
        # (the transport routes every search through the Lambda); otherwise it's
        # a direct SigV4 client. The store dimension is irrelevant for reads.
        self._aoss = AossVectorClient(
            endpoint=self._endpoint,
            region=self._region,
            timeout=self._timeout,
            transport=self._proxy_transport if self._proxy_lambda_arn else None,
        )

    # ── proxy transport ─────────────────────────────────────────────────

    def _proxy_transport(self, action: str, index: str, body: dict) -> dict:
        """Synchronous search-proxy Lambda call (runs inside the thread executor).

        TODO(aoss-proxy): Remove when AgentCore supports AOSS directly.
        """
        payload = json.dumps({"action": action, "index": index, "body": body or {}})
        response = self._lambda_client.invoke(
            FunctionName=self._proxy_lambda_arn,
            InvocationType="RequestResponse",
            Payload=payload.encode(),
        )
        raw = response["Payload"].read()
        if response.get("FunctionError"):
            logger.error("lambda_proxy_function_error", raw=raw.decode())
            raise RuntimeError("Vector search proxy returned an error")
        response_payload = json.loads(raw)
        if response_payload.get("statusCode") != 200:
            logger.error(
                "lambda_proxy_error",
                status=response_payload.get("statusCode"),
                error=response_payload.get("error", "unknown"),
            )
            raise RuntimeError("Vector search proxy returned an error")
        return response_payload.get("body", {})

    async def _run(self, fn, *args, **kwargs):
        """Run a sync shared-client call in the thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))

    # ── searches ─────────────────────────────────────────────────────────

    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str = "",
        top_k: int = 5,
        index: str | None = None,
        entity_type: str | None = None,
        require_mapped: bool = False,
    ) -> list[VectorHit]:
        """k-NN vector search, optionally filtered by entity_type / mapped-only.

        Filters are pushed SERVER-SIDE into the k-NN ``filter`` clause (the AOSS
        collection uses Faiss/HNSW, which supports filtered k-NN), so the engine
        returns the true top-``k`` within the filtered subset in one round-trip.

        - ``entity_type`` → ``term`` on the ``entity_type`` keyword field.
        - ``require_mapped`` → ``exists`` on ``data_source_id`` (written only for
          R2RML-mapped, Tier-2-capable classes). Callers that must serve legacy
          (all-unmapped) namespaces gate this via :meth:`count_documents`.
        """
        filters: list[dict[str, Any]] = []
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        if require_mapped:
            filters.append({"exists": {"field": "data_source_id"}})
        raw = await self._run(
            self._aoss.knn_search,
            index or self._index,
            query_vector,
            top_k=top_k,
            filters=filters or None,
            source=_SOURCE_FIELDS,
        )
        return [self._to_hit(s) for s in raw]

    async def count_documents(
        self,
        *,
        index: str | None = None,
        require_mapped: bool = False,
        entity_type: str | None = None,
    ) -> int:
        """Count documents matching optional entity_type / mapped filters.

        Cheap gate for the NL->SQL legacy-namespace bridge: a namespace with at
        least one R2RML-mapped class (``count_documents(require_mapped=True) > 0``)
        uses the server-side mapped filter; a namespace with zero mapped records
        is pre-marker/legacy and must fall back to an unfiltered k-NN so its
        classes stay visible to Tier-2.
        """
        filters: list[dict[str, Any]] = []
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        if require_mapped:
            filters.append({"exists": {"field": "data_source_id"}})
        return await self._run(self._aoss.count, index or self._index, filters=filters or None)

    async def hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        *,
        namespace: str = "",
        top_k: int = 5,
        index: str | None = None,
    ) -> list[VectorHit]:
        """Hybrid BM25 + k-NN search."""
        body: dict[str, Any] = {
            "size": top_k,
            "query": {
                "bool": {
                    "should": [
                        {"match": {"text": {"query": query_text, "boost": 0.3}}},
                        {"knn": {"embedding": {"vector": query_vector, "k": top_k}}},
                    ]
                }
            },
            "_source": _SOURCE_FIELDS,
        }
        results = await self._run(self._aoss.search_raw, index or self._index, body)
        return self._parse_hits(results)

    # ── hit shaping ──────────────────────────────────────────────────────

    def _to_hit(self, source: dict) -> VectorHit:
        """Shape a shared-client hit dict (``_source`` + ``_score``/``_id``) into a VectorHit."""
        return VectorHit(
            id=source.get("_id", "") or "",
            text=source.get("text", "") or source.get("value", ""),
            score=source.get("_score") or 0.0,
            metadata={
                "source_doc": _source_document_id(source),
                "source_doc_name": _source_document_name(source),
                "chunk_index": source.get("chunk_index"),
                "namespace": source.get("namespace", ""),
                "uri": source.get("uri", source.get("entity_uri", "")),
                "type": source.get("type", source.get("entity_type", "")),
                "label": source.get("label", source.get("text", "")),
                "description": source.get("description", ""),
                "entity_type": source.get("entity_type", ""),
                "entity_uri": source.get("entity_uri", ""),
                "data_source_id": source.get("data_source_id", ""),
                # Richer schema context (e.g. column allowed values) for the
                # NL→SQL generation prompt; absent for entities without it.
                "context_text": source.get("context_text", ""),
            },
        )

    def _parse_hits(self, results: dict) -> list[VectorHit]:
        """Parse a raw OpenSearch response into VectorHit objects.

        Hits with no ``_source`` still yield a VectorHit carrying only the id and
        score, preserving prior behaviour.
        """
        hits = []
        for hit in results.get("hits", {}).get("hits", []):
            if "_source" not in hit:
                # Preserve prior behaviour: a hit with no _source still yields a
                # VectorHit with empty fields (id/score retained).
                hits.append(VectorHit(id=hit.get("_id", ""), text="", score=hit.get("_score", 0.0) or 0.0))
                continue
            source = {**hit["_source"], "_id": hit.get("_id", ""), "_score": hit.get("_score", 0.0)}
            hits.append(self._to_hit(source))
        return hits

    async def health_check(self) -> dict[str, Any]:
        """Check AOSS connectivity (serve-specific ``healthy``/``unhealthy`` shape)."""
        try:
            result = await self._run(self._aoss.health_check, self._index)
            if result.get("status") == "ok":
                return {"status": "healthy", "via": "lambda_proxy" if self._proxy_lambda_arn else "direct"}
            return {"status": "unhealthy", "error": "connectivity check failed"}
        except Exception as e:  # noqa: BLE001 — health probe must not raise
            logger.warning("health_check_failed", error=str(e))
            return {"status": "unhealthy", "error": "connectivity check failed"}

    async def close(self) -> None:
        """No-op close kept for VectorClient interface compatibility."""
        # The shared client holds a lazily-built opensearch-py client; there's no
        # public close on it, and AOSS connections are short-lived HTTP — nothing
        # to tear down. Kept for VectorClient interface compatibility.
        return None
