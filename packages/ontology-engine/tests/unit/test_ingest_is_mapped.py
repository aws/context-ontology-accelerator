# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Layer-0 Tier-2 answerability signal: ingest_ontology tags R2RML-mapped classes.

End-to-end (through ``ingest_ontology`` with mocked stores) verification that a
class which is the ``rr:class`` of an R2RML TriplesMap is stored with
``is_mapped=True`` (=> serve T-Box context exposes it to the structured-query
prompt), while a class NOT in the R2RML is stored with ``is_mapped=False``
(=> hidden). Unstructured proposals pass no R2RML → every class is unmapped.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology
from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore

_ONTOLOGY_TTL = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <http://example.org/o#> .

<http://example.org/o#> a owl:Ontology .
ex:Orders    a owl:Class ; rdfs:label "Orders" .
ex:DocConcept a owl:Class ; rdfs:label "DocConcept" .
"""

# R2RML maps ONLY ex:Orders (structured/table-backed). ex:DocConcept has no
# TriplesMap → it is an unmapped (e.g. document-induced) class.
_R2RML_TTL = """
@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex:  <http://example.org/o#> .

ex:TriplesMap_Orders a rr:TriplesMap ;
    coa:datasourceId "ds-orders-1" ;
    rr:subjectMap [ rr:class ex:Orders ] .
"""


def _ingest_with_r2rml(r2rml_content, *, return_result=False):
    graph_store, vector_store = MagicMock(), MagicMock()
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ),
    ):
        ds.get_ontology_registry.return_value = None
        ds.put_namespace_meta.return_value = None
        result = ingest_ontology(
            graph_store,
            vector_store,
            _ONTOLOGY_TTL,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            validate=False,
            allow_append=True,
            r2rml_content=r2rml_content,
        )
    if return_result:
        return result
    return _is_mapped_by_uri(graph_store)


def _is_mapped_by_uri(graph_store) -> dict:
    """Extract is_mapped per class URI from the batch or the per-item projection path."""
    if graph_store.store_classes_batch.call_args_list:
        out: dict = {}
        for call in graph_store.store_classes_batch.call_args_list:
            for c in call.kwargs["classes"]:
                out[c["class_uri"]] = c.get("is_mapped")
        return out
    return {c.kwargs["class_uri"]: c.kwargs.get("is_mapped") for c in graph_store.store_class.call_args_list}


class TestIngestIsMappedTagging:
    def test_mapped_class_tagged_true_unmapped_false(self):
        by_uri = _ingest_with_r2rml(_R2RML_TTL)
        assert by_uri["http://example.org/o#Orders"] is True
        assert by_uri["http://example.org/o#DocConcept"] is False

    def test_no_r2rml_all_classes_unmapped(self):
        # Unstructured proposal path: no R2RML → nothing is mapped.
        by_uri = _ingest_with_r2rml(None)
        assert by_uri["http://example.org/o#Orders"] is False
        assert by_uri["http://example.org/o#DocConcept"] is False

    def test_per_item_fallback_when_backend_lacks_batch(self):
        # A bare MagicMock auto-provides store_classes_batch, so the other tests
        # only ever exercise the batch path. A backend WITHOUT the batch methods
        # (e.g. Neptune Analytics) must take the per-item store_class fallback —
        # spec= restricts the mock's attributes so hasattr(store, batch) is False.
        graph_store = MagicMock(spec=NeptuneAnalyticsGraphStore)
        vector_store = MagicMock()
        assert not hasattr(graph_store, "store_classes_batch")  # gate will pick fallback
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
            ),
        ):
            ds.get_ontology_registry.return_value = None
            ds.put_namespace_meta.return_value = None
            ingest_ontology(
                graph_store,
                vector_store,
                _ONTOLOGY_TTL,
                namespace="ns-test",
                metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
                validate=False,
                allow_append=True,
                r2rml_content=_R2RML_TTL,
            )
        by_uri = {c.kwargs["class_uri"]: c.kwargs.get("is_mapped") for c in graph_store.store_class.call_args_list}
        assert by_uri["http://example.org/o#Orders"] is True
        assert by_uri["http://example.org/o#DocConcept"] is False


# An ontology payload that FORGES the marker: ex:DocConcept has no R2RML
# TriplesMap (genuinely unmapped) yet asserts coa:isMapped true itself. This is
# the injection the strip must neutralize.
_FORGED_ONTOLOGY_TTL = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa:  <http://coa.amazon.com/vocab/coa#> .
@prefix ex:   <http://example.org/o#> .

<http://example.org/o#> a owl:Ontology .
ex:Orders     a owl:Class ; rdfs:label "Orders" .
ex:DocConcept a owl:Class ; rdfs:label "DocConcept" ;
    coa:isMapped "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
"""


