# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for _persist_induced_to_s3 and /datasources/induced endpoint."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestPersistInducedToS3:
    """Tests for proposals._persist_induced_to_s3."""

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_writes_ontology_and_r2rml(self, mock_dynamo, mock_gsp, mock_s3_client):
        from coa_ontology.proposals import _persist_induced_to_s3

        mock_gsp.return_value = "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n:Claim a owl:Class ."
        mock_dynamo.list_proposals.return_value = [
            {"r2rml_turtle": "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n:TM1 a rr:TriplesMap ."},
            {"r2rml_turtle": "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n:TM2 a rr:TriplesMap ."},
        ]
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3

        _persist_induced_to_s3(
            "http://example.org/ns/test/onto#",
            "my-namespace",
            "https://coa.amazon.com/graph/my-namespace/encoded-uri",
        )

        assert mock_s3.put_object.call_count == 4
        calls = mock_s3.put_object.call_args_list
        # induced/ writes
        assert calls[0].kwargs["Key"].endswith(".ttl")
        assert "my-namespace/induced/" in calls[0].kwargs["Key"]
        assert b"owl:Class" in calls[0].kwargs["Body"]
        assert calls[1].kwargs["Key"].endswith(".r2rml.ttl")
        assert b"TriplesMap" in calls[1].kwargs["Body"]
        # latest/ writes (VKG consumption)
        assert calls[2].kwargs["Key"] == "ontologies/my-namespace/latest/ontology.ttl"
        assert b"owl:Class" in calls[2].kwargs["Body"]
        assert calls[3].kwargs["Key"] == "ontologies/my-namespace/latest/mappings.r2rml"
        assert b"TriplesMap" in calls[3].kwargs["Body"]

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_skips_when_no_turtle(self, mock_dynamo, mock_gsp, mock_s3_client):
        from coa_ontology.proposals import _persist_induced_to_s3

        mock_gsp.return_value = None
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3

        _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")
        mock_s3.put_object.assert_not_called()

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_skips_r2rml_when_none(self, mock_dynamo, mock_gsp, mock_s3_client):
        from coa_ontology.proposals import _persist_induced_to_s3

        mock_gsp.return_value = ":X a owl:Class ."
        mock_dynamo.list_proposals.return_value = [{"r2rml_turtle": ""}]
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3

        _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")
        # induced ontology + latest ontology (no r2rml)
        assert mock_s3.put_object.call_count == 2
        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        assert any("induced/" in k for k in keys)
        assert any("latest/ontology.ttl" in k for k in keys)

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_deduplicates_prefixes(self, mock_dynamo, mock_gsp, mock_s3_client):
        from coa_ontology.proposals import _persist_induced_to_s3

        mock_gsp.return_value = ":X a owl:Class ."
        mock_dynamo.list_proposals.return_value = [
            {"r2rml_turtle": "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n:TM1 a rr:TriplesMap ."},
            {"r2rml_turtle": "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n:TM2 a rr:TriplesMap ."},
        ]
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3

        _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")

        # Check the induced r2rml (index 1)
        r2rml_body = mock_s3.put_object.call_args_list[1].kwargs["Body"].decode()
        assert r2rml_body.count("@prefix rr:") == 1
        assert ":TM1" in r2rml_body
        assert ":TM2" in r2rml_body
        # Check the latest r2rml (index 3) has same content
        latest_r2rml = mock_s3.put_object.call_args_list[3].kwargs["Body"].decode()
        assert latest_r2rml == r2rml_body

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_deduplicates_triplesmaps_across_proposals(self, mock_dynamo, mock_gsp, mock_s3_client):
        from coa_ontology.proposals import _persist_induced_to_s3

        # Two proposals both define TriplesMap_Claim with a logicalTable
        r2rml_common = (
            "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n"
            "@prefix ind: <http://ex.org/> .\n"
            "ind:TriplesMap_Claim a rr:TriplesMap ;\n"
            '    rr:logicalTable [ rr:tableName "\\"claim\\"" ] .\n'
        )
        mock_gsp.return_value = "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<http://ex.org/Claim> a owl:Class ."
        mock_dynamo.list_proposals.return_value = [
            {"r2rml_turtle": r2rml_common},
            {"r2rml_turtle": r2rml_common},
        ]
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3

        _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")

        # The latest R2RML (index 3) should have TriplesMap_Claim only once
        latest_r2rml = mock_s3.put_object.call_args_list[3].kwargs["Body"].decode()
        assert latest_r2rml.count("TriplesMap_Claim") >= 1
        # Ontop would fail if there are 2 logicalTable nodes — verify only 1
        assert latest_r2rml.count("tableName") == 1

    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_raises_on_s3_error(self, mock_dynamo, mock_gsp, mock_s3_client):
        """#467: an S3 write failure must RAISE, not be swallowed as a warning.

        Previously this returned silently, leaving DDB ``accepted`` + Neptune
        populated but the VKG ``latest/`` copy stale (a silent Neptune<->S3
        split-brain). It's now a retried step in the accept pipeline
        (``_run_accept_step`` → on exhaustion the proposal is ``accept_failed``),
        so the primitive must surface the failure."""
        from coa_ontology.proposals import _persist_induced_to_s3

        mock_gsp.return_value = ":X a owl:Class ."
        mock_dynamo.list_proposals.return_value = []
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = Exception("S3 boom")
        mock_s3_client.return_value = mock_s3

        with pytest.raises(Exception, match="S3 boom"):
            _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")


