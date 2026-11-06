# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for _accumulate_embeddings — specifically the R2RML data_source_id extraction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import GRAPH_BASE_URI
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

RR = Namespace("http://www.w3.org/ns/r2rml#")
SCL = Namespace(f"{GRAPH_BASE_URI}/vocab/coa#")
EX = Namespace("http://example.org/ontology#")


def _build_r2rml_graph_with_datasource(class_uri: str, datasource_id: str) -> Graph:
    """Build a minimal R2RML graph with a TriplesMap that links a class to a data_source_id."""
    g = Graph()
    tmap = URIRef("http://example.org/r2rml#TriplesMap_Test")
    subj_map = URIRef("http://example.org/r2rml#SubjectMap_Test")

    g.add((tmap, RDF.type, RR.TriplesMap))
    g.add((tmap, SCL.datasourceId, Literal(datasource_id)))
    g.add((tmap, RR.subjectMap, subj_map))
    g.add((subj_map, RR["class"], URIRef(class_uri)))

    # Add the class as owl:Class with a label
    g.add((URIRef(class_uri), RDF.type, OWL.Class))
    g.add((URIRef(class_uri), RDFS.label, Literal("TestClass")))

    return g


def _build_r2rml_graph_multi_datasource() -> Graph:
    """Build a graph with multiple TriplesMaps pointing to different data sources."""
    g = Graph()

    # TriplesMap 1 → Customer class → datasource-alpha
    tmap1 = URIRef("http://example.org/r2rml#TriplesMap_Customer")
    subj1 = URIRef("http://example.org/r2rml#SubjectMap_Customer")
    g.add((tmap1, RDF.type, RR.TriplesMap))
    g.add((tmap1, SCL.datasourceId, Literal("datasource-alpha")))
    g.add((tmap1, RR.subjectMap, subj1))
    g.add((subj1, RR["class"], EX.Customer))
    g.add((EX.Customer, RDF.type, OWL.Class))
    g.add((EX.Customer, RDFS.label, Literal("Customer")))

    # TriplesMap 2 → Order class → datasource-beta
    tmap2 = URIRef("http://example.org/r2rml#TriplesMap_Order")
    subj2 = URIRef("http://example.org/r2rml#SubjectMap_Order")
    g.add((tmap2, RDF.type, RR.TriplesMap))
    g.add((tmap2, SCL.datasourceId, Literal("datasource-beta")))
    g.add((tmap2, RR.subjectMap, subj2))
    g.add((subj2, RR["class"], EX.Order))
    g.add((EX.Order, RDF.type, OWL.Class))
    g.add((EX.Order, RDFS.label, Literal("Order")))

    # TriplesMap 3 → Product class → no datasourceId (should get empty string)
    tmap3 = URIRef("http://example.org/r2rml#TriplesMap_Product")
    subj3 = URIRef("http://example.org/r2rml#SubjectMap_Product")
    g.add((tmap3, RDF.type, RR.TriplesMap))
    g.add((tmap3, RR.subjectMap, subj3))
    g.add((subj3, RR["class"], EX.Product))
    g.add((EX.Product, RDF.type, OWL.Class))
    g.add((EX.Product, RDFS.label, Literal("Product")))

    # Add a DatatypeProperty for Customer
    g.add((EX.hasName, RDF.type, OWL.DatatypeProperty))
    g.add((EX.hasName, RDFS.domain, EX.Customer))
    g.add((EX.hasName, RDFS.label, Literal("hasName")))

    return g


