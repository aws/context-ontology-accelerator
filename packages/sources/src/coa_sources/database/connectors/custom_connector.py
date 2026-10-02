# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery for a customer-authored Athena Query Federation connector.

The customer writes a connector against AWS's Athena Query Federation SDK and
deploys it as a Lambda in their own account; the control plane registers it as a
``LAMBDA``-type Athena data catalog in this account at source-create. This module
then discovers that catalog's metadata **through Athena SQL** rather than through
the Athena metadata API:

* ``SHOW DATABASES IN <catalog>`` — confirm the configured database exists;
* ``SHOW TABLES IN <catalog>.<database>`` — enumerate tables;
* ``DESCRIBE <catalog>.<database>.<table>`` — one per table, for columns, types,
  and comments.

**Why SQL and not the API.** ``GetTableMetadata`` returns column names and types
only. It populates comments solely for ``GLUE``-type catalogs (where Athena reads
Glue's own comment column), and the federation protocol has no field for key
constraints anywhere — so a connector cannot report either through the metadata
API. Comments are exactly what this source type's ontology quality depends on,
and declared keys ride inside them as ``@pk``/``@fk`` tags (see
:mod:`~.constraint_tags`). ``DESCRIBE`` is a supported Athena interface that
returns them, which also keeps discovery decoupled from the connector's internal
wire protocol.

**Identifiers are backtick-quoted.** Athena parses these statements with two
different parsers and they disagree on quoting: ``SHOW``/``DESCRIBE`` accept
backticks and reject double quotes, while ``SELECT`` accepts double quotes and
rejects backticks. Every statement here is ``SHOW`` or ``DESCRIBE``, so all three
use backticks via :func:`_quote_ident`, which makes a name needing quoting
addressable rather than fatal. Serve needs no matching change, because it renders
table references through sqlglot, which double-quotes an identifier that requires
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    PrimaryKey,
    Table,
    TechnicalMetadata,
)
from coa_control_plane_server.models.source_sub_type import SourceSubType

from coa_sources.database.metrics import emit_metric

from .athena_statement import AthenaStatementError, AthenaStatementRunner
from .base import ConnectionCheck, ConnectionTestResult, MetadataConnector
from .constraint_tags import ParsedComment, assemble_constraints, parse_comment
from .filters import compile_filter

# structlog, not stdlib logging: these run in the sources-api and discovery
# Lambdas, whose setup_logging() pins the stdlib root logger to WARNING and
# builds a formatter without ExtraAdder — so stdlib logger.info(..., extra={...})
# emits nothing at all, dropping exactly the per-table accounting this module
# exists to produce. Matches athena_catalog.py, which switched for the same reason.
logger = structlog.get_logger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on bad input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid_int_env_ignored", var=name, value=raw, fallback=default)
        return default


# Concurrent DESCRIBE statements. Lower than the enum sampler's 16 on purpose:
# every DESCRIBE here spins up the CUSTOMER's connector Lambda, so the fan-out
# costs them invocations and is bounded by their concurrency limit, and Athena's
# DML concurrency quota is account-wide — shared with enum sampling and with
# serve traffic — so a scan that saturates it throttles work outside itself.
ATHENA_DISCOVERY_CONCURRENCY = _int_env("ATHENA_DISCOVERY_CONCURRENCY", 8)

# Same variable the discovery handler caps on, read here so the limit can be
# enforced BEFORE the DESCRIBE fan-out rather than on its result. Not via
# _int_env: that clamps to a minimum of 1, and 0 is the documented "no cap".
try:
    MAX_TABLES_PER_SOURCE = int(os.environ.get("MAX_TABLES_PER_SOURCE", "0"))
except ValueError:
    logger.warning(
        "invalid_int_env_ignored",
        var="MAX_TABLES_PER_SOURCE",
        value=os.environ.get("MAX_TABLES_PER_SOURCE"),
        fallback=0,
    )
    MAX_TABLES_PER_SOURCE = 0

# Identifiers safe to send unquoted to both of Athena's parsers. Still used to
# validate the catalog and database at registration — see test_connection — where
# the value also becomes serve's QueryExecutionContext and is compared against
# discovered names. Table names are NOT held to it: they arrive from SHOW TABLES
# and are quoted instead.
_UNQUOTED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(identifier: str) -> str:
    """Backtick-quote one identifier for Athena's ``SHOW``/``DESCRIBE`` parser.

    Hive-style: wrap in backticks and double any the name contains, which is the
    only escape that parser recognises. Double quotes are not usable here, since
    that parser rejects them, which is why discovery and serve spell quoting
    differently.

    Args:
        identifier: a catalog, database, or table name.
    """
    return "`" + identifier.replace("`", "``") + "`"


