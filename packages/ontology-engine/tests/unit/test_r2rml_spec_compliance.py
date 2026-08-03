# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive R2RML spec compliance tests for the table_to_ontology strategy.

Tests the base class's `build_r2rml` against the W3C R2RML specification
(https://www.w3.org/TR/r2rml/) scenarios. Each test creates a realistic
relational schema as CatalogTable fixtures, runs `build_r2rml`, and verifies
the generated R2RML graph satisfies the structural invariants required for
Ontop to produce correct SQL reformulations.

Key invariant: FK columns use Referencing Object Maps (R2RML §7.5) with
rr:parentTriplesMap + rr:joinCondition — the spec-standard mechanism for
expressing JOINs. Ontop reads the child/parent column pairs directly to
generate SQL JOINs, no IRI template unification needed.

Run:
    uv run pytest packages/ontology-engine/tests/unit/test_r2rml_spec_compliance.py -v
"""

import pytest
from coa_ontology.inducer.services.data_catalog import (
    CatalogColumn,
    CatalogConstraint,
    CatalogTable,
)
from coa_ontology.inducer.strategies.base import RR, SCL
from rdflib import OWL, RDF, RDFS, XSD, Graph, Namespace, URIRef

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"


@pytest.fixture
def strategy():
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    return TableToOntologyStrategy()


def _build(strategy, tables, prefix=PREFIX):
    """Helper to invoke build_r2rml with standard args."""
    novel = {t.name for t in tables}
    return strategy.build_r2rml(prefix, tables, novel, Graph())


def _subject_template(g, tmap_uri):
    """Extract the rr:template literal from a TriplesMap's SubjectMap."""
    subj_map = g.value(tmap_uri, RR.subjectMap)
    assert subj_map is not None, f"No SubjectMap for {tmap_uri}"
    tmpl = g.value(subj_map, RR.template)
    assert tmpl is not None, f"No rr:template on SubjectMap of {tmap_uri}"
    return str(tmpl)


def _object_map_column(g, pom_uri):
    """Extract the rr:column literal from a POM's ObjectMap (datatype case)."""
    om = g.value(pom_uri, RR.objectMap)
    assert om is not None, f"No ObjectMap for {pom_uri}"
    col = g.value(om, RR.column)
    return str(col) if col else None


def _object_map_parent_tmap(g, pom_uri):
    """Extract rr:parentTriplesMap from a POM's ObjectMap (FK referencing case)."""
    om = g.value(pom_uri, RR.objectMap)
    assert om is not None, f"No ObjectMap for {pom_uri}"
    return g.value(om, RR.parentTriplesMap)


def _object_map_join_conditions(g, pom_uri):
    """Extract all (child, parent) join condition pairs from a POM's ObjectMap.
    Returns a list of (child_col, parent_col) string tuples."""
    om = g.value(pom_uri, RR.objectMap)
    assert om is not None, f"No ObjectMap for {pom_uri}"
    conditions = []
    for jc in g.objects(om, RR.joinCondition):
        child = str(g.value(jc, RR.child))
        parent = str(g.value(jc, RR.parent))
        conditions.append((child, parent))
    return sorted(conditions)


def _object_map_datatype(g, pom_uri):
    """Extract the rr:datatype from a POM's ObjectMap."""
    om = g.value(pom_uri, RR.objectMap)
    assert om is not None
    return g.value(om, RR.datatype)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: TriplesMap Structure (R2RML §2)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTriplesMapStructure:
    """Every table MUST produce exactly one rr:TriplesMap with the required components."""

    def test_single_table_produces_one_triples_map(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="employees",
                fullyQualifiedName="hr.employees",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        tmaps = list(g.subjects(RDF.type, RR.TriplesMap))
        assert len(tmaps) == 1

    def test_multiple_tables_produce_separate_triples_maps(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="departments",
                fullyQualifiedName="hr.departments",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            ),
            CatalogTable(
                id="2",
                name="employees",
                fullyQualifiedName="hr.employees",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            ),
        ]
        g = _build(strategy, tables)
        tmaps = list(g.subjects(RDF.type, RR.TriplesMap))
        assert len(tmaps) == 2

    def test_triples_map_has_logical_table(self, strategy):
        """R2RML §2.1: Every TriplesMap MUST have exactly one rr:logicalTable."""
        tables = [
            CatalogTable(
                id="1",
                name="products",
                fullyQualifiedName="store.products",
                columns=[CatalogColumn(name="sku", dataType="VARCHAR")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["sku"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmap = ns.TriplesMap_Products
        lt = g.value(tmap, RR.logicalTable)
        assert lt is not None

    def test_logical_table_has_table_name(self, strategy):
        """R2RML §2.1.1: A base table or view MUST have rr:tableName."""
        tables = [
            CatalogTable(
                id="1",
                name="products",
                fullyQualifiedName="store.products",
                columns=[CatalogColumn(name="sku", dataType="VARCHAR")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["sku"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmap = ns.TriplesMap_Products
        lt = g.value(tmap, RR.logicalTable)
        table_name = g.value(lt, RR.tableName)
        assert table_name is not None
        assert str(table_name) == '"products"'

    def test_triples_map_has_subject_map(self, strategy):
        """R2RML §2.2: Every TriplesMap MUST have exactly one rr:subjectMap."""
        tables = [
            CatalogTable(
                id="1",
                name="items",
                fullyQualifiedName="db.items",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        subj = g.value(ns.TriplesMap_Items, RR.subjectMap)
        assert subj is not None

    def test_triples_map_has_class(self, strategy):
        """R2RML §2.2: SubjectMap SHOULD specify rr:class."""
        tables = [
            CatalogTable(
                id="1",
                name="items",
                fullyQualifiedName="db.items",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        subj = g.value(ns.TriplesMap_Items, RR.subjectMap)
        cls = g.value(subj, RR["class"])
        assert cls == ns.Items

    def test_every_column_has_predicate_object_map(self, strategy):
        """R2RML §2.3: Each column produces a PredicateObjectMap."""
        tables = [
            CatalogTable(
                id="1",
                name="accounts",
                fullyQualifiedName="db.accounts",
                columns=[
                    CatalogColumn(name="id", dataType="INT"),
                    CatalogColumn(name="name", dataType="VARCHAR"),
                    CatalogColumn(name="balance", dataType="DECIMAL"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        poms = list(g.objects(ns.TriplesMap_Accounts, RR.predicateObjectMap))
        assert len(poms) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Subject Maps — Primary Key Templates (R2RML §2.2, §7.1)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubjectMapPrimaryKeys:
    """SubjectMap template MUST produce unique IRIs — uses the full PK."""

    def test_simple_integer_pk(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="users",
                fullyQualifiedName="app.users",
                columns=[
                    CatalogColumn(name="user_id", dataType="BIGINT"),
                    CatalogColumn(name="email", dataType="VARCHAR"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["user_id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Users)
        assert tmpl == f'{PREFIX}users/{{"user_id"}}'
        assert '{"user_id"}' in tmpl
        assert "users/" in tmpl

    def test_composite_pk_two_columns(self, strategy):
        """Composite PK with 2 columns produces template with both as path segments."""
        tables = [
            CatalogTable(
                id="1",
                name="enrollments",
                fullyQualifiedName="school.enrollments",
                columns=[
                    CatalogColumn(name="student_id", dataType="INT"),
                    CatalogColumn(name="course_id", dataType="INT"),
                    CatalogColumn(name="grade", dataType="VARCHAR"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["student_id", "course_id"]),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Enrollments)
        assert '{"student_id"}' in tmpl
        assert '{"course_id"}' in tmpl
        assert tmpl.count("{") == 2
        # Columns joined by / as path segments
        assert '{"student_id"}/{"course_id"}' in tmpl

    def test_composite_pk_three_columns(self, strategy):
        """Composite PK with 3 columns — all present in order."""
        tables = [
            CatalogTable(
                id="1",
                name="flight_segments",
                fullyQualifiedName="travel.flight_segments",
                columns=[
                    CatalogColumn(name="airline", dataType="VARCHAR"),
                    CatalogColumn(name="flight_num", dataType="INT"),
                    CatalogColumn(name="departure_date", dataType="DATE"),
                    CatalogColumn(name="seat_class", dataType="VARCHAR"),
                ],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="PRIMARY_KEY",
                        columns=["airline", "flight_num", "departure_date"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_FlightSegments)
        assert '{"airline"}' in tmpl
        assert '{"flight_num"}' in tmpl
        assert '{"departure_date"}' in tmpl
        assert tmpl.count("{") == 3
        assert '{"airline"}/{"flight_num"}/{"departure_date"}' in tmpl

    def test_no_pk_falls_back_to_first_column(self, strategy):
        """When no PK constraint exists, use first column for subject template."""
        tables = [
            CatalogTable(
                id="1",
                name="logs",
                fullyQualifiedName="sys.logs",
                columns=[
                    CatalogColumn(name="timestamp", dataType="TIMESTAMP"),
                    CatalogColumn(name="message", dataType="TEXT"),
                ],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Logs)
        assert '{"timestamp"}' in tmpl
        assert tmpl.count("{") == 1

    def test_no_pk_no_columns_uses_literal_id(self, strategy):
        """Edge case: table with no columns at all falls back to {ID}."""
        tables = [
            CatalogTable(
                id="1",
                name="phantom",
                fullyQualifiedName="db.phantom",
                columns=[],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Phantom)
        assert "{ID}" in tmpl

    def test_pk_column_with_special_characters(self, strategy):
        """PK column names with spaces, parens, etc. are SQL-delimited in template."""
        tables = [
            CatalogTable(
                id="1",
                name="metrics",
                fullyQualifiedName="bi.metrics",
                columns=[
                    CatalogColumn(name="metric (id)", dataType="INT"),
                    CatalogColumn(name="value", dataType="DOUBLE"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["metric (id)"]),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Metrics)
        assert '{"metric (id)"}' in tmpl

    def test_pk_column_with_embedded_quotes(self, strategy):
        """Column name with double quotes gets them escaped (doubled) per SQL standard."""
        tables = [
            CatalogTable(
                id="1",
                name="weird",
                fullyQualifiedName="db.weird",
                columns=[CatalogColumn(name='col"name', dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=['col"name']),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Weird)
        # Embedded quote is doubled for SQL delimited identifier
        assert '{"col""name"}' in tmpl

    def test_pk_preserves_column_order(self, strategy):
        """Composite PK template columns appear in constraint declaration order."""
        tables = [
            CatalogTable(
                id="1",
                name="edges",
                fullyQualifiedName="graph.edges",
                columns=[
                    CatalogColumn(name="target", dataType="INT"),
                    CatalogColumn(name="source", dataType="INT"),
                    CatalogColumn(name="weight", dataType="FLOAT"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["source", "target"]),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Edges)
        src_pos = tmpl.index('"source"')
        tgt_pos = tmpl.index('"target"')
        assert src_pos < tgt_pos, "PK columns must appear in constraint declaration order"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Foreign Key Object Maps — Referencing Object Maps (R2RML §7.5)
#
# R2RML §7.5 specifies Referencing Object Maps as the mechanism for expressing
# JOINs between TriplesMaps. The ObjectMap uses:
#   - rr:parentTriplesMap pointing to the target TriplesMap
#   - rr:joinCondition BNode(s) with rr:child (FK column) and rr:parent (PK column)
#
# This approach is explicit — no IRI template unification needed. Ontop reads
# the child/parent column pairs directly to generate SQL JOINs.
#
# For composite FKs, a single POM is emitted for the first column in the
# constraint, with multiple joinConditions covering all column pairs.
# ═══════════════════════════════════════════════════════════════════════════════


class TestForeignKeyObjectMaps:
    """FK columns MUST produce Referencing Object Maps with rr:parentTriplesMap
    and rr:joinCondition — the R2RML §7.5 mechanism for expressing JOINs.

    Subsections:
      3.1 — Structural requirements (no rr:column, no rr:template, no rr:termType)
      3.2 — Simple FK → Simple PK (the happy path)
      3.3 — FK column name differs from target PK column name
      3.4 — Self-referencing FKs
      3.5 — Multiple FKs from a single table
      3.6 — FK target table not in the same build_r2rml call
      3.7 — referredColumns format variations
      3.8 — Composite FK → Composite PK (single POM, multiple joinConditions)
      3.9 — Single FK → Composite PK target (partial key reference)
    """

    # ── 3.1 Structural requirements ──────────────────────────────────────────

    def test_fk_object_map_has_no_rr_column(self, strategy):
        """R2RML §7.5: FK ObjectMaps use rr:parentTriplesMap, NOT rr:column."""
        tables = [
            CatalogTable(
                id="1",
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
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        pom = ns["TriplesMap_Orders/POM_CustomerId"]
        om = g.value(pom, RR.objectMap)
        assert om is not None, "ObjectMap node must exist"
        assert g.value(om, RR.column) is None

    def test_fk_object_map_has_no_rr_template(self, strategy):
        """R2RML §7.5: FK ObjectMaps use rr:parentTriplesMap, NOT rr:template."""
        tables = [
            CatalogTable(
                id="1",
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
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        pom = ns["TriplesMap_Orders/POM_CustomerId"]
        om = g.value(pom, RR.objectMap)
        assert om is not None, "ObjectMap node must exist"
        assert g.value(om, RR.template) is None

    def test_fk_object_map_has_no_rr_term_type(self, strategy):
        """R2RML §7.5: Referencing Object Maps do not need explicit rr:termType."""
        tables = [
            CatalogTable(
                id="1",
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
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        pom = ns["TriplesMap_Orders/POM_CustomerId"]
        om = g.value(pom, RR.objectMap)
        assert om is not None, "ObjectMap node must exist"
        assert g.value(om, RR.termType) is None

    def test_fk_object_map_has_parent_triples_map(self, strategy):
        """FK ObjectMaps MUST have rr:parentTriplesMap pointing to target TriplesMap."""
        tables = [
            CatalogTable(
                id="1",
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
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert parent is not None
        assert parent == ns.TriplesMap_Customers

    def test_fk_object_map_has_join_condition(self, strategy):
        """FK ObjectMaps MUST have rr:joinCondition with child/parent columns."""
        tables = [
            CatalogTable(
                id="1",
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
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        conditions = _object_map_join_conditions(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert conditions == [('"customer_id"', '"id"')]

    # ── 3.2 Simple FK → Simple PK (happy path) ──────────────────────────────

    def test_simple_fk_to_simple_pk(self, strategy):
        """Classic case: single-column FK references single-column PK.
        This is the most common relationship type in normalized schemas."""
        customers = CatalogTable(
            id="1",
            name="customers",
            fullyQualifiedName="store.customers",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="name", dataType="VARCHAR"),
            ],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        orders = CatalogTable(
            id="2",
            name="orders",
            fullyQualifiedName="store.orders",
            columns=[
                CatalogColumn(name="order_id", dataType="INT"),
                CatalogColumn(name="customer_id", dataType="INT"),
                CatalogColumn(name="total", dataType="DECIMAL"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["order_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.id"],
                ),
            ],
        )
        g = _build(strategy, [customers, orders])
        ns = Namespace(PREFIX)

        # FK POM uses parentTriplesMap pointing to customers
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert parent == ns.TriplesMap_Customers

        # joinCondition maps customer_id → id
        conditions = _object_map_join_conditions(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert conditions == [('"customer_id"', '"id"')]

    def test_fk_same_column_name_as_target_pk(self, strategy):
        """FK column has same name as target PK column (e.g. both 'customer_id').
        Join condition child and parent are the same column name."""
        customers = CatalogTable(
            id="1",
            name="customers",
            fullyQualifiedName="store.customers",
            columns=[CatalogColumn(name="customer_id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_id"])],
        )
        orders = CatalogTable(
            id="2",
            name="orders",
            fullyQualifiedName="store.orders",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="customer_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.customer_id"],
                ),
            ],
        )
        g = _build(strategy, [customers, orders])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert parent == ns.TriplesMap_Customers

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert conditions == [('"customer_id"', '"customer_id"')]

    # ── 3.3 FK column name differs from target PK ───────────────────────────

    def test_fk_column_name_differs_from_target_pk(self, strategy):
        """FK column 'cust_ref' references target PK 'customer_id'.
        Join condition maps cust_ref (child) to customer_id (parent)."""
        customers = CatalogTable(
            id="1",
            name="customers",
            fullyQualifiedName="store.customers",
            columns=[CatalogColumn(name="customer_id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_id"])],
        )
        orders = CatalogTable(
            id="2",
            name="orders",
            fullyQualifiedName="store.orders",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="cust_ref", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["cust_ref"],
                    referredColumns=["customers.customer_id"],
                ),
            ],
        )
        g = _build(strategy, [customers, orders])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Orders/POM_CustRef"])
        assert parent == ns.TriplesMap_Customers

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Orders/POM_CustRef"])
        assert conditions == [('"cust_ref"', '"customer_id"')]

    # ── 3.4 Self-referencing FKs ─────────────────────────────────────────────

    def test_self_referencing_fk(self, strategy):
        """Table with FK pointing to its own PK (e.g., manager_id → employees.id).
        parentTriplesMap points to the same TriplesMap (self-JOIN)."""
        employees = CatalogTable(
            id="1",
            name="employees",
            fullyQualifiedName="hr.employees",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="name", dataType="VARCHAR"),
                CatalogColumn(name="manager_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["manager_id"],
                    referredColumns=["employees.id"],
                ),
            ],
        )
        g = _build(strategy, [employees])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Employees/POM_ManagerId"])
        assert parent == ns.TriplesMap_Employees  # self-reference

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Employees/POM_ManagerId"])
        assert conditions == [('"manager_id"', '"id"')]

    def test_multiple_self_references(self, strategy):
        """Table with multiple self-referencing FKs (e.g., tree with parent + root)."""
        categories = CatalogTable(
            id="1",
            name="categories",
            fullyQualifiedName="catalog.categories",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="parent_id", dataType="INT"),
                CatalogColumn(name="root_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["parent_id"],
                    referredColumns=["categories.id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["root_id"],
                    referredColumns=["categories.id"],
                ),
            ],
        )
        g = _build(strategy, [categories])
        ns = Namespace(PREFIX)

        # Both point to same TriplesMap (self-reference)
        parent_fk = _object_map_parent_tmap(g, ns["TriplesMap_Categories/POM_ParentId"])
        root_fk = _object_map_parent_tmap(g, ns["TriplesMap_Categories/POM_RootId"])
        assert parent_fk == ns.TriplesMap_Categories
        assert root_fk == ns.TriplesMap_Categories

        # Each has its own join condition
        parent_conds = _object_map_join_conditions(g, ns["TriplesMap_Categories/POM_ParentId"])
        root_conds = _object_map_join_conditions(g, ns["TriplesMap_Categories/POM_RootId"])
        assert parent_conds == [('"parent_id"', '"id"')]
        assert root_conds == [('"root_id"', '"id"')]

    # ── 3.5 Multiple FKs from a single table ────────────────────────────────

    def test_multiple_fks_to_different_targets(self, strategy):
        """Table with FK columns pointing to different target tables."""
        departments = CatalogTable(
            id="1",
            name="departments",
            fullyQualifiedName="hr.departments",
            columns=[CatalogColumn(name="id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        locations = CatalogTable(
            id="2",
            name="locations",
            fullyQualifiedName="hr.locations",
            columns=[CatalogColumn(name="id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        employees = CatalogTable(
            id="3",
            name="employees",
            fullyQualifiedName="hr.employees",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="dept_id", dataType="INT"),
                CatalogColumn(name="location_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["dept_id"],
                    referredColumns=["departments.id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["location_id"],
                    referredColumns=["locations.id"],
                ),
            ],
        )
        g = _build(strategy, [departments, locations, employees])
        ns = Namespace(PREFIX)

        # Each FK points to its respective target TriplesMap
        dept_parent = _object_map_parent_tmap(g, ns["TriplesMap_Employees/POM_DeptId"])
        loc_parent = _object_map_parent_tmap(g, ns["TriplesMap_Employees/POM_LocationId"])
        assert dept_parent == ns.TriplesMap_Departments
        assert loc_parent == ns.TriplesMap_Locations

        # Each has correct join condition
        dept_conds = _object_map_join_conditions(g, ns["TriplesMap_Employees/POM_DeptId"])
        loc_conds = _object_map_join_conditions(g, ns["TriplesMap_Employees/POM_LocationId"])
        assert dept_conds == [('"dept_id"', '"id"')]
        assert loc_conds == [('"location_id"', '"id"')]

    def test_multiple_fks_to_same_target(self, strategy):
        """Two FK columns in one table both pointing to the same target (e.g., shipper/receiver)."""
        addresses = CatalogTable(
            id="1",
            name="addresses",
            fullyQualifiedName="logistics.addresses",
            columns=[CatalogColumn(name="id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        shipments = CatalogTable(
            id="2",
            name="shipments",
            fullyQualifiedName="logistics.shipments",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="from_address_id", dataType="INT"),
                CatalogColumn(name="to_address_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["from_address_id"],
                    referredColumns=["addresses.id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["to_address_id"],
                    referredColumns=["addresses.id"],
                ),
            ],
        )
        g = _build(strategy, [addresses, shipments])
        ns = Namespace(PREFIX)

        # Both FKs point to addresses TriplesMap
        from_parent = _object_map_parent_tmap(g, ns["TriplesMap_Shipments/POM_FromAddressId"])
        to_parent = _object_map_parent_tmap(g, ns["TriplesMap_Shipments/POM_ToAddressId"])
        assert from_parent == ns.TriplesMap_Addresses
        assert to_parent == ns.TriplesMap_Addresses

        # Each has distinct join condition (different child columns)
        from_conds = _object_map_join_conditions(g, ns["TriplesMap_Shipments/POM_FromAddressId"])
        to_conds = _object_map_join_conditions(g, ns["TriplesMap_Shipments/POM_ToAddressId"])
        assert from_conds == [('"from_address_id"', '"id"')]
        assert to_conds == [('"to_address_id"', '"id"')]

    # ── 3.6 FK target not in the same build call ─────────────────────────────

    def test_fk_target_not_included_in_tables_list(self, strategy):
        """FK references a table not passed to build_r2rml. The FK ObjectMap is
        still generated because referredColumns carries the target table name.
        parentTriplesMap is synthesized as TriplesMap_{PascalCase(target)}."""
        orders = CatalogTable(
            id="1",
            name="orders",
            fullyQualifiedName="store.orders",
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
                ),
            ],
        )
        # customers table NOT included
        g = _build(strategy, [orders])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Orders/POM_CustomerId"])
        # parentTriplesMap is still generated, referencing the expected TriplesMap URI
        assert parent == ns.TriplesMap_Customers

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Orders/POM_CustomerId"])
        assert conditions == [('"customer_id"', '"id"')]

    # ── 3.7 referredColumns format variations ────────────────────────────────

    def test_referred_columns_two_part_table_dot_column(self, strategy):
        """Standard format from induce_catalog: 'TargetTable.column'."""
        tables = [
            CatalogTable(
                id="1",
                name="invoices",
                fullyQualifiedName="billing.invoices",
                columns=[CatalogColumn(name="order_ref", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["order_ref"],
                        referredColumns=["orders.order_id"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert parent == ns.TriplesMap_Orders

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert conditions == [('"order_ref"', '"order_id"')]

    def test_referred_columns_single_part_table_only(self, strategy):
        """Defensive: referredColumns with just table name (no dot).
        No parent column can be extracted, so FK is treated as a datatype column
        (emitting parentTriplesMap without joinCondition would produce a Cartesian
        product in Ontop per R2RML §7.5)."""
        tables = [
            CatalogTable(
                id="1",
                name="invoices",
                fullyQualifiedName="billing.invoices",
                columns=[CatalogColumn(name="order_ref", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["order_ref"],
                        referredColumns=["orders"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)

        # Single-part referredColumns cannot produce a valid joinCondition,
        # so the column falls through to the datatype ObjectMap path.
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert parent is None, "No parentTriplesMap when joinCondition cannot be determined"
        col = _object_map_column(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert col == '"order_ref"'

    @pytest.mark.xfail(
        strict=True,
        reason="Three-part referredColumns ('schema.table.column') extracts schema as target — not supported",
    )
    def test_referred_columns_three_part_schema_table_column(self, strategy):
        """Future-proofing: referredColumns as 'schema.table.column'.
        parts[0] extracts 'schema' — this is WRONG. The correct target is parts[1]
        (the table name). Marked xfail so a future fix auto-promotes."""
        tables = [
            CatalogTable(
                id="1",
                name="invoices",
                fullyQualifiedName="billing.invoices",
                columns=[CatalogColumn(name="order_ref", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["order_ref"],
                        referredColumns=["public.orders.order_id"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)

        # Correct expectation: target table is "orders" (parts[1]), not "public" (parts[0])
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert parent == ns.TriplesMap_Orders

        # parts[-1] = "order_id" (correct — last part is always the column)
        conditions = _object_map_join_conditions(g, ns["TriplesMap_Invoices/POM_OrderRef"])
        assert conditions == [('"order_ref"', '"order_id"')]

    # ── 3.8 Composite FK → Composite PK (single POM, multiple joinConditions)

    def test_composite_fk_produces_single_pom_with_multiple_join_conditions(self, strategy):
        """Composite FK constraint with columns=[col_a, col_b] produces ONE POM
        for the first column in the constraint, with multiple joinConditions.
        The second column does NOT get its own POM."""
        time_slots = CatalogTable(
            id="1",
            name="time_slots",
            fullyQualifiedName="sched.time_slots",
            columns=[
                CatalogColumn(name="day", dataType="VARCHAR"),
                CatalogColumn(name="hour", dataType="INT"),
                CatalogColumn(name="room", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["day", "hour"]),
            ],
        )
        bookings = CatalogTable(
            id="2",
            name="bookings",
            fullyQualifiedName="sched.bookings",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="booking_day", dataType="VARCHAR"),
                CatalogColumn(name="booking_hour", dataType="INT"),
                CatalogColumn(name="notes", dataType="TEXT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["booking_day", "booking_hour"],
                    referredColumns=["time_slots.day", "time_slots.hour"],
                ),
            ],
        )
        g = _build(strategy, [time_slots, bookings])
        ns = Namespace(PREFIX)

        # The first FK column (booking_day) gets the POM with parentTriplesMap
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Bookings/POM_BookingDay"])
        assert parent == ns.TriplesMap_TimeSlots

        # Multiple join conditions — one per column pair
        conditions = _object_map_join_conditions(g, ns["TriplesMap_Bookings/POM_BookingDay"])
        assert conditions == [('"booking_day"', '"day"'), ('"booking_hour"', '"hour"')]

        # booking_hour does NOT get a second REFERENCING map — the relationship is
        # expressed once, on booking_day (R2RML §7.5).
        hour_pom = ns["TriplesMap_Bookings/POM_BookingHour"]
        assert _object_map_parent_tmap(g, hour_pom) is None, (
            "the composite relationship must be carried by exactly one referencing object map"
        )

        # It DOES get a literal mapping. The ontology declares a property for
        # every column, so leaving booking_hour unmapped left that declaration
        # with nothing behind it and any SPARQL using it returned nothing.
        assert _object_map_column(g, hour_pom) == '"booking_hour"'
        assert _object_map_datatype(g, hour_pom) == XSD.integer

    def test_composite_fk_three_columns(self, strategy):
        """Composite FK with 3 columns produces single POM with 3 joinConditions."""
        target = CatalogTable(
            id="1",
            name="schedules",
            fullyQualifiedName="cal.schedules",
            columns=[
                CatalogColumn(name="year", dataType="INT"),
                CatalogColumn(name="month", dataType="INT"),
                CatalogColumn(name="day", dataType="INT"),
                CatalogColumn(name="event", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["year", "month", "day"]),
            ],
        )
        refs = CatalogTable(
            id="2",
            name="reminders",
            fullyQualifiedName="cal.reminders",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="sched_year", dataType="INT"),
                CatalogColumn(name="sched_month", dataType="INT"),
                CatalogColumn(name="sched_day", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["sched_year", "sched_month", "sched_day"],
                    referredColumns=["schedules.year", "schedules.month", "schedules.day"],
                ),
            ],
        )
        g = _build(strategy, [target, refs])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_Reminders/POM_SchedYear"])
        assert parent == ns.TriplesMap_Schedules

        conditions = _object_map_join_conditions(g, ns["TriplesMap_Reminders/POM_SchedYear"])
        assert conditions == [
            ('"sched_day"', '"day"'),
            ('"sched_month"', '"month"'),
            ('"sched_year"', '"year"'),
        ]

    # ── 3.9 Single FK column → Composite PK target ──────────────────────────

    def test_single_fk_to_composite_pk_uses_referencing_object_map(self, strategy):
        """A single FK column referencing one column of a composite-PK target
        correctly uses rr:parentTriplesMap + rr:joinCondition (R2RML §7.5).
        This is a partial-key reference — the joinCondition only covers one
        column pair, but that's valid R2RML."""
        enrollments = CatalogTable(
            id="1",
            name="enrollments",
            fullyQualifiedName="school.enrollments",
            columns=[
                CatalogColumn(name="student_id", dataType="INT"),
                CatalogColumn(name="course_id", dataType="INT"),
                CatalogColumn(name="grade", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["student_id", "course_id"]),
            ],
        )
        grade_comments = CatalogTable(
            id="2",
            name="grade_comments",
            fullyQualifiedName="school.grade_comments",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="enrollment_student", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["enrollment_student"],
                    referredColumns=["enrollments.student_id"],
                ),
            ],
        )
        g = _build(strategy, [enrollments, grade_comments])
        ns = Namespace(PREFIX)

        parent = _object_map_parent_tmap(g, ns["TriplesMap_GradeComments/POM_EnrollmentStudent"])
        assert parent == ns.TriplesMap_Enrollments

        conditions = _object_map_join_conditions(g, ns["TriplesMap_GradeComments/POM_EnrollmentStudent"])
        assert conditions == [('"enrollment_student"', '"student_id"')]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Referencing Object Maps — JOIN Correctness (R2RML §7.5)
#
# Tests that the rr:parentTriplesMap + rr:joinCondition mechanism produces
# correct JOINs across various schema topologies (star, chain, diamond).
# ═══════════════════════════════════════════════════════════════════════════════


class TestReferencingObjectMaps:
    """The fundamental correctness property: for every FK relationship, the
    ObjectMap uses rr:parentTriplesMap to identify the target TriplesMap and
    rr:joinCondition to declare the child/parent column pairs that Ontop
    uses to emit SQL JOINs."""

    def _assert_referencing_correct(self, g, pom_uri, expected_parent, expected_conditions):
        """Assert that a POM has correct parentTriplesMap and joinConditions."""
        parent = _object_map_parent_tmap(g, pom_uri)
        assert parent == expected_parent, (
            f"parentTriplesMap mismatch for {pom_uri}\n  Expected: {expected_parent}\n  Got:      {parent}"
        )
        conditions = _object_map_join_conditions(g, pom_uri)
        assert conditions == expected_conditions, (
            f"joinCondition mismatch for {pom_uri}\n  Expected: {expected_conditions}\n  Got:      {conditions}"
        )

    def test_star_schema_fact_to_dimensions(self, strategy):
        """Star schema: fact table with FKs to multiple dimension tables.
        Every FK must have correct parentTriplesMap and joinCondition."""
        dim_customer = CatalogTable(
            id="1",
            name="dim_customer",
            fullyQualifiedName="dw.dim_customer",
            columns=[
                CatalogColumn(name="customer_key", dataType="INT"),
                CatalogColumn(name="name", dataType="VARCHAR"),
            ],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_key"])],
        )
        dim_product = CatalogTable(
            id="2",
            name="dim_product",
            fullyQualifiedName="dw.dim_product",
            columns=[
                CatalogColumn(name="product_key", dataType="INT"),
                CatalogColumn(name="sku", dataType="VARCHAR"),
            ],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["product_key"])],
        )
        dim_date = CatalogTable(
            id="3",
            name="dim_date",
            fullyQualifiedName="dw.dim_date",
            columns=[
                CatalogColumn(name="date_key", dataType="INT"),
                CatalogColumn(name="calendar_date", dataType="DATE"),
            ],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["date_key"])],
        )
        fact_sales = CatalogTable(
            id="4",
            name="fact_sales",
            fullyQualifiedName="dw.fact_sales",
            columns=[
                CatalogColumn(name="sale_id", dataType="INT"),
                CatalogColumn(name="customer_key", dataType="INT"),
                CatalogColumn(name="product_key", dataType="INT"),
                CatalogColumn(name="date_key", dataType="INT"),
                CatalogColumn(name="amount", dataType="DECIMAL"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["sale_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_key"],
                    referredColumns=["dim_customer.customer_key"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["product_key"],
                    referredColumns=["dim_product.product_key"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["date_key"],
                    referredColumns=["dim_date.date_key"],
                ),
            ],
        )
        g = _build(strategy, [dim_customer, dim_product, dim_date, fact_sales])
        ns = Namespace(PREFIX)

        # Each FK must reference its dimension's TriplesMap with correct join
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_FactSales/POM_CustomerKey"],
            ns.TriplesMap_DimCustomer,
            [('"customer_key"', '"customer_key"')],
        )
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_FactSales/POM_ProductKey"],
            ns.TriplesMap_DimProduct,
            [('"product_key"', '"product_key"')],
        )
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_FactSales/POM_DateKey"],
            ns.TriplesMap_DimDate,
            [('"date_key"', '"date_key"')],
        )

    def test_chain_of_fks(self, strategy):
        """Chain: A→B→C — each link must have correct referencing object map."""
        countries = CatalogTable(
            id="1",
            name="countries",
            fullyQualifiedName="geo.countries",
            columns=[CatalogColumn(name="code", dataType="VARCHAR")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["code"])],
        )
        cities = CatalogTable(
            id="2",
            name="cities",
            fullyQualifiedName="geo.cities",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="country_code", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["country_code"],
                    referredColumns=["countries.code"],
                ),
            ],
        )
        addresses = CatalogTable(
            id="3",
            name="addresses",
            fullyQualifiedName="geo.addresses",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="city_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["city_id"],
                    referredColumns=["cities.id"],
                ),
            ],
        )
        g = _build(strategy, [countries, cities, addresses])
        ns = Namespace(PREFIX)

        # cities.country_code → countries.code
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_Cities/POM_CountryCode"],
            ns.TriplesMap_Countries,
            [('"country_code"', '"code"')],
        )
        # addresses.city_id → cities.id
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_Addresses/POM_CityId"],
            ns.TriplesMap_Cities,
            [('"city_id"', '"id"')],
        )

    def test_diamond_dependency(self, strategy):
        """Diamond: D depends on B and C, both depend on A."""
        a = CatalogTable(
            id="1",
            name="base_entity",
            fullyQualifiedName="app.base_entity",
            columns=[CatalogColumn(name="id", dataType="INT")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
        )
        b = CatalogTable(
            id="2",
            name="extension_a",
            fullyQualifiedName="app.extension_a",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="base_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["base_id"],
                    referredColumns=["base_entity.id"],
                ),
            ],
        )
        c = CatalogTable(
            id="3",
            name="extension_b",
            fullyQualifiedName="app.extension_b",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="base_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["base_id"],
                    referredColumns=["base_entity.id"],
                ),
            ],
        )
        d = CatalogTable(
            id="4",
            name="combined",
            fullyQualifiedName="app.combined",
            columns=[
                CatalogColumn(name="id", dataType="INT"),
                CatalogColumn(name="ext_a_id", dataType="INT"),
                CatalogColumn(name="ext_b_id", dataType="INT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ext_a_id"],
                    referredColumns=["extension_a.id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ext_b_id"],
                    referredColumns=["extension_b.id"],
                ),
            ],
        )
        g = _build(strategy, [a, b, c, d])
        ns = Namespace(PREFIX)

        # All four FK→PK links must have correct referencing object maps
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_ExtensionA/POM_BaseId"],
            ns.TriplesMap_BaseEntity,
            [('"base_id"', '"id"')],
        )
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_ExtensionB/POM_BaseId"],
            ns.TriplesMap_BaseEntity,
            [('"base_id"', '"id"')],
        )
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_Combined/POM_ExtAId"],
            ns.TriplesMap_ExtensionA,
            [('"ext_a_id"', '"id"')],
        )
        self._assert_referencing_correct(
            g,
            ns["TriplesMap_Combined/POM_ExtBId"],
            ns.TriplesMap_ExtensionB,
            [('"ext_b_id"', '"id"')],
        )

    def test_composite_fk_in_star_schema(self, strategy):
        """Star schema with a composite FK to a dimension with composite PK."""
        dim_time = CatalogTable(
            id="1",
            name="dim_time",
            fullyQualifiedName="dw.dim_time",
            columns=[
                CatalogColumn(name="date", dataType="DATE"),
                CatalogColumn(name="hour", dataType="INT"),
                CatalogColumn(name="label", dataType="VARCHAR"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["date", "hour"]),
            ],
        )
        fact_events = CatalogTable(
            id="2",
            name="fact_events",
            fullyQualifiedName="dw.fact_events",
            columns=[
                CatalogColumn(name="event_id", dataType="INT"),
                CatalogColumn(name="event_date", dataType="DATE"),
                CatalogColumn(name="event_hour", dataType="INT"),
                CatalogColumn(name="payload", dataType="TEXT"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["event_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["event_date", "event_hour"],
                    referredColumns=["dim_time.date", "dim_time.hour"],
                ),
            ],
        )
        g = _build(strategy, [dim_time, fact_events])
        ns = Namespace(PREFIX)

        # Composite FK produces single POM on first column with multiple joinConditions
        parent = _object_map_parent_tmap(g, ns["TriplesMap_FactEvents/POM_EventDate"])
        assert parent == ns.TriplesMap_DimTime

        conditions = _object_map_join_conditions(g, ns["TriplesMap_FactEvents/POM_EventDate"])
        assert conditions == [('"event_date"', '"date"'), ('"event_hour"', '"hour"')]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Datatype Mapping (R2RML §10)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDatatypeMapping:
    """Non-FK columns MUST have appropriate rr:datatype based on SQL type."""

    @pytest.mark.parametrize(
        "sql_type,expected_xsd",
        [
            ("INT", XSD.integer),
            ("BIGINT", XSD.long),
            ("SMALLINT", XSD.short),
            ("TINYINT", XSD.byte),
            ("FLOAT", XSD.float),
            ("DOUBLE", XSD.double),
            ("DECIMAL", XSD.decimal),
            ("NUMERIC", XSD.decimal),
            ("VARCHAR", XSD.string),
            ("TEXT", XSD.string),
            ("CHAR", XSD.string),
            ("STRING", XSD.string),
            ("BOOLEAN", XSD.boolean),
            ("BINARY", XSD.hexBinary),
            ("BLOB", XSD.hexBinary),
            ("UUID", XSD.string),
        ],
    )
    def test_sql_to_xsd_mapping(self, strategy, sql_type, expected_xsd):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="col", dataType=sql_type)],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        dt = _object_map_datatype(g, ns["TriplesMap_T/POM_Col"])
        assert dt == expected_xsd

    @pytest.mark.parametrize(
        ("sql_type", "expected"),
        [
            ("DATE", XSD.date),
            ("TIMESTAMP", XSD.dateTime),
            ("DATETIME", XSD.dateTime),
            ("TIME", XSD.time),
        ],
    )
    def test_temporal_types_mapped_faithfully(self, strategy, sql_type, expected):
        """Temporal types keep their real XSD type — they are NOT downcast to string.

        Previously asserted xsd:string "for VKG compatibility". That downcast made
        Ontop's type reasoner treat every temporal column as a string, so a
        comparison against an xsd:date literal was disjoint, the query was proven
        unsatisfiable, and Ontop emitted its no-mapping placeholder
        ("SELECT 1 AS uselessVariable") — silently returning nothing for EVERY
        date-filtered query. Verified against Ontop 5.5.0 that faithful temporal
        types load without MappingOntologyMismatchException and reformulate to
        real SQL.
        """
        tables = [
            CatalogTable(
                id="1",
                name="events",
                fullyQualifiedName="db.events",
                columns=[CatalogColumn(name="ts", dataType=sql_type)],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        dt = _object_map_datatype(g, ns["TriplesMap_Events/POM_Ts"])
        assert dt == expected

    def test_unknown_type_defaults_to_string(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="col", dataType="JSONB")],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        dt = _object_map_datatype(g, ns["TriplesMap_T/POM_Col"])
        assert dt == XSD.string

    def test_type_with_precision_stripped(self, strategy):
        """DECIMAL(10,2) → base type DECIMAL → xsd:decimal."""
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="amount", dataType="DECIMAL(10,2)")],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        dt = _object_map_datatype(g, ns["TriplesMap_T/POM_Amount"])
        assert dt == XSD.decimal


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: Logical Table — SQL Identifier Handling (R2RML §2.1.1)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSqlIdentifierHandling:
    """Table and column names MUST be SQL-delimited identifiers to preserve
    verbatim names through Ontop's SQL generation."""

    def test_table_name_is_delimited(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="My Table",
                fullyQualifiedName="db.My Table",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        lt = g.value(ns["TriplesMap_MyTable"], RR.logicalTable)
        table_name = str(g.value(lt, RR.tableName))
        assert table_name == '"My Table"'

    def test_column_names_are_delimited(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="data",
                fullyQualifiedName="db.data",
                columns=[
                    CatalogColumn(name="First Name", dataType="VARCHAR"),
                    CatalogColumn(name="last-name", dataType="VARCHAR"),
                ],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        col1 = _object_map_column(g, ns["TriplesMap_Data/POM_FirstName"])
        col2 = _object_map_column(g, ns["TriplesMap_Data/POM_LastName"])
        assert col1 == '"First Name"'
        assert col2 == '"last-name"'

    def test_reserved_word_table_name(self, strategy):
        """SQL reserved words (SELECT, ORDER, etc.) work when delimited."""
        tables = [
            CatalogTable(
                id="1",
                name="order",
                fullyQualifiedName="db.order",
                columns=[CatalogColumn(name="select", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["select"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        lt = g.value(ns.TriplesMap_Order, RR.logicalTable)
        assert str(g.value(lt, RR.tableName)) == '"order"'
        tmpl = _subject_template(g, ns.TriplesMap_Order)
        assert '{"select"}' in tmpl

    def test_column_with_dots_in_name(self, strategy):
        """Column names containing dots (unusual but valid) are preserved.
        Note: to_pascal strips dots without word-splitting, so 'app.version' → 'Appversion'."""
        tables = [
            CatalogTable(
                id="1",
                name="config",
                fullyQualifiedName="app.config",
                columns=[CatalogColumn(name="app.version", dataType="VARCHAR")],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        # to_pascal("app.version") = "Appversion" (dot stripped, not a word boundary)
        col = _object_map_column(g, ns["TriplesMap_Config/POM_Appversion"])
        assert col == '"app.version"'


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: URI Prefix Normalization
# ═══════════════════════════════════════════════════════════════════════════════


class TestUriPrefixNormalization:
    """ontology_uri_prefix MUST end with '#' or '/' for valid IRI construction."""

    def test_prefix_with_hash_preserved(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables, prefix="http://example.org/onto#")
        ns = Namespace("http://example.org/onto#")
        assert (ns.TriplesMap_T, RDF.type, RR.TriplesMap) in g
        tmpl = _subject_template(g, ns.TriplesMap_T)
        assert tmpl.startswith("http://example.org/onto#t/")

    def test_prefix_with_slash_preserved(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables, prefix="http://example.org/onto/")
        ns = Namespace("http://example.org/onto/")
        assert (ns.TriplesMap_T, RDF.type, RR.TriplesMap) in g
        tmpl = _subject_template(g, ns.TriplesMap_T)
        assert tmpl.startswith("http://example.org/onto/t/")

    def test_prefix_without_separator_gets_hash(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables, prefix="http://example.org/onto")
        ns = Namespace("http://example.org/onto#")
        assert (ns.TriplesMap_T, RDF.type, RR.TriplesMap) in g
        tmpl = _subject_template(g, ns.TriplesMap_T)
        assert tmpl.startswith("http://example.org/onto#t/")

    def test_fk_parent_triples_map_uses_normalized_prefix(self, strategy):
        """FK parentTriplesMap URI must use the same (normalized) prefix as TriplesMap URIs."""
        tables = [
            CatalogTable(
                id="1",
                name="parent",
                fullyQualifiedName="db.parent",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            ),
            CatalogTable(
                id="2",
                name="child",
                fullyQualifiedName="db.child",
                columns=[CatalogColumn(name="parent_id", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["parent_id"],
                        referredColumns=["parent.id"],
                    ),
                ],
            ),
        ]
        # Use prefix without separator
        g = _build(strategy, tables, prefix="http://example.org/ns")
        ns = Namespace("http://example.org/ns#")

        # parentTriplesMap uses the normalized prefix with #
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Child/POM_ParentId"])
        assert parent == ns.TriplesMap_Parent
        assert str(parent).startswith("http://example.org/ns#")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: SCL Provenance Annotations
# ═══════════════════════════════════════════════════════════════════════════════


class TestProvenanceAnnotations:
    """TriplesMap annotations for VKG query routing (coa:datasourceId, coa:sourceSchema)."""

    def test_datasource_id_annotated(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="mydb.public.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
                datasourceId="production-rds",
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        ds = g.value(ns.TriplesMap_T, SCL.datasourceId)
        assert str(ds) == "production-rds"

    def test_source_schema_annotated(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="mydb.analytics.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
                sourceSchema="analytics",
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        schema = g.value(ns.TriplesMap_T, SCL.sourceSchema)
        assert str(schema) == "analytics"

    def test_no_annotations_when_fields_absent(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="t",
                fullyQualifiedName="db.t",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        assert g.value(ns.TriplesMap_T, SCL.datasourceId) is None
        assert g.value(ns.TriplesMap_T, SCL.sourceSchema) is None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: Predicate Naming Convention
# ═══════════════════════════════════════════════════════════════════════════════


class TestPredicateNaming:
    """Predicates follow the convention: ind:{camelCase(table)}_{camelCase(col)}."""

    def test_simple_names(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="user_accounts",
                fullyQualifiedName="db.user_accounts",
                columns=[
                    CatalogColumn(name="account_id", dataType="INT"),
                    CatalogColumn(name="email_address", dataType="VARCHAR"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["account_id"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        pom_id = ns["TriplesMap_UserAccounts/POM_AccountId"]
        pom_email = ns["TriplesMap_UserAccounts/POM_EmailAddress"]
        pred_id = g.value(pom_id, RR.predicate)
        pred_email = g.value(pom_email, RR.predicate)
        assert pred_id == ns.userAccounts_accountId
        assert pred_email == ns.userAccounts_emailAddress

    def test_single_word_names(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="users",
                fullyQualifiedName="db.users",
                columns=[CatalogColumn(name="name", dataType="VARCHAR")],
                tableConstraints=[],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        pred = g.value(ns["TriplesMap_Users/POM_Name"], RR.predicate)
        assert pred == ns.users_name


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: Edge Cases and Defensive Behavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Boundary conditions and unusual schemas."""

    def test_table_with_only_pk_column(self, strategy):
        """Table that is just an ID (e.g., enum/lookup with no other columns)."""
        tables = [
            CatalogTable(
                id="1",
                name="statuses",
                fullyQualifiedName="db.statuses",
                columns=[CatalogColumn(name="code", dataType="VARCHAR")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["code"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        # Should still produce a valid TriplesMap with SubjectMap and one POM
        assert (ns.TriplesMap_Statuses, RDF.type, RR.TriplesMap) in g
        tmpl = _subject_template(g, ns.TriplesMap_Statuses)
        assert '{"code"}' in tmpl
        poms = list(g.objects(ns.TriplesMap_Statuses, RR.predicateObjectMap))
        assert len(poms) == 1

    def test_fk_without_referred_columns_treated_as_data(self, strategy):
        """FK constraint with empty referredColumns should not produce IRI ObjectMap."""
        tables = [
            CatalogTable(
                id="1",
                name="broken",
                fullyQualifiedName="db.broken",
                columns=[CatalogColumn(name="ref_id", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(constraintType="FOREIGN_KEY", columns=["ref_id"], referredColumns=[]),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        om = ns["TriplesMap_Broken/POM_RefId/ObjectMap"]
        # Should be a datatype ObjectMap, not referencing
        assert g.value(om, RR.parentTriplesMap) is None
        assert g.value(om, RR.column) is not None

    def test_fk_constraint_with_none_referred_columns(self, strategy):
        """FK constraint where referredColumns is None."""
        tables = [
            CatalogTable(
                id="1",
                name="broken",
                fullyQualifiedName="db.broken",
                columns=[CatalogColumn(name="ref_id", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(constraintType="FOREIGN_KEY", columns=["ref_id"], referredColumns=None),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        om = ns["TriplesMap_Broken/POM_RefId/ObjectMap"]
        assert g.value(om, RR.parentTriplesMap) is None

    def test_unique_constraint_does_not_affect_subject_template(self, strategy):
        """UNIQUE constraints should not be confused with PRIMARY_KEY."""
        tables = [
            CatalogTable(
                id="1",
                name="accounts",
                fullyQualifiedName="db.accounts",
                columns=[
                    CatalogColumn(name="id", dataType="INT"),
                    CatalogColumn(name="email", dataType="VARCHAR"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                    CatalogConstraint(constraintType="UNIQUE", columns=["email"]),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        tmpl = _subject_template(g, ns.TriplesMap_Accounts)
        # Subject uses PK (id), not UNIQUE (email)
        assert '{"id"}' in tmpl
        assert '{"email"}' not in tmpl

    def test_mixed_pk_and_fk_on_same_column(self, strategy):
        """Column that is both PK and FK (e.g., identifying relationship).
        The column should be used in the subject template AND produce a
        referencing object map pointing to the target."""
        tables = [
            CatalogTable(
                id="1",
                name="user_profiles",
                fullyQualifiedName="db.user_profiles",
                columns=[
                    CatalogColumn(name="user_id", dataType="INT"),
                    CatalogColumn(name="bio", dataType="TEXT"),
                ],
                tableConstraints=[
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["user_id"]),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["user_id"],
                        referredColumns=["users.id"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        # Subject uses user_id (it's the PK)
        tmpl = _subject_template(g, ns.TriplesMap_UserProfiles)
        assert '{"user_id"}' in tmpl
        # user_id POM should be FK (referencing object map), not datatype
        parent = _object_map_parent_tmap(g, ns["TriplesMap_UserProfiles/POM_UserId"])
        assert parent == ns.TriplesMap_Users
        conditions = _object_map_join_conditions(g, ns["TriplesMap_UserProfiles/POM_UserId"])
        assert conditions == [('"user_id"', '"id"')]

    def test_table_name_case_sensitivity(self, strategy):
        """Mixed-case table names produce correct PascalCase TriplesMap URIs."""
        tables = [
            CatalogTable(
                id="1",
                name="UserActivity",
                fullyQualifiedName="db.UserActivity",
                columns=[CatalogColumn(name="ID", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["ID"])],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        # PascalCase applied to table name
        assert (ns.TriplesMap_Useractivity, RDF.type, RR.TriplesMap) in g
        # But rr:tableName preserves original case
        lt = g.value(ns.TriplesMap_Useractivity, RR.logicalTable)
        assert str(g.value(lt, RR.tableName)) == '"UserActivity"'

    def test_multiple_fk_constraints_on_same_column_first_wins(self, strategy):
        """If multiple FK constraints reference the same column, first one wins."""
        tables = [
            CatalogTable(
                id="1",
                name="refs",
                fullyQualifiedName="db.refs",
                columns=[CatalogColumn(name="target_id", dataType="INT")],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["target_id"],
                        referredColumns=["table_a.id"],
                    ),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["target_id"],
                        referredColumns=["table_b.id"],
                    ),
                ],
            )
        ]
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)
        # First FK constraint wins — parentTriplesMap points to table_a
        parent = _object_map_parent_tmap(g, ns["TriplesMap_Refs/POM_TargetId"])
        assert parent == ns.TriplesMap_TableA


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: IRI collision resolution (to_pascal is lossy)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestPascalCaseCollisions:
    """Distinct tables must never share a class / TriplesMap IRI.

    ``to_pascal`` folds separators and case, so names like ``order_item`` and
    ``order-item`` collapse onto one local name. Minting IRIs from it directly
    fused the two tables: one TriplesMap carrying two ``rr:tableName`` values
    (invalid R2RML — a TriplesMap has exactly one logical table) and one class
    carrying both tables' labels and key axioms.
    """

    @staticmethod
    def _tables(*names):
        return [
            CatalogTable(
                id=str(i),
                name=name,
                fullyQualifiedName=f"db.{name}",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            )
            for i, name in enumerate(names, start=1)
        ]

    def test_separator_variants_get_distinct_triples_maps(self, strategy):
        tables = self._tables("order_item", "order-item")
        g = _build(strategy, tables)

        tmaps = set(g.subjects(RDF.type, RR.TriplesMap))
        assert len(tmaps) == 2, f"Expected one TriplesMap per table, got {tmaps}"

    def test_each_triples_map_has_exactly_one_table_name(self, strategy):
        """The R2RML invariant that the collision broke."""
        tables = self._tables("order_item", "order-item")
        g = _build(strategy, tables)

        for tmap in g.subjects(RDF.type, RR.TriplesMap):
            logical_tables = list(g.objects(tmap, RR.logicalTable))
            assert len(logical_tables) == 1, f"{tmap} has {len(logical_tables)} logical tables"
            names = [str(n) for lt in logical_tables for n in g.objects(lt, RR.tableName)]
            assert len(names) == 1, f"{tmap} maps {len(names)} tables: {names}"

    def test_both_source_tables_are_mapped(self, strategy):
        """Neither table may be silently dropped by the disambiguation."""
        tables = self._tables("order_item", "order-item")
        g = _build(strategy, tables)

        mapped = {
            str(n)
            for tmap in g.subjects(RDF.type, RR.TriplesMap)
            for lt in g.objects(tmap, RR.logicalTable)
            for n in g.objects(lt, RR.tableName)
        }
        assert mapped == {'"order_item"', '"order-item"'}

    def test_case_only_variants_get_distinct_triples_maps(self, strategy):
        tables = self._tables("Customer", "customer")
        g = _build(strategy, tables)

        assert len(set(g.subjects(RDF.type, RR.TriplesMap))) == 2

    def test_non_colliding_names_keep_bare_pascal_iris(self, strategy):
        """No discriminator when there is nothing to disambiguate (IRI stability)."""
        tables = self._tables("orders", "customers")
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)

        assert (ns.TriplesMap_Orders, RDF.type, RR.TriplesMap) in g
        assert (ns.TriplesMap_Customers, RDF.type, RR.TriplesMap) in g

    def test_first_colliding_table_keeps_the_bare_iri(self, strategy):
        """Existing deployments keep byte-identical IRIs for the first name."""
        tables = self._tables("order_item", "order-item")
        g = _build(strategy, tables)
        ns = Namespace(PREFIX)

        assert (ns.TriplesMap_OrderItem, RDF.type, RR.TriplesMap) in g
        names = [
            str(n) for lt in g.objects(ns.TriplesMap_OrderItem, RR.logicalTable) for n in g.objects(lt, RR.tableName)
        ]
        assert names == ['"order_item"']

    def test_assignment_is_deterministic_across_runs(self, strategy):
        """Same input order must mint the same IRIs every time."""
        first = _build(strategy, self._tables("order_item", "order-item"))
        second = _build(strategy, self._tables("order_item", "order-item"))

        assert set(first.subjects(RDF.type, RR.TriplesMap)) == set(second.subjects(RDF.type, RR.TriplesMap))

    def test_ontology_and_r2rml_agree_on_class_iris(self, strategy):
        """The two builders must mint identical class IRIs for colliding names."""
        tables = self._tables("order_item", "order-item")
        onto, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        r2rml = strategy.build_r2rml(PREFIX, tables, novel, onto)

        onto_classes = set(onto.subjects(RDF.type, OWL.Class))
        r2rml_classes = {c for _, _, c in r2rml.triples((None, RR["class"], None))}
        assert r2rml_classes <= onto_classes, (
            f"R2RML references classes absent from the ontology: {r2rml_classes - onto_classes}"
        )
        assert len(r2rml_classes) == 2

    def test_ontology_gives_each_table_its_own_class_and_label(self, strategy):
        tables = self._tables("order_item", "order-item")
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])

        labels_by_class = {
            str(cls): sorted(str(lbl) for lbl in onto.objects(cls, RDFS.label))
            for cls in onto.subjects(RDF.type, OWL.Class)
        }
        assert len(labels_by_class) == 2
        for cls, labels in labels_by_class.items():
            assert len(labels) == 1, f"{cls} carries labels from multiple tables: {labels}"

    def test_colliding_tables_get_distinct_property_iris(self, strategy):
        """Discriminated classes must carry discriminated property IRIs too."""
        tables = [
            CatalogTable(
                id="1",
                name="order_item",
                fullyQualifiedName="db.order_item",
                columns=[CatalogColumn(name="qty", dataType="INT")],
            ),
            CatalogTable(
                id="2",
                name="order-item",
                fullyQualifiedName="db.order-item",
                columns=[CatalogColumn(name="qty", dataType="INT")],
            ),
        ]
        g = _build(strategy, tables)

        predicates = {str(p) for _, _, p in g.triples((None, RR.predicate, None))}
        assert len(predicates) == 2, f"Property IRIs still collide: {predicates}"

    def test_shacl_config_agrees_with_ontology_class_iris(self, strategy):
        """generate_config_from_db must target the classes the ontology declares."""
        from coa_ontology.validation.shapes.config import generate_config_from_db

        tables = self._tables("order_item", "order-item")
        onto, _ = strategy._build_proposal_ontology(PREFIX, tables, [])
        config = generate_config_from_db(tables, PREFIX)

        onto_classes = set(onto.subjects(RDF.type, OWL.Class))
        shape_classes = {URIRef(c.class_uri) for c in config.classes}
        assert shape_classes <= onto_classes, f"Shapes target absent classes: {shape_classes - onto_classes}"
        assert len(shape_classes) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11: ontology / R2RML property parity
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOntologyR2rmlParity:
    """Every property the ontology declares must have a mapping behind it.

    A declared-but-unmapped property is worse than a missing one: it shows up in
    the class/property browser and in the TBox context handed to the NL→SPARQL
    LLM, so the model is encouraged to author SPARQL against a property Ontop
    cannot resolve. The query compiles and returns nothing, with no mapping-gap
    signal anywhere.
    """

    @staticmethod
    def _declared_properties(onto):
        return {str(p) for p in onto.subjects(RDF.type, OWL.ObjectProperty)} | {
            str(p) for p in onto.subjects(RDF.type, OWL.DatatypeProperty)
        }

    @staticmethod
    def _mapped_predicates(r2rml):
        return {str(p) for _, _, p in r2rml.triples((None, RR.predicate, None))}

    def _both(self, strategy, tables):
        onto, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
        r2rml = strategy.build_r2rml(PREFIX, tables, novel, onto)
        return onto, r2rml

    @staticmethod
    def _composite_fk_tables(*, arity: int = 2):
        cols = [("day", "VARCHAR"), ("hour", "INT"), ("minute", "INT")][:arity]
        parent = CatalogTable(
            id="1",
            name="time_slots",
            fullyQualifiedName="s.time_slots",
            columns=[CatalogColumn(name=n, dataType=t) for n, t in cols]
            + [CatalogColumn(name="room", dataType="VARCHAR")],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=[n for n, _ in cols])],
        )
        child = CatalogTable(
            id="2",
            name="bookings",
            fullyQualifiedName="s.bookings",
            columns=[CatalogColumn(name="booking_id", dataType="INT")]
            + [CatalogColumn(name=n, dataType=t) for n, t in cols]
            + [CatalogColumn(name="note", dataType="VARCHAR")],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["booking_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=[n for n, _ in cols],
                    referredColumns=[f"time_slots.{n}" for n, _ in cols],
                ),
            ],
        )
        return [parent, child]

    def test_composite_fk_leaves_no_unmapped_property(self, strategy):
        onto, r2rml = self._both(strategy, self._composite_fk_tables())

        unmapped = self._declared_properties(onto) - self._mapped_predicates(r2rml)
        assert unmapped == set(), f"ontology declares properties with no R2RML mapping: {sorted(unmapped)}"

    def test_three_column_composite_fk_leaves_no_unmapped_property(self, strategy):
        """The gap scaled with arity — a 3-column FK orphaned two properties."""
        onto, r2rml = self._both(strategy, self._composite_fk_tables(arity=3))

        unmapped = self._declared_properties(onto) - self._mapped_predicates(r2rml)
        assert unmapped == set(), f"ontology declares properties with no R2RML mapping: {sorted(unmapped)}"

    def test_absorbed_column_is_a_datatype_property_not_an_object_property(self, strategy):
        """The relationship is carried once; the other columns are plain literals."""
        onto, _ = self._both(strategy, self._composite_fk_tables())
        ns = Namespace(PREFIX)

        assert (ns.bookings_day, RDF.type, OWL.ObjectProperty) in onto
        assert (ns.bookings_hour, RDF.type, OWL.DatatypeProperty) in onto
        assert (ns.bookings_hour, RDF.type, OWL.ObjectProperty) not in onto

    def test_absorbed_column_range_matches_its_r2rml_datatype(self, strategy):
        """rdfs:range and rr:datatype must agree or Ontop's type reasoning drops rows."""
        onto, r2rml = self._both(strategy, self._composite_fk_tables())
        ns = Namespace(PREFIX)

        onto_range = onto.value(ns.bookings_hour, RDFS.range)
        r2rml_datatype = _object_map_datatype(r2rml, ns["TriplesMap_Bookings/POM_Hour"])
        assert onto_range == r2rml_datatype == XSD.integer

    def test_absorbed_column_documents_where_the_relationship_lives(self, strategy):
        onto, _ = self._both(strategy, self._composite_fk_tables())
        ns = Namespace(PREFIX)

        comment = str(onto.value(ns.bookings_hour, RDFS.comment))
        assert "composite foreign key" in comment
        assert "bookings_day" in comment

    # ── malformed composite FKs degrade consistently on BOTH sides ────────────

    @staticmethod
    def _malformed_tables(*, referred):
        return [
            CatalogTable(
                id="1",
                name="bookings",
                fullyQualifiedName="s.bookings",
                columns=[
                    CatalogColumn(name="day", dataType="VARCHAR"),
                    CatalogColumn(name="hour", dataType="INT"),
                ],
                tableConstraints=[
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["day", "hour"],
                        referredColumns=referred,
                    )
                ],
            )
        ]

    def test_length_mismatch_degrades_to_datatype_on_both_sides(self, strategy):
        """build_r2rml falls back to literals; the ontology must not claim a relationship."""
        onto, r2rml = self._both(strategy, self._malformed_tables(referred=["slots.day"]))
        ns = Namespace(PREFIX)

        assert (ns.bookings_day, RDF.type, OWL.DatatypeProperty) in onto
        assert (ns.bookings_day, RDF.type, OWL.ObjectProperty) not in onto
        assert onto.value(ns.bookings_day, RDFS.range) == XSD.string
        assert _object_map_datatype(r2rml, ns["TriplesMap_Bookings/POM_Day"]) == XSD.string

    def test_multi_table_targets_degrade_to_datatype_on_both_sides(self, strategy):
        onto, r2rml = self._both(strategy, self._malformed_tables(referred=["a.day", "b.hour"]))
        ns = Namespace(PREFIX)

        for local, expected in (("bookings_day", XSD.string), ("bookings_hour", XSD.integer)):
            assert (ns[local], RDF.type, OWL.DatatypeProperty) in onto
            assert onto.value(ns[local], RDFS.range) == expected

    def test_malformed_composite_fk_leaves_no_unmapped_property(self, strategy):
        onto, r2rml = self._both(strategy, self._malformed_tables(referred=["slots.day"]))

        unmapped = self._declared_properties(onto) - self._mapped_predicates(r2rml)
        assert unmapped == set(), f"unmapped after malformed-FK fallback: {sorted(unmapped)}"

    # ── the simple cases must be untouched ───────────────────────────────────

    def test_simple_fk_still_yields_an_object_property(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="orders",
                fullyQualifiedName="s.orders",
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
                    ),
                ],
            ),
            CatalogTable(
                id="2",
                name="customers",
                fullyQualifiedName="s.customers",
                columns=[CatalogColumn(name="id", dataType="INT")],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
            ),
        ]
        onto, r2rml = self._both(strategy, tables)
        ns = Namespace(PREFIX)

        assert (ns.orders_customerId, RDF.type, OWL.ObjectProperty) in onto
        assert _object_map_parent_tmap(r2rml, ns["TriplesMap_Orders/POM_CustomerId"]) == ns.TriplesMap_Customers
        assert self._declared_properties(onto) - self._mapped_predicates(r2rml) == set()

    def test_no_fk_table_has_full_parity(self, strategy):
        tables = [
            CatalogTable(
                id="1",
                name="statuses",
                fullyQualifiedName="s.statuses",
                columns=[
                    CatalogColumn(name="code", dataType="VARCHAR"),
                    CatalogColumn(name="label", dataType="VARCHAR"),
                ],
                tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["code"])],
            )
        ]
        onto, r2rml = self._both(strategy, tables)

        assert self._declared_properties(onto) - self._mapped_predicates(r2rml) == set()
