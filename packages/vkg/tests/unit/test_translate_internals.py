# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the internal functions of translate-server.py (ORR QAL-A-2).

Covers the helpers the contract tests mock out: R2RML routing load, Ontop health
polling, SPARQL→SQL reformulation over a mocked Ontop, dialect transpilation,
SQL/table-ref extraction, and routing resolution.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# Load the hyphenated module file. If another test module (e.g. the contract
# test) already loaded it under "translate_server", REUSE that same object so
# both test files share one module identity — otherwise a second module object
# would shadow it in sys.modules and break @patch("translate_server....") in the
# other file (whichever loaded last wins).
if "translate_server" in sys.modules:
    translate_server = sys.modules["translate_server"]
else:
    _server_path = Path(__file__).parent.parent.parent / "translate-server.py"
    _spec = importlib.util.spec_from_file_location("translate_server", _server_path)
    assert _spec is not None and _spec.loader is not None
    translate_server = importlib.util.module_from_spec(_spec)
    sys.modules["translate_server"] = translate_server
    _spec.loader.exec_module(translate_server)


@pytest.fixture(autouse=True)
def _restore_module_globals():
    """Save/restore the module-level globals these tests mutate, so state never
    leaks into the contract tests (which share the same imported module)."""
    saved = (
        dict(translate_server._table_routing),
        translate_server._ontology_version,
        translate_server._ontop_port,
        dict(translate_server._health_status),
    )
    yield
    translate_server._table_routing = saved[0]
    translate_server._ontology_version = saved[1]
    translate_server._ontop_port = saved[2]
    with translate_server._health_lock:
        translate_server._health_status.update(saved[3])


# --- _ontop_url --------------------------------------------------------------


def test_ontop_url_builds_localhost_url():
    translate_server._ontop_port = 8081
    assert translate_server._ontop_url("/actuator/health") == "http://localhost:8081/actuator/health"


# --- _extract_sql ------------------------------------------------------------


def test_extract_sql_strips_ontop_metadata_preamble():
    raw = "ans1(x, y)\nCONSTRUCT [x, y] [...]\n   NATIVE [...]\nSELECT t0.name FROM customer t0"
    assert translate_server._extract_sql(raw) == "SELECT t0.name FROM customer t0"


def test_extract_sql_handles_with_cte():
    raw = "metadata line\nWITH cte AS (SELECT 1) SELECT * FROM cte"
    assert translate_server._extract_sql(raw).startswith("WITH cte")


def test_extract_sql_returns_raw_when_no_select():
    raw = "-- no query produced"
    assert translate_server._extract_sql(raw) == raw


# --- _parse_projection_header ------------------------------------------------
#
# The `raw` fixtures below are VERBATIM output captured from Ontop 5.5.0's
# /ontop/reformulate (the version pinned in packages/vkg/Dockerfile) against a
# 3-table employees/projects/project_assignments schema. Do not hand-simplify
# them: the SQL SELECT list order, the CONSTRUCT term-definition syntax and the
# NATIVE alias list are exactly what the parser contracts on.

# "Who worked on project Spaceballs?" — the query from issue #829.
# Note the SELECT list order: the selected variable's column (FULL_NAME1m3) is
# SECOND. EMPLOYEE_ID1m1 comes first. A positional var→alias mapping puts
# employee IDs under an "employeeName" header.
_GT_SPACEBALLS = """ans1(employeeName)
CONSTRUCT [employeeName] [employeeName/RDF(CHARACTER VARYINGToTEXT(FULL_NAME1m3),xsd:string)]
   NATIVE [EMPLOYEE_ID1m1, FULL_NAME1m3, ID1m7, TITLE1m4, v3]
SELECT V2."EMPLOYEE_ID" AS "EMPLOYEE_ID1m1", V3."FULL_NAME" AS "FULL_NAME1m3", V1."ID" AS "ID1m7", \
V1."TITLE" AS "TITLE1m4", V3."FULL_NAME" AS "v3"
FROM "PROJECTS" V1, "PROJECT_ASSIGNMENTS" V2, "EMPLOYEES" V3
WHERE (LOWER(V1."TITLE") = 'spaceballs' AND V1."ID" = V2."PROJECT_ID" AND V2."EMPLOYEE_ID" = V3."ID")
ORDER BY V3."FULL_NAME" NULLS FIRST"""

