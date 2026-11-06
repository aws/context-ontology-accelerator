# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Graph (RDF_SPARQL) import binding: ``coa_import:dataSourceName`` → server markers.

Two layers, both mocked (no network):

1. ``graph_source_resolver.resolve_graph_sources`` — names → RDF_SPARQL source ids from
   one paginated, namespace-scoped read of the sources table.
2. ``ingest_ontology`` — a class naming a registered graph source is stored mapped with
   ``query_backend``/``data_source_id``, its embedding is stamped, the directive never
   reaches the loaded Turtle, and any bad binding rejects the import before any write.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology.catalog.graph_source_resolver import (
    GraphSourceLookupError,
    GraphSourceResolution,
    ResolutionFailure,
    resolve_graph_sources,
)
from coa_ontology.catalog.ingest import (
    IngestMetadata,
    IngestStoreError,
    IngestValidationError,
    ingest_ontology,
)
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore

pytestmark = pytest.mark.unit

_RESOLVER = "coa_ontology.catalog.graph_source_resolver"
_INGEST = "coa_ontology.catalog.ingest"

_PREFIXES = """
@prefix owl:        <http://www.w3.org/2002/07/owl#> .
@prefix rdfs:       <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:         <http://example.org/o#> .
@prefix coa:        <http://coa.amazon.com/vocab/coa#> .
@prefix coa_import: <http://coa.amazon.com/vocab/coa_import#> .
<http://example.org/o#> a owl:Ontology .
"""

_GRAPH_ONTOLOGY_TTL = (
    _PREFIXES
    + """
ex:Person a owl:Class ; rdfs:label "Person" ; coa_import:dataSourceName "my-neptune" .
ex:Unbacked a owl:Class ; rdfs:label "Unbacked" .
"""
)

_PERSON = "http://example.org/o#Person"
_UNBACKED = "http://example.org/o#Unbacked"
_ORDER = "http://example.org/o#Order"


def _resolved(**by_name):
    """A resolve_graph_sources double returning the given per-name outcomes."""

    def _resolve(names, namespace_id):
        return {name: by_name[name] for name in names}

    return _resolve


_OK = {"my-neptune": GraphSourceResolution(source_id="src-neptune-1")}


def _ingest(ttl=_GRAPH_ONTOLOGY_TTL, *, resolve=None, graph_store=None, r2rml=None):
    graph_store = graph_store or MagicMock()
    vector_store = MagicMock()
    captured: dict = {}

    def _fake_accumulate(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "count": 0, "model_id": "test", "index": "test"}

    with (
        patch(f"{_INGEST}.dynamo_store") as ds,
        patch(f"{_INGEST}._accumulate_embeddings", side_effect=_fake_accumulate),
        patch(f"{_INGEST}.resolve_graph_sources", side_effect=resolve or _resolved(**_OK)) as resolver,
    ):
        ds.get_ontology_registry.return_value = None
        result = ingest_ontology(
            graph_store,
            vector_store,
            ttl,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            r2rml_content=r2rml,
        )
    return result, graph_store, captured, resolver


@pytest.fixture
def graph_sources_on(monkeypatch):
    monkeypatch.setenv("GRAPH_SOURCES_ENABLED", "true")


def _specs(graph_store) -> dict:
    return {
        c["class_uri"]: c for call in graph_store.store_classes_batch.call_args_list for c in call.kwargs["classes"]
    }


# ── Resolver ────────────────────────────────────────────────────────────


