# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Catalog qualification for cross-source SQL.

A query whose tables live in more than one Athena catalog (or more than one
database of the SAME catalog) cannot execute with bare table names: Athena's
``QueryExecutionContext`` pins exactly ONE ``(Catalog, Database)`` pair, and it
only supplies the default for *unqualified* names. A bare ``t_b`` in a context
pinned to ``db_a`` resolves as ``awsdatacatalog.db_a.t_b`` and fails with
``TABLE_NOT_FOUND``, even when ``t_b`` exists in ``db_b`` right next door.

Measured on Athena (us-east-1), two Glue databases, identical join:

    bare names,       context pinned to db_a  -> TABLE_NOT_FOUND
    catalog.db.table, any context (or none)   -> SUCCEEDED

Qualified names are wholly context-independent — Athena does not even validate
the context ``Database`` when every reference carries its own catalog. So the
fix is to rewrite each table reference to ``catalog.schema.table`` and let the
context become irrelevant, rather than to keep guessing at one catalog.

The routing metadata needed for the rewrite already exists per table: the
inducer stamps every R2RML TriplesMap with ``coa:datasourceId`` +
``coa:sourceSchema`` (``inducer/strategies/base.py:_annotate_triples_map``), the
VKG translation layer parses them at startup, and every translate response
returns the per-table subset as ``datasourceRouting``. Only the Athena catalog
NAME has to be fetched, once per distinct datasource, from the sources registry.

Two rewrites live here, and they are deliberately separate because they apply to
different populations:

1. :func:`restore_source_schemas` — replaces a *logical* schema with the real
   one. Applies to EVERY statement, single-source included, and needs no I/O.
   The inducer may mint a synthetic schema token (``public__1a2b3c4d``) to keep
   two datasources' same-named tables distinguishable in the mapping and in the
   H2 validation schema; that token names nothing in the real database, so it
   has to be undone before execution whether or not the query spans sources.
2. :func:`qualify_cross_source_sql` — adds the catalog. Applies only when the
   statement genuinely spans datasources, and needs one registry read per
   referenced datasource.

:func:`prepare_execution_sql` runs both in the right order and is the ONLY entry
point strategies should call, so the two Tier-2 strategies cannot drift in how
they order the passes or react to a failure.

Ordering constraint — all of this MUST run downstream of the SQL firewall. The
firewall normalises every table to its bare last component for the
``tableAllowlist`` / ``columnDenylist`` checks, so prefixes do not affect them;
but Cedar receives ``SQLFirewall.extract_tables`` output verbatim, so rewriting
BEFORE authorization would silently change the strings a future table-scoped
policy matches on. Rewriting after authorization changes no authorization input.
``tests/unit/test_tier2_vkg_translator.py`` and
``tests/unit/test_nl_to_sql_strategy.py`` each assert the firewall's actual
argument, so this constraint is executable rather than advisory.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol

import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp
import structlog
from coa_common.constants import split_sql_ident_path

logger = structlog.get_logger(__name__)

# Athena's built-in Glue catalog. Used when a source record carries no explicit
# federated catalog name — i.e. the Glue-native path, whose tables live in
# AwsDataCatalog. Athena accepts this quoted or unquoted, in any case.
_DEFAULT_GLUE_CATALOG = "awsdatacatalog"

# Catalog / schema names are interpolated into SQL as identifiers, so they are
# validated first. sqlglot's ``quoted=True`` already neutralises an embedded
# quote (it doubles it), so this is not the injection barrier — it is here so a
# malformed name from source onboarding or schema discovery fails as a skipped
# rewrite rather than as a quoted identifier that addresses nothing. Same shape
# the registry enforces on database names (``sources_registry`` uses this exact
# pattern before returning one).
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Routing-entry key the VKG translator uses to publish a reference that more than
# one table answers to; the value names the candidates. Must match
# ``packages/vkg/translate-server.py:AMBIGUOUS_KEY`` — the routing map crosses
# that process boundary as JSON, so the two spellings are one contract.
AMBIGUOUS_KEY = "ambiguousWith"


class QualificationError(Exception):
    """Raised when cross-source SQL cannot be safely qualified.

    Carries a message naming the offending table(s) — the caller surfaces it as
    a query error rather than executing SQL that would silently read the wrong
    physical table.
    """


