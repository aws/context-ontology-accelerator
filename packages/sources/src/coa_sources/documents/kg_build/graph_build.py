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
    NAMESPACE_LANGUAGE       — canonical namespace-language snapshot resolved by the
                               Sources API when the ingestion was triggered. Never
                               re-read from the namespace; a non-``en`` value adds a
                               fixed output-language instruction to the proposition
                               and topic extraction prompts.

  Optional:
    EXTRACTION_MODE          — "continuous" (default) or "separated"
    USE_BATCH_INFERENCE      — "true" (default) or "false" — uses Bedrock batch inference
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
    EXTRACTION_TIMEOUT_SECONDS — Bedrock extraction read timeout (default: 300;
                                valid range: 1–3600 seconds).
    EXTRACTION_CONNECT_TIMEOUT_SECONDS — Bedrock connection timeout (default: 10;
                                valid range: 0.1–300 seconds).
    EXTRACTION_MAX_ATTEMPTS  — extraction attempts after botocore normalization
                                (default: 5; valid range: 1–20).
    EXTRACTION_NUM_THREADS_PER_WORKER — per-process extraction fan-out used to
                                size the HTTP connection pool (default: 4;
                                valid range: 1–256).

Notes:
  AOSS NEXTGEN rejects knn_vector mappings with a 'method.engine' key.
  _patch_graphrag_toolkit_for_aoss_nextgen() works around this at startup.
"""

from __future__ import annotations

import inspect
import json
import math
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
from coa_common.language_tags import describe_language, needs_language_instruction
from coa_common.logging import setup_logging
from coa_common.s3 import get_s3_client, list_objects, read_file_bytes
from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.documents.kg_build import batch_patches
from coa_sources.namespace_language import NAMESPACE_LANGUAGE_ENV, require_language_snapshot

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


def _env_str_list(name: str, default_json: str) -> list[str]:
    """Decode a JSON-array-of-strings env var into a cleaned ``list[str]``.

    Entries are stripped and empties dropped. Raises on malformed or wrong-typed
    JSON, so a pipeline bug surfaces as SCAN_FAILED instead of extracting with no
    vocabulary. Over-cap input clamps to ``MAX_VOCABULARY_ENTRIES`` with a warning.
    """
    raw = os.environ.get(name, default_json)
    try:
        decoded = json.loads(raw) if raw else []
        if not isinstance(decoded, list) or not all(isinstance(x, str) for x in decoded):
            raise ValueError("must be a JSON array of strings")
    except (ValueError, json.JSONDecodeError) as exc:
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
# graphrag-toolkit default" (SentenceSplitter 256/25). A malformed value degrades
# to that default with a logged warning instead of crashing at import.
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


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a bounded integer environment variable or fail with its name and value."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]; got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]; got {raw!r}")
    return value


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Read a finite bounded float environment variable or fail explicitly."""
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number in [{minimum}, {maximum}]; got {raw!r}") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be finite and in [{minimum}, {maximum}]; got {raw!r}")
    return value


# Max OUTPUT tokens for the extraction LLM. The toolkit defaults a bare model
# string to 4096, which truncates extraction mid-response on dense chunks and
# silently drops that chunk's trailing entities/statements. 16384 matches the
# graphrag-toolkit benchmark harness. Env-overridable without a deploy.
EXTRACTION_MAX_TOKENS = int(os.environ.get("EXTRACTION_MAX_TOKENS", "16384"))

# Read timeout for a single extraction call, in seconds. BedrockConverse
# defaults to 60, too short for large Sonnet extractions. 300 is headroom for an
# outlier and costs nothing when calls return promptly.
EXTRACTION_TIMEOUT_SECONDS = _bounded_float_env(
    "EXTRACTION_TIMEOUT_SECONDS",
    300,
    minimum=1,
    maximum=3600,
)

# Attempts per extraction call, including the first. 5 covers a throttle window
# without turning one slow chunk into a long stall.
EXTRACTION_MAX_ATTEMPTS = _bounded_int_env(
    "EXTRACTION_MAX_ATTEMPTS",
    5,
    minimum=1,
    maximum=20,
)

