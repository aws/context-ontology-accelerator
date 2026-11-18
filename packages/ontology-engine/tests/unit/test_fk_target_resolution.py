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

If they disagree, the shape asserts a class-typed reference against a column the
mapping emits as an ``rr:datatype`` literal, which violates on every row.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.data_catalog import (
    CatalogColumn,
    CatalogConstraint,
    CatalogTable,
    parse_referred_schema,
)
from coa_ontology.inducer.strategies.base import (
    RR,
    pascal_names_for,
    reference_index,
    resolve_fk_target_identity,
    subject_template_names,
    table_identity,
)
from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.validation.shapes.config import generate_config_from_db
from rdflib import OWL, RDF, RDFS, Graph, URIRef

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
        """The schema probe uses ``fullyQualifiedName`` when ``sourceSchema`` is absent."""
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


# A reference that names its schema (``SCHEMA.TABLE.COLUMN``).
#
# The sources service writes ``SCHEMA.TABLE`` for a target outside the referrer's
# own schema: a Snowflake key into another schema, a steward key, an inferred key
# whose bare name repeats in the source. The schema says which same-named table the
# key points at, so every artifact must resolve it inside that schema of the
# referrer's datasource and nowhere else. Bare references keep the three-probe
# order above.

DEGRADED: dict[str, set[str]] = {"range": set(), "parent": set(), "shape": set()}


def _targets(tables: list[CatalogTable]) -> dict[str, set[str]]:
    """Where the ontology range, the mapping join and the shape point ``orders.customer_id``."""
    strategy = TableToOntologyStrategy()
    onto, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
    mapping = strategy.build_r2rml(PREFIX, tables, novel, onto)
    config = generate_config_from_db(tables, PREFIX)
    return {
        "range": {str(o) for _, _, o in onto.triples((None, RDFS.range, None)) if str(o).startswith(PREFIX)},
        "parent": {str(o) for o in mapping.objects(None, RR.parentTriplesMap)},
        "shape": {
            c.params["target_class"] for cls in config.classes for c in cls.constraints if "target_class" in c.params
        },
    }


def _pointing_at(tables: list[CatalogTable], target: CatalogTable) -> dict[str, set[str]]:
    """What :func:`_targets` returns when all three artifacts point at ``target``."""
    local = pascal_names_for(tables)[table_identity(target)]
    return {"range": {f"{PREFIX}{local}"}, "parent": {f"{PREFIX}TriplesMap_{local}"}, "shape": {f"{PREFIX}{local}"}}


def _rigor_reference_template(tables: list[CatalogTable]) -> str | None:
    """The IRI template RIGOR's mapping gives ``orders.customer_id``, or ``None`` for a literal.

    The FK is declared an object property, as a RIGOR proposal would, so the writer's
    own resolution decides rather than the ontology's demotion.
    """
    onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
    prop = URIRef(f"{PREFIX}orders_customerId")
    onto.remove((prop, RDF.type, OWL.DatatypeProperty))
    onto.remove((prop, RDFS.range, None))
    onto.add((prop, RDF.type, OWL.ObjectProperty))
    mapping = RigorOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, onto)
    templates = [str(o) for o in mapping.objects(None, RR.template) if str(o).endswith('{"customer_id"}')]
    assert len(templates) <= 1, templates
    return templates[0] if templates else None


def _with_fk(child: CatalogTable, **fields: object) -> CatalogTable:
    """``child`` with its FK constraint's fields replaced."""
    constraints = [
        tc.model_copy(update=fields) if tc.constraintType == "FOREIGN_KEY" else tc
        for tc in child.tableConstraints or []
    ]
    return child.model_copy(update={"tableConstraints": constraints})