# Rows in DESCRIBE output that are structure, not columns. Hive-style DESCRIBE
# can emit section markers and blank separators; a federated catalog was not
# observed to, but the output shape is connector- and Athena-version dependent,
# so they are skipped rather than parsed into columns named "#".
_DESCRIBE_ROW_SKIP_PREFIXES = ("#",)


def _is_addressable_unquoted(identifier: str) -> bool:
    """Whether ``identifier`` can be sent unquoted to both Athena parsers."""
    return bool(_UNQUOTED_IDENTIFIER_RE.match(identifier or ""))


def _catalog_failure_hint(config: dict) -> str:
    """Advice for a catalog that would not resolve, chosen by who deployed the connector.

    For ``CUSTOM_CONNECTOR`` the customer owns the Lambda, so the commonest cause is a
    missing resource policy. For ``DATABRICKS_SQL_WAREHOUSE`` this deployment operates the
    connector, and registration already resolved its ARN, so reaching here means the
    function or the source's configuration parameter went away afterwards.
    """
    if config.get("source_sub_type") == SourceSubType.DATABRICKS_SQL_WAREHOUSE.value:
        return (
            "This deployment operates the connector for this source type, so this is an "
            "operator-side fault rather than anything to change on the source: registration "
            "resolved a deployed connector ARN, so either that function has since been removed or "
            "renamed, or this source's connector configuration parameter is missing. Check that "
            "the Databricks connector stack is deployed under the name the catalog was created "
            "with, and that the parameter for this catalog name still exists."
        )
    return (
        "Check that the connector Lambda's resource policy allows this deployment's discovery and "
        "serve roles to invoke it, and that the Lambda is deployed and healthy."
    )


