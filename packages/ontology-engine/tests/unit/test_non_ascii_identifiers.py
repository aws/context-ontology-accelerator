# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A schema whose table and column names are not Latin must still induce usably.

``to_pascal`` / ``to_camel`` mint the local name of every class, property and
R2RML term in the STRUCTURED (database) induction path. Both stripped every
character outside ``[a-zA-Z0-9\\s_\\-]``, so an identifier written in Japanese,
Chinese, Korean, Cyrillic, Greek, Hindi or Arabic reduced to the empty string.

Table names survived that by luck: :func:`pascal_names_for` already treats
``to_pascal`` as lossy and appends a deterministic discriminator, so N tables
whose names all vanish become ``Entity``, ``Entity_<hash>``, ... — ugly, but
distinct. COLUMN names have no such layer. Every column of one table collapsed
onto the single key ``{table}_``, which fused the columns into one property and
dropped all but one ``rr:predicateObjectMap`` per TriplesMap.

Measured against a deployed environment before this was fixed: a 3-table
Japanese schema (15 columns) produced 3 classes named ``Entity``,
``Entity_4719e596`` and ``Entity_60f9f8f9``, one merged DatatypeProperty per
table carrying 4 labels and a mixed ``rdfs:range``, and an R2RML mapping for
6 of 15 columns — 60% of the schema unqueryable.

The Latin cases below are the other half of the contract: ASCII identifiers must
mint byte-identical local names before and after, because every ontology induced
so far has ASCII identifiers and their IRIs must not move.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable
from coa_ontology.inducer.strategies.base import camel_names_for, pascal_names_for, to_camel, to_pascal
from rdflib import Graph

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"

# Verbatim from the Glue schema used for the deployed-environment reproduction.
_JA_TABLES = {
    "顧客": ["顧客id", "氏名", "メールアドレス", "居住地域", "会員登録日"],
    "注文": ["注文id", "顧客id", "注文日時", "注文合計金額", "注文ステータス"],
    "商品": ["商品id", "商品名", "商品カテゴリ", "税込単価", "在庫数"],
}


def _table(name: str, columns: list[str]) -> CatalogTable:
    return CatalogTable(
        id=f"id-{name}",
        name=name,
        fullyQualifiedName=f"nihongo_kensho.{name}",
        columns=[CatalogColumn(name=c, dataType="STRING") for c in columns],
        datasourceId="ds-1",
        sourceSchema="nihongo_kensho",
    )


class TestNonAsciiIdentifiersSurviveNormalization:
    """The root cause: the normalizers must not delete non-Latin identifiers."""

    @pytest.mark.parametrize(
        "identifier",
        [
            "顧客",  # Han
            "メールアドレス",  # Katakana
            "ひらがな",  # Hiragana
            "客户名称",  # Han (Simplified Chinese)
            "고객이름",  # Hangul
            "Название",  # Cyrillic
            "όνομα",  # Greek
            "कर्मचारी",  # Devanagari, carries combining marks
            "العميل",  # Arabic
            "อุณหภูมิ",  # Thai
        ],
    )
    def test_non_latin_identifier_does_not_vanish(self, identifier):
        """An identifier made only of non-Latin letters must mint a non-empty name.

        Empty is the specific failure that fused columns: it is not a cosmetic
        problem, because two different columns both mapping to ``""`` collide.
        """
        assert to_pascal(identifier) != "", f"to_pascal({identifier!r}) vanished"
        assert to_camel(identifier) != "", f"to_camel({identifier!r}) vanished"
        assert to_pascal(identifier) != "Entity", (
            f"to_pascal({identifier!r}) fell back to the Entity sentinel, which is reserved for genuinely empty input"
        )

    def test_mixed_script_identifier_keeps_both_halves(self):
        """``顧客id`` must not reduce to the ASCII tail.

        Glue lowercases the ASCII part of an identifier, so a Japanese primary key
        arrives as ``顧客id``. Dropping the Han left ``id`` — every table's primary
        key then minted the same property name.
        """
        assert to_camel("顧客id") not in ("", "id"), "the Han part of 顧客id was dropped, leaving the bare ASCII tail"

    def test_distinct_non_latin_identifiers_stay_distinct(self):
        """Non-Latin identifiers must not be mapped onto one another."""
        columns = _JA_TABLES["商品"]
        minted = [to_camel(c) for c in columns]
        assert len(set(minted)) == len(columns), (
            f"{len(columns)} distinct columns minted only {len(set(minted))} distinct "
            f"names: {dict(zip(columns, minted, strict=True))}"
        )


