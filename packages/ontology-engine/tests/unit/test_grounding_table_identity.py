# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grounding must remain scoped to a data-source-qualified table identity."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable
from coa_ontology.inducer.services.pipeline import InductionPipeline, SourceConcept, _concept_identity
from coa_ontology.inducer.strategies.base import pascal_names_for, table_identity
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from rdflib import OWL, RDFS, Literal, URIRef

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INDUCTION_SMITHY = _REPO_ROOT / "models" / "src" / "main" / "smithy" / "ontology-induction.smithy"


class _EmbeddingPipeline(InductionPipeline):
    def embed_concepts(self, concepts, backend=None):
        for concept in concepts:
            concept.vector = [1.0]
        return concepts, "fixture-model"

    def _get_candidates(self, *_args, **_kwargs):
        return []


class _ScopedEmbeddingPipeline(_EmbeddingPipeline):
    def __init__(self):
        super().__init__(None, None, None)
        self.scoped_ontologies: dict[str, str | None] = {}
        self.structural_classes: dict[str, str] = {}

    def _get_candidates(
        self,
        concept,
        _model_id,
        _entity_type,
        *,
        ontology_id=None,
        **_kwargs,
    ):
        if ontology_id is None:
            return []
        identity = concept.source_table_identity
        self.scoped_ontologies[identity] = ontology_id
        return [
            {
                "entity_uri": f"http://example.test/foundation#{concept.column_name}",
                "ontology_id": ontology_id,
                "lexical_sim": 0.99,
                "source_table_identity": identity,
            }
        ]

    def _apply_structural_scores(self, candidates, class_uri, _structural_weight):
        identity = candidates[0]["source_table_identity"]
        self.structural_classes[identity] = class_uri
        for candidate in candidates:
            candidate["structural_sim"] = 1.0
            candidate["fused_score"] = candidate["lexical_sim"]


class _FailingGroundingPipeline(_EmbeddingPipeline):
    def match_concepts(self, *_args, **_kwargs):
        raise RuntimeError("forced grounding failure")


class _SelectedGroundingService:
    def __init__(self, **_kwargs):
        pass

    def ground_table(self, *, table_name, columns, **_kwargs):
        target = "FinancialAccount" if columns[0]["name"] == "balance" else "UserAccount"
        return ConceptMatch(
            source_column="",
            source_table=table_name,
            matched_class_uri=f"http://example.test/foundation#{target}",
            matched_ontology_id=f"fixture-{target.lower()}",
            similarity=0.99,
            match_type="exact",
        )

    @staticmethod
    def to_concept_match(result):
        return result


def _accounts(datasource_id: str, database: str, description: str, column_name: str) -> CatalogTable:
    return CatalogTable(
        id=f"{database}.accounts",
        name="accounts",
        fullyQualifiedName=f"{database}.public.accounts",
        description=description,
        datasourceId=datasource_id,
        sourceSchema="public",
        columns=[CatalogColumn(name=column_name, dataType="VARCHAR")],
    )


@pytest.mark.parametrize(
    ("source_table_identity", "table_fqn", "table_name", "expected"),
    [
        (
            "DS#sales::warehouse.public.accounts",
            "warehouse.public.accounts",
            "accounts",
            "DS#sales::warehouse.public.accounts",
        ),
        ("", "warehouse.public.accounts", "accounts", "warehouse.public.accounts"),
        ("", "", "accounts", "accounts"),
    ],
)
def test_concept_identity_uses_canonical_then_legacy_keys(
    source_table_identity,
    table_fqn,
    table_name,
    expected,
):
    concept = SourceConcept(
        table_fqn=table_fqn,
        table_name=table_name,
        column_name="",
        description="",
        data_type="",
        source_table_identity=source_table_identity,
    )

    assert _concept_identity(concept) == expected