class CustomConnector(MetadataConnector):
    """Discover a Lambda-backed Athena data catalog via Athena SQL."""

    def __init__(self, runner: AthenaStatementRunner | None = None) -> None:
        """Build the connector.

        Args:
            runner: Statement runner override, for tests. The default is built
                once here rather than per statement so the boto3 client is
                created before the ``DESCRIBE`` fan-out starts, which is the
                supported way to share a client across threads.
        """
        self._runner = runner or AthenaStatementRunner()

    # ── Connection test ──────────────────────────────────────────────────

    def test_connection(self, config: dict) -> ConnectionTestResult:
        """Verify the catalog resolves and the connector answers a metadata call.

        The discovery handler calls this before ``discover_metadata`` and raises
        on failure, which makes it the fast-fail for the one piece of wiring this
        service cannot do itself: the customer must add a resource policy on their
        connector Lambda allowing our roles to invoke it. Registration does not
        need that policy — it only records a name → ARN mapping — so a missing
        grant first shows up here, and the message has to say so.
        """
        checks: list[ConnectionCheck] = []
        catalog = config.get("athena_data_catalog_name", "")
        database = config.get("database_name", "")

        if not catalog:
            return ConnectionTestResult(
                success=False,
                message="athena_data_catalog_name is required",
                checks=[
                    ConnectionCheck(
                        check="configuration",
                        status="failed",
                        message="The source record carries no Athena data catalog name.",
                    )
                ],
            )
        if not database:
            return ConnectionTestResult(
                success=False,
                message="database_name is required",
                checks=[
                    ConnectionCheck(
                        check="configuration",
                        status="failed",
                        message="customConnectorConfiguration.databaseName is required for this source type.",
                    )
                ],
            )
        for label, value in (("catalog", catalog), ("database", database)):
            if not _is_addressable_unquoted(value):
                return ConnectionTestResult(
                    success=False,
                    message=f"{label} name {value!r} cannot be addressed unquoted",
                    checks=[
                        ConnectionCheck(
                            check="configuration",
                            status="failed",
                            message=(
                                f"This {label} also becomes the Athena query context serve sets for every "
                                f"query, and is matched against discovered names, so it is held to letters, "
                                f"digits, and underscores, starting with a letter or underscore. Table names "
                                f"are not: those are quoted."
                            ),
                        )
                    ],
                )

        try:
            databases = self._list_databases(catalog)
        except AthenaStatementError as exc:
            # Athena reports an AccessDenied naming our own role, which points at neither
            # fix, so the message has to name the likely cause per sub-type.
            return ConnectionTestResult(
                success=False,
                message=(
                    f"Could not list databases in Athena data catalog '{catalog}'. "
                    f"{_catalog_failure_hint(config)} Athena reported: {exc}"
                ),
                checks=[ConnectionCheck(check="catalog_access", status="failed", message=str(exc))],
            )

        checks.append(
            ConnectionCheck(
                check="catalog_access",
                status="ok",
                message=f"Catalog '{catalog}' resolved; {len(databases)} database(s) visible.",
            )
        )
        if database not in databases:
            return ConnectionTestResult(
                success=False,
                message=(
                    f"Database '{database}' was not found in catalog '{catalog}'. "
                    f"The connector reports: {', '.join(sorted(databases)) or '(none)'}."
                ),
                checks=[
                    *checks,
                    ConnectionCheck(
                        check="database_exists",
                        status="failed",
                        message=f"'{database}' is not among the databases the connector lists.",
                    ),
                ],
            )
        checks.append(
            ConnectionCheck(check="database_exists", status="ok", message=f"Database '{database}' is visible.")
        )
        return ConnectionTestResult(success=True, message="Custom connector reachable.", checks=checks)

    # ── Discovery ────────────────────────────────────────────────────────

    def discover_metadata(self, config: dict) -> DiscoveredMetadata:
        """Discover the configured database's tables, columns, and constraints.

        Raises:
            ValueError: the configuration is unusable, or the configured database
                is not one the connector exposes.
            RuntimeError: every listed table failed to read. A source with tables
                but no columns is not a successful scan — it would reach review
                looking empty rather than broken.
        """
        catalog = config.get("athena_data_catalog_name", "")
        database = config.get("database_name", "")
        if not catalog or not database:
            # Named from the record rather than hardcoded: this connector serves every
            # connector-backed sub-type, so a message naming CUSTOM_CONNECTOR would
            # misdirect a steward reading it about a Databricks source.
            sub_type = config.get("source_sub_type") or "connector-backed"
            raise ValueError(f"athena_data_catalog_name and database_name are required for a {sub_type} source")
        for label, value in (("catalog", catalog), ("database", database)):
            if not _is_addressable_unquoted(value):
                raise ValueError(
                    f"Athena {label} name {value!r} is not addressable: this value becomes serve's query "
                    f"context and is matched against discovered names, so it must be letters, digits, and "
                    f"underscores. Table names are quoted instead and have no such restriction"
                )

        databases = self._list_databases(catalog)
        if database not in databases:
            raise ValueError(
                f"Database {database!r} not found in Athena data catalog {catalog!r}; "
                f"the connector lists: {sorted(databases)}"
            )

        table_names = self._list_tables(catalog, database)
        kept = self._apply_filters(
            table_names,
            include=config.get("table_filter"),
            exclude=config.get("table_exclude_filter"),
        )
        logger.info(
            "custom_connector_tables_listed",
            catalog=catalog,
            database=database,
            listed=len(table_names),
            after_filters=len(kept),
        )
        if not kept:
            # Not an error: a filter that matches nothing, or an empty database,
            # is a configuration outcome the steward can see and correct.
            return DiscoveredMetadata(tables=[])

        # Enforced HERE, before the fan-out, not only in discovery_handler. That
        # check runs on `metadata.tables` — i.e. after this method has already
        # returned — so a connector exposing thousands of tables would fan out
        # first and be counted afterwards. A Lambda hard-timeout mid-fan-out skips
        # the handler's `except` entirely, so nothing writes SCAN_FAILED or an
        # errorMessage: the source stays in SCANNING, where DELETE and re-scan both
        # 409, until the reaper fires. Raising up front keeps the failure a
        # readable configuration error instead of a stuck source.
        if MAX_TABLES_PER_SOURCE and len(kept) > MAX_TABLES_PER_SOURCE:
            raise ValueError(
                f"{catalog}.{database} exposes {len(kept)} tables after filters, exceeding the limit of "
                f"{MAX_TABLES_PER_SOURCE}. Narrow the scope with tableFilter / tableExcludeFilter."
            )

        tables, failed = self._describe_tables(catalog, database, kept, config)
        if failed and not tables:
            raise RuntimeError(
                f"Every table in {catalog}.{database} failed to describe ({len(failed)} of {len(kept)}); "
                f"the connector is reachable but returned no readable schema"
            )

        emit_metric("CustomConnectorTablesDiscovered", len(tables), "Count")
        if failed:
            # Loud on purpose. These tables reach review with no columns and no
            # keys, and enrichment will fill descriptions over the gap, so the
            # count has to leave the logs.
            emit_metric("CustomConnectorTablesFailed", len(failed), "Count")
            logger.warning(
                "custom_connector_tables_degraded",
                catalog=catalog,
                database=database,
                failed_tables=failed,
            )
        return DiscoveredMetadata(tables=tables, failed_tables=[f"{database}.{t}" for t in failed])

    # ── Statement helpers ────────────────────────────────────────────────

    def _list_databases(self, catalog: str) -> set[str]:
        """Databases the connector exposes, from ``SHOW DATABASES IN <catalog>``."""
        rows = self._runner.run(f"SHOW DATABASES IN {_quote_ident(catalog)}", header_row="absent")
        return {row[0] for row in rows if row and row[0]}

    def _list_tables(self, catalog: str, database: str) -> list[str]:
        """Table names from ``SHOW TABLES IN <catalog>.<database>``."""
        rows = self._runner.run(f"SHOW TABLES IN {_quote_ident(catalog)}.{_quote_ident(database)}", header_row="absent")
        return [row[0] for row in rows if row and row[0]]

    @staticmethod
    def _apply_filters(names: list[str], *, include: str | None, exclude: str | None) -> list[str]:
        """Apply the include filter, then the exclude filter, preserving order."""
        include_re = compile_filter(include, "tableFilter")
        exclude_re = compile_filter(exclude, "tableExcludeFilter")
        kept = [n for n in names if include_re is None or include_re.match(n)]
        if exclude_re is not None:
            kept = [n for n in kept if not exclude_re.match(n)]
        return kept

    def _describe_tables(
        self, catalog: str, database: str, table_names: list[str], config: dict
    ) -> tuple[list[Table], list[str]]:
        """Describe every table concurrently, returning ``(tables, failed_names)``.

        Fail-soft per table: one table's failure must not cost the whole scan, so
        the exception is caught here and the name recorded. The caller turns the
        record into a scan-level signal.
        """
        tables: list[Table] = []
        failed: list[str] = []
        workers = min(ATHENA_DISCOVERY_CONCURRENCY, len(table_names))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._describe_one, catalog, database, name, config): name for name in table_names}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    table = future.result()
                except AthenaStatementError:
                    logger.warning(
                        "custom_connector_describe_failed",
                        catalog=catalog,
                        database=database,
                        table=name,
                        exc_info=True,
                    )
                    failed.append(name)
                    continue
                except Exception:
                    # A parse or assembly bug must also cost only this table —
                    # the alternative is one malformed comment failing the scan.
                    logger.warning(
                        "custom_connector_table_build_failed",
                        catalog=catalog,
                        database=database,
                        table=name,
                        exc_info=True,
                    )
                    failed.append(name)
                    continue
                if table.columns:
                    tables.append(table)
                else:
                    # A table that describes to zero columns is indistinguishable
                    # from one we failed to read, and shipping it would put an
                    # empty table into the ontology for enrichment to invent.
                    logger.warning(
                        "custom_connector_table_has_no_columns",
                        catalog=catalog,
                        database=database,
                        table=name,
                    )
                    failed.append(name)
        # as_completed yields in completion order, so sort for a stable result —
        # downstream hashing and the review UI should not reorder between scans.
        tables.sort(key=lambda t: t.name)
        failed.sort()
        return tables, failed

    def _describe_one(self, catalog: str, database: str, table: str, config: dict) -> Table:
        """Run ``DESCRIBE`` for one table and assemble its :class:`Table`.

        The table name is quoted rather than vetted. A name needing quoting (a
        hyphen, a reserved word, mixed case) describes correctly once quoted, and
        refusing it drops a table serve could have queried. Quoting is also what
        makes an embedded backtick safe.
        """
        rows = self._runner.run(
            f"DESCRIBE {_quote_ident(catalog)}.{_quote_ident(database)}.{_quote_ident(table)}",
            header_row="absent",
        )
        parsed_columns = _parse_describe_rows(rows)
        return self._build_table(database, table, parsed_columns, config)

    @staticmethod
    def _build_table(
        database: str,
        table: str,
        parsed_columns: list[tuple[str, str, ParsedComment]],
        config: dict,
    ) -> Table:
        """Assemble a :class:`Table` from parsed ``DESCRIBE`` rows."""
        columns = [
            Column(
                name=name,
                data_type=data_type,
                # DESCRIBE carries no nullability and neither does Athena's Column type,
                # so `@notnull` in the comment is the only channel. An absent tag must
                # keep meaning nullable: every connector deployed before the tag existed
                # emits none, and their columns would otherwise all flip.
                nullable=not comment.is_not_null,
                business_metadata=(
                    BusinessMetadata(
                        description=comment.description,
                        enrichment_source=EnrichmentSource.DETERMINISTIC,
                        confidence=1.0,
                    )
                    if comment.description
                    else BusinessMetadata()
                ),
            )
            for name, data_type, comment in parsed_columns
        ]
        # Order matters: assemble_constraints takes DESCRIBE order as the
        # composite primary key's column order.
        primary_key, foreign_keys = assemble_constraints([(name, comment) for name, _, comment in parsed_columns])
        return Table(
            name=table,
            database=database,
            data_source_id=config.get("data_source_id", ""),
            namespace_id=config.get("namespace_id", ""),
            technical_metadata=TechnicalMetadata(column_count=len(columns)),
            # Deliberately no table-level description. The bare DESCRIBE form
            # returns column rows only, and a table comment appears solely under
            # EXTENDED/FORMATTED, which federated catalogs reject — so table
            # descriptions always come from enrichment for this source type.
            business_metadata=BusinessMetadata(),
            # An empty PrimaryKey, not None: the field is non-optional, and an
            # empty one reads as "no declared key" everywhere downstream.
            primary_key=primary_key or PrimaryKey(),
            foreign_keys=foreign_keys,
            columns=columns,
            technical_metadata_hash=_compute_hash(columns),
        )


