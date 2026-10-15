# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ontology-inducer pipeline logic."""

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.services.pipeline import (
    InductionPipeline,
    SourceConcept,
    _cosine_similarity,
    _label_from_uri,
    _to_camel,
    _to_pascal,
)
from rdflib import OWL, RDF, RDFS, XSD, Namespace, URIRef
from rdflib.namespace import PROV, SKOS

# ── Pure helpers ──


@pytest.mark.parametrize(
    "input_,expected",
    [
        ("order_item", "OrderItem"),
        ("product", "Product"),
        ("line-item", "LineItem"),
        ("a_b_c", "ABC"),
    ],
)
def test_to_pascal(input_, expected):
    assert _to_pascal(input_) == expected


@pytest.mark.parametrize(
    "input_,expected",
    [
        ("order_date", "orderDate"),
        ("product", "product"),
        ("total_amount", "totalAmount"),
    ],
)
def test_to_camel(input_, expected):
    assert _to_camel(input_) == expected


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("http://schema.org/dateCreated", "date created"),
        ("http://schema.org/Product", "product"),
        ("http://purl.org/dc/terms/title", "title"),
        ("http://ex.org/foo#barBaz", "bar baz"),
    ],
)
def test_label_from_uri(uri, expected):
    assert _label_from_uri(uri) == expected


def test_cosine_similarity_identical():
    v = [1.0, 2.0, 3.0]
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal():
    assert abs(_cosine_similarity([1, 0], [0, 1])) < 1e-6


def test_cosine_similarity_zero_vector():
    assert _cosine_similarity([0, 0], [1, 1]) == 0.0


# ── extract_concepts ──


def _make_table(name="orders", columns=None, constraints=None):
    cols = columns or [
        CatalogColumn(name="order_id", dataType="INT", constraint="PRIMARY_KEY"),
        CatalogColumn(name="total_amount", dataType="DECIMAL", description="Order total"),
    ]
    return CatalogTable(
        id=f"test.{name}",
        name=name,
        fullyQualifiedName=f"test_db.public.{name}",
        description=f"The {name} table",
        columns=cols,
        tableConstraints=constraints,
    )


def test_extract_concepts():
    pipeline = InductionPipeline(None, None, None)
    table = _make_table()
    concepts = pipeline.extract_concepts([table])

    # 1 table-level + 2 column-level
    assert len(concepts) == 3

    table_concept = [c for c in concepts if c.is_table_level][0]
    assert table_concept.table_name == "orders"
    assert "orders" in table_concept.embedding_text

    col_concepts = [c for c in concepts if not c.is_table_level]
    assert col_concepts[0].column_name == "order_id"
    assert "INT" in col_concepts[0].embedding_text
    assert "Order total" in col_concepts[1].embedding_text


# ── _classify ──


def test_classify():
    pipeline = InductionPipeline(None, None, None)
    assert pipeline._classify(0.96, 0.80) == "exact"
    assert pipeline._classify(0.85, 0.80) == "high_confidence"
    assert pipeline._classify(0.60, 0.80) == "ambiguous"
    assert pipeline._classify(0.30, 0.80) == "novel"


# ── build_ontology ──


def _make_matches(table):
    return [
        ConceptMatch(
            source_table=table.name,
            source_column="",
            matched_class_uri="http://schema.org/Order",
            matched_ontology_id="https://schema.org/",
            similarity=0.92,
            match_type="high_confidence",
            scoring_strategy="lexical",
        ),
        ConceptMatch(
            source_table=table.name,
            source_column="order_id",
            matched_class_uri="http://schema.org/orderNumber",
            matched_ontology_id="https://schema.org/",
            similarity=0.88,
            match_type="high_confidence",
            scoring_strategy="lexical",
        ),
        ConceptMatch(
            source_table=table.name,
            source_column="total_amount",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
            scoring_strategy="lexical",
        ),
    ]