@pytest.mark.parametrize("reverse_order", [False, True])
def test_same_named_tables_keep_their_own_grounding(reverse_order):
    finance = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    identity = _accounts("DS#identity", "identity", "identity user account", "username")
    tables = [identity, finance] if reverse_order else [finance, identity]
    pipeline = _EmbeddingPipeline(None, None, None)

    with patch(
        "coa_ontology.inducer.services.pipeline.GroundingService",
        _SelectedGroundingService,
    ):
        graph, novel_tables, matches, dropped = TableToOntologyStrategy().induce(
            tables=tables,
            ontology_uri_prefix="http://example.test/induced#",
            config={},
            pipeline=pipeline,
            grounding_ontology_ids=["fixture-foundation"],
            grounding_mode="STANDARD",
        )

    matches_by_identity = {match.source_table_identity: match for match in matches if not match.source_column}
    assert matches_by_identity[table_identity(finance)].matched_class_uri == (
        "http://example.test/foundation#FinancialAccount"
    )
    assert matches_by_identity[table_identity(identity)].matched_class_uri == (
        "http://example.test/foundation#UserAccount"
    )

    local_names = pascal_names_for(tables)
    finance_class = URIRef(f"http://example.test/induced#{local_names[table_identity(finance)]}")
    identity_class = URIRef(f"http://example.test/induced#{local_names[table_identity(identity)]}")
    assert (
        finance_class,
        RDFS.subClassOf,
        URIRef("http://example.test/foundation#FinancialAccount"),
    ) in graph
    assert (
        identity_class,
        RDFS.subClassOf,
        URIRef("http://example.test/foundation#UserAccount"),
    ) in graph
    assert not novel_tables
    assert not dropped


def test_legacy_match_without_identity_still_applies():
    table = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    grounding_class = URIRef("http://example.test/foundation#FinancialAccount")
    legacy_match = ConceptMatch(
        source_column="",
        source_table=table.name,
        matched_class_uri=str(grounding_class),
        matched_ontology_id="fixture-foundation",
        similarity=0.99,
        match_type="exact",
    )

    graph, novel_tables = TableToOntologyStrategy()._build_proposal_ontology(
        "http://example.test/induced#",
        [table],
        [legacy_match],
    )

    local_name = pascal_names_for([table])[table_identity(table)]
    induced_class = URIRef(f"http://example.test/induced#{local_name}")
    assert (induced_class, RDFS.subClassOf, grounding_class) in graph
    assert not novel_tables


def test_ambiguous_legacy_matches_do_not_cross_contaminate_same_named_tables():
    finance = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    identity = _accounts("DS#identity", "identity", "identity user account", "username")
    finance_grounding = URIRef("http://example.test/foundation#FinancialAccount")
    identity_grounding = URIRef("http://example.test/foundation#UserAccount")
    legacy_matches = [
        ConceptMatch(
            source_column="",
            source_table="accounts",
            matched_class_uri=str(grounding),
            matched_ontology_id="fixture-foundation",
            similarity=0.99,
            match_type="exact",
        )
        for grounding in (finance_grounding, identity_grounding)
    ]

    graph, novel_tables = TableToOntologyStrategy()._build_proposal_ontology(
        "http://example.test/induced#",
        [finance, identity],
        legacy_matches,
    )

    local_names = pascal_names_for([finance, identity])
    induced_classes = {
        URIRef(f"http://example.test/induced#{local_names[table_identity(table)]}") for table in (finance, identity)
    }
    for induced_class in induced_classes:
        assert (induced_class, RDFS.subClassOf, finance_grounding) not in graph
        assert (induced_class, RDFS.subClassOf, identity_grounding) not in graph
    assert novel_tables == {"accounts"}


def test_mixed_identity_and_legacy_column_matches_do_not_cross_contaminate():
    finance = _accounts("DS#finance", "finance", "finance account ledger", "id")
    identity = _accounts("DS#identity", "identity", "identity user account", "id")
    finance_property = URIRef("http://example.test/foundation#financeAccountId")
    legacy_property = URIRef("http://example.test/foundation#legacyAccountId")
    matches = [
        ConceptMatch(
            source_column="id",
            source_table="accounts",
            source_table_identity=table_identity(finance),
            matched_class_uri=str(finance_property),
            matched_ontology_id="fixture-foundation",
            similarity=0.99,
            match_type="exact",
        ),
        ConceptMatch(
            source_column="id",
            source_table="accounts",
            matched_class_uri=str(legacy_property),
            matched_ontology_id="fixture-foundation",
            similarity=0.98,
            match_type="exact",
        ),
    ]

    graph, _novel_tables = TableToOntologyStrategy()._build_proposal_ontology(
        "http://example.test/induced#",
        [finance, identity],
        matches,
    )

    local_names = pascal_names_for([finance, identity])
    finance_class = URIRef(f"http://example.test/induced#{local_names[table_identity(finance)]}")
    identity_class = URIRef(f"http://example.test/induced#{local_names[table_identity(identity)]}")
    [finance_minted] = [
        prop for prop in graph.subjects(RDFS.domain, finance_class) if (prop, RDFS.label, Literal("id")) in graph
    ]
    [identity_minted] = [
        prop for prop in graph.subjects(RDFS.domain, identity_class) if (prop, RDFS.label, Literal("id")) in graph
    ]

    assert (finance_minted, OWL.equivalentProperty, finance_property) in graph
    assert not list(graph.objects(identity_minted, OWL.equivalentProperty))
    assert (identity_minted, OWL.equivalentProperty, legacy_property) not in graph


