# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Knowledge Graph Build Task — ECS Fargate entrypoint.

Reads pre-processed text files and their metadata sidecars from the
staging S3 prefix, wraps them as LlamaIndex Documents, and runs the
GraphRAG Toolkit's indexing pipeline.  Output: entities and relationships
in Neptune DB, vector embeddings in OpenSearch Serverless.

Environment variables:
  Required (from ECS task definition):
    BUCKET_NAME              — S3 bucket for staging data + extracted artifacts
    DOC_SOURCES_TABLE        — DynamoDB table for doc-source status tracking
    NEPTUNE_ENDPOINT         — Neptune DB endpoint hostname
    OPENSEARCH_ENDPOINT      — OpenSearch Serverless endpoint hostname

  Required (from Step Functions container overrides):
    DOC_SOURCE_ID            — doc-source identifier (DDB sort key)
    NAMESPACE_ID             — namespace / tenant identifier (DDB partition key)
    STAGING_PREFIX           — S3 prefix containing pre-processed text + metadata sidecars

  Optional:
    EXTRACTION_MODE          — "continuous" (default) or "separated"
    USE_BATCH_INFERENCE      — "true" or "false" (default) — uses Bedrock batch inference
    BATCH_INFERENCE_ROLE_ARN — IAM role for Bedrock batch inference (required if USE_BATCH_INFERENCE=true)
    ENABLE_VERSIONING        — "true" (default) or "false"
    ENABLE_PROPOSITION_EXTRACTION — "true" (default) or "false" — skip for ~50% fewer LLM calls
    INFER_ENTITY_CLASSIFICATIONS — "true" (default) or "false" — derive the entity-class
                                   vocabulary from THIS corpus at ingest start rather than
                                   inheriting graphrag's hardcoded news/finance defaults
                                   ('Company', 'Sports Team', 'Creative Work', …).
    PREFERRED_ENTITY_CLASSIFICATIONS — JSON-encoded list of entity-class labels (e.g.
                                   '["Policy","Claim","Loss Ratio"]'). Non-empty overrides
                                   INFER_ENTITY_CLASSIFICATIONS. Default: "[]".
    PREFERRED_TOPICS          — JSON-encoded list of preferred topic names (e.g.
                                '["Black Tie Gala","Everyday Elegance"]'). Steers which
                                thematic groupings the extractor assigns chunks to
                                (__Topic__ nodes). Empty (default "[]") lets the model
                                name topics freely. There is no INFER_TOPICS flag — the
                                toolkit has no topic inference pass.
    CHUNK_SIZE                — Positive integer to override the toolkit's default
                                SentenceSplitter chunk_size=256; "0" (default) keeps the
                                toolkit default.
    CHUNK_OVERLAP             — Positive integer to override the toolkit's default 25 tokens
                                of overlap; "0" (default) keeps the toolkit default.
    DELETE_PREV_VERSIONS      — "true" or "false" (default) — delete archived versions after ingestion
    BEDROCK_MODEL_ARN        — Bedrock inference profile ARN (set by Trigger Lambda, no default)
    AWS_REGION               — AWS region (default: us-east-1)
    LOG_LEVEL                — level for our own structlog loggers (default: INFO)
    DEPENDENCY_LOG_LEVEL     — level for third-party stdlib loggers, notably
                               graphrag-toolkit (default: INFO). DEBUG surfaces the
                               batch-write retry ladder but is very verbose.
    KG_HEARTBEAT_INTERVAL_SECONDS — liveness log interval (default: 60, 0 disables)

Notes:
  AOSS NEXTGEN rejects knn_vector mappings with a 'method.engine' key.
  _patch_graphrag_toolkit_for_aoss_nextgen() works around this at startup.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import traceback

import structlog
from coa_common.config import resolve_region
from coa_common.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_DELETE_PREV_VERSIONS,
    DEFAULT_EMBED_DIMENSIONS,
    DEFAULT_EMBED_MODEL_ID,
    DEFAULT_ENABLE_PROPOSITION_EXTRACTION,
    DEFAULT_ENABLE_VERSIONING,
    DEFAULT_EXTRACTION_MODE,
    DEFAULT_INFER_ENTITY_CLASSIFICATIONS,
    DEFAULT_PREFERRED_ENTITY_CLASSIFICATIONS_JSON,
    DEFAULT_PREFERRED_TOPICS_JSON,
    DEFAULT_USE_BATCH_INFERENCE,
    EXTRACTION_BATCH_SIZE,
    MAX_VOCABULARY_ENTRIES,
    STAGED_TEXT_EXTENSIONS,
    ExtractionMode,
    validate_id,
)
from coa_common.dao import DynamoDBDAO
from coa_common.embeddings import make_llama_index_embedding
from coa_common.logging import setup_logging
from coa_common.s3 import get_s3_client, list_objects, read_file_bytes
from coa_control_plane_server.models.source_status import SourceStatus

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# graphrag-toolkit logs via stdlib ``logging``, so its records only reach
# CloudWatch because setup_logging() bridges stdlib into structlog. Without the
# bridge they fall to ``logging.lastResort`` (stderr, WARNING-only), which drops
# every INFO line — including the per-batch "Running build pipeline [num_workers,
# job_sizes, batch_write_size]" record that is the only place the effective write
# parallelism is reported.
setup_logging(
    os.environ.get("LOG_LEVEL", "INFO"),
    stdlib_log_level=os.environ.get("DEPENDENCY_LOG_LEVEL", "INFO"),
)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWS_REGION = resolve_region()
EXTRACTION_MODE = ExtractionMode(os.environ.get("EXTRACTION_MODE", DEFAULT_EXTRACTION_MODE).lower())
USE_BATCH_INFERENCE = os.environ.get("USE_BATCH_INFERENCE", DEFAULT_USE_BATCH_INFERENCE).lower() == "true"
ENABLE_VERSIONING = os.environ.get("ENABLE_VERSIONING", DEFAULT_ENABLE_VERSIONING).lower() == "true"
ENABLE_PROPOSITION_EXTRACTION = (
    os.environ.get("ENABLE_PROPOSITION_EXTRACTION", DEFAULT_ENABLE_PROPOSITION_EXTRACTION).lower() == "true"
)
# When True, graphrag runs a lightweight LLM sweep at ingest start that derives
# the entity-class vocabulary from THIS corpus and replaces the toolkit's
# hardcoded DEFAULT_ENTITY_CLASSIFICATIONS. The toolkit's default is the news/
# finance list ('Company', 'Sports Team', 'Creative Work', …) — inheriting it
# on a non-news corpus mis-guides extraction, so we opt in by default. Costs
# one extra LLM sweep per ingest (see InferClassificationsConfig defaults:
# num_samples=5, num_iterations=1, num_classifications=15).
INFER_ENTITY_CLASSIFICATIONS = (
    os.environ.get("INFER_ENTITY_CLASSIFICATIONS", DEFAULT_INFER_ENTITY_CLASSIFICATIONS).lower() == "true"
)