class TestAsciiIdentifiersAreUnchanged:
    """Regression guard: existing ASCII ontologies must keep their exact IRIs."""

    @pytest.mark.parametrize(
        ("identifier", "pascal", "camel"),
        [
            ("customers", "Customers", "customers"),
            ("order_items", "OrderItems", "orderItems"),
            ("order-items", "OrderItems", "orderItems"),
            ("ORDER ITEMS", "OrderItems", "orderItems"),
            ("customer_id", "CustomerId", "customerId"),
            ("total_amount", "TotalAmount", "totalAmount"),
            ("addr2", "Addr2", "addr2"),
        ],
    )
    def test_ascii_identifier_mints_the_same_name_as_before(self, identifier, pascal, camel):
        assert to_pascal(identifier) == pascal
        assert to_camel(identifier) == camel

    @pytest.mark.parametrize("empty", ["", "   ", "---", "___", "!!!"])
    def test_genuinely_empty_input_still_falls_back_to_entity(self, empty):
        """The ``Entity`` sentinel must survive for input with no letters at all.

        ``pascal_names_for`` and the R2RML/SHACL layers rely on ``to_pascal``
        returning a usable name for empty input rather than an empty local part.
        """
        assert to_pascal(empty) == "Entity"


class TestJapaneseSchemaProducesAUsableOntology:
    """End-to-end: the shape that was broken in the deployed environment."""

    @pytest.fixture
    def tables(self):
        return [_table(name, cols) for name, cols in _JA_TABLES.items()]

    def test_class_local_names_are_not_the_entity_sentinel(self, tables):
        """Three Japanese tables must not become ``Entity`` plus two hashes."""
        minted = pascal_names_for(tables)
        assert len(set(minted.values())) == len(tables)
        for identity, local in minted.items():
            assert not local.startswith("Entity"), (
                f"{identity} minted {local!r}: the Japanese table name was stripped and "
                "the collision layer papered over it with a hash"
            )

    def test_every_column_gets_its_own_property(self, tables):
        """No column may share a property IRI with another column of its table."""
        collisions = {}
        for table in tables:
            minted = [to_camel(c.name) for c in table.columns]
            if len(set(minted)) != len(minted):
                collisions[table.name] = dict(zip([c.name for c in table.columns], minted, strict=True))
        assert not collisions, f"columns fused onto shared property names: {collisions}"

    def test_r2rml_maps_every_column(self, tables):
        """Each TriplesMap must carry one predicateObjectMap per source column.

        This is the assertion that fails loudest on the real defect: the deployed
        reproduction mapped 6 of 15 columns.

        Counted over ``rr:predicate`` OBJECTS, deliberately. The POM node itself is
        a ``URIRef`` derived from the predicate, so two columns minting the same
        local name produce the same POM node and rdflib silently keeps one — which
        means counting POM *links* would report 15 while only 6 predicates exist.
        Counting the distinct predicates is what actually detects the fusion.
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        graph = TableToOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, Graph())

        expected = sum(len(t.columns) for t in tables)
        actual = len(set(graph.objects(None, RR.predicate)))
        assert actual == expected, (
            f"R2RML maps {actual} of {expected} columns — {expected - actual} columns are unqueryable"
        )


class TestColumnsThatDifferOnlyOutsideTheRetainedSet:
    """Retaining letters removed the worst collision, not the general one.

    ``to_camel`` drops Unicode category ``S`` and all punctuation, so two columns
    can still mint the same local name. In R2RML that failure is SILENT: the POM
    node is a ``URIRef`` derived from the predicate, so the second column produces
    the identical triples rather than a visible duplicate, and rdflib keeps one.
    The column stops being queryable with nothing in the output to say so.

    :func:`camel_names_for` is what makes it not happen; these are the cases that
    reach it.
    """

    @pytest.mark.parametrize(
        ("label", "columns"),
        [
            ("symbol", ["温度℃", "温度"]),
            ("percent", ["湿度%", "湿度"]),
            ("interpunct", ["住所・氏名", "住所氏名"]),
            ("fullwidth_paren", ["会員（有効）", "会員有効"]),
            ("ascii_operator", ["price+tax", "price_tax"]),
        ],
    )
    def test_columns_differing_only_by_a_dropped_character_stay_distinct(self, label, columns):
        table = _table("t", columns)
        minted = camel_names_for(table)
        assert len(set(minted.values())) == len(columns), (
            f"{label}: {columns} minted {sorted(set(minted.values()))} — one column is unqueryable"
        )
        assert set(minted) == set(columns), "every column must appear in the mapping"

    def test_r2rml_maps_both_halves_of_a_dropped_character_pair(self):
        """The end-to-end shape of the silent drop."""
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        tables = [_table("計測", ["温度℃", "温度", "湿度%", "湿度"])]
        graph = TableToOntologyStrategy().build_r2rml(PREFIX, tables, {"計測"}, Graph())
        assert len(set(graph.objects(None, RR.predicate))) == 4
        # The POM node is an IRI too — a shared node collapses two mappings into one.
        assert len(set(graph.objects(None, RR.predicateObjectMap))) == 4

    def test_the_ontology_and_the_mapping_agree_on_every_property(self):
        """A property declared by the ontology must be one the mapping populates.

        The two are built by different functions from the same tables; if only one
        of them dedupes, the ontology declares a property with nothing behind it and
        SPARQL over it silently returns nothing.
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        strategy = TableToOntologyStrategy()
        tables = [_table("計測", ["温度℃", "温度", "湿度%", "湿度"])]
        novel = {"計測"}

        mapping = strategy.build_r2rml(PREFIX, tables, novel, Graph())
        mapped = {str(p) for p in mapping.objects(None, RR.predicate)}

        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        from rdflib import OWL, RDF

        declared = {str(s) for s in ontology.subjects(RDF.type, OWL.DatatypeProperty) if str(s).startswith(PREFIX)} | {
            str(s) for s in ontology.subjects(RDF.type, OWL.ObjectProperty) if str(s).startswith(PREFIX)
        }

        assert declared, "the ontology declared no properties — the fixture stopped exercising this"
        assert declared == mapped, (
            "ontology and R2RML disagree:\n"
            f"  declared but not mapped: {sorted(declared - mapped)}\n"
            f"  mapped but not declared: {sorted(mapped - declared)}"
        )


