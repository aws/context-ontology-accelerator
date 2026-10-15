# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fixed induction inputs shared by the namespace-language tests.

How the English goldens were generated
--------------------------------------
``tests/fixtures/namespace_language/structured-en.ttl`` and
``unstructured-en.ttl`` are the output of these builders run, without any
language argument, against the BASE engine: the ``packages/ontology-engine/src``
tree of ``main`` at ``d65d9b397``, exported with
``git archive d65d9b397 packages/ontology-engine/src libs/common/src`` and put
first on ``PYTHONPATH``. The written files are the UTF-8 encoding of the returned
Turtle. An ``en`` namespace must still produce them byte for byte. (Regenerated
when main was merged into the feature branch: #1167 joins a foreign-key column's
description into the FK note, which changed the structured file; !922 keeps
non-ASCII letters in predicate keys and drops the class name from the
datatype-property observation comment, which changed the unstructured file.)

The structured fixture covers a primary key (``owl:hasKey``), NOT NULL and
PRIMARY KEY cardinality restrictions, sampled values on two columns, an FK, a
composite FK and a grounded (``exact``) table match. Its blank nodes are
serialized inline by rdflib; each class carries at most one cardinality
restriction, because rdflib orders two inline blank nodes on the same subject by
their random IDs. With that, the output is stable across runs and hash seeds.
The unstructured fixture includes a predicate with non-ASCII Latin letters
(``café owner``) to pin the predicate normalization, which since !922 keeps
those letters in every namespace language.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.inducer.unstructured.services.class_normalizer import NormalizationResult
from coa_ontology.inducer.unstructured.services.induction_rules import (
    induce_classes,
    induce_concepts,
    induce_datatype_properties,
    induce_object_properties,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import InMemoryIRIResolver
from coa_ontology.inducer.unstructured.services.serializer import serialize
from coa_ontology.inducer.unstructured.stores.protocol import ClassRecord, FactRecord, TopicRecord
from rdflib import Graph

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "namespace_language"
STRUCTURED_PREFIX = "http://example.org/a1-golden#"
UNSTRUCTURED_PREFIX = "http://example.org/a1-golden-docs/"
_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def structured_tables() -> list[CatalogTable]:
    """Three tables with generated metadata, an FK and a composite FK.

    Every description and synonym list carries a ``generatedFields`` proof, so
    the English golden also proves that proof alone never adds a tag in ``en``.
    """
    proof = ["description", "synonyms"]
    customers = CatalogTable(
        id="shop.customers",
        name="customers",
        fullyQualifiedName="shop.customers",
        description="People who buy products",
        synonyms=["client", "buyer"],
        generatedFields=proof,
        datasourceId="ds1",
        sourceSchema="shop",
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_id"])],
        columns=[
            CatalogColumn(
                name="customer_id",
                dataType="int",
                description="Customer identifier",
                synonyms=["client id"],
                constraint="PRIMARY_KEY",
                generatedFields=proof,
            ),
            CatalogColumn(
                name="segment",
                dataType="varchar",
                description="Market segment",
                distinctValues=["retail", "wholesale"],
                generatedFields=["description"],
            ),
        ],
    )
    regions = CatalogTable(
        id="shop.regions",
        name="regions",
        fullyQualifiedName="shop.regions",
        description="Sales regions",
        datasourceId="ds1",
        sourceSchema="shop",
        columns=[
            CatalogColumn(name="code", dataType="varchar", distinctValues=["EU", "JP", "US"]),
            CatalogColumn(name="seq", dataType="int"),
        ],
    )
    orders = CatalogTable(
        id="shop.orders",
        name="orders",
        fullyQualifiedName="shop.orders",
        description="Purchase orders",
        synonyms=["purchase"],
        generatedFields=proof,
        datasourceId="ds1",
        sourceSchema="shop",
        columns=[
            CatalogColumn(name="order_id", dataType="int", description="Order number"),
            CatalogColumn(name="status", dataType="varchar", description="Order status", constraint="NOT_NULL"),
            CatalogColumn(name="customer_id", dataType="int", description="Buyer reference"),
            CatalogColumn(name="region_code", dataType="varchar"),
            CatalogColumn(name="region_seq", dataType="int"),
        ],
        tableConstraints=[
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=["customers.customer_id"],
                relationshipType="DETERMINISTIC",
            ),
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["region_code", "region_seq"],
                referredColumns=["regions.code", "regions.seq"],
                relationshipType="DETERMINISTIC",
            ),
        ],
    )
    return [customers, regions, orders]


