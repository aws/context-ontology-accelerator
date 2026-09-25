# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-source relationship materialisation (#1088).

An APPROVED cross-source foreign key must become an ``owl:ObjectProperty`` whose
range is the class of the target table in the OTHER datasource — resolved via
the relationship's ``targetDatasourceId`` even when the bare table name is
ambiguous across the unioned sources (two ``customers`` tables). A PENDING/
REJECTED one, or one whose disambiguator is missing, must NOT materialise.

Deterministic: drives ``_build_proposal_ontology`` directly (no Bedrock, no
induction job).
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.base import pascal_names_for, table_identity
from rdflib import OWL, RDF, RDFS, Namespace

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"


@pytest.fixture
def strategy():
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    return TableToOntologyStrategy()


def _tables(*, target_datasource_id: str | None, review_status: str):
    """Two same-named ``customers`` tables in different sources + an ``orders``
    table in DS#1 whose customer_id FK is an inferred cross-source link to DS#2."""
    customers_ds1 = CatalogTable(
        id="c1",
        name="customers",
        fullyQualifiedName="salesdb.customers",
        datasourceId="DS#1",
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )
    customers_ds2 = CatalogTable(
        id="c2",
        name="customers",
        fullyQualifiedName="crmdb.customers",
        datasourceId="DS#2",
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )
    orders = CatalogTable(
        id="o",
        name="orders",
        fullyQualifiedName="salesdb.orders",
        datasourceId="DS#1",
        columns=[CatalogColumn(name="order_id", dataType="INT"), CatalogColumn(name="customer_id", dataType="INT")],
        tableConstraints=[
            CatalogConstraint(constraintType="PRIMARY_KEY", columns=["order_id"]),
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=["customers.id"],
                relationshipType="AI_INFERRED",
                reviewStatus=review_status,
                targetDatasourceId=target_datasource_id,
            ),
        ],
    )
    return [customers_ds1, customers_ds2, orders]


def _customers_class_in(tables, ds_bare_id: str) -> Namespace:
    """The class IRI of the ``customers`` table in datasource ``ds_bare_id`` under the run's naming."""
    ns = Namespace(PREFIX)
    pascal = pascal_names_for(tables)
    t = next(t for t in tables if t.name == "customers" and (t.datasourceId or "").removeprefix("DS#") == ds_bare_id)
    return ns[pascal[table_identity(t)]]


def _ds2_customers_class(tables) -> Namespace:
    """The class IRI of the DS#2 customers table."""
    return _customers_class_in(tables, "2")


class TestCrossSourceMaterialisation:
    def test_approved_cross_source_fk_emits_object_property_to_correct_source(self, strategy):
        tables = _tables(target_datasource_id="DS#2", review_status="APPROVED")
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        ns = Namespace(PREFIX)

        # Materialised as an object property...
        assert (ns.orders_customerId, RDF.type, OWL.ObjectProperty) in onto
        # ...pointing at the DS#2 customers class, not DS#1's same-named table.
        assert onto.value(ns.orders_customerId, RDFS.range) == _ds2_customers_class(tables)

    def test_prefix_mismatch_between_fk_and_table_still_resolves(self, strategy):
        # The REAL production shape (MR !1215 review): the FK's targetDatasourceId
        # is written by the sources pipeline as a DS#-prefixed id, while
        # CatalogTable.datasourceId carries the bare uuid passed to /induce. The two
        # must compare equal or the lookup silently falls through to the ambiguous
        # bare-name path and no edge is emitted.
        tables = _tables(target_datasource_id="DS#2", review_status="APPROVED")
        for t in tables:
            t.datasourceId = t.datasourceId.removeprefix("DS#")  # tables: bare; FK: DS#2
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        ns = Namespace(PREFIX)

        assert (ns.orders_customerId, RDF.type, OWL.ObjectProperty) in onto
        assert onto.value(ns.orders_customerId, RDFS.range) == _ds2_customers_class(tables)

    def test_missing_target_datasource_id_resolves_within_own_source(self, strategy):
        # Without a cross-source disambiguator, a bare FK target resolves to the
        # same-named table in the referrer's OWN datasource (the same-datasource-
        # first probe in resolve_fk_target_identity) — i.e. DS#1's customers, NOT
        # the DS#2 one. This proves targetDatasourceId is what redirects the edge
        # across the source boundary; absent it, the relationship stays local.
        tables = _tables(target_datasource_id=None, review_status="APPROVED")
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        ns = Namespace(PREFIX)

        assert (ns.orders_customerId, RDF.type, OWL.ObjectProperty) in onto
        assert onto.value(ns.orders_customerId, RDFS.range) == _customers_class_in(tables, "1")
        assert onto.value(ns.orders_customerId, RDFS.range) != _ds2_customers_class(tables)

    def test_pending_cross_source_fk_is_withheld(self, strategy):
        # Resolvable, but not approved -> the gate withholds the edge.
        tables = _tables(target_datasource_id="DS#2", review_status="PENDING_REVIEW")
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        ns = Namespace(PREFIX)

        assert (ns.orders_customerId, RDF.type, OWL.ObjectProperty) not in onto
        assert (ns.orders_customerId, RDF.type, OWL.DatatypeProperty) in onto