class TestResolveGraphSources:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("SOURCES_TABLE", "sources")

    def _table(self, *pages):
        table = MagicMock()
        table.query.side_effect = list(pages)
        return table

    def _resolve(self, table, names):
        with patch(f"{_RESOLVER}.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            return resolve_graph_sources(names, "ns-test")

    def test_resolves_a_unique_graph_source(self):
        table = self._table(
            {
                "Items": [
                    {"name": "my-neptune", "sourceSubType": "RDF_SPARQL", "sourceId": "src-1", "status": "APPROVED"},
                    {"name": "orders", "sourceSubType": "JDBC_DATABASE", "sourceId": "src-2"},
                ]
            }
        )
        assert self._resolve(table, ["my-neptune"]) == {"my-neptune": GraphSourceResolution(source_id="src-1")}

    def test_reads_the_namespace_partition_consistently(self):
        table = self._table({"Items": []})
        self._resolve(table, ["x"])
        kwargs = table.query.call_args.kwargs
        assert kwargs["ConsistentRead"] is True
        assert "IndexName" not in kwargs

    def test_follows_pagination(self):
        table = self._table(
            {"Items": [{"name": "a", "sourceSubType": "RDF_SPARQL", "sourceId": "s1"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"name": "a", "sourceSubType": "RDF_SPARQL", "sourceId": "s2"}]},
        )
        result = self._resolve(table, ["a"])
        # The duplicate on page 2 is only visible if both pages are read.
        assert result["a"].failure is ResolutionFailure.AMBIGUOUS
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}

    def test_unknown_name(self):
        result = self._resolve(self._table({"Items": []}), ["gone"])
        assert result["gone"].failure is ResolutionFailure.UNKNOWN
        assert "register the graph source" in result["gone"].message

    def test_non_graph_source(self):
        table = self._table({"Items": [{"name": "sql", "sourceSubType": "JDBC_DATABASE", "sourceId": "s"}]})
        result = self._resolve(table, ["sql"])
        assert result["sql"].failure is ResolutionFailure.NOT_GRAPH
        assert "JDBC_DATABASE" in result["sql"].message

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED"])
    def test_a_source_being_deleted_is_ignored(self, status):
        table = self._table(
            {
                "Items": [
                    {"name": "g", "sourceSubType": "RDF_SPARQL", "sourceId": "old", "status": status},
                    {"name": "g", "sourceSubType": "RDF_SPARQL", "sourceId": "new", "status": "APPROVED"},
                ]
            }
        )
        assert self._resolve(table, ["g"]) == {"g": GraphSourceResolution(source_id="new")}

    def test_one_read_serves_every_name(self):
        table = self._table(
            {
                "Items": [
                    {"name": "a", "sourceSubType": "RDF_SPARQL", "sourceId": "s1"},
                    {"name": "b", "sourceSubType": "RDF_SPARQL", "sourceId": "s2"},
                ]
            }
        )
        result = self._resolve(table, ["a", "b", "a"])
        assert {n: r.source_id for n, r in result.items()} == {"a": "s1", "b": "s2"}
        assert table.query.call_count == 1

    def test_table_failure_is_a_lookup_error(self):
        table = MagicMock()
        table.query.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Query")
        with pytest.raises(GraphSourceLookupError):
            self._resolve(table, ["a"])

    def test_unset_table_is_a_lookup_error(self, monkeypatch):
        monkeypatch.delenv("SOURCES_TABLE")
        with pytest.raises(GraphSourceLookupError):
            resolve_graph_sources(["a"], "ns-test")

    def test_no_names_reads_nothing(self):
        with patch(f"{_RESOLVER}.boto3.resource") as resource:
            assert resolve_graph_sources([], "ns-test") == {}
        resource.assert_not_called()


# ── Ingest ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("graph_sources_on")
class TestIngestGraphBinding:
    def test_graph_class_is_stored_mapped_with_backend_and_source(self):
        _, graph_store, _, _ = _ingest()
        person = _specs(graph_store)[_PERSON]
        assert person["is_mapped"] is True
        assert person["query_backend"] == "RDF_SPARQL"
        assert person["data_source_id"] == "src-neptune-1"

    def test_unbound_class_carries_no_graph_keys(self):
        """Absent == SQL; a SQL spec must keep the keys every GraphStore accepts."""
        _, graph_store, _, _ = _ingest()
        unbacked = _specs(graph_store)[_UNBACKED]
        assert unbacked["is_mapped"] is False
        assert "query_backend" not in unbacked
        assert "data_source_id" not in unbacked

    def test_embedding_maps_carry_the_graph_source(self):
        _, _, captured, _ = _ingest()
        assert captured["class_to_datasource"] == {_PERSON: "src-neptune-1"}
        assert captured["class_to_graphsource"] == {_PERSON: "src-neptune-1"}

    def test_directive_never_reaches_the_loaded_turtle(self):
        _, graph_store, _, _ = _ingest()
        turtle = graph_store.load_turtle.call_args.kwargs["turtle"]
        assert "coa_import" not in turtle
        assert "dataSourceName" not in turtle

    def test_mapped_class_count_includes_graph_classes(self):
        result, _, _, _ = _ingest()
        assert result["mapped_class_count"] == 1

    def test_caller_forged_markers_are_stripped(self):
        ttl = (
            _PREFIXES
            + """
ex:Person a owl:Class ; coa_import:dataSourceName "my-neptune" .
ex:Forged a owl:Class ; coa:isMapped true ; coa:queryBackend "RDF_SPARQL" ; coa:dataSourceId "src-x" .
"""
        )
        _, graph_store, _, _ = _ingest(ttl)
        turtle = graph_store.load_turtle.call_args.kwargs["turtle"]
        assert "src-x" not in turtle
        assert "queryBackend" not in turtle
        forged = _specs(graph_store)["http://example.org/o#Forged"]
        assert forged["is_mapped"] is False
        assert "query_backend" not in forged

    def test_ontology_without_directives_does_not_read_the_sources_table(self):
        _, _, _, resolver = _ingest(_PREFIXES + "ex:Order a owl:Class .\n")
        resolver.assert_not_called()

    def test_store_without_batch_gets_graph_kwargs_only_for_graph_classes(self):
        graph_store = MagicMock()
        del graph_store.store_classes_batch
        _ingest(graph_store=graph_store)
        calls = {c.kwargs["class_uri"]: c.kwargs for c in graph_store.store_class.call_args_list}
        assert calls[_PERSON]["query_backend"] == "RDF_SPARQL"
        assert "query_backend" not in calls[_UNBACKED]


