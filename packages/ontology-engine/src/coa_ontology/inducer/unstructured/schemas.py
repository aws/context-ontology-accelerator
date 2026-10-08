# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for the unstructured ontology induction API.

This module defines three Pydantic models:

- :class:`UnstructuredInductionRequest` — the HTTP request body accepted
  by ``POST /induce/unstructured/``. All fields are JSON-serializable.
- :class:`UnstructuredInductionConfig` — the pipeline-internal config
  consumed by :class:`UnstructuredInductionPipeline.run`. Carries the
  already-constructed lexical store and Bedrock / LLM clients (so the
  orchestrator does not need to know how to build them).
- :class:`UnstructuredInductionReport` — the result returned by the
  pipeline. Mirrors the structured side's ``InductionReport`` shape plus
  unstructured-specific counts.

The split between request and config exists so the router can convert
HTTP input → already-constructed clients before invoking the pipeline,
keeping the orchestrator decoupled from AWS-client construction (and
unit-testable with mocks).

References:
    Requirements 4.1, 4.2, 4.3, 4.5 of
    ``.kiro/specs/unstructured-ontology-induction-api/requirements.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from coa_common.constants import validate_ontology_uri_prefix
from pydantic import BaseModel, Field, field_validator

from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

# Imported at runtime (not under TYPE_CHECKING) because it is used as a
# Pydantic field annotation on UnstructuredInductionConfig — Pydantic must
# resolve the type at class-build time. No import cycle: grounding.py does
# not import this module.
from coa_ontology.inducer.services.grounding import GroundingService
from coa_ontology.inducer.unstructured.services.description_generator import (
    LLMDescriptionClient,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    IRIResolver,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    LexicalGraphStore,
)


class SamplingParams(BaseModel):
    """Stratified-sampling configuration for the unstructured pipeline.

    ``k_min`` is the per-class sample target; ``floor`` is the minimum
    number of instances a class must have to be included at all.
    """

    k_min: int = 5
    floor: int = 3


class UnstructuredInductionRequest(BaseModel):
    """HTTP request body for ``POST /induce/unstructured/``.

    All fields are JSON-serializable. The router converts this into an
    :class:`UnstructuredInductionConfig` (constructing the lexical store
    and Bedrock / LLM clients from ``graph_arn`` and the app config)
    before invoking the pipeline.

    Security note: graph_arn accepts EITHER a Neptune Analytics ARN (strictly
    validated via regex) OR a sentinel value for Neptune Database. For Neptune
    DB, the actual endpoint is ALWAYS resolved from server-side configuration,
    never from user input, to prevent SSRF attacks.
    """

    namespace: str = "default"
    name: str  # the ontologyId — required
    graph_arn: str = Field(
        ...,
        pattern=r"^(arn:aws:neptune-graph:[a-z0-9-]+:\d{12}:graph/[a-zA-Z0-9_-]+|neptune-db)$",
        description=(
            "Neptune Analytics ARN (arn:aws:neptune-graph:<region>:<account>:graph/<id>) "
            "OR the literal string 'neptune-db' to use the server-configured Neptune Database endpoint"
        ),
    )
    ontology_uri_prefix: str  # required
    label: str | None = None
    datasource_ids: list[str] = Field(default_factory=list)
    confidence_threshold: float = 0.80
    sampling: SamplingParams = Field(default_factory=SamplingParams)
    description_mode: Literal["label_only", "instance", "schema"] = "schema"
    include_individuals: bool = False
    # Foundational grounding (mirrors the structured table_to_ontology
    # path): when grounding_ontology_ids is non-empty, induced classes are
    # grounded against those loaded ontologies via GroundingService.
    # Empty / None → no foundational grounding (all-novel), matching the
    # pre-grounding behavior. grounding_mode is ENHANCED (LLM rerank) or
    # STANDARD (embedding-only); ignored when no ontologies are selected.
    grounding_ontology_ids: list[str] | None = None
    grounding_mode: str = "ENHANCED"
    # Output-token cap for the ENHANCED-mode LLM grounding rerank (mirrors the
    # structured path). Default 1000; raise it when a reasoning model truncates
    # its JSON answer (the rerank then fails loud naming this knob). Bounds
    # mirror the Smithy RerankMaxTokens @range(min: 1, max: 8192).
    rerank_max_tokens: int = Field(default=1000, ge=1, le=8192)
    # entailment_enabled is always treated as False in v0; the field
    # exists so the API contract is forward-compatible with the planned
    # entailment engine (parent spec Task 13).
    entailment_enabled: bool = False

    @field_validator("ontology_uri_prefix")
    @classmethod
    def _validate_uri_prefix(cls, v: str) -> str:
        return validate_ontology_uri_prefix(v)


