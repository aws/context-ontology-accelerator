# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Logical table naming for same-named tables across datasources.

A bare ``rr:tableName`` is unambiguous only while one table in the run answers to
that name. When two datasources both expose ``customers``, the bare form collapses
them in THREE artifacts at once — the R2RML mapping, the H2 validation schema, and
the VKG routing map — and a cross-source query then reads the wrong physical table
or fails outright.

The fix qualifies the shared name as ``"schema"."table"``. These tests pin the two
halves that must agree (mapping and schema.sql) and, just as importantly, pin that
a table whose name is UNIQUE still emits the exact bare literal it emitted before,
so no already-accepted namespace needs re-inducing to keep working.
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
    logical_table_names,
    reference_index,
    same_datasource_target_identity,
    schema_from_fqn,
    subject_template_names,
    table_identity,
    table_schema_prefix,
)
from coa_ontology.proposals import SchemaSqlGenerationError, _mapping_table_names, _tables_to_h2_ddl
from rdflib import RDF, Graph

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"


def _table(name: str, schema: str, ds_id: str = "src-1", *, source_schema: bool = True) -> CatalogTable:
    """A minimal one-column table with a PK, in database ``schema``."""
    return CatalogTable(
        id=f"{schema}.{name}",
        name=name,
        fullyQualifiedName=f"{schema}.{name}",
        datasourceId=ds_id,
        sourceSchema=schema if source_schema else None,
        columns=[CatalogColumn(name="id", dataType="INT")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["id"])],
    )


