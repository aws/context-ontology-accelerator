# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the RIGOR induction strategy primitives.

Pure unit tests — no live AWS / Bedrock / OpenSearch calls. Exercises:
  - Module-level helpers (_to_pascal, _iri_local_name, _strip_markdown_fences,
    _format_schema_context)
  - Strategy methods (_order_by_fk, _parse_turtle, _new_graph, _check_novelty)
  - Strategy registry (get_strategy)
  - Prompt loading

Run:
    pytest tests/test_rigor_strategy.py -v
"""

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock

from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies import STRATEGIES, get_strategy
from coa_ontology.inducer.strategies.rigor_ontology import (
    _JUDGE_PROMPT_TEMPLATE,
    _MAX_CORE_FRAGMENTS_IN_CONTEXT,
    _MAX_OUTPUT_TOKENS,
    _SYSTEM_PROMPT,
    _TABLE_PROMPT_FALLBACK_TEMPLATE,
    _TABLE_PROMPT_TEMPLATE,
    RigorOntologyStrategy,
    _format_schema_context,
    _iri_local_name,
    _strip_markdown_fences,
    _to_pascal,
)
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

# ── Module-level helpers ────────────────────────────────────────────────


class TestToPascal:
    def test_snake_case(self):
        assert _to_pascal("patient_history") == "PatientHistory"

    def test_kebab_case(self):
        assert _to_pascal("tumor-board") == "TumorBoard"

    def test_already_pascal(self):
        assert _to_pascal("AlreadyPascal") == "Alreadypascal"  # single word → title-cased

    def test_mixed_separators(self):
        assert _to_pascal("foo_bar-baz qux") == "FooBarBazQux"

    def test_empty(self):
        assert _to_pascal("") == "Entity"

    def test_none_safe(self):
        # None would crash re.split; the guard returns "Entity"
        assert _to_pascal(None) == "Entity"

    def test_leading_trailing_separators(self):
        assert _to_pascal("_foo_bar_") == "FooBar"

    def test_single_word(self):
        assert _to_pascal("patient") == "Patient"


class TestIriLocalName:
    def test_hash_fragment(self):
        assert _iri_local_name("http://example.org/ontology#Patient") == "Patient"

    def test_path_trailing(self):
        assert _iri_local_name("http://a.b/c/Thing") == "Thing"

    def test_no_separator(self):
        assert _iri_local_name("bare_string") == "bare_string"

    def test_hash_wins_over_slash(self):
        # If both '#' and '/' are present, '#' takes precedence (last fragment)
        assert _iri_local_name("http://a.b/c/d#Local") == "Local"


class TestStripMarkdownFences:
    def test_turtle_fence(self):
        text = "```turtle\n:Foo a owl:Class .\n```"
        assert _strip_markdown_fences(text) == ":Foo a owl:Class ."

    def test_generic_fence(self):
        text = "```\n:Foo a owl:Class .\n```"
        assert _strip_markdown_fences(text) == ":Foo a owl:Class ."

    def test_no_fence(self):
        text = ":Foo a owl:Class ."
        assert _strip_markdown_fences(text) == ":Foo a owl:Class ."

    def test_whitespace(self):
        assert _strip_markdown_fences("  \n\n:X a owl:Class .\n  ") == ":X a owl:Class ."

    def test_fence_start_only(self):
        # Malformed — closing fence missing. Should still drop the opening fence.
        text = "```turtle\n:Foo a owl:Class ."
        assert _strip_markdown_fences(text) == ":Foo a owl:Class ."


class TestFormatSchemaContext:
    def test_simple_table(self):
        t = CatalogTable(
            id="db.patients",
            name="patients",
            fullyQualifiedName="db.patients",
            description="Patient master table",
            columns=[
                CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY"),
                CatalogColumn(name="name", dataType="VARCHAR", description="Full legal name"),
            ],
        )
        out = _format_schema_context(t)
        assert "Table: patients" in out
        assert "Patient master table" in out
        assert "id : BIGINT" in out
        assert "[PRIMARY_KEY]" in out
        assert "name : VARCHAR" in out
        assert "-- Full legal name" in out

    def test_with_constraints(self):
        t = CatalogTable(
            id="db.orders",
            name="orders",
            fullyQualifiedName="db.orders",
            columns=[CatalogColumn(name="customer_id", dataType="BIGINT")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.id"],
                ),
            ],
        )
        out = _format_schema_context(t)
        assert "Constraints:" in out
        assert "FOREIGN_KEY" in out
        assert "customers.id" in out

    def test_no_description(self):
        t = CatalogTable(id="t1", name="t1", fullyQualifiedName="t1")
        out = _format_schema_context(t)
        assert "Table: t1" in out
        assert "Description:" not in out


# ── Strategy registry ───────────────────────────────────────────────────


class TestStrategyRegistry:
    def test_has_both_strategies(self):
        assert "table_to_ontology" in STRATEGIES
        assert "rigor_ontology" in STRATEGIES

    def test_get_strategy_returns_class(self):
        cls = get_strategy("rigor_ontology")
        assert cls is RigorOntologyStrategy

    def test_get_strategy_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("does_not_exist")

    def test_rigor_can_instantiate(self):
        s = get_strategy("rigor_ontology")()
        assert isinstance(s, RigorOntologyStrategy)


# ── Prompt files ────────────────────────────────────────────────────────


class TestPrompts:
    def test_system_prompt_loaded(self):
        assert len(_SYSTEM_PROMPT) > 100
        assert "OWL 2 DL" in _SYSTEM_PROMPT
        assert "PascalCase" in _SYSTEM_PROMPT
        assert "Turtle" in _SYSTEM_PROMPT

    def test_delta_template_has_required_placeholders(self):
        required = [
            "{ontology_prefix}",
            "{table_class_name}",
            "{table_name}",
            "{schema_context}",
            "{documents}",
            "{existing_ontology}",
            "{core_ontology}",
            "{provenance_base}",
        ]
        for p in required:
            assert p in _TABLE_PROMPT_TEMPLATE, f"missing placeholder {p}"

    def test_judge_template_has_required_placeholders(self):
        for p in ["{core_ontology}", "{schema_context}", "{delta_ontology}"]:
            assert p in _JUDGE_PROMPT_TEMPLATE

    def test_fallback_template_loaded(self):
        """The fallback (stricter) delta prompt is loadable."""
        assert len(_TABLE_PROMPT_FALLBACK_TEMPLATE) > 100
        # Must forbid the exact constructs that caused the cardinality bug
        assert "owl:Restriction" in _TABLE_PROMPT_FALLBACK_TEMPLATE
        assert "Blank nodes" in _TABLE_PROMPT_FALLBACK_TEMPLATE
        assert "flat" in _TABLE_PROMPT_FALLBACK_TEMPLATE.lower()

    def test_fallback_template_has_required_placeholders(self):
        """The fallback prompt uses a subset of the main prompt's placeholders.

        Specifically, it skips the LLM-context fields (existing_ontology,
        core_ontology, documents) because those distract from the "just produce
        flat triples" instruction. The retry path must pass exactly these.
        """
        required = [
            "{ontology_prefix}",
            "{table_class_name}",
            "{table_name}",
            "{schema_context}",
            "{provenance_base}",
        ]
        for p in required:
            assert p in _TABLE_PROMPT_FALLBACK_TEMPLATE, f"missing placeholder {p}"

    def test_fallback_template_formats_with_real_context(self):
        """Smoke test: the fallback template renders without KeyError given the
        variables the retry path passes.
        """
        rendered = _TABLE_PROMPT_FALLBACK_TEMPLATE.format(
            ontology_prefix="http://ex/ind#",
            table_class_name="Claim",
            table_name="claims",
            schema_context="id int PK",
            provenance_base="http://ex/provenance",
        )
        assert "Claim" in rendered
        assert "http://ex/ind#" in rendered

    def test_output_budget_large_enough(self):
        # Must be well above the paper's 2k to handle wide tables.
        assert _MAX_OUTPUT_TOKENS >= 4096

    def test_core_fragment_cap_reasonable(self):
        assert 1 <= _MAX_CORE_FRAGMENTS_IN_CONTEXT <= 20


# ── FK topological sort ─────────────────────────────────────────────────


class TestOrderByFk:
    @pytest.fixture
    def strategy(self):
        return RigorOntologyStrategy()

    def test_referenced_before_referencing(self, strategy):
        tables = [
            CatalogTable(
                id="orders",
                name="orders",
                fullyQualifiedName="db.orders",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["customer_id"],
                        referredColumns=["customers.id"],
                    ),
                ],
            ),
            CatalogTable(id="customers", name="customers", fullyQualifiedName="db.customers"),
        ]
        ordered = strategy._order_by_fk(tables)
        names = [t.name for t in ordered]
        assert names.index("customers") < names.index("orders")

    def test_diamond_dependency(self, strategy):
        # order_items → orders → customers
        # order_items → products
        tables = [
            CatalogTable(
                id="order_items",
                name="order_items",
                fullyQualifiedName="db.order_items",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["order_id"],
                        referredColumns=["orders.id"],
                    ),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["product_id"],
                        referredColumns=["products.id"],
                    ),
                ],
            ),
            CatalogTable(
                id="orders",
                name="orders",
                fullyQualifiedName="db.orders",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["customer_id"],
                        referredColumns=["customers.id"],
                    ),
                ],
            ),
            CatalogTable(id="products", name="products", fullyQualifiedName="db.products"),
            CatalogTable(id="customers", name="customers", fullyQualifiedName="db.customers"),
        ]
        ordered = strategy._order_by_fk(tables)
        names = [t.name for t in ordered]
        # every dependency constraint must hold
        assert names.index("customers") < names.index("orders")
        assert names.index("orders") < names.index("order_items")
        assert names.index("products") < names.index("order_items")

    def test_cycle_does_not_hang(self, strategy):
        # A ↔ B cycle
        tables = [
            CatalogTable(
                id="A",
                name="A",
                fullyQualifiedName="db.A",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["b_id"],
                        referredColumns=["B.id"],
                    ),
                ],
            ),
            CatalogTable(
                id="B",
                name="B",
                fullyQualifiedName="db.B",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["a_id"],
                        referredColumns=["A.id"],
                    ),
                ],
            ),
        ]
        ordered = strategy._order_by_fk(tables)
        names = {t.name for t in ordered}
        assert names == {"A", "B"}
        assert len(ordered) == 2  # no duplicates

    def test_self_reference(self, strategy):
        # employees.manager_id → employees.id
        tables = [
            CatalogTable(
                id="employees",
                name="employees",
                fullyQualifiedName="db.employees",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["manager_id"],
                        referredColumns=["employees.id"],
                    ),
                ],
            ),
        ]
        ordered = strategy._order_by_fk(tables)
        assert [t.name for t in ordered] == ["employees"]

    def test_no_fk_preserves_all(self, strategy):
        tables = [CatalogTable(id=f"t{i}", name=f"t{i}", fullyQualifiedName=f"db.t{i}") for i in range(4)]
        ordered = strategy._order_by_fk(tables)
        assert len(ordered) == 4
        assert {t.name for t in ordered} == {"t0", "t1", "t2", "t3"}

    def test_fk_to_nonexistent_table_ignored(self, strategy):
        # FK references a table not in the input set — should not crash
        tables = [
            CatalogTable(
                id="t",
                name="t",
                fullyQualifiedName="db.t",
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["external_id"],
                        referredColumns=["external_table.id"],
                    ),
                ],
            ),
        ]
        ordered = strategy._order_by_fk(tables)
        assert [t.name for t in ordered] == ["t"]


# ── Turtle parsing ──────────────────────────────────────────────────────


class TestParseTurtle:
    @pytest.fixture
    def strategy(self):
        return RigorOntologyStrategy()

    def test_parse_without_prefix_block(self, strategy):
        """LLM may omit @prefix — our helper prepends them."""
        ttl = """
            ind:Patient a owl:Class ;
                rdfs:label "Patient" ;
                rdfs:comment "A patient" .
        """
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "patients")
        assert g is not None
        assert len(g) == 3

    def test_parse_with_prefix_block(self, strategy):
        """If LLM already includes @prefix, we don't duplicate them."""
        ttl = """
            @prefix ind:  <http://example.org/ind#> .
            @prefix owl:  <http://www.w3.org/2002/07/owl#> .
            ind:Drug a owl:Class .
        """
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "drugs")
        assert g is not None
        assert len(g) == 1

    def test_parse_with_markdown_fences(self, strategy):
        ttl = "```turtle\nind:Disease a owl:Class .\n```"
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "diseases")
        assert g is not None
        assert (URIRef("http://example.org/ind#Disease"), RDF.type, OWL.Class) in g

    def test_invalid_turtle_returns_none(self, strategy):
        g = strategy._parse_turtle(
            "this is not turtle",
            "http://example.org/ind#",
            "bad",
        )
        assert g is None

    def test_empty_input_returns_none(self, strategy):
        assert strategy._parse_turtle("", "http://example.org/ind#", "empty") is None
        assert strategy._parse_turtle("   \n\n   ", "http://example.org/ind#", "blank") is None

    # ── Layer 2 defensive sanitization (the RIGOR cardinality fix) ──

    def test_strips_empty_blank_nodes(self, strategy):
        """Empty `[]` should be stripped before parsing so it can't crash rdflib."""
        ttl = """
            ind:Foo a owl:Class .
            ind:Foo rdfs:label "Foo" .
            ind:bar a owl:DatatypeProperty .
            ind:bar rdfs:range [] .
        """
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "foo")
        # After sanitization, the empty [] triple is gone; the other 3 triples remain
        assert g is not None
        # The Class, label, and DatatypeProperty declarations survive
        assert (URIRef("http://example.org/ind#Foo"), RDF.type, OWL.Class) in g

    def test_strips_subclassof_restriction_blank_nodes(self, strategy):
        """`rdfs:subClassOf [ ... ]` restrictions are stripped (cardinality fix)."""
        # Use a shorter restriction to stay under the 120-char line limit.
        restriction = '[ a owl:Restriction ; owl:onProperty ind:id ; owl:minCardinality "1"^^xsd:nonNegativeInteger ]'
        ttl = (
            "ind:Claim a owl:Class .\n"
            'ind:Claim rdfs:label "Claim" .\n'
            f"ind:Claim rdfs:subClassOf {restriction} .\n"
            "ind:id a owl:DatatypeProperty .\n"
        )
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "claim")
        # The Class + label + DatatypeProperty survive; the restriction triple is stripped
        assert g is not None
        assert (URIRef("http://example.org/ind#Claim"), RDF.type, OWL.Class) in g
        # No rdfs:subClassOf triples should remain
        subclass_triples = list(g.triples((None, RDFS.subClassOf, None)))
        assert subclass_triples == [], f"expected no subClassOf, got {subclass_triples}"

    def test_sanitization_preserves_valid_turtle_untouched(self, strategy):
        """A clean fragment with no blank nodes should parse identically before and after."""
        ttl = """
            ind:Patient a owl:Class .
            ind:Patient rdfs:label "Patient" .
            ind:hasId a owl:DatatypeProperty .
            ind:hasId rdfs:domain ind:Patient .
            ind:hasId rdfs:range xsd:string .
        """
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "patients")
        assert g is not None
        assert len(g) == 5

    def test_malformed_restriction_doesnt_kill_whole_fragment(self, strategy):
        """Regression test for the core bug: a malformed `rdfs:subClassOf [ ... ]`
        triple used to make rdflib reject the ENTIRE document. With sanitization,
        the other triples survive.
        """
        ttl = """ind:Policy a owl:Class .
ind:Policy rdfs:label "Policy" .
ind:Policy rdfs:comment "An insurance policy" .
ind:Policy rdfs:subClassOf [ unclosed bracket
ind:policyId a owl:DatatypeProperty .
ind:policyId rdfs:domain ind:Policy .
"""
        g = strategy._parse_turtle(ttl, "http://example.org/ind#", "policies")
        # Without the Layer 2 sanitization, rdflib raises and we return None.
        # With sanitization, the malformed line is stripped and the rest parses.
        assert g is not None
        assert (URIRef("http://example.org/ind#Policy"), RDF.type, OWL.Class) in g


