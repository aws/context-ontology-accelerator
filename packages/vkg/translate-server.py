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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# Maps a table reference ("claims" or "public.claims", original + uppercase) →
# {"datasourceId": ..., "sourceSchema": ...}
_table_routing: dict[str, dict[str, str]] = {}

# Routing-entry key marking a reference that more than one table answers to. Its
# value is the comma-separated candidate refs, and such an entry deliberately
# carries NO datasourceId — serve refuses to execute a query touching it rather
# than picking a candidate. Mirrored by ``table_qualifier.AMBIGUOUS_KEY`` in the
# context-manager, which is the only consumer.
AMBIGUOUS_KEY = "ambiguousWith"

# Cached health state (refreshed by background thread)
_health_status = {"healthy": False, "reason": "starting up"}
_health_lock = threading.Lock()
_HEALTH_TTL_SECONDS = 10


_ONTOP_SCHEME = "http"
_ONTOP_HOST = "localhost"
_ONTOP_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _ontop_url(path):
    """Build a request URL for the local Ontop sidecar, enforcing loopback HTTP.

    The URL is assembled from fixed scheme/host constants plus the integer port;
    ``path`` is a trusted literal chosen by the caller (any user-controlled
    SPARQL is urlencoded into the query string, never into the scheme or host).
    The assembled URL is parsed and checked to be plain HTTP on a loopback host
    before it is returned, so a ``file://`` or foreign-host URL can never reach
    ``urllib.request.urlopen`` — the guarantee is explicit at the sink. Raises
    ``ValueError`` on anything that is not loopback HTTP.
    """
    url = f"{_ONTOP_SCHEME}://{_ONTOP_HOST}:{_ontop_port}{path}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in _ONTOP_ALLOWED_HOSTS:
        raise ValueError(f"refusing non-loopback Ontop URL: {parsed.scheme}://{parsed.hostname}")
    return url


