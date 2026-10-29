# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources registry — shared DynamoDB access for data source metadata.

Both SourceDBQueryExecutor (direct JDBC) and AthenaQueryExecutor (federation)
need to look up source records. This module provides a single access point
to avoid duplicating DDB key schema, configuration parsing, and boto3 setup.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from coa_common import resolve_namespace_id, resolve_region, sync_boto_config
from coa_common.language_tags import LanguageTagError, canonicalize_language_tag, effective_namespace_language

from ..query_utils import validate_namespace
from ..tier2.table_qualifier import AMBIGUOUS_SOURCE

logger = structlog.get_logger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_WORKGROUP_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_RESOLVE_TIMEOUT_S = 5.0
_CACHE_TTL_S = 120.0
# A failed Glue listing is retried sooner than a good one is refreshed: until it
# is, every table it would have placed stays unplaced (and a cross-database query
# is refused), so one throttle should not cost two minutes.
_FAILED_LISTING_TTL_S = 15.0
# Max SRC# records fetched per namespace query. If DynamoDB reports more results
# beyond this page (LastEvaluatedKey present), callers that derive a zero-count
# signal (get_source_composition) MUST treat the read as unknown, since a source
# of the "missing" type could exist beyond the page.
_QUERY_LIMIT = 50
# Upper bound on the number of namespaces held in the raw SRC# items cache. Keeps
# a long-lived process from growing without bound under high namespace cardinality
# (TTL alone only invalidates on read, it never evicts).
_ITEMS_CACHE_MAX = 1024


class NamespaceLanguageUnavailableError(RuntimeError):
    """The namespace's language could not be determined.

    Raised for a missing namespace item, a failed read, or a stored value that is
    not a supported tag. Never resolved to ``en``: only an existing item without a
    ``language`` attribute (a namespace created before the field) reads as ``en``.
    """


# Source-type literals as stored in the sources DDB table (mirror of the
# generated control-plane SourceType enum: DATABASE / DOCUMENTS). Kept as string
# literals here to avoid a cross-package import into the serve path — the same
# literals are already used for routing at find_database_source / find_sole_database_source.
_SOURCE_TYPE_DATABASE = "DATABASE"
_SOURCE_TYPE_DOCUMENTS = "DOCUMENTS"
_GLUE_SUB_TYPE = "GLUE_DATABASE"
# Athena's root catalog, lower-cased as the scope comparison is.
_ROOT_CATALOG_LC = "awsdatacatalog"

# (namespace, sorted (glue catalog id, database) pairs) — see `_glue_table_owners`.
_GlueListingKey = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class SQLNamespaceScope:
    """Physical Athena/Redshift objects a namespace is permitted to reference.

    Native Glue sources are addressed through ``AwsDataCatalog.<database>``.
    Federated JDBC/custom-connector sources are addressed through their generated
    catalog and discovered schema. The map deliberately omits table names: source
    onboarding, rather than a model/LLM table list, is the tenancy boundary.
    """

    native_databases: frozenset[str]
    federated_catalog_schemas: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class SourceComposition:
    """What kinds of data sources a namespace has, for serve-tier gating.

    ``has_structured_source`` gates Tier 1 (metrics) + Tier 2 (VKG/Ontop NL→SQL), which can
    only produce a result when a DATABASE source (with R2RML mappings) exists.
    ``has_unstructured_source`` gates Tier 3 (RAG over document chunks), which needs the
    chunk_/statement_ AOSS indexes produced by DOCUMENTS ingestion.

    ``unknown`` is True when the composition could not be determined (registry
    unavailable, timeout, or error). Callers MUST fail open on unknown — i.e. run
    every tier — so a lookup miss never silently drops a tier that would answer.
    """

    has_structured_source: bool
    has_unstructured_source: bool
    unknown: bool = False

    @classmethod
    def unknown_composition(cls) -> SourceComposition:
        """Fail-open sentinel: treat the namespace as if it has every source type."""
        return cls(has_structured_source=True, has_unstructured_source=True, unknown=True)


def _glue_database_mismatch(item: dict[str, Any], database: str, namespace: str) -> bool:
    """True when a Glue record's ``discoveredSchemas[0]`` is not its ``database``.

    The executor runs a Glue record against ``discoveredSchemas[0]`` when there is
    one (AthenaQueryExecutor), not ``athenaDatabase``. When the two disagree the
    record is not used to place tables, and a legacy root-catalog record is not
    granted either: both would act on a database the source is not queried as.
    """
    discovered = [str(d).lower() for d in item.get("discoveredSchemas") or []]
    if not discovered or discovered[0] == database:
        return False
    logger.warning(
        "glue_database_mismatch",
        namespace=namespace,
        athena_database=database,
        discovered_schema=discovered[0],
    )
    return True


