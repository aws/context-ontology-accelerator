# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace language on the unstructured (document) induction path.

Pins LLD sections 2.2.4 and 2.2.6 for document sources: generated class labels,
descriptions, relationship names and topics carry the namespace tag when they
are lexically compatible; fixed text follows the reviewed ``en``/``ja``
templates; the class-description prompts carry an output-language instruction
only for a non-English namespace; an ``en`` namespace is byte-identical to
``main``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_ontology.inducer.unstructured.services.description_generator import (
    _ENRICHED_SYSTEM_PROMPT,
    _SCHEMA_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    ClassSchemaContext,
    EntityContext,
    SchemaEdge,
    generate_descriptions,
    generate_descriptions_with_context,
    generate_descriptions_with_schema,
)
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal
from rdflib.namespace import SKOS

from .language_fixtures import GOLDEN_DIR, unstructured_turtle

pytestmark = pytest.mark.unit

_JA_CLASSES = [
    ("制御盤", "制御盤", "制御盤は設備の運転を制御する装置です。"),
    ("PLC制御盤", "PLC制御盤", "PLCを収める盤です。"),
]


def _graph(turtle: str) -> Graph:
    return Graph().parse(data=turtle, format="turtle")


def _objects(graph: Graph, subject_label: str, predicate) -> set[Literal]:
    subject = next(s for s in graph.subjects(RDFS.label, None) if str(graph.value(s, RDFS.label)) == subject_label)
    return set(graph.objects(subject, predicate))


class TestEnglishNamespaceIsUnchanged:
    def test_unstructured_output_is_byte_identical_to_main(self) -> None:
        # Golden generated from the base engine; see language_fixtures.py.
        golden = (GOLDEN_DIR / "unstructured-en.ttl").read_bytes()
        assert unstructured_turtle(namespace_language="en").encode("utf-8") == golden

    def test_default_language_is_english(self) -> None:
        assert unstructured_turtle().encode("utf-8") == (GOLDEN_DIR / "unstructured-en.ttl").read_bytes()


class TestJapaneseRules:
    @pytest.fixture
    def graph(self) -> Graph:
        return _graph(unstructured_turtle(classes=_JA_CLASSES, namespace_language="ja"))

    def test_compatible_class_label_and_description_are_tagged(self, graph: Graph) -> None:
        assert _objects(graph, "制御盤", RDFS.comment) == {Literal("制御盤は設備の運転を制御する装置です。", lang="ja")}
        cls = next(graph.subjects(RDFS.label, Literal("制御盤", lang="ja")))
        assert (cls, RDF.type, OWL.Class) in graph

    def test_latin_mixed_class_label_stays_untagged(self, graph: Graph) -> None:
        assert (None, RDFS.label, Literal("PLC制御盤", datatype=XSD.string)) in graph
        assert _objects(graph, "PLC制御盤", RDFS.comment) == {Literal("PLCを収める盤です。", datatype=XSD.string)}

    def test_english_extraction_output_is_not_tagged_ja(self, graph: Graph) -> None:
        # The fixture facts and topic are English: no Japanese evidence, no tag.
        assert (None, RDFS.label, Literal("ISSUES", datatype=XSD.string)) in graph
        assert (None, SKOS.prefLabel, Literal("Bond issuance", datatype=XSD.string)) in graph

    def test_datatype_observation_uses_the_japanese_template(self, graph: Graph) -> None:
        # The class comes from rdfs:domain, not the sentence: a Latin-mixed class
        # name such as PLC制御盤 inside the frame would make it untaggable.
        comments = {str(o) for o in graph.objects(None, RDFS.comment)}
        assert "1 件のファクトで観測されました。" in comments
        assert not any("PLC制御盤" in c and "ファクト" in c for c in comments)

    def test_topic_scheme_label_is_japanese_but_untagged_around_the_ascii_name(self, graph: Graph) -> None:
        scheme = next(graph.subjects(RDF.type, SKOS.ConceptScheme))
        assert set(graph.objects(scheme, RDFS.label)) == {Literal("golden トピック体系", datatype=XSD.string)}

    def test_ontology_name_stays_untagged(self, graph: Graph) -> None:
        ontology = next(graph.subjects(RDF.type, OWL.Ontology))
        assert set(graph.objects(ontology, RDFS.label)) == {Literal("golden", datatype=XSD.string)}


def test_japanese_relationships_and_topics_are_tagged() -> None:
    from coa_ontology.inducer.unstructured.services.induction_rules import (
        induce_concepts,
        induce_object_properties,
    )
    from coa_ontology.inducer.unstructured.services.iri_resolver import InMemoryIRIResolver
    from coa_ontology.inducer.unstructured.stores.protocol import FactRecord, TopicRecord

    resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
    class_iris = {"会社": resolver.resolve("会社", "class"), "製品": resolver.resolve("製品", "class")}
    facts = [
        FactRecord(
            subject_value="三菱",
            subject_class="会社",
            predicate="製造する",
            object_value="冷蔵庫",
            object_class="製品",
            complement=None,
        )
    ]
    props = induce_object_properties(facts=facts, class_iris=class_iris, iri_resolver=resolver, namespace_language="ja")
    assert (None, RDFS.label, Literal("製造する", lang="ja")) in props

    concepts = induce_concepts(
        topics=[
            TopicRecord(topic_id="t1", value="設備保全", statement_count=2),
            TopicRecord(topic_id="t2", value="PLC更新", statement_count=2),
        ],
        iri_resolver=resolver,
        namespace_name="onto",
        namespace_language="ja",
    )
    assert (None, SKOS.prefLabel, Literal("設備保全", lang="ja")) in concepts
    assert (None, SKOS.prefLabel, Literal("PLC更新", datatype=XSD.string)) in concepts


