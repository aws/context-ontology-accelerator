# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Component test for alternative-label persistence across model stages.

Exercises normalization → induction → serialization end to end with an
in-memory embedding client and IRI resolver. It reaches no AWS service, so
it runs in the unit suite; it is a component test rather than a unit test
because the assertions are about the contract *between* those stages.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_ontology.inducer.unstructured.services.class_normalizer import normalize
from coa_ontology.inducer.unstructured.services.induction_rules import (
    induce_classes,
    induce_concepts,
    induce_datatype_properties,
    induce_object_properties,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import InMemoryIRIResolver
from coa_ontology.inducer.unstructured.services.serializer import serialize
from coa_ontology.inducer.unstructured.stores.protocol import ClassRecord, FactRecord, TopicRecord
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, SKOS

pytestmark = pytest.mark.unit

_NAMESPACE = "business-vocabulary"
_ONTOLOGY = "equipment"
_BASE_IRI = f"{GRAPH_BASE_URI}/namespace/{_NAMESPACE}/ontology/{_ONTOLOGY}/"


class _FixedEmbeddingClient:
    """Return one vector so the second class is an exact semantic match."""

    def embed_text(self, _text: str) -> list[float]:
        return [1.0, 0.0]


def _spo(predicate: str) -> FactRecord:
    return FactRecord(
        subject_value="圧縮機A",
        subject_class="Compressor",
        predicate=predicate,
        object_value="圧縮機B",
        object_class="コンプレッサー",
        complement=None,
    )


def _spc(predicate: str, value: str) -> FactRecord:
    return FactRecord(
        subject_value="圧縮機A",
        subject_class="Compressor",
        predicate=predicate,
        object_value=None,
        object_class=None,
        complement=value,
    )


def test_alternative_labels_survive_normalization_induction_and_serialization():
    resolver = InMemoryIRIResolver(namespace=_NAMESPACE, ontology_name=_ONTOLOGY)
    normalizer_results = normalize(
        classes={
            "Compressor": "Equipment that compresses a gas.",
            "コンプレッサー": "気体を圧縮する装置。",
        },
        embedding_client=_FixedEmbeddingClient(),
        iri_resolver=resolver,
        source_mode="schema",
    )
    class_iris = {label: result.class_iri for label, result in normalizer_results.items() if result.class_iri}

    graph = induce_classes(
        class_distribution=[
            ClassRecord(value="Compressor", count=5),
            ClassRecord(value="コンプレッサー", count=4),
        ],
        descriptions={
            "Compressor": "Equipment that compresses a gas.",
            "コンプレッサー": "気体を圧縮する装置。",
        },
        normalizer_results=normalizer_results,
        iri_resolver=resolver,
        namespace_language="ja",
    )
    facts = [
        _spo("HAS_PART"),
        _spo("HAS_PART"),
        _spo("has part"),
        _spc("MODEL_NO", "A-1"),
        _spc("MODEL_NO", "B-2"),
        _spc("model no", "C-3"),
    ]
    graph += induce_object_properties(
        facts=facts,
        class_iris=class_iris,
        iri_resolver=resolver,
        namespace_language="ja",
    )
    graph += induce_datatype_properties(
        facts=facts,
        class_iris=class_iris,
        iri_resolver=resolver,
        namespace_language="ja",
    )
    graph += induce_concepts(
        topics=[TopicRecord(topic_id="topic-1", value="コンプレッサー", statement_count=3)],
        iri_resolver=resolver,
        namespace_name=_ONTOLOGY,
        namespace_language="ja",
        class_iris=class_iris,
    )

    class_uri = URIRef(normalizer_results["Compressor"].class_iri)
    assert normalizer_results["コンプレッサー"].class_iri == str(class_uri)
    assert set(graph.subjects(RDF.type, OWL.Class)) == {class_uri}
    assert list(graph.objects(class_uri, RDFS.label)) == [Literal("Compressor", datatype=XSD.string)]
    assert set(graph.objects(class_uri, SKOS.altLabel)) == {Literal("コンプレッサー", lang="ja")}

    object_property = next(graph.subjects(RDF.type, OWL.ObjectProperty))
    assert set(map(str, graph.objects(object_property, RDFS.label))) == {"HAS_PART"}
    assert set(map(str, graph.objects(object_property, SKOS.altLabel))) == {"has part"}

    datatype_property = next(graph.subjects(RDF.type, OWL.DatatypeProperty))
    assert set(map(str, graph.objects(datatype_property, RDFS.label))) == {"MODEL_NO"}
    assert set(map(str, graph.objects(datatype_property, SKOS.altLabel))) == {"model no"}

    concept_uri = next(graph.subjects(RDF.type, SKOS.Concept))
    assert (class_uri, DCTERMS.subject, concept_uri) in graph

    turtle, augmented_graph = serialize(
        graph=graph,
        namespace_name=_ONTOLOGY,
        namespace_iri=_BASE_IRI,
        timestamp=datetime(2026, 9, 14, tzinfo=UTC),
    )
    reparsed = Graph()
    reparsed.parse(data=turtle, format="turtle")
    assert isomorphic(augmented_graph, reparsed)