def _table_names(tables: list[CatalogTable]) -> set[str]:
    """Every ``rr:tableName`` literal the mapping emits for ``tables``."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    g = TableToOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, Graph())
    names: set[str] = set()
    for tmap in g.subjects(RDF.type, RR.TriplesMap):
        for lt in g.objects(tmap, RR.logicalTable):
            names.add(str(g.value(lt, RR.tableName)))
    return names


def _r2rml_with(*literals: str) -> str:
    """A minimal R2RML document declaring exactly these ``rr:tableName`` literals.

    The literals are SQL-delimited, so their double quotes need turtle escaping —
    ``"customers"`` reaches the parser as the turtle literal ``"\\"customers\\""``.
    """
    lines = ["@prefix rr: <http://www.w3.org/ns/r2rml#> ."]
    for i, literal in enumerate(literals):
        escaped = literal.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'<http://ex.org/TriplesMap_{i}> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "{escaped}" ] .')
    return "\n".join(lines)


class TestTableSchemaPrefix:
    def test_source_schema_preferred(self):
        assert table_schema_prefix(_table("claims", "public")) == "public"

    def test_falls_back_to_fully_qualified_name(self):
        """``_generate_schema_sql`` builds CatalogTable without sourceSchema; the fqn
        must still yield the same schema, or schema.sql and the mapping disagree."""
        assert table_schema_prefix(_table("claims", "public", source_schema=False)) == "public"

    def test_unqualified_name_has_no_schema(self):
        table = CatalogTable(id="1", name="claims", fullyQualifiedName="claims")
        assert table_schema_prefix(table) == ""


class TestLogicalTableNames:
    def test_unique_name_stays_bare(self):
        """Byte-identical to pre-fix output: no namespace is forced to re-induce."""
        tables = [_table("claims", "public"), _table("policies", "public")]
        assert set(logical_table_names(tables).values()) == {'"claims"', '"policies"'}

    def test_shared_name_is_schema_qualified(self):
        tables = [_table("customers", "public", "src-pg"), _table("customers", "crm", "src-glue")]
        assert set(logical_table_names(tables).values()) == {'"public"."customers"', '"crm"."customers"'}

    def test_only_the_shared_name_is_qualified(self):
        """A collision must not drag unrelated tables into qualified form."""
        tables = [
            _table("customers", "public", "src-pg"),
            _table("customers", "crm", "src-glue"),
            _table("claims", "public", "src-pg"),
        ]
        assert set(logical_table_names(tables).values()) == {
            '"public"."customers"',
            '"crm"."customers"',
            '"claims"',
        }

    def test_shared_name_with_no_resolvable_schema_stays_bare(self):
        """A half-qualified pair would be worse than the collision it half-fixes."""
        tables = [
            CatalogTable(id="a", name="customers", fullyQualifiedName="customers", datasourceId="src-a"),
            CatalogTable(id="b", name="customers", fullyQualifiedName="customers", datasourceId="src-b"),
        ]
        assert set(logical_table_names(tables).values()) == {'"customers"'}


class TestR2RMLEmitsLogicalNames:
    def test_shared_name_qualified_in_mapping(self):
        tables = [_table("customers", "public", "src-pg"), _table("customers", "crm", "src-glue")]
        assert _table_names(tables) == {'"public"."customers"', '"crm"."customers"'}

    def test_unique_names_unchanged_in_mapping(self):
        tables = [_table("claims", "public"), _table("policies", "public")]
        assert _table_names(tables) == {'"claims"', '"policies"'}


class TestDatasourceAwareIdentity:
    """Two datasources sharing a DATABASE name — the ordinary two-Postgres case,
    since ``public`` is every Postgres source's default schema. The fully-qualified
    name (``public.customers``) is identical for both, so the datasource id must
    enter the identity or the two tables fuse into one class / TriplesMap / logical
    table with half the data unreachable."""

    def test_identity_carries_the_datasource_id(self):
        a = _table("customers", "public", "src-pg1")
        b = _table("customers", "public", "src-pg2")
        assert table_identity(a) != table_identity(b)
        assert table_identity(a).startswith("src-pg1")
        assert table_identity(b).startswith("src-pg2")

    def test_same_schema_collision_gets_a_discriminator(self):
        """No real schema separates the two, so the logical name's schema segment is
        discriminated deterministically — safe because serve overwrites the schema
        from routing's sourceSchema before executing."""
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "public", "src-pg2")]
        names = set(logical_table_names(tables).values())
        assert len(names) == 2, names
        assert all(n.startswith('"public__') for n in names), names

    def test_same_schema_collision_ddl_agrees_with_mapping(self):
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "public", "src-pg2")]
        ddl = _tables_to_h2_ddl(tables)
        for name in logical_table_names(tables).values():
            assert f"CREATE TABLE IF NOT EXISTS {name} (" in ddl

    def test_same_schema_collision_without_source_schema_stays_bare(self):
        """A synthetic schema is only safe when serve can overwrite it from routing's
        sourceSchema; without sourceSchema the collision is left unresolved (logged),
        not papered over with a schema that does not exist in the executed SQL."""
        tables = [
            _table("customers", "public", "src-pg1", source_schema=False),
            _table("customers", "public", "src-pg2", source_schema=False),
        ]
        # fullyQualifiedName still yields "public", but sourceSchema is None, so the
        # discriminated form is withheld and both fall back to the bare name.
        assert set(logical_table_names(tables).values()) == {'"customers"'}

    def test_subject_templates_are_unique_per_identity(self):
        """Instance IRIs mint under the subject token; two tables sharing it fuse two
        real-world entities per PK value."""
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "public", "src-pg2")]
        tokens = set(subject_template_names(tables).values())
        assert len(tokens) == 2, tokens

    def test_reference_index_omits_the_ambiguous_bare_name(self):
        """A bare FK target two datasources answer to must not resolve to one of them
        arbitrarily — the bare form is absent, only the datasource-scoped identity
        resolves."""
        a = _table("customers", "public", "src-pg1")
        b = _table("customers", "public", "src-pg2")
        idx = reference_index([a, b])
        assert "customers" not in idx  # ambiguous bare name withheld
        assert idx[same_datasource_target_identity(a, "customers")] == table_identity(a)
        assert idx[same_datasource_target_identity(b, "customers")] == table_identity(b)


