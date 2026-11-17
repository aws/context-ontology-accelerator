# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve-side graphrag-toolkit patches: ``embedding`` in ``_source``, and LLM usage accounting.

Why this exists
---------------
``TopicBeamSearch`` expands the beam by fetching each neighbour topic's *stored*
embedding by id (``topic_beam_search.py:148`` -> ``OpenSearchIndex.get_embeddings``
-> ``get_all_embeddings`` -> ``paginated_search``). The toolkit's
``_to_get_embedding_result`` then reads ``source['embedding']``
(``opensearch_vector_indexes.py:643``) with a bare subscript.

On AOSS **NEXTGEN** collections the ``knn_vector`` field is NOT returned in the
default ``_source`` projection, so that subscript raises ``KeyError: 'embedding'``.
The exception escapes ``get_embeddings`` and is swallowed one level up by
``TopicBeamSearch._get_embeddings``' broad ``except Exception`` (logged only as
``Failed to fetch stored topic embeddings``). Every neighbour is then treated as
"no stored embedding" and skipped, so the beam collapses to its seed set and the
retriever returns ~1 node regardless of how good the seeds are.

The fix: explicitly project the fields ``_to_get_embedding_result`` consumes. This
is a read-path-only patch (serve never writes vectors), and it leaves the
``ids_only`` branch untouched since that path deliberately suppresses ``_source``.

Reported upstream; remove this module once the toolkit projects the vector field
itself. Applied lazily (never at import) to preserve the botocore sync-only
invariant documented in ``strategies.py``.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger(__name__)

# Fields ``_to_get_embedding_result`` reads: source['id'], source['value'],
# source['embedding'], source['metadata']['_node_content'].
_EMBEDDING_SOURCE_INCLUDES = ["id", "value", "embedding", "metadata"]

_patched = False


def patch_paginated_search_source() -> None:
    """Make ``paginated_search`` project ``embedding`` into ``_source``.

    Idempotent and failure-tolerant: if the toolkit's internals have moved, this
    logs and returns rather than breaking retrieval (the caller degrades to the
    pre-patch behavior, i.e. a narrow beam, not an error).
    """
    global _patched
    if _patched:
        return
    try:
        import graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes as ovi
        from opensearchpy.exceptions import NotFoundError
    except ImportError:
        logger.debug("graphrag_embedding_source_patch_skipped", reason="module not importable")
        return

    original = getattr(ovi.OpenSearchIndex, "paginated_search", None)
    if original is None:
        logger.warning("graphrag_embedding_source_patch_skipped", reason="paginated_search missing")
        return

    def paginated_search(self, query, page_size=10000, max_pages=None, ids_only=False):
        # ids_only deliberately sets "_source": False upstream; only widen the
        # projection for the embedding-fetch path.
        if ids_only:
            yield from original(self, query, page_size=page_size, max_pages=max_pages, ids_only=True)
            return

        client = self.client._os_client
        if client is None:
            return

        search_after = None
        page = 0
        while True:
            body = {
                "size": page_size,
                "query": query,
                "sort": [{"_id": "asc"}],
                # The one line this patch exists for: AOSS NEXTGEN omits
                # knn_vector from the default projection.
                "_source": {"includes": _EMBEDDING_SOURCE_INCLUDES},
            }
            if search_after:
                body["search_after"] = search_after

            # Preserve upstream's index-not-found retry (a namespace can be queried
            # before its topic index materializes); everything else propagates.
            not_found_retries = 0
            response = None
            while response is None:
                try:
                    response = client.search(index=self.underlying_index_name(), body=body)
                except NotFoundError:
                    not_found_retries += 1
                    if not_found_retries > 3:
                        raise
                    time.sleep(5)

            hits = response["hits"]["hits"]
            if not hits:
                break

            yield hits

            search_after = hits[-1]["sort"]
            page += 1
            if max_pages and page >= max_pages:
                break

    ovi.OpenSearchIndex.paginated_search = paginated_search
    _patched = True
    logger.info("graphrag_embedding_source_patch_applied", includes=_EMBEDDING_SOURCE_INCLUDES)