# Explicit vocabulary supplied by the user (or, in a future change, resolved
# from the namespace's accepted ontology). Arrives as a JSON-encoded list from
# the trigger Lambda — an object/array can't be inlined into a Step Functions
# container-override JsonPath, so it must be pre-stringified. When non-empty
# this WINS over INFER_ENTITY_CLASSIFICATIONS: the user asked for these exact
# labels, so don't second-guess them with an inference pass.
def _env_str_list(name: str, default_json: str) -> list[str]:
    """Decode a JSON-array-of-strings env var into a cleaned ``list[str]``.

    Values arrive pre-stringified because a Step Functions container-override
    JsonPath cannot inline an array. Entries are stripped and empties dropped.

    **Raises** on malformed / wrong-typed JSON rather than degrading to ``[]``.
    The API validates this vocabulary and 400s before ingestion starts, so a real
    caller's bad input never reaches here; a malformed value at container start
    therefore means a *pipeline* bug, not user input. Failing loudly surfaces it as
    SCAN_FAILED, where silently extracting with no vocabulary would hide it — the
    user would just get a graph that ignores what they configured (raised in review).

    Over-cap is the one case that still degrades: it clamps to
    ``MAX_VOCABULARY_ENTRIES`` with a warning, because the first N entries are a
    valid steer. The API already enforces the cap, so this is defence in depth for
    an execution started outside it; every entry is injected into every chunk's
    prompt, so an unbounded list is a cost and adherence problem.
    """
    raw = os.environ.get(name, default_json)
    try:
        decoded = json.loads(raw) if raw else []
        if not isinstance(decoded, list) or not all(isinstance(x, str) for x in decoded):
            raise ValueError("must be a JSON array of strings")
    except (ValueError, json.JSONDecodeError) as exc:
        # Fail loud: an API-validated field arriving malformed here is a pipeline
        # defect. Crashing the task (→ SCAN_FAILED) is more honest than proceeding
        # with no vocabulary and a warning the user never reads.
        raise ValueError(f"{name} is not a JSON array of strings: {raw[:256]!r}") from exc
    cleaned = [x.strip() for x in decoded if x.strip()]
    if len(cleaned) > MAX_VOCABULARY_ENTRIES:
        logger.warning(
            "vocabulary_env_var_over_cap_truncated",
            env_var=name,
            supplied=len(cleaned),
            cap=MAX_VOCABULARY_ENTRIES,
        )
        cleaned = cleaned[:MAX_VOCABULARY_ENTRIES]
    return cleaned


PREFERRED_ENTITY_CLASSIFICATIONS: list[str] = _env_str_list(
    "PREFERRED_ENTITY_CLASSIFICATIONS", DEFAULT_PREFERRED_ENTITY_CLASSIFICATIONS_JSON
)

# Explicit topic vocabulary. Steers the thematic grouping the extractor assigns
# chunks to (``__Topic__`` nodes, later induced as ``skos:Concept``) — a separate
# axis from the entity classifications above (``__Entity__.class`` → ``owl:Class``).
# Empty means "let the model name topics from the chunk text", which is the
# pre-existing behaviour; there is no inference pass to toggle.
PREFERRED_TOPICS: list[str] = _env_str_list("PREFERRED_TOPICS", DEFAULT_PREFERRED_TOPICS_JSON)

# Optional chunk-size / chunk-overlap override. 0 (default) means "use the
# graphrag-toolkit default" — SentenceSplitter(chunk_size=256, chunk_overlap=25).
# The benchmark harness pins 1024 for dense/tabular corpora; use that as the
# starting point when tables dominate the corpus.
# These arrive as stringified integers from the trigger Lambda via the SFN ->
# ECS env pipe. Guard the parse: a malformed value must degrade to the toolkit
# default with a logged warning, never crash the container at import time
# (which would fail the whole KG build before any diagnostic is emitted).
try:
    CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
    CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP)))
except (TypeError, ValueError) as _chunk_exc:
    logger.warning(
        "invalid CHUNK_SIZE/CHUNK_OVERLAP, using toolkit defaults",
        chunk_size=os.environ.get("CHUNK_SIZE"),
        chunk_overlap=os.environ.get("CHUNK_OVERLAP"),
        error=str(_chunk_exc),
    )
    CHUNK_SIZE = DEFAULT_CHUNK_SIZE
    CHUNK_OVERLAP = DEFAULT_CHUNK_OVERLAP

DELETE_PREV_VERSIONS = os.environ.get("DELETE_PREV_VERSIONS", DEFAULT_DELETE_PREV_VERSIONS).lower() == "true"
BATCH_INFERENCE_ROLE_ARN = os.environ.get("BATCH_INFERENCE_ROLE_ARN", "")
BEDROCK_MODEL_ARN = os.environ.get("BEDROCK_MODEL_ARN", "")
# Embedding model for the chunk_* vector index. MUST match what serve queries
# with (see coa_serve.lexical.baseline_retriever) — the shared
# default is the single source of truth; overridable via env for migrations.
BEDROCK_EMBED_MODEL_ID = os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID)
BEDROCK_EMBED_DIMENSIONS = int(os.environ.get("BEDROCK_EMBED_DIMENSIONS", str(DEFAULT_EMBED_DIMENSIONS)))

CHECKPOINT_DIR = "/tmp/graphrag-checkpoints"

# Max OUTPUT tokens for the extraction LLM. The toolkit defaults a bare model
# string to 4096, which truncates extraction mid-response on dense chunks and
# silently drops that chunk's trailing entities/statements. 16384 matches the
# graphrag-toolkit benchmark harness. Env-overridable without a deploy.
EXTRACTION_MAX_TOKENS = int(os.environ.get("EXTRACTION_MAX_TOKENS", "16384"))

# Vector indexes to build: ``chunk`` + ``topic``, matching the graphrag-toolkit
# benchmark harness (benchmarks/ingest_v5.py, INDEX_NAMES = ['chunk', 'topic']).
#
# Two deliberate deviations from the toolkit's ``DEFAULT_EMBEDDING_INDEXES``
# (``['chunk', 'statement']``, graphrag storage/constants.py):
#
#   + topic: ``TopicBeamSearch`` (new in graphrag-lexical-graph 3.18.7) seeds from
#     ``get_index('topic')`` and silently skips any topic with no stored vector, so
#     without this index it degenerates to an empty seed set.
#   - statement: statements are still BUILT into the graph, just not embedded.
#     Nothing reads the statement *vector* index — serve never calls
#     ``get_index('statement')``, and the traversal retrievers reach statements
#     through the graph (``__SUPPORTS__``/``__MENTIONED_IN__``), not by vector
#     search. Dropping it removes an embedding cost per statement (49,548 of them
#     on SEC-10-Q vs 3,125 chunks) with no retrieval loss.
#
# Embeddings are only written during indexing, so this takes effect on a fresh
# ingest — an existing graph needs a re-ingest to gain/lose an index.
# Env-overridable (e.g. EMBEDDING_INDEXES=chunk,statement,topic) without a deploy.
EMBEDDING_INDEXES = [
    name.strip() for name in os.environ.get("EMBEDDING_INDEXES", "chunk,topic").split(",") if name.strip()
]


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        logger.error("Missing required environment variable", key=key)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# S3 staging file loading → LlamaIndex Documents (with sidecar metadata)
# ---------------------------------------------------------------------------


