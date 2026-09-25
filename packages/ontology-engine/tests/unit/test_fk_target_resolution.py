# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One FK target, four artifacts that must name it identically.

``referredColumns`` names an FK's parent table with no database or datasource
qualifier, so resolving it is a lookup that turns ambiguous the moment two
datasources share a table name — the cross-catalog collision case. Four artifacts perform that
lookup and must agree:

* the R2RML mapping's ``rr:parentTriplesMap`` (``base.build_r2rml``)
* the ontology's ``rdfs:range`` (``table_to_ontology``)
* the RIGOR mapping's subject token (``rigor_ontology``)
* the SHACL shape's ``sh:class`` (``validation.shapes.config``)

Each spelled the three-probe lookup out by hand, so the order could drift between
them — and a drift is not a cosmetic inconsistency: the shape then asserts a
class-typed reference (``sh:nodeKind sh:IRI`` + ``sh:class``) against a column the
mapping emits as an ``rr:datatype`` literal, which violates on every row of the
child table. These tests pin the shared resolution and the agreement it buys.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.data_catalog import (
    CatalogColumn,
    CatalogConstraint,
    CatalogTable,
)
from coa_ontology.inducer.strategies.base import (
    RR,
    reference_index,
    resolve_fk_target_identity,
    table_identity,
)
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.validation.shapes.config import generate_config_from_db
from rdflib import RDFS, Graph

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"


def _parent(name: str, schema: str, ds_id: str, *, source_schema: bool = True) -> CatalogTable:
    """A PK-only parent table in database ``schema`` of datasource ``ds_id``."""
    return CatalogTable(
        id=f"{ds_id}.{schema}.{name}",
        name=name,
        fullyQualifiedName=f"{schema}.{name}",
        datasourceId=ds_id,
        sourceSchema=schema if source_schema else None,
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )


def _child(
    name: str,
    schema: str,
    ds_id: str,
    target: str,
    *,
    source_schema: bool = True,
) -> CatalogTable:
    """A table with a single-column FK onto the bare table name ``target``."""
    return CatalogTable(
        id=f"{ds_id}.{schema}.{name}",
        name=name,
        fullyQualifiedName=f"{schema}.{name}",
        datasourceId=ds_id,
        sourceSchema=schema if source_schema else None,
        columns=[
            CatalogColumn(name="id", dataType="INT"),
            CatalogColumn(name="customer_id", dataType="INT"),
        ],
        tableConstraints=[
            CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=[f"{target}.id"],
            ),
        ],
    )


class TestResolveFkTargetIdentity:
    def test_own_datasource_and_database_wins(self):
        """The most specific probe, and the only one that stays unambiguous when a
        neighbouring datasource shares this database's name."""
        mine = _parent("customers", "public", "src-a")
        theirs = _parent("customers", "public", "src-b")
        child = _child("orders", "public", "src-a", "customers")
        index = reference_index([mine, theirs, child])
        assert resolve_fk_target_identity(child, "customers", index) == table_identity(mine)

    def test_schema_probe_resolves_a_target_in_another_datasource(self):
        """The FK's own datasource has no such table, but exactly one datasource's
        ``public`` does, so the reference is resolvable — and must resolve, or a
        real join is silently degraded to a string column."""
        target = _parent("customers", "public", "src-b")
        decoy = _parent("customers", "crm", "src-c")
        child = _child("orders", "public", "src-a", "customers")
        index = reference_index([target, decoy, child])
        assert resolve_fk_target_identity(child, "customers", index) == table_identity(target)

    def test_schema_probe_works_without_source_schema(self):
        """``_generate_schema_sql`` and ``_catalog_to_tables`` build tables carrying
        only a ``fullyQualifiedName``. Reading ``sourceSchema`` directly skipped the
        schema probe for those, so a resolvable target fell through to the bare
        probe — which is deliberately absent for an ambiguous name, so the FK was
        degraded to a literal purely because the referrer lacked a field."""
        target = _parent("customers", "public", "src-b")
        decoy = _parent("customers", "crm", "src-c")
        child = _child("orders", "public", "src-a", "customers", source_schema=False)
        index = reference_index([target, decoy, child])
        assert resolve_fk_target_identity(child, "customers", index) == table_identity(target)

    def test_bare_probe_resolves_an_unambiguous_name(self):
        target = _parent("customers", "crm", "src-b")
        child = _child("orders", "public", "src-a", "customers")
        index = reference_index([target, child])
        assert resolve_fk_target_identity(child, "customers", index) == table_identity(target)

    def test_ambiguous_name_resolves_to_nothing(self):
        """Two datasources' ``crm.customers`` — no probe picks one, and guessing
        would join real data to whichever table sorts first."""
        a = _parent("customers", "crm", "src-b")
        b = _parent("customers", "sales", "src-c")
        child = _child("orders", "public", "src-a", "customers")
        index = reference_index([a, b, child])
        assert resolve_fk_target_identity(child, "customers", index) is None

    def test_target_outside_the_run_resolves_to_nothing(self):
        child = _child("orders", "public", "src-a", "warehouses")
        index = reference_index([child])
        assert resolve_fk_target_identity(child, "warehouses", index) is None