def test_build_ontology_structure():
    pipeline = InductionPipeline(None, None, None)
    table = _make_table()
    concepts = pipeline.extract_concepts([table])
    matches = _make_matches(table)

    g = pipeline.build_ontology("http://ex.org/induced#", [table], concepts, matches)

    # Ontology declaration
    ont = URIRef("http://ex.org/induced")
    assert (ont, RDF.type, OWL.Ontology) in g

    # Induced class
    orders_cls = URIRef("http://ex.org/induced#Orders")
    assert (orders_cls, RDF.type, OWL.Class) in g
    assert (orders_cls, RDFS.subClassOf, URIRef("http://schema.org/Order")) in g
    assert (orders_cls, SKOS.closeMatch, URIRef("http://schema.org/Order")) in g

    # Grounding must NOT emit owl:imports — a live import of the (often
    # non-dereferenceable) foundational URI makes Ontop/OWLAPI network-fetch it
    # at VKG load and fail every query. The grounding is fully carried by
    # rdfs:subClassOf + skos:* above, so no owl:imports triple should exist.
    assert list(g.triples((None, OWL.imports, None))) == []

    # Primary key → owl:hasKey + cardinality 1
    order_id_prop = URIRef("http://ex.org/induced#orders_orderId")
    assert table.columns[0].constraint == "PRIMARY_KEY"
    assert (orders_cls, OWL.hasKey, order_id_prop) in g

    # Novel column still gets a property
    total_prop = URIRef("http://ex.org/induced#orders_totalAmount")
    assert (total_prop, RDF.type, OWL.DatatypeProperty) in g
    assert (total_prop, RDFS.range, XSD.decimal) in g


def test_production_strategy_grounds_via_subclass_and_skos_not_groundedto():
    """The PRODUCTION path (TableToOntologyStrategy._build_proposal_ontology) must
    express a high_confidence grounding via rdfs:subClassOf + skos:closeMatch +
    matchConfidence, and must NOT emit the custom coa:groundedTo or owl:imports.

    coa:groundedTo was retired: it was redundant with the subClassOf/skos axioms
    and misaligned with the RIGOR path (which grounds via subClassOf only). The
    owl:imports omission remains a VKG-breaking-bug regression (Ontop #337) — a
    live import of the non-dereferenceable foundational URI fails every query.
    """
    from coa_ontology.inducer.strategies.base import SCL
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    ns_ind = Namespace("http://ex.org/induced#")
    match_confidence = ns_ind["matchConfidence"]

    table = _make_table()
    matches = _make_matches(table)  # 'orders' grounds high_confidence → Order

    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", [table], matches)

    orders_cls = URIRef("http://ex.org/induced#Orders")
    order_fc = URIRef("http://schema.org/Order")
    # Grounding linkage IS carried by subClassOf + skos:closeMatch...
    assert (orders_cls, RDFS.subClassOf, order_fc) in g
    assert (orders_cls, SKOS.closeMatch, order_fc) in g
    # ...with the similarity recorded via the matchConfidence annotation...
    assert list(g.triples((orders_cls, match_confidence, None))) != []
    # ...but the retired coa:groundedTo is NOT emitted...
    assert (orders_cls, SCL.groundedTo, order_fc) not in g
    assert list(g.triples((None, SCL.groundedTo, None))) == []
    # ...and NO owl:imports (the VKG-breaking triple).
    assert list(g.triples((None, OWL.imports, None))) == []


def test_production_strategy_exact_match_grounds_via_skos_exactmatch_not_groundedto():
    """The 'exact' match branch (skos:exactMatch, similarity >= 0.95) must ground
    via rdfs:subClassOf + skos:exactMatch (NOT closeMatch), must NOT emit the
    retired coa:groundedTo, and must emit no owl:imports. This is the branch that
    historically emitted the import, so it needs its own regression —
    high_confidence/closeMatch is covered above."""
    from coa_ontology.inducer.strategies.base import SCL
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    table = _make_table()
    matches = [
        ConceptMatch(
            source_table=table.name,
            source_column="",
            matched_class_uri="http://schema.org/Order",
            matched_ontology_id="https://schema.org/",
            similarity=0.97,
            match_type="exact",
            scoring_strategy="lexical",
        ),
    ]

    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", [table], matches)

    orders_cls = URIRef("http://ex.org/induced#Orders")
    order_fc = URIRef("http://schema.org/Order")
    # exact match → skos:exactMatch (NOT closeMatch) + subClassOf...
    assert (orders_cls, RDFS.subClassOf, order_fc) in g
    assert (orders_cls, SKOS.exactMatch, order_fc) in g
    assert (orders_cls, SKOS.closeMatch, order_fc) not in g
    # ...the retired coa:groundedTo is NOT emitted...
    assert (orders_cls, SCL.groundedTo, order_fc) not in g
    assert list(g.triples((None, SCL.groundedTo, None))) == []
    # ...but still NO owl:imports.
    assert list(g.triples((None, OWL.imports, None))) == []