@pytest.mark.usefixtures("graph_sources_on")
class TestIngestGraphBindingRejections:
    def _rejected(self, ttl=_GRAPH_ONTOLOGY_TTL, **kwargs):
        graph_store = MagicMock()
        with pytest.raises(IngestValidationError) as exc:
            _ingest(ttl, graph_store=graph_store, **kwargs)
        # Validated before any write: nothing was projected or loaded.
        graph_store.create_ontology.assert_not_called()
        graph_store.store_classes_batch.assert_not_called()
        graph_store.load_turtle.assert_not_called()
        return exc.value

    def test_unresolved_name_rejects_with_a_class_attributed_finding(self):
        failure = GraphSourceResolution(failure=ResolutionFailure.UNKNOWN, message='No data source named "my-neptune"')
        err = self._rejected(resolve=_resolved(**{"my-neptune": failure}))
        (finding,) = err.findings
        assert finding.code == "graph_source_unknown"
        assert finding.subject == _PERSON
        assert finding.details == {"dataSourceName": "my-neptune"}
        assert _PERSON in str(err)

    def test_every_bad_binding_is_reported_not_just_the_first(self):
        ttl = (
            _PREFIXES
            + """
ex:A a owl:Class ; coa_import:dataSourceName "gone" .
ex:B a owl:Class ; coa_import:dataSourceName "sql" .
ex:C a owl:Class ; coa_import:dataSourceName "gone" .
"""
        )
        resolve = _resolved(
            gone=GraphSourceResolution(failure=ResolutionFailure.UNKNOWN, message="unknown"),
            sql=GraphSourceResolution(failure=ResolutionFailure.NOT_GRAPH, message="not graph"),
        )
        err = self._rejected(ttl, resolve=resolve)
        assert {(f.subject, f.code) for f in err.findings} == {
            ("http://example.org/o#A", "graph_source_unknown"),
            ("http://example.org/o#B", "graph_source_not_graph"),
            ("http://example.org/o#C", "graph_source_unknown"),
        }

    def test_two_names_on_one_class_is_rejected(self):
        ttl = _PREFIXES + 'ex:Person a owl:Class ; coa_import:dataSourceName "a", "b" .\n'
        (finding,) = self._rejected(ttl).findings
        assert finding.code == "graph_source_multiple_names"

    def test_empty_name_is_rejected(self):
        ttl = _PREFIXES + 'ex:Person a owl:Class ; coa_import:dataSourceName "  " .\n'
        (finding,) = self._rejected(ttl).findings
        assert finding.code == "graph_source_name_empty"

    def test_directive_on_a_non_class_is_rejected(self):
        ttl = _PREFIXES + 'ex:hasName a owl:DatatypeProperty ; coa_import:dataSourceName "my-neptune" .\n'
        (finding,) = self._rejected(ttl).findings
        assert finding.code == "graph_source_marker_not_on_class"
        assert finding.subject == "http://example.org/o#hasName"

    def test_class_mapped_by_r2rml_and_a_graph_source_is_rejected(self):
        ttl = _PREFIXES + 'ex:Order a owl:Class ; coa_import:dataSourceName "my-neptune" .\n'
        r2rml = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