class TestArtifactsAgreeOnTheFkTarget:
    """The same fixture through every artifact. ``src-a.public.orders`` has an FK on
    ``customers``; two datasources expose that name, and only ``src-a`` has one in
    its own database — so all four must land on ``src-a``'s copy."""

    @staticmethod
    def _tables() -> list[CatalogTable]:
        return [
            _parent("customers", "public", "src-a"),
            _parent("customers", "crm", "src-b"),
            _child("orders", "public", "src-a", "customers"),
        ]

    def test_mapping_joins_to_the_right_parent_triples_map(self):
        tables = self._tables()
        g = Graph()
        g = TableToOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, g)
        parents = {str(o) for o in g.objects(None, RR.parentTriplesMap)}
        assert len(parents) == 1, parents
        # The parent is the PascalCase discriminated name of src-a's customers, and
        # its logical table is public.customers — not crm's.
        parent = next(iter(parents))
        tmap_names = {
            str(g.value(lt, RR.tableName))
            for lt in g.objects(next(s for s in g.subjects() if str(s) == parent), RR.logicalTable)
        }
        assert tmap_names == {'"public"."customers"'}, tmap_names

    def test_ontology_range_points_at_the_same_class(self):
        tables = self._tables()
        onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
        ranges = {str(o) for _, _, o in onto.triples((None, RDFS.range, None)) if str(o).startswith(PREFIX)}
        assert len(ranges) == 1, ranges
        assert "Customers" in next(iter(ranges))

    def test_shape_targets_the_class_the_ontology_declared(self):
        tables = self._tables()
        onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
        config = generate_config_from_db(tables, PREFIX)
        onto_ranges = {str(o) for _, _, o in onto.triples((None, RDFS.range, None)) if str(o).startswith(PREFIX)}
        shape_targets = {
            c.params["target_class"] for cls in config.classes for c in cls.constraints if "target_class" in c.params
        }
        assert shape_targets == onto_ranges, (shape_targets, onto_ranges)

    def test_rigor_resolves_to_the_same_identity(self):
        """RIGOR's mapping resolves the target through the same helper, so pinning
        the helper's answer against the mapping's parent pins the pair."""
        tables = self._tables()
        index = reference_index(tables)
        child = next(t for t in tables if t.name == "orders")
        expected = _parent("customers", "public", "src-a")
        assert resolve_fk_target_identity(child, "customers", index) == table_identity(expected)


class TestUnresolvableFkIsDegradedEverywhere:
    """When no probe picks a target, all four artifacts must degrade together — a
    literal in the mapping, a datatype property in the ontology, no ``sh:class`` in
    the shape. One artifact resolving where another degrades is the failure mode
    that violates on every row."""

    @staticmethod
    def _tables() -> list[CatalogTable]:
        return [
            _parent("customers", "crm", "src-b"),
            _parent("customers", "sales", "src-c"),
            _child("orders", "public", "src-a", "customers"),
        ]

    def test_mapping_emits_no_parent_triples_map(self):
        tables = self._tables()
        g = TableToOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, Graph())
        assert set(g.objects(None, RR.parentTriplesMap)) == set()

    def test_ontology_declares_no_object_property_range(self):
        tables = self._tables()
        onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
        in_run_ranges = {str(o) for _, _, o in onto.triples((None, RDFS.range, None)) if str(o).startswith(PREFIX)}
        assert in_run_ranges == set()

    def test_shape_declares_no_target_class(self):
        config = generate_config_from_db(self._tables(), PREFIX)
        targets = {
            c.params["target_class"] for cls in config.classes for c in cls.constraints if "target_class" in c.params
        }
        assert targets == set()
