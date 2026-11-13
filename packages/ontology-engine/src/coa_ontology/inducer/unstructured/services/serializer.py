# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turtle serializer for the unstructured induction pipeline.

:func:`serialize` binds the standard prefixes, adds the ``owl:Ontology``
declaration, serializes with rdflib, enforces a 50 MB cap and checks round-trip
isomorphism. The byte stream is not stable across runs (rdflib prefix and
blank-node ordering); compare outputs with :func:`rdflib.compare.isomorphic`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, PROV, SKOS

log = logging.getLogger(__name__)


# Hard cap on the serialized size. 50 MB is generous for
# a single ontology — even the dev graph's full induced output is
# only a few MB. A run that overflows this cap is almost certainly
# a bug (e.g. accidental A-Box dump).
_MAX_TURTLE_BYTES = 50 * 1024 * 1024


def serialize(
    graph: Graph,
    namespace_name: str,
    namespace_iri: str,
    timestamp: datetime,
) -> tuple[str, Graph]:
    """Serialize the induced ontology graph as Turtle.

    Mutates ``graph`` in place (prefixes and the ``owl:Ontology`` block); pass a
    copy to keep the original. Returns ``(turtle, augmented_graph)``. Naive
    timestamps are treated as UTC.

    Raises:
        ValueError: If ``namespace_name`` or ``namespace_iri`` is empty, or the
            Turtle exceeds the 50 MB cap.
        RuntimeError: If the Turtle does not round-trip to an isomorphic graph.
    """
    if not namespace_name:
        raise ValueError("serialize: namespace_name is required")
    if not namespace_iri:
        raise ValueError("serialize: namespace_iri is required")

    # Always emit UTC as ``"YYYY-MM-DDTHH:MM:SSZ"``.
    ts_utc = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    ts_iso_z = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Strip the trailing ``/`` or ``#`` for the ontology subject IRI
    # — matches the structured induction convention (see
    # ``inducer/services/pipeline.py:_init_graph``). The serialized
    # IRI in Turtle will look like ``<base>`` rather than ``<base/>``.
    ontology_subject_iri = namespace_iri.rstrip("/").rstrip("#")
    ontology_uri = URIRef(ontology_subject_iri)

    _bind_standard_prefixes(graph, namespace_name, namespace_iri)

    _add_ontology_declaration(
        graph,
        ontology_uri=ontology_uri,
        namespace_name=namespace_name,
        timestamp_iso_z=ts_iso_z,
    )

    turtle_str = graph.serialize(format="turtle")

    # The cap is on UTF-8 bytes (wire size), not characters.
    byte_len = len(turtle_str.encode("utf-8"))
    if byte_len > _MAX_TURTLE_BYTES:
        raise ValueError(
            f"serialize: serialized Turtle exceeds 50 MB cap "
            f"(actual size = {byte_len} bytes; limit = "
            f"{_MAX_TURTLE_BYTES} bytes). Reduce the input graph "
            f"size (e.g. disable include_individuals) before "
            f"retrying."
        )

    reparsed = Graph()
    reparsed.parse(data=turtle_str, format="turtle")
    if not isomorphic(graph, reparsed):
        raise RuntimeError(
            "serialize: round-trip isomorphism check failed — the "
            "parsed-back graph is not isomorphic to the original. "
            "This indicates a serializer bug; please open an issue "
            "with the failing input graph."
        )

    log.info(
        "serialize: emitted %d bytes of Turtle (%d triples, %d namespace prefixes bound)",
        byte_len,
        len(graph),
        sum(1 for _ in graph.namespaces()),
    )

    return turtle_str, graph


def _bind_standard_prefixes(graph: Graph, namespace_name: str, namespace_iri: str) -> None:
    """Bind the canonical set of prefixes on ``graph``.

    All bindings use ``override=True`` so they win against any prior
    bindings on the input graph.
    """
    graph.bind("owl", OWL, override=True)
    graph.bind("rdf", RDF, override=True)
    graph.bind("rdfs", RDFS, override=True)
    graph.bind("xsd", XSD, override=True)
    graph.bind("skos", SKOS, override=True)
    graph.bind("dcterms", DCTERMS, override=True)
    graph.bind("prov", PROV, override=True)

    # The custom prefix keeps the caller's trailing separator so it matches
    # how IRIs are minted upstream (only the ontology subject IRI strips it).
    custom_prefix = namespace_name.lower()
    graph.bind(custom_prefix, Namespace(namespace_iri), override=True)


def _add_ontology_declaration(
    graph: Graph,
    ontology_uri: URIRef,
    namespace_name: str,
    timestamp_iso_z: str,
) -> None:
    """Add (or refresh) the ``owl:Ontology`` declaration block.

    Always idempotent: the function first removes any prior
    ``rdfs:label``, ``owl:versionInfo``, and ``dcterms:created``
    triples on this subject, then re-adds the canonical set. The
    ``rdf:type owl:Ontology`` triple is added once (set semantics
    make a duplicate a no-op).

    The ``owl:versionInfo`` triple uses a plain Literal (no
    datatype) per the convention in the structured induction code.
    The ``dcterms:created`` triple uses ``xsd:dateTime``, which is
    the standard datatype for date-time literals.
    """
    graph.add((ontology_uri, RDF.type, OWL.Ontology))

    # Replace any prior annotation triples on this subject so the
    # serializer is idempotent (calling serialize twice on the same
    # graph does not duplicate annotations).
    graph.remove((ontology_uri, RDFS.label, None))
    graph.remove((ontology_uri, OWL.versionInfo, None))
    graph.remove((ontology_uri, DCTERMS.created, None))

    graph.add((ontology_uri, RDFS.label, Literal(namespace_name, datatype=XSD.string)))

    # owl:versionInfo is a plain literal: its range is rdfs:Literal.
    graph.add((ontology_uri, OWL.versionInfo, Literal(timestamp_iso_z)))

    graph.add(
        (
            ontology_uri,
            DCTERMS.created,
            Literal(timestamp_iso_z, datatype=XSD.dateTime),
        )
    )