class TestAllFourEmittersAgree:
    """The four artifacts are built by four functions; nothing made them agree.

    A property IRI is minted independently by the ontology builder, the R2RML
    builder, the SHACL shape builder and the RIGOR strategy's own R2RML override.
    If any one of them dedupes column names differently, it declares or targets a
    property the others never produce, and SPARQL over it silently returns nothing.
    Each of these tests fails if its emitter alone is reverted.
    """

    COLUMNS = ["温度℃", "温度", "湿度%", "湿度", "price+tax", "price_tax"]

    @pytest.fixture
    def tables(self):
        return [_table("計測", self.COLUMNS)]

    def test_shacl_shapes_target_properties_the_mapping_populates(self, tables):
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from coa_ontology.validation.shapes.config import generate_config_from_db

        mapped = {
            str(p)
            for p in TableToOntologyStrategy()
            .build_r2rml(PREFIX, tables, {t.name for t in tables}, Graph())
            .objects(None, RR.predicate)
        }
        config = generate_config_from_db(tables, PREFIX)
        targeted = {c.property_path for cls in config.classes for c in cls.constraints}

        assert targeted, "the SHACL config produced no constraints — the fixture stopped exercising this"
        assert targeted <= mapped, (
            f"SHACL shapes target properties the R2RML mapping never populates: {sorted(targeted - mapped)}"
        )

    def test_rigor_r2rml_emits_one_predicate_and_one_column_per_pom(self, tables):
        """Invalid R2RML, not just a dropped column.

        The RIGOR strategy takes its predicates from the proposal graph, so they are
        already distinct; it was the POM NODE that collided. Two columns sharing a
        POM put two ``rr:predicate`` on it and two ``rr:column`` on its ObjectMap,
        which cross-wires the columns rather than dropping one.
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        proposal = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])[0]
        graph = RigorOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, proposal)

        poms = set(graph.objects(None, RR.predicateObjectMap))
        if not poms:
            pytest.skip("RIGOR emitted no predicate-object maps for this fixture")
        for pom in poms:
            assert len(list(graph.objects(pom, RR.predicate))) == 1, f"{pom} carries multiple predicates"
            for om in graph.objects(pom, RR.objectMap):
                assert len(list(graph.objects(om, RR.column))) <= 1, f"{om} carries multiple columns"


class TestTwoEntriesSharingATableIdentity:
    """``table_identity`` is only ``{database}.{table}`` and a run flattens every
    datasource, so two catalog entries can share an identity while carrying
    different columns. Caching the per-table column mapping by identity is
    therefore last-wins, and hands one table the other's column names.
    """

    def test_the_ontology_declares_exactly_what_the_mapping_populates(self):
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from rdflib import OWL, RDF

        shared = "sales.orders"
        tables = [
            CatalogTable(
                id="a",
                name="orders",
                fullyQualifiedName=shared,
                columns=[CatalogColumn(name=c, dataType="STRING") for c in ("order_id", "qty")],
                datasourceId="ds-a",
                sourceSchema="sales",
            ),
            CatalogTable(
                id="b",
                name="orders",
                fullyQualifiedName=shared,
                columns=[CatalogColumn(name=c, dataType="STRING") for c in ("order_id", "温度℃", "温度")],
                datasourceId="ds-b",
                sourceSchema="sales",
            ),
        ]
        strategy = TableToOntologyStrategy()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, {"orders"}, Graph())

        declared = {str(s) for s in ontology.subjects(RDF.type, OWL.DatatypeProperty) if str(s).startswith(PREFIX)} | {
            str(s) for s in ontology.subjects(RDF.type, OWL.ObjectProperty) if str(s).startswith(PREFIX)
        }
        mapped = {str(p) for p in mapping.objects(None, RR.predicate)}

        assert mapped - declared == set(), (
            f"the mapping populates properties the ontology never declares: {sorted(mapped - declared)}"
        )


class TestFusionDoesNotMixColumnMetadata:
    """The column fusion carried each column's NL→SQL hints onto one property.

    Documented as Part B2 of the induction normalization issue and the reason the
    fusion is worse than a dropped column: ``_build_proposal_ontology`` attaches
    ``skos:altLabel`` from ``col.synonyms``, ``coa:distinctValues`` from
    ``col.distinctValues`` (sampled enum literals, emitted specifically so serve's
    NL→SQL context can hint the LLM with correct ``WHERE`` values) and
    ``rdfs:comment`` from ``col.description`` — all keyed on the property IRI.

    With every non-Latin column of a table minting one IRI, a single predicate was
    offered a city column's values AND a status column's values as candidate
    literals for the same ``WHERE`` clause. That produces a wrong query, not an ugly
    name.

    The columns below collide only OUTSIDE the retained character set, so they
    exercise :func:`camel_names_for` rather than the letter-retention fix — the
    latter already separates two plain Japanese names on its own, which would make
    this test pass for the wrong reason.
    """

    @pytest.fixture
    def graph(self):
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        table = CatalogTable(
            id="t",
            name="受注",
            fullyQualifiedName="sales.受注",
            columns=[
                CatalogColumn(
                    name="配送・都市",
                    dataType="STRING",
                    description="配送先都市",
                    synonyms=["市区町村"],
                    distinctValues=["東京", "大阪"],
                ),
                CatalogColumn(
                    name="配送都市",
                    dataType="STRING",
                    description="配送元都市",
                    synonyms=["発送地"],
                    distinctValues=["未処理", "完了"],
                ),
            ],
            datasourceId="ds",
            sourceSchema="sales",
        )
        return TableToOntologyStrategy()._build_proposal_ontology(PREFIX, [table], [])[0]

    def test_no_property_is_offered_two_columns_enum_values(self, graph):
        from rdflib import Namespace, URIRef

        scl = Namespace("http://coa.amazon.com/vocab/coa#")
        offenders = []
        for subject in {s for s in graph.subjects() if str(s).startswith(PREFIX)}:
            values = {str(v) for v in graph.objects(URIRef(str(subject)), scl.distinctValues)}
            if values & {"東京", "大阪"} and values & {"未処理", "完了"}:
                offenders.append((str(subject), sorted(values)))
        assert not offenders, f"a single property carries two columns' candidate WHERE literals: {offenders}"

    def test_each_column_keeps_only_its_own_metadata(self, graph):
        from rdflib import RDFS, Namespace, URIRef

        scl = Namespace("http://coa.amazon.com/vocab/coa#")
        skos = Namespace("http://www.w3.org/2004/02/skos/core#")
        expected = {
            ("東京", "大阪"): ({"市区町村"}, {"配送先都市"}),
            ("未処理", "完了"): ({"発送地"}, {"配送元都市"}),
        }
        by_values = {}
        for subject in {s for s in graph.subjects() if str(s).startswith(PREFIX)}:
            uri = URIRef(str(subject))
            values = frozenset(str(v) for v in graph.objects(uri, scl.distinctValues))
            if values:
                by_values[values] = uri
        assert len(by_values) == 2, (
            f"expected two distinctly-valued properties, got {len(by_values)}: "
            f"{ {tuple(sorted(k)): str(v) for k, v in by_values.items()} }"
        )
        for value_pair, (synonyms, comments) in expected.items():
            uri = by_values[frozenset(value_pair)]
            assert {str(v) for v in graph.objects(uri, skos.altLabel)} == synonyms, value_pair
            assert {str(v) for v in graph.objects(uri, RDFS.comment)} == comments, value_pair


class TestJapaneseEquipmentRegister:
    """The table that reproduced the defect on a deployed Tokyo stack.

    A Glue table with ten Japanese column names: on ``main`` nine of them fused into
    a single ``設備台帳_`` property, so the R2RML mapping exposed one column and the
    Tier 2 / VKG translation had nothing to join on for the rest.
    """

    COLUMNS = [
        "設備id",
        "建物名",
        "設備区分",
        "形名シリーズ",
        "駆動方式",
        "設置年",
        "定格出力kw",
        "製造者",
        "点検周期月",
        "備考",
    ]

    @pytest.fixture
    def tables(self):
        return [_table("設備台帳", self.COLUMNS)]

    @pytest.fixture
    def artifacts(self, tables):
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
        return ontology, mapping

    def _property_by_label(self, ontology):
        from rdflib import OWL, RDF, RDFS

        by_label: dict[str, list[str]] = {}
        for prop in ontology.subjects(RDF.type, OWL.DatatypeProperty):
            for label in ontology.objects(prop, RDFS.label):
                by_label.setdefault(str(label), []).append(str(prop))
        return by_label

    def test_each_column_gets_its_own_datatype_property_labelled_with_the_physical_name(self, artifacts):
        ontology, _ = artifacts
        by_label = self._property_by_label(ontology)
        for column in self.COLUMNS:
            assert len(by_label.get(column, [])) == 1, f"{column!r}: {by_label.get(column)}"
        iris = [by_label[c][0] for c in self.COLUMNS]
        assert len(set(iris)) == len(self.COLUMNS), f"columns share property IRIs: {iris}"
        for iri in iris:
            assert iri.startswith(PREFIX)
            assert not iri.endswith("_"), f"{iri} lost its column segment"

    def test_the_mapping_has_one_predicate_object_map_per_column(self, artifacts):
        from coa_common import sql_ident
        from coa_ontology.inducer.strategies.base import RR
        from rdflib import Literal, URIRef

        ontology, mapping = artifacts
        by_label = self._property_by_label(ontology)
        tmap = URIRef(f"{PREFIX}TriplesMap_{to_pascal('設備台帳')}")
        poms = list(mapping.objects(tmap, RR.predicateObjectMap))
        assert len(poms) == len(self.COLUMNS)

        column_to_predicate = {}
        for pom in poms:
            predicates = list(mapping.objects(pom, RR.predicate))
            object_maps = list(mapping.objects(pom, RR.objectMap))
            assert len(predicates) == 1 and len(object_maps) == 1, pom
            columns = list(mapping.objects(object_maps[0], RR.column))
            assert len(columns) == 1, pom
            column_to_predicate[str(columns[0])] = str(predicates[0])

        for column in self.COLUMNS:
            literal = str(Literal(sql_ident(column)))
            assert column_to_predicate.get(literal) == by_label[column][0], (
                f"{column!r} is not mapped to the property the ontology declares for it"
            )

    def test_artifacts_round_trip_through_turtle(self, artifacts):
        from rdflib.compare import isomorphic

        for graph in artifacts:
            reparsed = Graph().parse(data=graph.serialize(format="turtle"), format="turtle")
            assert isomorphic(graph, reparsed)

    def test_shacl_shapes_target_mapped_properties(self, tables, artifacts):
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.validation.shapes.config import generate_config_from_db

        _, mapping = artifacts
        mapped = {str(p) for p in mapping.objects(None, RR.predicate)}
        targeted = {c.property_path for cls in generate_config_from_db(tables, PREFIX).classes for c in cls.constraints}
        assert targeted and targeted <= mapped


class TestAsciiBehaviourChangesAreCollisionFixesOnly:
    """ASCII output moves only where ``main`` already minted one IRI for two columns.

    The golden in ``test_structured_ascii_golden`` covers the unchanged case. These
    two are the intended exceptions, both bug fixes.
    """

    def test_ascii_columns_that_collided_now_get_distinct_iris(self):
        """``OrderID`` and ``orderId`` both camel-case to ``orderid``.

        ``main`` fused them into one property. The lexicographically smallest name
        keeps the bare local name; the other gains a deterministic ``_<sha8>``.
        """
        table = _table("orders", ["OrderID", "orderId", "amount"])
        minted = camel_names_for(table)
        assert minted["OrderID"] == "orderid"
        assert minted["orderId"].startswith("orderid_") and len(minted["orderId"]) == len("orderid_") + 8
        assert minted["amount"] == "amount"
        assert camel_names_for(table) == minted, "the suffix must be deterministic"

    def test_symbol_only_column_names_agree_between_ontology_and_mapping(self):
        """A column named ``%`` minted ``t_`` in the ontology but ``t_entity`` in R2RML.

        ``table_to_ontology`` kept a private ``_to_pascal`` without the ``Entity``
        fallback the R2RML and SHACL builders use. Delegating to the shared helper
        makes the ontology declare the property the mapping populates.
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from rdflib import OWL, RDF

        tables = [_table("t", ["%", "id"])]
        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
        declared = {str(s) for s in ontology.subjects(RDF.type, OWL.DatatypeProperty)}
        mapped = {str(p) for p in mapping.objects(None, RR.predicate)}
        assert declared == mapped
        assert f"{PREFIX}t_entity" in declared