class SourceLookup(Protocol):
    """The slice of ``SourcesRegistry`` this module needs.

    The parameter names match ``SourcesRegistry.get_source`` exactly: a Protocol
    method is matched by keyword as well as position, so a rename here would
    silently stop the real registry from satisfying it.
    """

    async def get_source(self, namespace: str, data_source_id: str) -> dict[str, Any]:
        """Return the source record for ``data_source_id``, or a falsy value."""
        ...


@dataclass(frozen=True)
class PreparedSQL:
    """The statement to execute, or the reason it must not be executed.

    ``error`` is an operator-facing message. When it is set the SQL must NOT be
    submitted: the only way to reach it is a reference that cannot be attributed
    to one physical table, and executing such a statement risks silently reading
    a different table than the one that was authorized.
    """

    sql: str
    error: str | None = None


def ambiguous_reference(
    sql: str,
    table_routing: dict[str, dict[str, str]],
    *,
    dialect: str = "trino",
) -> str | None:
    """Return a diagnostic for the first ambiguously-routed table in ``sql``, else ``None``.

    The VKG translator publishes an ``AMBIGUOUS_KEY`` entry — candidates named,
    no ``datasourceId`` — for a reference that more than one table answers to. It
    used to withhold the entry instead, which made an ambiguous reference
    indistinguishable from a table outside the mapping; the two need opposite
    treatment. An out-of-mapping table is nothing to do with us. An ambiguous one
    means the query names a table we cannot attribute, and because such an entry
    contributes no datasource id, :func:`distinct_datasources` sees fewer sources
    than the query really spans and the cross-source qualifier never runs — so the
    statement executed against whatever catalog the query context defaulted to.

    Checked independently of cross-source-ness for exactly that reason. The dict
    scan comes first so the common (unambiguous) path never pays for a parse.

    Args:
        sql: The statement about to be executed.
        table_routing: Per-table routing from the VKG translate response.
        dialect: sqlglot dialect for parsing.

    Returns:
        A message naming the reference and its candidates, or ``None``.
    """
    flagged = {ref.lower(): entry[AMBIGUOUS_KEY] for ref, entry in table_routing.items() if AMBIGUOUS_KEY in entry}
    if not flagged:
        return None
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - unparseable SQL cannot be cleared
        return f"could not parse the SQL to check ambiguous table references ({type(exc).__name__})"
    known = {ref.lower() for ref in table_routing}
    for table in real_tables(parsed):
        # Qualified form first, exactly as :func:`_match_routing` resolves: only a
        # BARE reference is ambiguous, and a marker is published under the bare
        # key beside the real entries for each qualified candidate. Testing both
        # forms unconditionally would condemn ``public.claims`` — which names one
        # physical table — for the ambiguity of bare ``claims``.
        qualified = f"{table.db}.{table.name}".lower().lstrip(".")
        ref = qualified if qualified in known else table.name.lower()
        if ref in flagged:
            return f"table reference {ref!r} is ambiguous: it could be any of {flagged[ref]}"
    return None


def distinct_datasources(table_routing: dict[str, dict[str, str]]) -> set[str]:
    """Return the distinct, non-empty ``datasourceId`` values in ``table_routing``.

    More than one means the query spans sources — which is the signal to
    qualify, NOT (as it was previously treated) a signal that the target source
    is unresolvable.
    """
    return {v.get("datasourceId", "") for v in table_routing.values()} - {""}