def _split_sql_qualified(name: str) -> list[str]:
    """Split a possibly schema-qualified SQL identifier into its parts.

    Splits only on dots outside double-quoted segments, so a dot embedded in a
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

    Returns a dict mapping table references (original case, uppercase, qualified and
    bare) to their datasource routing info, or an ambiguity marker for a bare name
    shared by several tables. Logs a warning and returns an empty dict if the file
    is unparseable; returns an empty dict if it is missing.
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
        # Bare names claimed by more than one table. Routing such a name would
        # silently resolve to whichever TriplesMap rdflib yielded last, so the key
        # gets the ambiguity marker instead and the serve-side qualifier fails
        # loudly on a bare reference rather than routing to the wrong catalog.
        bare_owners: dict[str, set[str]] = {}
        for tmap in g.subjects(predicate=None, object=RR.TriplesMap):
            ds_id = str(next(g.objects(tmap, SCL.datasourceId), "")) or None
            schema = str(next(g.objects(tmap, SCL.sourceSchema), "")) or None
            for lt in g.objects(tmap, RR.logicalTable):
                table_name = str(next(g.objects(lt, RR.tableName), ""))
                if table_name:
                    entry: dict[str, str] = {}
                    if ds_id:
                        entry["datasourceId"] = ds_id
                    if schema:
                        entry["sourceSchema"] = schema
                    if entry:
                        # Quotes are stripped per segment so quoted dots survive.
                        parts = [p.strip('"') for p in _split_sql_qualified(table_name)]
                        bare = parts[-1]
                        ref = ".".join(parts)
                        for key in {ref, ref.upper()}:
                            routing[key] = entry
                        # Ambiguity is measured on the QUALIFIED refs answering to a
                        # bare name, not on their datasource ids: two schemas of one
                        # datasource collide on the bare name just as two datasources do.
                        owners = bare_owners.setdefault(bare.upper(), set())
                        owners.add(ref.upper())
                        if len(owners) > 1:
                            # Absent bare key would read as "nothing to route" and
                            # run against the default context; the marker makes serve refuse.
                            marker = {AMBIGUOUS_KEY: ",".join(sorted(owners))}
                            for key in {bare, bare.upper()}:
                                routing[key] = marker
                        else:
                            for key in {bare, bare.upper()}:
                                routing[key] = entry
        logger.info("Loaded datasource routing for %d tables from %s", len(routing), mappings_path)
        return routing
    except Exception as e:
        logger.warning("Failed to parse R2RML annotations from %s: %s", mappings_path, e)
        return {}


# Health probe query. A namespace-agnostic query that reformulates to SQL only
# once the mappings load, so it proves reformulation works, not just that the
# process is up.
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
    # Actuator was UP but the query did not reformulate: mappings/ontology loaded partially or not at all.
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
            # The header must be parsed before _extract_sql discards it.
            projection = _parse_projection_header(raw, sparql)
            sql = _extract_sql(raw)
            # Ontop emits H2 SQL; sqlglot has no H2 dialect, so postgres is the closest parser.
            if target_dialect and target_dialect != "h2":
                sql = _translate_dialect(sql, "postgres", target_dialect)
            source_table_refs = _extract_table_refs(sql)
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

    ``SUBSTR`` needs no rewrite: sqlglot already parses it as ``exp.Substring``.

    SQL that sqlglot cannot parse is returned unchanged. Only ``SqlglotError`` is
    caught; bugs in the transform callback propagate.
    """
    try:
        # RAISE is required: IGNORE would rebuild a mangled AST from broken SQL.
        parsed = sqlglot.parse_one(sql, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as e:
        logger.warning("H2 function rewrite: sqlglot parse failed, leaving SQL unchanged: %s", e)
        return sql

    def transform(node):
        if not isinstance(node, sqlglot.exp.Anonymous):
            return node

        fn_name = node.this.upper() if isinstance(node.this, str) else str(node.this).upper()

        # FORMATDATETIME(expr, pattern) → CAST(expr AS VARCHAR); no-arg calls fall through.
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

    Returns None if the header is absent or unparseable, or if any selected
    variable cannot be resolved to exactly one SQL alias; the caller then falls
    back to raw SQL columns rather than mislabelling data. Every degradation path
    logs its reason (non-SELECT queries log at debug).
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

    ans_match = re.search(r"ans1\(([\w,\s]+)\)", raw)
    if not ans_match:
        return _drop("no ans1(...) signature in reformulate output")
    var_names = [v.strip() for v in ans_match.group(1).split(",") if v.strip()]
    if not var_names:
        return _drop("ans1(...) signature lists no variables")

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
    """Extract the SQL portion (first SELECT or WITH line) of Ontop's reformulation output."""
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

    When a referenced table's bare name is one that MORE THAN ONE table answers to
    (an ambiguity marker was recorded for the bare name at load time), the sibling
    candidates' entries are included too — even though the query referenced only
    one of them. A bare name shared across datasources cannot be resolved by the
    query context's single default catalog, so the serve-side qualifier must know
    the name is shared to add the catalog; it can only see that if the routing it
    receives carries the other owners. Including the siblings is what lets a
    single-source query over a shared name be catalog-qualified rather than run
    unqualified against a guessed context.
    """
    if not _table_routing:
        return {}
    routing: dict[str, dict[str, str]] = {}
    for ref in source_table_refs:
        table_upper = ref.upper()
        if table_upper in _table_routing:
            routing[ref] = _table_routing[table_upper]
        else:
            bare_table = ref.rsplit(".", 1)[-1].upper()
            if bare_table in _table_routing:
                routing[ref] = _table_routing[bare_table]
        if "." not in ref:
            continue
        bare_upper = ref.rsplit(".", 1)[-1].upper()
        marker = _table_routing.get(bare_upper, {})
        if AMBIGUOUS_KEY in marker:
            for candidate in marker[AMBIGUOUS_KEY].split(","):
                entry = _table_routing.get(candidate, {})
                if entry.get("datasourceId") and candidate not in routing:
                    routing[candidate] = entry
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
        """Log each request through the module logger instead of stderr."""
        logger.info("%s %s", self.client_address[0], fmt % args)


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

    _table_routing = _load_table_routing(args.mappings_file)

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