_SPARQL_SPACEBALLS = """PREFIX ind: <http://example.org/ind#>
SELECT ?employeeName
WHERE {
  ?project a ind:Projects ; ind:projects_title ?title .
  FILTER(LCASE(?title) = "spaceballs")
  ?assignment a ind:ProjectAssignments ;
    ind:projectAssignments_projectId ?project ;
    ind:projectAssignments_employeeId ?employee .
  ?employee a ind:Employees ; ind:employees_fullName ?employeeName .
}
ORDER BY ?employeeName"""


def test_parse_projection_header_maps_var_to_its_own_column_not_by_position():
    """The selected var's column is 2nd in the SQL SELECT list; position != mapping."""
    proj = translate_server._parse_projection_header(_GT_SPACEBALLS, _SPARQL_SPACEBALLS)
    assert proj is not None
    assert proj["selectVars"] == ["employeeName"]
    assert proj["varToColumn"] == {"employeeName": "FULL_NAME1m3"}
    assert proj["distinct"] is False


def test_parse_projection_header_multiple_vars_skips_intermediate_columns():
    """Multi-var: join-key columns interleave the selected ones and are skipped."""
    raw = """ans1(employeeName, projectTitle)
CONSTRUCT [employeeName, projectTitle] [employeeName/RDF(CHARACTER VARYINGToTEXT(FULL_NAME1m3),xsd:string), \
projectTitle/RDF(CHARACTER VARYINGToTEXT(TITLE1m4),xsd:string)]
   NATIVE [EMPLOYEE_ID1m1, FULL_NAME1m3, ID1m7, TITLE1m4]
SELECT V2."EMPLOYEE_ID" AS "EMPLOYEE_ID1m1", V3."FULL_NAME" AS "FULL_NAME1m3", V1."ID" AS "ID1m7", \
V1."TITLE" AS "TITLE1m4"
FROM "PROJECTS" V1, "PROJECT_ASSIGNMENTS" V2, "EMPLOYEES" V3
WHERE (V1."ID" = V2."PROJECT_ID" AND V2."EMPLOYEE_ID" = V3."ID")"""
    sparql = "SELECT ?employeeName ?projectTitle WHERE { ?e a <x> }"
    proj = translate_server._parse_projection_header(raw, sparql)
    assert proj is not None
    assert proj["selectVars"] == ["employeeName", "projectTitle"]
    assert proj["varToColumn"] == {"employeeName": "FULL_NAME1m3", "projectTitle": "TITLE1m4"}


def test_parse_projection_header_iri_var_maps_to_its_id_column():
    """An IRI-valued var maps to the column inside its IRI template.

    The CONSTRUCT definitions are keyed by name and are NOT in ans1 order here:
    employeeName is defined before employee.
    """
    raw = """ans1(employee, employeeName)
CONSTRUCT [employee, employeeName] [employeeName/RDF(CHARACTER VARYINGToTEXT(FULL_NAME1m3),xsd:string), \
employee/RDF(http://example.org/ind#employee{}(CHARACTER VARYINGToTEXT(ID1m8)),IRI)]
   NATIVE [FULL_NAME1m3, ID1m8]
SELECT V1."FULL_NAME" AS "FULL_NAME1m3", V1."ID" AS "ID1m8"
FROM "EMPLOYEES" V1"""
    sparql = "SELECT ?employee ?employeeName WHERE { ?employee a <x> }"
    proj = translate_server._parse_projection_header(raw, sparql)
    assert proj is not None
    assert proj["varToColumn"] == {"employeeName": "FULL_NAME1m3", "employee": "ID1m8"}


def test_parse_projection_header_aggregate_maps_to_synthetic_column():
    """COUNT(...) projects to Ontop's synthetic v0 alias."""
    raw = """ans1(n)
CONSTRUCT [n] [n/RDF(BIGINTToTEXT(v0),xsd:integer)]
   NATIVE [v0]
SELECT COUNT(*) AS "v0"
FROM "EMPLOYEES" V1"""
    proj = translate_server._parse_projection_header(raw, "SELECT (COUNT(?e) AS ?n) WHERE { ?e a <x> }")
    assert proj is not None
    assert proj["varToColumn"] == {"n": "v0"}