def distinct_source_targets(table_routing: dict[str, dict[str, str]]) -> set[tuple[str, str]]:
    """Return the distinct ``(datasourceId, sourceSchema)`` pairs in ``table_routing``.

    A single Athena ``QueryExecutionContext`` names ONE ``(catalog, database)``
    pair and supplies it to unqualified names only, so a query needs qualifying
    whenever it spans more than one such pair. That is one pair per distinct
    ``(datasourceId, sourceSchema)`` here: the catalog is a function of the
    datasource (:func:`catalog_for_source`) and the database is the schema.

    A superset of :func:`distinct_datasources` as the qualification trigger — it
    additionally catches a SINGLE source whose tables span two schemas, the
    adjacent limitation: ``athena.py`` pins ``discoveredSchemas[0]`` as
    the context Database, so a table in the second schema was ``TABLE_NOT_FOUND``.
    Entries with no ``datasourceId`` (an ambiguity marker) are excluded, exactly
    as :func:`distinct_datasources` excludes them.

    Entries with no ``sourceSchema`` (the NL->SQL path, whose retrieval hits carry
    no schema) collapse to one pair per datasource, so that path keeps its
    datasource-count trigger and never qualifies a genuinely single-source query.
    """
    targets: set[tuple[str, str]] = set()
    for v in table_routing.values():
        ds_id = v.get("datasourceId", "")
        if not ds_id:
            continue
        targets.add((ds_id, v.get("sourceSchema", "")))
    return targets


def bare_names_shared_across_datasources(table_routing: dict[str, dict[str, str]]) -> set[str]:
    """Bare table names that more than one DATASOURCE in the routing map answers to.

    A name shared across datasources cannot be resolved by the query context's
    single ``(catalog, database)`` default — even a statement that references only
    ONE of the same-named tables must name that table's CATALOG explicitly, or a
    caller pinned to the other source's context would silently read the wrong
    physical table. So this is the set of bare names for which a reference has to
    carry its catalog even when the statement is otherwise single-source.

    Measured on the DATASOURCE id, not the ``(datasource, schema)`` pair: two
    schemas of ONE source colliding on a bare name are separated by the schema (a
    two-part ``schema.table`` is enough), so they are NOT included here. Only a
    name owned by two different datasources needs the catalog. Entries with no
    ``datasourceId`` (an ambiguity marker) contribute no owner.

    The bare name is taken from the routing KEY's last path segment, so a
    schema-qualified key (``"schema"."table"``) and a bare key (``table``) both
    fold onto the same bare name.
    """
    owners: dict[str, set[str]] = {}
    for ref, entry in table_routing.items():
        ds_id = entry.get("datasourceId", "")
        if not ds_id:
            continue
        bare = split_sql_ident_path(ref.lower())[-1]
        owners.setdefault(bare, set()).add(ds_id)
    return {bare for bare, ds_ids in owners.items() if len(ds_ids) > 1}


def catalog_for_source(source: dict[str, Any]) -> str:
    """Return the Athena catalog a source's tables live in.

    Federated JDBC sources get their own nested catalog at onboarding
    (``athenaDataCatalogName``); Glue-native sources live in the account's Glue
    catalog. Mirrors the catalog half of ``athena.py``'s
    ``_resolve_catalog_and_database`` so the two cannot disagree about where a
    source's tables are.
    """
    return source.get("athenaDataCatalogName") or source.get("athenaCatalog") or _DEFAULT_GLUE_CATALOG


def schema_for_source(source: dict[str, Any]) -> str:
    """Return the best per-SOURCE schema/database, for callers with no per-table one.

    The per-table ``coa:sourceSchema`` is always preferable (it is exact, and it
    lets a single source span several schemas). This is the fallback for the
    NL->SQL path, whose retrieval hits carry ``data_source_id`` but no schema.

    Branches exactly as ``athena.py``'s ``_resolve_catalog_and_database`` does —
    federated sources take the first discovered schema (defaulting to
    ``public``), Glue-native sources take the database name. Reading
    ``discoveredSchemas`` on the Glue-native branch too, as an earlier version
    did, made the two paths resolve the same record to different databases, and
    this one is the value baked into the executed SQL.

    A source exposing more than one schema is a genuine limitation of this
    fallback: there is nothing in a bare retrieval hit to say which schema the
    table came from, so the first is used and the choice is logged. Callers with
    per-table ``sourceSchema`` (the VKG path) never reach it.

    A federated source with NO ``discoveredSchemas`` returns ``""`` (not an
    invented ``public``): ``sql_namespace_scope`` builds the federated authorized
    set exclusively from ``discoveredSchemas``, so a schema this source never
    discovered can never be in scope. Because the rewrite turns a bare statement
    into a qualified one, a qualified reference then runs through
    ``_authorize_qualified_references`` and would be DENIED rather than merely
    failing at Athena. Returning ``""`` makes the caller take its documented
    ``qualification_incomplete_no_schema`` abandon path and leave the SQL bare.
    """
    if source.get("athenaDataCatalogName"):
        discovered = [str(s) for s in source.get("discoveredSchemas") or []]
        if len(discovered) > 1:
            logger.warning(
                "qualification_schema_ambiguous_for_source",
                source_id=str(source.get("sourceId") or source.get("id") or ""),
                schema_count=len(discovered),
                chosen=discovered[0],
            )
        return discovered[0] if discovered else ""
    return str(source.get("athenaDatabase") or source.get("glueDatabaseName") or "")