def _parse_describe_rows(rows: list[list[str | None]]) -> list[tuple[str, str, ParsedComment]]:
    """Turn ``DESCRIBE`` rows into ``(name, data_type, parsed_comment)`` triples.

    Athena advertises three columns for ``DESCRIBE`` (``col_name``/``data_type``/
    ``comment``) but, against a Lambda-backed catalog, returns each row as a
    **single** cell holding all three fields TAB-separated (verified live). The
    output shape is connector- and Athena-version dependent, so both layouts are
    handled: a row with several cells is read positionally, and a single cell is
    split on tabs.

    Unparsable rows are skipped rather than raising. A ``DESCRIBE`` that returns
    nothing usable yields no columns, which the caller already treats as a failed
    table — so nothing is lost silently, and one odd row does not cost the rest.
    """
    out: list[tuple[str, str, ParsedComment]] = []
    for row in rows:
        fields = _describe_row_fields(row)
        if fields is None:
            continue
        name, data_type, raw_comment = fields
        out.append((name, data_type, parse_comment(raw_comment)))
    return out


def _describe_row_fields(row: list[str | None]) -> tuple[str, str, str] | None:
    """Extract ``(name, data_type, comment)`` from one row, or ``None`` to skip.

    Positions are read by index, never compacted. Athena reports an absent cell as
    ``None``, and dropping those shifts every later field left: the row
    ``["customer_id", None, "@pk surrogate key"]`` would compact to two cells and be
    read as ``data_type="@pk surrogate key"``, giving the column a nonsense type AND
    losing the tag before ``parse_comment`` ever sees it — silently, because a
    non-empty ``data_type`` passes the guard below. Read positionally, that row has no
    type and is skipped, which the caller counts as an unreadable column.
    """
    padded = [(row[i] or "") if i < len(row) else "" for i in range(3)]
    name, data_type, comment = padded
    # The live shape for a LAMBDA catalog is ONE cell with tab-separated fields,
    # despite the result set advertising three columns. Detected by an empty second
    # position rather than by cell count, so it also covers a three-column result
    # whose first cell happens to be packed.
    if not data_type and "\t" in name:
        # maxsplit=2 so a comment containing a tab stays intact.
        parts = name.split("\t", 2)
        if len(parts) < 2:
            return None
        name, data_type = parts[0], parts[1]
        comment = parts[2] if len(parts) > 2 else ""
    name, data_type, comment = name.strip(), data_type.strip(), comment.strip()
    if not name or not data_type or name.startswith(_DESCRIBE_ROW_SKIP_PREFIXES):
        return None
    return name, data_type, comment


def _compute_hash(columns: list[Column]) -> str:
    """Deterministic hash of the column schema, for re-scan change detection.

    Mirrors ``GlueCatalogConnector._compute_hash`` so both connectors' hashes
    change under the same conditions.
    """
    normalized = sorted(
        [{"n": c.name, "t": c.data_type, "p": c.is_partition_key} for c in columns],
        key=lambda x: str(x["n"]),
    )
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:16]