def _client(text: str = "説明です。") -> MagicMock:
    client = MagicMock()
    client.generate.return_value = text
    return client


class TestClassDescriptionPrompts:
    def test_english_prompts_are_unchanged(self) -> None:
        client = _client("A description.")
        generate_descriptions({"Company": ["Acme"]}, client, namespace_language="en")
        assert client.generate.call_args.kwargs["system"] == _SYSTEM_PROMPT

        client = _client("A description.")
        generate_descriptions_with_context({"Company": [EntityContext(label="Acme")]}, client)
        assert client.generate.call_args.kwargs["system"] == _ENRICHED_SYSTEM_PROMPT

        client = _client("A description.")
        ctx = ClassSchemaContext(class_label="Company", outgoing=[SchemaEdge("OWNS", "Plant", 3)])
        generate_descriptions_with_schema({"Company": ctx}, client, namespace_language="en-US")
        assert client.generate.call_args.kwargs["system"] == _SCHEMA_SYSTEM_PROMPT

    @pytest.mark.parametrize(
        ("call", "base"),
        [
            (lambda c: generate_descriptions({"会社": ["三菱"]}, c, namespace_language="ja"), _SYSTEM_PROMPT),
            (
                lambda c: generate_descriptions_with_context(
                    {"会社": [EntityContext(label="三菱")]}, c, namespace_language="ja"
                ),
                _ENRICHED_SYSTEM_PROMPT,
            ),
            (
                lambda c: generate_descriptions_with_schema(
                    {"会社": ClassSchemaContext(class_label="会社", outgoing=[SchemaEdge("製造する", "製品", 3)])},
                    c,
                    namespace_language="ja",
                ),
                _SCHEMA_SYSTEM_PROMPT,
            ),
        ],
    )
    def test_japanese_namespace_adds_a_trusted_output_language_instruction(self, call, base) -> None:
        client = _client()
        call(client)
        system = client.generate.call_args.kwargs["system"]
        assert system.startswith(base)
        assert "Japanese (BCP 47 tag `ja`)" in system
        # The instruction stays out of the user prompt that carries the labels.
        assert "BCP 47" not in client.generate.call_args.args[0]

    def test_unsupported_tag_keeps_the_english_prompt(self) -> None:
        # Not reachable from a route (it canonicalizes the namespace language),
        # but the prompt degrades to English like the fixed-text templates do,
        # and the LLM call still runs instead of falling back.
        client = _client("A description.")
        out = generate_descriptions({"Company": ["Acme"]}, client, namespace_language="ja_JP")
        assert client.generate.call_args.kwargs["system"] == _SYSTEM_PROMPT
        assert out["Company"] == "A description."


class TestDeterministicFallbacks:
    def test_label_fallback_english_is_unchanged(self) -> None:
        out = generate_descriptions({"Company": []}, _client(), namespace_language="en")
        assert out["Company"] == "Class representing 0 entities including: "

    def test_label_fallback_japanese(self) -> None:
        client = MagicMock()
        client.generate.side_effect = RuntimeError("boom")
        out = generate_descriptions({"会社": ["日立", "三菱"]}, client, namespace_language="ja")
        assert out["会社"] == "2 件のエンティティを表すクラスです。例: 三菱、日立"

    def test_schema_fallback_english_is_unchanged(self) -> None:
        ctx = ClassSchemaContext(
            class_label="Company",
            outgoing=[SchemaEdge("OWNS", "Plant", 3)],
            incoming=[SchemaEdge("EMPLOYS", "Person", 2)],
        )
        client = MagicMock()
        client.generate.side_effect = RuntimeError("boom")
        out = generate_descriptions_with_schema({"Company": ctx}, client, namespace_language="en")
        assert out["Company"] == (
            "Class 'Company' with edges connected outgoing via OWNS and connected incoming via EMPLOYS."
        )
        empty = generate_descriptions_with_schema({"Company": ClassSchemaContext(class_label="Company")}, client)
        assert empty["Company"] == "Class 'Company' with no observed inter-class relationships."

    def test_schema_fallback_japanese(self) -> None:
        ctx = ClassSchemaContext(
            class_label="会社",
            outgoing=[SchemaEdge("製造する", "製品", 3), SchemaEdge("所有する", "工場", 1)],
            incoming=[SchemaEdge("勤務する", "人", 2)],
        )
        client = MagicMock()
        client.generate.side_effect = RuntimeError("boom")
        out = generate_descriptions_with_schema({"会社": ctx}, client, namespace_language="ja")
        assert out["会社"] == (
            "クラス「会社」は、出力側で 所有する、製造する により、入力側で 勤務する により他のクラスと接続しています。"
        )
        empty = generate_descriptions_with_schema(
            {"会社": ClassSchemaContext(class_label="会社")}, client, namespace_language="ja"
        )
        assert empty["会社"] == "クラス「会社」には、他のクラスとの関係が観測されていません。"

    def test_unreviewed_language_keeps_english_fallback(self) -> None:
        out = generate_descriptions({"회사": []}, _client(), namespace_language="ko")
        assert out["회사"] == "Class representing 0 entities including: "