def _is_cte_reference(table: exp.Table, cte_names: set[str]) -> bool:
    """True when ``table`` resolves to a CTE rather than to a real table.

    Scope-correct rather than name-only: SQL binds a BARE reference whose name
    matches a ``WITH`` alias to the CTE, but a reference carrying a schema or
    catalog always names the real table. Testing the name alone made a CTE named
    after a table it wraps (``WITH customers AS (SELECT * FROM crm.customers)``)
    shadow the real ``crm.customers``, which produced a partial rewrite — the one
    outcome the all-or-nothing contract forbids — and made
    :func:`is_fully_catalog_qualified` certify the result as fully qualified.
    """
    return not table.db and not table.catalog and table.name.lower() in cte_names


def real_tables(parsed: exp.Expression) -> list[exp.Table]:
    """Return the table nodes in ``parsed`` that name real tables, not CTEs."""
    cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
    return [t for t in parsed.find_all(exp.Table) if t.name and not _is_cte_reference(t, cte_names)]


def is_fully_catalog_qualified_ast(parsed: exp.Expression) -> bool:
    """AST form of :func:`is_fully_catalog_qualified`, for callers that already parsed.

    Athena's executor parses the statement anyway to inject a LIMIT; re-parsing
    the identical text purely to answer this question costs ~1.7 ms per KB of
    SQL, synchronously, on every Tier-2 query.
    """
    tables = real_tables(parsed)
    return bool(tables) and all(t.catalog for t in tables)


