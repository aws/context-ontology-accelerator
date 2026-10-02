# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic request/response schemas for the ontology-catalog and embedding APIs."""

from pydantic import BaseModel, Field, field_validator

# --- Ontology schemas ---


class OntologyCreate(BaseModel):
    """Request body to register a new ontology in the catalog."""

    uri: str
    title: str
    ontology_type: str = "foundational"  # foundational | induced | user_created
    description: str | None = None
    version: str | None = None
    version_iri: str | None = None
    license: str | None = None
    license_tier: str | None = None
    source_registry: str | None = None
    source_url: str | None = None
    format: str | None = None
    domain_tags: list[str] = []
    imports: list[str] = []


class OntologyUpdate(BaseModel):
    """Request body with the mutable fields of a catalog ontology (all optional)."""

    title: str | None = None
    description: str | None = None
    version: str | None = None
    license: str | None = None
    license_tier: str | None = None
    source_url: str | None = None
    format: str | None = None
    domain_tags: list[str] | None = None
    imports: list[str] | None = None


class OntologyResponse(BaseModel):
    """API representation of a catalog ontology, including counts and lifecycle status."""

    ontology_id: str
    uri: str
    ontology_type: str | None
    title: str
    description: str | None
    version: str | None
    version_iri: str | None
    license: str | None
    license_tier: str | None
    source_registry: str | None
    source_url: str | None
    format: str | None
    class_count: int | None
    property_count: int | None
    axiom_count: int | None
    last_fetched: str | None = None
    last_modified_upstream: str | None = None
    parse_status: str | None = None
    # Lifecycle status for the registry row. Currently only set to "deleting"
    # while an async ontology delete tears down its graph + embeddings; the row
    # is removed once teardown completes. None for normal (fully-materialized)
    # ontologies.
    status: str | None = None
    # Set when an async delete's teardown failed (graph/embedding hard gate),
    # leaving the row in ``status="deleting"``. Lets the UI/ops distinguish a
    # stuck delete (needs a re-issued DELETE) from one still in progress.
    # Cleared when a fresh delete attempt starts. None otherwise.
    delete_error: str | None = None
    domain_tags: list[str] = []
    imports: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None

    # Populated by the registry + ingest pipeline; optional so legacy
    # graph-store-only rows still serialise cleanly.
    graph_uri: str | None = None
    embedding_index: str | None = None
    embedding_model_id: str | None = None
    embedding_count: int | None = None
    source: str | None = None

    model_config = {"from_attributes": True}


class OntologyFetchRequest(BaseModel):
    """Request to fetch/update an ontology from its source URL."""

    source_url: str | None = None  # override the stored source_url
    accept_formats: list[str] = Field(
        default=["text/turtle", "application/rdf+xml", "application/ld+json"],
        description="Content-negotiation Accept header values",
    )
    extract_properties_for_classes: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of class URIs. If provided, the /fetch handler "
            "returns every property in the parsed ontology whose rdfs:domain "
            "or rdfs:range (or schema:domainIncludes/rangeIncludes) is one "
            "of these classes. Useful for populating property embeddings "
            "without listing them by hand in config.yaml."
        ),
    )
    extract_all_properties: bool = Field(
        default=False,
        description=(
            "If true, return every property declared in the parsed ontology, "
            "not just ones whose domain/range intersects "
            "extract_properties_for_classes. Useful when the caller wants "
            "full property coverage regardless of declared domain/range. "
            "Mutually exclusive with extract_properties_for_classes — if both "
            "set, this flag wins. Off by default to limit Bedrock embedding cost."
        ),
    )
    known_ontology_uris: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of ontology base URIs the caller has already loaded. "
            "If provided, /fetch inspects the parsed ontology for rdfs:subClassOf, "
            "rdfs:domain, or rdfs:range targets pointing at external namespaces "
            "that AREN'T covered by this list and returns them under "
            "`unresolved_references` for the caller to surface as warnings."
        ),
    )


