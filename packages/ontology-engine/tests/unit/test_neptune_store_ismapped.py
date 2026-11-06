# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NeptuneDBGraphStore.store_class emits the coa:isMapped marker correctly.

This exercises the REAL store_class SPARQL emission (only the network call
``_sparql_update`` is patched), so the exact INSERT DATA triple — including the
coa:isMapped IRI and the ``"true"^^xsd:boolean`` literal the serve T-Box filter
matches against — is verified. Everything else in the pipeline mocks store_class,
so without this the actual triple syntax would be untested until deploy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore

_ISMAPPED_IRI = f"{GRAPH_BASE_URI}/vocab/coa#isMapped"
_QUERY_BACKEND_IRI = f"{GRAPH_BASE_URI}/vocab/coa#queryBackend"
_DATA_SOURCE_ID_IRI = f"{GRAPH_BASE_URI}/vocab/coa#dataSourceId"
_XSD_BOOL = "http://www.w3.org/2001/XMLSchema#boolean"


def _capture_store_class(*, is_mapped: bool) -> str:
    """Run the real store_class with _sparql_update patched; return the SPARQL."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        store.store_class(
            ontology_uri="http://example.org/o#",
            class_uri="http://example.org/o#Orders",
            labels=["Orders"],
            is_mapped=is_mapped,
        )
        assert upd.call_count == 1, "store_class must issue exactly one SPARQL update"
        return upd.call_args[0][0]


def _capture_store_class_graph(*, query_backend, data_source_id) -> str:
    """store_class for a GRAPH class (query_backend/data_source_id set); return SPARQL."""
    store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
    with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
        store.store_class(
            ontology_uri="http://example.org/o#",
            class_uri="http://example.org/o#Person",
            labels=["Person"],
            is_mapped=True,
            query_backend=query_backend,
            data_source_id=data_source_id,
        )
        assert upd.call_count == 1
        return upd.call_args[0][0]


class TestStoreClassIsMappedEmission:
    def test_mapped_class_emits_ismapped_boolean_triple(self):
        sparql = _capture_store_class(is_mapped=True)
        # The exact triple the serve filter (?class <scl#isMapped> true) matches.
        assert _ISMAPPED_IRI in sparql
        assert f'"true"^^<{_XSD_BOOL}>' in sparql
        # Sanity: it's inside an INSERT DATA and carries the class as subject.
        assert "INSERT DATA" in sparql
        assert "http://example.org/o#Orders" in sparql

    def test_unmapped_class_omits_ismapped_triple(self):
        sparql = _capture_store_class(is_mapped=False)
        # Absent marker == unmapped == hidden by the serve filter. No isMapped
        # triple, and definitely no "false" (we never emit a negative).
        assert _ISMAPPED_IRI not in sparql
        # Class is still stored as an owl:Class (just without the marker).
        assert "http://example.org/o#Orders" in sparql
        assert "2002/07/owl#Class" in sparql

    def test_written_triple_matches_serve_filter_pattern_via_rdflib(self):
        """Round-trip: the literal store_class WRITES must satisfy the term the
        serve T-Box filter QUERIES (``?class <scl#isMapped> true``).

        The write side emits ``"true"^^xsd:boolean``; the read side uses the
        SPARQL keyword ``true``. This asserts, via rdflib, that they are the
        SAME term — so the real Neptune write→read actually matches (the one
        thing mocks can't prove short of a live cluster)."""
        import re

        from rdflib import Graph

        # Pull the ACTUAL isMapped triple line store_class emitted (not a
        # hand-written copy), load it into rdflib, and confirm the serve filter's
        # `true` keyword matches the written "true"^^xsd:boolean term.
        sparql = _capture_store_class(is_mapped=True)
        m = re.search(rf"(\S+\s+<{re.escape(_ISMAPPED_IRI)}>\s+\S+\s*\.)", sparql)
        assert m, f"could not find the isMapped triple in emitted SPARQL:\n{sparql}"
        triple = m.group(1)

        g = Graph()
        g.parse(data=triple, format="turtle")
        ask = g.query(f"ASK {{ ?c <{_ISMAPPED_IRI}> true }}")
        assert bool(ask), (
            f"serve filter `<isMapped> true` must match the boolean literal store_class actually wrote: {triple!r}"
        )


class TestStoreClassQueryBackendEmission:
    """A GRAPH class (query_backend/data_source_id set) emits the second serve
    gate signal coa:queryBackend as a PLAIN string literal ``"RDF_SPARQL"`` (the
    term the serve gate matches by term-equality) plus coa:dataSourceId."""

    def test_graph_class_emits_query_backend_plain_string(self):
        sparql = _capture_store_class_graph(query_backend="RDF_SPARQL", data_source_id="src-1")
        # The exact triple the serve gate (?class <coa#queryBackend> "RDF_SPARQL")
        # matches — a PLAIN (untyped) string literal, NOT ^^xsd:string.
        assert _QUERY_BACKEND_IRI in sparql
        assert '"RDF_SPARQL"' in sparql
        assert '"RDF_SPARQL"^^' not in sparql  # must be a plain literal
        # And the stable source id for routing.
        assert _DATA_SOURCE_ID_IRI in sparql
        assert '"src-1"' in sparql

    def test_query_backend_matches_serve_gate_via_rdflib(self):
        """Round-trip: the queryBackend triple store_class WRITES must satisfy the
        serve gate term ``?class <coa#queryBackend> "RDF_SPARQL"`` (plain literal)."""
        import re

        from rdflib import Graph

        sparql = _capture_store_class_graph(query_backend="RDF_SPARQL", data_source_id="src-1")
        m = re.search(rf"(\S+\s+<{re.escape(_QUERY_BACKEND_IRI)}>\s+\S+\s*\.)", sparql)
        assert m, f"could not find the queryBackend triple in emitted SPARQL:\n{sparql}"
        triple = m.group(1)
        g = Graph()
        g.parse(data=triple, format="turtle")
        ask = g.query(f'ASK {{ ?c <{_QUERY_BACKEND_IRI}> "RDF_SPARQL" }}')
        assert bool(ask), f'serve gate `<queryBackend> "RDF_SPARQL"` must match what store_class wrote: {triple!r}'

    def test_sql_class_omits_query_backend_and_source_id(self):
        # A normal (SQL/legacy) class passes neither param → neither triple is
        # emitted, so serve's default-SQL-on-absence behavior is preserved.
        sparql = _capture_store_class(is_mapped=True)
        assert _QUERY_BACKEND_IRI not in sparql
        assert _DATA_SOURCE_ID_IRI not in sparql

    def test_batch_path_emits_the_same_graph_markers(self):
        """store_classes_batch must emit byte-identical triples to store_class."""
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_update") as upd:
            store.store_classes_batch(
                "http://example.org/o#",
                [
                    {
                        "class_uri": "http://example.org/o#Person",
                        "labels": ["Person"],
                        "is_mapped": True,
                        "query_backend": "RDF_SPARQL",
                        "data_source_id": "src-1",
                    },
                    {"class_uri": "http://example.org/o#Order", "labels": ["Order"], "is_mapped": True},
                ],
            )
        sparql = "".join(c.args[0] for c in upd.call_args_list)
        assert f'<http://example.org/o#Person> <{_QUERY_BACKEND_IRI}> "RDF_SPARQL" .' in sparql
        assert f'<http://example.org/o#Person> <{_DATA_SOURCE_ID_IRI}> "src-1" .' in sparql
        assert f"<http://example.org/o#Order> <{_QUERY_BACKEND_IRI}>" not in sparql


class TestNAStoreAcceptsGraphMarkers:
    def test_na_store_class_accepts_and_ignores_graph_markers(self):
        """NA has no marker persistence yet; it must accept the kwargs, not raise."""
        from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore

        store = NeptuneAnalyticsGraphStore(namespace="ns-test")
        with patch("coa_ontology.stores.na_graph.na_store.store_class", return_value={"uri": "u"}) as na:
            store.store_class(
                "http://example.org/o#",
                "http://example.org/o#Person",
                is_mapped=True,
                query_backend="RDF_SPARQL",
                data_source_id="src-1",
            )
        assert "query_backend" not in na.call_args.kwargs
        assert "data_source_id" not in na.call_args.kwargs


class TestMappedClassBackends:
    """``mapped_class_backends`` reads which backend answers each class across the namespace's graphs."""

    def _run(self, bindings):
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            q.return_value = {"results": {"bindings": bindings}}
            result = store.mapped_class_backends({"http://example.org/o#Order"}, "ns-test")
        return result, q.call_args[0][0]

    def test_an_unmarked_mapped_class_is_sql_and_a_marked_one_is_its_backend(self):
        result, _ = self._run(
            [
                {"c": {"value": "http://example.org/o#Order"}},
                {"c": {"value": "http://example.org/o#Person"}, "backend": {"value": "RDF_SPARQL"}},
            ]
        )
        assert result == {"http://example.org/o#Order": "SQL", "http://example.org/o#Person": "RDF_SPARQL"}

    def test_the_query_spans_every_graph_of_the_namespace_for_the_given_classes(self):
        _, sparql = self._run([])
        assert "<http://example.org/o#Order>" in sparql
        assert f"<{_ISMAPPED_IRI}>" in sparql
        assert f"<{_QUERY_BACKEND_IRI}>" in sparql
        assert "/ns-test/" in sparql

    def test_no_classes_reads_nothing(self):
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            assert store.mapped_class_backends(set(), "ns-test") == {}
        q.assert_not_called()

    @pytest.mark.parametrize("odd", ["tag:example.org,2026:Extra", "Order", "http://example.org/o#a b"])
    def test_a_class_iri_neptune_cannot_hold_is_skipped_not_raised(self, odd):
        """It can carry no marker, so it is answered by no backend; the others are still read."""
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            q.return_value = {"results": {"bindings": [{"c": {"value": "http://example.org/o#Order"}}]}}
            result = store.mapped_class_backends({"http://example.org/o#Order", odd}, "ns-test")
        assert result == {"http://example.org/o#Order": "SQL"}
        assert f"<{odd}>" not in q.call_args[0][0]

    def test_only_unholdable_iris_read_nothing(self):
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            assert store.mapped_class_backends({"tag:example.org,2026:Extra"}, "ns-test") == {}
            assert store.mapped_class_graph_sources({"tag:example.org,2026:Extra"}, "ns-test") == {}
        q.assert_not_called()


class TestMappedClassGraphSources:
    """``mapped_class_graph_sources`` reads which graph sources each class is already bound to in the namespace."""

    def _run(self, bindings):
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            q.return_value = {"results": {"bindings": bindings}}
            result = store.mapped_class_graph_sources({"http://example.org/o#Order"}, "ns-test")
        return result, q.call_args[0][0]

    def test_each_class_maps_to_every_source_it_is_bound_to(self):
        result, _ = self._run(
            [
                {"c": {"value": "http://example.org/o#Order"}, "src": {"value": "src-1"}},
                {"c": {"value": "http://example.org/o#Order"}, "src": {"value": "src-2"}},
            ]
        )
        assert result == {"http://example.org/o#Order": {"src-1", "src-2"}}

    def test_the_query_reads_graph_bound_classes_across_the_namespace(self):
        _, sparql = self._run([])
        assert "<http://example.org/o#Order>" in sparql
        assert f"<{_DATA_SOURCE_ID_IRI}>" in sparql
        assert f"<{_QUERY_BACKEND_IRI}>" in sparql and '"RDF_SPARQL"' in sparql
        assert "/ns-test/" in sparql

    def test_no_classes_reads_nothing(self):
        store = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        with patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q:
            assert store.mapped_class_graph_sources(set(), "ns-test") == {}
        q.assert_not_called()