# ── Graph construction ──────────────────────────────────────────────────


class TestNewGraph:
    def test_has_ontology_declaration(self):
        s = RigorOntologyStrategy()
        g = s._new_graph("http://example.org/ind#", "Test Ontology")
        ont = URIRef("http://example.org/ind")
        assert (ont, RDF.type, OWL.Ontology) in g
        assert (ont, RDFS.label, Literal("Test Ontology")) in g

    def test_binds_standard_prefixes(self):
        s = RigorOntologyStrategy()
        g = s._new_graph("http://example.org/ind#", "Test")
        prefixes = {p for p, _ in g.namespaces()}
        for required in {"ind", "owl", "rdfs", "xsd", "prov"}:
            assert required in prefixes, f"missing {required} prefix"

    def test_prefix_trailing_slash(self):
        s = RigorOntologyStrategy()
        g = s._new_graph("http://example.org/ind/", "T")
        ont = URIRef("http://example.org/ind")
        assert (ont, RDF.type, OWL.Ontology) in g


# ── Novelty check ───────────────────────────────────────────────────────


class TestCheckNovelty:
    """Test _check_novelty with mocked pipeline — no real Bedrock / OpenSearch."""

    def _make_pipeline(self, search_score: float = 0.0):
        """Build a mock pipeline whose embedding + search return predictable values."""
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        # embed_texts returns a zero vector per input
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]

        # search_embeddings returns one hit with the configured score
        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if search_score > 0:
                return [
                    {
                        "entity_uri": f"http://existing/{entity_type}/Match",
                        "ontology_id": "existing-onto",
                        "score": search_score,
                    }
                ]
            return []

        pipeline.oc.search_embeddings.side_effect = _search
        return pipeline

    def _graph_with_terms(self, prefix: str) -> tuple[Graph, dict[str, str]]:
        """Build a graph with 2 classes + 2 properties linked to source tables."""
        g = Graph()
        ns = Namespace(prefix)
        g.add((ns.Patient, RDF.type, OWL.Class))
        g.add((ns.Patient, RDFS.label, Literal("Patient")))
        g.add((ns.Visit, RDF.type, OWL.Class))
        g.add((ns.Visit, RDFS.label, Literal("Visit")))
        g.add((ns.hasAge, RDF.type, OWL.DatatypeProperty))
        g.add((ns.hasAge, RDFS.label, Literal("hasAge")))
        g.add((ns.hasVisit, RDF.type, OWL.ObjectProperty))
        g.add((ns.hasVisit, RDFS.label, Literal("hasVisit")))
        uri_to_table = {
            str(ns.Patient): "patients",
            str(ns.hasAge): "patients",
            str(ns.Visit): "visits",
            str(ns.hasVisit): "visits",
        }
        return g, uri_to_table

    def test_all_novel_when_no_hits(self):
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.0)
        tables = [
            CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients"),
            CatalogTable(id="v", name="visits", fullyQualifiedName="db.visits"),
        ]

        novel_graph, novel_tables, matches = s._check_novelty(
            g,
            tables,
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        assert len(matches) == 4
        assert all(m.match_type == "novel" for m in matches)
        assert novel_tables == {"patients", "visits"}
        # Novel graph should contain all 4 term declarations + ontology header triples
        classes_in_novel = list(novel_graph.subjects(RDF.type, OWL.Class))
        # 2 class terms + 1 owl:Ontology header
        assert len(classes_in_novel) >= 2

    def test_high_score_becomes_high_confidence_match(self):
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.85)
        tables = [
            CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients"),
            CatalogTable(id="v", name="visits", fullyQualifiedName="db.visits"),
        ]

        _, novel_tables, matches = s._check_novelty(
            g,
            tables,
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        assert all(m.match_type == "high_confidence" for m in matches)
        assert novel_tables == set()  # nothing novel

    def test_very_high_score_becomes_exact_match(self):
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.98)

        _, _, matches = s._check_novelty(
            g,
            [
                CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients"),
                CatalogTable(id="v", name="visits", fullyQualifiedName="db.visits"),
            ],
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        assert all(m.match_type == "exact" for m in matches)

    def test_source_table_is_tracked(self):
        """Every match should carry the source table from uri_to_table."""
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.0)

        _, _, matches = s._check_novelty(
            g,
            [
                CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients"),
                CatalogTable(id="v", name="visits", fullyQualifiedName="db.visits"),
            ],
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        tables_found = {m.source_table for m in matches}
        assert tables_found == {"patients", "visits"}

    def test_classes_searched_with_entity_type_class(self):
        """Class terms should be matched against existing classes only."""
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.0)

        s._check_novelty(
            g,
            [
                CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients"),
                CatalogTable(id="v", name="visits", fullyQualifiedName="db.visits"),
            ],
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        # Inspect search_embeddings calls — each term uses its own entity_type
        entity_types_used = {call.kwargs["entity_type"] for call in pipeline.oc.search_embeddings.call_args_list}
        assert entity_types_used == {"class", "property"}

    def test_empty_graph_handled(self):
        s = RigorOntologyStrategy()
        pipeline = self._make_pipeline(search_score=0.0)
        novel_graph, novel_tables, matches = s._check_novelty(
            Graph(),
            [],
            "http://example.org/ind#",
            pipeline,
            0.8,
            {},
        )
        assert matches == []
        assert novel_tables == set()
        # Ontology header still present
        assert (URIRef("http://example.org/ind"), RDF.type, OWL.Ontology) in novel_graph

    def test_embedding_failure_falls_back_to_novel(self):
        """If embed_texts raises, all terms should be marked novel."""
        s = RigorOntologyStrategy()
        g, uri_to_table = self._graph_with_terms("http://example.org/ind#")
        pipeline = self._make_pipeline(search_score=0.0)
        pipeline.eg.embed_texts.side_effect = RuntimeError("Bedrock down")

        _, _, matches = s._check_novelty(
            g,
            [CatalogTable(id="p", name="patients", fullyQualifiedName="db.patients")],
            "http://example.org/ind#",
            pipeline,
            0.8,
            uri_to_table,
        )

        assert len(matches) == 4
        assert all(m.match_type == "novel" for m in matches)


# ── RAG context retrieval ───────────────────────────────────────────────


class TestRetrieveOntologyContext:
    def _make_pipeline_with_hits(self, class_hits=None, prop_hits=None):
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_text.return_value = [0.0] * 8
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]

        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if entity_type == "class":
                return class_hits or []
            if entity_type == "property":
                return prop_hits or []
            return []

        pipeline.oc.search_embeddings.side_effect = _search
        return pipeline

    def test_returns_fallback_when_no_hits(self):
        s = RigorOntologyStrategy()
        pipeline = self._make_pipeline_with_hits()
        t = CatalogTable(id="t", name="patients", fullyQualifiedName="db.patients", description="Patient records")
        ctx = s._retrieve_ontology_context(t, pipeline)
        assert "no related existing concepts found" in ctx

    def test_includes_class_hits_with_labels(self):
        s = RigorOntologyStrategy()
        pipeline = self._make_pipeline_with_hits(
            class_hits=[
                {"entity_uri": "http://example.org/onto#Patient", "score": 0.91},
                {"entity_uri": "http://example.org/onto#Person", "score": 0.82},
            ],
        )
        t = CatalogTable(id="t", name="patients", fullyQualifiedName="db.patients")
        ctx = s._retrieve_ontology_context(t, pipeline)
        assert "Related classes" in ctx
        assert "<http://example.org/onto#Patient>" in ctx
        assert "name=Patient" in ctx  # derived via _iri_local_name
        assert "similarity=0.91" in ctx

    def test_includes_property_hits_per_column(self):
        s = RigorOntologyStrategy()
        pipeline = self._make_pipeline_with_hits(
            prop_hits=[
                {"entity_uri": "http://example.org/onto#hasAge", "score": 0.75},
            ],
        )
        t = CatalogTable(
            id="t",
            name="patients",
            fullyQualifiedName="db.patients",
            columns=[CatalogColumn(name="age", dataType="INT")],
        )
        ctx = s._retrieve_ontology_context(t, pipeline)
        assert "Related properties for columns" in ctx
        assert "column 'age'" in ctx
        assert "hasAge" in ctx

    def test_embedding_failure_is_caught(self):
        """RAG failure returns fallback message, doesn't crash."""
        s = RigorOntologyStrategy()
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_text.side_effect = RuntimeError("Bedrock timeout")
        pipeline.eg.embed_texts.side_effect = RuntimeError("Bedrock timeout")
        t = CatalogTable(id="t", name="patients", fullyQualifiedName="db.patients")
        ctx = s._retrieve_ontology_context(t, pipeline)
        assert "no related existing concepts found" in ctx


