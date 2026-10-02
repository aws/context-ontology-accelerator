# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unified Ontology Workbench API.

Single FastAPI application providing:
  - Ontology catalog (CRUD + embeddings) backed by Neptune Analytics
  - Ontology induction from datasource catalogs via Bedrock Titan embeddings
  - Multi-tier ontology validation

Run:
    uvicorn app.main:app --port 8000
"""

import logging
import os

import defusedxml

# ── Workbench-specific ──────────────────────────────────────────────────
from coa_common import DEFAULT_EMBED_MODEL_ID
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from coa_ontology import induce_catalog
from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

# ── Catalog ─────────────────────────────────────────────────────────────
from coa_ontology.catalog.routers import (
    embeddings,
    foundational,
    graph,
    ontologies,
)
from coa_ontology.inducer import schemas as inducer_schemas

# ── Inducer ─────────────────────────────────────────────────────────────
from coa_ontology.inducer.routers import induce, induce_unstructured
from coa_ontology.inducer.services.data_catalog import CatalogTable
from coa_ontology.inducer.services.llm import LLMClient
from coa_ontology.proposals import router as proposals_router

# ── Pluggable backends (vector + graph abstractions) ────────────────────
from coa_ontology.stores import build_stores
from coa_ontology.stores.adapters import StoreOntologyCatalogAdapter

# ── Validation ──────────────────────────────────────────────────────────
from coa_ontology.validation.routers import validate

# ── Security: XXE protection ────────────────────────────────────────────
# Patch Python's XML parsers (xml.sax, xml.etree, xml.dom, xml.minidom)
# to reject external entity resolution and DTD expansion. Must happen BEFORE
# any XML parsing occurs. This mitigates XXE vulnerabilities in ontology
# ingest (RDF/XML format) where rdflib internally uses xml.sax. See CWE-502.
defusedxml.defuse_stdlib()

# ── Logging ─────────────────────────────────────────────────────────────
# The service runs under a bare ``uvicorn coa_ontology.main:app``
# command, which configures only uvicorn's own ``uvicorn.*`` loggers. Without
# this, every module logger (``logging.getLogger(__name__)``) inherits the
# root logger — WARNING level, no handler — so application INFO/WARNING logs
# (ingest pipeline, embedding-readiness waits, proposal-accept transitions,
# background worker threads) are silently dropped and never reach CloudWatch.
# Configure the root logger once at import time (uvicorn imports this module)
# so all module loggers emit to stdout. Level tunable via ``LOG_LEVEL``.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,  # override any handler uvicorn/root already installed
)

logger = logging.getLogger(__name__)

# ── App ─────────────────────────────────────────────────────────────────
# Orphaned-job recovery is fully on-demand — the lazy ``get_job`` sweep, the
# ``count_active_jobs`` self-heal, and ``acquire_induction_lock``'s stale-takeover
# each recover a hard-killed worker's row without a boot-time scan, so no
# startup-reconcile lifespan is wired here.
app = FastAPI(
    title="Ontology Workbench",
    description=(
        "Unified API for ontology catalog management, induction from "
        "structured/unstructured sources, and multi-tier validation.\n\n"
        "Embeddings are generated directly via **Amazon Bedrock Cohere Embed "
        "v4** — no separate embedding-generator service needed."
    ),
    version="1.0.0",
)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request: StarletteRequest, exc: RequestValidationError) -> JSONResponse:
    """Return the failing fields WITHOUT echoing the caller-supplied input.

    FastAPI's default handler leaks the offending value through TWO channels:

    1. An ``input`` field on every error entry — stripped here directly.
    2. The ``msg`` field, when Pydantic's custom validators
       (``@field_validator`` / ``@model_validator``) raise ``ValueError`` /
       ``AssertionError`` — Pydantic uses the exception text verbatim as
       ``msg``, and a validator that interpolates the rejected value into
       its message text (a common pattern, e.g.
       ``ValueError(f"Bad: {v!r}")``) reflects that value into the response
       body regardless of what happens to ``input``.

    Pydantic's built-in type-level errors (``float_parsing``, ``missing``,
    ``string_pattern_mismatch``, …) carry a fixed ``msg`` that never
    contains caller-supplied content, so we keep it — it's what tells a
    legitimate caller how to fix their payload. Custom-validator errors
    (``value_error`` / ``assertion_error``) are the ones whose ``msg`` is
    untrusted; those get an opaque replacement and rely on ``type`` +
    ``loc`` to say what's wrong.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": err.get("type"),
                    "loc": err.get("loc"),
                    "msg": ("Invalid value" if err.get("type") in _CUSTOM_VALIDATOR_ERROR_TYPES else err.get("msg")),
                }
                for err in exc.errors()
            ],
        },
    )


