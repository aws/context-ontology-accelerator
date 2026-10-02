# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schemas for the namespace-scoped graph vertex lookup endpoints.

Kept in a separate module from ``catalog/schemas.py`` so the existing
ontology / embedding payloads remain isolated from this newer surface.
"""

from pydantic import BaseModel, Field


class GraphNeighbor(BaseModel):
    """A destination of an outgoing edge from the source vertex.

    Only minimal identifying information is included — by design, this
    endpoint does not recursively expand neighbors; clients are expected
    to issue follow-up requests for full neighbor data.
    """

    uri: str = Field(..., description="Full IRI of the destination, or lexical form for literals.")
    label: str | None = Field(
        default=None,
        description=(
            "Human-readable name. For URI neighbors this is the first "
            "``rdfs:label`` found in any namespace graph; for literals "
            "this is the literal value itself."
        ),
    )
    kind: str = Field(
        ...,
        description=(
            "Coarse category — one of: ``class``, ``object-property``, "
            "``datatype-property``, ``annotation-property``, ``ontology``, "
            "``property``, ``literal``, ``blank-node``, ``unknown``."
        ),
    )


class GraphEdge(BaseModel):
    """One outgoing edge from the source vertex."""

    predicate: str = Field(..., description="Full IRI of the predicate.")
    predicate_label: str | None = Field(
        default=None,
        description="Local name of the predicate (e.g. ``subClassOf``) for display.",
    )
    direction: str = Field(
        default="outgoing",
        description="``outgoing`` if source→neighbor, ``incoming`` if neighbor→source.",
    )
    neighbor: GraphNeighbor


class GraphVertex(BaseModel):
    """The source vertex with its outgoing edges and minimal neighbors."""

    uri: str = Field(..., description="Full IRI of the source vertex.")
    kind: str = Field(
        ...,
        description=(
            "The vertex kind requested by the client — one of ``class``, ``object-property``, ``datatype-property``."
        ),
    )
    namespace: str = Field(..., description="Namespace whose graphs were searched.")
    types: list[str] = Field(
        default_factory=list,
        description="All ``rdf:type`` values declared on the vertex.",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="All ``rdfs:label`` literals on the vertex.",
    )
    comments: list[str] = Field(
        default_factory=list,
        description="All ``rdfs:comment`` literals on the vertex.",
    )
    alt_labels: list[str] = Field(
        default_factory=list,
        description=(
            "All ``skos:altLabel`` literals — source synonyms curated during "
            "enrichment and emitted onto the induced class/property."
        ),
    )
    superseded: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Previous generations of this vertex's annotations, retired when a "
            "re-induced proposal was accepted into the same ontology. Keys "
            "are ``comments`` / ``definitions`` / ``altLabels``; values are the "
            "literals that used to be live (typically the AI-generated text a "
            "steward's curation replaced). Read from the ``coa:superseded*`` "
            "history predicates; NOT live content and excluded from ``edges``."
        ),
    )
    graph_uris: list[str] = Field(
        default_factory=list,
        description=(
            "Named graphs in which the vertex appears as subject. "
            "Useful for surfacing 'this class is defined in ontology X "
            "but referenced from ontology Y' in a UI."
        ),
    )
    is_mapped: bool = Field(
        default=False,
        description=(
            "Whether this vertex carries the ``coa:isMapped`` marker — i.e. it "
            "is backed by an R2RML mapping and therefore answerable by the "
            "structured Tier-2 (NL→SPARQL/NL→SQL) path. Structured/table-backed "
            "classes are True; unstructured-induced and foundational classes are "
            "False (the marker is absent). Pulled from the top-level "
            "``coa:isMapped`` triple and excluded from ``edges``."
        ),
    )
    edges: list[GraphEdge] = Field(
        default_factory=list,
        description=(
            "Outgoing edges. ``rdf:type``, ``rdfs:label``, ``rdfs:comment``, and "
            "``coa:isMapped`` are pulled into the top-level ``types``, "
            "``labels``, ``comments``, and ``is_mapped`` fields and excluded from "
            "this list to keep it focused on schema relationships."
        ),
    )


class GraphSearchHit(BaseModel):
    """One result from a substring search over entity IRIs."""

    uri: str = Field(..., description="Full IRI of the matched entity.")
    label: str | None = Field(default=None, description="rdfs:label if available.")
    kind: str = Field(..., description="class | object-property | datatype-property | property | unknown")
    graph_uris: list[str] = Field(
        default_factory=list,
        description=(
            "Named graphs the entity is defined in. A class IRI asserted in more "
            "than one ontology (e.g. a shared IRI referenced by both schema.org "
            "and FIBO) lists every source graph, not just one."
        ),
    )


class GraphSearchResult(BaseModel):
    """A page of substring-search hits plus the total match count.

    ``hits`` is the requested ``limit``/``offset`` window (in a deterministic
    order); ``total_count`` is the number of distinct entities matching the
    query across all pages, so a client can render a known page count.
    """

    hits: list[GraphSearchHit] = Field(..., description="The page of matching hits.")
    total_count: int = Field(..., description="Total distinct matches across all pages.")