class TestParseReferredSchema:
    @pytest.mark.parametrize(
        ("ref", "schema"),
        [
            ("orders.order_id", ""),
            ("CRM.CUSTOMERS.ID", "CRM"),
            ("db.public.orders.order_id", "public"),
            ("orders", ""),
            ("", ""),
            # An empty schema segment names no schema.
            (".orders.order_id", ""),
            ("db..orders.order_id", ""),
            ("orders.", ""),
        ],
    )
    def test_parse_referred_schema_reads_the_segment_before_the_table(self, ref, schema):
        assert parse_referred_schema(ref) == schema


class TestSchemaQualifiedTarget:
    @pytest.mark.parametrize("target", ["CRM.customers", "ANALYTICS.CRM.customers"])
    def test_named_schema_wins_over_a_same_named_table_in_the_referrer_schema(self, target):
        """SALES has its own ``customers``; the key says CRM's. The referrer-schema probe
        used to win and joined orders to SALES.customers."""
        crm = _parent("customers", "CRM", "src-a")
        tables = [
            _parent("customers", "SALES", "src-a"),
            crm,
            _parent("customers", "MARKETING", "src-a"),
            _child("orders", "SALES", "src-a", target),
        ]
        assert _targets(tables) == _pointing_at(tables, crm)

    def test_named_schema_not_in_the_run_never_binds_to_another_schema(self):
        """The example from the bug: the source scans SALES and MARKETING, the key
        targets CRM.customers. The bare-name probe used to join orders to the run's
        only ``customers``, MARKETING's."""
        tables = [_parent("customers", "MARKETING", "src-a"), _child("orders", "SALES", "src-a", "CRM.customers")]
        assert _targets(tables) == DEGRADED

    @pytest.mark.parametrize("other_schema", ["CRM", "SALES"])
    def test_named_schema_never_binds_to_another_source(self, other_schema):
        """Only another source has a ``customers``: as ``CRM.customers`` it was the
        unique bare name, as ``SALES.customers`` it answered the any-datasource schema
        probe. Neither is the key's target."""
        tables = [_parent("customers", other_schema, "src-b"), _child("orders", "SALES", "src-a", "CRM.customers")]
        assert _targets(tables) == DEGRADED

    def test_named_schema_resolves_in_the_referrer_source_when_another_source_has_it_too(self):
        """Two sources both have CRM.customers. The bare name is ambiguous, so the key
        used to degrade; the named schema plus the key's own source pick one."""
        mine = _parent("customers", "CRM", "src-a")
        tables = [_parent("customers", "CRM", "src-b"), mine, _child("orders", "SALES", "src-a", "CRM.customers")]
        assert _targets(tables) == _pointing_at(tables, mine)

    @pytest.mark.parametrize("schema", ["CRM", "Crm"])
    def test_mixed_case_schema_matches_exactly(self, schema):
        """Snowflake and PostgreSQL keep a quoted ``"Crm"`` apart from ``CRM``, and
        sources stores both the key and the table's schema as the engine reports them,
        so the match is exact."""
        upper, mixed = _parent("customers", "CRM", "src-a"), _parent("customers", "Crm", "src-a")
        tables = [upper, mixed, _child("orders", "SALES", "src-a", f"{schema}.customers")]
        assert _targets(tables) == _pointing_at(tables, upper if schema == "CRM" else mixed)

    def test_bare_target_keeps_the_bare_name_fallback(self):
        """No schema in the reference: unchanged, the run's only ``customers`` is used."""
        marketing = _parent("customers", "MARKETING", "src-a")
        tables = [marketing, _child("orders", "SALES", "src-a", "customers")]
        assert _targets(tables) == _pointing_at(tables, marketing)

    def test_bare_target_in_a_source_that_stores_no_schema_still_resolves(self):
        """A catalog with no schema at all: names only, bare references, as before."""
        customers = _parent("customers", "x", "src-a").model_copy(
            update={"fullyQualifiedName": "customers", "sourceSchema": None}
        )
        orders = _child("orders", "x", "src-a", "customers").model_copy(
            update={"fullyQualifiedName": "orders", "sourceSchema": None}
        )
        tables = [customers, orders]
        assert _targets(tables) == _pointing_at(tables, customers)

    def test_cross_source_hint_with_a_named_schema_picks_that_schema(self):
        """An approved cross-source key into a source with public.customers AND
        crm.customers: the hint alone is ambiguous, the named schema is not."""
        crm = _parent("customers", "crm", "src-b")
        orders = _with_fk(
            _child("orders", "public", "src-a", "crm.customers"),
            relationshipType="AI_INFERRED",
            reviewStatus="APPROVED",
            targetDatasourceId="DS#src-b",
        )
        tables = [_parent("customers", "public", "src-b"), crm, _parent("customers", "crm", "src-a"), orders]
        assert _targets(tables) == _pointing_at(tables, crm)

    def test_composite_key_resolves_in_the_named_schema(self):
        def parent(schema: str) -> CatalogTable:
            return _parent("customers", schema, "src-a").model_copy(
                update={
                    "columns": [CatalogColumn(name="id", dataType="INT"), CatalogColumn(name="region", dataType="INT")],
                    "tableConstraints": [CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id", "region"])],
                }
            )

        crm = parent("CRM")
        orders = _child("orders", "SALES", "src-a", "CRM.customers").model_copy(
            update={
                "columns": [CatalogColumn(name=c, dataType="INT") for c in ("id", "customer_id", "region")],
                "tableConstraints": [
                    CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"]),
                    CatalogConstraint(
                        constraintType="FOREIGN_KEY",
                        columns=["customer_id", "region"],
                        referredColumns=["CRM.customers.id", "CRM.customers.region"],
                    ),
                ],
            }
        )
        tables = [parent("SALES"), crm, orders]
        assert _targets(tables) == _pointing_at(tables, crm)

    def test_rigor_mapping_points_at_the_named_schema(self):
        crm = _parent("customers", "CRM", "src-a")
        tables = [_parent("customers", "SALES", "src-a"), crm, _child("orders", "SALES", "src-a", "CRM.customers")]
        token = subject_template_names(tables)[table_identity(crm)]
        assert _rigor_reference_template(tables) == f'{PREFIX}{token}/{{"customer_id"}}'

    def test_rigor_mapping_degrades_a_named_schema_not_in_the_run(self):
        tables = [_parent("customers", "MARKETING", "src-a"), _child("orders", "SALES", "src-a", "CRM.customers")]
        assert _rigor_reference_template(tables) is None

    def test_resolver_returns_nothing_for_a_named_schema_not_in_the_run(self):
        tables = [_parent("customers", "MARKETING", "src-a"), _child("orders", "SALES", "src-a", "CRM.customers")]
        assert resolve_fk_target_identity(tables[1], "customers", reference_index(tables), None, "CRM") is None
        # The same call without the schema is the bare-name fallback.
        assert resolve_fk_target_identity(tables[1], "customers", reference_index(tables)) == table_identity(tables[0])


def _multi_fk_child(schema: str, refs: list[str]) -> CatalogTable:
    """``orders`` in ``schema`` of src-a whose ``customer_id`` carries one FK per entry of ``refs``."""
    child = _child("orders", schema, "src-a", "unused")
    pk = [tc for tc in child.tableConstraints or [] if tc.constraintType == "PRIMARY_KEY"]
    fks = [
        CatalogConstraint(constraintType="FOREIGN_KEY", columns=["customer_id"], referredColumns=[ref]) for ref in refs
    ]
    return child.model_copy(update={"tableConstraints": pk + fks})


class TestSchemaQualifiedTargetSelection:
    def test_keys_to_same_named_tables_in_two_schemas_both_survive(self):
        """One column with keys to CRM.customers and SALES.customers in its own source.
        The FK selection used to dedupe on the bare target, so the second key was
        dropped from the ontology, the mapping and the shape alike."""
        crm, sales = _parent("customers", "CRM", "src-a"), _parent("customers", "SALES", "src-a")
        tables = [crm, sales, _multi_fk_child("APP", ["CRM.customers.id", "SALES.customers.id"])]
        crm_at, sales_at = _pointing_at(tables, crm), _pointing_at(tables, sales)
        assert _targets(tables) == {k: crm_at[k] | sales_at[k] for k in crm_at}
        # Two relationships need two property names, and all three artifacts use the same two.
        onto, novel = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
        onto_props = {str(p) for p in onto.subjects(RDF.type, OWL.ObjectProperty) if "customerId" in str(p)}
        mapping = TableToOntologyStrategy().build_r2rml(PREFIX, tables, novel, onto)
        joined = {
            str(mapping.value(pom, RR.predicate))
            for pom, _, om in mapping.triples((None, RR.objectMap, None))
            if mapping.value(om, RR.parentTriplesMap) is not None
        }
        shape_props = {
            c.property_path
            for cls in generate_config_from_db(tables, PREFIX).classes
            for c in cls.constraints
            if "target_class" in c.params
        }
        assert len(onto_props) == 2, onto_props
        assert joined == onto_props, (joined, onto_props)
        assert shape_props == onto_props, (shape_props, onto_props)

    def test_bare_key_and_own_schema_key_stay_one_relationship(self):
        """``customers.id`` and ``SALES.customers.id`` written on a SALES table name the
        same table, so they remain one relationship under the plain property name."""
        sales = _parent("customers", "SALES", "src-a")
        tables = [sales, _multi_fk_child("SALES", ["customers.id", "SALES.customers.id"])]
        assert _targets(tables) == _pointing_at(tables, sales)
        onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, tables, [])
        props = {str(p) for p in onto.subjects(RDF.type, OWL.ObjectProperty) if "customerId" in str(p)}
        assert props == {f"{PREFIX}orders_customerId"}

    def test_cross_source_hint_and_a_named_schema_missing_there_never_bind_elsewhere(self):
        """The key names src-b's crm.customers; src-b only has public.customers. The
        referrer's own source has a crm.customers, but that is another source's table,
        so every artifact degrades."""
        orders = _with_fk(
            _child("orders", "public", "src-a", "crm.customers"),
            relationshipType="AI_INFERRED",
            reviewStatus="APPROVED",
            targetDatasourceId="DS#src-b",
        )
        tables = [_parent("customers", "public", "src-b"), _parent("customers", "crm", "src-a"), orders]
        assert _targets(tables) == DEGRADED
        assert _rigor_reference_template(tables) is None
        assert resolve_fk_target_identity(orders, "customers", reference_index(tables), "DS#src-b", "crm") is None

    def test_named_schema_resolves_a_target_with_a_three_part_name(self):
        """``fullyQualifiedName`` is sometimes ``catalog.schema.table``. The named-schema
        lookup used to build ``schema.table`` and miss it, degrading a resolvable key."""
        crm = _parent("customers", "CRM", "src-a").model_copy(update={"fullyQualifiedName": "ANALYTICS.CRM.customers"})
        sales = _parent("customers", "SALES", "src-a").model_copy(
            update={"fullyQualifiedName": "ANALYTICS.SALES.customers"}
        )
        tables = [sales, crm, _child("orders", "SALES", "src-a", "CRM.customers")]
        assert resolve_fk_target_identity(tables[2], "customers", reference_index(tables), None, "CRM") == (
            table_identity(crm)
        )
        assert _targets(tables) == _pointing_at(tables, crm)

    def test_rigor_order_keeps_same_named_tables_in_different_schemas(self):
        """RIGOR's FK ordering was keyed on the bare name, so one of two same-named
        tables never reached the processing order and was never induced."""
        crm, sales = _parent("customers", "CRM", "src-a"), _parent("customers", "SALES", "src-a")
        orders = _child("orders", "SALES", "src-a", "CRM.customers")
        ordered = RigorOntologyStrategy()._order_by_fk([orders, crm, sales])
        assert {table_identity(t) for t in ordered} == {table_identity(t) for t in (orders, crm, sales)}
        assert len(ordered) == 3
        assert ordered.index(orders) > ordered.index(crm)