def _fk(column: str, referred: str):
    from coa_ontology.inducer.services.data_catalog import CatalogConstraint

    return CatalogConstraint(constraintType="FOREIGN_KEY", columns=[column], referredColumns=[referred])


def _fk_table(name: str, columns: list[str], fks: list) -> CatalogTable:
    return _table(name, columns).model_copy(update={"tableConstraints": fks})


def _relationship_views(tables: list[CatalogTable], referrer: str) -> tuple[dict, dict, dict]:
    """The FK properties of ``referrer`` as the ontology, the mapping and the shapes see them.

    Each view is ``{property local name: target}``: the ``rdfs:range`` class for the
    ontology, ``(parent TriplesMap, child column, parent column)`` for the mapping,
    and the ``sh:class`` target for the shapes.
    """
    from coa_ontology.inducer.strategies.base import RR
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
    from coa_ontology.validation.shapes.config import ConstraintType, generate_config_from_db
    from rdflib import OWL, RDF, RDFS, URIRef

    strategy = TableToOntologyStrategy()
    ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
    mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
    shapes = generate_config_from_db(tables, PREFIX)
    referrer_local = pascal_names_for(tables)[f"ds-1::nihongo_kensho.{referrer}"]

    def local(iri) -> str:
        return str(iri).removeprefix(PREFIX)

    in_ontology = {
        local(p): local(ontology.value(p, RDFS.range))
        for p in ontology.subjects(RDF.type, OWL.ObjectProperty)
        if (p, RDFS.domain, URIRef(f"{PREFIX}{referrer_local}")) in ontology
    }
    in_mapping = {}
    for pom in mapping.objects(URIRef(f"{PREFIX}TriplesMap_{referrer_local}"), RR.predicateObjectMap):
        object_map = mapping.value(pom, RR.objectMap)
        parent = mapping.value(object_map, RR.parentTriplesMap)
        if parent is None:
            continue
        join = mapping.value(object_map, RR.joinCondition)
        in_mapping[local(mapping.value(pom, RR.predicate))] = (
            local(parent),
            str(mapping.value(join, RR.child)),
            str(mapping.value(join, RR.parent)),
        )
    in_shapes = {
        local(c.property_path): local(c.params["target_class"])
        for cls in shapes.classes
        if cls.class_uri == f"{PREFIX}{referrer_local}"
        for c in cls.constraints
        if c.constraint_type == ConstraintType.REFERENCE
    }
    return in_ontology, in_mapping, in_shapes