<urn:tm:order> a rr:TriplesMap ; coa:datasourceId "ds-sql" ;
    rr:subjectMap [ rr:class <http://example.org/o#Order> ] .
"""
        (finding,) = self._rejected(ttl, r2rml=r2rml).findings
        assert finding.code == "graph_source_conflicts_r2rml"
        assert finding.subject == _ORDER

    def test_sources_table_failure_is_a_store_error(self):
        def _boom(names, namespace_id):
            raise GraphSourceLookupError("denied")

        with pytest.raises(IngestStoreError, match="graph source lookup failed"):
            _ingest(resolve=_boom)


_ORDER_R2RML = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
<urn:tm:order> a rr:TriplesMap ; coa:datasourceId "ds-sql" ;
    rr:subjectMap [ rr:class <http://example.org/o#Order> ] .
"""


@pytest.mark.usefixtures("graph_sources_on")
class TestIngestCrossImportBackendConflict:
    """A class already answered by one backend in the namespace cannot be bound to the other by a later import."""

    def _store(self, existing: dict[str, str], sources: dict[str, set[str]] | None = None):
        graph_store = MagicMock()
        graph_store.mapped_class_backends.return_value = existing
        graph_store.mapped_class_graph_sources.return_value = sources or {}
        return graph_store

    def _rejected(self, ttl, existing, **kwargs):
        graph_store = self._store(existing)
        with pytest.raises(IngestValidationError) as exc:
            _ingest(ttl, graph_store=graph_store, **kwargs)
        graph_store.create_ontology.assert_not_called()
        graph_store.store_classes_batch.assert_not_called()
        graph_store.load_turtle.assert_not_called()
        return exc.value

    def test_graph_binding_of_a_class_another_ontology_maps_by_r2rml_is_rejected(self):
        ttl = _PREFIXES + 'ex:Order a owl:Class ; coa_import:dataSourceName "my-neptune" .\n'
        (finding,) = self._rejected(ttl, {_ORDER: "SQL"}).findings
        assert finding.code == "graph_source_conflicts_r2rml"
        assert finding.subject == _ORDER

    def test_r2rml_mapping_of_a_class_another_ontology_graph_binds_is_rejected(self):
        ttl = _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n'
        (finding,) = self._rejected(ttl, {_ORDER: "RDF_SPARQL"}, r2rml=_ORDER_R2RML).findings
        assert finding.code == "r2rml_conflicts_graph_source"
        assert finding.subject == _ORDER

    def test_a_failed_backend_read_refuses_the_import_while_graph_sources_are_on(self):
        graph_store = self._store({})
        graph_store.mapped_class_backends.side_effect = RuntimeError("neptune down")
        with pytest.raises(IngestStoreError, match="existing class backend lookup failed"):
            _ingest(
                _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n', graph_store=graph_store, r2rml=_ORDER_R2RML
            )
        graph_store.load_turtle.assert_not_called()

    def test_an_r2rml_import_of_a_class_already_on_sql_is_accepted(self):
        graph_store = self._store({_ORDER: "SQL"})
        _ingest(
            _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n', graph_store=graph_store, r2rml=_ORDER_R2RML
        )
        graph_store.mapped_class_backends.assert_called_once_with({_ORDER}, "ns-test")
        graph_store.load_turtle.assert_called_once()

    def test_the_store_is_asked_about_exactly_the_incoming_mapped_classes(self):
        graph_store = self._store({})
        _ingest(graph_store=graph_store)
        graph_store.mapped_class_backends.assert_called_once_with({_PERSON}, "ns-test")

    def test_graph_binding_of_a_class_another_ontology_binds_to_another_graph_source_is_rejected(self):
        # Serve picks the source from the top retrieval hit, so one class on two sources would answer from either.
        graph_store = self._store({_PERSON: "RDF_SPARQL"}, {_PERSON: {"src-other"}})
        with pytest.raises(IngestValidationError) as exc:
            _ingest(graph_store=graph_store)
        (finding,) = exc.value.findings
        assert finding.code == "graph_source_conflicts_graph_source"
        assert finding.subject == _PERSON
        graph_store.store_classes_batch.assert_not_called()
        graph_store.mapped_class_graph_sources.assert_called_once_with({_PERSON}, "ns-test")

    def test_a_class_bound_again_to_the_same_graph_source_is_accepted(self):
        graph_store = self._store({_PERSON: "RDF_SPARQL"}, {_PERSON: {"src-neptune-1"}})
        _ingest(graph_store=graph_store)
        assert _specs(graph_store)[_PERSON]["data_source_id"] == "src-neptune-1"

    def test_a_class_already_on_the_same_backend_is_accepted(self):
        graph_store = self._store({_PERSON: "RDF_SPARQL"})
        _ingest(graph_store=graph_store)
        assert _specs(graph_store)[_PERSON]["query_backend"] == "RDF_SPARQL"


class TestIngestGraphSourcesSwitchedOff:
    @pytest.fixture(autouse=True)
    def _graph_sources_off(self, monkeypatch):
        monkeypatch.delenv("GRAPH_SOURCES_ENABLED", raising=False)

    def test_a_graph_binding_is_refused_before_the_sources_table_is_read(self):
        graph_store = MagicMock()
        resolve = MagicMock(side_effect=_resolved(**_OK))
        with pytest.raises(IngestValidationError) as exc:
            _ingest(graph_store=graph_store, resolve=resolve)
        (finding,) = exc.value.findings
        assert finding.code == "graph_sources_disabled"
        assert finding.subject == _PERSON
        assert "graph_sources" in finding.message
        assert finding.message in str(exc.value)
        resolve.assert_not_called()
        graph_store.mapped_class_backends.assert_not_called()
        graph_store.create_ontology.assert_not_called()
        graph_store.store_classes_batch.assert_not_called()
        graph_store.load_turtle.assert_not_called()

    def test_an_r2rml_mapping_of_a_graph_bound_class_is_still_refused(self):
        """Classes bound while graph sources were on keep their one backend."""
        graph_store = MagicMock()
        graph_store.mapped_class_backends.return_value = {_ORDER: "RDF_SPARQL"}
        graph_store.mapped_class_graph_sources.return_value = {}
        with pytest.raises(IngestValidationError) as exc:
            _ingest(
                _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n', graph_store=graph_store, r2rml=_ORDER_R2RML
            )
        assert [f.code for f in exc.value.findings] == ["r2rml_conflicts_graph_source"]
        graph_store.load_turtle.assert_not_called()

    def test_a_class_iri_neptune_cannot_hold_does_not_open_the_check(self):
        """One rr:class the store cannot render must not turn the read into a failure that lets the import through."""
        real = NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace="ns-test")
        graph_store = MagicMock()
        graph_store.mapped_class_backends.side_effect = real.mapped_class_backends
        graph_store.mapped_class_graph_sources.return_value = {}
        r2rml = _ORDER_R2RML + (
            '<urn:tm:odd> a rr:TriplesMap ; coa:datasourceId "ds-sql" ;\n'
            "    rr:subjectMap [ rr:class <tag:example.org,2026:Extra> ] .\n"
        )
        held = {"results": {"bindings": [{"c": {"value": _ORDER}, "backend": {"value": "RDF_SPARQL"}}]}}
        ttl = _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n'
        with (
            patch("coa_ontology.stores.neptune_db_graph._sparql_query", return_value=held),
            pytest.raises(IngestValidationError) as exc,
        ):
            _ingest(ttl, graph_store=graph_store, r2rml=r2rml)
        assert [f.code for f in exc.value.findings] == ["r2rml_conflicts_graph_source"]
        graph_store.load_turtle.assert_not_called()

    def test_a_failed_backend_read_refuses_the_import_while_graph_sources_are_off_too(self):
        """Classes bound while graph sources were on keep one backend even when the check can't run."""
        graph_store = MagicMock()
        graph_store.mapped_class_backends.side_effect = RuntimeError("neptune down")
        with pytest.raises(IngestStoreError, match="existing class backend lookup failed"):
            _ingest(
                _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n', graph_store=graph_store, r2rml=_ORDER_R2RML
            )
        graph_store.load_turtle.assert_not_called()

    def test_a_failed_backend_read_is_logged_apart_from_a_failed_write(self, caplog):
        """Operators can tell this 502 (only the check's read failed) from a store write failure."""
        graph_store = MagicMock()
        graph_store.mapped_class_backends.side_effect = RuntimeError("neptune down at https://cluster.internal")
        with caplog.at_level(logging.WARNING, logger="coa_ontology.catalog.ingest"), pytest.raises(IngestStoreError):
            _ingest(
                _PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n', graph_store=graph_store, r2rml=_ORDER_R2RML
            )
        lookups = [r for r in caplog.records if r.getMessage().startswith("ontology_backend_lookup_failed")]
        assert len(lookups) == 1
        assert "graph_sources_enabled=False" in lookups[0].getMessage()
        assert "cluster.internal" not in lookups[0].getMessage()

    def test_an_ontology_without_the_directive_imports_as_before(self):
        result, graph_store, captured, resolver = _ingest(_PREFIXES + 'ex:Order a owl:Class ; rdfs:label "Order" .\n')
        resolver.assert_not_called()
        assert result["mapped_class_count"] == 0
        assert captured["class_to_graphsource"] == {}
        graph_store.load_turtle.assert_called_once()
        assert _ORDER in _specs(graph_store)