# Connect timeout, separate from the read timeout: BedrockConverse sets both to
# one value, so a long read budget would also delay detecting an unreachable endpoint.
EXTRACTION_CONNECT_TIMEOUT_SECONDS = _bounded_float_env(
    "EXTRACTION_CONNECT_TIMEOUT_SECONDS",
    10,
    minimum=0.1,
    maximum=300,
)

# Per-process fan-out (the toolkit's own default is 4), read only to size the
# connection pool; the toolkit still owns the concurrency.
EXTRACTION_NUM_THREADS_PER_WORKER = _bounded_int_env(
    "EXTRACTION_NUM_THREADS_PER_WORKER",
    4,
    minimum=1,
    maximum=256,
)

# Vector indexes to build. ``topic`` is embedded because ``TopicBeamSearch``
# seeds from ``get_index('topic')`` and skips topics with no stored vector.
# ``statement`` is not, because nothing reads the statement vector index
# (retrievers reach statements through the graph). Takes effect on a fresh
# ingest only. Env-overridable, e.g. EMBEDDING_INDEXES=chunk,statement,topic.
EMBEDDING_INDEXES = [
    name.strip() for name in os.environ.get("EMBEDDING_INDEXES", "chunk,topic").split(",") if name.strip()
]


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        logger.error("Missing required environment variable", key=key)
        sys.exit(1)
    return value


try:
    from llama_index.llms.bedrock_converse import BedrockConverse as _BedrockConverse

    _BEDROCK_CONVERSE_INIT_PARAMS = frozenset(inspect.signature(_BedrockConverse.__init__).parameters) - {"self"}

    class _PicklableBedrockConverse(_BedrockConverse):  # type: ignore[valid-type,misc]
        """BedrockConverse whose ``__setstate__`` actually reconstructs the boto3 client.

        See :func:`_picklable_bedrock_converse_cls` for why this exists.
        """

        def __setstate__(self, state: dict) -> None:
            """Re-init from the pickled fields ``__init__`` accepts, dropping the rest."""
            if not isinstance(state, dict):
                raise TypeError("BedrockConverse pickle state must be a mapping")
            serialized_fields = state.get("__dict__")
            if not isinstance(serialized_fields, dict):
                raise TypeError("BedrockConverse pickle state must contain a '__dict__' mapping")
            kwargs = {k: v for k, v in serialized_fields.items() if k in _BEDROCK_CONVERSE_INIT_PARAMS}
            try:
                self.__init__(**kwargs)  # type: ignore[misc]
            except (TypeError, ValueError) as exc:
                # Do not log values here: the accepted fields may include
                # credentials or other deployment configuration.
                accepted_fields = ", ".join(sorted(kwargs))
                raise TypeError(
                    f"Could not reconstruct BedrockConverse from accepted pickle fields: {accepted_fields or '<none>'}"
                ) from exc

    _picklable_bedrock_converse_class: type | None = _PicklableBedrockConverse
except ImportError:  # pragma: no cover - unit tests stub this module out
    # Only the kg-build image installs llama_index. Leaving the class undefined keeps
    # importing graph_build possible everywhere else (and in tests that stub the
    # module); _picklable_bedrock_converse_cls() then raises with the reason.
    _picklable_bedrock_converse_class = None


def _picklable_bedrock_converse_cls() -> type:
    """A BedrockConverse subclass that keeps its boto3 client across a ProcessPool hop.

    Upstream drops the unpicklable ``_client`` on pickle and cannot rebuild it,
    because ``__init__`` rejects inherited LLM fields, so the worker would get an
    LLM with no client. Filtering the state to the parameters ``__init__`` accepts
    lets it rebuild the client from our botocore_config. Defined at module level
    so pickle can find it under every start method; ``None`` when llama_index is
    stubbed (unit tests).
    """
    if _picklable_bedrock_converse_class is None:  # pragma: no cover - import guard
        raise RuntimeError(
            "llama_index.llms.bedrock_converse was not importable at module load, "
            "so the picklable extraction LLM class could not be defined"
        )
    return _picklable_bedrock_converse_class


