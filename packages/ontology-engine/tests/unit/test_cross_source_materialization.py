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
from coa_ontology.inducer.strategies.base import (
    RR,
    pascal_names_for,
    subject_template_names,
    table_identity,
)
from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy
from rdflib import OWL, RDF, RDFS, XSD, Graph, Namespace, URIRef
from rdflib.compare import to_canonical_graph

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


def _tables_targeting_source_a() -> list[CatalogTable]:
    """Source B orders explicitly target source A's same-named customers table."""
    customers_a = CatalogTable(
        id="customers-a",
        name="customers",
        fullyQualifiedName="public.customers",
        datasourceId="source-a",
        sourceSchema="public",
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )
    customers_b = CatalogTable(
        id="customers-b",
        name="customers",
        fullyQualifiedName="public.customers",
        datasourceId="source-b",
        sourceSchema="public",
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )
    orders_b = CatalogTable(
        id="orders-b",
        name="orders",
        fullyQualifiedName="public.orders",
        datasourceId="source-b",
        sourceSchema="public",
        columns=[
            CatalogColumn(name="id", dataType="INT"),
            CatalogColumn(name="customer_id", dataType="INT"),
        ],
        tableConstraints=[
            CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=["customers.id"],
                relationshipType="AI_INFERRED",
                reviewStatus="APPROVED",
                targetDatasourceId="DS#source-a",
            ),
        ],
    )
    return [customers_a, customers_b, orders_b]


def _canonical_bytes(graph: Graph) -> bytes:
    """Stable serialized bytes for compatibility assertions despite blank nodes."""
    canonical = to_canonical_graph(graph)
    return "\n".join(sorted(canonical.serialize(format="nt").splitlines())).encode()


@pytest.mark.unit
class TestCrossSourceR2RMLTargetDatasource:
    def test_base_parent_triples_map_matches_owl_range(self, strategy):
        tables = _tables_targeting_source_a()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, {table.name for table in tables}, ontology)
        names = pascal_names_for(tables)
        target = tables[0]
        expected_class = URIRef(f"{PREFIX}{names[table_identity(target)]}")
        expected_tmap = URIRef(f"{PREFIX}TriplesMap_{names[table_identity(target)]}")
        object_map = URIRef(f"{PREFIX}TriplesMap_Orders/POM_CustomerId/ObjectMap")

        assert ontology.value(URIRef(f"{PREFIX}orders_customerId"), RDFS.range) == expected_class
        assert mapping.value(object_map, RR.parentTriplesMap) == expected_tmap

    def test_rigor_object_template_targets_same_source_as_owl_range(self, strategy):
        tables = _tables_targeting_source_a()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = RigorOntologyStrategy().build_r2rml(
            PREFIX,
            tables,
            {table.name for table in tables},
            ontology,
        )
        target = tables[0]
        token = subject_template_names(tables)[table_identity(target)]
        expected_template = f'{PREFIX}{token}/{{"customer_id"}}'
        object_map = URIRef(f"{PREFIX}TriplesMap_orders/POM_CustomerId/ObjectMap")

        assert str(mapping.value(object_map, RR.template)) == expected_template

    def test_single_source_r2rml_is_byte_identical_with_target_datasource_hint(self, strategy):
        customers = CatalogTable(
            id="customers",
            name="customers",
            fullyQualifiedName="public.customers",
            datasourceId="source-a",
            sourceSchema="public",
            columns=[CatalogColumn(name="id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )

        def build(target_datasource_id: str | None) -> bytes:
            orders = CatalogTable(
                id="orders",
                name="orders",
                fullyQualifiedName="public.orders",
                datasourceId="source-a",
                sourceSchema="public",
                columns=[CatalogColumn(name="customer_id", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["customer_id"],
                        referredColumns=["customers.id"],
                        targetDatasourceId=target_datasource_id,
                    )
                ],
            )
            tables = [customers, orders]
            return _canonical_bytes(strategy.build_r2rml(PREFIX, tables, {table.name for table in tables}, Graph()))

        assert build(None) == build("DS#source-a")


# ── One shared resolver for all four artifacts (!1229 review) ─────────────────
#
# OWL ``rdfs:range``, the base R2RML ``rr:parentTriplesMap``, the RIGOR object
# template and the SHACL ``sh:class`` must name the SAME table. Each used to carry
# its own (name, datasource) lookup next to ``resolve_fk_target_identity``; the
# lookups kept whichever same-named table was added last, so they could disagree
# with the resolver and with each other.


def _shacl_reference_targets(tables: list[CatalogTable], class_name: str) -> dict[str, str]:
    """{property path: target class} for the SHACL REFERENCE constraints on ``class_name``."""
    from coa_ontology.validation.shapes.config import ConstraintType, generate_config_from_db

    config = generate_config_from_db(tables, PREFIX)
    for cls in config.classes:
        if cls.class_uri == f"{PREFIX}{class_name}":
            return {
                c.property_path: c.params["target_class"]
                for c in cls.constraints
                if c.constraint_type == ConstraintType.REFERENCE
            }
    return {}


def _parent_tmaps(mapping: Graph) -> set[URIRef]:
    return {o for o in mapping.objects(None, RR.parentTriplesMap) if isinstance(o, URIRef)}


def _customers(datasource_id: str, schema: str, table_id: str, extra_cols: tuple[str, ...] = ()) -> CatalogTable:
    pk = ["id", *extra_cols]
    return CatalogTable(
        id=table_id,
        name="customers",
        fullyQualifiedName=f"{schema}.customers",
        datasourceId=datasource_id,
        sourceSchema=schema,
        columns=[CatalogColumn(name=c, dataType="INT") for c in pk],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=pk)],
    )


def _orders_b(fk: CatalogConstraint, extra_cols: tuple[str, ...] = ()) -> CatalogTable:
    return CatalogTable(
        id="orders-b",
        name="orders",
        fullyQualifiedName="public.orders",
        datasourceId="source-b",
        sourceSchema="public",
        columns=[
            CatalogColumn(name="id", dataType="INT"),
            CatalogColumn(name="customer_id", dataType="INT"),
            *[CatalogColumn(name=c, dataType="INT") for c in extra_cols],
        ],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]), fk],
    )