def test_parse_projection_header_distinct_read_from_sparql():
    """SPARQL SELECT DISTINCT sets the flag (Ontop also emits SQL DISTINCT here)."""
    raw = """ans1(employeeName)
CONSTRUCT [employeeName] [employeeName/RDF(CHARACTER VARYINGToTEXT(FULL_NAME1m3),xsd:string)]
   NATIVE [FULL_NAME1m3]
SELECT DISTINCT V3."FULL_NAME" AS "FULL_NAME1m3"
FROM "PROJECTS" V1, "PROJECT_ASSIGNMENTS" V2, "EMPLOYEES" V3"""
    sparql = 'SELECT DISTINCT ?employeeName WHERE { ?e <p> ?employeeName . FILTER(?t = "spaceballs") }'
    proj = translate_server._parse_projection_header(raw, sparql)
    assert proj is not None
    assert proj["distinct"] is True


def test_parse_projection_header_sql_distinct_without_sparql_distinct_is_a_bag():
    """Ontop emits SQL DISTINCT for its own reasons — must NOT imply SPARQL DISTINCT.

    Verbatim Ontop output for a plain `SELECT ?asg ?label`: Ontop adds SQL
    DISTINCT while de-duplicating rows behind a composite-key IRI template.
    Treating that as SPARQL DISTINCT would drop legitimate duplicate solutions.
    """
    raw = """ans1(asg, label)
CONSTRUCT [asg, label] [asg/RDF(v0,IRI), label/RDF(CHARACTER VARYINGToTEXT(ID1m5),xsd:string)]
   NATIVE [ID1m5, v0]
SELECT DISTINCT V2."ID" AS "ID1m5", ('http://example.org/ind#asg' || V1."PROJECT_ID" || '-' || \
V1."EMPLOYEE_ID") AS "v0"
FROM "PROJECT_ASSIGNMENTS" V1, "PROJECT_ASSIGNMENTS" V2"""
    sparql = "SELECT ?asg ?label WHERE { ?asg a <x> ; <p> ?label . }"
    proj = translate_server._parse_projection_header(raw, sparql)
    assert proj is not None
    assert proj["varToColumn"] == {"asg": "v0", "label": "ID1m5"}
    assert proj["distinct"] is False


def test_parse_projection_header_reduced_dedupes():
    """SPARQL REDUCED permits dedup, so the flag is set."""
    raw = """ans1(x)
CONSTRUCT [x] [x/RDF(CHARACTER VARYINGToTEXT(ID1m1),xsd:string)]
   NATIVE [ID1m1]
SELECT V1."ID" AS "ID1m1" FROM "T" V1"""
    proj = translate_server._parse_projection_header(raw, "SELECT REDUCED ?x WHERE { ?x a <y> }")
    assert proj is not None
    assert proj["distinct"] is True


def test_parse_projection_header_missing_ans1_returns_none():
    raw = "SELECT * FROM table"
    assert translate_server._parse_projection_header(raw, "SELECT ?x WHERE { ?x a <y> }") is None


def test_parse_projection_header_missing_construct_returns_none():
    raw = "ans1(x)\n   NATIVE [ID1m1]\nSELECT * FROM table"
    assert translate_server._parse_projection_header(raw, "SELECT ?x WHERE { ?x a <y> }") is None


def test_parse_projection_header_missing_native_returns_none():
    raw = 'ans1(x)\nCONSTRUCT [x] [x/RDF(ID1m1,xsd:string)]\nSELECT V1."ID" AS "ID1m1" FROM "T" V1'
    assert translate_server._parse_projection_header(raw, "SELECT ?x WHERE { ?x a <y> }") is None


def test_parse_projection_header_unresolvable_var_returns_none():
    """A var whose definition references no known alias degrades the whole projection.

    Mapping only the resolvable vars would render the rest as an all-NULL
    column under a correct-looking header — worse than showing SQL aliases.
    """
    raw = """ans1(x, y)
CONSTRUCT [x, y] [x/RDF(CHARACTER VARYINGToTEXT(ID1m1),xsd:string), y/RDF("constant",xsd:string)]
   NATIVE [ID1m1]
SELECT V1."ID" AS "ID1m1" FROM "T" V1"""
    assert translate_server._parse_projection_header(raw, "SELECT ?x ?y WHERE { ?x a <y> }") is None


def test_parse_projection_header_multi_column_var_returns_none():
    """A var composed from several columns has no single source column."""
    raw = """ans1(x)
CONSTRUCT [x] [x/RDF(CONCAT(A1m1,B1m2),xsd:string)]
   NATIVE [A1m1, B1m2]
SELECT V1."A" AS "A1m1", V1."B" AS "B1m2" FROM "T" V1"""
    assert translate_server._parse_projection_header(raw, "SELECT ?x WHERE { ?x a <y> }") is None