def _load_staged_documents(bucket: str, staging_prefix: str) -> tuple[list, int]:
    """Download staged text files + metadata sidecars from S3.

    Returns ``(documents, unloaded_count)`` where ``unloaded_count`` is the number
    of staged text files that were listed but produced no document — either they
    could not be downloaded / decoded, or they held no text at all.

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


_graphrag_llm_client_patched = False


def _patch_graphrag_llm_cache_client_config() -> None:
    """Bound the toolkit's own fallback client, for when ours does not reach the worker.

    Defence in depth behind ``_picklable_bedrock_converse_cls``. When the LLM
    arrives without a client, ``LLMCache.predict`` builds one from llm_cache's
    module constants (``TIMEOUT = 60.0``, ``MAX_ATTEMPTS = 2``), so the configured
    timeout and retries would never apply. This rebinds those constants. The
    rebind reaches workers under ``fork`` only, and llm_cache uses one value for
    both connect and read timeouts.

    Separate flag + narrow import so an ImportError here cannot disable the other
    patches. Must run BEFORE graphrag is imported elsewhere, like its siblings.
    """
    global _graphrag_llm_client_patched
    if _graphrag_llm_client_patched:
        return
    import graphrag_toolkit.lexical_graph.utils.llm_cache as _llm_cache

    _llm_cache.TIMEOUT = EXTRACTION_TIMEOUT_SECONDS
    _llm_cache.MAX_ATTEMPTS = EXTRACTION_MAX_ATTEMPTS
    logger.info(
        "Patched graphrag llm_cache client config",
        timeout=EXTRACTION_TIMEOUT_SECONDS,
        max_attempts=EXTRACTION_MAX_ATTEMPTS,
    )
    _graphrag_llm_client_patched = True


# Where the language instruction goes in each toolkit prompt: immediately before
# the first block that carries document-derived (untrusted) content, so it stays
# with the trusted instructions and never inside the data the model is told to
# read. Keyed by the ExtractionConfig parameter the rewritten template feeds.
_LANGUAGE_INSTRUCTION_ANCHORS: dict[str, tuple[str, str]] = {
    "extract_propositions_prompt_template": ("EXTRACT_PROPOSITIONS_PROMPT", "<sourceInformation>"),
    "extract_topics_prompt_template": ("EXTRACT_TOPICS_PROMPT", "<propositions>"),
}

# The same rule for the entity-classification inference sweep, keyed by the
# InferClassificationsConfig parameter: the instruction goes ahead of the sample
# chunks the model reads. The toolkit's ranking prompt is not listed because
# InferClassificationsConfig has no field for it and LexicalGraphIndex does not
# pass one to InferClassifications.
_CLASSIFICATION_LANGUAGE_INSTRUCTION_ANCHORS: dict[str, tuple[str, str]] = {
    "prompt_template": ("DOMAIN_ENTITY_CLASSIFICATIONS_PROMPT", "Sample text chunks:"),
}


def _extraction_language_instruction(namespace_language: str) -> str:
    """Fixed output-language instruction for the GraphRAG extraction prompts.

    Only the canonical tag and a name from a fixed table are interpolated. The
    text contains no braces: the toolkit renders these templates with
    ``str.format``.
    """
    target = describe_language(namespace_language)
    return f"""# Output Language (REQUIRED)

Write your output in {target}, the language of this knowledge graph.

- The response format is a fixed template read by a program. Keep its fixed labels \
(`topic:`, `entities:`, `proposition:`, `entity-entity relationships:`, `entity-attributes:`), \
colons, `|` separators and layout exactly as written, in English. Write only the values that \
fill its placeholders in {target}, using the words the source text uses. The classification \
after the `|` separator is a value: write it in {target}.
- Relationship and attribute names keep the uppercase-with-underscores format, written in \
{target}. Uppercase applies only to letters that have case, and an underscore stands only \
where the name would otherwise contain a space.
- Treat English preferred topics and classifications as concepts, not spellings; name the \
matching concept in {target}. Do not append an English gloss.
- Copy identifiers exactly as printed, whatever script they use: model, type, part, drawing \
and option numbers; standard and regulation designations; company, brand and product names; \
signal and terminal names; units; codes and IRIs. An identifier that is ASCII in the source \
stays ASCII.
- These instructions are in English for your benefit. Do not echo them, and do not treat \
them as evidence about the language of the output.