class TestGenerateSchemaSql:
    """proposals._generate_schema_sql: the three outcomes stay distinct —
    ``None`` for the empty states (no datasources / no tables) and a raised
    ``SchemaSqlGenerationError`` for a genuine failure (#457).
    """

    @patch("coa_ontology.proposals.dynamo_store")
    def test_returns_none_when_no_datasources(self, mock_dynamo):
        from coa_ontology.proposals import _generate_schema_sql

        # Accepted proposals exist but none carry datasource_ids (e.g. an
        # unstructured ontology). Nothing to describe — legitimate None.
        mock_dynamo.list_proposals.return_value = [{"metadata": {}}, {"metadata": {"datasource_ids": []}}]
        assert _generate_schema_sql("ns", "http://x.org/o#") is None

    @patch("coa_ontology.induce_catalog._catalog_to_tables")
    @patch("coa_ontology.induce_catalog._fetch_catalog")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_returns_none_when_datasources_yield_no_tables(self, mock_dynamo, mock_fetch, mock_to_tables):
        from coa_ontology.proposals import _generate_schema_sql

        mock_dynamo.list_proposals.return_value = [{"metadata": {"datasource_ids": ["ds-a"]}}]
        mock_fetch.return_value = {"tables": []}
        mock_to_tables.return_value = []  # datasource resolved but exposed no tables
        assert _generate_schema_sql("ns", "http://x.org/o#") is None

    @patch("coa_ontology.induce_catalog._fetch_catalog")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_raises_on_catalog_fetch_failure(self, mock_dynamo, mock_fetch):
        # The regression: a real catalog-fetch error must NOT be masked as the
        # None "nothing to describe" state — it must raise so the caller can log
        # it distinctly (#457).
        from coa_ontology.proposals import SchemaSqlGenerationError, _generate_schema_sql

        mock_dynamo.list_proposals.return_value = [{"metadata": {"datasource_ids": ["ds-a"]}}]
        mock_fetch.side_effect = RuntimeError("catalog endpoint 503")
        with pytest.raises(SchemaSqlGenerationError, match="catalog endpoint 503"):
            _generate_schema_sql("ns", "http://x.org/o#")


class TestPersistSchemaSqlFailureIsNonFatalButVisible:
    """A schema.sql generation failure during persist logs at ERROR, writes no
    schema.sql, and does not crash persist (#457).
    """

    @patch("coa_ontology.proposals._generate_schema_sql")
    @patch("boto3.client")
    @patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_generation_failure_logs_error_and_skips_schema_sql(
        self, mock_dynamo, mock_gsp, mock_s3_client, mock_gen, caplog
    ):
        import logging

        from coa_ontology.proposals import SchemaSqlGenerationError, _persist_induced_to_s3

        mock_gsp.return_value = ":X a owl:Class ."
        mock_dynamo.list_proposals.return_value = []
        mock_s3 = MagicMock()
        mock_s3_client.return_value = mock_s3
        mock_gen.side_effect = SchemaSqlGenerationError("RuntimeError: catalog endpoint 503")

        with caplog.at_level(logging.ERROR):
            # Must not raise — persist stays best-effort.
            _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")

        # No schema.sql was written (generation failed).
        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        assert not any(k.endswith("schema.sql") for k in keys)
        # But the failure is visible as an ERROR log, not silently swallowed.
        assert any("schema.sql generation FAILED" in r.message for r in caplog.records)