# Pydantic error ``type`` strings emitted when a custom validator raises
# ``ValueError`` / ``AssertionError``. The ``msg`` for these entries is the
# exception text verbatim, and any validator that interpolates the rejected
# value into that text leaks it back to the caller — a channel that survives
# stripping the ``input`` field. Adding a new custom-validator error type
# upstream would be caught by ``test_custom_validator_msg_is_not_echoed``.
_CUSTOM_VALIDATOR_ERROR_TYPES = frozenset({"value_error", "assertion_error"})


# ── Config ──────────────────────────────────────────────────────────────
# After MR !39 consolidation, the ontology-catalog + embedding-generator
# routes are served by this same FastAPI app. Default these URLs to
# localhost:8001 (the consolidated engine's default port) so internal
# self-calls resolve without requiring Docker-Compose DNS. Override via
# env vars for multi-service deployments if you ever bring them back.
_DEFAULT_ENGINE_SELF_URL = "http://localhost:8001"
app.state.config = {
    "data_catalog_url": os.getenv("DATA_CATALOG_URL", "http://localhost:8003"),
    "embedding_generator_url": os.getenv("EMBEDDING_GENERATOR_URL", _DEFAULT_ENGINE_SELF_URL),
    "ontology_catalog_url": os.getenv("ONTOLOGY_CATALOG_URL", _DEFAULT_ENGINE_SELF_URL),
    "llm_model_id": os.getenv("LLM_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
    "llm_region": os.getenv("LLM_REGION", "us-east-1"),
    "description_llm_model_id": os.getenv("DESCRIPTION_LLM_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
    "description_mode": os.getenv("DESCRIPTION_MODE", "schema"),
    "neptune_analytics_url": os.getenv("NEPTUNE_ANALYTICS_URL", "http://localhost:9200"),
    "neptune_endpoint": os.getenv("NEPTUNE_ENDPOINT", os.getenv("NDB_ENDPOINT", "")),
    "bedrock_region": os.getenv("BEDROCK_REGION", os.getenv("LLM_REGION", "us-east-1")),
    "bedrock_embed_model_id": os.getenv("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID),
    "bedrock_embed_dimensions": int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024")),
    "catalog_source": os.getenv("CATALOG_SOURCE", "http"),
    "smus_domain_id": os.getenv("SMUS_DOMAIN_ID", ""),
    "namespaces_table": os.getenv("NAMESPACES_TABLE", ""),
    "smus_datasources_table": os.getenv("DATASOURCES_TABLE", ""),
    "sources_table": os.getenv("SOURCES_TABLE", ""),
    "smus_region": os.getenv("AWS_REGION", "us-east-1"),
}


# ── Bedrock embedding patch ─────────────────────────────────────────────
def _make_bedrock_client(config: dict) -> BedrockEmbeddingClient:
    return BedrockEmbeddingClient(
        region=config.get("bedrock_region", "us-east-1"),
        model_id=config.get("bedrock_embed_model_id", DEFAULT_EMBED_MODEL_ID),
        dimensions=config.get("bedrock_embed_dimensions", 1024),
    )


def _make_description_llm_client(config: dict) -> LLMClient:
    """Construct the description-generator LLM client from app config.

    Mirrors ``_make_bedrock_client`` so the readiness probe and the
    unstructured induction worker construct the client the same way. The
    description LLM uses its own ``description_llm_model_id`` config key
    (independent of ``llm_model_id``) so the description generator and any
    future entailment LLM can run on different model versions.
    """
    return LLMClient(
        model_id=config.get("description_llm_model_id", "us.anthropic.claude-sonnet-4-6"),
        region=config.get("llm_region", "us-east-1"),
    )


_orig_build_pipeline = getattr(induce, "_orig_build_pipeline", induce._build_pipeline)
# Stable handle across reloads — stored as a module attr so idempotent reloads work.
induce._orig_build_pipeline = _orig_build_pipeline  # type: ignore[attr-defined]

# Pick the configured (graph, vector) pair once at startup.
_graph_store, _vector_store = build_stores()
_store_adapter = StoreOntologyCatalogAdapter(_graph_store, _vector_store)
app.state.graph_store = _graph_store
app.state.vector_store = _vector_store


def _patched_build_pipeline(config: dict):
    pipeline = _orig_build_pipeline(config)
    pipeline.eg = _make_bedrock_client(config)
    # Route all ontology-catalog / embedding traffic through the configured
    # backend instead of the legacy HTTP ontology-catalog service.
    pipeline.oc = _store_adapter
    return pipeline


induce._build_pipeline = _patched_build_pipeline

# Wire induce_catalog with pipeline builder and schemas.
# These are module-level attribute injections declared as Optional in induce_catalog;
# mypy sees the targets as None but we're setting them at import time. The module's
# runtime code uses these after main.py has imported and patched them.
induce_catalog._build_pipeline = _patched_build_pipeline  # type: ignore[assignment]
induce_catalog._schemas_mod = inducer_schemas  # type: ignore[assignment]
induce_catalog._catalog_table_cls = CatalogTable  # type: ignore[assignment]


# ── Routes ──────────────────────────────────────────────────────────────
app.include_router(ontologies.router, prefix="/ontologies", tags=["ontologies"])
app.include_router(foundational.router, prefix="/ontology", tags=["foundational"])
app.include_router(embeddings.router, prefix="/embeddings", tags=["embeddings"])
app.include_router(graph.router, prefix="/graph", tags=["graph"])
app.include_router(induce_catalog.router, prefix="/induce", tags=["induction"])
app.include_router(
    induce_unstructured.router,
    prefix="/induce/unstructured",
    tags=["induction-unstructured"],
)
app.include_router(proposals_router, prefix="/ontology/proposals", tags=["proposals"])
app.include_router(validate.router, prefix="/validate", tags=["validation"])


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
def health():
    """Cheap liveness probe.

    Returns 200 as long as the FastAPI process is up and serving HTTP. This
    is what the ECS container healthcheck calls — it must not perform network
    I/O, because container healthchecks have a tight per-call timeout (10s)
    and run continuously for the lifetime of the task. Any flakiness here
    will trigger the ECS deployment circuit breaker.

    Use ``/readiness`` for full subsystem checks (Neptune, OpenSearch,
    Bedrock).
    """
    return {"status": "ok"}


@app.get("/readiness", tags=["health"])
def readiness():
    """Deep subsystem health check.

    Probes the configured graph store, vector store, and Bedrock embeddings
    backend. Each call performs real network I/O and may take several
    seconds — do NOT wire this to the ECS container healthcheck.

    Subsystem failures are reported as a bare ``"error"`` status. The
    exception detail is logged server-side rather than returned, because the
    messages carry infrastructure internals (Neptune/OpenSearch endpoints,
    role ARNs, botocore request ids) and this endpoint is reachable by any
    caller that can hit the service.
    """
    backend = os.getenv("WORKBENCH_BACKEND", "opensearch_neptune")

    graph_status = "unreachable"
    try:
        graph_status = app.state.graph_store.health_check().get("status", "unknown")
    except Exception:
        logger.warning("readiness_graph_store_probe_failed", exc_info=True)
        graph_status = "error"

    vector_status = "unreachable"
    try:
        vector_status = app.state.vector_store.health_check().get("status", "unknown")
    except Exception:
        logger.warning("readiness_vector_store_probe_failed", exc_info=True)
        vector_status = "error"

    bedrock_status = "ok"
    try:
        client = _make_bedrock_client(app.state.config)
        vec = client.embed_text("health check")
        if not vec or len(vec) != app.state.config["bedrock_embed_dimensions"]:
            bedrock_status = "unexpected_response"
    except Exception:
        logger.warning("readiness_bedrock_probe_failed", exc_info=True)
        bedrock_status = "error"

    description_llm_status = "ok"
    try:
        # Cheap probe: ask the description-generator LLM to emit at most a
        # handful of tokens. The Bedrock converse() call is the same
        # critical path the unstructured induction worker uses, so an error
        # here surfaces the misconfiguration before any job is started.
        llm_client = _make_description_llm_client(app.state.config)
        out = llm_client.generate(prompt="ping", max_tokens=4)
        if not out:
            description_llm_status = "unexpected_response"
    except Exception:
        logger.warning("readiness_description_llm_probe_failed", exc_info=True)
        description_llm_status = "error"

    overall = (
        "ok"
        if graph_status == "ok" and vector_status == "ok" and bedrock_status == "ok" and description_llm_status == "ok"
        else "degraded"
    )
    return {
        "status": overall,
        "backend": backend,
        "graph_store": graph_status,
        "vector_store": vector_status,
        "bedrock_embeddings": bedrock_status,
        "description_llm": description_llm_status,
    }