def test_production_strategy_does_not_emit_prov_lineage():
    """Induction must NOT emit prov:wasDerivedFrom source lineage (removed).
    Neither classes nor properties carry any prov:wasDerivedFrom triple."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    table = _make_table()  # 'orders' with columns order_id, total_amount
    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", [table], [])

    assert not list(g.triples((None, PROV.wasDerivedFrom, None)))


def test_production_strategy_grounded_table_emits_grounding_axioms():
    """A GROUNDED table carries its grounding axioms (subClassOf + skos:closeMatch)
    and no prov:wasDerivedFrom lineage. 'orders' grounds high_confidence →
    schema.org/Order via _make_matches."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    table = _make_table()
    matches = _make_matches(table)  # 'orders' grounds high_confidence → Order

    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", [table], matches)

    orders_cls = URIRef("http://ex.org/induced#Orders")
    order_fc = URIRef("http://schema.org/Order")
    # Grounding is carried by subClassOf + skos:closeMatch.
    assert (orders_cls, RDFS.subClassOf, order_fc) in g
    assert (orders_cls, SKOS.closeMatch, order_fc) in g

    # No source lineage is emitted.
    assert not list(g.triples((None, PROV.wasDerivedFrom, None)))


@pytest.mark.parametrize("match_type", ["exact", "high_confidence", "ambiguous"])
def test_production_strategy_self_match_emits_no_reflexive_grounding_axioms(match_type):
    """Regression: if a class's grounding match resolves to its own IRI
    (``matched_class_uri`` equals the class's own IRI), the builder must emit NO
    reflexive grounding axiom — ``X rdfs:subClassOf X`` is a self-loop the Tier-1
    TaxonomyCycleValidator reports as TAXONOMY_CYCLE, and a self
    ``skos:exactMatch``/``closeMatch``/``relatedMatch`` is semantically vacuous.
    It must also record no ``matchConfidence`` for the vacuous self-match. The
    class itself is still declared. The genuine cross-namespace match (guard does
    not over-fire) is covered by
    test_production_strategy_grounded_table_emits_grounding_axioms and the
    exact-match test above."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    table = _make_table()
    orders_cls = URIRef("http://ex.org/induced#Orders")
    match_confidence = Namespace("http://ex.org/induced#")["matchConfidence"]

    matches = [
        ConceptMatch(
            source_table=table.name,
            source_column="",
            matched_class_uri=str(orders_cls),  # self-match: the class's own IRI
            matched_ontology_id="http://ex.org/induced#",
            similarity=0.99,
            match_type=match_type,
            scoring_strategy="lexical",
        ),
    ]

    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", [table], matches)

    # The class is still declared...
    assert (orders_cls, RDF.type, OWL.Class) in g
    # ...but carries no reflexive grounding axioms.
    assert (orders_cls, RDFS.subClassOf, orders_cls) not in g
    assert (orders_cls, SKOS.exactMatch, orders_cls) not in g
    assert (orders_cls, SKOS.closeMatch, orders_cls) not in g
    assert (orders_cls, SKOS.relatedMatch, orders_cls) not in g
    # ...and no vacuous self matchConfidence.
    assert list(g.triples((orders_cls, match_confidence, None))) == []


def test_build_ontology_fk():
    """Foreign key columns become ObjectProperties."""
    pipeline = InductionPipeline(None, None, None)
    cols = [
        CatalogColumn(name="id", dataType="INT", constraint="PRIMARY_KEY"),
        CatalogColumn(name="customer_id", dataType="INT"),
    ]
    constraints = [
        CatalogConstraint(
            constraintType="FOREIGN_KEY",
            columns=["customer_id"],
            referredColumns=["test_db.public.customers.id"],
        ),
    ]
    table = _make_table("orders", cols, constraints)
    matches = [
        ConceptMatch(
            source_table="orders",
            source_column="",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
            scoring_strategy="lexical",
        ),
        ConceptMatch(
            source_table="orders",
            source_column="id",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
            scoring_strategy="lexical",
        ),
        ConceptMatch(
            source_table="orders",
            source_column="customer_id",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
            scoring_strategy="lexical",
        ),
    ]

    g = pipeline.build_ontology("http://ex.org/induced#", [table], pipeline.extract_concepts([table]), matches)

    fk_prop = URIRef("http://ex.org/induced#orders_customerId")
    assert (fk_prop, RDF.type, OWL.ObjectProperty) in g
    assert (fk_prop, RDFS.range, URIRef("http://ex.org/induced#Customers")) in g


def test_build_ontology_self_match_emits_no_reflexive_axioms():
    """The retired InductionPipeline.build_ontology reference path must also skip
    a self-match (matched_class_uri == the table's own class IRI): no reflexive
    rdfs:subClassOf / skos:exactMatch / closeMatch. Guards the latent copy of the
    induction self-loop bug even though this path is not mounted in production."""
    pipeline = InductionPipeline(None, None, None)
    table = _make_table()  # 'orders' → http://ex.org/induced#Orders
    orders_cls = URIRef("http://ex.org/induced#Orders")
    matches = [
        ConceptMatch(
            source_table=table.name,
            source_column="",
            matched_class_uri=str(orders_cls),  # self-match
            matched_ontology_id="http://ex.org/induced#",
            similarity=0.99,
            match_type="exact",
            scoring_strategy="lexical",
        ),
    ]

    g = pipeline.build_ontology("http://ex.org/induced#", [table], pipeline.extract_concepts([table]), matches)

    assert (orders_cls, RDF.type, OWL.Class) in g
    assert (orders_cls, RDFS.subClassOf, orders_cls) not in g
    assert (orders_cls, SKOS.exactMatch, orders_cls) not in g
    assert (orders_cls, SKOS.closeMatch, orders_cls) not in g


def _haskey_members(g) -> list[URIRef]:
    """Every property URI named in any ``owl:hasKey ( … )`` RDF collection."""
    from rdflib.collection import Collection

    members: list[URIRef] = []
    for _cls, key_bnode in g.subject_objects(OWL.hasKey):
        members.extend(Collection(g, key_bnode))
    return members


def test_pk_sharing_child_declares_its_haskey_property():
    """BUG 1 regression — the ACTIVE builder (TableToOntologyStrategy).

    For a CONFIRMED PK-sharing child, the child's FK column IS its PK column,
    so it appears in the child's ``owl:hasKey`` list. The builder must still
    declare that column as an owl:ObjectProperty/DatatypeProperty; otherwise
    ``owl:hasKey`` references an undeclared property → OWL-DL structural
    inconsistency (fails Tier-1/OoPS/reasoner).

    Scenario: parent ``claim_amount`` with a single-column PK, and TWO children
    (``loss_payment``, ``recovery``) whose single-column PK is also a
    single-column FK to the parent PK → detect_pk_sharing_subtypes marks both
    as CONFIRMED (≥ MULTI_CHILD_THRESHOLD children per parent).
    """
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    parent = _make_table(
        "claim_amount",
        columns=[
            CatalogColumn(name="claim_amount_id", dataType="INT", constraint="PRIMARY_KEY"),
            CatalogColumn(name="amount", dataType="DECIMAL"),
        ],
        constraints=[
            CatalogConstraint(constraintType="PRIMARY_KEY", columns=["claim_amount_id"]),
        ],
    )

    def _child(name):
        return _make_table(
            name,
            columns=[
                CatalogColumn(name="claim_amount_id", dataType="INT", constraint="PRIMARY_KEY"),
                CatalogColumn(name="note", dataType="VARCHAR"),
            ],
            constraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["claim_amount_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["claim_amount_id"],
                    referredColumns=["test_db.public.claim_amount.claim_amount_id"],
                    relationshipType="PK_SHARING",
                ),
            ],
        )

    loss_payment = _child("loss_payment")
    recovery = _child("recovery")
    tables = [parent, loss_payment, recovery]

    # All novel (no grounding matches) so we exercise the PK-sharing path only.
    matches = [
        ConceptMatch(
            source_table=t.name,
            source_column="",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        )
        for t in tables
    ]

    # Sanity: the fixture really is a CONFIRMED PK-sharing pair (≥2 children).
    from coa_ontology.inducer.services.subtype_detection import detect_pk_sharing_subtypes

    confirmed, _suggested = detect_pk_sharing_subtypes(tables)
    assert ("loss_payment", "claim_amount") in confirmed
    assert ("recovery", "claim_amount") in confirmed

    g, _novel = TableToOntologyStrategy()._build_proposal_ontology("http://ex.org/induced#", tables, matches)

    # The child IS a subclass of the parent (PK-sharing inheritance).
    child_cls = URIRef("http://ex.org/induced#LossPayment")
    parent_cls = URIRef("http://ex.org/induced#ClaimAmount")
    assert (child_cls, RDFS.subClassOf, parent_cls) in g

    # Core assertion: EVERY property named in ANY owl:hasKey list is declared as
    # an owl:ObjectProperty or owl:DatatypeProperty (no undeclared-property-in-hasKey).
    key_members = _haskey_members(g)
    assert key_members, "expected at least one owl:hasKey member across the graph"
    for prop in key_members:
        declared = (prop, RDF.type, OWL.ObjectProperty) in g or (prop, RDF.type, OWL.DatatypeProperty) in g
        assert declared, f"owl:hasKey references undeclared property {prop}"

    # Specifically, the PK-sharing child's key column (its FK/PK) is declared.
    child_key = URIRef("http://ex.org/induced#lossPayment_claimAmountId")
    assert child_key in key_members
    assert (child_key, RDF.type, OWL.ObjectProperty) in g


def test_match_concepts_non_transport_failure_logs_and_falls_back_to_all_novel(caplog):
    """Narrowed contract: a non-transport failure (a programming bug, e.g.
    RuntimeError) still falls back to all-novel with the real exception logged —
    it must not crash the job. Genuine search/transport failures now RE-RAISE
    instead (see the sibling test below and test_column_match_correctness.py);
    only non-transport bugs are tolerated by the all-novel fallback.

    Regression for the silent-degrade bug: the bare except used to classify every
    table 'novel' with NO signal, making a grounding/recall failure look identical
    to a legitimately-novel schema.
    """
    import logging

    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    table = _make_table()

    class _FakePipeline:
        def extract_concepts(self, tables):
            # one table-level + per-column concepts (use the real extractor shape)
            return InductionPipeline(None, None, None).extract_concepts(tables)

        def embed_concepts(self, concepts, backend=None):
            return concepts, "model-x"

        def match_concepts(self, *a, **k):
            raise RuntimeError("a programming bug, not a transport fault")

    with caplog.at_level(logging.ERROR):
        graph, novel, matches, dropped = TableToOntologyStrategy().induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/induced#",
            config={},
            pipeline=_FakePipeline(),
            grounding_ontology_ids=["urn:ontology:foundational"],
            grounding_mode="ENHANCED",
        )

    # Did NOT crash; every concept fell back to novel.
    assert all(m.match_type == "novel" for m in matches)
    assert "orders" in novel
    # The real exception was logged (diagnosable), not swallowed. Assert on the
    # exc_info the handler attached rather than the message wording: the try now
    # spans extract → embed → match, so the message names the batch, not the stage.
    fallback_logs = [r for r in caplog.records if "falling back to all-novel" in r.message]
    assert fallback_logs
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in fallback_logs)


def test_match_concepts_transport_failure_reraises_not_all_novel():
    """New contract: a genuine AOSS transport failure must RE-RAISE so
    _run_induction marks the job FAILED — NOT be swallowed into an all-novel
    matches list (the silent-degrade bug this whole fix targets). This flips the
    prior behavior for the transport-error case."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
    from opensearchpy.exceptions import TransportError

    table = _make_table()

    class _RaisingPipeline:
        def extract_concepts(self, tables):
            return InductionPipeline(None, None, None).extract_concepts(tables)

        def embed_concepts(self, concepts, backend=None):
            return concepts, "model-x"

        def match_concepts(self, *a, **k):
            raise TransportError(429, "throttle")

    with pytest.raises(TransportError):
        TableToOntologyStrategy().induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/induced#",
            config={},
            pipeline=_RaisingPipeline(),
            grounding_ontology_ids=["urn:ontology:foundational"],
            grounding_mode="ENHANCED",
        )