def _tables_ambiguous_within_hinted_source() -> list[CatalogTable]:
    """A has public.customers AND crm.customers, B has public.customers; the FK hints A.

    The hint narrows the target to source A but A still has two ``customers``
    tables, so nothing identifies the parent. ``crm.customers`` is listed AFTER
    ``public.customers`` on purpose: a ``{(name, datasource): identity}`` dict keeps
    the last one, which is how OWL used to pick ``A::crm.customers`` silently.
    """
    fk = CatalogConstraint(
        constraintType="FOREIGN_KEY",
        columns=["customer_id"],
        referredColumns=["customers.id"],
        relationshipType="AI_INFERRED",
        reviewStatus="APPROVED",
        targetDatasourceId="DS#source-a",
    )
    return [
        _customers("source-a", "public", "customers-a-public"),
        _customers("source-a", "crm", "customers-a-crm"),
        _customers("source-b", "public", "customers-b"),
        _orders_b(fk),
    ]


@pytest.mark.unit
class TestSharedFkResolverAcrossArtifacts:
    def test_shacl_target_class_equals_owl_range_and_r2rml_parent(self, strategy):
        # Kun, !1229 thread 1: OWL and R2RML pointed at A while sh:class still
        # pointed at B, so every non-null customer_id failed the shape.
        tables = _tables_targeting_source_a()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, {table.name for table in tables}, ontology)
        names = pascal_names_for(tables)
        class_a = URIRef(f"{PREFIX}{names[table_identity(tables[0])]}")

        owl_range = ontology.value(URIRef(f"{PREFIX}orders_customerId"), RDFS.range)
        parent = mapping.value(URIRef(f"{PREFIX}TriplesMap_Orders/POM_CustomerId/ObjectMap"), RR.parentTriplesMap)
        shacl = _shacl_reference_targets(tables, "Orders")

        assert owl_range == class_a
        assert parent == URIRef(f"{PREFIX}TriplesMap_{names[table_identity(tables[0])]}")
        assert shacl == {f"{PREFIX}orders_customerId": str(class_a)}

    def test_ambiguous_within_hinted_source_degrades_owl_r2rml_and_shacl(self, strategy):
        # Kun, !1229 thread 2: never resolve outside the hinted datasource, and do
        # not pick one of several matches inside it. All three degrade together.
        tables = _tables_ambiguous_within_hinted_source()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, {table.name for table in tables}, ontology)
        prop = URIRef(f"{PREFIX}orders_customerId")

        assert (prop, RDF.type, OWL.DatatypeProperty) in ontology
        assert (prop, RDF.type, OWL.ObjectProperty) not in ontology
        assert ontology.value(prop, RDFS.range) == XSD.integer
        assert _parent_tmaps(mapping) == set()
        object_map = URIRef(f"{PREFIX}TriplesMap_Orders/POM_CustomerId/ObjectMap")
        assert str(mapping.value(object_map, RR.column)) == '"customer_id"'
        assert _shacl_reference_targets(tables, "Orders") == {}

    def test_ambiguous_within_hinted_source_degrades_rigor_mapping(self, strategy):
        # The RIGOR writer emits an IRI template only for an object property whose
        # target resolves. Declare the FK an object property (as a RIGOR proposal
        # would) so the test exercises the writer's own resolution, not the
        # ontology's demotion.
        tables = _tables_ambiguous_within_hinted_source()
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        prop = URIRef(f"{PREFIX}orders_customerId")
        ontology.remove((prop, RDF.type, OWL.DatatypeProperty))
        ontology.remove((prop, RDFS.range, None))
        ontology.add((prop, RDF.type, OWL.ObjectProperty))
        mapping = RigorOntologyStrategy().build_r2rml(PREFIX, tables, {table.name for table in tables}, ontology)
        object_map = URIRef(f"{PREFIX}TriplesMap_orders/POM_CustomerId/ObjectMap")

        assert mapping.value(object_map, RR.template) is None
        assert str(mapping.value(object_map, RR.column)) == '"customer_id"'

    def test_composite_fk_target_datasource_reaches_all_three_artifacts(self, strategy):
        # A composite FK carries targetDatasourceId too; its anchor column must
        # point every artifact at source A's customers.
        fk = CatalogConstraint(
            constraintType="FOREIGN_KEY",
            columns=["customer_id", "region_id"],
            referredColumns=["customers.id", "customers.region_id"],
            relationshipType="AI_INFERRED",
            reviewStatus="APPROVED",
            targetDatasourceId="DS#source-a",
        )
        tables = [
            _customers("source-a", "public", "customers-a", ("region_id",)),
            _customers("source-b", "public", "customers-b", ("region_id",)),
            _orders_b(fk, ("region_id",)),
        ]
        ontology, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        mapping = strategy.build_r2rml(PREFIX, tables, {table.name for table in tables}, ontology)
        names = pascal_names_for(tables)
        class_a = URIRef(f"{PREFIX}{names[table_identity(tables[0])]}")

        assert ontology.value(URIRef(f"{PREFIX}orders_customerId"), RDFS.range) == class_a
        assert _parent_tmaps(mapping) == {URIRef(f"{PREFIX}TriplesMap_{names[table_identity(tables[0])]}")}
        assert _shacl_reference_targets(tables, "Orders") == {f"{PREFIX}orders_customerId": str(class_a)}