def _load_staged_documents(bucket: str, staging_prefix: str) -> tuple[list, int]:
    """Download staged text files + metadata sidecars from S3.

    Returns ``(documents, unloaded_count)`` where ``unloaded_count`` is the number
    of staged text files that were listed but produced no document — either they
    could not be downloaded / decoded, or they held no text at all.

    An empty staged file used to be skipped with a bare ``continue`` that left
    every counter untouched, so a source whose files all extracted to zero bytes
    reported a clean success having contributed nothing to the graph. Both
    outcomes mean "a file we staged is not in the graph", which is what the
    caller's failure accounting is for, so both are counted here.

    Raises on a LISTING failure (transient S3 error, wrong prefix, IAM hiccup):
    an empty listing must NOT be silently indistinguishable from a real failure,
    or a throttled ListObjects would look like "no documents to ingest" and the
    job would report success having ingested nothing.
    """
    from llama_index.core import Document

    if ENABLE_VERSIONING:
        from graphrag_toolkit.lexical_graph import add_versioning_info

    s3 = get_s3_client()

    try:
        all_files = list_objects(s3, bucket, staging_prefix)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Failed to list staged files", bucket=bucket, prefix=staging_prefix, error=tb)
        raise RuntimeError(f"Failed to list staged files under s3://{bucket}/{staging_prefix}") from exc

    # Index sidecar metadata by stem
    sidecars: dict[str, dict] = {}
    for f in all_files:
        key = f["Key"]
        if key.endswith(".metadata.json"):
            stem = key.rsplit("/", 1)[-1].removesuffix(".metadata.json")
            try:
                raw = read_file_bytes(s3, bucket, key)
                sidecars[stem] = json.loads(raw.decode("utf-8"))
            except Exception:
                logger.warning("Failed to read sidecar", s3_key=key, exc_info=True)

    text_files = [f for f in all_files if any(f["Key"].endswith(ext) for ext in STAGED_TEXT_EXTENSIONS)]
    logger.info("Listed staged files", text_count=len(text_files), sidecar_count=len(sidecars), prefix=staging_prefix)

    documents: list[Document] = []
    failed_count = 0
    empty_count = 0
    for file_obj in text_files:
        key = file_obj["Key"]
        try:
            content_bytes = read_file_bytes(s3, bucket, key)
            text = content_bytes.decode("utf-8", errors="replace")

            if not text.strip():
                # Preprocessing now refuses to stage an empty extraction, so this
                # only fires for a prefix staged before that guard existed. Count
                # it: a staged file missing from the graph is the same outcome as
                # one that failed to download, and a bare `continue` on data that
                # was expected to exist is how a 50% corpus loss reported clean.
                logger.warning("Empty staged file, skipping", s3_key=key)
                empty_count += 1
                continue

            filename = key.rsplit("/", 1)[-1] if "/" in key else key
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename

            metadata: dict[str, str | int] = {"filename": filename}
            sidecar = sidecars.get(stem)
            if sidecar:
                # Sanitize keys: replace dots and hyphens with underscores so they
                # are valid as Neptune/OpenCypher property names. The toolkit embeds
                # metadata keys directly in Cypher queries without quoting.
                # Two distinct source keys can collapse to the same sanitized key
                # (e.g. "report.id" and "report-id" both -> "report_id"); a plain
                # dict assignment would silently drop one value. Disambiguate
                # collisions with a numeric suffix so no source metadata is lost.
                origin_of: dict[str, str] = {}
                for k, v in sidecar.items():
                    safe_key = k.replace(".", "_").replace("-", "_")
                    if safe_key in origin_of and origin_of[safe_key] != k:
                        logger.warning(
                            "Sanitized metadata key collision; disambiguating",
                            s3_key=key,
                            original_key=k,
                            collides_with=origin_of[safe_key],
                            sanitized_key=safe_key,
                        )
                        # Probe origin_of (the sanitized-key namespace this loop
                        # owns) rather than metadata: metadata also carries
                        # pre-seeded keys (filename, later versioning fields) that
                        # are unrelated to sanitized-key disambiguation, so scoping
                        # the free-slot search to origin_of keeps this self-contained.
                        suffix = 2
                        while f"{safe_key}_{suffix}" in origin_of:
                            suffix += 1
                        safe_key = f"{safe_key}_{suffix}"
                    origin_of[safe_key] = k
                    metadata[safe_key] = v

            if ENABLE_VERSIONING:
                id_fields = ["source_s3_key"] if "source_s3_key" in metadata else ["filename"]
                metadata = add_versioning_info(metadata, id_fields=id_fields)

            doc = Document(text=text, metadata=metadata)
            # Exclude all metadata from the chunker's token budget.
            # These fields live on the Source node in the graph (document-level),
            # not in each chunk's text window. Without this, long fields like
            # source_s3_key consume most of the 256-token chunk budget.
            # Both must be set: the splitter uses max(embed, llm) metadata length.
            doc.excluded_llm_metadata_keys = list(metadata.keys())
            doc.excluded_embed_metadata_keys = list(metadata.keys())
            documents.append(doc)
        except Exception:
            tb = traceback.format_exc()
            logger.error("Failed to load staged file", s3_key=key, error=tb)
            failed_count += 1

    logger.info(
        "Loaded documents",
        count=len(documents),
        failed=failed_count,
        empty=empty_count,
        total_text_files=len(text_files),
    )
    return documents, failed_count + empty_count


# ---------------------------------------------------------------------------
# GraphRAG setup
# ---------------------------------------------------------------------------


_graphrag_aoss_patched = False


def _patch_graphrag_toolkit_for_aoss_nextgen(*, allow_create: bool = True) -> None:
    """Patch graphrag_toolkit's index_exists to drop the unsupported 'engine' field.

    AOSS NEXTGEN rejects knn_vector mappings with a 'method.engine' key.
    Same fix as opensearch_vector.py commit 2261418. No-op in unit tests
    (stubs don't expose the full module path).

    ``allow_create=False`` makes the patch never create a missing index, only
    report whether it exists. The DELETION task passes this: graphrag's
    ``VectorIndex.writeable`` defaults to True and its ``index_exists`` creates
    when writeable, so a cleanup run against an already-empty tenant would
    RECREATE ``chunk_*``/``topic_*`` as empty shells. Nothing ever reclaims
    those (the namespace they belong to is being deleted), and AOSS caps a
    collection at 1000 indexes, so they accumulate until every embedding write
    in the deployment fails with ``index_limit_breached``. Same
    reads-must-not-create rule the ontology vector store follows — see
    ``OpenSearchVectorStore._read_index``. With no index there are no
    embeddings to delete, so graphrag falls back to its
    ``DummyOpensearchVectorClient`` and the delete is correctly a no-op.
    """
    global _graphrag_aoss_patched
    if _graphrag_aoss_patched:
        return
    try:
        import graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes as _ovi
        from opensearchpy.exceptions import RequestError as _RequestError
    except (ImportError, AttributeError):
        logger.debug("graphrag_toolkit AOSS patch skipped (module not available)")
        return

    def _nextgen_index_exists(endpoint: str, index_name: str, dimensions: int, writeable: bool) -> bool:
        idx_conf = {
            "settings": {"index": {"knn": True}},
            "mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": dimensions}}},
        }
        client = _ovi.create_os_client(endpoint, pool_maxsize=1)
        index_exists = False
        try:
            index_exists = client.indices.exists(index=index_name)
            if not index_exists and writeable and allow_create:
                client.indices.create(index=index_name, body=idx_conf)
                index_exists = True
        except _RequestError as e:
            if e.error == "resource_already_exists_exception":
                index_exists = True
            else:
                logger.exception("AOSS NEXTGEN index creation failed [%s]", index_name)
                index_exists = _ovi.index_is_available(client, index_name)
        finally:
            client.close()
        return index_exists

    _ovi.index_exists = _nextgen_index_exists
    _graphrag_aoss_patched = True


_graphrag_paginated_search_patched = False

# Statuses worth retrying: 429 (throttle) + 5xx. AOSS NEXTGEN surfaces transient
# OCU-pressure faults as any of these; opensearch-py's transport only retries
# 502/503/504 (and not 500/429), so we retry the full set at this layer.
_PAGINATED_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_PAGINATED_MAX_RETRIES = 6


