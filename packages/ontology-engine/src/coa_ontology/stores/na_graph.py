# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune Analytics graph store — thin wrapper over ``app.catalog.na_store``.

All heavy lifting already lives in ``na_store``. This module adapts the
module-level functions to the :class:`GraphStore` Protocol and binds a
namespace to every call so the underlying queries filter on it.
"""

from __future__ import annotations

from coa_ontology.catalog import na_store
from coa_ontology.stores.base import GraphStore


class NeptuneAnalyticsGraphStore(GraphStore):
    """Graph CRUD backed by Neptune Analytics (opencypher).

    Idempotent to construct; the underlying ``na_store`` module holds a
    lazily-initialised boto3 client. When constructed with a namespace,
    every call threads the namespace through so that vertices, edges,
    and queries are scoped to that namespace.
    """

    def __init__(self, namespace: str | None = None):
        """Bind the store to a namespace threaded through every call.

        Args:
            namespace: Namespace to scope all vertices, edges, and queries to.
        """
        self._namespace = namespace

    # Ontology CRUD ----------------------------------------------------
    def create_ontology(self, data):
        """Create a new ontology in the bound namespace."""
        return na_store.create_ontology(data, namespace=self._namespace)

    def get_ontology(self, oid):
        """Fetch an ontology by id within the bound namespace."""
        return na_store.get_ontology(oid, namespace=self._namespace)

    def get_ontology_by_uri(self, uri):
        """Fetch an ontology by URI within the bound namespace."""
        return na_store.get_ontology_by_uri(uri, namespace=self._namespace)

    def list_ontologies(
        self,
        ontology_type=None,
        uri=None,
        source_registry=None,
        domain_tags=None,
        license_tier=None,
        limit=None,
    ):
        """List ontologies in the bound namespace, optionally filtered.

        Args:
            ontology_type: Restrict to a single ontology type when provided.
            uri: Restrict to a specific ontology URI when provided.
            source_registry: Restrict to ontologies from a source registry.
            domain_tags: Domain tag filter; only the first is used (NA accepts
                a single tag), a list or scalar is accepted.
            license_tier: Restrict to a license tier when provided.
            limit: Maximum number of ontologies to return (defaults to 50).

        Returns:
            List of matching ontology dicts.
        """
        # na_store.list_ontologies accepts a single domain_tag; pick the first
        # if a list is passed. (The Neptune-DB graph store accepts a list too,
        # which is why the Protocol uses the plural form.)
        domain_tag = None
        if domain_tags:
            domain_tag = domain_tags[0] if isinstance(domain_tags, list) else domain_tags
        return na_store.list_ontologies(
            ontology_type=ontology_type,
            uri=uri,
            source_registry=source_registry,
            domain_tag=domain_tag,
            license_tier=license_tier,
            limit=limit or 50,
            namespace=self._namespace,
        )

    def update_ontology(self, uri, updates):
        """Apply updates to the ontology with the given URI in the namespace."""
        return na_store.update_ontology(uri, updates, namespace=self._namespace)

    def delete_ontology(self, uri):
        """Delete the ontology with the given URI from the bound namespace."""
        return na_store.delete_ontology(uri, namespace=self._namespace)

    # Schema elements --------------------------------------------------
    def store_class(
        self,
        ontology_uri,
        class_uri,
        labels=None,
        comments=None,
        super_classes=None,
        is_mapped=False,
    ):
        """Persist a class into the ontology's graph in the bound namespace.

        Args:
            ontology_uri: URI of the owning ontology.
            class_uri: URI of the class to store.
            labels: Human-readable labels for the class.
            comments: Definitions/comments for the class.
            super_classes: Parent class URIs (``rdfs:subClassOf`` targets).
            is_mapped: Tier-2 R2RML-answerability marker; accepted for protocol
                parity but not persisted on this backend (see the note below).

        Returns:
            The stored class as a dict.
        """
        # NOTE: ``is_mapped`` (the Tier-2 R2RML-answerability marker) is accepted
        # to keep the GraphStore protocol consistent with the NDB backend, but is
        # intentionally NOT persisted here yet. NA uses a property-graph model
        # (node properties via openCypher), not RDF triples, so both the write
        # (tag the class node) and the serve-side T-Box read filter need a
        # separate NA-specific implementation — see ``store_class`` in
        # neptune_db_graph.py for the NDB reference and ``_IS_MAPPED_PATTERN`` in
        # context-manager's tbox_context.py for the serve gate. The live
        # deployment is ``opensearch_neptune`` (NDB), which is fully implemented
        # and E2E-verified; NA parity for this marker is tracked in.
        # Until then, NA-backed namespaces carry no isMapped marker and serve treats
        # absent as unmapped (i.e. NA classes are hidden from the structured
        # Tier-2 prompt rather than wrongly exposed).
        _ = is_mapped  # accepted for protocol parity; NA persistence is a follow-up
        return na_store.store_class(
            ontology_uri,
            class_uri,
            labels=labels or [],
            comments=comments or [],
            superclass_uris=super_classes or [],
            namespace=self._namespace,
        )

    def store_property(
        self,
        ontology_uri,
        prop_uri,
        label="",
        comment="",
        domains=None,
        ranges=None,
    ):
        """Persist a property into the ontology's graph in the bound namespace.

        Args:
            ontology_uri: URI of the owning ontology.
            prop_uri: URI of the property to store.
            label: Human-readable label for the property.
            comment: Definition/comment for the property.
            domains: Domain class URIs for the property.
            ranges: Range URIs (class IRIs for object properties, datatypes otherwise).

        Returns:
            The stored property as a dict.
        """
        return na_store.store_property(
            ontology_uri,
            prop_uri,
            label=label,
            comment=comment,
            domain_uris=domains or [],
            range_uris=ranges or [],
            namespace=self._namespace,
        )

    # Annotation supersession -------------------------------------------
    def supersede_annotations(self, ontology_uri, subject_uris, predicates):
        """Nothing to do on this backend.

        NA is a property graph: :meth:`store_class` / :meth:`store_property`
        ``SET`` the node's ``comment`` on every write, so a re-accepted proposal
        already overwrites the previous description. There is no
        per-value altLabel triple to clear either — :meth:`load_turtle` is a
        no-op here. Returns ``0`` for protocol parity with the NDB backend.
        """
        _ = (ontology_uri, subject_uris, predicates)
        return 0

    # Bulk Turtle ingest -----------------------------------------------
    def load_turtle(self, ontology_uri, turtle, graph_uri=None):
        """Neptune Analytics does not expose a Turtle bulk-load endpoint.

        Ingestion happens through :meth:`store_class` and
        :meth:`store_property` (which the acceptance flow also calls),
        so we intentionally no-op here rather than raising.
        """
        return {
            "status": "skipped",
            "reason": "Neptune Analytics backend uses store_class/store_property",
            "graph_uri": graph_uri or ontology_uri,
            "bytes_loaded": 0,
        }

    # Bulk Turtle export -----------------------------------------------
    def get_graph_turtle(self, ontology_id):
        """Neptune Analytics has no native Turtle round-trip endpoint.

        Always returns ``None`` so callers fall back to whatever local
        cache exists. Reconstructing Turtle from Neptune Analytics would
        require walking opencypher vertices/edges and serializing — out
        of scope for this backend.
        """
        return None

    # Aggregate counts -------------------------------------------------
    def count_classes_and_properties(self, ontology_id):
        """Neptune Analytics does not implement deduped count reconciliation.

        Returns ``None`` so the accept-flow reconcile step
        (``_reconcile_counts``) leaves the registry counts unchanged.
        Computing this would require an opencypher aggregation over the
        ontology's vertices — out of scope for this backend, which is
        not the default workbench backend.
        """
        return None

    # Vertex lookup ----------------------------------------------------
    def get_vertex(self, vertex_uri, namespace=None):
        """Return None — Neptune Analytics has no cross-graph vertex lookup.

        Neptune Analytics doesn't expose a SPARQL surface, so this backend
        doesn't implement the cross-graph vertex lookup. The catalog/graph
        router falls back to a 501 when the active backend returns ``None``.
        """
        return None

    # Entity search ----------------------------------------------------
    def search_entities(
        self,
        query,
        namespace=None,
        kind=None,
        ontology_id=None,
        exclude_ontology_id=None,
        limit=50,
        offset=0,
    ):
        """Not supported on Neptune Analytics."""
        return []

    def count_entities(
        self,
        query,
        namespace=None,
        kind=None,
        ontology_id=None,
        exclude_ontology_id=None,
    ):
        """Not supported on Neptune Analytics."""
        return 0

    # Ontology overview ------------------------------------------------
    def get_ontology_overview(self, ontology_id, namespace=None, *, sample=False):
        """Not supported on Neptune Analytics — no SPARQL surface.

        Returns ``None`` so the catalog/graph router maps this to a 501,
        same as :meth:`get_vertex`. The ``sample`` flag is accepted for
        interface parity and ignored.
        """
        return None

    # Health -----------------------------------------------------------
    def health_check(self):
        """Probe backend health via the underlying na_store client."""
        return na_store.health_check()

    # Namespace schema (bulk) ------------------------------------------
    def get_namespace_schema(
        self,
        namespace: str,
        *,
        max_results: int = 100,
        include_properties: bool = True,
    ) -> dict:
        """Raise — Neptune Analytics has no bulk namespace schema query.

        Args:
            namespace: Namespace whose schema was requested.
            max_results: Unused; kept for protocol parity.
            include_properties: Unused; kept for protocol parity.

        Raises:
            NotImplementedError: Always, on this backend.
        """
        raise NotImplementedError("Neptune Analytics does not support bulk namespace schema queries")
