# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data source and ontology lookup implementations for metric validation.

Extracted from validator.py per review feedback — keeps the validator focused
on orchestrating checks while concrete lookup logic lives here.

Contains:
  - Base interfaces: DataSourceLookup, ColumnMetadata, OntologyLookup
  - Production: SmusCatalogDataSourceLookup (SMUS/DataZone)
  - Production: NeptuneOntologyLookup (Neptune SPARQL)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from coa_common.domain_models import ReviewStatus, Table
from coa_common.metadata_store.reader import (
    read_asset_names_for_datasource,
    read_table_for_asset,
)

logger = structlog.get_logger(__name__)


# ── Data types ──────────────────────────────────────────────────────────


@dataclass
class ColumnMetadata:
    """Metadata for a single column in a table."""

    name: str
    data_type: str  # e.g., "varchar", "integer", "decimal", "date", "timestamp"


# ── Abstract interfaces ─────────────────────────────────────────────────


class DataSourceLookup:
    """Interface for looking up data source and table metadata.

    Implementations can query SMUS (production), DynamoDB snapshots,
    or mock data for testing.
    """

    def data_source_exists(self, data_source_id: str) -> bool:
        """Check if a data source ID exists."""
        raise NotImplementedError

    def table_exists(self, data_source_id: str, table_name: str) -> bool:
        """Check if a table exists in a data source."""
        raise NotImplementedError

    def get_table_columns(self, data_source_id: str, table_name: str) -> list[ColumnMetadata] | None:
        """Get column metadata for a table. Returns None if unavailable."""
        raise NotImplementedError

    def catalog_available(self, data_source_id: str) -> bool:
        """Whether table metadata could actually be read for this source (#161).

        ``table_exists`` fails OPEN — it returns False both for a table that is
        genuinely absent and for a catalog that could not be read. Callers that
        turn absence into a hard rejection must consult this first.

        Args:
            data_source_id: The data source id to check.

        Returns:
            True by default: an implementation that cannot tell is assumed
            available, and ``known_tables`` (empty by default) is what keeps
            absence unprovable.
        """
        return True

    def known_tables(self, data_source_id: str) -> set[str]:
        """The lower-cased table names this lookup can enumerate (#161).

        Args:
            data_source_id: The data source id to enumerate.

        Returns:
            An empty set by default — "I cannot enumerate", which makes a
            missing table unprovable and so never hard-blocking.
        """
        return set()

    def get_column_type(self, data_source_id: str, table_name: str, column_name: str) -> str | None:
        """Get the data type of a specific column. Returns None if not found."""
        columns = self.get_table_columns(data_source_id, table_name)
        if columns is None:
            return None
        for col in columns:
            if col.name.lower() == column_name.lower():
                return col.data_type
        return None


class OntologyLookup:
    """Interface for verifying ontology class existence in Neptune."""

    def class_exists(self, class_uri: str, namespace: str) -> bool:
        """Check if an OWL class exists in the namespace's published graph."""
        raise NotImplementedError


# ── Production: SMUS Catalog Lookup ─────────────────────────────────────