def test_match_concepts_serialization_error_reraises_not_all_novel():
    """Same contract for opensearch-py's SerializationError: a 2xx-with-
    unparseable/truncated body means recall is incomplete, yet SerializationError
    does NOT subclass TransportError (its MRO is SerializationError →
    OpenSearchException → Exception). Before widening the handler to the base
    OpenSearchException it slipped past the transport guard into the tolerant
    all-novel fallback — re-introducing the exact silent-novel corruption. It must
    RE-RAISE so _run_induction marks the job FAILED, not degrade to all-novel."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
    from opensearchpy.exceptions import SerializationError

    table = _make_table()

    class _RaisingPipeline:
        def extract_concepts(self, tables):
            return InductionPipeline(None, None, None).extract_concepts(tables)

        def embed_concepts(self, concepts, backend=None):
            return concepts, "model-x"

        def match_concepts(self, *a, **k):
            raise SerializationError("truncated body")

    with pytest.raises(SerializationError):
        TableToOntologyStrategy().induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/induced#",
            config={},
            pipeline=_RaisingPipeline(),
            grounding_ontology_ids=["urn:ontology:foundational"],
            grounding_mode="ENHANCED",
        )


# ── Column-level candidate scoping (grounding opt-out) ──


def _concept(vector=None):
    return SourceConcept(
        table_fqn="db.orders",
        table_name="orders",
        column_name="total",
        description="order total",
        data_type="int",
        vector=vector if vector is not None else [0.1] * 8,
    )


def test_get_candidates_empty_scope_returns_nothing_without_search():
    """An explicit empty ``grounding_ontology_ids`` (no singular ontology_id)
    means the resolved grounding pool is empty → return no candidates, and do
    NOT fall back to a namespace-wide search that would match a
    loaded-but-unselected ontology (the grounding opt-out bug)."""
    oc = MagicMock()
    pipeline = InductionPipeline(None, None, oc)
    result = pipeline._get_candidates(_concept(), model_id="titan", entity_type="property", grounding_ontology_ids=[])
    assert result == []
    oc.search_embeddings.assert_not_called()


def test_get_candidates_none_scope_treated_as_empty_returns_nothing():
    """``None`` scope is an empty grounding pool, identical to ``[]``: return no
    candidates and do NOT fall back to a namespace-wide search. There is no
    unscoped/global recall."""
    oc = MagicMock()
    oc.search_embeddings.return_value = []
    pipeline = InductionPipeline(None, None, oc)
    result = pipeline._get_candidates(_concept(), model_id="titan", entity_type="property", grounding_ontology_ids=None)
    assert result == []
    oc.search_embeddings.assert_not_called()


# ── Sub-stage timing ──


def test_match_concepts_records_column_match_stage_duration():
    """Guards the #784 Phase 0 timer: the serial column-matching second pass is
    wrapped so its wall-clock is recorded as the ``ColumnMatch`` stage on the
    per-job CostTracker (auto-emitted downstream as ColumnMatchDurationMs).
    Without the timer, the stage never appears in ``stage_durations_ms``."""
    from unittest.mock import patch

    from coa_common.bedrock_metrics import CostTracker

    # Arrange: one table-level + one column-level concept; grounding stubbed so
    # the first pass yields no scoped ontology (column pass takes the fallback,
    # empty-pool path — hermetic, no real search).
    table_concept = SourceConcept(
        table_fqn="db.orders",
        table_name="orders",
        column_name="",
        description="orders table",
        data_type="",
        is_table_level=True,
    )
    column_concept = _concept()
    oc = MagicMock()
    oc.search_embeddings.return_value = []
    pipeline = InductionPipeline(None, None, oc)
    tracker = CostTracker()

    benign_match = ConceptMatch(
        source_column="",
        source_table="orders",
        matched_class_uri=None,
        matched_ontology_id=None,
        similarity=None,
        match_type="novel",
    )

    with patch("coa_ontology.inducer.services.pipeline.GroundingService") as mock_grounding_cls:
        grounding_svc = mock_grounding_cls.return_value
        grounding_svc.to_concept_match.return_value = benign_match

        # Act
        pipeline.match_concepts(
            [table_concept, column_concept],
            confidence_threshold=0.8,
            model_id="titan",
            grounding_ontology_ids=[],
            cost_tracker=tracker,
        )

    # Assert: the second-pass timer recorded the ColumnMatch stage.
    assert "ColumnMatch" in tracker.stage_durations_ms
    assert tracker.stage_durations_ms["ColumnMatch"] >= 0.0


# ── Grounding rerank model (GH #236) ──


def _run_match_with_mock_grounding(pipeline):
    from unittest.mock import patch

    table_concept = SourceConcept(
        table_fqn="db.orders",
        table_name="orders",
        column_name="",
        description="orders table",
        data_type="",
        is_table_level=True,
    )
    benign_match = ConceptMatch(
        source_column="",
        source_table="orders",
        matched_class_uri=None,
        matched_ontology_id=None,
        similarity=None,
        match_type="novel",
    )
    with patch("coa_ontology.inducer.services.pipeline.GroundingService") as mock_grounding_cls:
        mock_grounding_cls.return_value.to_concept_match.return_value = benign_match
        pipeline.match_concepts([table_concept], confidence_threshold=0.8, model_id="titan", grounding_ontology_ids=[])
    mock_grounding_cls.assert_called_once()
    return mock_grounding_cls.call_args.kwargs


def test_structured_grounding_uses_configured_model_and_region():
    """The structured rerank must use the pipeline's region + configured
    grounding model (here a GROUNDING_LLM_MODEL_ID override), not
    GroundingService's us-east-1 / us. defaults (GH #236)."""
    oc = MagicMock()
    oc.search_embeddings.return_value = []
    pipeline = InductionPipeline(
        None,
        None,
        oc,
        llm_region="ap-northeast-1",
        grounding_llm_model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    kwargs = _run_match_with_mock_grounding(pipeline)
    assert kwargs["llm_region"] == "ap-northeast-1"
    assert kwargs["llm_model_id"] == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_build_pipeline_threads_grounding_config():
    """induce._build_pipeline (wrapped by main.py's patched builder) must carry
    llm_region and grounding_llm_model_id from app config onto the pipeline."""
    from coa_ontology.inducer.routers import induce

    build = getattr(induce, "_orig_build_pipeline", induce._build_pipeline)
    base = {
        "data_catalog_url": "http://dc",
        "embedding_generator_url": "http://eg",
        "ontology_catalog_url": "http://oc",
    }
    jp_haiku = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    pipeline = build({**base, "llm_region": "ap-northeast-1", "grounding_llm_model_id": jp_haiku})
    assert pipeline.llm_region == "ap-northeast-1"
    assert pipeline.grounding_llm_model_id == jp_haiku

    # Absent/empty grounding model id → the Haiku rerank fallback.
    defaulted = build({**base, "grounding_llm_model_id": ""})
    assert defaulted.llm_region == "us-east-1"
    assert defaulted.grounding_llm_model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