def is_fully_catalog_qualified(sql: str, dialect: str = "trino") -> bool:
    """True when every real table reference in ``sql`` carries a catalog component.

    Such SQL is context-independent (verified against Athena), so the executor
    can skip catalog resolution and the single-schema federation rewrite
    entirely. Returns False on a parse failure or when there are no table
    references at all — i.e. it never claims qualification it cannot prove.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError:
        return False
    return is_fully_catalog_qualified_ast(parsed)


def restore_source_schemas(
    sql: str,
    table_routing: dict[str, dict[str, str]],
    *,
    dialect: str = "trino",
) -> str:
    """Replace a logical schema qualifier in ``sql`` with the real source schema.

    Runs for EVERY statement, single-source included, and performs no I/O.

    When two datasources expose the same ``schema.table``, no real qualifier
    separates them, so the inducer mints a synthetic schema token
    (``public__1a2b3c4d``) for the ``rr:tableName`` literal and the matching H2
    validation schema (``inducer/strategies/base.py:disambiguated_refs``, case
    3). That token keeps the two logical tables distinct — without it Ontop
    validates both TriplesMaps against whichever table H2 created first — but it
    names no schema in the real database, so Ontop's SQL arrives here addressing
    something that does not exist.

    Gating this on "the query spans sources" would fix cross-source queries and
    break every single-source query over the same table, since a one-table
    statement never reaches the qualifier. The real schema travels beside the
    synthetic one in the routing entry's ``coa:sourceSchema``, so restoring it
    needs nothing but the routing map.

    Returns ``sql`` unchanged when there is nothing to restore, and is
    idempotent: a reference already carrying its real schema is left alone.
    """
    if not table_routing:
        return sql
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError as exc:
        # Type only, never the message: sqlglot embeds a window of the statement
        # (literals included) in its error text.
        logger.warning("schema_restore_parse_failed", error=type(exc).__name__, sql_len=len(sql))
        return sql

    lookup, ambiguous_bare = _routing_index(table_routing)
    restored: list[tuple[exp.Table, str]] = []
    for table in real_tables(parsed):
        if not table.db:
            continue
        # A synthetic token whose BARE name is claimed by more than one target
        # (two datasources, or two schemas of one source, sharing the real
        # schema+table) must NOT be restored here. Restoring it collapses the two
        # references to an identical ``real.table`` and destroys the only token
        # that told them apart — after which qualify_cross_source_sql can no longer
        # attribute either and refuses a query it could have qualified.
        # Leave it for the qualifier: it matches on the synthetic key and sets the
        # schema from ``sourceSchema`` in the same pass, so the schema is restored
        # AND the catalog attributed together.
        if table.name.lower() in ambiguous_bare:
            continue
        # Only the QUALIFIED key can carry a logical schema: a bare reference has
        # no schema to correct.
        entry = lookup.get(f"{table.db.lower()}.{table.name.lower()}")
        if entry is None:
            continue
        real = entry.get("sourceSchema", "")
        if not real or real.lower() == table.db.lower():
            continue
        if not _IDENTIFIER_PATTERN.match(real):
            logger.warning("schema_restore_invalid_identifier", table=table.name)
            continue
        restored.append((table, real))

    if not restored:
        return sql
    for table, real in restored:
        table.set("db", exp.to_identifier(real, quoted=True))
    logger.info("logical_schema_restored", tables_restored=len(restored))
    return parsed.sql(dialect=dialect)


async def prepare_execution_sql(
    sql: str,
    table_routing: dict[str, dict[str, str]],
    sources: SourceLookup | None,
    *,
    namespace: str,
    dialect: str = "trino",
) -> PreparedSQL:
    """Make ``sql`` executable: restore real schemas, then qualify if cross-source.

    The single entry point for both Tier-2 strategies. Each strategy compiles its
    own SQL but the post-authorization rewrite is one policy, so it lives here
    rather than being restated per strategy — a fix applied to one strategy and
    not the other is the class of bug this module exists to remove.

    Refuses only the genuinely UNRESOLVABLE case: a BARE reference that more than
    one ``(datasource, schema)`` answers to sets ``error`` and must not be
    executed, because guessing which physical table is meant can return a wrong
    answer as success (the same bare name in two schemas of one source).
    Both strategies now reach this refusal: the VKG path via the mapping's
    ``AMBIGUOUS_KEY`` marker, the NL->SQL path via
    :func:`~..nl_to_sql.sql_generator.sql_table_routing`, which emits the same
    marker for a name its retrieval hits attribute to two classes.

    Every OTHER incompleteness — a reference no routing entry attributes, a source
    that is not queryable, a malformed catalog name — leaves the statement
    UNCHANGED rather than half-rewritten, so it executes bare and fails loudly at
    Athena (``TABLE_NOT_FOUND``) exactly as before. That is NOT fail-closed; it is
    the all-or-nothing contract of :func:`qualify_cross_source_sql`, which never
    produces a partial rewrite.

    :func:`ambiguous_reference` runs FIRST, before either rewrite, because an
    ambiguously-routed table contributes no datasource id — so it would otherwise
    slip past the cross-source test that gates the qualifier and execute
    unqualified.
    """
    ambiguous = ambiguous_reference(sql, table_routing, dialect=dialect)
    if ambiguous:
        return PreparedSQL(sql=sql, error=ambiguous)
    restored = restore_source_schemas(sql, table_routing, dialect=dialect)
    try:
        qualified = await qualify_cross_source_sql(
            restored, table_routing, sources, namespace=namespace, dialect=dialect
        )
    except QualificationError as exc:
        return PreparedSQL(sql=restored, error=str(exc))
    return PreparedSQL(sql=qualified)


async def qualify_cross_source_sql(
    sql: str,
    table_routing: dict[str, dict[str, str]],
    sources: SourceLookup | None,
    *,
    namespace: str,
    dialect: str = "trino",
) -> str:
    """Qualify table refs so one ``QueryExecutionContext`` no longer has to serve them all.

    Two output forms, by how many DATASOURCES the statement references:

    - spans ONE datasource but two of its schemas → ``"schema"."table"``
      (two-part). The single catalog is supplied by the context (Athena) or the
      connection (direct JDBC), so naming it would only break the direct-JDBC
      route, whose dialect cannot resolve an Athena DataCatalog name.
    - spans TWO OR MORE datasources → ``"catalog"."schema"."table"`` (three-part),
      the only form that can name two catalogs at once. This is the original
      cross-source rewrite, unchanged and verified live.

    Returns ``sql`` UNCHANGED — no rewrite attempted — whenever qualification is
    unnecessary or cannot be done completely:

    - fewer than two distinct ``(datasource, schema)`` targets, i.e. one
      ``(catalog, database)`` context can serve the whole statement (the
      single-target path already works, and is left bit-for-bit untouched so this
      can never regress it). A single source spanning two schemas is two targets
      and DOES qualify — see :func:`distinct_source_targets`;
    - no sources registry wired;
    - any table reference that routing cannot attribute to a source;
    - a source that is not queryable, or whose catalog/schema name is malformed.

    A partial rewrite is deliberately not produced: it would leave SQL that is
    neither context-resolvable nor self-describing, which is strictly worse than
    today's loud ``TABLE_NOT_FOUND``. Every reference is therefore attributed
    before any node is mutated.

    Registry reads happen AFTER attribution and concurrently, so the two abandon
    paths cost no I/O at all and the latency added is one round trip rather than
    one per datasource.

    Raises:
        QualificationError: when a BARE table reference in ``sql`` answers to more
            than one ``(datasource, schema)`` pair. Such a reference cannot be
            attributed to one physical table, and guessing would read the wrong
            one. A reference that carries its schema is attributable and does NOT
            raise, which is what makes a mapping produced after this fix
            (schema-qualified ``rr:tableName`` for shared names) executable. See
            :func:`_match_routing`.
    """
    targets = distinct_source_targets(table_routing)
    # A bare name owned by two datasources needs its catalog even in a statement
    # that references only ONE of the same-named tables: the context's single
    # (catalog, database) default cannot tell the two apart, so a caller pinned to
    # the other source's context would read the wrong physical table. Such a name
    # is a qualification trigger in its own right, independent of how many distinct
    # (datasource, schema) targets the referenced tables span.
    cross_source_shared = bare_names_shared_across_datasources(table_routing)
    if len(targets) < 2 and not cross_source_shared:
        return sql
    if sources is None:
        logger.warning("qualification_skipped_no_registry", target_count=len(targets))
        return sql

    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError as exc:
        # Type only, never str(exc): sqlglot's message embeds a window of the
        # statement, so logging it would write user-influenced filter values
        # (names, account numbers) into CloudWatch.
        logger.warning("qualification_parse_failed", error=type(exc).__name__, sql_len=len(sql))
        return sql

    lookup, ambiguous_bare = _routing_index(table_routing)

    # Pass 1 — attribute every reference. No I/O, so both abandon paths below
    # are free, and a namespace with an incomplete routing map costs nothing.
    plan: list[tuple[exp.Table, str, str]] = []
    for table in real_tables(parsed):
        entry = _match_routing(table, lookup, ambiguous_bare)
        if entry is None:
            # Unattributable reference — abandon the whole rewrite (see docstring).
            logger.warning(
                "qualification_incomplete_unrouted_table",
                table=table.name,
                routed_tables=sorted(lookup),
            )
            return sql
        schema = entry.get("sourceSchema", "") or (table.db or "")
        plan.append((table, entry.get("datasourceId", ""), schema))

    if not plan:
        # No real table references (e.g. an all-CTE statement): nothing to
        # qualify, and claiming a rewrite that did not happen would let the
        # executor skip catalog resolution on SQL that still needs it.
        logger.warning("qualification_no_table_references", target_count=len(targets))
        return sql

    # Pass 2 — one registry read per datasource the statement ACTUALLY
    # references, issued concurrently so the tail is one round trip, not N.
    referenced = sorted({ds_id for _, ds_id, _ in plan})
    records = await asyncio.gather(*(sources.get_source(namespace, ds_id) for ds_id in referenced))
    catalogs: dict[str, str] = {}
    source_schemas: dict[str, str] = {}
    for ds_id, source in zip(referenced, records, strict=True):
        if not source:
            logger.warning("qualification_source_not_found", namespace=namespace, datasource_id=ds_id)
            return sql
        if source.get("queryable") is False:
            # Leave the SQL UNQUALIFIED for a non-queryable source. The actual
            # Lake Formation gate is enforced by Athena at execution time, not by
            # the serve code here: athena.py:_resolve_catalog_and_database does NOT
            # deny a non-queryable source — it declines to assert the source's
            # catalog and falls back to the default context, letting Athena + LF
            # make the access decision. Qualifying here would be the actual
            # mistake: it asserts a catalog the LF grant may not permit, turning a
            # clean downstream denial into a malformed reference. Returning the
            # statement bare keeps that enforcement where it belongs.
            logger.warning("qualification_source_not_queryable", namespace=namespace, datasource_id=ds_id)
            return sql
        catalogs[ds_id] = catalog_for_source(source)
        source_schemas[ds_id] = schema_for_source(source)

    # Pass 3 — resolve and validate everything BEFORE mutating any node, so a
    # late failure cannot leave a half-rewritten statement.
    resolved: list[tuple[exp.Table, str, str]] = []
    for table, ds_id, schema in plan:
        schema = schema or source_schemas.get(ds_id, "")
        if not schema:
            logger.warning("qualification_incomplete_no_schema", table=table.name, datasource_id=ds_id)
            return sql
        catalog = catalogs[ds_id]
        if not _IDENTIFIER_PATTERN.match(catalog) or not _IDENTIFIER_PATTERN.match(schema):
            logger.warning(
                "qualification_invalid_identifier",
                namespace=namespace,
                datasource_id=ds_id,
                table=table.name,
            )
            return sql
        resolved.append((table, catalog, schema))

    # A statement confined to ONE datasource that merely spans two of its schemas
    # needs the SCHEMA made explicit, not the catalog: the single catalog is
    # supplied by the Athena QueryExecutionContext (athena.py resolves it from the
    # one data_source_id) or by the direct-JDBC connection, and both routes accept
    # a bare "schema"."table". Prepending the Athena DataCatalog name instead would
    # push a direct-JDBC source onto SQL its own dialect cannot resolve (the
    # federated catalog is not an object in the source database), so the
    # single-catalog case is deliberately TWO-part. A query that genuinely spans
    # datasources keeps the three-part "catalog"."schema"."table" form — the only
    # form that can name two catalogs at once, and the one verified live.
    #
    # A single-datasource statement whose table name is ALSO owned by another
    # datasource is the exception: it must carry its catalog too, so that a context
    # pinned to the other same-named source cannot resolve the bare name against
    # the wrong catalog. The schema alone does not separate them once the two live
    # in different catalogs, so this case is three-part even though the statement
    # references one datasource.
    references_shared_name = any(t.name.lower() in cross_source_shared for t, _, _ in resolved)
    qualify_catalog = len(referenced) > 1 or references_shared_name
    for table, catalog, schema in resolved:
        # quoted=True: an Athena DataCatalog name may contain hyphens (the
        # onboarding-provisioned connector catalogs routinely do), which Trino
        # would not parse unquoted. Athena accepts the quoted form for both
        # catalog and schema (verified live), so quoting is unconditional rather
        # than conditional on the characters present.
        table.set("db", exp.to_identifier(schema, quoted=True))
        if qualify_catalog:
            table.set("catalog", exp.to_identifier(catalog, quoted=True))

    qualified = parsed.sql(dialect=dialect)
    # Make the all-or-nothing invariant executable rather than trusting it by
    # construction. The rewrite mutates AST nodes in place and re-renders the WHOLE
    # statement, so a sqlglot round-trip bug could in principle drop or duplicate a
    # table reference. Re-parse the output and confirm the real (non-CTE) table set
    # is preserved: same count, and every rewritten reference carries its intended
    # (catalog?, schema, name). On any mismatch, abandon to the untouched input —
    # the documented safe path — and log for diagnosis. (Structural firewall rules
    # are separately re-run on this output by the executor's _firewall.validate.)
    try:
        reparsed = sqlglot.parse_one(qualified, read=dialect)
        after = real_tables(reparsed)
    except sqlglot.errors.SqlglotError:
        after = None
    if after is None or len(after) != len(resolved):
        logger.warning(
            "qualification_roundtrip_mismatch",
            namespace=namespace,
            before=len(resolved),
            after=(len(after) if after is not None else -1),
        )
        return sql
    logger.info(
        "cross_source_sql_qualified",
        namespace=namespace,
        target_count=len(targets),
        tables_qualified=len(resolved),
    )
    return qualified


def _routing_index(
    table_routing: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, set[tuple[str, str]]]]:
    """Index routing entries by every key form a SQL reference might take.

    VKG keys ``datasourceRouting`` by the reference as it appeared in the
    translated SQL (``schema.table`` or bare ``table``). Index both, lowercased,
    so matching does not depend on how sqlglot normalised the identifier.

    A bare key is registered only while every qualified ref answering to it would
    qualify the SAME way. Ambiguity is therefore measured on the
    ``(datasourceId, sourceSchema)`` pair each ref resolves to — NOT on the
    datasource id alone, which missed two schemas of ONE source colliding on a
    bare name (``public.claims`` vs ``restricted.claims``) and silently attributed
    the reference to whichever indexed first. Two refs that would produce
    identical SQL are not ambiguous, so this adds no false positives. This
    mirrors the rule ``packages/vkg/translate-server.py:_load_table_routing``
    applies when it marks a bare key ambiguous upstream; the two are independent
    because ``_resolve_routing`` sends only the entries the statement's own refs
    matched, so the entries needed to re-derive a collision here may be absent —
    which is what :func:`ambiguous_reference` covers.

    Returns:
        ``(index, ambiguous_bare)`` where ``ambiguous_bare`` maps a withheld bare
        name to the ``(datasource, schema)`` pairs that claim it, so
        :func:`_match_routing` can tell "no routing for this table" (abandon the
        rewrite) apart from "this table name is ambiguous" (fail loudly) — the two
        need opposite treatment.
    """
    index: dict[str, dict[str, str]] = {}
    bare_targets: dict[str, set[tuple[str, str]]] = {}
    for ref, entry in table_routing.items():
        parts = split_sql_ident_path(ref.lower())
        key = ".".join(parts)
        index[key] = entry
        bare = parts[-1]
        target = (entry.get("datasourceId", ""), entry.get("sourceSchema", "").lower())
        bare_targets.setdefault(bare, set()).add(target)
        index.setdefault(bare, entry)
    ambiguous_bare = {bare: targets for bare, targets in bare_targets.items() if len(targets) > 1}
    for bare in ambiguous_bare:
        index.pop(bare, None)
    return index, ambiguous_bare


def _match_routing(
    table: exp.Table,
    lookup: dict[str, dict[str, str]],
    ambiguous_bare: dict[str, set[tuple[str, str]]],
) -> dict[str, str] | None:
    """Resolve ``table`` against the routing index, qualified form first.

    Returns ``None`` when the reference is simply unrouted — the caller abandons
    the whole rewrite, which degrades to today's behaviour.

    Raises:
        QualificationError: when the reference is BARE and that bare name is
            claimed by two ``(datasource, schema)`` pairs. Nothing downstream can
            recover: the collision destroyed information UPSTREAM, at induction
            time. A mapping produced before this fix emits ``rr:tableName`` bare, so both sources'
            TriplesMaps declare the same logical table, the VKG routing map keeps
            only the last one, and the H2 validation schema — ``CREATE TABLE IF
            NOT EXISTS`` — silently skips the second, leaving Ontop to reason over
            the FIRST table's column set. The SQL that arrives here may already
            name columns belonging to the other source's table, so guessing a
            catalog risks reading the wrong physical table. Re-inducing the
            namespace makes the mapping emit ``"schema"."table"`` for the shared
            name (``inducer/strategies/base.py:logical_table_names``), after which
            the reference carries its schema and resolves here without raising.
    """
    name = table.name.lower()
    if table.db:
        qualified = f"{table.db.lower()}.{name}"
        if qualified in lookup:
            return lookup[qualified]
    if name in ambiguous_bare:
        claimants = sorted(f"{ds or '?'}/{schema or '?'}" for ds, schema in ambiguous_bare[name])
        raise QualificationError(
            f"Cannot execute a cross-source query: table '{table.name}' exists in more than one "
            f"data source or schema ({claimants}) and the query does not say which. "
            "Re-induce this namespace so its mappings carry schema-qualified table names."
        )
    return lookup.get(name)