"""


def _language_instructed_templates(anchors: dict[str, tuple[str, str]], namespace_language: str) -> dict[str, str]:
    """Toolkit templates from ``anchors`` with the language instruction inserted.

    Empty for a namespace whose primary subtag is ``en``. Otherwise each listed
    toolkit template gains the fixed instruction immediately before its anchor.

    Raises:
        RuntimeError: If a toolkit template no longer contains its anchor, so a
            toolkit upgrade cannot silently move the instruction into (or drop
            it from) the document-content portion.
    """
    if not needs_language_instruction(namespace_language):
        return {}

    from graphrag_toolkit.lexical_graph.indexing import prompts

    instruction = _extraction_language_instruction(namespace_language)
    overrides: dict[str, str] = {}
    for parameter, (template_name, anchor) in anchors.items():
        template: str = getattr(prompts, template_name)
        position = template.find(anchor)
        if position < 0:
            raise RuntimeError(
                f"graphrag-toolkit {template_name} has no {anchor} block to anchor the language instruction"
            )
        overrides[parameter] = template[:position] + instruction + template[position:]
    return overrides


def _namespace_language_prompts(namespace_language: str) -> dict[str, str]:
    """ExtractionConfig prompt overrides for a namespace language.

    Empty for a namespace whose primary subtag is ``en``: English and legacy
    namespaces keep the toolkit prompts unchanged. Otherwise both the
    proposition and topic templates gain the fixed instruction, placed ahead of
    their document-content blocks.

    Raises:
        RuntimeError: If a toolkit template no longer contains its anchor.
    """
    return _language_instructed_templates(_LANGUAGE_INSTRUCTION_ANCHORS, namespace_language)


def _classification_inference_prompts(namespace_language: str) -> dict[str, str]:
    """InferClassificationsConfig prompt override for a namespace language.

    Empty for a namespace whose primary subtag is ``en``. Otherwise the
    toolkit's domain-classification inference template gains the fixed
    instruction ahead of its sample chunks, so the inferred class vocabulary is
    named in the namespace language.

    Raises:
        RuntimeError: If the toolkit template no longer contains its anchor.
    """
    return _language_instructed_templates(_CLASSIFICATION_LANGUAGE_INSTRUCTION_ANCHORS, namespace_language)


_graphrag_aoss_patched = False


def _patch_graphrag_toolkit_for_aoss_nextgen(*, allow_create: bool = True) -> None:
    """Patch graphrag_toolkit's index_exists to drop the unsupported 'engine' field.

    AOSS NEXTGEN rejects knn_vector mappings with a 'method.engine' key. No-op in
    unit tests (stubs don't expose the full module path).

    ``allow_create=False`` never creates a missing index, only reports whether it
    exists. The DELETION task passes it: graphrag would otherwise recreate empty
    ``chunk_*``/``topic_*`` indexes for an already-empty tenant, and AOSS caps a
    collection at 1000 indexes.
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

    # Reassigning the typed method is the point of the patch ([method-assign]).
    _osvs.OpensearchVectorClient._bulk_ingest_embeddings = _retrying_bulk_ingest_embeddings  # type: ignore[method-assign]
    _graphrag_bulk_ingest_patched = True


def _setup_graphrag(tenant_id: str):
    """Configure GraphRAG and return (graph_store_uri, vector_store_uri)."""
    # Patch BEFORE importing graphrag — the monkey-patch must replace index_exists
    # in the module namespace before LexicalGraphIndex/VectorStoreFactory load and
    # bind their own references to it. Importing GraphRAGConfig triggers those loads.
    _patch_graphrag_toolkit_for_aoss_nextgen()
    _patch_graphrag_paginated_search_retry()
    _patch_graphrag_llm_cache_client_config()

    from graphrag_toolkit.lexical_graph import GraphRAGConfig

    # AFTER the import: graphrag binds its own _bulk_ingest_embeddings onto
    # llama_index's OpensearchVectorClient at import time, so our retry wrapper
    # must be assigned last to win.
    _patch_graphrag_bulk_ingest_retry()
    # AFTER the import: wraps get_request_body in every module that binds it so
    # batch serialization omits temperature for models that reject it (default is
    # Sonnet 5). Safe to run post-import — the serializer is read at job build.
    # Lives in the thin batch_patches module so spawn-started workers that import
    # the initializer do not pull all of graph_build with them.
    batch_patches._patch_graphrag_batch_request_body()
    # AFTER the import: the serializer patch above lives only in THIS process, but
    # extraction transforms run in spawn-started workers that re-import the toolkit
    # fresh. Route those workers through our initializer so the patch re-applies in
    # each one; otherwise the no-temp KeyError resurfaces per worker.
    batch_patches._patch_graphrag_worker_init()

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
        # Pass the extraction LLM as an INSTANCE: to_llm() reads only
        # model/temperature/max_tokens/region_name/profile_name from the JSON form
        # and drops the timeout, leaving BedrockConverse's 60s default.
        #
        # max_tokens: the toolkit hardcodes 4096 for a bare model string, which
        # truncates extraction mid-response on dense chunks.
        #
        # max_retries bounds llama_index's retry layer, which sits on top of
        # botocore's. The layers multiply for throttling and 5xx; a read timeout is
        # retried by botocore only, so a slow chunk costs at most
        # (EXTRACTION_MAX_ATTEMPTS + 1) * EXTRACTION_TIMEOUT_SECONDS.
        #
        # Must stay picklable (ProcessPoolExecutor); see
        # _picklable_bedrock_converse_cls. region_name is explicit because a worker
        # has no session to inherit it from. temperature is discarded by llama_index
        # for models in BEDROCK_NO_TEMP_MODELS, which Bedrock rejects it for.
        #
        # The pool is sized to the per-process fan-out: botocore defaults to 10 and
        # excess calls open throwaway connections. Retries mode is explicit because
        # supplying botocore_config reverts an omitted `retries` to the legacy
        # default; standard, not adaptive, because each process has its own client.
        from botocore.config import Config

        extraction_pool_size = max(EXTRACTION_NUM_THREADS_PER_WORKER, 10)

        GraphRAGConfig.extraction_llm = _picklable_bedrock_converse_cls()(
            model=BEDROCK_MODEL_ARN,
            temperature=0.0,
            max_tokens=EXTRACTION_MAX_TOKENS,
            timeout=EXTRACTION_TIMEOUT_SECONDS,
            max_retries=EXTRACTION_MAX_ATTEMPTS,
            region_name=AWS_REGION,
            botocore_config=Config(
                max_pool_connections=extraction_pool_size,
                connect_timeout=EXTRACTION_CONNECT_TIMEOUT_SECONDS,
                read_timeout=EXTRACTION_TIMEOUT_SECONDS,
                retries={"max_attempts": EXTRACTION_MAX_ATTEMPTS, "mode": "standard"},
                # Supplying a Config replaces upstream's entirely, which would drop
                # the marker it sets here.
                user_agent_extra="x-client-framework:llama_index",
            ),
        )
    GraphRAGConfig.local_output_dir = CHECKPOINT_DIR
    if USE_BATCH_INFERENCE:
        GraphRAGConfig.extraction_batch_size = EXTRACTION_BATCH_SIZE

    neptune_endpoint = _require_env("NEPTUNE_ENDPOINT")
    opensearch_endpoint = _require_env("OPENSEARCH_ENDPOINT")

    neptune_host = neptune_endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
    opensearch_host = opensearch_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    graph_store_uri = f"neptune-db://{neptune_host}:8182"
    vector_store_uri = f"aoss://{opensearch_host}:443"

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
        extraction_timeout_seconds=EXTRACTION_TIMEOUT_SECONDS,
        extraction_connect_timeout_seconds=EXTRACTION_CONNECT_TIMEOUT_SECONDS,
        extraction_max_attempts=EXTRACTION_MAX_ATTEMPTS,
        extraction_pool_size=max(EXTRACTION_NUM_THREADS_PER_WORKER, 10),
        chunk_size="256 (toolkit default)",
    )
    return graph_store_uri, vector_store_uri