def _patch_graphrag_paginated_search_retry() -> None:
    """Make graphrag's OpenSearchIndex.paginated_search resilient to transient AOSS 5xx.

    Upstream 3.18.5 has two problems in the search retry loop: it does not retry
    transient 5xx/429 (only NotFoundError), and its generic handler does
    ``except Exception as ee: ... raise e`` where ``e`` is unbound — so any non-404
    error raises ``UnboundLocalError`` that masks the real fault and kills the whole
    KG build. Replace the method with a line-faithful copy that retries transient
    statuses with capped backoff and, on exhaustion, re-raises the *real* error with
    its full AOSS body logged (the original loses it). Separate flag + narrow import
    so an ImportError here never disables the engine patch above.
    """
    global _graphrag_paginated_search_patched
    if _graphrag_paginated_search_patched:
        return
    try:
        import graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes as _ovi
        from opensearchpy.exceptions import NotFoundError, TransportError
    except (ImportError, AttributeError):
        logger.debug("graphrag_toolkit paginated_search patch skipped (module not available)")
        return

    def _nextgen_paginated_search(self, query, page_size=10000, max_pages=None, ids_only=False):
        client = self.client._os_client
        if client is None:
            return

        search_after = None
        page = 0

        while True:
            body = {"size": page_size, "query": query, "sort": [{"_id": "asc"}]}
            if ids_only:
                body["_source"] = False
                body["fields"] = ["id"]
            if search_after:
                body["search_after"] = search_after

            not_found_retries = 0
            transient_retries = 0
            response = None

            while response is None:
                try:
                    response = client.search(index=self.underlying_index_name(), body=body)
                except NotFoundError as exc:
                    not_found_retries += 1
                    if not_found_retries > 3:
                        raise exc
                    logger.debug(
                        "[%s] Index not found during paginated search — retrying in 5s [retry %d]",
                        self.underlying_index_name(),
                        not_found_retries,
                    )
                    time.sleep(5)
                except TransportError as exc:
                    status = getattr(exc, "status_code", None)
                    if status not in _PAGINATED_RETRY_STATUS or transient_retries >= _PAGINATED_MAX_RETRIES:
                        # Out of retries (or non-retryable) — surface the REAL error with its
                        # full body so the cause is diagnosable (upstream lost it via `raise e`).
                        logger.error(
                            "[%s] paginated search failed (status=%s, transient_retries=%d): %s",
                            self.underlying_index_name(),
                            status,
                            transient_retries,
                            getattr(exc, "info", None) or getattr(exc, "error", None) or str(exc),
                        )
                        raise
                    transient_retries += 1
                    backoff = min(30.0, 2.0**transient_retries) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] transient AOSS error (status=%s) on paginated search — retry %d/%d after %.1fs",
                        self.underlying_index_name(),
                        status,
                        transient_retries,
                        _PAGINATED_MAX_RETRIES,
                        backoff,
                    )
                    time.sleep(backoff)

            hits = response["hits"]["hits"]
            if not hits:
                break

            yield hits

            search_after = hits[-1]["sort"]
            page += 1
            if max_pages and page >= max_pages:
                break

    _ovi.OpenSearchIndex.paginated_search = _nextgen_paginated_search
    _graphrag_paginated_search_patched = True


_graphrag_bulk_ingest_patched = False
# AOSS NEXTGEN trips the parent (JVM-heap) circuit breaker under concurrent
# bulk embedding writes at low OCU: status 429, type circuit_breaking_exception
# ("[parent] Data too large …"). It's transient — the breaker clears as heap
# drains — but graphrag's bulk() uses raise_on_error=True with only a 5/10/20s
# backoff, so the BulkIndexError kills the whole KG build. 503 = node unavailable
# during a rolling window; 500 = transient AOSS internal error ("Internal error
# occurred while processing request") observed under load — also worth retrying.
_BULK_RETRY_STATUS = frozenset({429, 500, 503})
_BULK_MAX_RETRIES = 6


def _patch_graphrag_bulk_ingest_retry() -> None:
    """Make graphrag's bulk embedding writes survive transient AOSS breaker 429s.

    graphrag monkey-patches ``OpensearchVectorClient._bulk_ingest_embeddings`` to
    call opensearchpy ``bulk(...)`` with the default ``raise_on_error=True``, so a
    per-item ``circuit_breaking_exception`` (429) becomes a ``BulkIndexError`` that
    fails the entire 510-doc build. Re-patch it to collect errors instead of
    raising, then retry ONLY the breaker-429/503 items with capped backoff long
    enough for the node heap to drain (graphrag's 5-20s is too short for a heap
    breaker). Non-retryable item errors re-raise loudly with their bodies.

    Separate flag + narrow import so an ImportError here can't disable the other
    patches. Must run AFTER graphrag binds its own ``_bulk_ingest_embeddings``
    (it does so at import of opensearch_vector_indexes), so we assign last.
    """
    global _graphrag_bulk_ingest_patched
    if _graphrag_bulk_ingest_patched:
        return
    try:
        import uuid as _uuid

        import llama_index.vector_stores.opensearch as _osvs
        from opensearchpy.helpers import bulk as _os_bulk
    except (ImportError, AttributeError):
        logger.debug("graphrag bulk-ingest patch skipped (module not available)")
        return

    def _retrying_bulk_ingest_embeddings(
        self,
        client,
        index_name,
        embeddings,
        texts,
        metadatas=None,
        ids=None,
        vector_field="embedding",
        text_field="content",
        mapping=None,
        max_chunk_bytes=1 * 1024 * 1024,
        is_aoss=False,
    ):
        if not mapping:
            mapping = {}
        not_found_error = self._import_not_found_error()
        try:
            client.indices.get(index=index_name)
        except not_found_error:
            client.indices.create(index=index_name, body=mapping)

        requests = []
        return_ids = []
        for i, text in enumerate(texts):
            _id = ids[i] if ids else str(_uuid.uuid4())
            req = {
                "_op_type": "index",
                "_index": index_name,
                vector_field: embeddings[i],
                text_field: text,
                "metadata": metadatas[i] if metadatas else {},
            }
            req["id" if is_aoss else "_id"] = _id
            requests.append(req)
            return_ids.append(_id)

        by_id = {(r.get("_id") or r.get("id")): r for r in requests}
        pending = requests
        for attempt in range(_BULK_MAX_RETRIES + 1):
            # raise_on_error/raise_on_exception=False → failures come back as a
            # list of {op: info} dicts instead of throwing.
            _, errors = _os_bulk(
                client,
                pending,
                max_chunk_bytes=max_chunk_bytes,
                raise_on_error=False,
                raise_on_exception=False,
            )
            retriable_ids = []
            fatal = []
            for err in errors or []:
                info = next(iter(err.values()))
                status = info.get("status")
                doc_id = info.get("_id") or info.get("id")
                if status in _BULK_RETRY_STATUS and doc_id is not None:
                    retriable_ids.append(doc_id)
                else:
                    # Non-retryable status, OR a retryable status we can't
                    # map back to a pending doc (no _id/id). Fail loud rather
                    # than silently dropping the item from the retry set,
                    # which would lose that embedding without erroring.
                    fatal.append(info)
            if fatal:
                raise RuntimeError(f"[{index_name}] non-retryable bulk index errors: {fatal[:3]}")
            if not retriable_ids or attempt == _BULK_MAX_RETRIES:
                if retriable_ids:
                    raise RuntimeError(
                        f"[{index_name}] {len(retriable_ids)} docs still throttled "
                        f"(AOSS circuit breaker) after {_BULK_MAX_RETRIES} retries — "
                        f"lower BUILD_BATCH_WRITE_SIZE or raise AOSS min OCU"
                    )
                break
            pending = [by_id[i] for i in retriable_ids if i in by_id]
            backoff = min(60.0, 5.0 * 2**attempt) + random.uniform(0, 2)
            logger.warning(
                "AOSS circuit breaker on bulk write — retrying %d docs in %.1fs [retry %d/%d, index=%s]",
                len(pending),
                backoff,
                attempt + 1,
                _BULK_MAX_RETRIES,
                index_name,
            )
            time.sleep(backoff)

        if not is_aoss:
            client.indices.refresh(index=index_name)
        return []  # graphrag contract: empty == no unresolved errors

    # mypy: OpensearchVectorClient is typed (llama_index ships stubs in CI), so
    # reassigning its method trips [method-assign]. The reassignment is the whole
    # point of the monkey-patch; ignore it (same pattern as main.py's patched
    # module-level rebinds).
    _osvs.OpensearchVectorClient._bulk_ingest_embeddings = _retrying_bulk_ingest_embeddings  # type: ignore[method-assign]
    _graphrag_bulk_ingest_patched = True