class TestGenerateSchemaSqlStampsDatasource:
    """``_generate_schema_sql`` builds CatalogTable straight from ``_catalog_to_tables``
    output, which sets neither datasourceId nor sourceSchema. It must stamp both the
    same way the induction and infer-constraints paths do, or two datasources'
    same-named tables collapse to one identity and the H2 DDL disagrees with the
    mapping's logical names."""

    def test_two_sources_same_table_name_yield_two_discriminated_tables(self, monkeypatch):
        import coa_ontology.induce_catalog as induce_catalog
        import coa_ontology.proposals as proposals

        def _catalog_for(ds_id):
            # Both datasources expose public.customers.
            return {
                "databases": [
                    {
                        "name": "public",
                        "tables": [
                            {
                                "name": "customers",
                                "columns": [{"name": "id", "type": "INT"}],
                                "primaryKey": {"columns": ["id"]},
                            }
                        ],
                    }
                ]
            }

        accepted = [
            {"metadata": {"datasource_ids": ["src-pg1"]}},
            {"metadata": {"datasource_ids": ["src-pg2"]}},
        ]
        monkeypatch.setattr(proposals.dynamo_store, "list_proposals", lambda **kw: accepted)
        monkeypatch.setattr(induce_catalog, "_fetch_catalog", lambda url, ds_id: _catalog_for(ds_id))
        monkeypatch.delenv("CATALOG_SOURCE", raising=False)

        ddl = proposals._generate_schema_sql("ns", "ont-1")
        assert ddl is not None
        # Two distinct discriminated tables, not one silently-skipped duplicate.
        create_lines = [ln for ln in ddl.splitlines() if ln.startswith("CREATE TABLE")]
        assert len(create_lines) == 2, create_lines
        assert len({ln for ln in create_lines}) == 2, create_lines
        assert all('"public__' in ln for ln in create_lines), create_lines


class TestH2DDLMatchesMapping:
    def test_shared_name_gets_a_schema_and_two_tables(self):
        """``CREATE TABLE IF NOT EXISTS`` used to skip the second same-named table,
        leaving Ontop to validate BOTH TriplesMaps against the first one's columns."""
        tables = [_table("customers", "public", "src-pg"), _table("customers", "crm", "src-glue")]
        ddl = _tables_to_h2_ddl(tables)
        assert 'CREATE SCHEMA IF NOT EXISTS "public";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "crm";' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "public"."customers"' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "crm"."customers"' in ddl

    def test_unique_name_ddl_unchanged(self):
        ddl = _tables_to_h2_ddl([_table("claims", "public")])
        assert 'CREATE TABLE IF NOT EXISTS "claims"' in ddl
        assert "CREATE SCHEMA" not in ddl

    def test_ddl_and_mapping_agree_on_every_table(self):
        """The invariant that matters: Ontop validates the mapping against this DDL,
        so every rr:tableName must be creatable by it."""
        tables = [
            _table("customers", "public", "src-pg"),
            _table("customers", "crm", "src-glue"),
            _table("claims", "public", "src-pg"),
        ]
        ddl = _tables_to_h2_ddl(tables)
        for name in _table_names(tables):
            assert f"CREATE TABLE IF NOT EXISTS {name} (" in ddl

    def test_dotted_table_name_does_not_corrupt_the_schema_declaration(self):
        """A dot inside ONE identifier is not a qualifier.

        Splitting the emitted literal textually produced ``CREATE SCHEMA IF NOT
        EXISTS "q1;`` — unbalanced quotes, so H2 rejects the whole file and Ontop
        validates the mapping against no schema at all, not just against one
        missing table.
        """
        ddl = _tables_to_h2_ddl([_table("q1.results", "reports")])
        assert "CREATE SCHEMA" not in ddl
        assert 'CREATE TABLE IF NOT EXISTS "q1.results" (' in ddl

    def test_dotted_table_name_inside_a_real_schema_declares_only_that_schema(self):
        tables = [_table("q1.results", "reports", "src-a"), _table("q1.results", "archive", "src-b")]
        ddl = _tables_to_h2_ddl(tables)
        assert 'CREATE SCHEMA IF NOT EXISTS "reports";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "archive";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "q1' not in ddl
        assert 'CREATE TABLE IF NOT EXISTS "reports"."q1.results" (' in ddl


class TestSchemaFromFqn:
    """One derivation of a table's SQL schema, shared by the routing stamp and
    :func:`table_schema_prefix`. The value reaches ``coa:sourceSchema`` and from
    there the schema slot of the SQL serve executes, so naming the wrong segment
    is not cosmetic — it addresses a schema the source does not have."""

    def test_two_part_name_yields_the_database(self):
        assert schema_from_fqn("public.customers") == "public"

    def test_three_part_name_yields_the_database_not_the_catalog(self):
        """The segment NEAREST the table is the schema. Taking the head put the
        catalog name in the schema position; the 2-part case is identical either
        way, which is how head-vs-tail diverged unnoticed between call sites."""
        assert schema_from_fqn("awsdatacatalog.public.customers") == "public"

    def test_bare_name_has_no_schema(self):
        assert schema_from_fqn("customers") == ""

    def test_missing_name_has_no_schema(self):
        assert schema_from_fqn(None) == ""

    def test_table_schema_prefix_uses_the_same_rule(self):
        table = CatalogTable(id="1", name="customers", fullyQualifiedName="awsdatacatalog.public.customers")
        assert table_schema_prefix(table) == schema_from_fqn(table.fullyQualifiedName)