def _build_indexing_config(bucket_name: str, namespace_id: str, doc_source_id: str, *, namespace_language: str):
    """Build the IndexingConfig handed to LexicalGraphIndex.

    Always returns an IndexingConfig with a fully-specified ExtractionConfig,
    never None, so the toolkit's news/finance default classifications are not
    inherited.

    Vocabulary: an explicit ``PREFERRED_ENTITY_CLASSIFICATIONS`` wins and no
    inference runs; otherwise ``INFER_ENTITY_CLASSIFICATIONS`` (default) infers
    labels from the corpus; with both off the extractor runs unguided.
    ``PREFERRED_TOPICS`` steers topic grouping (empty lets the model name topics).
    ``CHUNK_SIZE`` / ``CHUNK_OVERLAP`` of 0 use the toolkit's default splitter
    (256/25); positive values switch to that splitter.
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
        # Corpus-inferred vocabulary: an empty ``preferred_entity_classifications``
        # is read as the seed vocabulary, and ``replace_default_classifications=True``
        # replaces it with labels inferred from this corpus. A non-English namespace
        # also passes the inference prompt with the language instruction.
        preferred = []
        infer_config = InferClassificationsConfig(
            replace_default_classifications=True,
            **_classification_inference_prompts(namespace_language),
        )
    else:
        # Both off → unguided extraction. Logged loudly, since this is the
        # only configuration where the LLM types entities from scratch.
        logger.warning(
            "Both PREFERRED_ENTITY_CLASSIFICATIONS and INFER_ENTITY_CLASSIFICATIONS are off — "
            "extraction runs unguided. Set one of them to steer entity typing."
        )
        preferred = []
        infer_config = False

    extraction_config = ExtractionConfig(
        enable_proposition_extraction=ENABLE_PROPOSITION_EXTRACTION,
        preferred_entity_classifications=preferred,
        infer_entity_classifications=infer_config,
        preferred_topics=PREFERRED_TOPICS,
        **_namespace_language_prompts(namespace_language),
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
        namespace_language=namespace_language,
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


def _warn_if_no_topics(graph_store_uri: str, tenant_id: str, doc_source_id: str, docs: int) -> None:
    """Log ``kg_build_zero_topics_extracted`` when a finished build left the tenant with no topics.

    An extraction reply the toolkit cannot parse (for example translated response labels)
    yields no topics and no error, so the source would otherwise end COMPLETED with an
    empty graph. Best-effort: never fails the build.

    ponytail: counts the whole tenant, so an empty source in a namespace that already has
    topics from a *different* source is not flagged. The caller runs this after
    DELETE_PREV_VERSIONS, so a re-ingest that regresses to zero is no longer masked by the
    source's own prior good version. Scoping to this source's current version
    (``__EXTRACTED_FROM__``/``__MENTIONED_IN__`` as the toolkit's delete_sources.py does, keyed
    on the toolkit-assigned sourceId — not doc_source_id) would close the cross-source case too.
    """
    from graphrag_toolkit.lexical_graph import TenantId
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory

    try:
        tenant = TenantId(tenant_id)
        with GraphStoreFactory.for_graph_store(graph_store_uri) as graph_store:
            topics = graph_store.execute_query(f"MATCH (t:{tenant.format_label('__Topic__')}) RETURN count(t) AS n")
            if topics[0]["n"] != 0:
                return
            chunks = graph_store.execute_query(f"MATCH (c:{tenant.format_label('__Chunk__')}) RETURN count(c) AS n")
        logger.warning(
            "kg_build_zero_topics_extracted",
            doc_source_id=doc_source_id,
            docs=docs,
            chunks=chunks[0]["n"],
            hint="extraction replies were not parsed; check the response format labels in the model output",
        )
    except Exception as e:  # noqa: BLE001 — diagnostics must never fail the build
        logger.debug("kg_build_zero_topics_check_failed", error=type(e).__name__)


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
            ddb.update(
                ddb_key,
                {"status": SourceStatus.SCAN_FAILED, "errorMessage": _stage_failure_message("SCAN_FAILED_BUILD")},
            )
            return 0, total_docs


# Client-facing text per failure stage. errorMessage is returned to callers by
# GetSource, so it carries the stage code and a fixed explanation; the traceback
# goes to the log only (it names hosts, ARNs and internal paths).
_STAGE_FAILURE_TEXT: dict[str, str] = {
    "SCAN_FAILED_BUILD": "the knowledge-graph build failed",
    "SCAN_FAILED_EXTRACT": "entity extraction failed",
    "SCAN_FAILED_LOAD": "the staged documents could not be loaded",
    "SCAN_FAILED_GRAPH_STORE": "the graph store health check failed",
    "SCAN_FAILED_VECTOR_STORE": "the vector store health check failed",
}


def _stage_failure_message(code: str) -> str:
    """Return ``"<code>: <fixed explanation>; see the service logs for details."``."""
    return f"{code}: {_STAGE_FAILURE_TEXT.get(code, 'processing failed')}; see the service logs for details."


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
                "errorMessage": _stage_failure_message("SCAN_FAILED_GRAPH_STORE"),
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
                "errorMessage": _stage_failure_message("SCAN_FAILED_VECTOR_STORE"),
            },
        )
        return False

    return True


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
                ddb_key,
                {"status": SourceStatus.SCAN_FAILED, "errorMessage": _stage_failure_message("SCAN_FAILED_EXTRACT")},
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
            ddb.update(
                ddb_key,
                {"status": SourceStatus.SCAN_FAILED, "errorMessage": _stage_failure_message("SCAN_FAILED_BUILD")},
            )
            return 0, total_docs

        ddb.update(ddb_key, {"docsGraphIngested": total_docs})

    return total_docs, 0


# main() screens each document with one ApplyGuardrail call per window of its
# text (10 guardrail text units per window).
_SCREEN_WINDOW_SIZE = 10_000
# Neighbouring windows share this many chars, so any span up to this length lies
# wholly inside at least one window and cannot be split across two windows that
# each pass the guardrail.
_SCREEN_WINDOW_OVERLAP = 1_000
_SCREEN_WINDOW_STEP = _SCREEN_WINDOW_SIZE - _SCREEN_WINDOW_OVERLAP


def _screening_windows(length: int) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` slices that content screening sends for a text of ``length`` chars.

    Windows are ``_SCREEN_WINDOW_SIZE`` chars long and start every
    ``_SCREEN_WINDOW_STEP`` chars, so neighbours overlap by
    ``_SCREEN_WINDOW_OVERLAP``. Starts stop once a window reaches the end of the
    text, so the last window always ends at ``length`` and no window re-screens
    only a suffix the previous one already covered. A text no longer than one
    window, including the empty text, gets exactly one window.
    """
    starts = range(0, max(1, length - _SCREEN_WINDOW_OVERLAP), _SCREEN_WINDOW_STEP)
    return [(start, min(start + _SCREEN_WINDOW_SIZE, length)) for start in starts]


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

    ddb = DynamoDBDAO(ddb_table, region=AWS_REGION)
    ddb_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{doc_source_id}"}

    # Idempotency check
    item = ddb.get(ddb_key)
    if item and item.get("status") == SourceStatus.COMPLETED:
        logger.info("Doc source already completed, skipping")
        return

    # The job's namespace-language snapshot. A missing or invalid value fails the
    # build rather than defaulting to English or re-reading the namespace;
    # re-scanning the source resolves the language again.
    try:
        namespace_language = require_language_snapshot(os.environ.get(NAMESPACE_LANGUAGE_ENV))
    except ValueError as exc:
        logger.error("Namespace language snapshot invalid", error=str(exc))
        ddb.update(ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": f"SCAN_FAILED_CONFIG: {exc}"})
        sys.exit(1)

    logger.info(
        "KG Build Task started",
        doc_source_id=doc_source_id,
        namespace_id=namespace_id,
        namespace_language=namespace_language,
        staging_prefix=staging_prefix,
    )

    # 1. Load. A listing failure raises (transient S3/IAM error must not look
    # like "empty prefix") → mark SCAN_FAILED and exit non-zero. load_failed is
    # the count of listed files that could not be downloaded/decoded.
    try:
        documents, load_failed = _load_staged_documents(bucket_name, staging_prefix)
    except Exception:
        tb = traceback.format_exc()
        logger.error("Failed to load staged documents", staging_prefix=staging_prefix, error=tb)
        ddb.update(
            ddb_key, {"status": SourceStatus.SCAN_FAILED, "errorMessage": _stage_failure_message("SCAN_FAILED_LOAD")}
        )
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
    indexing_config = _build_indexing_config(
        bucket_name, namespace_id, doc_source_id, namespace_language=namespace_language
    )

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
    # it enters the graph/vector store.
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
        for doc in documents:
            text = doc.text
            try:
                quarantined = False
                for start, end in _screening_windows(len(text)):
                    results = screener.screen_texts([text[start:end]])
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

    # Counts topics only after prior archived versions are deleted, so a re-ingest
    # that regresses to zero topics is not masked by the previous good version.
    if docs_ok > 0:
        _warn_if_no_topics(graph_store_uri, tenant_id, doc_source_id, docs_ok)

    elapsed = round(time.time() - start_ts, 2)

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