def _setup_graphrag(tenant_id: str):
    """Configure GraphRAG and return (graph_store_uri, vector_store_uri)."""
    # Patch BEFORE importing graphrag — the monkey-patch must replace index_exists
    # in the module namespace before LexicalGraphIndex/VectorStoreFactory load and
    # bind their own references to it. Importing GraphRAGConfig triggers those loads.
    _patch_graphrag_toolkit_for_aoss_nextgen()
    _patch_graphrag_paginated_search_retry()

    from graphrag_toolkit.lexical_graph import GraphRAGConfig

    # AFTER the import: graphrag binds its own _bulk_ingest_embeddings onto
    # llama_index's OpensearchVectorClient at import time, so our retry wrapper
    # must be assigned last to win.
    _patch_graphrag_bulk_ingest_retry()

    GraphRAGConfig.aws_region = AWS_REGION
    GraphRAGConfig.enable_versioning = ENABLE_VERSIONING
    # Pin the embedding model explicitly (do NOT rely on the toolkit default) so
    # ingestion writes chunk vectors with the SAME model serve queries with.
    # Pass an embedding INSTANCE, not a model-id string: the toolkit's string path
    # builds a llama_index BedrockEmbedding whose Cohere request omits
    # output_dimension → Cohere v4 returns 1536-dim vectors instead of the
    # configured 1024, mismatching the index. The instance controls the request.
    GraphRAGConfig.embed_model = make_llama_index_embedding(
        model_id=BEDROCK_EMBED_MODEL_ID, dimensions=BEDROCK_EMBED_DIMENSIONS, region=AWS_REGION
    )
    GraphRAGConfig.embed_dimensions = BEDROCK_EMBED_DIMENSIONS
    if BEDROCK_MODEL_ARN:
        # Pass the extraction LLM as a JSON config, not a bare model string. The
        # toolkit's GraphRAGConfig.to_llm() builds BedrockConverse with a hardcoded
        # max_tokens=4096 for a bare string, and only reads max_tokens from the JSON
        # form (config.py to_llm). 4096 output tokens truncates extraction on dense
        # chunks — the model stops mid-response and the trailing entities/statements
        # for that chunk are silently lost. 16384 matches the graphrag-toolkit
        # benchmark harness (benchmarks/ingest_v5.py) for the same reason.
        # temperature is 0.0 either way; region/profile still come from GraphRAGConfig.
        GraphRAGConfig.extraction_llm = json.dumps({"model": BEDROCK_MODEL_ARN, "max_tokens": EXTRACTION_MAX_TOKENS})
    GraphRAGConfig.local_output_dir = CHECKPOINT_DIR
    if USE_BATCH_INFERENCE:
        GraphRAGConfig.extraction_batch_size = EXTRACTION_BATCH_SIZE

    neptune_endpoint = _require_env("NEPTUNE_ENDPOINT")
    opensearch_endpoint = _require_env("OPENSEARCH_ENDPOINT")

    neptune_host = neptune_endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
    opensearch_host = opensearch_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    graph_store_uri = f"neptune-db://{neptune_host}:8182"
    vector_store_uri = f"aoss://{opensearch_host}:443"

    # NOTE: chunking is intentionally NOT overridden here. With no SentenceSplitter
    # in the IndexingConfig, the toolkit falls back to its default
    # SentenceSplitter(chunk_size=256, chunk_overlap=25) (graphrag
    # lexical_graph_index.py). The graphrag-toolkit benchmark harness pins this
    # explicitly (--chunk-size, e.g. 1024 for dense corpora); we keep the default so
    # the r8g / worker / 3.18.7 / max_tokens deltas are measured against the existing
    # SEC-10-Q baseline without chunking as a confound. Revisit chunk_size as a
    # separate, isolated experiment.
    logger.info(
        "GraphRAG config",
        tenant_id=tenant_id,
        extraction_mode=EXTRACTION_MODE,
        batch_inference=USE_BATCH_INFERENCE,
        versioning=ENABLE_VERSIONING,
        proposition_extraction=ENABLE_PROPOSITION_EXTRACTION,
        delete_prev_versions=DELETE_PREV_VERSIONS,
        bedrock_model=BEDROCK_MODEL_ARN or "(toolkit default)",
        extraction_max_tokens=EXTRACTION_MAX_TOKENS,
        chunk_size="256 (toolkit default)",
    )
    return graph_store_uri, vector_store_uri