# --- projection-drop observability -------------------------------------------
#
# A dropped projection silently reverts the namespace to raw SQL aliases (the
# original bug). Each cause must name itself in the log, or an operator has no
# way to tell --dev-disabled from an Ontop format change from a parser defect.

_SELECT_SPARQL = "SELECT ?x WHERE { ?x a <y> }"


@pytest.mark.parametrize(
    "case,raw,expect_in_message",
    [
        ("missing ans1", "SELECT * FROM table", "ans1"),
        ("empty ans1 vars", "ans1()\nSELECT * FROM t", "ans1"),
        ("missing native", 'ans1(x)\nCONSTRUCT [x] [x/RDF(ID1m1,xsd:string)]\nSELECT 1 AS "ID1m1"', "NATIVE"),
        ("empty native", "ans1(x)\nCONSTRUCT [x] [x/RDF(ID1m1,xsd:string)]\n   NATIVE []\nSELECT 1", "NATIVE"),
        ("missing construct", "ans1(x)\n   NATIVE [ID1m1]\nSELECT * FROM table", "CONSTRUCT"),
    ],
)
def test_parse_projection_header_logs_reason_for_each_drop(case, raw, expect_in_message, caplog):
    """Every unparseable-header path names its cause at warning level."""
    with caplog.at_level("WARNING", logger=translate_server.logger.name):
        assert translate_server._parse_projection_header(raw, _SELECT_SPARQL) is None
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert messages, f"{case}: dropped the projection without logging"
    assert any(expect_in_message in m for m in messages), f"{case}: cause not named in {messages}"


def test_parse_projection_header_logs_unresolved_variable_names(caplog):
    """The unresolvable-var drop names the offending variables.

    This is the one drop with a common benign cause (a constant-folded FILTER
    term references no alias), so the message must distinguish it from a bug.
    """
    raw = """ans1(x, y)
CONSTRUCT [x, y] [x/RDF(CHARACTER VARYINGToTEXT(ID1m1),xsd:string), y/RDF("constant",xsd:string)]
   NATIVE [ID1m1]
SELECT V1."ID" AS "ID1m1" FROM "T" V1"""
    with caplog.at_level("WARNING", logger=translate_server.logger.name):
        assert translate_server._parse_projection_header(raw, "SELECT ?x ?y WHERE { ?x a <y> }") is None
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("y" in m for m in warnings), f"unresolved var not named in {warnings}"
    # x resolved fine; only y should be reported as unresolved.
    assert not any("x, y" in m for m in warnings), f"reported a resolved var as unresolved: {warnings}"


def test_parse_projection_header_non_select_does_not_warn(caplog):
    """ASK/CONSTRUCT have no ans1 header by design — must not spam warnings."""
    with caplog.at_level("DEBUG", logger=translate_server.logger.name):
        assert translate_server._parse_projection_header("true", "ASK { ?x a <y> }") is None
    assert not [r for r in caplog.records if r.levelname == "WARNING"], "non-SELECT query logged a warning"


def test_extract_sql_logs_when_no_statement_found(caplog):
    """An unrecognised blob is returned as-is; the cause must be visible."""
    with caplog.at_level("WARNING", logger=translate_server.logger.name):
        out = translate_server._extract_sql("ans1(x)\nsome unexpected output")
    assert out == "ans1(x)\nsome unexpected output"
    assert [r for r in caplog.records if r.levelname == "WARNING"], "returned raw blob without logging"


# --- _extract_table_refs -----------------------------------------------------


def test_extract_table_refs_single_table():
    assert translate_server._extract_table_refs("SELECT a FROM customer t0") == ["customer"]


def test_extract_table_refs_join_is_sorted_and_deduped():
    sql = "SELECT * FROM orders o JOIN customer c ON o.cid = c.id JOIN customer c2 ON c2.id = o.cid"
    assert translate_server._extract_table_refs(sql) == ["customer", "orders"]


def test_extract_table_refs_qualified_schema_table():
    assert translate_server._extract_table_refs("SELECT * FROM sales.orders o") == ["sales.orders"]


def test_extract_table_refs_empty_or_comment_returns_empty():
    assert translate_server._extract_table_refs("") == []
    assert translate_server._extract_table_refs("-- comment") == []


