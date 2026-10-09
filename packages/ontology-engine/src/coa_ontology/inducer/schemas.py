# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic request/response schemas for the ontology induction API."""

from datetime import datetime

from coa_common.constants import validate_ontology_uri_prefix

# Import enums from Smithy-generated models (single source of truth for API contracts)
from coa_control_plane_server.models.induction_job_status import (
    InductionJobStatus as JobStatus,
)
from coa_control_plane_server.models.induction_strategy import (  # noqa: F401
    InductionStrategy,
)
from coa_control_plane_server.models.proposal_status import (  # noqa: F401
    ProposalStatus,
)
from pydantic import BaseModel, field_validator


class InductionRequest(BaseModel):
    """Kick off an ontology induction job."""

    table_ids: list[str] = []  # data-catalog table IDs or FQNs
    ontology_uri_prefix: str  # e.g. "https://example.com/ontology/retail#"
    label: str | None = None  # human-readable name for the induced ontology
    embedding_backend: str | None = None  # override default lexical backend
    confidence_threshold: float = 0.80  # minimum cosine similarity for auto-match
    grounding_ontology_ids: list[str] | None = None  # restrict search to specific ontology-catalog IDs
    scoring_strategy: str = "lexical"  # "lexical" | "structural_fusion"
    structural_weight: float = 0.05  # weight for structural signal in fusion
    strategy: str = "table_to_ontology"  # "table_to_ontology" | "rigor_ontology" | "unstructured_lexical_graph"
    grounding_mode: str = "ENHANCED"  # "NONE" | "STANDARD" | "ENHANCED"
    graph_arn: str | None = None  # Neptune Analytics graph ARN (required for unstructured_lexical_graph)

    @field_validator("ontology_uri_prefix")
    @classmethod
    def _validate_uri_prefix(cls, v: str) -> str:
        return validate_ontology_uri_prefix(v)


# JobStatus is re-exported from the Smithy-generated InductionJobStatus enum.
# Generated enum uses UPPERCASE attributes (PENDING, COMPLETED).


class ConceptMatch(BaseModel):
    """A grounding decision for one source column against a foundational class."""

    source_column: str  # fully qualified column name
    source_table: str
    # Stable identity, data-source-qualified when available, for same-named tables.
    # Optional so previously persisted matches remain readable.
    source_table_identity: str | None = None
    matched_class_uri: str | None  # ontology class URI, None if novel
    matched_ontology_id: str | None
    similarity: float | None
    match_type: str  # "exact" | "high_confidence" | "ambiguous" | "novel"
    # SKOS relationship chosen by the reranker: exactMatch | closeMatch |
    # broadMatch | narrowMatch | relatedMatch (None when novel or unset). Drives
    # which SKOS predicate emission asserts and whether a forward rdfs:subClassOf
    # is implied — match_type only decides acceptance. Kept optional/defaulted so
    # older persisted matches deserialize unchanged.
    relationship: str | None = None
    scoring_strategy: str | None = None  # which strategy produced this match
    reason: str | None = None  # LLM's justification for the grounding decision
    candidates: list["MatchCandidate"] | None = None  # top-K for comparison


class MatchCandidate(BaseModel):
    """One candidate foundational class considered during grounding, with its scores."""

    entity_uri: str
    ontology_id: str | None
    lexical_sim: float
    structural_sim: float | None = None  # cosine similarity in structural space
    fused_score: float | None = None  # combined score (strategy-dependent)
    definition: str | None = None  # class definition from foundational ontology


class DroppedTable(BaseModel):
    """A table that could not be processed during induction."""

    table_name: str
    reason: str  # short failure category: "parse_failed" | "gen_llm_empty" | etc.


class InductionReport(BaseModel):
    """Summary of a completed induction run: counts, matches, and the stored ontology."""

    job_id: str
    ontology_uri_prefix: str
    tables_processed: int
    columns_processed: int
    matches: list[ConceptMatch]
    novel_classes_created: int
    grounding_ontologies_used: list[str]
    induced_ontology_id: str | None = None  # ID in ontology-catalog once stored
    induced_ontology_uri: str | None = None
    dropped_tables: list[DroppedTable] = []  # tables excluded due to processing failures


class JobResponse(BaseModel):
    """Status envelope for an induction job, including its report or error once finished."""

    job_id: str
    status: JobStatus
    created_at: datetime
    report: InductionReport | None = None
    error: str | None = None
    # Set when duplicate detection short-circuits a run: the ``proposal_id`` of
    # the already-accepted proposal with an identical structural fingerprint.
    # No new proposal is created in that case, so the client must redirect to
    # THIS id (not the job id, which has no proposal row). Persisted on the DDB
    # job item too; surfaced here so it reaches the client on the live-worker
    # poll path (which returns this in-memory object, not the DDB row).
    duplicate_of: str | None = None