class SmusCatalogDataSourceLookup(DataSourceLookup):
    """Look up data source / table / column metadata from the SMUS catalog.

    Uses ``read_approved_catalog`` from libs/common (same function the
    ontology-induction pipeline uses). Table/column metadata lives in
    DataZone as steward-approved assets, not in DynamoDB.

    The catalog is fetched lazily (first lookup per data source) and cached
    in-memory for the Lambda invocation lifetime. Only steward-approved
    tables/columns are returned.
    """

    def __init__(
        self,
        *,
        domain_id: str,
        project_id: str,
        namespace_id: str,
        datasources_table: str,
        region: str = "us-east-1",
    ) -> None:
        """Configure the SMUS/DataZone catalog lookup and its caches.

        Args:
            domain_id: DataZone domain id owning the catalog.
            project_id: DataZone project id scoping the assets.
            namespace_id: Namespace the lookup serves.
            datasources_table: DynamoDB table mapping data sources.
            region: AWS region for catalog reads.
        """
        self._domain_id = domain_id
        self._project_id = project_id
        self._namespace_id = namespace_id
        self._datasources_table = datasources_table
        self._region = region
        self._tables_by_source: dict[str, dict[str, list[ColumnMetadata]]] = {}
        self._source_present: dict[str, bool] = {}
        self._load_failed: dict[str, bool] = {}
        # Cheap path: table name → asset id, from search pages only.
        self._names_by_source: dict[str, dict[str, str]] = {}
        self._names_load_failed: dict[str, bool] = {}
        # One parsed asset per (source, table), fetched on demand.
        self._table_cache: dict[tuple[str, str], Table | None] = {}

    def _ensure_names_loaded(self, data_source_id: str) -> None:
        """Index the source's table NAMES (once). No per-asset form fetch.

        Answers every existence question this lookup is asked. Costs one DataZone
        search call per 50 assets, independent of table count — where
        ``_ensure_loaded`` costs one ``get_asset_forms`` per asset on top.
        """
        if data_source_id in self._names_by_source:
            return

        try:
            names = read_asset_names_for_datasource(self._domain_id, self._project_id, data_source_id)
        except Exception as exc:
            logger.warning(
                "smus_catalog_names_read_failed",
                data_source_id=data_source_id,
                namespace=self._namespace_id,
                error=str(exc),
            )
            self._names_by_source[data_source_id] = {}
            self._names_load_failed[data_source_id] = True
            return

        logger.info(
            "smus_catalog_names_read",
            data_source_id=data_source_id,
            namespace=self._namespace_id,
            table_count=len(names),
        )
        self._names_by_source[data_source_id] = names
        self._names_load_failed[data_source_id] = False

    def _resolve_asset_id(self, data_source_id: str, table_name: str) -> str | None:
        """Asset id for a table, accepting a bare or ``database.table`` name.

        The index is keyed on the database-qualified name so same-named tables in
        different databases stay distinct (see ``_qualified_name_from_asset``). An
        exact match on the given name wins; a bare name resolves only when exactly
        ONE database has a table by that name. An ambiguous bare name (two
        databases, same table) returns ``None`` rather than guessing — the caller
        must qualify it — and is logged so the ambiguity is diagnosable.
        """
        names = self._names_by_source.get(data_source_id, {})
        key = table_name.lower()
        if key in names:
            return names[key]
        candidates = [asset_id for qualified, asset_id in names.items() if qualified.rsplit(".", 1)[-1] == key]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning(
                "smus_ambiguous_table_name",
                data_source_id=data_source_id,
                table=table_name,
                match_count=len(candidates),
            )
        return None

    def _approved_table(self, data_source_id: str, table_name: str) -> Table | None:
        """The named table's approved metadata, or ``None``.

        ``None`` covers the source having no such asset, the asset's form being
        missing/unparseable, or the table not being steward-approved. A *transient*
        read failure is handled separately — it is not cached as absence and flips
        catalog_available() to False (see below) so callers stay fail-open (#161).
        The approval check is what keeps this equivalent to the previous
        approved-catalog index — the name index alone cannot see review status, so
        the ONE candidate's form is fetched to check it. One call, not one per table.
        """
        key = (data_source_id, table_name.lower())
        if key in self._table_cache:
            return self._table_cache[key]

        self._ensure_names_loaded(data_source_id)
        asset_id = self._resolve_asset_id(data_source_id, table_name)
        if asset_id is None:
            self._table_cache[key] = None
            return None

        ds_key = data_source_id if data_source_id.startswith("DS#") else f"DS#{data_source_id}"
        try:
            table = read_table_for_asset(self._domain_id, asset_id, f"{ds_key}:{table_name}", data_source_id)
        except Exception as exc:
            logger.warning(
                "smus_asset_read_failed",
                data_source_id=data_source_id,
                table=table_name,
                error=str(exc),
            )
            # A transient read failure is NOT provable absence (#161): don't cache
            # it, and flip the source to unavailable so catalog_available() returns
            # False. Callers then degrade to the soft table_reference warning
            # instead of a hard 400, and a later lookup retries the fetch. This
            # keeps the name-index and per-asset reads failing together, as the old
            # single whole-catalog read did.
            self._names_load_failed[data_source_id] = True
            return None

        if table is not None and table.business_metadata.review_status != ReviewStatus.APPROVED:
            table = None
        self._table_cache[key] = table
        return table

    def _ensure_loaded(self, data_source_id: str) -> None:
        """Fetch and index the approved catalog for a data source (once).

        The FULL read: one ``get_asset_forms`` per asset. Retained for
        :meth:`data_source_exists`, whose contract is "this source is APPROVED and
        has at least one approved table" — a whole-catalog fact that no name index
        can answer. Only the OSI-import paths call it, so the O(tables) cost stays
        off metric creation. Do not reintroduce it into the existence checks below.
        """
        if data_source_id in self._tables_by_source:
            return

        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        tables_index: dict[str, list[ColumnMetadata]] = {}
        present = False

        try:
            catalog = read_approved_catalog(
                domain_id=self._domain_id,
                project_id=self._project_id,
                namespace_id=self._namespace_id,
                data_source_ids=[data_source_id],
                datasources_table=self._datasources_table,
                region=self._region,
            )
        except Exception as exc:
            logger.warning(
                "smus_catalog_read_failed",
                data_source_id=data_source_id,
                namespace=self._namespace_id,
                error=str(exc),
            )
            self._tables_by_source[data_source_id] = tables_index
            self._source_present[data_source_id] = False
            self._load_failed[data_source_id] = True
            return

        for source in catalog.get("sources", []):
            if source.get("datasourceId") != data_source_id:
                continue
            present = True
            for db in source.get("databases", []):
                for tbl in db.get("tables", []):
                    name = tbl.get("name", "")
                    if not name:
                        continue
                    columns = [
                        ColumnMetadata(
                            name=col.get("name", ""),
                            data_type=(col.get("type") or "unknown"),
                        )
                        for col in tbl.get("columns", [])
                        if col.get("name")
                    ]
                    tables_index[name.lower()] = columns

        self._tables_by_source[data_source_id] = tables_index
        self._source_present[data_source_id] = present
        self._load_failed[data_source_id] = False

    def data_source_exists(self, data_source_id: str) -> bool:
        """Return whether the data source is present in the approved catalog.

        Args:
            data_source_id: The data source id to check.

        Returns:
            True if the catalog contains the data source, else False.
        """
        self._ensure_loaded(data_source_id)
        return self._source_present.get(data_source_id, False)

    def table_exists(self, data_source_id: str, table_name: str) -> bool:
        """Return whether the named approved table exists in the source's catalog.

        Args:
            data_source_id: The data source id owning the table.
            table_name: The table name (matched case-insensitively).

        Returns:
            True if the approved catalog contains the table, else False.
        """
        return self._approved_table(data_source_id, table_name) is not None

    def catalog_available(self, data_source_id: str) -> bool:
        """Whether the approved catalog was read successfully for this source.

        Args:
            data_source_id: The data source id to check.

        Returns:
            False only when the catalog read raised — in that case an absent
            table proves nothing (see ``DataSourceLookup.catalog_available``).
        """
        self._ensure_names_loaded(data_source_id)
        return not self._names_load_failed.get(data_source_id, False)

    def known_tables(self, data_source_id: str) -> set[str]:
        """Return the lower-cased table names the source's catalog knows.

        Args:
            data_source_id: The data source id to enumerate.

        Returns:
            The set of table names in the catalog (empty when the source is
            unknown or its read failed).

        Note:
            Counts every table asset, approved or not — the previous
            implementation counted approved ones only. The sole caller
            (``check_source_table_exists``) uses this as a non-empty guard for "the
            catalog knows at least one table", i.e. to decide whether a table's
            absence is *provable*. Counting unapproved tables makes that guard more
            conservative, which is the safe direction: it can only turn a hard 400
            into the pre-existing soft warning, never the reverse.

            Returns both the database-qualified names (``sales.customers``) and
            their bare forms (``customers``), so the caller's dual-form membership
            check matches a ``sourceTable`` declared either way.
        """
        self._ensure_names_loaded(data_source_id)
        qualified = set(self._names_by_source.get(data_source_id, {}))
        return qualified | {name.rsplit(".", 1)[-1] for name in qualified}

    def get_table_columns(self, data_source_id: str, table_name: str) -> list[ColumnMetadata] | None:
        """Return the approved column metadata for a table.

        Args:
            data_source_id: The data source id owning the table.
            table_name: The table name (matched case-insensitively).

        Returns:
            The list of column metadata, or None if the source or table is
            unknown.

        Shares :meth:`_approved_table`'s per-table cache with :meth:`table_exists`,
        so the validator's usual sequence — exists? then columns, on the same
        ``sourceTable`` — costs ONE ``get_asset_forms`` call in total.
        """
        table = self._approved_table(data_source_id, table_name)
        if table is None:
            return None
        return [
            ColumnMetadata(name=col.name, data_type=(col.data_type or "unknown"))
            for col in table.columns
            if col.name and col.business_metadata.review_status == ReviewStatus.APPROVED
        ]


