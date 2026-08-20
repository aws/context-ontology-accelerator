# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace-scoped graph vertex lookup router.

Endpoints:

  GET /graph/class/{class_uri:path}?namespace=<ns>
  GET /graph/object-property/{prop_uri:path}?namespace=<ns>
  GET /graph/datatype-property/{prop_uri:path}?namespace=<ns>

All three return a {@link GraphVertex} — the source vertex's
descriptive fields plus its outgoing edges, where each edge carries
just the destination's URI / label / kind. The query covers every
named graph in the namespace, so a vertex from an induced ontology
can include edges that reach into foundational ontologies registered
in the same namespace.

Vertex-kind validation is enforced server-side: hitting
``/graph/class/{uri}`` for a URI that exists in the namespace but is
typed as an ``owl:ObjectProperty`` returns 404 with a hint, since
mismatched routes usually indicate a UI bug.
"""

from __future__ import annotations

import logging

from coa_control_plane_server.models import (
    GetOntologyOverviewResponseContent as _OntologyOverview,
)
from coa_control_plane_server.models import (
    OntologyClassSummary as _OntologyClassSummary,
)
from coa_control_plane_server.models import (
    OntologyPropertySummary as _OntologyPropertySummary,
)
from fastapi import APIRouter, HTTPException

from coa_ontology.catalog.graph_schemas import (
    GraphSearchHit,
    GraphSearchResult,
    GraphVertex,
)
from coa_ontology.stores import build_stores
from coa_ontology.stores.base import GraphStore

router = APIRouter()
log = logging.getLogger(__name__)

# Map the URL-segment kind to the rdf:type the vertex must carry.
_OWL = "http://www.w3.org/2002/07/owl#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_KIND_TO_REQUIRED_TYPES: dict[str, set[str]] = {
    # Some sources mark classes as ``rdfs:Class`` rather than
    # ``owl:Class``; accept either.
    "class": {_OWL + "Class", _RDFS + "Class"},
    "object-property": {_OWL + "ObjectProperty"},
    "datatype-property": {_OWL + "DatatypeProperty"},
}


# Page-size bounds for /graph/search. ``limit`` is interpolated straight into
# the SPARQL LIMIT clause, so an unbounded value is an unbounded Neptune scan
# and a negative one is invalid SPARQL (surfacing as a 500). Reject out-of-range
# values at the edge with a 400, mirroring the sources layer's ``maxResults``
# clamp. The ceiling is above the largest limit any caller sends today (the
# Explorer's 100-class fetch chunk).
_DEFAULT_SEARCH_LIMIT = 50
_MAX_SEARCH_LIMIT = 500


def _graph(namespace: str | None) -> GraphStore:
    return build_stores(namespace)[0]


def _lookup(vertex_uri: str, namespace: str, kind: str) -> GraphVertex:
    graph = _graph(namespace)
    try:
        record = graph.get_vertex(vertex_uri, namespace=namespace)
    except NotImplementedError as e:
        raise HTTPException(501, f"Active backend does not support graph lookup: {e}") from e
    except ValueError as e:
        # _iri() raises ValueError for malformed URIs.
        raise HTTPException(400, f"Invalid vertex URI: {e}") from e
    if record is None:
        # Backend may also return None when the call is unsupported
        # (e.g. Neptune Analytics today).
        if not getattr(graph, "get_vertex", None) or graph.__class__.__name__ == "NeptuneAnalyticsGraphStore":
            raise HTTPException(501, "Vertex lookup is not supported on this backend.")
        raise HTTPException(
            404,
            f"No triples found for '{vertex_uri}' in namespace '{namespace}'.",
        )

    required = _KIND_TO_REQUIRED_TYPES[kind]
    if not (set(record.get("types") or []) & required):
        raise HTTPException(
            404,
            (
                f"'{vertex_uri}' exists in namespace '{namespace}' but is not a "
                f"{kind.replace('-', ' ')} (rdf:type = {record.get('types') or []})."
            ),
        )

    return GraphVertex(
        uri=record["uri"],
        kind=kind,
        namespace=namespace,
        types=record.get("types", []),
        labels=record.get("labels", []),
        comments=record.get("comments", []),
        alt_labels=record.get("alt_labels", []),
        graph_uris=record.get("graph_uris", []),
        is_mapped=record.get("is_mapped", False),
        edges=record.get("edges", []),
    )


@router.get("/search", response_model=GraphSearchResult)
def search_entities(
    q: str,
    namespace: str = "default",
    kind: str | None = None,
    ontology_id: str | None = None,
    exclude_ontology_id: str | None = None,
    limit: int = _DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
):
    """Substring search over entity IRIs and labels in a namespace.

    Searches all named graphs under the namespace for subjects whose
    IRI or ``rdfs:label`` contains the query string (case-insensitive).
    Optionally filter by ``kind`` (``class``, ``object-property``,
    ``datatype-property``) and/or ``ontology_id`` (restricts to a
    single named graph). Use ``exclude_ontology_id`` to omit results
    from a specific ontology (e.g., governed-metrics).

    Returns a ``{hits, total_count}`` envelope: ``hits`` is the ``limit`` /
    ``offset`` page window (in a deterministic order) with URI, label, kind,
    and graph_uris (all named graphs the entity is defined in); ``total_count``
    is the number of distinct matches across all pages so the client can render
    a known page count.
    """
    if ontology_id and exclude_ontology_id:
        raise HTTPException(400, "Cannot use both ontology_id and exclude_ontology_id")
    if exclude_ontology_id is not None and not exclude_ontology_id.strip():
        raise HTTPException(400, "exclude_ontology_id cannot be empty")
    if offset < 0:
        raise HTTPException(400, "offset cannot be negative")
    if not 1 <= limit <= _MAX_SEARCH_LIMIT:
        raise HTTPException(400, f"limit must be between 1 and {_MAX_SEARCH_LIMIT}")

    graph = _graph(namespace)
    try:
        results = graph.search_entities(
            query=q,
            namespace=namespace,
            kind=kind,
            ontology_id=ontology_id,
            exclude_ontology_id=exclude_ontology_id,
            limit=limit,
            offset=offset,
        )
        # Recomputed per page by design: ``total_count`` is part of every page's
        # envelope contract (clients use it as the paging stop condition), so it
        # must stay identical across pages rather than be derived from the window.
        total = graph.count_entities(
            query=q,
            namespace=namespace,
            kind=kind,
            ontology_id=ontology_id,
            exclude_ontology_id=exclude_ontology_id,
        )
    except (NotImplementedError, AttributeError) as e:
        raise HTTPException(501, f"Active backend does not support entity search: {e}") from e
    return GraphSearchResult(hits=[GraphSearchHit(**r) for r in results], total_count=total)


@router.get("/ontology-overview", response_model=_OntologyOverview, response_model_by_alias=True)
def get_ontology_overview(ontology_id: str, namespace: str = "default", sample: bool = False):
    """Enumerate the classes + properties persisted in one ontology's graph.

    Reads directly from the graph store (not the Dynamo registry), so the
    result reflects the triples that actually landed in the knowledge
    graph for ``ontology_id``. Returns the ontology's classes, object
    properties (relationships between classes), and datatype properties.

    Set ``sample=true`` for a bounded taxonomy seed (a few largest-subtree
    root classes + a capped set of their descendants, no properties) instead
    of the full ontology. The Graph view uses this for its initial render so a
    multi-thousand-class ontology doesn't fan out one detail call per class;
    the rest of the taxonomy loads on demand as the user expands nodes.

    Returns 501 when the active backend cannot enumerate graphs (e.g.
    Neptune Analytics). An ontology that exists but has no schema triples
    yields a 200 with empty lists.
    """
    graph = _graph(namespace)
    try:
        overview = graph.get_ontology_overview(ontology_id, namespace=namespace, sample=sample)
    except NotImplementedError as e:
        raise HTTPException(501, f"Active backend does not support ontology overview: {e}") from e
    except ValueError as e:
        raise HTTPException(400, f"Invalid ontology id: {e}") from e
    except Exception as e:
        log.exception("get_ontology_overview failed for %s in %s", ontology_id, namespace)
        raise HTTPException(500, f"Internal error querying ontology graph: {type(e).__name__}") from e
    if overview is None:
        raise HTTPException(501, "Ontology overview is not supported on this backend.")
    # Construct the generated model. The store returns dicts with snake_case
    # keys; the generated model accepts them via populate_by_name=True.
    return _OntologyOverview(
        ontology_id=overview["ontology_id"],
        namespace=namespace,
        graph_uri=overview.get("graph_uri"),
        classes=[_OntologyClassSummary(**c) for c in overview.get("classes", [])],
        object_properties=[_OntologyPropertySummary(**p) for p in overview.get("object_properties", [])],
        datatype_properties=[_OntologyPropertySummary(**p) for p in overview.get("datatype_properties", [])],
    )


@router.get("/schema")
def get_namespace_schema(
    namespace: str = "default",
    max_results: int = 100,
    include_properties: bool = True,
    class_filter: str | None = None,
):
    """List all OWL classes (and optionally properties) across all ontologies in a namespace.

    Returns a bulk schema summary for MCP discovery tools and UI schema views.
    Queries all named graphs under the namespace prefix.

    ``class_filter`` restricts the response to classes whose IRI equals the
    given value or starts with it (treating a trailing ``#``/``/`` as a
    prefix). Matches the Smithy ``DescribeSchema.classFilter`` input; a
    non-matching filter returns an empty ``classes`` list.

    When ``class_filter`` is set the backend is queried with the 500-class
    ceiling instead of the caller's ``max_results`` and the filter is applied
    BEFORE the truncation — otherwise a filter targeting a class that isn't
    in the first ``max_results`` backend rows would return empty even though
    the class exists. ``max_results`` still bounds the FINAL response so a
    prefix that matches every class still respects the caller's cap.
    """
    graph = _graph(namespace)
    # When filtering, fetch the full page so the filter matches even if the
    # target class isn't in the first ``max_results`` classes the backend
    # returns; otherwise honour the caller's requested cap directly.
    backend_limit = 500 if class_filter else min(max_results, 500)
    try:
        result = graph.get_namespace_schema(
            namespace,
            max_results=backend_limit,
            include_properties=include_properties,
        )
    except NotImplementedError as e:
        raise HTTPException(501, f"Active backend does not support namespace schema: {e}") from e
    except Exception as e:
        log.exception("get_namespace_schema failed for %s", namespace)
        raise HTTPException(500, f"Internal error querying schema: {type(e).__name__}") from e

    if class_filter:
        prefix = class_filter
        classes = result.get("classes") if isinstance(result, dict) else None
        if isinstance(classes, list):
            filtered = [c for c in classes if isinstance(c, dict) and _matches_class_filter(c.get("uri", ""), prefix)]
            # Re-apply the caller's cap AFTER filtering so callers get at most
            # ``max_results`` matches (not the raw 500 from the backend).
            result["classes"] = filtered[: min(max_results, 500)]
    return result


def _matches_class_filter(uri: str, prefix: str) -> bool:
    """True if ``uri`` equals ``prefix`` or begins with it as an IRI prefix.

    Treats ``prefix`` as either an exact IRI or a namespace-style prefix.
    A trailing ``#`` or ``/`` on ``prefix`` is honored verbatim; otherwise
    the match tolerates a following ``#`` or ``/`` before any local name so
    ``https://schema.org/`` matches ``https://schema.org/Order``.
    """
    if not uri:
        return False
    if uri == prefix:
        return True
    if prefix.endswith(("#", "/")):
        return uri.startswith(prefix)
    return uri.startswith(prefix + "#") or uri.startswith(prefix + "/")


@router.get("/vertex", response_model=GraphVertex)
def get_vertex(uri: str, namespace: str = "default"):
    """Look up any vertex by IRI — no kind validation."""
    graph = _graph(namespace)
    try:
        record = graph.get_vertex(uri, namespace=namespace)
    except NotImplementedError as e:
        raise HTTPException(501, f"Active backend does not support graph lookup: {e}") from e
    except ValueError as e:
        raise HTTPException(400, f"Invalid vertex URI: {e}") from e
    if record is None:
        raise HTTPException(404, f"No triples found for '{uri}' in namespace '{namespace}'.")
    record.setdefault("kind", "unknown")
    record.setdefault("namespace", namespace)
    return GraphVertex(**record)


# Vertex lookups take the IRI as ``?uri=`` query param. OWL IRIs frequently
# contain slashes and ``#`` fragments which double-encode poorly through
# API Gateway path templates and through some HTTP clients.
@router.get("/class/", response_model=GraphVertex)
def get_class(uri: str, namespace: str = "default"):
    """Look up an ``owl:Class`` vertex (URI in ``?uri=`` query param)."""
    return _lookup(uri, namespace, "class")


@router.get("/object-property/", response_model=GraphVertex)
def get_object_property(uri: str, namespace: str = "default"):
    """Look up an ``owl:ObjectProperty`` vertex (URI in ``?uri=`` query param)."""
    return _lookup(uri, namespace, "object-property")


@router.get("/datatype-property/", response_model=GraphVertex)
def get_datatype_property(uri: str, namespace: str = "default"):
    """Look up an ``owl:DatatypeProperty`` vertex (URI in ``?uri=`` query param)."""
    return _lookup(uri, namespace, "datatype-property")