# ── R2RML generation (base default + RIGOR override) ────────────────────


class TestBuildR2rmlDefault:
    """Tests the base class's default R2RML implementation.

    This is what table_to_ontology uses (unchanged behaviour).
    """

    @pytest.fixture
    def strategy(self):
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        return TableToOntologyStrategy()

    def test_datatype_columns_get_literal_objectmap(self, strategy):
        table = CatalogTable(
            id="t",
            name="customers",
            fullyQualifiedName="db.customers",
            columns=[
                CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY"),
                CatalogColumn(name="email", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
            ],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            {"customers"},
            Graph(),
        )
        ns = Namespace("http://ex.org/ind#")
        from coa_ontology.inducer.strategies.base import RR

        assert (ns.TriplesMap_Customers, RDF.type, RR.TriplesMap) in g
        # Subject map uses the primary key
        subj_template = str(
            g.value(
                URIRef("http://ex.org/ind#TriplesMap_Customers/SubjectMap"),
                RR.template,
            )
        )
        assert '{"id"}' in subj_template
        # Predicate is the default naming: customers_email
        assert (
            URIRef("http://ex.org/ind#TriplesMap_Customers/POM_Email"),
            RR.predicate,
            ns.customers_email,
        ) in g

    def test_fk_column_gets_referencing_object_map(self, strategy):
        table = CatalogTable(
            id="orders",
            name="orders",
            fullyQualifiedName="db.orders",
            columns=[CatalogColumn(name="customer_id", dataType="BIGINT")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.id"],
                ),
            ],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            {"orders"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        om = URIRef("http://ex.org/ind#TriplesMap_Orders/POM_CustomerId/ObjectMap")
        parent = g.value(om, RR.parentTriplesMap)
        assert parent == URIRef("http://ex.org/ind#TriplesMap_Customers")
        jc = g.value(om, RR.joinCondition)
        assert str(g.value(jc, RR.child)) == '"customer_id"'
        assert str(g.value(jc, RR.parent)) == '"id"'

    def test_uri_prefix_without_separator_gets_hash_appended(self, strategy):
        """Bug fix: ontology_uri_prefix without # or / gets # appended automatically."""
        table = CatalogTable(
            id="t",
            name="district",
            fullyQualifiedName="db.district",
            columns=[
                CatalogColumn(name="district_id", dataType="INTEGER", constraint="PRIMARY_KEY"),
                CatalogColumn(name="name", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["district_id"]),
            ],
        )
        # Prefix WITHOUT trailing # or /
        g = strategy.build_r2rml(
            "http://scl.example.org/financial",
            [table],
            {"district"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        # Should produce URIs with # separator (normalized)
        ns = Namespace("http://scl.example.org/financial#")
        assert (ns.TriplesMap_District, RDF.type, RR.TriplesMap) in g
        subj_template = str(
            g.value(
                URIRef("http://scl.example.org/financial#TriplesMap_District/SubjectMap"),
                RR.template,
            )
        )
        assert "financial#district/" in subj_template
        assert (None, RR.predicate, ns.district_name) in g

    def test_uri_prefix_with_slash_preserved(self, strategy):
        """Prefix ending with / is left as-is."""
        table = CatalogTable(
            id="t",
            name="loan",
            fullyQualifiedName="db.loan",
            columns=[CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ontology/",
            [table],
            {"loan"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        ns = Namespace("http://ex.org/ontology/")
        assert (ns.TriplesMap_Loan, RDF.type, RR.TriplesMap) in g

    def test_all_tables_get_r2rml(self, strategy):
        table = CatalogTable(
            id="t",
            name="customers",
            fullyQualifiedName="db.customers",
            columns=[CatalogColumn(name="id", dataType="BIGINT")],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            set(),  # empty novel_tables — grounded tables still get R2RML
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        triples = list(g.triples((None, RDF.type, RR.TriplesMap)))
        assert len(triples) == 1

    def test_special_char_columns_delimited_verbatim(self, strategy):
        """Columns with parens, percent signs, and digits are preserved verbatim as delimited rr:column values."""
        table = CatalogTable(
            id="t",
            name="frpm",
            fullyQualifiedName="db.frpm",
            columns=[
                CatalogColumn(name="free_meal_count_(k-12)", dataType="DOUBLE"),
                CatalogColumn(name="2013-14_status", dataType="INT"),
                CatalogColumn(name="cross", dataType="VARCHAR"),
            ],
            tableConstraints=[],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            {"frpm"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        # Collect all rr:column literals — original names preserved, double-quoted
        col_values = [str(o) for s, p, o in g.triples((None, RR.column, None))]
        assert '"free_meal_count_(k-12)"' in col_values
        assert '"2013-14_status"' in col_values
        assert '"cross"' in col_values
        # Every value is a delimited identifier (begins and ends with a double quote)
        for v in col_values:
            assert v.startswith('"') and v.endswith('"')

    def test_pk_column_delimited_verbatim_in_subject_template(self, strategy):
        """Primary key with special chars is preserved verbatim (delimited) in the SubjectMap template."""
        table = CatalogTable(
            id="t",
            name="data",
            fullyQualifiedName="db.data",
            columns=[
                CatalogColumn(name="record_(id)", dataType="INT"),
                CatalogColumn(name="value", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["record_(id)"]),
            ],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            {"data"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        subj_template = str(
            g.value(
                URIRef("http://ex.org/ind#TriplesMap_Data/SubjectMap"),
                RR.template,
            )
        )
        assert '{"record_(id)"}' in subj_template

    def test_no_pk_uses_first_column_as_fallback(self, strategy):
        """When no PK exists, subject map uses first column instead of hardcoded 'ID'."""
        table = CatalogTable(
            id="t",
            name="seasons",
            fullyQualifiedName="db.seasons",
            columns=[
                CatalogColumn(name="year", dataType="BIGINT"),
                CatalogColumn(name="url", dataType="VARCHAR"),
            ],
            tableConstraints=[],
        )
        g = strategy.build_r2rml(
            "http://ex.org/ind#",
            [table],
            {"seasons"},
            Graph(),
        )
        from coa_ontology.inducer.strategies.base import RR

        subj_template = str(
            g.value(
                URIRef("http://ex.org/ind#TriplesMap_Seasons/SubjectMap"),
                RR.template,
            )
        )
        assert '{"year"}' in subj_template
        assert '{"ID"}' not in subj_template

    def test_composite_pk_all_columns_in_subject_template(self, strategy):
        """Bug 2b: composite PKs must use ALL key columns in the IRI template to ensure uniqueness."""
        table = CatalogTable(
            id="t",
            name="yearmonth",
            fullyQualifiedName="db.yearmonth",
            columns=[
                CatalogColumn(name="CustomerID", dataType="INT"),
                CatalogColumn(name="Date", dataType="VARCHAR"),
                CatalogColumn(name="Consumption", dataType="REAL"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["CustomerID", "Date"]),
            ],
        )
        g = strategy.build_r2rml("http://ex.org/ind#", [table], {"yearmonth"}, Graph())
        from coa_ontology.inducer.strategies.base import RR

        subj_template = str(g.value(URIRef("http://ex.org/ind#TriplesMap_Yearmonth/SubjectMap"), RR.template))
        assert '{"CustomerID"}' in subj_template
        assert '{"Date"}' in subj_template
        # Both keys must appear — single-column template would produce non-unique IRIs
        assert subj_template.count("{") == 2

    def test_fk_target_extracted_from_two_part_referredcolumns(self, strategy):
        """Bug 2a: referredColumns format is 'TargetTable.column' — parts[0] is the table name.
        induce_catalog.py always writes two-part format so this is the canonical path."""
        table = CatalogTable(
            id="t",
            name="orders",
            fullyQualifiedName="db.orders",
            columns=[CatalogColumn(name="customer_id", dataType="INT")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.id"],
                ),
            ],
        )
        g = strategy.build_r2rml("http://ex.org/ind#", [table], {"orders"}, Graph())
        from coa_ontology.inducer.strategies.base import RR

        om = URIRef("http://ex.org/ind#TriplesMap_Orders/POM_CustomerId/ObjectMap")
        parent = g.value(om, RR.parentTriplesMap)
        # Target table is "customers" (parts[0])
        assert parent == URIRef("http://ex.org/ind#TriplesMap_Customers")
        jc = g.value(om, RR.joinCondition)
        assert str(g.value(jc, RR.child)) == '"customer_id"'
        assert str(g.value(jc, RR.parent)) == '"id"'

    def test_fk_target_single_part_referredcolumns(self, strategy):
        """Defensive: if referredColumns is single-part (just table name), parts[0] still works."""
        table = CatalogTable(
            id="t",
            name="orders",
            fullyQualifiedName="db.orders",
            columns=[CatalogColumn(name="customer_id", dataType="INT")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers"],  # single-part (no dot)
                ),
            ],
        )
        g = strategy.build_r2rml("http://ex.org/ind#", [table], {"orders"}, Graph())
        from coa_ontology.inducer.strategies.base import RR

        om = URIRef("http://ex.org/ind#TriplesMap_Orders/POM_CustomerId/ObjectMap")
        parent = g.value(om, RR.parentTriplesMap)
        # Single-part referredColumns cannot produce a valid joinCondition,
        # so no parentTriplesMap is emitted — column treated as datatype.
        assert parent is None
        col = g.value(om, RR.column)
        assert col is not None


class TestBuildR2rmlRigorOverride:
    """Tests RIGOR's graph-driven R2RML builder."""

    @pytest.fixture
    def strategy(self):
        return RigorOntologyStrategy()

    def _make_graph(self, ontology_prefix: str):
        """Build a sample RIGOR-style proposal graph for a 'customers' table.

        Generated class: ind:Customer (LLM singularized "customers")
        Generated properties:
          - ind:hasEmail (datatype)
          - ind:customerId (datatype, FK placeholder when we're not a FK)
        Plus an auxiliary class ind:PersonConcept with no backing table.
        """
        g = Graph()
        ns = Namespace(ontology_prefix)
        PROV = Namespace("http://www.w3.org/ns/prov#")
        g.bind("ind", ns)
        g.bind("prov", PROV)
        # Table-class with provenance
        g.add((ns.Customer, RDF.type, OWL.Class))
        g.add((ns.Customer, RDFS.label, Literal("customers")))
        g.add(
            (
                ns.Customer,
                PROV.wasDerivedFrom,
                URIRef(f"{ontology_prefix.rstrip('#/')}/provenance/customers"),
            )
        )
        # Auxiliary class (no corresponding table) — must be ignored
        g.add((ns.PersonConcept, RDF.type, OWL.Class))
        g.add((ns.PersonConcept, RDFS.label, Literal("PersonConcept")))
        # Datatype property with provenance
        g.add((ns.hasEmail, RDF.type, OWL.DatatypeProperty))
        g.add((ns.hasEmail, RDFS.domain, ns.Customer))
        g.add((ns.hasEmail, RDFS.range, XSD.string))
        g.add((ns.hasEmail, RDFS.label, Literal("hasEmail")))
        g.add(
            (
                ns.hasEmail,
                PROV.wasDerivedFrom,
                URIRef(f"{ontology_prefix.rstrip('#/')}/provenance/customers/email"),
            )
        )
        # Datatype property with no provenance (label fallback test)
        g.add((ns.identifier, RDF.type, OWL.DatatypeProperty))
        g.add((ns.identifier, RDFS.domain, ns.Customer))
        g.add((ns.identifier, RDFS.range, XSD.long))
        g.add((ns.identifier, RDFS.label, Literal("id")))  # matches column name
        return g

    def _customers_table(self):
        return CatalogTable(
            id="customers",
            name="customers",
            fullyQualifiedName="db.customers",
            columns=[
                CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY"),
                CatalogColumn(name="email", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
            ],
        )

    def test_predicate_comes_from_generated_graph_not_naming_convention(self, strategy):
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        ns = Namespace(prefix)
        pom_email = URIRef(f"{prefix}TriplesMap_customers/POM_Email")
        # Predicate must be the LLM-generated ind:hasEmail, NOT ind:customers_email
        assert (pom_email, RR.predicate, ns.hasEmail) in g
        assert (pom_email, RR.predicate, ns.customers_email) not in g

    def test_class_resolved_via_provenance_even_when_label_differs(self, strategy):
        """Customer (singular) class is found via provenance despite table name being 'customers'."""
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        ns = Namespace(prefix)
        subj = URIRef(f"{prefix}TriplesMap_customers/SubjectMap")
        assert (subj, RR["class"], ns.Customer) in g

    def test_property_resolved_by_label_when_provenance_missing(self, strategy):
        """Column 'id' matches property with label 'id' even without provenance."""
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        ns = Namespace(prefix)
        pom_id = URIRef(f"{prefix}TriplesMap_customers/POM_Id")
        # 'id' column should map to ind:identifier (matched via label)
        assert (pom_id, RR.predicate, ns.identifier) in g

    def test_table_without_matching_class_is_skipped(self, strategy):
        """If RIGOR didn't produce a class for a table, skip cleanly."""
        prefix = "http://ex.org/ind#"
        proposal = Graph()  # empty
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        triples = list(g.triples((None, RDF.type, RR.TriplesMap)))
        assert triples == []  # no mapping generated

    def test_auxiliary_class_does_not_create_mapping(self, strategy):
        """Classes without corresponding tables must not produce R2RML mappings."""
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        ns = Namespace(prefix)
        # PersonConcept should NOT appear as a subject class
        assert (None, RR["class"], ns.PersonConcept) not in g

    def test_object_property_fk_gets_iri_template(self, strategy):
        """FK column with generated ObjectProperty → IRI template ObjectMap."""
        prefix = "http://ex.org/ind#"
        g = Graph()
        ns = Namespace(prefix)
        PROV = Namespace("http://www.w3.org/ns/prov#")
        # Class for 'orders'
        g.add((ns.Order, RDF.type, OWL.Class))
        g.add(
            (
                ns.Order,
                PROV.wasDerivedFrom,
                URIRef(f"{prefix.rstrip('#/')}/provenance/orders"),
            )
        )
        # Object property for customer FK
        g.add((ns.placedBy, RDF.type, OWL.ObjectProperty))
        g.add((ns.placedBy, RDFS.domain, ns.Order))
        g.add((ns.placedBy, RDFS.range, ns.Customer))
        g.add((ns.placedBy, RDFS.label, Literal("placedBy")))
        g.add(
            (
                ns.placedBy,
                PROV.wasDerivedFrom,
                URIRef(f"{prefix.rstrip('#/')}/provenance/orders/customer_id"),
            )
        )

        table = CatalogTable(
            id="orders",
            name="orders",
            fullyQualifiedName="db.orders",
            columns=[CatalogColumn(name="customer_id", dataType="BIGINT")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.id"],
                ),
            ],
        )
        r = RigorOntologyStrategy().build_r2rml(
            prefix,
            [table],
            {"orders"},
            g,
        )
        from coa_ontology.inducer.strategies.base import RR

        om = URIRef(f"{prefix}TriplesMap_orders/POM_CustomerId/ObjectMap")
        assert (om, RR.termType, RR.IRI) in r
        tmpl = str(r.value(om, RR.template))
        assert "customers/" in tmpl
        assert '{"customer_id"}' in tmpl

    def test_datatype_property_uses_declared_xsd_range(self, strategy):
        """If a generated DatatypeProperty declares rdfs:range xsd:*, use it."""
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = self._customers_table()
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        # ind:hasEmail declares rdfs:range xsd:string
        om_email = URIRef(f"{prefix}TriplesMap_customers/POM_Email/ObjectMap")
        assert (om_email, RR.datatype, XSD.string) in g
        # ind:identifier declares rdfs:range xsd:long → use it
        om_id = URIRef(f"{prefix}TriplesMap_customers/POM_Id/ObjectMap")
        assert (om_id, RR.datatype, XSD.long) in g

    def test_missing_property_for_column_logs_and_skips(self, strategy):
        """If no generated property matches a column, omit its POM (don't crash)."""
        prefix = "http://ex.org/ind#"
        proposal = self._make_graph(prefix)
        table = CatalogTable(
            id="customers",
            name="customers",
            fullyQualifiedName="db.customers",
            columns=[
                CatalogColumn(name="id", dataType="BIGINT"),
                CatalogColumn(name="nonexistent_col", dataType="VARCHAR"),
            ],
        )
        g = strategy.build_r2rml(prefix, [table], {"customers"}, proposal)
        from coa_ontology.inducer.strategies.base import RR

        # POM for nonexistent_col should NOT exist
        pom = URIRef(f"{prefix}TriplesMap_customers/POM_NonexistentCol")
        assert (pom, None, None) not in g
        # But the 'id' column still gets its mapping
        pom_id = URIRef(f"{prefix}TriplesMap_customers/POM_Id")
        assert list(g.triples((pom_id, RR.predicate, None)))

    def test_class_resolved_by_pascal_local_name_fallback(self, strategy):
        """No provenance, no label match → fall back to PascalCase(table) local name."""
        prefix = "http://ex.org/ind#"
        g = Graph()
        ns = Namespace(prefix)
        # Class named PurchaseOrder with NO provenance and a non-matching label
        g.add((ns.PurchaseOrder, RDF.type, OWL.Class))
        g.add((ns.PurchaseOrder, RDFS.label, Literal("Purchase Order Entity")))
        # One property so the mapping is non-trivial
        g.add((ns.hasAmount, RDF.type, OWL.DatatypeProperty))
        g.add((ns.hasAmount, RDFS.domain, ns.PurchaseOrder))
        g.add((ns.hasAmount, RDFS.label, Literal("amount")))

        table = CatalogTable(
            id="t",
            name="purchase_order",
            fullyQualifiedName="db.purchase_order",
            columns=[CatalogColumn(name="amount", dataType="DECIMAL")],
        )
        r = RigorOntologyStrategy().build_r2rml(
            prefix,
            [table],
            {"purchase_order"},
            g,
        )
        from coa_ontology.inducer.strategies.base import RR

        subj = URIRef(f"{prefix}TriplesMap_purchase_order/SubjectMap")
        # Class resolved via PascalCase("purchase_order") == "PurchaseOrder" local-name match
        assert (subj, RR["class"], ns.PurchaseOrder) in r


class TestBuildR2rmlRigorCrossSourceCollision:
    """RIGOR must not fuse two datasources' same-named tables.

    Before the identity model, both ``public.customers`` tables were told to
    declare the same ``ind:Customers`` class and shared one ``TriplesMap_customers``
    IRI, one ``rr:tableName`` and one subject template — half the data unreachable,
    plus a mapping whose bare ``rr:tableName`` disagreed with the schema-qualified
    H2 validation schema, so Ontop rejected the whole namespace.
    """

    def _colliding_tables(self):
        def mk(ds, schema, extra_col):
            return CatalogTable(
                id=f"{ds}:{schema}.customers",
                name="customers",
                fullyQualifiedName=f"{schema}.customers",
                datasourceId=ds,
                sourceSchema=schema,
                columns=[
                    CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY"),
                    CatalogColumn(name=extra_col, dataType="VARCHAR"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )

        # Two PostgreSQL sources, both public.customers — the default schema for
        # every Postgres source, so this is the ordinary two-source case.
        return [mk("ds-pg1", "public", "name"), mk("ds-pg2", "public", "email")]

    def _proposal_graph(self, prefix, tables):
        """A proposal graph the LLM would emit given the disambiguated class names."""
        from coa_ontology.inducer.strategies.base import pascal_names_for, table_identity

        g = Graph()
        ns = Namespace(prefix)
        pascal = pascal_names_for(tables)
        for t in tables:
            cls = ns[pascal[table_identity(t)]]
            g.add((cls, RDF.type, OWL.Class))
            g.add((cls, RDFS.label, Literal(t.name)))
            for col in t.columns:
                prop = ns[f"{pascal[table_identity(t)]}_{col.name}"]
                g.add((prop, RDF.type, OWL.DatatypeProperty))
                g.add((prop, RDFS.domain, cls))
                g.add((prop, RDFS.label, Literal(col.name)))
        return g

    def test_collision_yields_two_distinct_triplesmaps(self):
        from coa_ontology.inducer.strategies.base import RR

        prefix = "http://ex.org/ind#"
        tables = self._colliding_tables()
        proposal = self._proposal_graph(prefix, tables)
        g = RigorOntologyStrategy().build_r2rml(prefix, tables, {"customers"}, proposal)

        tmaps = set(g.subjects(RDF.type, RR.TriplesMap))
        assert len(tmaps) == 2, "two same-named tables must not fuse onto one TriplesMap"
        # Each TriplesMap has exactly one logical table with exactly one tableName.
        for tm in tmaps:
            lts = list(g.objects(tm, RR.logicalTable))
            assert len(lts) == 1
            assert len(list(g.objects(lts[0], RR.tableName))) == 1

    def test_collision_tablenames_match_h2_schema(self):
        """Asserted against the DDL TEXT, not against ``logical_table_names``.

        Comparing the mapping's names to that helper's return value would pass even
        if the helper itself were wrong, because both sides call it. The invariant
        that actually matters is that Ontop, handed this mapping and this schema.sql,
        finds every ``rr:tableName`` declared — so the check is: does the generated
        DDL contain a CREATE for each emitted logical name, and a CREATE SCHEMA for
        each schema those names reference (H2 rejects a qualified CREATE TABLE whose
        schema does not exist).
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.proposals import _tables_to_h2_ddl

        prefix = "http://ex.org/ind#"
        tables = self._colliding_tables()
        proposal = self._proposal_graph(prefix, tables)
        g = RigorOntologyStrategy().build_r2rml(prefix, tables, {"customers"}, proposal)

        emitted = {str(g.value(lt, RR.tableName)) for lt in g.objects(None, RR.logicalTable)}
        assert len(emitted) == 2, "the two logical tables must be distinct"
        # The DDL is generated FROM the mapping's names, exactly as the accept path
        # does it (proposals._generate_schema_sql passes _mapping_table_names).
        ddl = _tables_to_h2_ddl(tables, mapping_table_names=emitted)
        for name in emitted:
            assert f"CREATE TABLE IF NOT EXISTS {name} (" in ddl, (
                f"the mapping declares logical table {name} but schema.sql does not create it — "
                f"Ontop rejects the whole namespace at VKG load. ddl=\n{ddl}"
            )
            # Split on the quote-dot-quote BOUNDARY, not on any dot: a table named
            # ``q1.results`` is one delimited identifier and has no schema.
            boundary = '"."'
            schema = f'{name.split(boundary)[0]}"' if boundary in name else ""
            if schema:
                assert f"CREATE SCHEMA IF NOT EXISTS {schema};" in ddl, (
                    f"schema.sql creates {name} without declaring schema {schema}; H2 fails the DDL. ddl=\n{ddl}"
                )
        # Both tables declared, so neither set of columns is silently dropped: the
        # second table's own column is present, which is what fusion destroyed.
        assert '"email"' in ddl and '"name"' in ddl, ddl

    def test_collision_classes_and_subjects_unfused(self):
        from coa_ontology.inducer.strategies.base import RR

        prefix = "http://ex.org/ind#"
        tables = self._colliding_tables()
        proposal = self._proposal_graph(prefix, tables)
        g = RigorOntologyStrategy().build_r2rml(prefix, tables, {"customers"}, proposal)

        subj_maps = list(g.objects(None, RR.subjectMap))
        classes = {g.value(sm, RR["class"]) for sm in subj_maps}
        templates = {str(g.value(sm, RR.template)) for sm in subj_maps}
        assert len(classes) == 2, "each table must map to its own class"
        assert len(templates) == 2, "each table must mint instance IRIs under its own token"

    def test_shared_name_llm_miss_does_not_drop_table(self):
        """F10: for a shared bare name, class resolution uses strategy 3 (the
        discriminated PascalCase local name) ALONE. If the LLM did not emit that
        exact name, the table was absent from the class index and build_r2rml
        dropped its TriplesMap entirely — its rows unreachable, strictly worse than
        fusing to one class. A deterministic fallback must pair the same-named
        tables against the generated classes so BOTH still get a TriplesMap."""
        from coa_ontology.inducer.strategies.base import RR

        prefix = "http://ex.org/ind#"
        tables = self._colliding_tables()

        # Non-compliant LLM response: BOTH classes named a bare "Customers" variant
        # (with distinct properties), not the discriminated names strategy 3
        # expects. This is the miss that dropped a table.
        g = Graph()
        ns = Namespace(prefix)
        for suffix, col in (("", "name"), ("_2", "email")):
            cls = ns[f"Customers{suffix}"]
            g.add((cls, RDF.type, OWL.Class))
            g.add((cls, RDFS.label, Literal("customers")))
            prop = ns[f"Customers{suffix}_{col}"]
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.domain, cls))
        result = RigorOntologyStrategy().build_r2rml(prefix, tables, {"customers"}, g)

        tmaps = set(result.subjects(RDF.type, RR.TriplesMap))
        assert len(tmaps) == 2, "an LLM class-name miss must not drop a same-named table's TriplesMap"

    def test_unique_name_output_unchanged(self):
        """A single-source table with a unique name keeps its bare artifacts."""
        from coa_ontology.inducer.strategies.base import RR

        prefix = "http://ex.org/ind#"
        table = CatalogTable(
            id="ds-pg:public.orders",
            name="orders",
            fullyQualifiedName="public.orders",
            datasourceId="ds-pg",
            sourceSchema="public",
            columns=[CatalogColumn(name="id", dataType="BIGINT", constraint="PRIMARY_KEY")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        proposal = self._proposal_graph(prefix, [table])
        g = RigorOntologyStrategy().build_r2rml(prefix, [table], {"orders"}, proposal)
        ns = Namespace(prefix)
        # Bare TriplesMap IRI and bare rr:tableName, exactly as before the fix.
        assert (ns.TriplesMap_orders, RDF.type, RR.TriplesMap) in g
        lt = g.value(ns.TriplesMap_orders, RR.logicalTable)
        assert str(g.value(lt, RR.tableName)) == '"orders"'


class TestNovelGraphSelfConsistency:
    """Tests that the novel graph returned by _check_novelty is self-consistent:
    referenced classes are declared, and no dangling blank-node references
    cause strict-RDF parser failures (e.g. Neptune)."""

    def _make_pipeline(self, score=0.85):
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]

        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            # High score so classes match existing; properties will also match
            return [
                {
                    "entity_uri": f"http://existing/{entity_type}/Match",
                    "ontology_id": "existing",
                    "score": score,
                }
            ]

        pipeline.oc.search_embeddings.side_effect = _search
        return pipeline

    def _build_graph_with_bnode_subclass(self, prefix: str) -> tuple[Graph, dict[str, str]]:
        """Graph where the class has owl:Restriction blank-node cardinality
        subClassOfs — exactly the pattern Opus 4.7 tends to emit."""
        g = Graph()
        ns = Namespace(prefix)
        Namespace("http://www.w3.org/ns/prov#")
        g.bind("ind", ns)
        # Class with cardinality restrictions
        g.add((ns.Order, RDF.type, OWL.Class))
        g.add((ns.Order, RDFS.label, Literal("Order")))
        # Owl:Restriction blank nodes — the bug trigger
        from rdflib import BNode

        bn = BNode()
        g.add((ns.Order, RDFS.subClassOf, bn))
        g.add((bn, RDF.type, OWL.Restriction))
        g.add((bn, OWL.onProperty, ns.hasId))
        # Novel object property referencing the (matched) class
        g.add((ns.placedBy, RDF.type, OWL.ObjectProperty))
        g.add((ns.placedBy, RDFS.domain, ns.Order))
        g.add((ns.placedBy, RDFS.range, ns.Customer))
        g.add((ns.placedBy, RDFS.label, Literal("placedBy")))
        uri_to_table = {
            str(ns.Order): "orders",
            str(ns.placedBy): "orders",
        }
        return g, uri_to_table

    def test_referenced_class_pulled_into_novel_graph(self):
        """When only a property is novel, its domain/range class must be included."""
        s = RigorOntologyStrategy()
        prefix = "http://ex.org/ind#"
        merged, uri_to_table = self._build_graph_with_bnode_subclass(prefix)
        pipeline = self._make_pipeline()

        # Override search so: Order matches (score 0.85), placedBy doesn't (score 0.0)
        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if entity_type == "class":
                return [{"entity_uri": "http://existing/class/Match", "score": 0.90}]
            return [{"entity_uri": "http://existing/property/Match", "score": 0.0}]

        pipeline.oc.search_embeddings.side_effect = _search

        novel_graph, _, _ = s._check_novelty(
            merged,
            [CatalogTable(id="o", name="orders", fullyQualifiedName="db.orders")],
            prefix,
            pipeline,
            0.80,
            uri_to_table,
        )

        ns = Namespace(prefix)
        # placedBy (novel) must be present
        assert (ns.placedBy, RDF.type, OWL.ObjectProperty) in novel_graph
        # Order (matched but referenced) must also have a class declaration
        assert (ns.Order, RDF.type, OWL.Class) in novel_graph
        assert (ns.Order, RDFS.label, Literal("Order")) in novel_graph

    def test_bnode_subclassof_not_copied_to_novel_graph(self):
        """Blank-node rdfs:subClassOf on a pulled-in referenced class
        must NOT be copied (would produce dangling RDF rejected by Neptune)."""
        s = RigorOntologyStrategy()
        prefix = "http://ex.org/ind#"
        merged, uri_to_table = self._build_graph_with_bnode_subclass(prefix)
        pipeline = self._make_pipeline()

        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if entity_type == "class":
                return [{"entity_uri": "http://existing/class/Match", "score": 0.90}]
            return [{"entity_uri": "http://existing/property/Match", "score": 0.0}]

        pipeline.oc.search_embeddings.side_effect = _search

        novel_graph, _, _ = s._check_novelty(
            merged,
            [CatalogTable(id="o", name="orders", fullyQualifiedName="db.orders")],
            prefix,
            pipeline,
            0.80,
            uri_to_table,
        )

        from rdflib import BNode

        # No subClassOf edge with a blank-node target should appear in the novel graph
        bnode_subclass_edges = [
            (s_, o_) for s_, _, o_ in novel_graph.triples((None, RDFS.subClassOf, None)) if isinstance(o_, BNode)
        ]
        assert bnode_subclass_edges == [], (
            f"Novel graph contains {len(bnode_subclass_edges)} rdfs:subClassOf "
            "triples pointing to blank nodes — would cause Neptune Malformed RDF"
        )

    def test_novel_turtle_round_trips(self):
        """The novel graph must serialize and re-parse without warnings."""
        s = RigorOntologyStrategy()
        prefix = "http://ex.org/ind#"
        merged, uri_to_table = self._build_graph_with_bnode_subclass(prefix)
        pipeline = self._make_pipeline()

        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if entity_type == "class":
                return [{"entity_uri": "http://existing/class/Match", "score": 0.90}]
            return [{"entity_uri": "http://existing/property/Match", "score": 0.0}]

        pipeline.oc.search_embeddings.side_effect = _search

        novel_graph, _, _ = s._check_novelty(
            merged,
            [CatalogTable(id="o", name="orders", fullyQualifiedName="db.orders")],
            prefix,
            pipeline,
            0.80,
            uri_to_table,
        )
        ttl = novel_graph.serialize(format="turtle")
        round_trip = Graph()
        round_trip.parse(data=ttl, format="turtle")
        # No loss/gain of triples on a round trip
        assert len(round_trip) == len(novel_graph)

    def test_table_with_referenced_class_added_to_novel_tables(self):
        """When a table's class is matched (not novel) but a property from
        that table IS novel, the table must still appear in novel_tables
        so the R2RML builder iterates it."""
        s = RigorOntologyStrategy()
        prefix = "http://ex.org/ind#"
        merged, uri_to_table = self._build_graph_with_bnode_subclass(prefix)
        pipeline = self._make_pipeline()

        def _search(*, vector, embedding_type, model_id, entity_type, top_k):
            if entity_type == "class":
                return [{"entity_uri": "http://existing/class/Match", "score": 0.90}]
            return []

        pipeline.oc.search_embeddings.side_effect = _search

        _, novel_tables, _ = s._check_novelty(
            merged,
            [CatalogTable(id="o", name="orders", fullyQualifiedName="db.orders")],
            prefix,
            pipeline,
            0.80,
            uri_to_table,
        )

        assert "orders" in novel_tables


class TestFixDualTypedProperties:
    """Tests for _fix_dual_typed_properties — OWL 2 QL compliance sanitization."""

    def test_removes_datatype_property_when_also_object_property(self):
        g = Graph()
        ns = Namespace("http://ex.org/")
        prop = ns["fk_col"]
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.range, XSD.long))
        g.add((prop, RDFS.range, ns["Target"]))

        RigorOntologyStrategy._fix_dual_typed_properties(g)

        assert (prop, RDF.type, OWL.ObjectProperty) in g
        assert (prop, RDF.type, OWL.DatatypeProperty) not in g
        assert (prop, RDFS.range, XSD.long) not in g
        assert (prop, RDFS.range, ns["Target"]) in g

    def test_leaves_pure_object_property_untouched(self):
        g = Graph()
        ns = Namespace("http://ex.org/")
        prop = ns["fk_col"]
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, ns["Target"]))

        RigorOntologyStrategy._fix_dual_typed_properties(g)

        assert (prop, RDF.type, OWL.ObjectProperty) in g
        assert (prop, RDFS.range, ns["Target"]) in g

    def test_leaves_pure_datatype_property_untouched(self):
        g = Graph()
        ns = Namespace("http://ex.org/")
        prop = ns["name"]
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.range, XSD.string))

        RigorOntologyStrategy._fix_dual_typed_properties(g)

        assert (prop, RDF.type, OWL.DatatypeProperty) in g
        assert (prop, RDFS.range, XSD.string) in g

    def test_handles_multiple_dual_typed_properties(self):
        g = Graph()
        ns = Namespace("http://ex.org/")
        for name in ["fk1", "fk2", "fk3"]:
            prop = ns[name]
            g.add((prop, RDF.type, OWL.ObjectProperty))
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.range, XSD.integer))

        RigorOntologyStrategy._fix_dual_typed_properties(g)

        for name in ["fk1", "fk2", "fk3"]:
            prop = ns[name]
            assert (prop, RDF.type, OWL.ObjectProperty) in g
            assert (prop, RDF.type, OWL.DatatypeProperty) not in g


class TestGroundedTableConnectivity:
    """Grounded tables must emit rdfs:subClassOf + all properties (connected graph)."""

    def _make_tables(self):
        return [
            CatalogTable(
                id="t1",
                name="Claim",
                fullyQualifiedName="db.Claim",
                columns=[
                    CatalogColumn(name="id", dataType="BIGINT"),
                    CatalogColumn(name="policy_id", dataType="BIGINT"),
                    CatalogColumn(name="amount", dataType="DECIMAL"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["policy_id"],
                        referredColumns=["db.Policy.id"],
                    ),
                ],
            ),
            CatalogTable(
                id="t2",
                name="Policy",
                fullyQualifiedName="db.Policy",
                columns=[
                    CatalogColumn(name="id", dataType="BIGINT"),
                    CatalogColumn(name="org_id", dataType="BIGINT"),
                    CatalogColumn(name="premium", dataType="DECIMAL"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["org_id"],
                        referredColumns=["db.Organization.id"],
                    ),
                ],
            ),
            CatalogTable(
                id="t3",
                name="Organization",
                fullyQualifiedName="db.Organization",
                columns=[
                    CatalogColumn(name="id", dataType="BIGINT"),
                    CatalogColumn(name="name", dataType="VARCHAR"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                ],
            ),
        ]

    def test_grounded_tables_get_properties(self):
        from coa_ontology.inducer.schemas import ConceptMatch
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        strategy = TableToOntologyStrategy()
        tables = self._make_tables()
        matches = [
            ConceptMatch(
                source_table="Claim",
                source_column="",
                matched_class_uri=None,
                matched_ontology_id=None,
                similarity=None,
                match_type="novel",
            ),
            ConceptMatch(
                source_table="Policy",
                source_column="",
                matched_class_uri="https://fibo.org/Policy",
                matched_ontology_id="fibo-agreements",
                similarity=0.92,
                match_type="exact",
            ),
            ConceptMatch(
                source_table="Organization",
                source_column="",
                matched_class_uri="https://schema.org/Organization",
                matched_ontology_id="schema-org",
                similarity=0.88,
                match_type="high_confidence",
            ),
        ]

        g, novel_tables = strategy._build_proposal_ontology("http://ex.org/ind#", tables, matches)

        ns = Namespace("http://ex.org/ind#")

        # Novel tables tracked correctly
        assert novel_tables == {"Claim"}

        # All 3 classes declared
        classes = set(g.subjects(RDF.type, OWL.Class))
        assert ns["Claim"] in classes
        assert ns["Policy"] in classes
        assert ns["Organization"] in classes

        # Grounded classes have rdfs:subClassOf
        assert (ns["Policy"], RDFS.subClassOf, URIRef("https://fibo.org/Policy")) in g
        assert (ns["Organization"], RDFS.subClassOf, URIRef("https://schema.org/Organization")) in g

        # Policy has its FK ObjectProperty to Organization (connectivity)
        policy_org_prop = ns["policy_orgId"]
        assert (policy_org_prop, RDF.type, OWL.ObjectProperty) in g
        assert (policy_org_prop, RDFS.domain, ns["Policy"]) in g
        assert (policy_org_prop, RDFS.range, ns["Organization"]) in g

        # Policy has its DatatypeProperty (premium)
        policy_premium = ns["policy_premium"]
        assert (policy_premium, RDF.type, OWL.DatatypeProperty) in g
        assert (policy_premium, RDFS.domain, ns["Policy"]) in g

        # Claim has FK to Policy
        claim_policy = ns["claim_policyId"]
        assert (claim_policy, RDF.type, OWL.ObjectProperty) in g
        assert (claim_policy, RDFS.range, ns["Policy"]) in g

    def test_graph_connectivity(self):
        """All classes should be reachable via ObjectProperty edges."""
        from coa_ontology.inducer.schemas import ConceptMatch
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        strategy = TableToOntologyStrategy()
        tables = self._make_tables()
        matches = [
            ConceptMatch(
                source_table="Claim",
                source_column="",
                matched_class_uri=None,
                matched_ontology_id=None,
                similarity=None,
                match_type="novel",
            ),
            ConceptMatch(
                source_table="Policy",
                source_column="",
                matched_class_uri="https://fibo.org/Policy",
                matched_ontology_id="fibo",
                similarity=0.92,
                match_type="exact",
            ),
            ConceptMatch(
                source_table="Organization",
                source_column="",
                matched_class_uri="https://schema.org/Organization",
                matched_ontology_id="schema-org",
                similarity=0.88,
                match_type="high_confidence",
            ),
        ]

        g, _ = strategy._build_proposal_ontology("http://ex.org/ind#", tables, matches)

        # Build adjacency from ObjectProperty domain/range
        ns = Namespace("http://ex.org/ind#")
        local_classes = {str(ns["Claim"]), str(ns["Policy"]), str(ns["Organization"])}
        adj: dict[str, set[str]] = {c: set() for c in local_classes}

        for prop in g.subjects(RDF.type, OWL.ObjectProperty):
            domains = [str(d) for d in g.objects(prop, RDFS.domain)]
            ranges = [str(r) for r in g.objects(prop, RDFS.range)]
            for d in domains:
                for r in ranges:
                    if d in local_classes and r in local_classes:
                        adj[d].add(r)
                        adj[r].add(d)

        # BFS from Claim — should reach all 3
        visited: set[str] = set()
        queue = [str(ns["Claim"])]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            queue.extend(adj.get(node, set()) - visited)

        assert visited == local_classes, f"Disconnected: only reached {visited}"


# ── Dropped tables tracking ─────────────────────────────────────────────


class TestDroppedTables:
    """Tests for dropped_tables tracking in the RIGOR strategy."""

    def test_gen_llm_empty_returns_dropped_table(self):
        """When Gen-LLM returns empty, table is marked as dropped with reason."""
        s = RigorOntologyStrategy()
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]
        pipeline.oc.search_embeddings.return_value = []

        # Mock LLM client that returns None
        mock_client = MagicMock()
        s._get_llm_client = MagicMock(return_value=mock_client)
        s._call_llm = MagicMock(return_value=None)

        table = CatalogTable(id="t", name="patients", fullyQualifiedName="db.patients")

        _, _, _, dropped = s.induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/ind#",
            config={},
            pipeline=pipeline,
        )

        assert len(dropped) == 1
        assert dropped[0]["table_name"] == "patients"
        assert dropped[0]["reason"] == "gen_llm_empty"

    def test_parse_failure_returns_dropped_table(self):
        """When all parse attempts fail, table is marked as dropped."""
        s = RigorOntologyStrategy()
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]
        pipeline.oc.search_embeddings.return_value = []

        # Mock LLM client that returns unparseable Turtle
        mock_client = MagicMock()
        s._get_llm_client = MagicMock(return_value=mock_client)
        s._call_llm = MagicMock(return_value="this is not turtle at all!!!")

        table = CatalogTable(id="t", name="orders", fullyQualifiedName="db.orders")

        _, _, _, dropped = s.induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/ind#",
            config={},
            pipeline=pipeline,
        )

        assert len(dropped) == 1
        assert dropped[0]["table_name"] == "orders"
        assert dropped[0]["reason"] == "parse_failed"

    def test_successful_tables_not_in_dropped(self):
        """Successfully processed tables do not appear in dropped_tables."""
        s = RigorOntologyStrategy()
        pipeline = MagicMock()
        pipeline.eg.model_id = "titan-v2"
        pipeline.eg.embed_texts.side_effect = lambda texts: [[0.0] * 8 for _ in texts]
        pipeline.oc.search_embeddings.return_value = []

        # Mock LLM client that returns valid Turtle
        mock_client = MagicMock()
        s._get_llm_client = MagicMock(return_value=mock_client)
        s._call_llm = MagicMock(return_value="ind:Patient a owl:Class .")

        table = CatalogTable(id="t", name="patients", fullyQualifiedName="db.patients")

        _, _, _, dropped = s.induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/ind#",
            config={},
            pipeline=pipeline,
        )

        assert dropped == []