@pytest.mark.unit
class TestAccumulateEmbeddingsDataSourceExtraction:
    """Test that _accumulate_embeddings correctly extracts data_source_id from R2RML."""

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_single_class_gets_datasource_id(self, mock_bedrock_cls):
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        g = _build_r2rml_graph_with_datasource(str(EX.Customer), "ds-123-abc")

        # New contract: the class→datasource map is derived from the R2RML by
        # the caller and passed in (not scanned from the ontology graph, which
        # never carries TriplesMaps at accept time).
        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
            class_to_datasource={str(EX.Customer): "ds-123-abc"},
        )

        assert result["status"] == "ok"
        assert result["count"] == 1

        # Verify the stored item has data_source_id
        call_args = mock_vector_store.store_embeddings_batch.call_args[0][0]
        assert len(call_args) == 1
        item = call_args[0]
        assert item["data_source_id"] == "ds-123-abc"
        assert item["entity_type"] == "class"
        assert item["namespace"] == "test-ns"

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_multi_datasource_maps_correctly(self, mock_bedrock_cls):
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        # 3 classes + 1 property = 4 embeddings
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024] * 4
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        g = _build_r2rml_graph_multi_datasource()

        # Multi-datasource map passed in by the caller. Here we deliberately omit
        # Product from the map to exercise the embedding side: a class absent from
        # the map (or with an empty id) gets NO data_source_id field written, so
        # the NL->SQL presence filter drops it. (Map DERIVATION — where Product is
        # now included with an empty id — is covered in TestClassDatasourceMapFromR2rml.)
        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer, EX.Order, EX.Product},
            obj_props=set(),
            dt_props={EX.hasName},
            namespace="test-ns",
            class_to_datasource={
                str(EX.Customer): "datasource-alpha",
                str(EX.Order): "datasource-beta",
            },
        )

        assert result["status"] == "ok"
        assert result["count"] == 4

        items = mock_vector_store.store_embeddings_batch.call_args[0][0]
        items_by_uri = {it["entity_uri"]: it for it in items}

        # Customer → datasource-alpha
        assert items_by_uri[str(EX.Customer)]["data_source_id"] == "datasource-alpha"
        # Order → datasource-beta
        assert items_by_uri[str(EX.Order)]["data_source_id"] == "datasource-beta"
        # Product → no datasourceId, key should not be present
        assert "data_source_id" not in items_by_uri[str(EX.Product)]
        # Property → no datasourceId (properties don't get data_source_id)
        assert "data_source_id" not in items_by_uri[str(EX.hasName)]