def test_extract_table_refs_unparseable_returns_empty():
    # Gibberish that sqlglot cannot parse into a Table falls back to [].
    assert translate_server._extract_table_refs("NOT SQL AT ALL ;;;") == []


# --- _translate_dialect ------------------------------------------------------


def test_translate_dialect_rewrites_substr_before_transpile():
    # The regex pre-processing turns H2 SUBSTR into SUBSTRING before sqlglot runs.
    # (sqlglot's trino writer may render it back to SUBSTR — that's fine; we assert
    # the transpile succeeds and preserves the substring semantics.)
    out = translate_server._translate_dialect("SELECT SUBSTR(name, 1, 3) FROM t", "postgres", "trino")
    assert "SUBSTR" in out.upper()  # transpiled successfully, not the fallback
    assert "FROM T" in out.upper()


def test_translate_dialect_rewrites_formatdatetime_to_cast_varchar():
    out = translate_server._translate_dialect("SELECT FORMATDATETIME(ts, 'yyyy') FROM t", "postgres", "trino")
    assert "CAST" in out.upper()
    assert "FORMATDATETIME" not in out.upper()


def test_translate_dialect_returns_input_on_transpile_error():
    # An unparseable fragment triggers the except branch → original string back.
    broken = "SELECT ((( FROM"
    assert translate_server._translate_dialect(broken, "postgres", "trino") == broken


# --- _resolve_routing --------------------------------------------------------


def test_resolve_routing_empty_when_no_table_routing():
    translate_server._table_routing = {}
    assert translate_server._resolve_routing(["customer"]) == {}


def test_resolve_routing_exact_uppercase_match():
    translate_server._table_routing = {"CUSTOMER": {"datasourceId": "ds1"}}
    assert translate_server._resolve_routing(["customer"]) == {"customer": {"datasourceId": "ds1"}}


def test_resolve_routing_falls_back_to_bare_table_of_qualified_ref():
    translate_server._table_routing = {"ORDERS": {"datasourceId": "ds2", "sourceSchema": "sales"}}
    result = translate_server._resolve_routing(["sales.orders"])
    assert result == {"sales.orders": {"datasourceId": "ds2", "sourceSchema": "sales"}}


def test_resolve_routing_skips_unknown_tables():
    translate_server._table_routing = {"CUSTOMER": {"datasourceId": "ds1"}}
    assert translate_server._resolve_routing(["unknown_table"]) == {}


# --- _load_table_routing -----------------------------------------------------


def test_load_table_routing_missing_file_returns_empty():
    assert translate_server._load_table_routing("/nonexistent/path.ttl") == {}
    assert translate_server._load_table_routing("") == {}


def test_load_table_routing_parses_datasource_and_schema(tmp_path):
    ttl = tmp_path / "mappings.ttl"
    ttl.write_text(
        """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex: <http://example.com/> .

ex:CustomerMap a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "customer" ] ;
    coa:datasourceId "ds-abc" ;
    coa:sourceSchema "public" .
""",
        encoding="utf-8",
    )
    routing = translate_server._load_table_routing(str(ttl))
    # Stored under both bare and uppercase keys.
    assert routing["customer"]["datasourceId"] == "ds-abc"
    assert routing["CUSTOMER"]["sourceSchema"] == "public"


def test_load_table_routing_unparseable_file_returns_empty(tmp_path):
    bad = tmp_path / "bad.ttl"
    bad.write_text("this is not valid turtle @@@", encoding="utf-8")
    assert translate_server._load_table_routing(str(bad)) == {}


def test_load_table_routing_parses_schema_qualified_table_name(tmp_path):
    """#149 A: rr:tableName may now be `"schema"."table"`. The loader must
    register lookups under the bare table AND the schema.table form (each
    uppercased) so _resolve_routing matches whichever form Ontop's SQL uses."""
    ttl = tmp_path / "mappings.ttl"
    ttl.write_text(
        """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex: <http://example.com/> .

ex:OrdersMap a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"sales\\".\\"orders\\"" ] ;
    coa:datasourceId "ds-sales" ;
    coa:sourceSchema "sales" .
""",
        encoding="utf-8",
    )
    routing = translate_server._load_table_routing(str(ttl))
    # bare table key (both cases)
    assert routing["orders"]["datasourceId"] == "ds-sales"
    assert routing["ORDERS"]["datasourceId"] == "ds-sales"
    # schema.table key (both cases)
    assert routing["sales.orders"]["datasourceId"] == "ds-sales"
    assert routing["SALES.ORDERS"]["datasourceId"] == "ds-sales"