class TestNonLatinForeignKeys:
    """An FK is minted from three names at once, and each one can be non-Latin.

    The FK property local comes from the referrer's class name and the column's
    deduped local, the join target from the target table's run-unique class name,
    and the ``rr:joinCondition`` from the raw column names. A path that re-derives
    any of them with ``to_pascal`` / ``to_camel`` instead of the collision-free
    maps points the join at a class or column the other artifacts never mint.
    """

    def test_a_japanese_fk_joins_in_every_emitter(self):
        tables = [
            _fk_table("顧客", ["顧客id", "氏名"], []),
            _fk_table("注文", ["注文id", "顧客id"], [_fk("顧客id", "顧客.顧客id")]),
        ]
        in_ontology, in_mapping, in_shapes = _relationship_views(tables, "注文")

        assert in_ontology == {"注文_顧客id": "顧客"}
        assert in_mapping == {"注文_顧客id": ("TriplesMap_顧客", '"顧客id"', '"顧客id"')}
        assert in_shapes == {"注文_顧客id": "顧客"}

    def test_an_fk_column_that_collides_with_a_plain_column_keeps_both(self):
        """``顧客ID`` and ``顧客ＩＤ`` fold to one camel name; only one of them is the FK.

        The FK column sorts first and keeps the bare local, so the relationship
        property is the same local the FK mints without the fullwidth twin. The fullwidth column
        gains the discriminator and stays a plain literal mapping rather than being
        absorbed into the join.
        """
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

        tables = [
            _fk_table("顧客", ["顧客id", "氏名"], []),
            _fk_table("注文", ["注文id", "顧客ID", "顧客ＩＤ"], [_fk("顧客ID", "顧客.顧客id")]),
        ]
        in_ontology, in_mapping, in_shapes = _relationship_views(tables, "注文")
        assert in_ontology == {"注文_顧客id": "顧客"}
        assert in_mapping == {"注文_顧客id": ("TriplesMap_顧客", '"顧客ID"', '"顧客id"')}
        assert in_shapes == {"注文_顧客id": "顧客"}

        plain = camel_names_for(tables[1])["顧客ＩＤ"]
        assert plain.startswith("顧客id_") and len(plain) == len("顧客id_") + 8
        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
        literal_columns = {
            str(mapping.value(mapping.value(pom, RR.objectMap), RR.column))
            for pom in mapping.subjects(RR.predicate, None)
            if str(mapping.value(pom, RR.predicate)) == f"{PREFIX}注文_{plain}"
        }
        assert literal_columns == {'"顧客ＩＤ"'}

    def test_an_fk_to_a_table_whose_name_collides_targets_that_tables_own_class(self):
        """``取引先（旧）`` and ``取引先旧`` mint one Pascal base; the FK names the first.

        ``取引先旧`` sorts first and keeps the bare class, so the FK has to resolve
        to the discriminated ``取引先旧_<sha8>`` in all three artifacts. Re-deriving
        the target with ``to_pascal`` would join the order to the other table.
        """
        tables = [
            _fk_table("取引先旧", ["id"], []),
            _fk_table("取引先（旧）", ["id"], []),
            _fk_table("注文", ["注文id", "取引先id"], [_fk("取引先id", "取引先（旧）.id")]),
        ]
        target = pascal_names_for(tables)["ds-1::nihongo_kensho.取引先（旧）"]
        assert target.startswith("取引先旧_") and len(target) == len("取引先旧_") + 8

        in_ontology, in_mapping, in_shapes = _relationship_views(tables, "注文")
        assert in_ontology == {"注文_取引先id": target}
        assert in_mapping == {"注文_取引先id": (f"TriplesMap_{target}", '"取引先id"', '"id"')}
        assert in_shapes == {"注文_取引先id": target}


