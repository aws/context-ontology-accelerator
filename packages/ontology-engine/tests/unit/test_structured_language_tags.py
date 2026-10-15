# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace language on the structured ``table_to_ontology`` path.

Pins LLD sections 2.2.4 and 2.2.6 for database sources: labels stay the
physical names and are never tagged; descriptions, synonyms, glossary terms
and tags are tagged only when ``generatedFields`` proves COA generated them; a
note joined with a description without that proof stays untagged; fixed text
follows the reviewed ``en``/``ja`` templates; an ``en`` namespace is
byte-identical to ``main``.
"""

from __future__ import annotations

import pytest
from coa_ontology.induce_catalog import _catalog_to_tables
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.base import GLOSSARY_TERM, SCL, TAG, add_glossary_and_tags
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from rdflib import RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from .language_fixtures import GOLDEN_DIR, STRUCTURED_PREFIX, structured_turtle

pytestmark = pytest.mark.unit

IND = Namespace(STRUCTURED_PREFIX)


def _build(tables: list[CatalogTable], namespace_language: str) -> Graph:
    graph, _novel = TableToOntologyStrategy()._build_proposal_ontology(
        STRUCTURED_PREFIX, tables, [], namespace_language=namespace_language
    )
    return graph


def _japanese_tables() -> list[CatalogTable]:
    return [
        CatalogTable(
            id="shop.customers",
            name="customers",
            fullyQualifiedName="shop.customers",
            description="商品を購入する顧客",
            synonyms=["得意先", "購入者"],
            generatedFields=["description", "synonyms"],
            columns=[
                CatalogColumn(
                    name="customer_id",
                    dataType="int",
                    description="顧客の識別番号",
                    synonyms=["顧客番号"],
                    generatedFields=["description", "synonyms"],
                ),
                # A steward edited the description, so only synonyms keep the proof.
                CatalogColumn(
                    name="segment",
                    dataType="varchar",
                    description="市場区分（担当者が記入）",
                    synonyms=["区分"],
                    distinctValues=["小売", "卸売"],
                    generatedFields=["synonyms"],
                ),
                # Generated, but mixes in a Latin identifier.
                CatalogColumn(
                    name="plc_panel",
                    dataType="varchar",
                    description="PLC制御盤の名称",
                    generatedFields=["description"],
                ),
            ],
        ),
        # Legacy metadata: no proof at all, even though the text is Japanese.
        CatalogTable(
            id="shop.orders",
            name="注文",
            fullyQualifiedName="shop.注文",
            description="購入注文",
            synonyms=["受注"],
            columns=[
                CatalogColumn(name="注文番号", dataType="int", description="注文を識別する番号"),
                CatalogColumn(name="customer_id", dataType="int"),
            ],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.customer_id"],
                    relationshipType="DETERMINISTIC",
                ),
            ],
        ),
    ]


def _literals(graph: Graph, subject: URIRef, predicate: URIRef) -> set[Literal]:
    return {o for o in graph.objects(subject, predicate) if isinstance(o, Literal)}


class TestEnglishNamespaceIsUnchanged:
    def test_structured_output_is_byte_identical_to_main(self) -> None:
        # Golden generated from the base engine; see language_fixtures.py.
        golden = (GOLDEN_DIR / "structured-en.ttl").read_bytes()
        assert structured_turtle(namespace_language="en").encode("utf-8") == golden

    def test_default_language_is_english(self) -> None:
        assert structured_turtle().encode("utf-8") == (GOLDEN_DIR / "structured-en.ttl").read_bytes()

    @pytest.mark.parametrize("language", ["en-US", "en-GB"])
    def test_region_qualified_english_is_unchanged(self, language: str) -> None:
        golden = (GOLDEN_DIR / "structured-en.ttl").read_bytes()
        assert structured_turtle(namespace_language=language).encode("utf-8") == golden


class TestJapaneseNamespace:
    @pytest.fixture
    def graph(self) -> Graph:
        return _build(_japanese_tables(), "ja")

    def test_labels_stay_physical_names_and_untagged(self, graph: Graph) -> None:
        assert _literals(graph, IND.Customers, RDFS.label) == {Literal("customers")}
        # A Japanese physical name is still an identifier, not generated text.
        orders_cls = next(graph.subjects(RDFS.label, Literal("注文")))
        assert _literals(graph, orders_cls, RDFS.label) == {Literal("注文")}
        column_labels = {o for o in graph.objects(None, RDFS.label) if str(o) in {"customer_id", "注文番号"}}
        assert column_labels and all(o.language is None for o in column_labels)

    def test_table_description_and_synonyms_tagged_with_proof(self, graph: Graph) -> None:
        assert _literals(graph, IND.Customers, RDFS.comment) == {Literal("商品を購入する顧客", lang="ja")}
        assert _literals(graph, IND.Customers, SKOS.altLabel) == {
            Literal("得意先", lang="ja"),
            Literal("購入者", lang="ja"),
        }

    def test_column_description_and_synonyms_tagged_with_proof(self, graph: Graph) -> None:
        prop = IND.customers_customerId
        assert _literals(graph, prop, RDFS.comment) == {Literal("顧客の識別番号", lang="ja")}
        assert _literals(graph, prop, SKOS.altLabel) == {Literal("顧客番号", lang="ja")}

    def test_only_the_proven_field_is_tagged(self, graph: Graph) -> None:
        prop = IND.customers_segment
        assert _literals(graph, prop, RDFS.comment) == {Literal("市場区分（担当者が記入）")}
        assert _literals(graph, prop, SKOS.altLabel) == {Literal("区分", lang="ja")}
        # Sampled values are source data.
        assert _literals(graph, prop, SCL.distinctValues) == {Literal("小売"), Literal("卸売")}

    def test_latin_mixed_generated_text_stays_untagged(self, graph: Graph) -> None:
        assert _literals(graph, IND.customers_plcPanel, RDFS.comment) == {Literal("PLC制御盤の名称")}

    def test_missing_proof_leaves_japanese_text_untagged(self, graph: Graph) -> None:
        orders_cls = next(graph.subjects(RDFS.label, Literal("注文")))
        assert _literals(graph, orders_cls, RDFS.comment) == {Literal("購入注文")}
        assert _literals(graph, orders_cls, SKOS.altLabel) == {Literal("受注")}
        column = next(graph.subjects(RDFS.label, Literal("注文番号")))
        assert _literals(graph, column, RDFS.comment) == {Literal("注文を識別する番号")}

    def test_ontology_label_uses_the_reviewed_japanese_title(self, graph: Graph) -> None:
        assert _literals(graph, URIRef(STRUCTURED_PREFIX.rstrip("#")), RDFS.label) == {
            Literal("提案オントロジー", lang="ja")
        }

    def test_foreign_key_note_is_japanese_and_untagged_because_of_identifiers(self, graph: Graph) -> None:
        fk_props = [s for s in graph.subjects(SCL.fkProvenance, Literal("DETERMINISTIC"))]
        assert len(fk_props) == 1
        assert _literals(graph, fk_props[0], RDFS.comment) == {
            Literal("外部キー: 注文.customer_id は customers.customer_id を参照します")
        }

    def test_platform_vocabulary_stays_english(self, graph: Graph) -> None:
        assert (IND.matchConfidence, None, None) in graph
        assert _literals(graph, next(graph.subjects(SCL.fkProvenance, None)), SCL.fkProvenance) == {
            Literal("DETERMINISTIC")
        }


def test_unreviewed_language_keeps_english_fixed_text_untagged() -> None:
    graph = _build(_japanese_tables(), "ko")
    assert _literals(graph, URIRef(STRUCTURED_PREFIX.rstrip("#")), RDFS.label) == {Literal("Proposal Ontology")}
    # Japanese text cannot be Korean: no tag even with the proof.
    assert _literals(graph, IND.Customers, RDFS.comment) == {Literal("商品を購入する顧客")}


class TestCatalogProjectionCarriesGeneratedFields:
    def _catalog(self, *, with_proof: bool) -> dict:
        table_bm: dict = {"description": "注文", "synonyms": ["受注"]}
        column_bm: dict = {"description": "金額", "synonyms": []}
        if with_proof:
            table_bm["generatedFields"] = ["description", "synonyms"]
            column_bm["generatedFields"] = ["description"]
        return {
            "databases": [
                {
                    "name": "shop",
                    "tables": [
                        {
                            "name": "orders",
                            "businessMetadata": table_bm,
                            "columns": [{"name": "amount", "type": "int", "businessMetadata": column_bm}],
                        }
                    ],
                }
            ]
        }

    def test_table_and_column_proofs_reach_catalog_tables(self) -> None:
        (table,) = [CatalogTable(**t) for t in _catalog_to_tables(self._catalog(with_proof=True))]
        assert table.generatedFields == ["description", "synonyms"]
        assert table.columns[0].generatedFields == ["description"]

    def test_legacy_catalog_without_proof_proves_nothing(self) -> None:
        (table,) = [CatalogTable(**t) for t in _catalog_to_tables(self._catalog(with_proof=False))]
        assert table.generatedFields == []
        assert table.columns[0].generatedFields == []

    def test_projected_proof_drives_tagging(self) -> None:
        tables = [CatalogTable(**t) for t in _catalog_to_tables(self._catalog(with_proof=True))]
        graph = _build(tables, "ja")
        assert _literals(graph, IND.Orders, RDFS.comment) == {Literal("注文", lang="ja")}
        assert _literals(graph, IND.orders_amount, RDFS.comment) == {Literal("金額", lang="ja")}


# ── One comment for a generated note and a column description ────────────
#
# Main joins a foreign-key or composite-FK column's description and COA's note
# into one rdfs:comment, "<description> (<note>)". The note is generated; the
# description needs its generatedFields proof. Without the proof the joined
# literal has mixed provenance and stays untagged (languageTagCompatibilityV1
# rule 1). Japanese table and column names keep the note free of Latin letters,
# so the tag decision rests on provenance alone.

_FK_NOTE = "外部キー: 注文.顧客番号 は 顧客.顧客番号 を参照します"
_MALFORMED_NOTE = (
    "入庫 の不正な複合外部キーの一部です（列と参照先の数が一致しないか、参照先が複数のテーブルに"
    "またがっています）。結合条件を導出できないため、リテラルとしてマッピングしています"
)
_STEWARD_TEXT = "購入した顧客（営業部が記入）"
_GENERATED_TEXT = "購入した顧客"


def _japanese_fk_tables(description: str | None, generated_fields: list[str]) -> list[CatalogTable]:
    """``注文.顧客番号`` references ``顧客.顧客番号``; one Japanese column per table."""
    customers = CatalogTable(
        id="shop.顧客",
        name="顧客",
        fullyQualifiedName="shop.顧客",
        columns=[CatalogColumn(name="顧客番号", dataType="int")],
    )
    orders = CatalogTable(
        id="shop.注文",
        name="注文",
        fullyQualifiedName="shop.注文",
        columns=[
            CatalogColumn(name="顧客番号", dataType="int", description=description, generatedFields=generated_fields)
        ],
        tableConstraints=[
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["顧客番号"],
                referredColumns=["顧客.顧客番号"],
                relationshipType="DETERMINISTIC",
            )
        ],
    )
    return [customers, orders]


def _fk_comments(graph: Graph) -> set[Literal]:
    (fk_prop,) = graph.subjects(SCL.fkProvenance, Literal("DETERMINISTIC"))
    return _literals(graph, fk_prop, RDFS.comment)


def _japanese_composite_tables(member_description: str | None, generated_fields: list[str]) -> list[CatalogTable]:
    """A usable composite FK on ``在庫`` and a malformed one on ``入庫``.

    The usable FK ``(store_region, 店舗番号) -> 店舗`` absorbs ``店舗番号``. The
    malformed FK ``(倉庫番号, shelf) -> [倉庫.番号]`` has unequal column and target
    counts, so ``倉庫番号`` gets the malformed-composite note. Each referring table
    has one Japanese column name, so that column keeps its own property IRI.
    """
    stores = CatalogTable(
        id="shop.店舗",
        name="店舗",
        fullyQualifiedName="shop.店舗",
        columns=[CatalogColumn(name="region", dataType="varchar"), CatalogColumn(name="番号", dataType="int")],
    )
    warehouses = CatalogTable(
        id="shop.倉庫",
        name="倉庫",
        fullyQualifiedName="shop.倉庫",
        columns=[CatalogColumn(name="番号", dataType="int")],
    )
    stock = CatalogTable(
        id="shop.在庫",
        name="在庫",
        fullyQualifiedName="shop.在庫",
        columns=[
            CatalogColumn(name="store_region", dataType="varchar"),
            CatalogColumn(
                name="店舗番号", dataType="int", description=member_description, generatedFields=generated_fields
            ),
        ],
        tableConstraints=[
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["store_region", "店舗番号"],
                referredColumns=["店舗.region", "店舗.番号"],
                relationshipType="DETERMINISTIC",
            ),
        ],
    )
    receipts = CatalogTable(
        id="shop.入庫",
        name="入庫",
        fullyQualifiedName="shop.入庫",
        columns=[
            CatalogColumn(
                name="倉庫番号", dataType="int", description=member_description, generatedFields=generated_fields
            ),
            CatalogColumn(name="shelf", dataType="int"),
        ],
        tableConstraints=[
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["倉庫番号", "shelf"],
                referredColumns=["倉庫.番号"],
                relationshipType="DETERMINISTIC",
            ),
        ],
    )
    return [stores, warehouses, stock, receipts]


def _column_comments(graph: Graph, column: str) -> set[Literal]:
    (prop,) = graph.subjects(RDFS.label, Literal(column))
    return _literals(graph, prop, RDFS.comment)


class TestJoinedCommentProvenance:
    def test_steward_description_on_fk_column_leaves_joined_comment_untagged(self) -> None:
        graph = _build(_japanese_fk_tables(_STEWARD_TEXT, []), "ja")
        assert _fk_comments(graph) == {Literal(f"{_STEWARD_TEXT} ({_FK_NOTE})")}

    def test_generated_description_on_fk_column_tags_joined_comment(self) -> None:
        graph = _build(_japanese_fk_tables(_GENERATED_TEXT, ["description"]), "ja")
        assert _fk_comments(graph) == {Literal(f"{_GENERATED_TEXT} ({_FK_NOTE})", lang="ja")}

    def test_proof_for_another_field_does_not_cover_the_description(self) -> None:
        graph = _build(_japanese_fk_tables(_STEWARD_TEXT, ["synonyms", "glossaryTerms", "tags"]), "ja")
        assert _fk_comments(graph) == {Literal(f"{_STEWARD_TEXT} ({_FK_NOTE})")}

    @pytest.mark.parametrize("description", [None, "", "   "])
    def test_fk_column_without_description_tags_the_note_alone(self, description: str | None) -> None:
        graph = _build(_japanese_fk_tables(description, []), "ja")
        assert _fk_comments(graph) == {Literal(_FK_NOTE, lang="ja")}

    def test_english_namespace_keeps_the_joined_comment_untagged(self) -> None:
        graph = _build(_japanese_fk_tables(_GENERATED_TEXT, ["description"]), "en")
        (comment,) = _fk_comments(graph)
        assert comment.language is None
        assert str(comment).startswith(f"{_GENERATED_TEXT} (Foreign key: ")

    def test_steward_description_on_composite_members_leaves_joined_comments_untagged(self) -> None:
        graph = _build(_japanese_composite_tables(_STEWARD_TEXT, []), "ja")
        assert _column_comments(graph, "倉庫番号") == {Literal(f"{_STEWARD_TEXT} ({_MALFORMED_NOTE})")}
        (member,) = _column_comments(graph, "店舗番号")
        assert member.language is None
        assert str(member).startswith(f"{_STEWARD_TEXT} (在庫 の複合外部キーの一部です。関係は ")

    def test_generated_description_on_composite_members_follows_the_note(self) -> None:
        graph = _build(_japanese_composite_tables(_GENERATED_TEXT, ["description"]), "ja")
        assert _column_comments(graph, "倉庫番号") == {Literal(f"{_GENERATED_TEXT} ({_MALFORMED_NOTE})", lang="ja")}
        # The carrier property's local name is ASCII, so this note stays untagged
        # whatever the description's provenance (rule 5).
        (member,) = _column_comments(graph, "店舗番号")
        assert member.language is None
        assert str(member).startswith(f"{_GENERATED_TEXT} (在庫 の複合外部キーの一部です。")

    def test_composite_members_without_description_tag_the_note_alone(self) -> None:
        graph = _build(_japanese_composite_tables(None, []), "ja")
        assert _column_comments(graph, "倉庫番号") == {Literal(_MALFORMED_NOTE, lang="ja")}


# ── Glossary terms and tags ──────────────────────────────────────────────
#
# Enrichment writes glossary terms and tags in the namespace language and marks
# them in generatedFields (LLD 2.2.3, 4.2), so they follow the same rule as
# descriptions and synonyms: tagged with the field's proof, plain without it.


def _glossary_tables(table_proof: list[str], column_proof: list[str]) -> list[CatalogTable]:
    customers = CatalogTable(
        id="shop.顧客",
        name="顧客",
        fullyQualifiedName="shop.顧客",
        columns=[CatalogColumn(name="顧客番号", dataType="int")],
    )
    orders = CatalogTable(
        id="shop.orders",
        name="orders",
        fullyQualifiedName="shop.orders",
        glossaryTerms=["受注", "PO番号"],
        tags=["財務"],
        generatedFields=table_proof,
        columns=[
            CatalogColumn(
                name="status",
                dataType="varchar",
                glossaryTerms=["注文状態"],
                tags=["区分値"],
                generatedFields=column_proof,
            ),
            CatalogColumn(
                name="customer_id",
                dataType="int",
                glossaryTerms=["購入者"],
                tags=["結合キー"],
                generatedFields=column_proof,
            ),
        ],
        tableConstraints=[
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=["顧客.顧客番号"],
                relationshipType="DETERMINISTIC",
            )
        ],
    )
    return [customers, orders]


_ALL_PROOF = ["glossaryTerms", "tags"]


class TestGlossaryTermsAndTags:
    def test_generated_glossary_terms_and_tags_are_tagged(self) -> None:
        graph = _build(_glossary_tables(_ALL_PROOF, _ALL_PROOF), "ja")
        # A term that mixes in Latin letters stays untagged (rule 5).
        assert _literals(graph, IND.Orders, GLOSSARY_TERM) == {Literal("受注", lang="ja"), Literal("PO番号")}
        assert _literals(graph, IND.Orders, TAG) == {Literal("財務", lang="ja")}
        assert _literals(graph, IND.orders_status, GLOSSARY_TERM) == {Literal("注文状態", lang="ja")}
        assert _literals(graph, IND.orders_status, TAG) == {Literal("区分値", lang="ja")}

    def test_fk_column_glossary_terms_and_tags_are_tagged(self) -> None:
        graph = _build(_glossary_tables([], _ALL_PROOF), "ja")
        (fk_prop,) = graph.subjects(SCL.fkProvenance, Literal("DETERMINISTIC"))
        assert _literals(graph, fk_prop, GLOSSARY_TERM) == {Literal("購入者", lang="ja")}
        assert _literals(graph, fk_prop, TAG) == {Literal("結合キー", lang="ja")}

    def test_without_proof_values_stay_plain(self) -> None:
        graph = _build(_glossary_tables([], []), "ja")
        assert _literals(graph, IND.Orders, GLOSSARY_TERM) == {Literal("受注"), Literal("PO番号")}
        assert _literals(graph, IND.Orders, TAG) == {Literal("財務")}
        assert _literals(graph, IND.orders_status, TAG) == {Literal("区分値")}

    def test_each_field_needs_its_own_proof(self) -> None:
        graph = _build(_glossary_tables(["glossaryTerms", "description"], ["tags", "synonyms"]), "ja")
        assert _literals(graph, IND.Orders, GLOSSARY_TERM) == {Literal("受注", lang="ja"), Literal("PO番号")}
        assert _literals(graph, IND.Orders, TAG) == {Literal("財務")}
        assert _literals(graph, IND.orders_status, GLOSSARY_TERM) == {Literal("注文状態")}
        assert _literals(graph, IND.orders_status, TAG) == {Literal("区分値", lang="ja")}

    @pytest.mark.parametrize("language", ["en", "en-US"])
    def test_english_namespace_keeps_them_plain(self, language: str) -> None:
        graph = _build(_glossary_tables(_ALL_PROOF, _ALL_PROOF), language)
        values = {o for p in (GLOSSARY_TERM, TAG) for o in graph.objects(None, p)}
        assert values and all(isinstance(o, Literal) and o.language is None for o in values)

    def test_helper_defaults_keep_main_behavior(self) -> None:
        # rigor_ontology (English-only) calls the helper without a language.
        g = Graph()
        add_glossary_and_tags(g, IND.X, ["受注"], ["財務"])
        assert _literals(g, IND.X, GLOSSARY_TERM) == {Literal("受注")}
        assert _literals(g, IND.X, TAG) == {Literal("財務")}
        add_glossary_and_tags(g, IND.Y, ["受注"], ["財務"], generated_fields=_ALL_PROOF)
        assert _literals(g, IND.Y, GLOSSARY_TERM) == {Literal("受注")}
