# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A platform foundational load catalogs only the classes in its own namespace."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog.graph_source_resolver import GraphSourceResolution
from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology

pytestmark = pytest.mark.unit

_SCHEMA_LIKE_TTL = """
@prefix rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:   <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:    <http://www.w3.org/2002/07/owl#> .
@prefix schema: <https://schema.org/> .
@prefix dcat:   <http://www.w3.org/ns/dcat#> .
@prefix foaf:   <http://xmlns.com/foaf/0.1/> .

schema:Thing  a rdfs:Class ; rdfs:label "Thing" .
schema:Person a rdfs:Class ; rdfs:label "Person" ; rdfs:subClassOf schema:Thing ;
    owl:equivalentClass foaf:Person .
schema:DataCatalog a rdfs:Class ; rdfs:label "DataCatalog" ; rdfs:subClassOf schema:Thing, dcat:Catalog .
schema:catalog a rdf:Property ; rdfs:domain dcat:Catalog ; rdfs:range foaf:Person .
dcat:Catalog a rdfs:Class .
foaf:Person  a rdfs:Class .
"""


def _ingest(
    ttl: str,
    ontology_id: str,
    *,
    r2rml: str | None = None,
    existing: dict | None = None,
    platform_foundational: bool = True,
) -> tuple[dict, MagicMock, MagicMock]:
    graph_store, vector_store = MagicMock(), MagicMock()
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ) as accumulate,
    ):
        ds.get_ontology_registry.return_value = existing
        ds.extend_ontology_registry.return_value = {}
        result = ingest_ontology(
            graph_store,
            vector_store,
            ttl,
            namespace="ns-test",
            metadata=IngestMetadata(
                ontology_id=ontology_id,
                format="turtle",
                source="unit-test",
                ontology_type="foundational",
                platform_foundational=platform_foundational,
            ),
            validate=False,
            allow_append=existing is not None,
            r2rml_content=r2rml,
        )
    return result, graph_store, accumulate


def _projected(graph_store: MagicMock) -> dict[str, dict]:
    return {
        c["class_uri"]: c for call in graph_store.store_classes_batch.call_args_list for c in call.kwargs["classes"]
    }


def test_ingest_ontology_foreign_classes_not_cataloged_under_loading_ontology():
    result, graph_store, accumulate = _ingest(_SCHEMA_LIKE_TTL, "https://schema.org/")

    own = {"https://schema.org/Thing", "https://schema.org/Person", "https://schema.org/DataCatalog"}
    assert set(_projected(graph_store)) == own
    assert {str(c) for c in accumulate.call_args.kwargs["classes"]} == own
    assert result["class_count"] == 3


def test_ingest_ontology_keeps_edges_to_foreign_classes():
    _, graph_store, _ = _ingest(_SCHEMA_LIKE_TTL, "https://schema.org/")

    data_catalog = _projected(graph_store)["https://schema.org/DataCatalog"]
    assert set(data_catalog["super_classes"]) == {"https://schema.org/Thing", "http://www.w3.org/ns/dcat#Catalog"}
    prop = next(
        p
        for call in graph_store.store_properties_batch.call_args_list
        for p in call.kwargs["properties"]
        if p["prop_uri"] == "https://schema.org/catalog"
    )
    assert prop["domains"] == ["http://www.w3.org/ns/dcat#Catalog"]
    assert prop["ranges"] == ["http://xmlns.com/foaf/0.1/Person"]


@pytest.mark.parametrize("class_ns", ["http://example.org/onto#", "http://example.org/onto/"])
def test_ingest_ontology_iri_without_separator_matches_its_classes(class_ns):
    ttl = f"""
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <{class_ns}A> a rdfs:Class .
    <{class_ns}B> a rdfs:Class .
    <http://example.org/ontology2#C> a rdfs:Class .
    """
    _, graph_store, _ = _ingest(ttl, "http://example.org/onto")

    assert set(_projected(graph_store)) == {f"{class_ns}A", f"{class_ns}B"}


def test_ingest_ontology_classes_outside_ontology_iri_are_all_kept():
    """An ontology IRI that is not its classes' namespace (e.g. ``.../bfo.owl``) filters nothing."""
    ttl = """
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <http://purl.obolibrary.org/obo/BFO_0000001> a rdfs:Class .
    <http://purl.obolibrary.org/obo/BFO_0000002> a rdfs:Class .
    """
    _, graph_store, _ = _ingest(ttl, "http://purl.obolibrary.org/obo/bfo.owl")

    assert len(_projected(graph_store)) == 2