class TestDiscriminatorCollisionWidening:
    """The widening loop runs only on a genuine SHA-256 prefix collision.

    ``to_pascal`` never emits an underscore, so no base can equal another base's
    ``{base}_{sha8}`` form, and the loop is otherwise unreachable. The tests force
    the collision by pinning the discriminator to a constant.
    """

    @pytest.fixture
    def constant_discriminator(self, monkeypatch):
        from coa_ontology.inducer.strategies import base

        monkeypatch.setattr(base, "_name_discriminator", lambda _identity: "deadbeef")

    def test_columns_widen_until_every_local_is_unique(self, constant_discriminator):
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from coa_ontology.validation.shapes.config import generate_config_from_db
        from rdflib import OWL, RDF

        columns = ["温度℃", "温度", "温度%", "温度‰"]
        tables = [_table("計測", columns)]
        minted = camel_names_for(tables[0])
        assert minted == {
            "温度": "温度",
            "温度%": "温度_deadbeef",
            "温度‰": "温度_deadbeef_2",
            "温度℃": "温度_deadbeef_3",
        }
        assert camel_names_for(_table("計測", columns[::-1])) == minted, "the widening must not depend on column order"

        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
        declared = {str(s) for s in ontology.subjects(RDF.type, OWL.DatatypeProperty)}
        mapped = {str(p) for p in mapping.objects(None, RR.predicate)}
        assert declared == mapped == {f"{PREFIX}計測_{local}" for local in minted.values()}
        assert len(set(mapping.objects(None, RR.predicateObjectMap))) == 4
        targeted = {c.property_path for cls in generate_config_from_db(tables, PREFIX).classes for c in cls.constraints}
        assert targeted and targeted <= mapped

    def test_tables_widen_until_every_class_is_unique(self, constant_discriminator):
        """Four tables, so the widening loop has to step past ``_2`` as well."""
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from coa_ontology.validation.shapes.config import generate_config_from_db
        from rdflib import OWL, RDF

        tables = [_table(name, ["id"]) for name in ("取引先（旧）", "取引先旧", "取引先・旧", "取引先-旧")]
        minted = pascal_names_for(tables)
        # Ordered by identity: - (U+002D) < ・ (U+30FB) < 旧 (U+65E7) < （ (U+FF08).
        assert minted == {
            "ds-1::nihongo_kensho.取引先-旧": "取引先旧",
            "ds-1::nihongo_kensho.取引先・旧": "取引先旧_deadbeef",
            "ds-1::nihongo_kensho.取引先旧": "取引先旧_deadbeef_2",
            "ds-1::nihongo_kensho.取引先（旧）": "取引先旧_deadbeef_3",
        }
        assert pascal_names_for(tables[::-1]) == minted, "the widening must not depend on table order"

        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)
        expected = {f"{PREFIX}{local}" for local in minted.values()}
        assert {str(c) for c in ontology.subjects(RDF.type, OWL.Class) if str(c).startswith(PREFIX)} == expected
        assert {str(c) for c in mapping.objects(None, RR["class"])} == expected
        assert {cls.class_uri for cls in generate_config_from_db(tables, PREFIX).classes} == expected


