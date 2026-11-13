# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-engine database adapters for the direct-JDBC serve path.

The direct-JDBC executor (``clients/source_db.py``) delegates every
engine-specific decision to an :class:`EngineAdapter`. Each supported engine has
exactly one adapter that co-locates:

- the connection transport (SQLAlchemy async engine for wire-protocol drivers,
  or a synchronous driver wrapped in ``asyncio.to_thread`` — e.g. python-tds for
  SQL Server),
- the sqlglot dialect used to parse/transpile SQL for that engine,
- the per-transaction session settings (statement timeout, search_path, ...),
- the row-limit injection idiom (``LIMIT`` vs ``TOP``).

The executor holds NO engine ``if`` branches: it resolves ONE adapter via
:func:`get_adapter` and calls the abstract methods. Adding a new engine is a new
adapter subclass plus one registry entry — no executor edit.

``ConnHandle`` is the opaque connection object an adapter caches and later runs
queries against; its concrete type is private to each adapter (a SQLAlchemy
``AsyncEngine`` for the asyncpg/aiomysql adapters, a connection factory for the
python-tds adapter). The executor treats it as opaque.
"""

from __future__ import annotations

import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import sqlglot
import sqlglot.errors
import structlog

logger = structlog.get_logger(__name__)

# Opaque connection handle. Each adapter defines its own concrete type; the
# executor only ever passes it back to the SAME adapter's methods.
ConnHandle = Any


@dataclass(frozen=True)
class DBCredentials:
    """Resolved connection credentials + metadata for a source database."""

    username: str
    password: str
    host: str
    port: int
    database: str
    # Comma-separated search-path schema list. Defaults to "public" for parity with
    # the PostgreSQL/Redshift path (the only engines that consume it); MySQL/SQL
    # Server adapters ignore it. The real credential path always sets it explicitly
    # via _resolve_search_path.
    schema: str = "public"
    # Source engine ("POSTGRESQL"/"REDSHIFT"/"MYSQL"/"SQLSERVER"/...). Selects the
    # adapter and therefore the driver/dialect/session behavior.
    engine_type: str = "POSTGRESQL"


def build_ssl_context() -> ssl.SSLContext:
    """Permissive TLS context for source-DB connections.

    Redshift enforces TLS (``require_ssl=true`` by default), managed PostgreSQL
    (RDS/Aurora) accepts it, and managed MySQL (RDS) commonly requires it too, so
    the direct-JDBC executor must offer SSL or a require_ssl source refuses the
    plaintext connection.

    NOTE: this is intentionally MORE permissive than a fully-verifying context —
    it DISABLES hostname and certificate-chain verification because source
    endpoints (Redshift Serverless, RDS) commonly present certificates that don't
    chain to the container's default trust store; full verification would fail the
    handshake. The security boundary is the authenticated DB credentials + the
    VPC/SG network path, not TLS peer-identity pinning; this gives transport
    encryption in transit, not mutual auth. (A future hardening step could load
    the AWS RDS/Redshift CA bundle and re-enable verification.)
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Built once at import — a single shared context is safe across connections/engines.
SSL_CONTEXT = build_ssl_context()


class EngineAdapter(ABC):
    """Base class for per-engine direct-JDBC behavior.

    Subclasses set the three class vars and implement connection + query
    execution. ``inject_limit`` has a sensible default (sqlglot ``LIMIT`` cap)
    that engines with a different row-limit idiom (SQL Server ``TOP``) override.
    """

    #: Canonical engine token as stored in the source configuration blob
    #: (matches DIRECT_QUERY_ENGINES / the sources connector registry).
    engine_type: ClassVar[str]
    #: sqlglot dialect used to (a) transpile Trino->engine in the composite
    #: executor and (b) parse the already-transpiled SQL for firewall + limit.
    sqlglot_dialect: ClassVar[str]
    #: Default TCP port when the source config omits one.
    default_port: ClassVar[int]

    @abstractmethod
    async def open_connection(self, creds: DBCredentials) -> ConnHandle:
        """Create and return a cacheable connection handle for the source.

        Called once per (namespace, data_source) cache key. Whatever is returned
        is later passed back to :meth:`run` and :meth:`close`.
        """
        raise NotImplementedError

    @abstractmethod
    async def run(
        self,
        conn: ConnHandle,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        schema: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Execute read-only SQL and return ``(rows, columns)``.

        The adapter is responsible for applying engine-appropriate session
        settings (statement timeout, search_path) inside this call.
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self, conn: ConnHandle) -> None:
        """Dispose the connection handle and release resources."""
        raise NotImplementedError

    def _limit_fallback(self, sql: str, max_rows: int) -> str:
        """String-concat row cap used only when sqlglot parse/render fails.

        Default is the ``LIMIT`` idiom (PostgreSQL/Redshift/MySQL). A ``TOP``-only
        engine (SQL Server) MUST NOT emit a literal ``LIMIT``, so ``SqlServerAdapter``
        overrides this hook — the AST happy path in :meth:`inject_limit` is shared.
        """
        return f"{sql.rstrip(';')} LIMIT {int(max_rows)}"

    def inject_limit(self, sql: str, max_rows: int) -> str:
        """Cap the result size at the TOP LEVEL, parsing/emitting in this dialect.

        Enforces a row cap on the ROOT statement: adds a ``LIMIT`` when the root has
        none, lowers an existing root ``LIMIT`` above the cap. sqlglot renders the
        root ``LIMIT`` node in the target dialect — for SQL Server that becomes
        ``SELECT TOP N`` (the ``tsql`` dialect renders the root ``Limit`` node as
        ``TOP`` on this same happy path).

        CRITICAL: we inspect the ROOT node's ``limit`` arg, NOT ``find(exp.Limit)``.
        ``find`` returns the first ``Limit`` ANYWHERE depth-first — including inside
        a subquery (``SELECT * FROM (SELECT ... LIMIT 999999) z``) — so capping that
        would leave the OUTER result set unbounded and defeat the memory guard. The
        root's own ``limit`` arg is the only correct target.

        On any sqlglot parse/render failure we defer to :meth:`_limit_fallback`,
        which each engine overrides to use its own row-limit idiom (``LIMIT`` vs
        ``TOP``). We catch ``SqlglotError`` specifically — a genuine bug (e.g.
        ``AttributeError``) must surface, not be swallowed into the fallback.
        """
        dialect = self.sqlglot_dialect
        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            return self._limit_fallback(sql, max_rows)

        limit_node = parsed.args.get("limit") if isinstance(parsed, sqlglot.exp.Expression) else None
        if limit_node is None:
            parsed.set("limit", sqlglot.exp.Limit(expression=sqlglot.exp.Literal.number(max_rows)))
            try:
                return parsed.sql(dialect=dialect)
            except sqlglot.errors.SqlglotError:
                return self._limit_fallback(sql, max_rows)

        existing = limit_node.expression
        # Replace a non-integer or above-cap root limit; leave a within-cap one alone.
        if existing and getattr(existing, "is_int", False) and int(existing.this) <= max_rows:
            return sql
        limit_node.set("expression", sqlglot.exp.Literal.number(max_rows))
        try:
            return parsed.sql(dialect=dialect)
        except sqlglot.errors.SqlglotError:
            return self._limit_fallback(sql, max_rows)