class TestDDLAlignsToTheMapping:
    """``logical_table_names`` is set-dependent by design: a bare name is qualified
    only while another table in the run shares it. ``_generate_schema_sql`` re-derives
    over the union of ALL accepted proposals' datasources — a different set from the
    one that produced the mapping being stored beside it — so the two can name the
    same table differently. Ontop then fails VKG load for the WHOLE namespace, not
    just the disagreeing table, so the DDL is made a function of the mapping."""

    def test_mapping_names_are_read_back(self):
        turtle = _r2rml_with('"customers"', '"public"."orders"')
        assert _mapping_table_names(turtle) == {'"customers"', '"public"."orders"'}

    def test_unparseable_mapping_falls_back_to_catalog_names(self):
        """Unknown is not the same as empty: an empty set would mean "the mapping
        names nothing", which would misalign every table."""
        assert _mapping_table_names("this is not turtle {{{") is None
        assert _mapping_table_names(None) is None

    def test_derived_qualification_yields_to_the_mapping_s_bare_name(self):
        """The measured cross-catalog skew: the mapping was accepted while one source
        exposed ``customers``; a later accept re-derives over two sources and
        would qualify it. The mapping wins — it is what Ontop validates."""
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "public", "src-pg2")]
        assert all("public__" in n for n in logical_table_names(tables).values())
        ddl = _tables_to_h2_ddl(tables, {'"customers"'})
        assert 'CREATE TABLE IF NOT EXISTS "customers" (' in ddl
        assert "public__" not in ddl

    def test_mapping_qualification_wins_over_a_derived_bare_name(self):
        """The reverse skew — the mapping qualified a name this table set does not
        have to. Emitting the bare form would leave the mapping's logical table
        undeclared, which is the failure that kills the namespace."""
        ddl = _tables_to_h2_ddl([_table("customers", "public", "src-pg1")], {'"public__deadbeef"."customers"'})
        assert 'CREATE SCHEMA IF NOT EXISTS "public__deadbeef";' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "public__deadbeef"."customers" (' in ddl

    def test_ambiguous_reverse_skew_declares_both_literals(self):
        """Two mapping literals end in this table's bare name. Both are declared by
        Ontop's mapping, so BOTH must appear in schema.sql — emitting only the bare
        catalog-derived name leaves both qualified literals undeclared, which fails
        VKG load for the whole namespace. The coverage guard emits a CREATE for
        every mapping literal, reusing the matching table's columns."""
        ddl = _tables_to_h2_ddl(
            [_table("customers", "public", "src-pg1")],
            {'"a"."customers"', '"b"."customers"'},
        )
        assert 'CREATE TABLE IF NOT EXISTS "a"."customers" (' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "b"."customers" (' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "a";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "b";' in ddl

    def test_h2_ddl_covers_every_mapping_literal(self):
        """The CRITICAL mixed-version state: the merged mapping (concatenation of
        every accepted proposal's stored r2rml) declares three literals for two
        physical tables because one proposal was accepted while ``customers`` was
        bare and another while it was qualified. Every literal — including the
        surplus bare ``"customers"`` — must get a CREATE, or Ontop rejects the
        namespace at load. Red before the coverage guard."""
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "crm", "src-pg2")]
        mapping = {'"public"."customers"', '"customers"', '"crm"."customers"'}
        ddl = _tables_to_h2_ddl(tables, mapping)
        created = {ln for ln in ddl.splitlines() if ln.startswith("CREATE TABLE")}
        for literal in mapping:
            assert any(f"CREATE TABLE IF NOT EXISTS {literal} (" in ln for ln in created), (
                f"mapping literal {literal} has no CREATE — would fail VKG load"
            )

    def test_uncoverable_mapping_literal_raises(self):
        """A mapping literal whose bare name matches no physical table cannot be
        described, so schema.sql generation raises loudly at generation time rather
        than emitting a mapping Ontop will silently reject at VKG load."""
        with pytest.raises(SchemaSqlGenerationError, match="no matching physical table"):
            _tables_to_h2_ddl([_table("customers", "public", "src-pg1")], {'"orders"'})

    def test_unknown_mapping_keeps_the_derived_names(self):
        tables = [_table("customers", "public", "src-pg"), _table("customers", "crm", "src-glue")]
        assert _tables_to_h2_ddl(tables, None) == _tables_to_h2_ddl(tables)

    def test_qualify_on_collision_emits_warning(self, caplog):
        """F9: the ordinary qualification path (a bare name shared across
        datasources, each with a DISTINCT schema) must log the shape change, so an
        operator diagnosing a namespace whose persisted rr:tableName changed shape
        has a signal. main logged the equivalent from proposals; this restores it
        at the disambiguation point."""
        tables = [_table("customers", "public", "src-pg1"), _table("customers", "crm", "src-pg2")]
        with caplog.at_level("WARNING"):
            logical_table_names(tables)
        assert "logical_table_name_qualified_on_collision" in caplog.text

    def test_pre_revert_qualified_mapping_still_covered(self):
        """F2: a namespace accepted under the pre-revert UNCONDITIONAL-qualification
        rule (issue #149, on main) persisted a QUALIFIED rr:tableName (e.g.
        "public"."customers") even for a table whose name is unique. This MR reverts
        induction to bare-when-unique, so the current catalog derives "customers" —
        but the STORED mapping still declares the qualified literal. That persisted
        mapping must still load: _align_to_mapping picks the stored qualified name
        (the reverse-skew candidate) and _tables_to_h2_ddl covers it. This is the
        'byte-identical relative to pre-#149' migration path proven executable."""
        tables = [_table("customers", "public", "src-pg1")]
        # What the pre-#149 mapping persisted for this now-unique table:
        pre_revert_mapping = {'"public"."customers"'}
        ddl = _tables_to_h2_ddl(tables, pre_revert_mapping)
        assert 'CREATE TABLE IF NOT EXISTS "public"."customers" (' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "public";' in ddl

    def test_two_tables_collapsing_onto_one_name_are_declared_once_and_reported(self, caplog):
        """H2 makes a repeated ``CREATE TABLE IF NOT EXISTS`` a silent no-op, so
        emitting it would read as two declarations while behaving as one — and the
        second table's columns are NOT what Ontop validates against. Declare once
        and say so."""
        wide = _table("customers", "public", "src-pg1")
        narrow = _table("customers", "public", "src-pg2")
        narrow.columns = [CatalogColumn(name="id", dataType="INT"), CatalogColumn(name="email", dataType="VARCHAR")]
        with caplog.at_level("WARNING"):
            ddl = _tables_to_h2_ddl([wide, narrow], {'"customers"'})
        assert len([ln for ln in ddl.splitlines() if ln.startswith("CREATE TABLE")]) == 1
        assert "different columns" in caplog.text

    def test_generate_schema_sql_threads_the_mapping_through(self, monkeypatch):
        """End to end: the accept path hands ``_generate_schema_sql`` the very
        mapping it is about to write to S3."""
        import coa_ontology.induce_catalog as induce_catalog
        import coa_ontology.proposals as proposals

        catalog = {
            "databases": [
                {
                    "name": "public",
                    "tables": [
                        {
                            "name": "customers",
                            "columns": [{"name": "id", "type": "INT"}],
                            "primaryKey": {"columns": ["id"]},
                        }
                    ],
                }
            ]
        }
        accepted = [
            {"metadata": {"datasource_ids": ["src-pg1"]}},
            {"metadata": {"datasource_ids": ["src-pg2"]}},
        ]
        monkeypatch.setattr(proposals.dynamo_store, "list_proposals", lambda **kw: accepted)
        monkeypatch.setattr(induce_catalog, "_fetch_catalog", lambda url, ds_id: catalog)
        monkeypatch.delenv("CATALOG_SOURCE", raising=False)

        ddl = proposals._generate_schema_sql("ns", "ont-1", _r2rml_with('"customers"'))
        assert ddl is not None
        # One table, named the way the mapping names it — NOT the two synthetic
        # discriminated tables the union of both datasources would derive.
        create_lines = [ln for ln in ddl.splitlines() if ln.startswith("CREATE TABLE")]
        assert len(create_lines) == 1, create_lines
        assert create_lines[0].startswith('CREATE TABLE IF NOT EXISTS "customers" ('), create_lines