# ── Production: Neptune Ontology Lookup ─────────────────────────────────


class NeptuneOntologyLookup(OntologyLookup):
    """Verify ontology class existence via Neptune SPARQL ASK query."""

    # Defense-in-depth: ontology concepts are user-controlled (request body).
    _SAFE_CONCEPT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:[A-Za-z0-9_.:/#\-]+$")

    def __init__(self) -> None:
        """Bind the Neptune SPARQL helpers used for class-existence checks."""
        from coa_metrics.neptune_client import (
            OWL,
            RDF,
            _esc,
            _iri,
            _resolve_graph_base,
            _sparql_query,
        )

        self._iri = _iri
        self._esc = _esc
        self._sparql_query = _sparql_query
        self._resolve_graph_base = _resolve_graph_base
        self._OWL = OWL
        self._RDF = RDF

    def class_exists(self, class_uri: str, namespace: str) -> bool:
        """ASK if the class exists as an owl:Class in any graph within this namespace."""
        if not isinstance(class_uri, str) or not self._SAFE_CONCEPT_RE.match(class_uri):
            logger.warning("ontology_concept_rejected", class_uri=class_uri, namespace=namespace)
            return False
        ns_prefix = f"{self._resolve_graph_base()}/{namespace}/"
        query = f"""
        ASK {{
          GRAPH ?g {{
            {self._iri(class_uri)} {self._iri(self._RDF + "type")} {self._iri(self._OWL + "Class")} .
          }}
          FILTER(STRSTARTS(STR(?g), "{self._esc(ns_prefix)}"))
        }}
        """
        try:
            result = self._sparql_query(query)
            return result.get("boolean", False)
        except Exception as exc:
            logger.warning(
                "ontology_lookup_failed",
                class_uri=class_uri,
                namespace=namespace,
                error=str(exc),
            )
            return False  # Fail open — don't block on Neptune errors