class TestListInducedDatasources:
    """Tests for GET /induce/datasources/induced."""

    @patch("coa_ontology.induce_catalog.dynamo_store")
    def test_returns_distinct_datasources(self, mock_dynamo):
        from coa_ontology.induce_catalog import list_induced_datasources

        mock_dynamo.list_proposals.return_value = [
            {
                "proposal_id": "p1",
                "ontology_id": "http://x.org/o1#",
                "metadata": {
                    "datasource_ids": ["ds-a", "ds-b"],
                    "label": "Test",
                    "tables_processed": 5,
                    "columns_processed": 20,
                    "novel_classes_created": 10,
                },
            },
            {
                "proposal_id": "p2",
                "ontology_id": "http://x.org/o2#",
                "metadata": {
                    "datasource_ids": ["ds-a"],
                    "label": "Test2",
                    "tables_processed": 3,
                    "columns_processed": 10,
                    "novel_classes_created": 5,
                },
            },
        ]

        result = list_induced_datasources(namespace="test-ns")

        mock_dynamo.list_proposals.assert_called_once_with(namespace="test-ns", status="accepted")
        assert len(result) == 2
        ids = {r["datasourceId"] for r in result}
        assert ids == {"ds-a", "ds-b"}

    @patch("coa_ontology.induce_catalog.dynamo_store")
    def test_empty_when_no_proposals(self, mock_dynamo):
        from coa_ontology.induce_catalog import list_induced_datasources

        mock_dynamo.list_proposals.return_value = []
        result = list_induced_datasources(namespace="empty-ns")
        assert result == []