def _ingest_capture_turtle(ontology_ttl, r2rml_content):
    """Ingest and return (is_mapped-by-uri, turtle-string-passed-to-load_turtle)."""
    graph_store, vector_store = MagicMock(), MagicMock()
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
        ),
    ):
        ds.get_ontology_registry.return_value = None
        ds.put_namespace_meta.return_value = None
        ingest_ontology(
            graph_store,
            vector_store,
            ontology_ttl,
            namespace="ns-test",
            metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
            validate=False,
            allow_append=True,
            r2rml_content=r2rml_content,
        )
    by_uri = _is_mapped_by_uri(graph_store)
    turtle = graph_store.load_turtle.call_args.kwargs["turtle"]
    return by_uri, turtle


class TestIngestStripsForgedIsMapped:
    """coa:isMapped is a server-derived trust boundary: a caller-supplied marker
    in the ontology payload must NOT survive to the graph, or an unmapped/unbacked
    class could force itself into the Tier-2 structured prompt."""

    def test_forged_marker_stripped_from_loaded_turtle(self):
        _, turtle = _ingest_capture_turtle(_FORGED_ONTOLOGY_TTL, _R2RML_TTL)
        # The raw Turtle bulk-loaded into Neptune must carry NO isMapped triple —
        # store_class is the only permitted emitter.
        assert "isMapped" not in turtle

    def test_forged_marker_does_not_flip_store_class(self):
        by_uri, _ = _ingest_capture_turtle(_FORGED_ONTOLOGY_TTL, _R2RML_TTL)
        # DocConcept forged the marker but has no TriplesMap → still stored unmapped;
        # Orders is mapped via R2RML as normal (proves the strip didn't over-reach).
        assert by_uri["http://example.org/o#DocConcept"] is False
        assert by_uri["http://example.org/o#Orders"] is True


class TestIngestMappedClassCount:
    """Observability: ingest reports how many classes are Tier-2-mapped, the
    signal that tells operators whether a namespace will be dark to Tier-2."""

    def test_result_reports_mapped_class_count(self):
        result = _ingest_with_r2rml(_R2RML_TTL, return_result=True)
        # Orders is mapped (has a TriplesMap), DocConcept is not → exactly 1.
        assert result["mapped_class_count"] == 1
        assert result["class_count"] >= 2  # both classes ingested

    def test_no_r2rml_reports_zero_mapped(self):
        # Unstructured path: 0 mapped classes — the dark-to-Tier-2 signal.
        result = _ingest_with_r2rml(None, return_result=True)
        assert result["mapped_class_count"] == 0


class TestProjectionFailureRollback:
    """A class/property projection that fails partway must roll the ontology
    back on a FRESH ingest (Neptune DB has no multi-statement transaction, so a
    batch that fails on a later chunk leaves earlier writes committed). On an
    APPEND the pre-existing state is live and must NOT be wiped.
    """

    def _run(self, *, existing_row):
        from coa_ontology.catalog.ingest import IngestStoreError, ingest_ontology

        graph_store, vector_store = MagicMock(), MagicMock()
        # Bare MagicMock auto-provides store_classes_batch → the batch path runs.
        graph_store.store_classes_batch.side_effect = RuntimeError("neptune 400 on chunk 2")
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "t", "index": "t"},
            ),
        ):
            # get_ontology_registry is the fresh-vs-append signal: a row + allow_append
            # → appending=True; None → fresh ingest.
            ds.get_ontology_registry.return_value = existing_row
            ds.put_namespace_meta.return_value = None
            with pytest.raises(IngestStoreError, match="class projection failed"):
                ingest_ontology(
                    graph_store,
                    vector_store,
                    _ONTOLOGY_TTL,
                    namespace="ns-test",
                    metadata=IngestMetadata(ontology_id="http://example.org/o#", format="turtle", source="unit-test"),
                    validate=False,
                    allow_append=True,
                    r2rml_content=None,
                )
        return graph_store

    def test_fresh_ingest_rolls_back_on_projection_failure(self):
        # No existing row → fresh ingest → the failed projection must trigger a
        # delete_ontology cleanup so no partial class set survives.
        graph_store = self._run(existing_row=None)
        graph_store.delete_ontology.assert_called_once_with("http://example.org/o#")

    def test_append_does_not_roll_back_on_projection_failure(self):
        # A real pre-existing row + allow_append → appending=True → the live
        # ontology must NOT be wiped on a transient projection failure.
        graph_store = self._run(existing_row={"uri": "http://example.org/o#", "status": "active"})
        graph_store.delete_ontology.assert_not_called()