def _build_indexing_config(bucket_name: str, namespace_id: str, doc_source_id: str):
    """Build the IndexingConfig handed to LexicalGraphIndex.

    ALWAYS returns an IndexingConfig with a fully-specified ExtractionConfig —
    never None, and never a bare ``ExtractionConfig()``. Without this, when
    the default flags applied, ``_build_indexing_config``
    returned None, the toolkit built its own ``ExtractionConfig()``, and
    ``preferred_entity_classifications`` silently defaulted to
    ``DEFAULT_ENTITY_CLASSIFICATIONS`` (``['Company', 'Location', 'Event',
    'Sports Team', 'Person', 'Role', 'Product', 'Service', 'Creative Work',
    'Software', 'Financial Instrument']``, ``graphrag/indexing/constants.py``).
    That is a news/finance list — inheriting it steers the extraction LLM
    away from any non-news domain (insurance, healthcare, manufacturing, …).

    Vocabulary source (checked in this order):
    1. ``PREFERRED_ENTITY_CLASSIFICATIONS`` (JSON list) — an explicit list from
       the user is authoritative; no inference runs.
    2. ``INFER_ENTITY_CLASSIFICATIONS=true`` (default) — the toolkit runs a
       short LLM sweep at ingest start (``InferClassifications`` in
       ``graphrag/indexing/extract/infer_classifications.py``, default 5
       samples × 1 iteration → 15 labels) and uses the labels it discovered
       in this corpus. ``preferred_entity_classifications=[]`` +
       ``replace_default_classifications=True`` together mean "start from
       nothing, keep only what the corpus proves".
    3. Both off — the extractor runs unguided (empty list). Do not enable
       both off unless the toolkit-default vocabulary has been checked to be
       inappropriate for this corpus.

    Topic vocabulary: ``PREFERRED_TOPICS`` (JSON list) steers which thematic
    groupings the extractor assigns chunks to. Unlike the classifications above
    there is no inference option — graphrag-toolkit has no topic analogue of
    ``InferClassificationsConfig`` — so an empty list already means "let the model
    name topics freely", which is the behaviour of every ingest before this field
    existed. Topics land as ``__Topic__`` nodes and are later induced as
    ``skos:Concept``, so an unsteered vocabulary drifts across chunks.

    Chunking: ``CHUNK_SIZE`` / ``CHUNK_OVERLAP`` are ``0`` by default, in
    which case the toolkit's ``SentenceSplitter(chunk_size=256,
    chunk_overlap=25)`` is used. Setting a positive integer switches the
    IndexingConfig to that splitter — 1024 is the graphrag benchmark
    harness value for dense/tabular corpora.

    Future enhancement: when ``PREFERRED_ENTITY_CLASSIFICATIONS`` is
    empty AND the namespace has an accepted ontology, resolve its class
    labels here. Deferred because it requires a cross-service HTTP call to
    ontology-engine (or a direct SPARQL client), neither of which
    ``packages/sources`` has today.
    """
    from graphrag_toolkit.lexical_graph import ExtractionConfig, IndexingConfig
    from graphrag_toolkit.lexical_graph.indexing.extract import InferClassificationsConfig
    from llama_index.core.node_parser import SentenceSplitter

    # --- Extraction config ---
    infer_config: InferClassificationsConfig | bool
    if PREFERRED_ENTITY_CLASSIFICATIONS:
        # Explicit list wins. Never run inference on top: the user asked for
        # THESE labels, not "these plus whatever else the LLM proposes".
        preferred = list(PREFERRED_ENTITY_CLASSIFICATIONS)
        infer_config = False
    elif INFER_ENTITY_CLASSIFICATIONS:
        # Corpus-inferred vocabulary. ``preferred_entity_classifications=[]``
        # is NOT the same as "unset": the toolkit reads an empty list as the
        # seed vocabulary (see ``lexical_graph_index.py`` ~line 377); with
        # ``replace_default_classifications=True`` the inferer replaces that
        # seed with what it derives from THIS corpus, so the final vocabulary
        # is entirely corpus-driven. Leaving the argument unset would have
        # fallen back to the news/finance list — the exact bug this fixes.
        preferred = []
        infer_config = InferClassificationsConfig(replace_default_classifications=True)
    else:
        # Both off → unguided extraction. Logged loudly, since this is the
        # only configuration where the LLM types entities from scratch.
        logger.warning(
            "Both PREFERRED_ENTITY_CLASSIFICATIONS and INFER_ENTITY_CLASSIFICATIONS are off — "
            "extraction runs unguided. Set one of them to steer entity typing."
        )
        preferred = []
        infer_config = False

    # ``preferred_topics`` is a separate axis from the classifications above:
    # it steers the THEMATIC grouping (``__Topic__`` nodes) rather than what an
    # entity IS. An empty list is passed through unchanged — the toolkit reads it
    # as "no preferred topics", the extractor names them from the chunk text, and
    # behaviour matches every ingest before this field existed.
    extraction_config = ExtractionConfig(
        enable_proposition_extraction=ENABLE_PROPOSITION_EXTRACTION,
        preferred_entity_classifications=preferred,
        infer_entity_classifications=infer_config,
        preferred_topics=PREFERRED_TOPICS,
    )

    # --- Chunking ---
    chunking = None
    if CHUNK_SIZE > 0:
        overlap = CHUNK_OVERLAP if CHUNK_OVERLAP > 0 else 25
        # @range on chunkSize/chunkOverlap is per-field; it cannot express the
        # cross-field invariant overlap < size. A SentenceSplitter with
        # overlap >= size produces degenerate/empty chunks, so clamp defensively.
        if overlap >= CHUNK_SIZE:
            clamped = max(1, CHUNK_SIZE - 1)
            logger.warning(
                "CHUNK_OVERLAP >= CHUNK_SIZE, clamping",
                chunk_size=CHUNK_SIZE,
                requested_overlap=overlap,
                clamped_overlap=clamped,
            )
            overlap = clamped
        chunking = [SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=overlap)]

    # --- Batch config ---
    batch_config = None
    if USE_BATCH_INFERENCE:
        from graphrag_toolkit.lexical_graph.indexing.extract import BatchConfig

        if BATCH_INFERENCE_ROLE_ARN:
            batch_config = BatchConfig(
                region=AWS_REGION,
                bucket_name=bucket_name,
                key_prefix=f"{namespace_id}/batch-inference/{doc_source_id}",
                role_arn=BATCH_INFERENCE_ROLE_ARN,
            )
        else:
            logger.warning(
                "USE_BATCH_INFERENCE=true but BATCH_INFERENCE_ROLE_ARN not set — falling back to real-time inference"
            )

    logger.info(
        "Extraction config built",
        proposition_extraction=ENABLE_PROPOSITION_EXTRACTION,
        preferred_entity_classifications_count=len(preferred),
        infer_entity_classifications=bool(infer_config),
        preferred_topics_count=len(PREFERRED_TOPICS),
        chunk_size=CHUNK_SIZE if CHUNK_SIZE > 0 else "256 (toolkit default)",
        chunk_overlap=CHUNK_OVERLAP if CHUNK_OVERLAP > 0 else "25 (toolkit default)",
        batch_inference=USE_BATCH_INFERENCE and batch_config is not None,
    )

    kwargs: dict = {"extraction": extraction_config, "batch_config": batch_config}
    if chunking is not None:
        kwargs["chunking"] = chunking
    return IndexingConfig(**kwargs)


# ---------------------------------------------------------------------------
# Mode: continuous — extract_and_build() in a single pass
# ---------------------------------------------------------------------------


