# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Translation Server - API facade for Ontop SPARQL-to-SQL translation.

Endpoints:
  POST /sparql/translate  - accepts {sparql, namespace, targetDialect}
                            returns {sql, dialect, ontologyVersion, sourceTableRefs}
  GET  /health            - 200 only when a canonical query reformulates to SQL
                            (functional probe, not just process-up), else 503.

SQL Extraction Strategy:
  Ontop 5.x (in --dev mode) exposes a /ontop/reformulate endpoint that accepts
  SPARQL and returns the reformulated SQL directly as the HTTP response body —
  without executing the query against any database.

  Why --dev mode is required and safe for production:
    - /ontop/reformulate is only available with --dev (no alternative config exists)
    - --dev also enables auto-restart on config file changes, but our container
      treats config as immutable after startup (downloaded from S3 once), so
      auto-restart never triggers unintentionally
    - No documented performance penalty, caching changes, or security concerns
    - See: https://ontop-vkg.org/guide/cli.html#ontop-endpoint

  Previous approaches considered and rejected:
    - X-Ontop-Reformulated-Query header: does not exist in Ontop 5.x
    - DEBUG log parsing (--dev + log file): fragile, disk growth risk, format-dependent
    - Structured query logging (ontop.queryLogging): still file-based, async flush issues
    - /ontop/reformulate: direct HTTP, no file I/O, no parsing — cleanest solution

Runtime note: this file runs on the container's system ``python3``, which is
Python 3.9 (Amazon Corretto AL2023 base image) — NOT the 3.12 the rest of the
repo targets. ``from __future__ import annotations`` is therefore mandatory:
without it a PEP 604 ``X | None`` annotation is evaluated at import time and
raises ``TypeError``, killing the server before it can serve /health.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

import sqlglot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("vkg-translate")

_ontology_version = os.environ.get("ONTOLOGY_VERSION", "unknown")
_ontop_port = int(os.environ.get("ONTOP_PORT", "8081"))
_listen_port = int(os.environ.get("ENDPOINT_PORT", "8080"))

# Table-to-datasource routing map, loaded from R2RML annotations at startup.
# Maps uppercase table name → {"datasourceId": ..., "sourceSchema": ...}
_table_routing: dict[str, dict[str, str]] = {}

# Cached health state (refreshed by background thread)
_health_status = {"healthy": False, "last_check": 0.0, "reason": "starting up"}
_health_lock = threading.Lock()
_HEALTH_TTL_SECONDS = 10


def _ontop_url(path):
    return f"http://localhost:{_ontop_port}{path}"