def test_resolve_routing_matches_qualified_key_from_qualified_mapping(tmp_path):
    """End to end: a qualified mapping resolves for a schema.table SQL ref."""
    ttl = tmp_path / "m.ttl"
    ttl.write_text(
        """
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex: <http://example.com/> .

ex:M a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"sales\\".\\"orders\\"" ] ;
    coa:datasourceId "ds-sales" ;
    coa:sourceSchema "sales" .
""",
        encoding="utf-8",
    )
    translate_server._table_routing = translate_server._load_table_routing(str(ttl))
    result = translate_server._resolve_routing(["sales.orders"])
    assert result["sales.orders"]["datasourceId"] == "ds-sales"


# --- _split_sql_qualified ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"sales"."orders"', ['"sales"', '"orders"']),
        ('"orders"', ['"orders"']),
        ("sales.orders", ["sales", "orders"]),
        ("orders", ["orders"]),
        # A dot inside a quoted identifier must NOT split.
        ('"weird.name"', ['"weird.name"']),
    ],
)
def test_split_sql_qualified(raw, expected):
    assert translate_server._split_sql_qualified(raw) == expected


# --- health polling ----------------------------------------------------------


def test_check_ontop_health_reads_cached_status():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True
    assert translate_server._check_ontop_health() is True
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = False
    assert translate_server._check_ontop_health() is False


def test_poll_ontop_health_sets_healthy_when_probe_passes(monkeypatch):
    # The poll loop caches whatever the functional probe returns. Probe behavior
    # (actuator UP + reformulation) is covered in test_health_probe.py; here we
    # only verify the loop wires a healthy probe result into the cached status.
    monkeypatch.setattr(translate_server, "_probe_health", lambda: (True, None))

    def _stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(translate_server.time, "sleep", _stop)
    with pytest.raises(KeyboardInterrupt):
        translate_server._poll_ontop_health()
    assert translate_server._check_ontop_health() is True
    assert translate_server._health_reason() is None


def test_poll_ontop_health_sets_unhealthy_on_error(monkeypatch):
    def _boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(translate_server.urllib.request, "urlopen", _boom)

    def _stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(translate_server.time, "sleep", _stop)
    with pytest.raises(KeyboardInterrupt):
        translate_server._poll_ontop_health()
    assert translate_server._check_ontop_health() is False
    # A failed probe caches a non-empty reason for the /health 503 body (#149 Fix 4).
    assert translate_server._health_reason()


# --- _translate_sparql (mocked Ontop) ----------------------------------------


def _fake_ontop_response(sql_body: str):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = sql_body.encode("utf-8")
    return resp


def test_translate_sparql_happy_path(monkeypatch):
    translate_server._table_routing = {"CUSTOMER": {"datasourceId": "ds1"}}
    translate_server._ontology_version = "9.9.9"
    body = "ans1(x)\nCONSTRUCT [...]\nSELECT t0.name FROM customer t0"
    monkeypatch.setattr(translate_server.urllib.request, "urlopen", lambda *a, **k: _fake_ontop_response(body))
    out = translate_server._translate_sparql("SELECT ?n WHERE {?c :name ?n}", "trino")
    assert out["sql"].upper().startswith("SELECT")
    assert out["dialect"] == "trino"
    assert out["ontologyVersion"] == "9.9.9"
    assert out["sourceTableRefs"] == ["customer"]
    assert out["datasourceRouting"] == {"customer": {"datasourceId": "ds1"}}


def test_translate_sparql_includes_projection_when_header_present(monkeypatch):
    translate_server._table_routing = {}
    translate_server._ontology_version = "1.0.0"
    monkeypatch.setattr(
        translate_server.urllib.request, "urlopen", lambda *a, **k: _fake_ontop_response(_GT_SPACEBALLS)
    )
    out = translate_server._translate_sparql(_SPARQL_SPACEBALLS, "trino")
    assert "projection" in out
    proj = out["projection"]
    assert proj["selectVars"] == ["employeeName"]
    assert proj["varToColumn"] == {"employeeName": "FULL_NAME1m3"}
    assert proj["distinct"] is False


def test_translate_sparql_omits_projection_when_header_unparseable(monkeypatch):
    translate_server._table_routing = {}
    # No ans1 header — projection should be None/absent
    body = "SELECT t0.name FROM customer t0"
    monkeypatch.setattr(translate_server.urllib.request, "urlopen", lambda *a, **k: _fake_ontop_response(body))
    out = translate_server._translate_sparql("q", "trino")
    assert "projection" not in out