class TestLiteralEntityColumnBesideASymbolOnlyColumn:
    """``%`` falls back to the ``entity`` local; a column can also be named ``entity``.

    ``camel_names_for`` (since !1274) keeps the bare form for the smaller column
    name, and ``%`` sorts before ``entity``, so the symbol-only column keeps
    ``t_entity`` and the literal ``entity`` column gains the discriminator. Before
    !1274 the mapping minted ``t_entity`` / ``POM_Entity`` for both and cross-wired
    them, while the ontology declared ``t_entity`` labelled ``entity`` and ``t_``
    labelled ``%``. So the ontology meaning of ``t_entity`` moved from the literal
    column to ``%``. What this pins is that the two stay distinct and that every
    emitter agrees on which one is which.
    """

    def test_the_two_columns_mint_distinct_agreeing_properties(self):
        from coa_common import sql_ident
        from coa_ontology.inducer.strategies.base import RR
        from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
        from coa_ontology.validation.shapes.config import generate_config_from_db
        from rdflib import OWL, RDF, RDFS, Literal, URIRef

        tables = [_table("t", ["entity", "%"])]
        minted = camel_names_for(tables[0])
        assert minted["%"] == "entity"
        assert minted["entity"].startswith("entity_") and len(minted["entity"]) == len("entity_") + 8

        strategy = TableToOntologyStrategy()
        ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, novel, ontology)

        labels = {
            str(label): str(prop)
            for prop in ontology.subjects(RDF.type, OWL.DatatypeProperty)
            for label in ontology.objects(prop, RDFS.label)
        }
        assert labels == {"%": f"{PREFIX}t_entity", "entity": f"{PREFIX}t_{minted['entity']}"}

        tmap = URIRef(f"{PREFIX}TriplesMap_T")
        column_to_predicate = {
            str(mapping.value(mapping.value(pom, RR.objectMap), RR.column)): str(mapping.value(pom, RR.predicate))
            for pom in mapping.objects(tmap, RR.predicateObjectMap)
        }
        assert column_to_predicate == {str(Literal(sql_ident(column))): labels[column] for column in ("entity", "%")}
        targeted = {c.property_path for cls in generate_config_from_db(tables, PREFIX).classes for c in cls.constraints}
        assert targeted and targeted <= set(labels.values())