def structured_matches() -> list[ConceptMatch]:
    """A grounded table match (exact) and a novel one."""
    return [
        ConceptMatch(
            source_column="",
            source_table="customers",
            matched_class_uri="http://schema.org/Person",
            matched_ontology_id="http://schema.org/",
            similarity=0.97,
            match_type="exact",
        ),
        ConceptMatch(
            source_column="",
            source_table="orders",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        ),
    ]


def structured_turtle(**language_kwargs: Any) -> str:
    """Build the structured proposal ontology for :func:`structured_tables`."""
    graph, _novel = TableToOntologyStrategy()._build_proposal_ontology(
        STRUCTURED_PREFIX, structured_tables(), structured_matches(), **language_kwargs
    )
    return graph.serialize(format="turtle")


def unstructured_turtle(classes: list[tuple[str, str, str]] | None = None, **language_kwargs: Any) -> str:
    """Run induction rules 1, 3, 4 and 5 and the serializer on a fixed corpus.

    Args:
        classes: ``(sys_class_value, spaced_label, description)`` triples; the
            default is an English corpus.
        language_kwargs: Passed to every rule. The serializer takes none: its only
            literal is the caller-supplied ontology name, which stays untagged.
    """
    classes = classes or [
        ("FinancialInstrument", "Financial Instrument", "Tradable assets such as bonds."),
        ("Company", "Company", "Business organisations that issue instruments."),
    ]
    resolver = InMemoryIRIResolver(namespace="golden-ns", ontology_name="golden")
    normalizer_results = {
        spaced: NormalizationResult(class_iri=resolver.resolve(spaced, "class"), band="novel", similarity_score=0.0)
        for _value, spaced, _desc in classes
    }
    class_iris = {label: nr.class_iri for label, nr in normalizer_results.items()}
    (_v0, first, _d0), (_v1, second, _d1) = classes[0], classes[1]
    facts = [
        FactRecord(
            subject_value="Acme",
            subject_class=second,
            predicate="ISSUES",
            object_value="Bond A",
            object_class=first,
            complement=None,
        ),
        FactRecord(
            subject_value="Acme",
            subject_class=second,
            predicate="café owner",
            object_value="Bond A",
            object_class=first,
            complement=None,
        ),
        FactRecord(
            subject_value="Acme",
            subject_class=second,
            predicate="HEADQUARTERED IN",
            object_value=None,
            object_class=None,
            complement="Tokyo",
        ),
    ]
    graph = Graph()
    graph += induce_classes(
        class_distribution=[ClassRecord(value=value, count=5) for value, _s, _d in classes],
        descriptions={spaced: desc for _v, spaced, desc in classes},
        normalizer_results=normalizer_results,
        iri_resolver=resolver,
        **language_kwargs,
    )
    graph += induce_object_properties(facts=facts, class_iris=class_iris, iri_resolver=resolver, **language_kwargs)
    graph += induce_datatype_properties(facts=facts, class_iris=class_iris, iri_resolver=resolver, **language_kwargs)
    graph += induce_concepts(
        topics=[TopicRecord(topic_id="t1", value="Bond issuance", statement_count=3)],
        iri_resolver=resolver,
        namespace_name="golden",
        **language_kwargs,
    )
    turtle, _graph = serialize(
        graph=graph,
        namespace_name="golden",
        namespace_iri=UNSTRUCTURED_PREFIX,
        timestamp=_TIMESTAMP,
    )
    return turtle