@pytest.mark.unit
class TestResolveFkTargetIdentityHint:
    def _resolve(self, tables: list[CatalogTable], hint: str | None) -> str | None:
        from coa_ontology.inducer.strategies.base import reference_index, resolve_fk_target_identity

        return resolve_fk_target_identity(tables[-1], "customers", reference_index(tables), hint)

    def test_single_match_in_hinted_source_wins_over_referrer_source(self):
        tables = _tables_targeting_source_a()
        assert self._resolve(tables, "DS#source-a") == table_identity(tables[0])
        assert self._resolve(tables, "source-a") == table_identity(tables[0])

    def test_several_matches_in_hinted_source_return_none_and_warn(self, caplog):
        tables = _tables_ambiguous_within_hinted_source()
        with caplog.at_level("WARNING", logger="coa_ontology.inducer.strategies.base"):
            assert self._resolve(tables, "DS#source-a") is None
        assert any(r.getMessage() == "fk_target_ambiguous_in_hinted_datasource" for r in caplog.records)

    def test_no_hint_keeps_the_referrer_source_first_probe(self):
        tables = _tables_ambiguous_within_hinted_source()
        assert self._resolve(tables, None) == table_identity(tables[2])

    def test_hint_naming_no_in_run_table_falls_back_to_the_probe_order(self):
        # Pinned by test_multi_fk_column's qualifier test (#1150): a hint that
        # matches nothing in the run falls back to the unhinted probes. Every
        # artifact uses this same resolver, so they still agree.
        tables = _tables_targeting_source_a()
        assert self._resolve(tables, "DS#source-z") == table_identity(tables[1])