class PropertyExtract(BaseModel):
    """A property auto-extracted from a parsed ontology."""

    property_uri: str
    labels: list[str] = []
    comments: list[str] = []
    domain: list[str] = []
    range: list[str] = []


class UnresolvedReference(BaseModel):
    """A namespace referenced by the fetched ontology that no other loaded ontology covers.

    Non-fatal — the ontology still registers and embeds; this is a diagnostic to
    help users discover dangling foreign references.
    """

    namespace: str  # e.g. "http://www.hpclab.ceid.upatras.gr/omg#"
    sample_iris: list[str] = []  # up to 5 concrete IRIs from this namespace
    reference_count: int = 0  # total times IRIs from this namespace appear as subClassOf/domain/range targets


class OntologyFetchResponse(BaseModel):
    """Result of fetching/parsing an ontology: counts, extracted properties, diagnostics."""

    ontology_id: str
    uri: str
    parse_status: str
    class_count: int | None
    property_count: int | None
    axiom_count: int | None
    last_fetched: str | None = None
    file_path: str | None
    properties: list[PropertyExtract] | None = Field(
        default=None,
        description=(
            "Populated when the request included extract_properties_for_classes. "
            "Each property's domain/range are filtered to only IRIs that appear "
            "in the request's class list."
        ),
    )
    unresolved_references: list[UnresolvedReference] | None = Field(
        default=None,
        description=(
            "External namespaces the ontology references via rdfs:subClassOf, "
            "rdfs:domain, or rdfs:range but which don't match any of the "
            "registered ontology URIs supplied in the request. Purely diagnostic "
            "— the fetch still succeeds. Populated when the request includes "
            "known_ontology_uris; otherwise None."
        ),
    )


# --- Embedding schemas ---


class EmbeddingCreate(BaseModel):
    """Request body to store one entity embedding vector for an ontology entity."""

    ontology_id: str
    entity_uri: str
    entity_type: str = "class"  # "class" | "property"
    embedding_type: str  # "lexical" | "structural"
    model_id: str
    vector: list[float]
    dimensions: int


class EmbeddingBatchCreate(BaseModel):
    """Request body to store a batch of entity embeddings in one call."""

    embeddings: list[EmbeddingCreate]


class EmbeddingResponse(BaseModel):
    """API representation of a stored embedding's metadata (without the vector)."""

    id: str
    ontology_id: str
    entity_uri: str
    entity_type: str
    embedding_type: str
    model_id: str
    dimensions: int
    text: str | None = None
    created_at: str | None = None

    model_config = {"from_attributes": True}


class EmbeddingWithVectorResponse(EmbeddingResponse):
    """Embedding response that also includes the raw vector."""

    vector: list[float]


class EmbeddingQuery(BaseModel):
    """Query for nearest-neighbor lookup (vector provided by caller)."""

    vector: list[float]
    embedding_type: str
    model_id: str
    entity_type: str = "class"  # filter by entity type
    ontology_id: str | None = None  # optional: scope search to a single ontology
    top_k: int = 10

    @field_validator("embedding_type", "model_id", "entity_type", "ontology_id", mode="before")
    @classmethod
    def _reject_injection_chars(cls, v: str | None) -> str | None:
        """Reject values containing characters that could enable openCypher injection."""
        if v is None:
            return v
        import re

        if not re.match(r"^[a-zA-Z0-9_\-.:/# ]+$", v):
            # Never interpolate ``v`` into the message — Pydantic uses this
            # text verbatim as the 422 response's ``msg``, and echoing an
            # attacker-supplied value there is the leak we're rejecting the
            # value to prevent. The handler at ``coa_ontology.main`` masks
            # ``msg`` for ``value_error``/``assertion_error`` types as a
            # safety net; this is the belt to that suspenders.
            raise ValueError("value contains characters outside [a-zA-Z0-9_.-:/# ]")
        return v