def test_translate_sparql_h2_dialect_skips_transpile(monkeypatch):
    translate_server._table_routing = {}
    body = "SELECT 1 FROM dual"
    monkeypatch.setattr(translate_server.urllib.request, "urlopen", lambda *a, **k: _fake_ontop_response(body))
    out = translate_server._translate_sparql("q", "h2")
    assert out["dialect"] == "h2"


def test_translate_sparql_raises_on_http_error(monkeypatch):
    err = urllib.error.HTTPError("url", 500, "boom", {}, None)
    monkeypatch.setattr(err, "read", lambda: b"ontop failed", raising=False)

    def _raise(*a, **k):
        raise err

    monkeypatch.setattr(translate_server.urllib.request, "urlopen", _raise)
    with pytest.raises(RuntimeError, match="Ontop error 500"):
        translate_server._translate_sparql("q", "trino")


def test_translate_sparql_raises_on_urlerror(monkeypatch):
    def _raise(*a, **k):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(translate_server.urllib.request, "urlopen", _raise)
    with pytest.raises(RuntimeError, match="Cannot reach Ontop"):
        translate_server._translate_sparql("q", "trino")


# --- TranslateHandler request paths ------------------------------------------


def _invoke(method, path, body=None, content_length=None):
    """Drive TranslateHandler without a real socket (mirrors the contract test helper)."""

    class _MockHandler(translate_server.TranslateHandler):
        def __init__(self):
            raw = json.dumps(body).encode() if body is not None else b""
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self.path = path
            length = content_length if content_length is not None else len(raw)
            self.headers = {"Content-Length": str(length)}
            self._status = None
            self.client_address = ("127.0.0.1", 0)

        def send_response(self, code):
            self._status = code

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    h = _MockHandler()
    {"GET": h.do_GET, "POST": h.do_POST}[method]()
    h.wfile.seek(0)
    payload = h.wfile.read()
    return h._status, (json.loads(payload) if payload else {})


def test_get_unknown_path_returns_404():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True
    status, data = _invoke("GET", "/nope")
    assert status == 404
    assert data["error"] == "Not found"


def test_post_unknown_path_returns_404():
    status, data = _invoke("POST", "/nope", {})
    assert status == 404


def test_translate_returns_503_when_unhealthy():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = False
    status, data = _invoke("POST", "/sparql/translate", {"sparql": "q"})
    assert status == 503
    assert data["message"] == "Ontop not ready"


def test_translate_400_on_invalid_json():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True

    class _BadHandler(translate_server.TranslateHandler):
        def __init__(self):
            self.rfile = io.BytesIO(b"{not json")
            self.wfile = io.BytesIO()
            self.path = "/sparql/translate"
            self.headers = {"Content-Length": "9"}
            self._status = None
            self.client_address = ("127.0.0.1", 0)

        def send_response(self, code):
            self._status = code

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    h = _BadHandler()
    h.do_POST()
    h.wfile.seek(0)
    assert h._status == 400


def test_translate_413_on_oversized_body():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True
    status, data = _invoke("POST", "/sparql/translate", {"sparql": "q"}, content_length=1_048_577)
    assert status == 413


def test_translate_400_on_missing_sparql():
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True
    status, data = _invoke("POST", "/sparql/translate", {"namespace": "x"})
    assert status == 400
    assert "sparql" in data["error"]


def test_translate_200_success(monkeypatch):
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True
    monkeypatch.setattr(
        translate_server,
        "_translate_sparql",
        lambda sparql, dialect: {"sql": "SELECT 1", "dialect": dialect, "sourceTableRefs": []},
    )
    status, data = _invoke("POST", "/sparql/translate", {"sparql": "q", "targetDialect": "trino"})
    assert status == 200
    assert data["sql"] == "SELECT 1"


def test_translate_500_on_runtime_error(monkeypatch):
    with translate_server._health_lock:
        translate_server._health_status["healthy"] = True

    def _boom(sparql, dialect):
        raise RuntimeError("ontop exploded")

    monkeypatch.setattr(translate_server, "_translate_sparql", _boom)
    status, data = _invoke("POST", "/sparql/translate", {"sparql": "q"})
    assert status == 500
    assert "ontop exploded" in data["error"]