_llm_usage_patched = False
# Set on each wrapper so a re-application never wraps a wrapper (which would record
# every call twice) and so tests can restore the true original.
_ORIGINAL_ATTR = "__coa_llm_usage_original__"
# BedrockConverse entry points that bypass ``chat`` and whose usage this patch does
# not read. GraphRAG's keyword provider uses none of them (LLMCache.predict ->
# LLM.predict -> chat); a call through one is reported, never silently uncounted.
_UNACCOUNTED_LLM_METHODS = ("stream_chat", "achat", "astream_chat", "astream_complete", "acomplete")


def patch_llm_usage_accounting() -> None:
    """Record GraphRAG's LlamaIndex Bedrock LLM calls in the request's LLM usage tracker.

    GraphRAG's keyword provider calls its LLM through ``LLMCache.predict``, which
    returns only text. The usage lives one level down: ``BedrockConverse.chat``
    returns ``ChatResponse(raw=<Converse response>)`` carrying Bedrock's
    ``inputTokens``/``outputTokens``. Wrapping ``chat`` sits BELOW the cache, so a
    cache hit makes no provider call and correctly costs nothing.

    A provider call is attributed to the tracker bound on the calling thread (serve
    binds it in the lexical worker; see ``llm_usage.graphrag_retrieval``). A call
    seen with neither a bound tracker nor a deliberate-untracked marker while
    tracked GraphRAG retrieval is in flight marks those requests incomplete
    instead of being lost.

    Idempotent and failure-tolerant like :func:`patch_paginated_search_source`: if
    LlamaIndex has moved, this logs and returns, and :func:`llm_usage_patch_active`
    stays False so the lexical retriever reports tracked requests incomplete rather
    than letting GraphRAG's LLM spend go uncounted.
    """
    global _llm_usage_patched
    if _llm_usage_patched:
        return
    try:
        from llama_index.llms.bedrock_converse import BedrockConverse
    except ImportError:
        logger.warning("graphrag_llm_usage_patch_skipped", reason="module not importable")
        return

    from ..llm_usage import (
        REASON_PROVIDER_ERROR,
        REASON_UNSUPPORTED_CALL,
        current_tracker,
        is_untracked_graphrag_retrieval,
        note_unattributed_graphrag_call,
        record_usage,
    )

    def _original(name: str):
        method = getattr(BedrockConverse, name, None)
        return getattr(method, _ORIGINAL_ATTR, method)

    original_chat = _original("chat")
    if original_chat is None:
        logger.warning("graphrag_llm_usage_patch_skipped", reason="BedrockConverse.chat missing")
        return

    def _bound(original, instance):
        # LlamaIndex wraps these methods in its instrumentation dispatcher, a
        # descriptor that only binds correctly through ``__get__`` — calling the
        # class attribute with an explicit ``self`` drops it.
        return original.__get__(instance, type(instance))

    def chat(self, messages, **kwargs):
        tracker = current_tracker()
        if tracker is None:
            if not is_untracked_graphrag_retrieval():
                note_unattributed_graphrag_call()
            return _bound(original_chat, self)(messages, **kwargs)
        handle = tracker.register_call("graphrag_llm", str(getattr(self, "model", "")))
        try:
            response = _bound(original_chat, self)(messages, **kwargs)
        except BaseException:
            tracker.fail_call(handle, REASON_PROVIDER_ERROR)
            raise
        raw = getattr(response, "raw", None)
        record_usage(tracker, handle, raw.get("usage") if isinstance(raw, dict) else None)
        return response

    setattr(chat, _ORIGINAL_ATTR, original_chat)
    BedrockConverse.chat = chat

    def _unaccounted(name: str, original):
        def method(self, *args, **kwargs):
            tracker = current_tracker()
            if tracker is None:
                if not is_untracked_graphrag_retrieval():
                    note_unattributed_graphrag_call()
            else:
                tracker.mark_incomplete(REASON_UNSUPPORTED_CALL)
            return _bound(original, self)(*args, **kwargs)

        method.__name__ = name
        setattr(method, _ORIGINAL_ATTR, original)
        return method

    for name in _UNACCOUNTED_LLM_METHODS:
        original = _original(name)
        if original is not None:
            setattr(BedrockConverse, name, _unaccounted(name, original))

    _llm_usage_patched = True
    logger.info("graphrag_llm_usage_patch_applied")


def llm_usage_patch_active() -> bool:
    """Whether GraphRAG's LLM calls are being recorded (see :func:`patch_llm_usage_accounting`)."""
    return _llm_usage_patched