@pytest.mark.unit
class TestClassDatasourceMapFromR2rml:
    """The R2RML→(class→datasource) derivation feeding the Tier-2 isMapped signal."""

    def test_derives_class_to_datasource(self):
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

        r2rml = _build_r2rml_graph_multi_datasource().serialize(format="turtle")
        mapping = _class_datasource_map_from_r2rml(r2rml)

        # Membership == "is the rr:class of a TriplesMap" (SQL-backed / mapped),
        # INDEPENDENT of the datasource-id. Customer/Order carry an id; Product's
        # TriplesMap has none but is STILL mapped, with an empty-string id.
        assert mapping == {
            str(EX.Customer): "datasource-alpha",
            str(EX.Order): "datasource-beta",
            str(EX.Product): "",
        }

    def test_class_without_datasource_id_is_mapped_with_empty_value(self):
        """A TriplesMap with rr:class but no coa:datasourceId → class is MAPPED
        (present, drives isMapped=True) with an empty ds-id (NL->SQL will drop
        it, Ontop can still answer it). Regression guard for the decoupling of
        membership from the routing id."""
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

        r2rml = _build_r2rml_graph_multi_datasource().serialize(format="turtle")
        mapping = _class_datasource_map_from_r2rml(r2rml)

        assert str(EX.Product) in mapping  # mapped (rr:class present)
        assert mapping[str(EX.Product)] == ""  # but no routable datasource id

    def test_multiple_triplesmaps_keep_last_nonempty_datasource_id(self):
        """A class mapped by several TriplesMaps keeps the LAST NON-EMPTY id; an
        id-less TriplesMap never blanks out an id already found."""
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml
        from rdflib import BNode, Graph, Literal, Namespace, URIRef
        from rdflib.namespace import RDF

        RR = Namespace("http://www.w3.org/ns/r2rml#")
        SCL = Namespace(f"{GRAPH_BASE_URI}/vocab/coa#")
        cls = URIRef("http://example.org/ontology#Shared")

        def _tmap(g: Graph, name: str, ds_id: str | None) -> None:
            tm = URIRef(f"http://example.org/r2rml#{name}")
            g.add((tm, RDF.type, RR.TriplesMap))
            if ds_id is not None:
                g.add((tm, SCL.datasourceId, Literal(ds_id)))
            sm = BNode()
            g.add((tm, RR.subjectMap, sm))
            g.add((sm, RR["class"], cls))

        # id -> id-less: id-less must NOT blank the found id.
        g1 = Graph()
        _tmap(g1, "T1", "ds-first")
        _tmap(g1, "T2", None)
        assert _class_datasource_map_from_r2rml(g1.serialize(format="turtle")) == {str(cls): "ds-first"}

        # id-less -> id: the later non-empty id wins.
        g2 = Graph()
        _tmap(g2, "T1", None)
        _tmap(g2, "T2", "ds-second")
        assert _class_datasource_map_from_r2rml(g2.serialize(format="turtle")) == {str(cls): "ds-second"}

    def test_empty_or_none_r2rml_yields_empty_map(self):
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

        # Unstructured proposals have no R2RML — no class is mapped.
        assert _class_datasource_map_from_r2rml(None) == {}
        assert _class_datasource_map_from_r2rml("") == {}
        assert _class_datasource_map_from_r2rml("   ") == {}

    def test_malformed_r2rml_fails_soft(self):
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

        # Garbage must not raise — a class with no derivable mapping is unmapped.
        assert _class_datasource_map_from_r2rml("this is not turtle {{{") == {}

    def test_last_writer_wins_for_multi_mapped_class(self):
        from coa_ontology.catalog.ingest import _class_datasource_map_from_r2rml

        # A class mapped by TWO TriplesMaps (two datasources): documented
        # last-writer-wins behavior. Membership (= mapped) is what matters.
        g = Graph()
        for i, ds in enumerate(("ds-1", "ds-2")):
            tmap = URIRef(f"http://example.org/r2rml#TMap_{i}")
            subj = URIRef(f"http://example.org/r2rml#SMap_{i}")
            g.add((tmap, RDF.type, RR.TriplesMap))
            g.add((tmap, SCL.datasourceId, Literal(ds)))
            g.add((tmap, RR.subjectMap, subj))
            g.add((subj, RR["class"], EX.Customer))
        mapping = _class_datasource_map_from_r2rml(g.serialize(format="turtle"))
        assert str(EX.Customer) in mapping  # mapped regardless of which ds won
        assert mapping[str(EX.Customer)] in ("ds-1", "ds-2")

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_no_r2rml_means_no_datasource_id(self, mock_bedrock_cls):
        """When graph has no TriplesMap annotations, no item gets data_source_id."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        # Minimal graph — just a class, no R2RML
        g = Graph()
        g.add((EX.Standalone, RDF.type, OWL.Class))
        g.add((EX.Standalone, RDFS.label, Literal("Standalone")))

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Standalone},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "ok"
        items = mock_vector_store.store_embeddings_batch.call_args[0][0]
        assert len(items) == 1
        assert "data_source_id" not in items[0]

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_empty_classes_skips(self, mock_bedrock_cls):
        """When no classes or properties, embedding is skipped."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_vector_store = MagicMock()
        g = Graph()

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes=set(),
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "skipped"
        assert result["count"] == 0
        mock_vector_store.store_embeddings_batch.assert_not_called()

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_class_text_includes_columns(self, mock_bedrock_cls):
        """The EMBEDDED text lists column NAMES (retrieval signal) but omits the
        data type — the type (:string/:decimal) is low-cardinality noise for
        recall and belongs only in the generation context (context_text)."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        # one vector per entry: the Customer class + the hasName property
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024, [0.2] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        g = Graph()
        g.add((EX.Customer, RDF.type, OWL.Class))
        g.add((EX.Customer, RDFS.label, Literal("Customer")))
        g.add((EX.hasName, RDF.type, OWL.DatatypeProperty))
        g.add((EX.hasName, RDFS.domain, EX.Customer))
        g.add((EX.hasName, RDFS.label, Literal("name")))
        g.add((EX.hasName, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))

        _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer},
            obj_props=set(),
            dt_props={EX.hasName},
            namespace="test-ns",
        )

        # EMBEDDED text: column name present, data type absent.
        texts = mock_bedrock.embed_texts.call_args[0][0]
        class_text = texts[0]
        assert "Table: Customer" in class_text
        assert "Columns:" in class_text
        assert "name" in class_text
        assert "name:string" not in class_text
        assert ":string" not in class_text

        # STORED context_text (for the generation prompt): carries the type.
        items = mock_vector_store.store_embeddings_batch.call_args[0][0]
        class_item = next(it for it in items if it["text"].startswith("Table: Customer"))
        assert class_item["context_text"].count("name:string") == 1


@pytest.mark.unit
class TestDistinctValuesNotInClassText:
    """coa:distinctValues must NOT leak into the class *embedding* text. Sampled
    enum values are a signal for the serve NL→SPARQL *generation* prompt
    (injected via the Tier-2 TBoxContextBuilder), not for the retrieval vector —
    putting them in the embedded text would pollute recall without helping
    generation. The values still live on the property in the graph
    (coa:distinctValues); this test guards that the embedding text stays clean."""

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_distinct_values_do_not_render_in_class_text(self, mock_bedrock_cls):
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "us.cohere.embed-v4:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024, [0.2] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock
        mock_vector_store = MagicMock()

        g = Graph()
        cls = EX.Orders
        prop = EX.Orders_returnState
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal("Orders")))
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, cls))
        g.add((prop, RDFS.label, Literal("return_state")))
        # the distinct enum values sampled at scan time (still on the graph)
        g.add((prop, SCL.distinctValues, Literal("RETURNED")))
        g.add((prop, SCL.distinctValues, Literal("RETURN_REQUESTED")))

        _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={cls},
            obj_props=set(),
            dt_props={prop},
            namespace="ns",
        )

        # the property name is legitimate context; the ENUM VALUES are not — they
        # must not appear in the embedded text.
        texts = mock_bedrock.embed_texts.call_args[0][0]
        class_text = next(t for t in texts if t.startswith("Table: Orders"))
        assert "return_state" in class_text
        assert "allowed values:" not in class_text
        assert "RETURN_REQUESTED" not in class_text


@pytest.mark.unit
class TestAccumulateEmbeddingsQueryBackend:
    """The SECOND retrieval signal: _accumulate_embeddings stamps
    query_backend="RDF_SPARQL" on a graph class doc (from class_to_graphsource)
    and OMITS it for SQL/plain classes (backward compatible)."""

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_graph_class_gets_query_backend_sql_class_does_not(self, mock_bedrock_cls):
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024, [0.2] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        g = Graph()
        # A graph class (bound to an RDF_SPARQL source) and a plain SQL class.
        g.add((EX.Alliance, RDF.type, OWL.Class))
        g.add((EX.Alliance, RDFS.label, Literal("Airline Alliance")))
        g.add((EX.Orders, RDF.type, OWL.Class))
        g.add((EX.Orders, RDFS.label, Literal("Orders")))

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Alliance, EX.Orders},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
            # Alliance is graph-bound; Orders is SQL-mapped (data_source_id only).
            class_to_datasource={str(EX.Orders): "ds-sql-1", str(EX.Alliance): "src-neptune-1"},
            class_to_graphsource={str(EX.Alliance): "src-neptune-1"},
        )
        assert result["status"] == "ok"

        items = {i["entity_uri"]: i for i in mock_vector_store.store_embeddings_batch.call_args[0][0]}
        # Graph class carries the second signal.
        assert items[str(EX.Alliance)]["query_backend"] == "RDF_SPARQL"
        # SQL class does NOT carry query_backend (absent == SQL, backward compatible),
        # but still carries its data_source_id.
        assert "query_backend" not in items[str(EX.Orders)]
        assert items[str(EX.Orders)]["data_source_id"] == "ds-sql-1"

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_no_graphsource_map_omits_query_backend_everywhere(self, mock_bedrock_cls):
        # Pure-SQL / legacy ingest (no class_to_graphsource) → no doc gets the
        # field, so existing namespaces are byte-for-byte unchanged.
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "m"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock
        mock_vector_store = MagicMock()

        g = Graph()
        g.add((EX.Orders, RDF.type, OWL.Class))
        g.add((EX.Orders, RDFS.label, Literal("Orders")))

        _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes={EX.Orders},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
            class_to_datasource={str(EX.Orders): "ds-sql-1"},
            # class_to_graphsource omitted (None)
        )
        item = mock_vector_store.store_embeddings_batch.call_args[0][0][0]
        assert "query_backend" not in item