def _split_sql_qualified(name: str) -> list[str]:
    """Split a possibly schema-qualified SQL identifier into its parts.

    ``'"sales"."orders"'`` -> ``['"sales"', '"orders"']``; ``'"orders"'`` ->
    ``['"orders"']``; ``'sales.orders'`` -> ``['sales', 'orders']``. Splits only
    on dots that sit *outside* double-quoted segments, so a dot embedded in a
    quoted identifier (``"a.b"``) is preserved. Callers strip the quotes.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == '"':
            # A doubled "" inside a quoted identifier is an escaped quote.
            if in_quotes and i + 1 < len(name) and name[i + 1] == '"':
                buf.append('""')
                i += 2
                continue
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "." and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _load_table_routing(mappings_path: str) -> dict[str, dict[str, str]]:
    """Parse coa:datasourceId and coa:sourceSchema annotations from an R2RML file.

    Returns a dict mapping uppercase table names to their datasource routing info.
    Silently returns empty dict if the file is missing or unparseable.
    """
    if not mappings_path or not os.path.isfile(mappings_path):
        return {}
    try:
        from rdflib import Graph, Namespace

        RR = Namespace("http://www.w3.org/ns/r2rml#")
        SCL = Namespace("http://coa.amazon.com/vocab/coa#")

        g = Graph()
        g.parse(mappings_path, format="turtle")

        routing: dict[str, dict[str, str]] = {}
        for tmap in g.subjects(predicate=None, object=RR.TriplesMap):
            ds_id = str(next(g.objects(tmap, SCL.datasourceId), "")) or None
            schema = str(next(g.objects(tmap, SCL.sourceSchema), "")) or None
            # Get table name from logicalTable
            for lt in g.objects(tmap, RR.logicalTable):
                table_name = str(next(g.objects(lt, RR.tableName), ""))
                if table_name:
                    entry: dict[str, str] = {}
                    if ds_id:
                        entry["datasourceId"] = ds_id
                    if schema:
                        entry["sourceSchema"] = schema
                    if entry:
                        # rr:tableName may now be schema-qualified
                        # (`"schema"."table"`) to disambiguate same-named tables
                        # across schemas (#149 cause A). Parse it into its parts
                        # and register keys for every form _resolve_routing may
                        # look up: bare table, and schema.table — each also
                        # uppercased so lookups match regardless of whether
                        # sqlglot preserves or normalizes case.
                        parts = [p.strip('"') for p in _split_sql_qualified(table_name)]
                        bare = parts[-1]
                        for key in {bare, bare.upper()}:
                            routing[key] = entry
                        if len(parts) > 1:
                            qualified = ".".join(parts)
                            for key in {qualified, qualified.upper()}:
                                routing[key] = entry
        logger.info("Loaded datasource routing for %d tables from %s", len(routing), mappings_path)
        return routing
    except Exception as e:
        logger.warning("Failed to parse R2RML annotations from %s: %s", mappings_path, e)
        return {}


# Health probe query. A namespace-agnostic query that reformulates to SQL only
# once the mappings load. The unbound triple pattern is intentional: it reports
# healthy only when Ontop can actually reformulate a query, not just when its
# process is up (the functional-probe design #149 relies on).
_PROBE_SPARQL = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"


def _reformulate_probe():
    """Return (ok, reason) — ok True when the probe query reformulates to SQL.

    reason is None on success, else a short human-readable cause suitable for
    surfacing in the /health 503 body and, downstream, the vkgHealth reason.
    """
    params = urllib.parse.urlencode({"query": _PROBE_SPARQL})
    req = urllib.request.Request(_ontop_url(f"/ontop/reformulate?{params}"))
    req.add_header("Accept", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8").strip()
    except Exception as e:
        return False, f"reformulation probe failed: {e}"
    if _extract_sql(raw).upper().startswith(("SELECT", "WITH")):
        return True, None
    # Actuator was UP but the query did not reformulate — the mappings/ontology
    # loaded partially or not at all (the silent-failure case behind #170/#149).
    return False, "mappings did not reformulate a probe query (ontology/R2RML not fully loaded)"


def _probe_health():
    """Return (healthy, reason).

    Healthy only when Ontop actuator is UP *and* a real query reformulates.
    Actuator UP alone is insufficient under --lazy: a broken reload can look up
    while every translation fails silently.
    """
    try:
        req = urllib.request.Request(_ontop_url("/actuator/health"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return False, f"Ontop actuator unreachable: {e}"
    if data.get("status") != "UP":
        return False, f"Ontop actuator status={data.get('status')!r}"
    return _reformulate_probe()


def _poll_ontop_health():
    """Background thread that refreshes cached health every TTL seconds."""
    while True:
        healthy, reason = _probe_health()

        with _health_lock:
            _health_status["healthy"] = healthy
            _health_status["reason"] = None if healthy else (reason or "unhealthy")
            _health_status["last_check"] = time.time()

        time.sleep(_HEALTH_TTL_SECONDS)


def _check_ontop_health():
    """Return cached health status (bool)."""
    with _health_lock:
        return _health_status["healthy"]


def _health_reason():
    """Return the cached unhealthy reason (None when healthy)."""
    with _health_lock:
        return _health_status.get("reason")


def _translate_sparql(sparql, target_dialect):
    """Translate SPARQL to SQL via Ontop's /ontop/reformulate endpoint.

    This endpoint (available in --dev mode) returns the reformulated SQL
    directly as the response body without executing against any database.
    """
    params = urllib.parse.urlencode({"query": sparql})
    url = _ontop_url(f"/ontop/reformulate?{params}")

    req = urllib.request.Request(url)
    req.add_header("Accept", "text/plain")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8").strip()
            # Parse SPARQL projection metadata from the ans1(...) / CONSTRUCT
            # header before _extract_sql discards it. The SPARQL is needed for
            # the DISTINCT modifier, which is not recoverable from the SQL.
            projection = _parse_projection_header(raw, sparql)
            # Ontop's reformulate output includes metadata before the SQL.
            # Extract only the SQL portion (starts with SELECT/WITH).
            sql = _extract_sql(raw)
            # Translate from H2 dialect to target dialect (default: trino for Athena).
            # Ontop's /ontop/reformulate outputs H2 SQL. sqlglot has no H2 dialect, so
            # we use "postgres" as the closest parser — H2 syntax is largely compatible.
            # H2-specific coercions (e.g. REAL + VARCHAR) are pre-processed in
            # _translate_dialect before sqlglot sees the SQL.
            if target_dialect and target_dialect != "h2":
                sql = _translate_dialect(sql, "postgres", target_dialect)
            source_table_refs = _extract_table_refs(sql)
            # Enrich table refs with datasource routing from R2RML annotations
            datasource_routing = _resolve_routing(source_table_refs)
            result = {
                "sql": sql,
                "dialect": target_dialect or "h2",
                "ontologyVersion": _ontology_version,
                "sourceTableRefs": source_table_refs,
                "datasourceRouting": datasource_routing,
            }
            if projection:
                result["projection"] = projection
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        raise RuntimeError(f"Ontop error {e.code}: {error_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Ontop: {e.reason}") from e


def _rewrite_h2_functions(sql: str) -> str:
    """Rewrite H2-specific functions to standard SQL equivalents.

    H2 dialect functions that sqlglot's postgres parser doesn't recognise
    natively surface as ``exp.Anonymous`` nodes. This pass walks the AST
    and rewrites them into standard SQL before transpilation runs.

    Registry of H2 rewrites (extend by adding a branch on ``fn_name``):
      - ``FORMATDATETIME(expr, pattern)`` → ``CAST(expr AS VARCHAR)``

    Note: ``SUBSTR`` is intentionally NOT rewritten here. sqlglot's parser
    (all dialects tested — postgres/trino/duckdb/etc.) already models
    ``SUBSTR(...)`` as ``exp.Substring`` and its Trino/Postgres writer
    emits ``SUBSTRING`` unchanged, so no Anonymous-node branch is needed
    on the current sqlglot pin (30.8.0). A future upgrade that regresses
    this behaviour would need a new branch here.

    On sqlglot parse failure — Ontop occasionally emits SQL that sqlglot's
    postgres dialect can't tokenise — returns ``sql`` unchanged. The
    downstream ``_translate_dialect`` transpile then either handles it or
    falls back to the same unchanged input. There is intentionally no
    regex fallback here: the previous regex could not match nested
    ``FORMATDATETIME(DATEADD(..., ...))`` (``[^,]+`` stops at the first
    comma), which is precisely the shape the AST rewrite was introduced
    to handle. Keeping the regex as a "safety net" would silently keep
    that failure mode alive for exactly the cases the fix targets.

    Only ``SqlglotError`` is caught. Implementation bugs in the transform
    callback (``AttributeError``/``IndexError``) propagate so tests catch
    them rather than degrading the pipeline silently.
    """
    try:
        # error_level=RAISE surfaces genuine parse failures instead of
        # silently reconstructing a mangled AST — see the pre-existing test
        # `test_translate_dialect_returns_input_on_transpile_error` for a
        # concrete case (`"SELECT ((( FROM"` becomes `"SELECT (((SELECT * FROM )))"`
        # under IGNORE, which then flows through to transpile and defeats the
        # "return input unchanged on error" contract of `_translate_dialect`).
        parsed = sqlglot.parse_one(sql, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as e:
        logger.warning("H2 function rewrite: sqlglot parse failed, leaving SQL unchanged: %s", e)
        return sql

    def transform(node):
        if not isinstance(node, sqlglot.exp.Anonymous):
            return node

        fn_name = node.this.upper() if isinstance(node.this, str) else str(node.this).upper()

        # FORMATDATETIME(expr, pattern) → CAST(expr AS VARCHAR).
        # Malformed calls with no arguments fall through to `return node`
        # at the end — returning None here would delete the node from the
        # AST, silently mangling e.g. `SELECT FORMATDATETIME() FROM t` into
        # `SELECT FROM t`.
        if fn_name == "FORMATDATETIME" and node.expressions:
            return sqlglot.exp.Cast(
                this=node.expressions[0],
                to=sqlglot.exp.DataType.build("VARCHAR"),
            )

        return node

    transformed = parsed.transform(transform)
    return transformed.sql(dialect="postgres", identify=False)


def _translate_dialect(sql, source_dialect, target_dialect):
    """Translate SQL between dialects using sqlglot and normalize identifiers.

    Handles H2-specific functions that sqlglot doesn't transpile automatically.
    """
    # Pre-process H2-specific constructs that sqlglot's postgres parser doesn't handle.
    # H2 allows implicit coercions that Trino rejects — convert them explicitly before
    # handing off to sqlglot so the transpiler sees valid postgres-dialect SQL.
    sql = _rewrite_h2_functions(sql)

    try:
        # identify=False: preserve quotes Ontop placed on reserved-word identifiers
        # (order, user, date, etc.) without adding new quotes to normal identifiers.
        # Do NOT strip quotes after transpile — bare reserved words produce invalid SQL.
        return sqlglot.transpile(sql, read=source_dialect, write=target_dialect, identify=False)[0]
    except Exception as e:
        logger.warning(f"Dialect translation failed ({source_dialect}->{target_dialect}): {e}")
        return sql


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested inside parentheses.

    Ontop's CONSTRUCT term definitions contain commas inside function calls
    (``RDF(CHARACTER VARYINGToTEXT(TITLE1m4),xsd:string)``), so a plain
    ``str.split(",")`` would tear a single definition into fragments.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_projection_header(raw: str, sparql: str) -> dict[str, Any] | None:
    """Parse SPARQL projection metadata from Ontop's reformulate header.

    Ontop's /ontop/reformulate output describes the SPARQL solution above the SQL:

        ans1(employeeName)
        CONSTRUCT [employeeName] [employeeName/RDF(CHARACTER VARYINGToTEXT(FULL_NAME1m3),xsd:string)]
           NATIVE [EMPLOYEE_ID1m1, FULL_NAME1m3, ID1m7, TITLE1m4, v3]
        SELECT V2."EMPLOYEE_ID" AS "EMPLOYEE_ID1m1", V3."FULL_NAME" AS "FULL_NAME1m3", ...

    The variable→column correspondence comes from the CONSTRUCT term definitions
    (second bracket), which name the source alias per variable. It is NOT
    positional: above, ``employeeName`` is the ONLY selected variable yet its
    column ``FULL_NAME1m3`` is the *second* entry in the SQL SELECT list — the
    others are join keys and RDF-construction ingredients. Mapping by position
    would label the column ``employeeName`` and fill it with employee IDs.

    ``NATIVE [...]`` enumerates the SQL output aliases and is used as the
    vocabulary for recognising which alias a term definition references.

    ``distinct`` is read from the SPARQL itself, never inferred from the SQL:
    Ontop emits ``SELECT DISTINCT`` for its own reasons (e.g. de-duplicating
    rows behind a composite-key IRI template) on queries whose SPARQL has no
    DISTINCT, and conversely omits it when it can prove distinctness redundant.

    Returns a dict with:
        selectVars: list of SPARQL variable names (in SELECT order)
        varToColumn: dict mapping SPARQL var -> SQL output alias
        distinct: bool (true only for SPARQL SELECT DISTINCT / REDUCED)

    Returns None if the header is absent or unparseable, or if any selected
    variable cannot be resolved to exactly one SQL alias — the caller then
    falls back to returning raw SQL columns rather than mislabelling data.

    Every degradation path logs its reason. Without that, a namespace silently
    reverts to showing raw SQL aliases and there is no way to tell which of the
    causes below fired (--dev disabled, an Ontop version change, or a genuine
    parser bug). Non-SELECT queries have no ans1 header at all, so that one
    case logs at debug to keep ASK/CONSTRUCT traffic quiet.
    """
    # A missing header is expected for non-SELECT forms and unexpected otherwise.
    is_select = bool(re.search(r"\bSELECT\b", sparql, re.IGNORECASE))

    def _drop(reason):
        """Log why the projection was dropped, then signal the fallback."""
        if is_select:
            logger.warning("Projection unavailable (%s); results will use raw SQL aliases", reason)
        else:
            logger.debug("Projection unavailable (%s); non-SELECT query", reason)
        return None

    # Parse ans1(...) signature — the SPARQL SELECT variables, in SELECT order.
    ans_match = re.search(r"ans1\(([\w,\s]+)\)", raw)
    if not ans_match:
        return _drop("no ans1(...) signature in reformulate output")
    var_names = [v.strip() for v in ans_match.group(1).split(",") if v.strip()]
    if not var_names:
        return _drop("ans1(...) signature lists no variables")

    # Parse NATIVE [...] — the SQL output aliases.
    native_match = re.search(r"NATIVE\s+\[([^\]]*)\]", raw, re.IGNORECASE)
    if not native_match:
        return _drop("no NATIVE [...] alias list")
    native_aliases = {a.strip() for a in native_match.group(1).split(",") if a.strip()}
    if not native_aliases:
        return _drop("NATIVE [...] alias list is empty")

    # Parse the CONSTRUCT term-definition bracket (the SECOND bracket on the line):
    #   CONSTRUCT [vars] [var/RDF(expr,type), ...]
    construct_match = re.search(r"CONSTRUCT\s+\[[^\]]*\]\s+\[(.*)\]\s*$", raw, re.IGNORECASE | re.MULTILINE)
    if not construct_match:
        return _drop("no CONSTRUCT term-definition bracket")

    # Each definition is "var/<term expression>"; the expression references the
    # SQL alias holding the value. Match aliases on word boundaries so that
    # e.g. "v0" does not match inside "v01".
    var_to_column: dict[str, str] = {}
    for definition in _split_top_level(construct_match.group(1)):
        name, sep, expr = definition.partition("/")
        if not sep:
            continue
        name = name.strip()
        if name not in var_names:
            continue
        referenced = [a for a in native_aliases if re.search(rf"\b{re.escape(a)}\b", expr)]
        # Exactly one alias == an unambiguous column to read the value from.
        # Zero (a constant) or several (a value composed from multiple columns)
        # cannot be projected onto a single column.
        if len(referenced) == 1:
            var_to_column[name] = referenced[0]

    # Partial mappings would silently render an all-NULL column under a
    # correct-looking header, so degrade the whole projection instead.
    if len(var_to_column) != len(var_names):
        unresolved = [v for v in var_names if v not in var_to_column]
        # Naming the variables distinguishes the common benign cause (a
        # constant-folded FILTER term, which references no alias) from a
        # parser defect.
        return _drop("no unique SQL alias for {}".format(", ".join(unresolved)))

    # DISTINCT comes from the SPARQL's own projection modifier. Dedup is
    # spec-legal for REDUCED, so both collapse duplicate solutions.
    distinct = bool(re.search(r"\bSELECT\s+(DISTINCT|REDUCED)\b", sparql, re.IGNORECASE))

    return {
        "selectVars": var_names,
        "varToColumn": var_to_column,
        "distinct": distinct,
    }


def _extract_sql(raw):
    """Extract the SQL SELECT statement from Ontop's reformulation output.

    Ontop's /ontop/reformulate endpoint returns metadata before the SQL:
        ans1(x, y)
        CONSTRUCT [x, y] [...]
           NATIVE [...]
        SELECT ...

    This function extracts only the SQL portion.
    """
    # Find first SELECT or WITH statement
    match = re.search(r"(?m)^(SELECT|WITH)\b", raw, re.IGNORECASE)
    if match:
        return raw[match.start() :].strip()
    # No recognisable statement start: the caller receives the raw blob, which
    # will fail downstream. Log it here so the cause is visible rather than
    # surfacing as an opaque SQL error.
    logger.warning("No SELECT/WITH found in reformulate output; returning it unmodified")
    return raw


def _extract_table_refs(sql):
    """Extract table references using sqlglot AST parsing."""
    if not sql or sql.startswith("--"):
        return []
    try:
        parsed = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.IGNORE)
        tables = set()
        for table in parsed.find_all(sqlglot.exp.Table):
            name = table.name
            if table.db:
                name = f"{table.db}.{name}"
            if name:
                tables.add(name)
        return sorted(tables)
    except Exception:
        return []


def _resolve_routing(source_table_refs: list[str]) -> dict[str, dict[str, str]]:
    """Look up datasource routing info for each table reference.

    Returns a dict mapping each table ref to its routing metadata
    (datasourceId, sourceSchema) from the R2RML annotations loaded at startup.
    Only includes entries for tables that have routing info.
    """
    if not _table_routing:
        return {}
    routing: dict[str, dict[str, str]] = {}
    for ref in source_table_refs:
        # Table refs may be "schema.table" or just "table" — try both
        table_upper = ref.upper()
        # Try exact match first
        if table_upper in _table_routing:
            routing[ref] = _table_routing[table_upper]
        else:
            # Try just the table part (last segment)
            bare_table = ref.rsplit(".", 1)[-1].upper()
            if bare_table in _table_routing:
                routing[ref] = _table_routing[bare_table]
    return routing


class TranslateHandler(BaseHTTPRequestHandler):
    """HTTP request handler exposing the health and SPARQL-translation routes."""

    def do_GET(self):
        """Route GET requests: ``/health`` reports readiness, else 404."""
        if self.path == "/health":
            self._handle_health()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        """Route POST requests: ``/sparql/translate`` translates, else 404."""
        if self.path == "/sparql/translate":
            self._handle_translate()
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_health(self):
        if _check_ontop_health():
            self._send_json(
                200,
                {
                    "status": "healthy",
                    "ontologyVersion": _ontology_version,
                    "ontologyLoaded": True,
                },
            )
        else:
            self._send_json(
                503,
                {
                    "status": "unavailable",
                    "ontologyLoaded": False,
                    "message": "Ontop not ready",
                    "reason": _health_reason() or "Ontop not ready",
                },
            )

    def _handle_translate(self):
        if not _check_ontop_health():
            self._send_json(
                503,
                {
                    "error": "Service unavailable",
                    "message": "Ontop not ready",
                    "reason": _health_reason() or "Ontop not ready",
                },
            )
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 1_048_576:  # 1MB max request body
                self._send_json(413, {"error": "Request body too large (max 1MB)"})
                return
            body = self.rfile.read(length).decode("utf-8")
            request = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": f"Invalid request: {e}"})
            return

        sparql = request.get("sparql")
        if not sparql:
            self._send_json(400, {"error": "Missing field: sparql"})
            return

        target_dialect = request.get("targetDialect", "h2")

        try:
            result = _translate_sparql(sparql, target_dialect)
            self._send_json(200, result)
        except RuntimeError as e:
            self._send_json(500, {"error": str(e)})

    def _send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        """Log each request through the module logger instead of stderr.

        Args:
            fmt: printf-style format string supplied by the base handler.
            *args: Positional arguments interpolated into ``fmt``.
        """
        logger.info("%s %s", self.client_address[0], fmt % args)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that serves each request in its own daemon thread."""

    daemon_threads = True


def main():
    """Parse CLI args, load routing, start the health poller, and serve forever."""
    global _ontology_version, _ontop_port, _listen_port, _table_routing

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=_listen_port)
    parser.add_argument("--ontop-port", type=int, default=_ontop_port)
    parser.add_argument("--ontology-version", default=_ontology_version)
    parser.add_argument("--mappings-file", default=os.environ.get("MAPPINGS_FILE", ""))
    args = parser.parse_args()

    _listen_port = args.port
    _ontop_port = args.ontop_port
    _ontology_version = args.ontology_version

    # Load datasource routing from R2RML annotations
    _table_routing = _load_table_routing(args.mappings_file)

    # Start background health poller
    health_thread = threading.Thread(target=_poll_ontop_health, daemon=True)
    health_thread.start()

    # Bind to 0.0.0.0 — required for Docker container networking.
    # Access is restricted by VPC security group (CIDR-only ingress on port 8080).
    server = ThreadingHTTPServer(("0.0.0.0", _listen_port), TranslateHandler)
    logger.info("Listening on port %d (Ontop on %d)", _listen_port, _ontop_port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