class UnstructuredInductionConfig(BaseModel):
    """Internal pipeline config consumed by ``UnstructuredInductionPipeline.run``.

    Carries already-constructed clients (a :class:`LexicalGraphStore`,
    a :class:`BedrockEmbeddingClient`, an :class:`LLMDescriptionClient`,
    and an :class:`IRIResolver`) so the orchestrator does not need to
    know how to build them. ``arbitrary_types_allowed=True`` is required
    for Pydantic to accept the protocol / concrete client instances as
    field values.
    """

    model_config = {"arbitrary_types_allowed": True}

    namespace: str
    name: str
    ontology_uri_prefix: str
    graph_arn: str
    confidence_threshold: float
    sampling: SamplingParams
    description_mode: Literal["label_only", "instance", "schema"]
    include_individuals: bool
    entailment_enabled: bool

    # Foundational grounding params (threaded from the request). When
    # grounding_ontology_ids is non-empty AND grounding_service is set, the
    # pipeline grounds induced classes against those loaded ontologies.
    grounding_ontology_ids: list[str] | None = None
    grounding_mode: str = "ENHANCED"
    # Output-token cap for the ENHANCED-mode LLM grounding rerank (threaded from
    # the request; mirrors the structured path). Default 1000.
    rerank_max_tokens: int = 1000

    # Already-constructed dependency-injected clients
    lexical_store: LexicalGraphStore
    embedding_client: BedrockEmbeddingClient
    description_llm: LLMDescriptionClient
    iri_resolver: IRIResolver
    # Optional grounding service. Built by the router from the app config's
    # ontology_catalog_url + embedding_generator_url. None → grounding is
    # skipped entirely (the pipeline behaves as it did pre-grounding). Kept
    # optional so existing unit tests that build a config without grounding
    # keep working unchanged.
    grounding_service: GroundingService | None = None


class UnstructuredInductionReport(BaseModel):
    """Result returned by ``UnstructuredInductionPipeline.run``.

    Mirrors the structured side's ``InductionReport`` shape plus
    unstructured-specific counts and the serialized Turtle. Counts are
    derived by the orchestrator from the merged ``rdflib.Graph`` after
    the rules stage.

    The ``subclassAxiomCount``, ``equivalenceAxiomCount``, and
    ``disjointnessAxiomCount`` fields are always 0 in v0 because the
    entailment stage is a pass-through (Requirement 9.2). ``warnings``
    surfaces the v0 limitations from Requirement 10.3 to the client.
    """

    pipeline_run_id: str  # uuid; equal to job_id at the router level
    namespace: str
    ontology_id: str

    # Counts derived from the merged rdflib.Graph
    tboxClassCount: int
    objectPropertyCount: int
    datatypePropertyCount: int
    conceptCount: int
    individualCount: int
    subclassAxiomCount: int = 0  # always 0 in v0 (entailment pass-through)
    equivalenceAxiomCount: int = 0  # always 0 in v0
    disjointnessAxiomCount: int = 0  # always 0 in v0
    closeMatchAxiomCount: int
    # Count of classes grounded against loaded foundational ontologies
    # (exact/close/related match). 0 when grounding is disabled or nothing
    # matched. Distinct from closeMatchAxiomCount, which also counts the
    # within-run class↔class dedupe links from Rule 1.
    foundationalGroundedCount: int = 0
    # Per-class grounding ConceptMatch records (one per class that produced
    # recall candidates, chosen or not). Persisted by the router as the
    # proposal's metadata.matches (→ matches.json) so the review UI shows the
    # candidate count and powers the manual grounding-override editor, exactly
    # like structured proposals. Empty when grounding is disabled.
    # Typed as list[dict] (model_dump of ConceptMatch) to keep the report
    # JSON-serializable without importing the inducer schema here.
    groundingMatches: list[dict] = []

    description_mode: str
    started_at: datetime
    completed_at: datetime | None = None
    turtle: str
    warnings: list[str] = []
