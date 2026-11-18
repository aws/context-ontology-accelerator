# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ingest format sniffing + parse fallback.

Covers the FOAF failure mode: a source's declared format (rdf+xml) does not
match what the server actually returned (JSON-LD via content negotiation), so
the declared-format parse fails and ingest must fall back to a sniffed format
instead of dropping the whole load.
"""

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.catalog.ingest import _sniff_rdf_format


class TestSniffRdfFormat:
    def test_json_ld_object(self):
        assert _sniff_rdf_format('  {"@context": {}}') == "json-ld"

    def test_json_ld_array(self):
        assert _sniff_rdf_format("[\n  {}\n]") == "json-ld"

    def test_rdf_xml_declaration(self):
        assert _sniff_rdf_format('<?xml version="1.0"?><rdf:RDF/>') == "xml"

    def test_rdf_xml_root_without_decl(self):
        assert _sniff_rdf_format("<rdf:RDF xmlns:rdf='...'></rdf:RDF>") == "xml"

    def test_turtle_prefix(self):
        assert _sniff_rdf_format("@prefix ex: <http://ex/> .") == "turtle"

    def test_sparql_style_prefix(self):
        assert _sniff_rdf_format("PREFIX ex: <http://ex/>") == "turtle"

    def test_unknown_returns_none(self):
        assert _sniff_rdf_format("just some text") is None

    def test_empty_returns_none(self):
        assert _sniff_rdf_format("   \n  ") is None


class TestIngestParseFallback:
    """End-to-end: declared format wrong, sniffed format recovers the parse."""

    def _ingest(self, content, declared_format):
        from unittest.mock import MagicMock, patch

        from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology

        graph_store, vector_store = MagicMock(), MagicMock()
        # Patch the registry and embeddings so no AWS call is made.
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test"},
            ),
        ):
            ds.get_ontology_registry.return_value = None
            ds.put_namespace_meta.return_value = None
            return ingest_ontology(
                graph_store,
                vector_store,
                content,
                namespace="ns-test",
                metadata=IngestMetadata(
                    ontology_id="http://example.org/o#",
                    format=declared_format,
                    source="unit-test",
                ),
                validate=False,
                allow_append=True,
            )

    def test_jsonld_body_declared_as_rdfxml_recovers(self):
        """The FOAF scenario: format says rdf+xml, body is JSON-LD → must parse."""
        jsonld = (
            '{"@context": {"owl": "http://www.w3.org/2002/07/owl#"},'
            ' "@graph": ['
            '   {"@id": "http://example.org/o#", "@type": "owl:Ontology"},'
            '   {"@id": "http://example.org/o#Person", "@type": "owl:Class"},'
            '   {"@id": "http://example.org/o#Org", "@type": "owl:Class"}'
            " ]}"
        )
        # Should NOT raise IngestParseError — the sniffed json-ld format recovers it.
        result = self._ingest(jsonld, "rdf+xml")
        assert result["ontology_id"] == "http://example.org/o#"
        assert result["class_count"] >= 1

    def test_correct_turtle_still_parses(self):
        ttl = (
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "<http://example.org/o#> a owl:Ontology .\n"
            "<http://example.org/o#Person> a owl:Class .\n"
        )
        result = self._ingest(ttl, "turtle")
        assert result["class_count"] >= 1

    def test_garbage_still_raises_parse_error(self):
        from coa_ontology.catalog.ingest import IngestParseError

        with pytest.raises(IngestParseError):
            self._ingest("this is not rdf in any format", "turtle")