def test_ingest_ontology_exactly_half_own_namespace_filters_nothing():
    ttl = """
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <http://example.org/onto#A> a rdfs:Class .
    <http://example.org/onto#B> a rdfs:Class .
    <http://example.org/vocab#C> a rdfs:Class .
    <http://example.org/vocab#D> a rdfs:Class .
    """
    _, graph_store, _ = _ingest(ttl, "http://example.org/onto")

    assert len(_projected(graph_store)) == 4


def test_ingest_ontology_minority_own_namespace_filters_nothing():
    """One stray class under the ontology IRI does not make every other class foreign."""
    ttl = """
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <http://example.org/vocab#A> a rdfs:Class .
    <http://example.org/vocab#B> a rdfs:Class .
    <http://example.org/onto#Stray> a rdfs:Class .
    """
    _, graph_store, _ = _ingest(ttl, "http://example.org/onto")

    assert len(_projected(graph_store)) == 3


def test_ingest_ontology_mapped_classes_do_not_decide_the_majority():
    """Mapped classes are kept, but they do not make the other classes look foreign."""
    ttl = "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n" + "".join(
        f"<http://example.org/vocab#C{i}> a owl:Class .\n" for i in range(10)
    )
    r2rml = "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n" + "".join(
        f"<http://example.org/tm#T{i}> a rr:TriplesMap ; rr:subjectMap [ rr:class <http://example.org/vocab#C{i}> ] .\n"
        for i in range(6)
    )
    _, graph_store, _ = _ingest(ttl, "http://example.org/vocab#C0", r2rml=r2rml)

    assert len(_projected(graph_store)) == 10


def test_ingest_ontology_r2rml_mapped_class_outside_namespace_is_kept_and_mapped():
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    <http://example.org/onto#A> a owl:Class .
    <http://example.org/onto#B> a owl:Class .
    <http://example.org/other#Orders> a owl:Class .
    """
    r2rml = """
    @prefix rr:  <http://www.w3.org/ns/r2rml#> .
    @prefix coa: <http://coa.amazon.com/vocab/coa#> .
    <http://example.org/tm#Orders> a rr:TriplesMap ;
        coa:datasourceId "ds-1" ;
        rr:subjectMap [ rr:class <http://example.org/other#Orders> ] .
    """
    _, graph_store, _ = _ingest(ttl, "http://example.org/onto", r2rml=r2rml)

    assert _projected(graph_store)["http://example.org/other#Orders"]["is_mapped"] is True


def test_ingest_ontology_graph_bound_class_outside_namespace_is_kept_and_mapped(monkeypatch):
    monkeypatch.setenv("GRAPH_SOURCES_ENABLED", "true")
    ttl = """
    @prefix rdfs:       <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix coa_import: <http://coa.amazon.com/vocab/coa_import#> .
    <https://example.com/onto#A> a rdfs:Class .
    <https://example.com/onto#B> a rdfs:Class .
    <http://xmlns.com/foaf/0.1/Person> a rdfs:Class ; coa_import:dataSourceName "my-neptune" .
    """
    with patch(
        "coa_ontology.catalog.ingest.resolve_graph_sources",
        side_effect=lambda names, ns: {n: GraphSourceResolution(source_id="src-neptune-1") for n in names},
    ):
        _, graph_store, _ = _ingest(ttl, "https://example.com/onto")

    assert _projected(graph_store)["http://xmlns.com/foaf/0.1/Person"]["is_mapped"] is True


def test_ingest_ontology_append_supersedes_all_declared_subjects():
    """A reload retires annotations and embeddings an earlier load made for now-skipped classes."""
    with (
        patch("coa_ontology.catalog.ingest._supersede_incoming_annotations", return_value=0) as annotations,
        patch("coa_ontology.catalog.ingest._supersede_incoming_embeddings", return_value=0) as embeddings,
    ):
        _ingest(_SCHEMA_LIKE_TTL, "https://schema.org/", existing={"status": "ok"})

    foreign = {"http://www.w3.org/ns/dcat#Catalog", "http://xmlns.com/foaf/0.1/Person"}
    assert foreign <= {str(s) for s in annotations.call_args.args[2]}
    assert foreign <= {str(s) for s in embeddings.call_args.args[3]}


def test_ingest_ontology_not_platform_foundational_keeps_every_class():
    """ontology_type is caller-supplied, so an upload typed "foundational" is not narrowed."""
    result, graph_store, _ = _ingest(_SCHEMA_LIKE_TTL, "https://schema.org/", platform_foundational=False)

    assert len(_projected(graph_store)) == 5
    assert result["class_count"] == 5


def test_ingest_metadata_is_not_platform_foundational_by_default():
    assert IngestMetadata(ontology_type="foundational").platform_foundational is False