class SourcesRegistry:
    """Read-only access to the sources and namespaces DynamoDB tables with TTL caching."""

    def __init__(
        self,
        table_name: str | None = None,
        namespaces_table: str | None = None,
        region: str | None = None,
        executor: ThreadPoolExecutor | None = None,
    ):
        """Configure the DynamoDB tables, region, and executor for cached reads.

        Args:
            table_name: Sources table name. Defaults to the ``DATA_SOURCES_TABLE`` env var.
            namespaces_table: Namespaces table name. Defaults to ``NAMESPACES_TABLE``.
            region: AWS region. Defaults to the resolved region.
            executor: Thread pool for running blocking boto3 calls off the event loop.
        """
        self._table_name = table_name or os.environ.get("DATA_SOURCES_TABLE", "")
        self._namespaces_table = namespaces_table or os.environ.get("NAMESPACES_TABLE", "")
        self._region = region or resolve_region()
        self._executor = executor
        if self._table_name or self._namespaces_table:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region, config=sync_boto_config())
        else:
            self._dynamodb = None
        self._db_cache: dict[str, tuple[str, float]] = {}
        self._ns_cache: dict[str, tuple[str, float]] = {}
        # Single source-of-truth cache for the raw SRC# records of a namespace,
        # keyed by namespace. All derived signals (sole DATABASE source, source
        # composition) read from here, so one request no longer fires the same
        # SRC# query multiple times across gating + Tier-2 routing. Bounded LRU so
        # a long-lived process serving high-cardinality namespaces cannot grow
        # without bound. Truncated/uncertain reads are never cached (see
        # _query_sources), so the cache only ever holds complete result sets.
        self._items_cache: OrderedDict[str, tuple[list[dict[str, Any]], float]] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        # Namespace language is immutable once the namespace exists, so a value
        # read from its METADATA item never goes stale. Bounded LRU, no TTL.
        self._language_cache: OrderedDict[str, str] = OrderedDict()
        # Only a namespace pooling several Glue databases ever lists the catalog
        # (see ``table_placements``). Keyed by (namespace, listed databases), so a
        # source added or removed since the listing is a cache miss, not a stale
        # answer about which databases hold a name.
        self._glue: Any = (
            boto3.client("glue", region_name=self._region, config=sync_boto_config()) if self._table_name else None
        )
        self._glue_tables_cache: OrderedDict[_GlueListingKey, tuple[dict[str, set[str]] | None, float]] = OrderedDict()
        # One listing in flight per key: a burst of cache misses shares it rather
        # than each holding a pool thread (a timed-out listing keeps its thread).
        self._glue_inflight: dict[_GlueListingKey, asyncio.Future[dict[str, set[str]] | None]] = {}

    @property
    def available(self) -> bool:
        """Return whether a DynamoDB resource and sources table are configured."""
        return bool(self._dynamodb and self._table_name)

    @property
    def namespaces_configured(self) -> bool:
        """Whether the namespaces table is wired (so existence CAN be checked).

        Lets the entrypoint distinguish ``namespace_exists() -> None`` meaning
        "feature absent, cannot check" (this is False) from "configured but the
        lookup errored" (this is True). Only the latter must fail CLOSED — a
        deployment without a namespaces table must still serve requests.
        """
        return bool(self._namespaces_table and self._dynamodb)

    def _resolve_ns(self, namespace: str) -> str:
        """Resolve namespace name to UUID for DDB key construction."""
        if not self._namespaces_table or not self._dynamodb:
            return namespace
        try:
            table = self._dynamodb.Table(self._namespaces_table)
            return resolve_namespace_id(namespace, table)
        except (ValueError, Exception):
            return namespace

    async def namespace_exists(self, namespace: str) -> bool | None:
        """Return whether a namespace exists in the namespaces table.

        Returns:
            ``True``  — the namespace resolves and has a ``METADATA`` record.
            ``False`` — the name/UUID does not resolve to an existing namespace.
            ``None``  — existence is *undeterminable* (table not configured, or a
                        DynamoDB error). Callers MUST treat ``None`` as "don't
                        know" and fail OPEN (proceed) rather than 404, so a
                        transient lookup failure or an unconfigured table never
                        blocks otherwise-valid queries.

        Unlike ``_resolve_ns`` (which swallows a miss and echoes the input so
        downstream key construction still works), this distinguishes a genuine
        "not found" from "couldn't check" so the entrypoint can return a precise
        404 for an unknown namespace instead of silently resolving a query
        against a namespace that does not exist.
        """
        if not self._namespaces_table or not self._dynamodb:
            return None
        loop = asyncio.get_running_loop()
        table = self._dynamodb.Table(self._namespaces_table)
        try:
            ns_id = await loop.run_in_executor(self._executor, lambda: resolve_namespace_id(namespace, table))
        except ValueError:
            # Name/UUID does not resolve to any namespace — a genuine "not found".
            return False
        except Exception:
            logger.warning("namespace_exists_resolve_failed", namespace=namespace, exc_info=True)
            return None
        try:
            resp = await loop.run_in_executor(
                self._executor,
                lambda: table.get_item(Key={"PK": f"NS#{ns_id}", "SK": "METADATA"}),
            )
        except Exception:
            logger.warning("namespace_exists_metadata_failed", namespace=namespace, exc_info=True)
            return None
        item = resp.get("Item")
        if item:
            # The existence gate already holds the item: remember its language so
            # the request's label preference costs no second read.
            self._remember_language(namespace, item)
        return bool(item)

    def _remember_language(self, namespace: str, item: dict[str, Any]) -> None:
        """Cache the canonical language of a loaded METADATA item, if valid."""
        try:
            language = canonicalize_language_tag(effective_namespace_language(item))
        except (LanguageTagError, TypeError, AttributeError):
            # Leave it uncached; namespace_language() re-reads and reports it.
            return
        self._language_cache[namespace] = language
        self._language_cache.move_to_end(namespace)
        while len(self._language_cache) > _ITEMS_CACHE_MAX:
            self._language_cache.popitem(last=False)

    async def namespace_language(self, namespace: str) -> str | None:
        """Return the namespace's canonical effective language.

        Returns:
            The stored language, ``"en"`` for an existing item written before the
            field existed, or ``None`` when no namespaces table is configured (the
            feature is absent, as for :meth:`namespace_exists`).

        Raises:
            NamespaceLanguageUnavailableError: The table is configured but the
                item is missing, the read failed, or the stored value is invalid.
        """
        if not self._namespaces_table or not self._dynamodb:
            return None
        cached = self._language_cache.get(namespace)
        if cached is not None:
            self._language_cache.move_to_end(namespace)
            return cached
        loop = asyncio.get_running_loop()
        table = self._dynamodb.Table(self._namespaces_table)
        try:
            ns_id = await loop.run_in_executor(self._executor, lambda: resolve_namespace_id(namespace, table))
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    lambda: table.get_item(Key={"PK": f"NS#{ns_id}", "SK": "METADATA"}),
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except Exception as exc:
            logger.warning("namespace_language_read_failed", namespace=namespace, error=type(exc).__name__)
            raise NamespaceLanguageUnavailableError(f"cannot read namespace {namespace!r}") from exc
        item = resp.get("Item")
        if not item:
            raise NamespaceLanguageUnavailableError(f"namespace {namespace!r} not found")
        try:
            language = canonicalize_language_tag(effective_namespace_language(item))
        except (LanguageTagError, TypeError, AttributeError) as exc:
            raise NamespaceLanguageUnavailableError(f"namespace {namespace!r} has an invalid language") from exc
        self._remember_language(namespace, item)
        return language

    async def get_source(self, namespace: str, data_source_id: str) -> dict[str, Any]:
        """Fetch a single source record by namespace + source ID."""
        if not self.available:
            return {}
        validate_namespace(namespace)
        if not _IDENTIFIER_PATTERN.match(data_source_id):
            raise ValueError(f"Invalid data_source_id format: {data_source_id!r}")
        ns_id = self._resolve_ns(namespace)
        loop = asyncio.get_running_loop()
        table = self._dynamodb.Table(self._table_name)
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    lambda: table.get_item(Key={"PK": f"NS#{ns_id}", "SK": f"SRC#{data_source_id}"}),
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning("sources_registry_timeout", op="get_source", namespace=namespace)
            return {}
        return response.get("Item", {})

    async def _query_sources(self, namespace: str) -> list[dict[str, Any]] | None:
        """Shared, cached DDB query for the SRC# records under a namespace.

        Returns the complete list of source items, or ``None`` when the result is
        uncertain — timeout, error, OR a truncated (paginated) response. Callers
        must treat ``None`` as "no reliable info" and fail toward running/keeping
        everything rather than dropping a tier or a source.

        The complete result is cached (per-namespace, 120s TTL, bounded LRU) so a
        single request that touches both tier-gating and Tier-2 routing issues the
        underlying query at most once. Uncertain reads are never cached, so a
        transient timeout or truncation is retried on the next call.
        """
        if not self.available:
            return None

        cached = self._items_cache.get(namespace)
        if cached is not None:
            items, ts = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                self._items_cache.move_to_end(namespace)  # LRU touch
                return items

        validate_namespace(namespace)
        ns_id = self._resolve_ns(namespace)
        loop = asyncio.get_running_loop()
        table = self._dynamodb.Table(self._table_name)
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    lambda: table.query(
                        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                        ExpressionAttributeValues={
                            ":pk": f"NS#{ns_id}",
                            ":prefix": "SRC#",
                        },
                        Limit=_QUERY_LIMIT,
                    ),
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning("sources_registry_timeout", op="_query_sources", namespace=namespace)
            return None

        # Authoritative truncation signal: DynamoDB sets LastEvaluatedKey when more
        # records exist beyond this page (either the Limit or the 1 MB page cap was
        # hit). Deriving a zero-count from a partial page could wrongly skip a tier,
        # so treat truncation as uncertain (None) and do NOT cache it.
        if response.get("LastEvaluatedKey"):
            # Include the returned count so operators can distinguish "many small
            # sources hit the Limit" from "few large items hit the 1 MB page cap".
            logger.warning(
                "sources_query_truncated",
                namespace=namespace,
                limit=_QUERY_LIMIT,
                items_returned=len(response.get("Items", [])),
            )
            return None

        items = response.get("Items", [])
        self._items_cache[namespace] = (items, time.monotonic())
        self._items_cache.move_to_end(namespace)
        while len(self._items_cache) > _ITEMS_CACHE_MAX:
            self._items_cache.popitem(last=False)  # evict least-recently-used
        return items

    async def find_database_source(self, namespace: str) -> dict[str, Any]:
        """Find the first DATABASE source in a namespace.

        Used when no specific data_source_id is provided (single-source namespaces).
        """
        items = await self._query_sources(namespace)
        if items is None:
            return {}
        for item in items:
            if item.get("sourceType") == _SOURCE_TYPE_DATABASE:
                return item
        return {}

    async def find_sole_database_source(self, namespace: str) -> dict[str, Any] | None:
        """Find the sole DATABASE source in a namespace, or None if ambiguous.

        Returns the source record only if exactly one DATABASE source exists.
        Returns None if zero or multiple DATABASE sources are found — the caller
        should fall back to Athena (ambiguous routing).

        The underlying SRC# query is cached in ``_query_sources`` (120s TTL), so
        repeated routing lookups within a request share a single round-trip.
        """
        items = await self._query_sources(namespace)
        if items is None:
            return None
        db_sources = [item for item in items if item.get("sourceType") == _SOURCE_TYPE_DATABASE]
        return db_sources[0] if len(db_sources) == 1 else None

    async def database_source_count(self, namespace: str) -> int:
        """Number of DATABASE sources registered to ``namespace``.

        Lets a caller tell a single-source namespace (where pinning the sole
        DATABASE source is sound) apart from a multi-source one (where pinning the
        FIRST source silently mis-routes same-named tables). Reuses the
        cached ``_query_sources`` round-trip. Returns 0 when the inventory is
        unavailable, which the caller must treat as "cannot confirm single-source".
        """
        items = await self._query_sources(namespace)
        if items is None:
            return 0
        return sum(1 for item in items if item.get("sourceType") == _SOURCE_TYPE_DATABASE)

    async def sql_namespace_scope(self, namespace: str) -> SQLNamespaceScope | None:
        """Return the physical SQL objects registered to ``namespace``.

        ``None`` means the source inventory is unavailable or incomplete and must
        be treated as a deny by the query executor. A partial inventory cannot
        safely authorize a qualified reference: it could omit an owned source, but
        allowing on uncertainty reopens F-3's cross-namespace catalog escape.
        """
        items = await self._query_sources(namespace)
        if items is None:
            logger.warning("sql_namespace_scope_unavailable", namespace=namespace)
            return None

        native_databases: set[str] = set()
        federated_catalog_schemas: set[tuple[str, str]] = set()
        for item in items:
            if item.get("sourceType") != _SOURCE_TYPE_DATABASE or item.get("queryable") is False:
                continue

            catalog = str(item.get("athenaDataCatalogName") or "").lower()
            # `POST /sources` copied the caller's
            # `glueConfiguration.athenaDataCatalogName` into this attribute until
            # 217d7c5fa (2026-09-03), and onboarding clients passed
            # `AwsDataCatalog`, so a deployment that onboarded Glue before then
            # still has such records. Filed as a federated
            # (awsdatacatalog, schema) pair they authorized nothing:
            # validate_namespace_sql_scope checks an `awsdatacatalog` reference
            # ONLY against `native_databases`, so the qualifier's own rewrite
            # ("awsdatacatalog"."bank_risk"."risk_ecl") was denied as outside the
            # namespace. Such a Glue record falls through to the native path
            # below, which authorizes its own `athenaDatabase` — the database a
            # bare query already runs against (the executor and the qualifier
            # still read the record as catalog `AwsDataCatalog` + its discovered
            # schema, which names the same database). Limited to Glue records: a
            # connector that carried this value would otherwise be granted a root
            # database it never owned.
            legacy_root_glue = catalog == _ROOT_CATALOG_LC and item.get("sourceSubType") == _GLUE_SUB_TYPE
            if catalog and not legacy_root_glue:
                for schema in item.get("discoveredSchemas") or []:
                    schema_name = str(schema).lower()
                    if _IDENTIFIER_PATTERN.fullmatch(catalog) and _IDENTIFIER_PATTERN.fullmatch(schema_name):
                        federated_catalog_schemas.add((catalog, schema_name))
                continue

            config = self.parse_configuration(item)
            database = str(
                item.get("athenaDatabase") or item.get("glueDatabaseName") or config.get("databaseName") or ""
            ).lower()
            if legacy_root_glue and _glue_database_mismatch(item, database, namespace):
                continue

            # A native Glue source can sit in a DISTINCT (non-root) Athena catalog,
            # declared at create and recorded in ``athenaCatalog`` (NOT the
            # system-managed ``athenaDataCatalogName``, which is unset for Glue).
            # The executor addresses it as ``<athenaCatalog>.<database>.<table>``
            # (AthenaQueryExecutor._resolve_catalog_and_database's glue_nested path),
            # so the qualifier attributes that catalog to its tables. It must be
            # authorized under that catalog, not folded into AwsDataCatalog — else a
            # correctly-qualified cross-catalog reference is denied. Key it by the
            # Glue DATABASE (the schema part of the three-part name), matching the
            # executor. A root ``athenaCatalog`` means "no nested catalog" and stays
            # on the native-database path below.
            glue_catalog = str(item.get("athenaCatalog") or "").lower()
            if glue_catalog and glue_catalog != "awsdatacatalog":
                # Authorize the nested catalog ONLY when source-create recorded that
                # its ownership was verified (assert_namespace_may_catalog ran at
                # POST /sources). `athenaCatalog` itself is derived from a
                # caller-supplied `catalogId` and a legacy or seeded row can carry a
                # name it never owned, so trusting the raw field would let a
                # declared-but-unowned catalog become an authorization grant. A row
                # without the marker (legacy, or a source onboarded before this
                # field existed) is NOT authorized 3-part here; it falls through to
                # the native-database path and must be re-onboarded to regain nested
                # addressing — the fail-closed direction.
                if item.get("athenaCatalogOwnershipVerified") is True:
                    if _IDENTIFIER_PATTERN.fullmatch(glue_catalog) and _IDENTIFIER_PATTERN.fullmatch(database):
                        federated_catalog_schemas.add((glue_catalog, database))
                    continue
                logger.warning(
                    "sql_namespace_scope_unverified_catalog_skipped",
                    namespace=namespace,
                    athena_catalog=glue_catalog,
                )
                # Fall through: keep the source addressable via its native database
                # under the default catalog rather than dropping it entirely.

            if _IDENTIFIER_PATTERN.fullmatch(database):
                native_databases.add(database)

        return SQLNamespaceScope(
            native_databases=frozenset(native_databases),
            federated_catalog_schemas=frozenset(federated_catalog_schemas),
        )

    async def native_glue_databases(self, namespace: str) -> dict[str, tuple[str, str]]:
        """``source_id -> (glue catalog id, database)`` for the root-catalog Glue sources.

        Only queryable Glue sources in the account's root catalog are listed: a
        nested/federated catalog is addressed as ``catalog.schema``, not as a bare
        database, so it cannot be named the same way.
        """
        items = await self._query_sources(namespace)
        out: dict[str, tuple[str, str]] = {}
        for item in items or []:
            if item.get("sourceType") != _SOURCE_TYPE_DATABASE or item.get("queryable") is False:
                continue
            if item.get("sourceSubType") != _GLUE_SUB_TYPE:
                continue
            nested = [str(item.get(k) or "").lower() for k in ("athenaDataCatalogName", "athenaCatalog")]
            if any(c and c != _ROOT_CATALOG_LC for c in nested):
                continue
            config = self.parse_configuration(item)
            database = str(
                item.get("athenaDatabase") or item.get("glueDatabaseName") or config.get("databaseName") or ""
            ).lower()
            if _glue_database_mismatch(item, database, namespace):
                # Not placeable under `database`: the executor queries a different one.
                continue
            source_id = str(item.get("sourceId") or str(item.get("SK", "")).removeprefix("SRC#"))
            if source_id and _IDENTIFIER_PATTERN.fullmatch(database):
                out[source_id] = (str(config.get("catalogId") or ""), database)
        return out

    async def table_placements(self, namespace: str, table_sources: dict[str, str]) -> dict[str, tuple[str, str]]:
        """Map each bare table name to ``(source_id, database)`` of the Glue source holding it.

        Only meaningful when a namespace pools SEVERAL root-catalog Glue databases:
        the SQL writer is shown bare table names, and a query joining tables from
        two of them cannot run against any one default database. Returns ``{}``
        for every other namespace, so the writer's prompt and the routing are
        unchanged there.

        A table's own ``data_source_id`` decides first. A table WITHOUT one (an
        uploaded ontology carries no R2RML, so its classes are unplaced) is looked
        up in the Glue catalog of the namespace's own databases, and placed only
        when exactly one of them holds that name. A name held by two or more of them
        (or by a database two sources share) maps to ``(AMBIGUOUS_SOURCE, "")``, so
        a query touching it is refused rather than run against a guessed database.
        That lookup runs only when EVERY
        database source of the namespace is one of these Glue databases: a JDBC
        (or nested-catalog) source cannot be listed the same way, so a same-named
        table there would be invisible and the guess could pick the wrong source.
        Such a table stays unplaced and is refused downstream, as before. A
        non-queryable database source, or a Glue record whose two database fields
        disagree, turns the lookup off for the same reason. A table whose id names
        some other source is never looked up: it is not a Glue table.

        Args:
            namespace: Namespace whose sources bound the lookup.
            table_sources: Bare table name -> the retrieved class's
                ``data_source_id`` ("" when the class is unplaced).
        """
        sources = await self.native_glue_databases(namespace)
        if len({db for _, db in sources.values()}) < 2:
            return {}
        out = {t.lower(): (s, sources[s][1]) for t, s in table_sources.items() if s in sources}
        unplaced = [t.lower() for t, s in table_sources.items() if not s]
        if unplaced and await self.database_source_count(namespace) == len(sources):
            # A database two sources both register cannot name one source.
            by_database: dict[str, list[str]] = {}
            for source_id, (_, database) in sources.items():
                by_database.setdefault(database, []).append(source_id)
            catalog = await self._glue_table_owners(namespace, sorted(set(sources.values())))
            if catalog is not None:
                for table in unplaced:
                    owners = catalog.get(table, set())
                    if not owners:
                        continue
                    database = next(iter(owners))
                    if len(owners) == 1 and len(by_database.get(database, [])) == 1:
                        out[table] = (by_database[database][0], database)
                    else:
                        out[table] = (AMBIGUOUS_SOURCE, "")
        return out

    async def same_name_databases(self, namespace: str, tables: list[str]) -> dict[str, dict[str, str]]:
        """For each of ``tables`` held by two or more of the namespace's Glue databases: ``{database: source_id}``.

        Answers "which tables of this name exist?" independently of what retrieval
        returned. Retrieval keeps a handful of classes, so it can return one of
        several same-named tables (one per study, say). Nothing then collides, and
        a query read that one table and returned its rows as the whole answer. The
        caller treats a name listed here as shared even when one copy was
        retrieved, and names every database in the refusal.

        Only the namespace's own root-catalog Glue databases are listed (as for
        :meth:`table_placements`), so a database outside its sources never makes a
        name look shared, and a name only one of them holds is not returned. A
        database two sources both register maps to ``AMBIGUOUS_SOURCE``. ``{}``
        when the namespace holds fewer than two such databases, or when any of
        them cannot be listed (the caller then behaves as before).

        Args:
            namespace: Namespace whose sources bound the lookup.
            tables: Bare table names to check.
        """
        if not tables:
            return {}
        sources = await self.native_glue_databases(namespace)
        if len({db for _, db in sources.values()}) < 2:
            return {}
        catalog = await self._glue_table_owners(namespace, sorted(set(sources.values())))
        if catalog is None:
            return {}
        by_database: dict[str, list[str]] = {}
        for source_id, (_, database) in sources.items():
            by_database.setdefault(database, []).append(source_id)
        out: dict[str, dict[str, str]] = {}
        for table in {t.lower() for t in tables}:
            owners = catalog.get(table, set())
            if len(owners) < 2:
                continue
            out[table] = {
                database: ids[0] if len(ids) == 1 else AMBIGUOUS_SOURCE
                for database in sorted(owners)
                for ids in [by_database.get(database, [])]
            }
        return out

    async def _glue_table_owners(self, namespace: str, databases: list[tuple[str, str]]) -> dict[str, set[str]] | None:
        """Bare table name -> the databases (of ``databases``) holding it; cached per listed set.

        ``None`` when ANY database could not be listed (e.g. a cross-account
        catalog the serve role cannot read, or a listing past the timeout): a
        partial listing could make a name look unique when it is not. The failure
        is cached too (for ``_FAILED_LISTING_TTL_S``), so a namespace that cannot be
        listed pays the timeout once per window rather than on every request.

        The key covers which databases were listed, not what they hold: a table
        created in one of them is seen only after the entry expires.
        """
        key: _GlueListingKey = (namespace, tuple(databases))
        cached = self._glue_tables_cache.get(key)
        if cached is not None:
            ttl = _CACHE_TTL_S if cached[0] is not None else _FAILED_LISTING_TTL_S
            if time.monotonic() - cached[1] < ttl:
                return cached[0]
        inflight = self._glue_inflight.get(key)
        if inflight is None:
            inflight = asyncio.ensure_future(self._list_glue_table_owners(key))
            self._glue_inflight[key] = inflight
            inflight.add_done_callback(lambda _f: self._glue_inflight.pop(key, None))
        return await asyncio.shield(inflight)

    async def _list_glue_table_owners(self, key: _GlueListingKey) -> dict[str, set[str]] | None:
        """List ``key``'s databases once and cache the result (``None`` on any failure)."""
        namespace, databases = key
        glue = self._glue

        def _list() -> dict[str, set[str]]:
            if glue is None:
                raise RuntimeError("no sources table configured")
            owners: dict[str, set[str]] = {}
            for catalog_id, database in databases:
                kwargs: dict[str, Any] = {"DatabaseName": database, "AttributesToGet": ["NAME"]}
                if catalog_id:
                    kwargs["CatalogId"] = catalog_id
                for page in glue.get_paginator("get_tables").paginate(**kwargs):
                    for table in page.get("TableList", []):
                        owners.setdefault(str(table["Name"]).lower(), set()).add(database)
            return owners

        loop = asyncio.get_running_loop()
        owners: dict[str, set[str]] | None
        try:
            owners = await asyncio.wait_for(loop.run_in_executor(self._executor, _list), timeout=_RESOLVE_TIMEOUT_S)
        except Exception as e:
            # Degrade to "unplaced": the table stays bare and is handled as before.
            logger.warning("glue_table_owners_failed", namespace=namespace, error=f"{type(e).__name__}: {str(e)[:120]}")
            owners = None
        self._glue_tables_cache[key] = (owners, time.monotonic())
        self._glue_tables_cache.move_to_end(key)
        while len(self._glue_tables_cache) > _ITEMS_CACHE_MAX:
            self._glue_tables_cache.popitem(last=False)
        return owners

    async def get_source_composition(self, namespace: str) -> SourceComposition:
        """Return which source types a namespace has (for serve-tier gating).

        Reads the shared, cached ``SRC#`` query (``_query_sources``), so it adds no
        extra round-trip when Tier-2 routing has already looked the namespace up
        this request. The signal is deliberately conservative:

        - A tier is only eligible to be skipped on a *positive zero-count* signal
          (e.g. ``has_unstructured_source == False`` → Tier 3 cannot contribute).
          Callers must first check ``unknown`` — the raw flags are True on the
          fail-open sentinel and do not reflect real topology.
        - On ANY uncertainty (registry unavailable, timeout, truncated/paginated
          result, or unexpected error) it returns
          ``SourceComposition.unknown_composition()`` — every flag True — so the
          caller fails OPEN and runs all tiers. A lookup miss must never drop a
          tier that could have answered.

        Note: ingestion-completeness is not considered here — a registered-but-not
        -yet-ingested DOCUMENTS source still counts as ``has_unstructured_source``,
        the safe direction (we run Tier 3, which degrades to empty, rather than
        wrongly skip it).
        """
        if not self.available:
            return SourceComposition.unknown_composition()

        try:
            items = await self._query_sources(namespace)
        except Exception as e:
            # Honor the fail-open contract at the source for ANY error (e.g. a
            # non-timeout boto3 ClientError that _query_sources does not convert).
            logger.warning("source_composition_lookup_failed", namespace=namespace, error=type(e).__name__)
            return SourceComposition.unknown_composition()

        if items is None:
            # Timeout / truncated / unavailable — uncertain, fail open. Not cached
            # (the shared cache only holds complete result sets), so the next
            # request retries rather than being pinned to "run everything".
            return SourceComposition.unknown_composition()

        if not items:
            # No SRC# records at all — an empty or not-yet-provisioned namespace, or a
            # name that does not resolve. This is NOT a reliable "namespace has zero
            # DATABASE and zero DOCUMENTS sources" signal (that would skip every tier);
            # it's an ambiguous case that must fail OPEN. Treat empty as unknown so
            # gating never drops a tier on a namespace it can't see.
            return SourceComposition.unknown_composition()

        has_structured_source = any(item.get("sourceType") == _SOURCE_TYPE_DATABASE for item in items)
        has_unstructured_source = any(item.get("sourceType") == _SOURCE_TYPE_DOCUMENTS for item in items)
        return SourceComposition(
            has_structured_source=has_structured_source, has_unstructured_source=has_unstructured_source
        )

    async def resolve_database_name(self, namespace: str, data_source_id: str = "") -> str:
        """Resolve the Glue database / Athena catalog name for a source.

        Resolution order:
        1. TTL cache hit
        2. Top-level ``glueDatabaseName`` (native Glue sources)
        3. Top-level ``athenaDataCatalogName`` (JDBC federation)
        4. ``configuration.databaseName`` (stored at source creation)

        When data_source_id is empty or "default", finds any DATABASE source
        in the namespace.

        Returns empty string if unresolvable (caller falls back to env var).
        """
        if not self.available:
            return ""

        cache_key = f"{namespace}:{data_source_id or 'default'}"
        cached = self._db_cache.get(cache_key)
        if cached:
            value, ts = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                return value

        async with self._cache_lock:
            cached = self._db_cache.get(cache_key)
            if cached and time.monotonic() - cached[1] < _CACHE_TTL_S:
                return cached[0]

            try:
                if data_source_id and data_source_id != "default":
                    item = await self.get_source(namespace, data_source_id)
                else:
                    item = await self.find_database_source(namespace)

                if not item:
                    self._db_cache[cache_key] = ("", time.monotonic())
                    return ""

                db_name = item.get("glueDatabaseName") or item.get("athenaDataCatalogName") or ""
                if not db_name:
                    config = self.parse_configuration(item)
                    db_name = config.get("databaseName", "")

                if db_name and not _IDENTIFIER_PATTERN.match(db_name):
                    logger.warning("invalid_database_name", database=db_name, namespace=namespace)
                    db_name = ""

                if db_name:
                    logger.debug("resolved_database", namespace=namespace, source=data_source_id, database=db_name)

                self._db_cache[cache_key] = (db_name, time.monotonic())
                return db_name
            except Exception as e:
                logger.warning("database_resolve_failed", namespace=namespace, source=data_source_id, error=str(e))
                return ""

    async def resolve_workgroup(self, namespace: str) -> str:
        """Resolve the Athena workgroup name from the namespaces table.

        Reads the ``athenaWorkgroupName`` field from the namespace's
        METADATA record. Returns empty string if unresolvable (caller
        falls back to prefix-based computation).
        """
        if not self._dynamodb or not self._namespaces_table:
            return ""

        cached = self._ns_cache.get(namespace)
        if cached:
            value, ts = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                return value

        validate_namespace(namespace)
        ns_id = self._resolve_ns(namespace)
        loop = asyncio.get_running_loop()
        table = self._dynamodb.Table(self._namespaces_table)
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    lambda: table.get_item(
                        Key={"PK": f"NS#{ns_id}", "SK": "METADATA"},
                        ProjectionExpression="athenaWorkgroupName",
                    ),
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning("sources_registry_timeout", op="resolve_workgroup", namespace=namespace)
            return ""
        except Exception as e:
            logger.warning("workgroup_resolve_failed", namespace=namespace, error=str(e))
            return ""

        workgroup = response.get("Item", {}).get("athenaWorkgroupName", "")
        if workgroup and not _WORKGROUP_PATTERN.match(workgroup):
            logger.warning("invalid_workgroup_name", workgroup=workgroup, namespace=namespace)
            return ""
        self._ns_cache[namespace] = (workgroup, time.monotonic())
        if workgroup:
            logger.debug("resolved_workgroup", namespace=namespace, workgroup=workgroup)
        return workgroup

    @staticmethod
    def parse_configuration(item: dict[str, Any]) -> dict[str, Any]:
        """Parse the JSON configuration blob from a source record."""
        raw = item.get("configuration", "{}")
        if isinstance(raw, str):
            try:
                return json.loads(raw) if raw else {}
            except (json.JSONDecodeError, ValueError):
                logger.warning("invalid_configuration_json", raw_length=len(raw))
                return {}
        return raw if isinstance(raw, dict) else {}