def test_column_grounding_uses_parent_table_identity():
    finance = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    identity = _accounts("DS#identity", "identity", "identity user account", "username")
    pipeline = _ScopedEmbeddingPipeline()
    concepts, model_id = pipeline.embed_concepts(pipeline.extract_concepts([finance, identity]))

    with patch(
        "coa_ontology.inducer.services.pipeline.GroundingService",
        _SelectedGroundingService,
    ):
        matches = pipeline.match_concepts(
            concepts,
            confidence_threshold=0.80,
            model_id=model_id,
            scoring_strategy="structural_fusion",
            grounding_ontology_ids=["fixture-fallback"],
            grounding_mode="STANDARD",
            tables=[finance, identity],
        )

    finance_identity = table_identity(finance)
    identity_identity = table_identity(identity)
    assert pipeline.scoped_ontologies == {
        finance_identity: "fixture-financialaccount",
        identity_identity: "fixture-useraccount",
    }
    assert pipeline.structural_classes == {
        finance_identity: "http://example.test/foundation#FinancialAccount",
        identity_identity: "http://example.test/foundation#UserAccount",
    }
    column_matches = {match.source_table_identity: match for match in matches if match.source_column}
    assert column_matches[finance_identity].matched_ontology_id == "fixture-financialaccount"
    assert column_matches[identity_identity].matched_ontology_id == "fixture-useraccount"


def test_legacy_concepts_recover_unique_catalog_table_identity():
    table = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    pipeline = _EmbeddingPipeline(None, None, None)
    concepts = pipeline.extract_concepts([table])
    for concept in concepts:
        concept.source_table_identity = ""
        concept.vector = [1.0]

    with patch(
        "coa_ontology.inducer.services.pipeline.GroundingService",
        _SelectedGroundingService,
    ):
        matches = pipeline.match_concepts(
            concepts,
            confidence_threshold=0.80,
            model_id="fixture-model",
            grounding_ontology_ids=["fixture-foundation"],
            grounding_mode="STANDARD",
            tables=[table],
        )

    expected_identity = table_identity(table)
    assert {match.source_table_identity for match in matches} == {expected_identity}
    table_match = next(match for match in matches if not match.source_column)
    assert table_match.matched_class_uri == "http://example.test/foundation#FinancialAccount"


def test_all_novel_fallback_preserves_colliding_table_identities():
    finance = _accounts("DS#finance", "finance", "finance account ledger", "balance")
    identity = _accounts("DS#identity", "identity", "identity user account", "username")

    graph, _novel_tables, matches, dropped = TableToOntologyStrategy().induce(
        tables=[finance, identity],
        ontology_uri_prefix="http://example.test/induced#",
        config={},
        pipeline=_FailingGroundingPipeline(None, None, None),
        grounding_ontology_ids=["fixture-foundation"],
        grounding_mode="STANDARD",
    )

    expected_identities = {table_identity(finance), table_identity(identity)}
    table_matches = {match.source_table_identity for match in matches if not match.source_column}
    assert table_matches == expected_identities

    local_names = pascal_names_for([finance, identity])
    expected_classes = {
        URIRef(f"http://example.test/induced#{local_names[identity_key]}") for identity_key in expected_identities
    }
    assert set(graph.subjects(RDFS.label, Literal("accounts"))) == expected_classes
    assert not dropped


def test_smithy_concept_match_exposes_source_table_identity():
    model = _INDUCTION_SMITHY.read_text()
    shape = re.search(r"structure\s+ConceptMatch\s*\{(?P<body>.*?)\n\}", model, re.DOTALL)

    assert shape, "ConceptMatch structure not found in ontology-induction.smithy"
    assert re.search(r"\bsourceTableIdentity\s*:\s*String\b", shape.group("body")), (
        "ConceptMatch must expose the data-source-qualified table identity on the wire"
    )