def _run_continuous(
    documents: list,
    graph_store_uri: str,
    vector_store_uri: str,
    tenant_id: str,
    indexing_config,
    ddb: DynamoDBDAO,
    ddb_key: dict,
    progress_monitor,
) -> tuple[int, int]:
    """Single-pass extract_and_build(). Toolkit handles micro-batching internally."""
    from graphrag_toolkit.lexical_graph import LexicalGraphIndex
    from graphrag_toolkit.lexical_graph.indexing.build import Checkpoint
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory

    total_docs = len(documents)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)

    logger.info("Starting continuous KG build", total_docs=total_docs, batch_inference=USE_BATCH_INFERENCE)

    with (
        GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store,
        VectorStoreFactory.for_vector_store(vector_store_uri, index_names=EMBEDDING_INDEXES) as vector_store,
    ):
        graph_index = LexicalGraphIndex(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            extraction_dir=CHECKPOINT_DIR,
            indexing_config=indexing_config,
        )

        # Checkpoint helps with within-process retries (Bedrock throttle →
        # toolkit retries → resumes from last checkpoint instead of chunk 1)
        checkpoint = Checkpoint("continuous")
        ddb.update(ddb_key, {"status": SourceStatus.SCANNING_ENTITY_EXTRACTION})

        try:
            t0 = time.time()
            graph_index.extract_and_build(documents, checkpoint=checkpoint, progress_monitor=progress_monitor)
            elapsed = round(time.time() - t0, 2)
            logger.info("Continuous build complete", docs=total_docs, elapsed_seconds=elapsed)
            ddb.update(ddb_key, {"docsGraphIngested": total_docs})
            return total_docs, 0
        except Exception:
            tb = traceback.format_exc()
            logger.error("Continuous build failed", error=tb)
            ddb.update(ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": f"SCAN_FAILED_BUILD: {tb[:500]}"})
            return 0, total_docs


# ---------------------------------------------------------------------------
# Backend health checks
# ---------------------------------------------------------------------------


def _check_backend_health(
    graph_store_uri: str,
    vector_store_uri: str,
    ddb: DynamoDBDAO,
    ddb_key: dict,
) -> bool:
    """Verify graph store and vector store are reachable before starting the build.

    Returns True if both backends are healthy. On failure, updates DDB with a
    descriptive error message and returns False.
    """
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory

    # --- Graph store (Neptune) health check ---
    try:
        with GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store:
            graph_store.execute_query("RETURN 1 AS health")
        logger.info("Graph store health check passed")
    except Exception:
        tb = traceback.format_exc()
        logger.error("Graph store health check failed", error=tb)
        ddb.update(
            ddb_key,
            {
                "status": SourceStatus.SCAN_FAILED,
                "errorMessage": f"SCAN_FAILED_GRAPH_STORE: {tb[:500]}",
            },
        )
        return False

    # --- Vector store (AOSS) health check ---
    try:
        from graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes import (
            create_os_client,
        )

        vector_endpoint = vector_store_uri.replace("aoss://", "")
        client = create_os_client(vector_endpoint, pool_maxsize=1)
        try:
            client.cat.indices(format="json")
        finally:
            client.close()
        logger.info("Vector store health check passed")
    except Exception:
        tb = traceback.format_exc()
        logger.error("Vector store health check failed", error=tb)
        ddb.update(
            ddb_key,
            {
                "status": SourceStatus.SCAN_FAILED,
                "errorMessage": f"SCAN_FAILED_VECTOR_STORE: {tb[:500]}",
            },
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Mode: separated — extract() → S3 → build()
# ---------------------------------------------------------------------------


def _run_separated(
    documents: list,
    graph_store_uri: str,
    vector_store_uri: str,
    tenant_id: str,
    indexing_config,
    bucket_name: str,
    namespace_id: str,
    doc_source_id: str,
    ddb: DynamoDBDAO,
    ddb_key: dict,
    progress_monitor,
) -> tuple[int, int]:
    """Extract → S3 → Build. Extraction artifacts are persisted and reusable."""
    from graphrag_toolkit.lexical_graph import LexicalGraphIndex
    from graphrag_toolkit.lexical_graph.indexing.load import S3BasedDocs
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory

    total_docs = len(documents)
    # Use tenant_id as the collection prefix for S3 artifact organization.
    collection_id = f"{tenant_id}.{int(time.time())}"
    extracted_prefix = f"{namespace_id}/extracted/{doc_source_id}"

    extracted_docs = S3BasedDocs(
        region=AWS_REGION,
        bucket_name=bucket_name,
        key_prefix=extracted_prefix,
        collection_id=collection_id,
    )

    logger.info(
        "Starting separated KG build",
        total_docs=total_docs,
        batch_inference=USE_BATCH_INFERENCE,
        extracted_s3=f"s3://{bucket_name}/{extracted_prefix}/{collection_id}/",
    )

    with (
        GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store,
        VectorStoreFactory.for_vector_store(vector_store_uri, index_names=EMBEDDING_INDEXES) as vector_store,
    ):
        graph_index = LexicalGraphIndex(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            indexing_config=indexing_config,
        )

        # Stage 1: Extract
        logger.info("Stage 1: Extract")
        ddb.update(ddb_key, {"status": SourceStatus.SCANNING_ENTITY_EXTRACTION})

        try:
            t0 = time.time()
            graph_index.extract(documents, handler=extracted_docs, progress_monitor=progress_monitor)
            logger.info("Extract complete", elapsed_seconds=round(time.time() - t0, 2))
        except Exception:
            tb = traceback.format_exc()
            logger.error("Extract failed", error=tb)
            ddb.update(
                ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": f"SCAN_FAILED_EXTRACT: {tb[:500]}"}
            )
            return 0, total_docs

        # Stage 2: Build
        logger.info("Stage 2: Build")
        ddb.update(ddb_key, {"status": SourceStatus.SCANNING_KG_BUILD})

        try:
            t0 = time.time()
            graph_index.build(extracted_docs, progress_monitor=progress_monitor)
            logger.info("Build complete", elapsed_seconds=round(time.time() - t0, 2))
        except Exception:
            tb = traceback.format_exc()
            logger.error(
                "Build failed (extracted artifacts preserved at s3://%s/%s/%s/)",
                bucket_name,
                extracted_prefix,
                collection_id,
                error=tb,
            )
            ddb.update(ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": f"SCAN_FAILED_BUILD: {tb[:500]}"})
            return 0, total_docs

        ddb.update(ddb_key, {"docsGraphIngested": total_docs})

    return total_docs, 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the knowledge-graph build task (ECS Fargate entrypoint).

    Reads its inputs from environment variables (doc source, namespace, tenant,
    staging prefix, bucket, and status table), loads the staged documents,
    screens them against the retrieval guardrail, and runs the GraphRAG indexing
    pipeline into Neptune + OpenSearch while tracking progress in DynamoDB.

    Idempotent: exits early if the source is already COMPLETED. On a load or
    backend-health failure it marks the source SCAN_FAILED and exits non-zero so
    the Step Functions state machine surfaces the failure.
    """
    start_ts = time.time()

    doc_source_id = _require_env("DOC_SOURCE_ID")
    namespace_id = _require_env("NAMESPACE_ID")
    tenant_id = _require_env("TENANT_ID")
    staging_prefix = _require_env("STAGING_PREFIX")
    bucket_name = _require_env("BUCKET_NAME")
    ddb_table = _require_env("DOC_SOURCES_TABLE")

    try:
        validate_id(namespace_id, "namespace_id")
        validate_id(doc_source_id, "doc_source_id")
    except ValueError as exc:
        logger.error("Input validation failed", error=str(exc))
        sys.exit(1)

    logger.info(
        "KG Build Task started", doc_source_id=doc_source_id, namespace_id=namespace_id, staging_prefix=staging_prefix
    )

    ddb = DynamoDBDAO(ddb_table, region=AWS_REGION)
    ddb_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{doc_source_id}"}

    # Idempotency check
    item = ddb.get(ddb_key)
    if item and item.get("status") == SourceStatus.COMPLETED:
        logger.info("Doc source already completed, skipping")
        return

    # 1. Load. A listing failure raises (transient S3/IAM error must not look
    # like "empty prefix") → mark SCAN_FAILED and exit non-zero. load_failed is
    # the count of listed files that could not be downloaded/decoded.
    try:
        documents, load_failed = _load_staged_documents(bucket_name, staging_prefix)
    except Exception:
        tb = traceback.format_exc()
        logger.error("Failed to load staged documents", staging_prefix=staging_prefix, error=tb)
        ddb.update(ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": f"SCAN_FAILED_LOAD: {tb[:500]}"})
        sys.exit(1)
    if not documents:
        # Genuinely empty prefix (listing succeeded, zero usable docs). If some
        # files were listed but every one failed to download, that's a failure,
        # not an empty source.
        if load_failed > 0:
            logger.error("All staged files failed to load", staging_prefix=staging_prefix, load_failed=load_failed)
            ddb.update(
                ddb_key,
                {
                    "status": SourceStatus.SCAN_FAILED,
                    "errorMessage": (
                        f"SCAN_FAILED_LOAD: all {load_failed} staged file(s) yielded no document "
                        "(download/decode error, or no text content)"
                    ),
                },
            )
            sys.exit(1)
        logger.warning("No staged documents found", staging_prefix=staging_prefix)
        return

    # 2. Setup
    graph_store_uri, vector_store_uri = _setup_graphrag(tenant_id)
    indexing_config = _build_indexing_config(bucket_name, namespace_id, doc_source_id)

    # Build the progress monitor. The toolkit invokes its increment_* methods at
    # document boundaries (single-threaded, main process), and each call atomic-
    # ADDs the count to PK=METRICS#{ns}, SK=KGBUILD#{ds} for live UI progress.
    # No-op monitor when metrics are disabled / toolkit unavailable, so callers
    # pass it unconditionally. Best-effort; never fails the build.
    from coa_sources.documents.kg_build import kg_metrics

    # Clear any leftover metrics record from a prior build first — the per-stage
    # counters accumulate via atomic ADD, so without a reset a rescan would report
    # this run's counts on top of the previous totals.
    kg_metrics.reset_metrics(
        table=ddb_table,
        region=AWS_REGION,
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
    )

    progress_monitor = kg_metrics.make_progress_monitor(
        table=ddb_table,
        region=AWS_REGION,
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
    )

    # Periodic liveness. The toolkit reports progress only at document-batch
    # boundaries (15-25 min apart in practice), so a task that stalls mid-batch
    # goes silent and looks identical to one that is working. The heartbeat also
    # logs RSS, which is the only memory signal available on this cluster.
    heartbeat_stop = kg_metrics.start_heartbeat(namespace_id=namespace_id, doc_source_id=doc_source_id)

    # 2b. Backend health check — fail fast if Neptune or AOSS is unreachable
    if not _check_backend_health(graph_store_uri, vector_store_uri, ddb, ddb_key):
        logger.error("Backend health check failed, aborting KG build")
        sys.exit(1)

    # 2c. Content screening — evaluate document text against retrieval guardrail
    # (MEDIUM PROMPT_ATTACK sensitivity) to quarantine poisoned content before
    # it enters the graph/vector store. Per SDO-188.
    retrieval_guardrail_id = os.environ.get("RETRIEVAL_GUARDRAIL_ID", "")
    retrieval_guardrail_version = os.environ.get("RETRIEVAL_GUARDRAIL_VERSION", "DRAFT")
    docs_quarantined = 0
    if retrieval_guardrail_id:
        from coa_common.guardrail_screener import GuardrailScreener, GuardrailScreenerError

        screener = GuardrailScreener(
            guardrail_id=retrieval_guardrail_id,
            guardrail_version=retrieval_guardrail_version,
            region=AWS_REGION,
        )
        safe_documents = []
        # Screen each document individually. ApplyGuardrail evaluates the entire
        # content list as a single input (not per-block), so batching multiple
        # docs in one call doesn't give per-doc results.
        # Screen in 10k-char windows to catch payloads placed past the API char limit.
        _WINDOW_SIZE = 10000
        for doc in documents:
            text = doc.text
            try:
                quarantined = False
                for start in range(0, max(1, len(text)), _WINDOW_SIZE):
                    window = text[start : start + _WINDOW_SIZE]
                    results = screener.screen_texts([window])
                    if not results[0].passed:
                        quarantined = True
                        docs_quarantined += 1
                        logger.warning(
                            "document_quarantined",
                            filename=doc.metadata.get("filename", "unknown"),
                            reason=results[0].intervention_reason,
                            window_start=start,
                        )
                        break
                if not quarantined:
                    safe_documents.append(doc)
            except GuardrailScreenerError as e:
                # Graceful degradation: if screener fails, allow doc through
                logger.warning(
                    "content_screening_failed", error=str(e), filename=doc.metadata.get("filename", "unknown")
                )
                safe_documents.append(doc)

        if docs_quarantined > 0:
            logger.info("content_screening_complete", quarantined=docs_quarantined, passed=len(safe_documents))
        documents = safe_documents

        if not documents:
            logger.error("All documents quarantined by content screening")
            ddb.update(ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": "All documents quarantined"})
            sys.exit(1)
    else:
        logger.info("content_screening_skipped", reason="RETRIEVAL_GUARDRAIL_ID not set")

    # 3. Run
    try:
        if EXTRACTION_MODE == ExtractionMode.SEPARATED:
            docs_ok, docs_fail = _run_separated(
                documents,
                graph_store_uri,
                vector_store_uri,
                tenant_id,
                indexing_config,
                bucket_name,
                namespace_id,
                doc_source_id,
                ddb,
                ddb_key,
                progress_monitor,
            )
        else:
            docs_ok, docs_fail = _run_continuous(
                documents,
                graph_store_uri,
                vector_store_uri,
                tenant_id,
                indexing_config,
                ddb,
                ddb_key,
                progress_monitor,
            )
    finally:
        # Stop beating once the build is done, so the remaining teardown work
        # (version deletion, metrics stamping) isn't interleaved with liveness
        # lines that no longer mean anything.
        heartbeat_stop.set()

    # Optional: delete previous archived versions from Neptune + AOSS.
    # Only runs when versioning is enabled and DELETE_PREV_VERSIONS=true.
    if DELETE_PREV_VERSIONS and ENABLE_VERSIONING and docs_ok > 0:
        try:
            from graphrag_toolkit.lexical_graph import LexicalGraphIndex
            from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory
            from graphrag_toolkit.lexical_graph.versioning import VersioningConfig, VersioningMode

            logger.info("Deleting previous versions", tenant_id=tenant_id)
            with (
                GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store,
                VectorStoreFactory.for_vector_store(vector_store_uri, index_names=EMBEDDING_INDEXES) as vector_store,
            ):
                graph_index = LexicalGraphIndex(graph_store, vector_store, tenant_id=tenant_id)
                graph_index.delete_sources(versioning_config=VersioningConfig(versioning_mode=VersioningMode.PREVIOUS))
            logger.info("Previous versions deleted", tenant_id=tenant_id)
        except Exception:
            tb = traceback.format_exc()
            logger.warning("Failed to delete previous versions (non-fatal)", error=tb)

    elapsed = round(time.time() - start_ts, 2)

    # Stamp the metrics record header + total run time. The per-stage counts were
    # written live by the ProgressMonitor via atomic ADD as extraction/build
    # progressed; this just records identifying metadata + the run duration.
    # Best-effort, never fatal.
    kg_metrics.write_record_metadata(
        table=ddb_table,
        region=AWS_REGION,
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
        extra={"stage_total_run_ms": round(elapsed * 1000.0, 1)},
    )

    # Fold documents that never loaded (download/decode failures during staging
    # load) into the failure total so partial load loss is not silently reported
    # as full success. Total attempted = docs_ok + docs_fail (build) + load_failed.
    total_fail = docs_fail + load_failed

    if total_fail > 0 and docs_ok == 0:
        logger.error("KG Build failed", docs_failed=docs_fail, load_failed=load_failed, elapsed_seconds=elapsed)
        ddb.update(
            ddb_key,
            {
                "status": SourceStatus.SCAN_FAILED,
                "errorMessage": (
                    f"KG Build failed: {docs_fail} doc(s) failed build, {load_failed} staged file(s) yielded "
                    "no document"
                ),
            },
        )
        sys.exit(1)

    if total_fail > 0:
        logger.warning(
            "KG Build completed with partial failures",
            docs_ok=docs_ok,
            docs_failed=docs_fail,
            load_failed=load_failed,
            elapsed_seconds=elapsed,
        )
        return

    logger.info("KG Build completed", docs=docs_ok, elapsed_seconds=elapsed)


if __name__ == "__main__":
    main()