class TestCleanOntologyForVkg:
    """Tests for proposals._clean_ontology_for_vkg."""

    def test_strips_rdf_property_and_haskey(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://ex.org/Claim> a owl:Class .
<http://ex.org/id> a rdf:Property , owl:DatatypeProperty .
<http://ex.org/Claim> owl:hasKey ( <http://ex.org/id> ) .
<http://ex.org/id> <https://ontology-workbench.local/vocab#definedBy> <http://ex.org/> .
"""
        result = _clean_ontology_for_vkg(turtle)
        assert "rdf:Property" not in result or "a rdf:Property" not in result
        assert "hasKey" not in result
        assert "ontology-workbench" not in result
        assert "owl:Class" in result
        assert "DatatypeProperty" in result

    def test_strips_owl_imports(self):
        """A grounded ontology declares owl:imports pointing at the foundational
        ontology it grounds against. Ontop/OWLAPI tries to network-resolve each
        import and throws UnloadableImportException (UnknownHostException) on a
        non-dereferenceable URI, failing every query. The VKG copy must drop it
        — the grounding relationship is carried by the subClassOf axioms."""
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://hcp360.example.org/onto#> a owl:Ontology ;
    owl:imports <https://parallax.aws/commercial/ontology> .
<https://hcp360.example.org/onto#Hcp> a owl:Class ;
    rdfs:subClassOf <https://parallax.aws/commercial/ontology/HealthcareProfessional> .
"""
        result = _clean_ontology_for_vkg(turtle)
        assert "owl:imports" not in result
        # No owl:imports triple in any prefix form (e.g. a re-serialized full URI).
        assert "/owl#imports" not in result
        # The grounding relationship (subClassOf) must survive — only the
        # network-fetchable import declaration is removed.
        assert "subClassOf" in result
        assert "owl:Class" in result

    def test_returns_original_on_parse_error(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        bad = ":X a owl:Class ."
        assert _clean_ontology_for_vkg(bad) == bad

    def test_resolves_dual_typed_property_to_object_property(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/fk_col> a owl:DatatypeProperty , owl:ObjectProperty ;
    rdfs:range xsd:long , <http://ex.org/Target> .
"""
        result = _clean_ontology_for_vkg(turtle)
        assert "ObjectProperty" in result
        assert "DatatypeProperty" not in result
        assert "xsd:long" not in result and "integer" not in result.lower()
        assert "Target" in result

    def test_leaves_pure_datatype_property_unchanged(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/name> a owl:DatatypeProperty ;
    rdfs:range xsd:string .
"""
        result = _clean_ontology_for_vkg(turtle)
        assert "DatatypeProperty" in result
        assert "xsd:string" in result or "string" in result

    def test_aligns_range_with_r2rml_datatype(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        ontology = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/amount> a owl:DatatypeProperty ;
    rdfs:range xsd:string .
<http://ex.org/name> a owl:DatatypeProperty ;
    rdfs:range xsd:string .
"""
        r2rml = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/POM_Amount> rr:predicate <http://ex.org/amount> ;
    rr:objectMap <http://ex.org/POM_Amount/OM> .
<http://ex.org/POM_Amount/OM> rr:column "amount" ;
    rr:datatype xsd:decimal .
<http://ex.org/POM_Name> rr:predicate <http://ex.org/name> ;
    rr:objectMap <http://ex.org/POM_Name/OM> .
<http://ex.org/POM_Name/OM> rr:column "name" ;
    rr:datatype xsd:string .
"""
        result = _clean_ontology_for_vkg(ontology, r2rml_turtle=r2rml)
        # amount should be changed from xsd:string to xsd:decimal
        assert "decimal" in result
        # name should remain xsd:string (already matches)
        assert "string" in result

    def test_r2rml_alignment_no_op_when_no_r2rml(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        ontology = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/amount> a owl:DatatypeProperty ;
    rdfs:range xsd:string .
"""
        result = _clean_ontology_for_vkg(ontology, r2rml_turtle=None)
        assert "string" in result

    def test_strips_suggestion_triples(self):
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
<http://ex.org/Child> a owl:Class ;
    rdfs:label "child" ;
    coa:suggestedSubClassOf <http://ex.org/Parent> ;
    coa:suggestionReason "PK_SHARING_SINGLE_CHILD" .
<http://ex.org/Parent> a owl:Class ;
    rdfs:label "parent" .
"""
        result = _clean_ontology_for_vkg(turtle)
        assert "suggestedSubClassOf" not in result
        assert "suggestionReason" not in result
        assert "PK_SHARING_SINGLE_CHILD" not in result
        assert "child" in result  # class itself kept
        assert "parent" in result

    def test_canonicalizes_malformed_datatype_token(self):
        """Accept-time SAFETY NET (proposals.py:261): a malformed/mis-cased token
        in the ontology must be canonicalized on the VKG copy, so a user who skips
        Repair can't push an undefined datatype to Ontop.

        Asserted via a typed LITERAL (not an rdfs:range) on purpose: the date/time
        range-normalization step folds canonicalized dateTime/date/time RANGES to
        xsd:string, which would mask whether the canonicalize actually ran. Literal
        datatypes are canonicalized but never folded, so they're the honest probe.
        """
        from coa_ontology.proposals import _clean_ontology_for_vkg

        turtle = """
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/s> <http://ex.org/p> "2020-01-01T00:00:00"^^xsd:datetime .
"""
        result = _clean_ontology_for_vkg(turtle)
        # lowercase-t xsd:datetime → correctly-cased xsd:dateTime on the VKG copy
        assert "dateTime" in result
        assert "datetime" not in result.replace("dateTime", "")

    def test_aligns_range_with_canonicalized_malformed_r2rml_datatype(self):
        """R2RML gap: the range-alignment step (proposals.py:336) canonicalizes a
        malformed rr:datatype via _canonical_for BEFORE copying it into the ontology
        range — so a malformed rr:datatype can't re-inject an undefined datatype into
        the served copy AFTER the graph-level canonicalize already cleaned it.
        Fails if line 336 copied dt_ref without _canonical_for."""
        from coa_ontology.proposals import _clean_ontology_for_vkg
        from rdflib import RDFS, Graph, URIRef
        from rdflib.namespace import XSD

        ontology = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/ts> a owl:DatatypeProperty ;
    rdfs:range xsd:string .
"""
        r2rml = """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://ex.org/POM_ts> rr:predicate <http://ex.org/ts> ;
    rr:objectMap <http://ex.org/POM_ts/OM> .
<http://ex.org/POM_ts/OM> rr:column "ts" ;
    rr:datatype xsd:datetime .
"""
        result = _clean_ontology_for_vkg(ontology, r2rml_turtle=r2rml)
        g = Graph()
        g.parse(data=result, format="turtle")
        prop = URIRef("http://ex.org/ts")
        assert (prop, RDFS.range, XSD.dateTime) in g
        assert (prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#datetime")) not in g


class TestSqlIdent:
    """Tests for sql_ident — SQL-delimited identifier formatting (Option B).

    Identifiers are preserved verbatim and wrapped in double quotes; nothing is
    stripped, lowercased, or otherwise mangled, so the emitted SQL matches the
    source datasource exactly.
    """

    def test_normal_columns_quoted_verbatim(self):
        from coa_common import sql_ident

        assert sql_ident("account_id") == '"account_id"'
        assert sql_ident("claim_status_code") == '"claim_status_code"'

    def test_special_characters_preserved(self):
        from coa_common import sql_ident

        assert sql_ident("free_meal_count_(k-12)") == '"free_meal_count_(k-12)"'
        assert sql_ident("percent_(%)_eligible") == '"percent_(%)_eligible"'
        assert sql_ident("aCL IgG") == '"aCL IgG"'

    def test_digit_leading_names_preserved(self):
        from coa_common import sql_ident

        assert (
            sql_ident("2013-14 CALPADS Fall 1 Certification Status") == '"2013-14 CALPADS Fall 1 Certification Status"'
        )
        assert sql_ident("1st_column") == '"1st_column"'

    def test_mixed_case_and_spaces_preserved(self):
        from coa_common import sql_ident

        assert sql_ident("Column With Spaces") == '"Column With Spaces"'
        assert sql_ident("Examination Date") == '"Examination Date"'

    def test_embedded_double_quote_escaped(self):
        from coa_common import sql_ident

        # SQL-standard escaping: " → ""
        assert sql_ident('weird"name') == '"weird""name"'

    def test_empty_returns_empty_quoted(self):
        from coa_common import sql_ident

        assert sql_ident("") == '""'


class TestTablesToH2Ddl:
    """Tests for proposals._tables_to_h2_ddl."""

    def test_generates_ddl_with_types_and_pk(self):
        from coa_ontology.inducer.services.data_catalog import (
            CatalogColumn,
            CatalogConstraint,
            CatalogTable,
        )
        from coa_ontology.proposals import _tables_to_h2_ddl

        tables = [
            CatalogTable(
                id="t1",
                name="Claim",
                fullyQualifiedName="db.Claim",
                columns=[
                    CatalogColumn(name="Claim_Id", dataType="INT"),
                    CatalogColumn(name="Amount", dataType="DECIMAL"),
                    CatalogColumn(name="Filed_Date", dataType="DATETIME"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["Claim_Id"])],
            )
        ]
        ddl = _tables_to_h2_ddl(tables)
        assert "CREATE TABLE" in ddl
        # Names preserved verbatim (original case), delimited
        assert '"Claim_Id" INTEGER NOT NULL' in ddl
        assert '"Amount" DECIMAL' in ddl
        # Temporal columns keep their real H2 type. Previously VARCHAR(255),
        # which forced R2RML rr:datatype and the ontology range to xsd:string to
        # match, making every date FILTER unsatisfiable in Ontop.
        assert '"Filed_Date" TIMESTAMP' in ddl
        assert 'PRIMARY KEY ("Claim_Id")' in ddl
        assert '"Claim"' in ddl

    def test_preserves_special_characters_in_columns(self):
        from coa_ontology.inducer.services.data_catalog import (
            CatalogColumn,
            CatalogTable,
        )
        from coa_ontology.proposals import _tables_to_h2_ddl

        tables = [
            CatalogTable(
                id="t1",
                name="test_table",
                fullyQualifiedName="db.test_table",
                columns=[
                    CatalogColumn(name="free_meal_count_(k-12)", dataType="DOUBLE"),
                    CatalogColumn(name="percent_(%)_eligible", dataType="DOUBLE"),
                    CatalogColumn(name="2013-14 CALPADS Status", dataType="INT"),
                    CatalogColumn(name="cross", dataType="VARCHAR"),
                ],
            )
        ]
        ddl = _tables_to_h2_ddl(tables)
        # Original names preserved verbatim inside double quotes — no mangling
        assert '"free_meal_count_(k-12)" DOUBLE' in ddl
        assert '"percent_(%)_eligible" DOUBLE' in ddl
        assert '"2013-14 CALPADS Status" INTEGER' in ddl
        # "cross" is a reserved word — quoting makes it safe
        assert '"cross"' in ddl
        # Table name preserved verbatim, delimited
        assert '"test_table"' in ddl

    def test_qualifies_same_named_tables_across_schemas(self):
        """#149 A: two same-named tables from different schemas must both
        survive with distinct schema-qualified names (no silent IF NOT EXISTS
        drop), and a CREATE SCHEMA is emitted for each."""
        from coa_ontology.inducer.services.data_catalog import (
            CatalogColumn,
            CatalogTable,
        )
        from coa_ontology.proposals import _tables_to_h2_ddl

        tables = [
            CatalogTable(
                id="t1",
                name="events_daily",
                fullyQualifiedName="sales.events_daily",
                sourceSchema="sales",
                columns=[CatalogColumn(name="sale_id", dataType="INT")],
            ),
            CatalogTable(
                id="t2",
                name="events_daily",
                fullyQualifiedName="ops.events_daily",
                sourceSchema="ops",
                columns=[CatalogColumn(name="op_id", dataType="INT")],
            ),
        ]
        ddl = _tables_to_h2_ddl(tables)
        # Both schemas declared
        assert 'CREATE SCHEMA IF NOT EXISTS "sales";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "ops";' in ddl
        # Both tables survive as distinct qualified names
        assert 'CREATE TABLE IF NOT EXISTS "sales"."events_daily"' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "ops"."events_daily"' in ddl
        # Each keeps its OWN columns (the bug was the loser's columns vanishing)
        assert '"sale_id"' in ddl
        assert '"op_id"' in ddl
        # Exactly two CREATE TABLE statements — neither was dropped
        assert ddl.count("CREATE TABLE IF NOT EXISTS") == 2

    def test_unqualified_when_no_source_schema(self):
        """Back-compat: a table without sourceSchema emits a bare quoted name
        and no CREATE SCHEMA, byte-identical to the pre-fix behaviour."""
        from coa_ontology.inducer.services.data_catalog import (
            CatalogColumn,
            CatalogTable,
        )
        from coa_ontology.proposals import _tables_to_h2_ddl

        tables = [
            CatalogTable(
                id="t1",
                name="orders",
                fullyQualifiedName="orders",
                columns=[CatalogColumn(name="id", dataType="INT")],
            )
        ]
        ddl = _tables_to_h2_ddl(tables)
        assert "CREATE SCHEMA" not in ddl
        assert 'CREATE TABLE IF NOT EXISTS "orders"' in ddl
        # NOT qualified with a dotted prefix
        assert '"."orders"' not in ddl

    def test_catalog_to_tables_populates_source_schema_end_to_end(self):
        """#149 A CONTRACT: the schema.sql path must qualify same-named tables
        when fed a RAW CATALOG (as the live accept pipeline does), not only when
        a caller pre-sets ``sourceSchema``.

        The original fix patched the DDL EMITTER (_tables_to_h2_ddl) and tested it
        with sourceSchema already set — but ``_catalog_to_tables`` (the function
        that actually builds the tables at accept time) never populated
        sourceSchema, so on live data the guard was silently falsy and the DDL
        collapsed to two bare colliding ``CREATE TABLE IF NOT EXISTS "claims"``.
        This test binds the two halves: catalog dict -> _catalog_to_tables ->
        CatalogTable.sourceSchema -> qualified DDL, so the silent drift cannot
        recur. Found by live e2e; unit-only coverage missed it.
        """
        from coa_ontology.induce_catalog import _catalog_to_tables
        from coa_ontology.inducer.services.data_catalog import CatalogTable
        from coa_ontology.proposals import _tables_to_h2_ddl

        # Two schemas, SAME table name — the #149 A collision, as a raw catalog.
        catalog = {
            "databases": [
                {
                    "name": "schema_a",
                    "tables": [
                        {
                            "name": "claims",
                            "columns": [{"name": "id", "type": "INT"}],
                        }
                    ],
                },
                {
                    "name": "schema_b",
                    "tables": [
                        {
                            "name": "claims",
                            "columns": [{"name": "id", "type": "INT"}],
                        }
                    ],
                },
            ]
        }

        tbl_dicts = _catalog_to_tables(catalog)
        # CONTRACT 1: _catalog_to_tables must carry the schema through.
        assert {d["sourceSchema"] for d in tbl_dicts} == {"schema_a", "schema_b"}

        tables = [CatalogTable(**d) for d in tbl_dicts]
        ddl = _tables_to_h2_ddl(tables)
        # CONTRACT 2: the DDL the emitter produces from those tables is qualified,
        # so neither same-named table is silently dropped by IF NOT EXISTS.
        assert 'CREATE SCHEMA IF NOT EXISTS "schema_a";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "schema_b";' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "schema_a"."claims"' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "schema_b"."claims"' in ddl
        assert ddl.count("CREATE TABLE IF NOT EXISTS") == 2

    def test_catalog_to_tables_no_schema_prefix_stays_unqualified(self):
        """NEGATIVE CONTROL: a catalog whose database name is empty must NOT
        invent a schema — sourceSchema stays falsy and the DDL is bare. This
        proves CONTRACT 1 above can actually fail (a check that cannot fail
        proves nothing), so the qualification is data-driven, not hard-coded.
        """
        from coa_ontology.induce_catalog import _catalog_to_tables
        from coa_ontology.inducer.services.data_catalog import CatalogTable
        from coa_ontology.proposals import _tables_to_h2_ddl

        catalog = {
            "databases": [
                {
                    "name": "",
                    "tables": [{"name": "orders", "columns": [{"name": "id", "type": "INT"}]}],
                }
            ]
        }
        tbl_dicts = _catalog_to_tables(catalog)
        assert tbl_dicts[0]["sourceSchema"] is None
        ddl = _tables_to_h2_ddl([CatalogTable(**d) for d in tbl_dicts])
        assert "CREATE SCHEMA" not in ddl
        assert 'CREATE TABLE IF NOT EXISTS "orders"' in ddl
        assert '"."orders"' not in ddl


class TestSubstepFailureVisibility:
    """#467, second half: a substep that fails must NOT leave the proposal
    'accepted' while pretending it succeeded.

    Three substeps used to swallow their own exceptions and log a warning, so the
    accept completed regardless. Each is handled per its actual blast radius:

      - schema.sql generation -> FATAL (Ontop can't validate R2RML without it)
      - reconcile_counts      -> non-fatal, but checkpointed 'skipped' not 'done'
      - EventBridge notify    -> non-fatal, but its own retried step + 'skipped'
    """

    @patch("coa_ontology.proposals.dynamo_store")
    def test_schema_sql_generation_error_propagates(self, mock_dynamo):
        """A BROKEN catalog fetch must raise, not silently yield no schema.sql.

        Regression: this returned None on any exception, indistinguishable from
        "nothing to generate", so a structured ontology could be accepted with no
        schema.sql and Tier-2 SQL then failed at query time.

        The failure surfaces as ``SchemaSqlGenerationError`` (#457), which wraps
        the original error so a caller can tell a genuine failure apart from the
        ``None`` empty state. Asserting the wrapper — not the inner RuntimeError —
        is the contract: the whole point is that this type is distinguishable.
        """
        from coa_ontology.proposals import SchemaSqlGenerationError, _generate_schema_sql

        mock_dynamo.list_proposals.return_value = [{"metadata": {"datasource_ids": ["ds-1"]}}]
        with (
            patch(
                "coa_ontology.induce_catalog._fetch_catalog",
                side_effect=RuntimeError("catalog 503"),
            ),
            pytest.raises(SchemaSqlGenerationError, match="RuntimeError: catalog 503"),
        ):
            _generate_schema_sql("ns", "http://x.org/o#")

    @patch("coa_ontology.proposals.dynamo_store")
    def test_no_datasources_still_returns_none_not_an_error(self, mock_dynamo):
        """The legitimate 'nothing to generate' case must stay non-fatal —
        unstructured proposals have no R2RML and no schema.sql."""
        from coa_ontology.proposals import _generate_schema_sql

        mock_dynamo.list_proposals.return_value = [{"metadata": {}}]
        assert _generate_schema_sql("ns", "http://x.org/o#") is None

    def test_reconcile_counts_raises_so_the_wrapper_can_retry(self):
        """Must RAISE, not swallow. Non-fatality is the caller's declaration
        (``_run_accept_step(..., fatal=False)``); a step that catches its own
        exception makes the wrapper's 3-attempt retry silently never happen."""
        from coa_ontology.proposals import _reconcile_counts

        with (
            patch(
                "coa_ontology.stores.build_stores",
                side_effect=RuntimeError("neptune down"),
            ),
            pytest.raises(RuntimeError, match="neptune down"),
        ):
            _reconcile_counts("http://x.org/o#", "ns", "https://graph/uri")

    def test_reconcile_counts_reports_nothing_to_do_as_false(self):
        """``False`` is reserved for 'genuinely nothing to reconcile' (no counts
        from the backend) — checkpointed ``skipped``, not a false ``done``."""
        from coa_ontology.proposals import _reconcile_counts

        graph = MagicMock()
        graph.count_classes_and_properties.return_value = {}
        with (
            patch("coa_ontology.stores.build_stores", return_value=(graph, MagicMock())),
            patch("coa_ontology.proposals.dynamo_store"),
        ):
            assert _reconcile_counts("http://x.org/o#", "ns", "https://graph/uri") is False

    def test_reconcile_counts_reports_success_as_true(self):
        from coa_ontology.proposals import _reconcile_counts

        graph = MagicMock()
        graph.count_classes_and_properties.return_value = {"classes": 3, "properties": 4}
        with (
            patch("coa_ontology.stores.build_stores", return_value=(graph, MagicMock())),
            patch("coa_ontology.proposals.dynamo_store"),
        ):
            assert _reconcile_counts("http://x.org/o#", "ns", "https://graph/uri") is True

    def test_emit_ontology_published_raises_so_the_wrapper_can_retry(self):
        """A lost VKG-reload signal must be retried, not swallowed on attempt 1:
        S3 is correct but VKG keeps serving the previous payload."""
        from coa_ontology.proposals import _emit_ontology_published

        with (
            patch("boto3.client") as mock_client,
            pytest.raises(RuntimeError, match="eventbridge down"),
        ):
            mock_client.return_value.put_events.side_effect = RuntimeError("eventbridge down")
            _emit_ontology_published("ns", "v1")

    def test_emit_ontology_published_raises_on_partial_failure(self):
        """FailedEntryCount > 0 is a failure even though put_events returned 200,
        so it must raise into the retry like any other transient failure."""
        from coa_ontology.proposals import _emit_ontology_published

        with (
            patch("boto3.client") as mock_client,
            pytest.raises(RuntimeError, match="FailedEntryCount=1"),
        ):
            mock_client.return_value.put_events.return_value = {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]}
            _emit_ontology_published("ns", "v1")

    def test_emit_ontology_published_reports_success_as_true(self):
        from coa_ontology.proposals import _emit_ontology_published

        with patch("boto3.client") as mock_client:
            mock_client.return_value.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "e1"}]}
            assert _emit_ontology_published("ns", "v1") is True

    def test_persist_no_longer_emits_the_event_itself(self):
        """notify_vkg is its own retried step now; persist must do S3 writes only,
        so a lost notification can't be hidden inside a successful persist."""
        from coa_ontology.proposals import _persist_induced_to_s3

        with (
            patch("boto3.client") as mock_client,
            patch(
                "coa_ontology.stores.neptune_db_graph._gsp_get_turtle",
                return_value=":X a owl:Class .",
            ),
            patch("coa_ontology.proposals.dynamo_store") as mock_ds,
            patch("coa_ontology.proposals._generate_schema_sql", return_value=None),
            patch("coa_ontology.proposals._emit_ontology_published") as mock_emit,
        ):
            mock_ds.list_proposals.return_value = []
            mock_client.return_value = MagicMock()
            _persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")
        mock_emit.assert_not_called()
