# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for data source / ontology lookup implementations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _lookup():
    from coa_metrics.lookups import SmusCatalogDataSourceLookup

    return SmusCatalogDataSourceLookup(
        domain_id="dzd-1",
        project_id="prj-1",
        namespace_id="ns-1",
        datasources_table="sources-tbl",
        region="us-east-1",
    )


_CATALOG = {
    "sources": [
        {
            "datasourceId": "ds-1",
            "databases": [
                {
                    "tables": [
                        {
                            "name": "Orders",
                            "columns": [
                                {"name": "order_id", "type": "integer"},
                                {"name": "amount", "type": "decimal"},
                                {"name": "", "type": "ignored"},  # empty name → skipped
                                {"type": "no_name_key"},  # missing name → skipped
                            ],
                        },
                        {"name": "", "columns": []},  # empty table name → skipped
                    ]
                }
            ],
        }
    ]
}

# ── Fixtures for the cheap (names-only) catalog path ──────────────────────
#
# Existence and column lookups no longer go through read_approved_catalog: the
# lookup indexes table NAMES from DataZone search pages and fetches ONE asset's
# form when a caller actually needs its columns or review status. These build the
# two seams that replaced it. `_CATALOG` above is still used by
# data_source_exists, which keeps the full read.

# What read_asset_names_for_datasource returns: table name (lower) → asset id.
_NAMES = {"orders": "asset-orders"}


def _bm(status="APPROVED"):
    from coa_common.domain_models import BusinessMetadata, ReviewStatus

    return BusinessMetadata(review_status=ReviewStatus(status))


def _table(*, name="Orders", columns=(("order_id", "integer"), ("amount", "decimal")), status="APPROVED"):
    """A parsed DataZone asset, as read_table_for_asset would return it."""
    from coa_common.domain_models import Column, Table

    return Table(
        name=name,
        database="public",
        business_metadata=_bm(status),
        columns=[Column(name=n, data_type=t, business_metadata=_bm()) for n, t in columns],
    )


class TestSmusCatalogDataSourceLookup:
    """Tests for the SMUS catalog-backed data source lookup."""

    @patch("coa_common.metadata_store.catalog_reader.read_approved_catalog")
    def test_data_source_exists_true_when_in_catalog(self, mock_read):
        mock_read.return_value = _CATALOG
        lookup = _lookup()

        assert lookup.data_source_exists("ds-1") is True
        # Cached: a second lookup does not re-read the catalog.
        assert lookup.data_source_exists("ds-1") is True
        mock_read.assert_called_once()

    @patch("coa_common.metadata_store.catalog_reader.read_approved_catalog")
    def test_data_source_exists_false_when_absent(self, mock_read):
        mock_read.return_value = {"sources": []}
        lookup = _lookup()

        assert lookup.data_source_exists("ds-missing") is False

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_table_exists_is_case_insensitive(self, mock_names, mock_asset):
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table()
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "orders") is True
        assert lookup.table_exists("ds-1", "ORDERS") is True
        assert lookup.table_exists("ds-1", "customers") is False

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_same_table_name_across_databases_stays_distinct(self, mock_names, mock_asset):
        """Two databases with a same-named table must NOT collide: the index keys
        on the qualified name, and a qualified lookup resolves to its own asset."""
        mock_names.return_value = {"sales.customers": "asset-sales", "marketing.customers": "asset-mkt"}
        mock_asset.side_effect = lambda _domain, asset_id, _name, _ds: _table(
            name=asset_id, columns=((f"{asset_id}_col", "integer"),)
        )
        lookup = _lookup()

        assert lookup.get_column_type("ds-1", "sales.customers", "asset-sales_col") == "integer"
        assert lookup.get_column_type("ds-1", "marketing.customers", "asset-mkt_col") == "integer"
        # The two resolved to different assets — no overwrite.
        assert {c.args[1] for c in mock_asset.call_args_list} == {"asset-sales", "asset-mkt"}

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_ambiguous_bare_name_does_not_resolve(self, mock_names, mock_asset):
        """A bare name matching tables in two databases is ambiguous — resolve to
        None (caller must qualify) rather than silently picking one."""
        mock_names.return_value = {"sales.customers": "asset-sales", "marketing.customers": "asset-mkt"}
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "customers") is False
        mock_asset.assert_not_called()
        # Both forms are enumerable so the caller's guard still sees the catalog.
        assert lookup.known_tables("ds-1") == {"sales.customers", "marketing.customers", "customers"}

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_bare_name_resolves_when_unique(self, mock_names, mock_asset):
        """A bare name that matches exactly one database resolves to it."""
        mock_names.return_value = {"public.orders": "asset-orders"}
        mock_asset.return_value = _table()
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "orders") is True

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_get_table_columns_returns_named_columns_only(self, mock_names, mock_asset):
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table(columns=(("order_id", "integer"), ("amount", "decimal"), ("", "ignored")))
        lookup = _lookup()

        columns = lookup.get_table_columns("ds-1", "orders")

        assert columns is not None
        assert [c.name for c in columns] == ["order_id", "amount"]

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_get_table_columns_none_for_unknown_source(self, mock_names):
        mock_names.return_value = {}
        lookup = _lookup()

        assert lookup.get_table_columns("ds-unknown", "orders") is None

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_get_column_type_case_insensitive_match(self, mock_names, mock_asset):
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table()
        lookup = _lookup()

        assert lookup.get_column_type("ds-1", "orders", "ORDER_ID") == "integer"
        assert lookup.get_column_type("ds-1", "orders", "amount") == "decimal"

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_get_column_type_returns_none_for_missing_column(self, mock_names, mock_asset):
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table()
        lookup = _lookup()

        assert lookup.get_column_type("ds-1", "orders", "no_such_col") is None

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.read_approved_catalog")
    def test_catalog_read_failure_degrades_to_empty(self, mock_read, mock_names):
        mock_read.side_effect = RuntimeError("catalog down")
        mock_names.side_effect = RuntimeError("catalog down")
        lookup = _lookup()

        assert lookup.data_source_exists("ds-1") is False
        assert lookup.table_exists("ds-1", "orders") is False
        assert lookup.get_table_columns("ds-1", "orders") is None

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_catalog_available_false_on_read_failure(self, mock_names):
        """#161: table_exists() fails OPEN (False) on a read failure, so it
        cannot distinguish 'absent' from 'unknown'. catalog_available() is the
        signal that makes absence provable — without it a hard sourceTable
        block would reject valid metrics whenever the catalog is down."""
        mock_names.side_effect = RuntimeError("catalog down")
        lookup = _lookup()

        assert lookup.catalog_available("ds-1") is False

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_catalog_available_true_on_successful_read(self, mock_names):
        mock_names.return_value = _NAMES
        lookup = _lookup()

        assert lookup.catalog_available("ds-1") is True

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_catalog_available_true_when_source_absent_but_read_succeeded(self, mock_names):
        """A successful read that simply lacks the source is still 'available' —
        the catalog spoke, it just had nothing for this source."""
        mock_names.return_value = {}
        lookup = _lookup()

        assert lookup.catalog_available("ds-missing") is True

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_known_tables_returns_lowercased_names(self, mock_names):
        mock_names.return_value = _NAMES
        lookup = _lookup()

        assert lookup.known_tables("ds-1") == {"orders"}

    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_known_tables_empty_when_read_failed(self, mock_names):
        mock_names.side_effect = RuntimeError("catalog down")
        lookup = _lookup()

        assert lookup.known_tables("ds-1") == set()

    def test_base_lookup_defaults_never_prove_absence(self):
        """The base interface must default to 'I cannot enumerate tables', so a
        lookup implementation that doesn't override these can never trigger the
        hard sourceTable block."""
        from coa_metrics.lookups import DataSourceLookup

        base = DataSourceLookup()
        assert base.catalog_available("ds-1") is True
        assert base.known_tables("ds-1") == set()


class TestSmusCatalogReadCost:
    """The cost of a lookup must not scale with the source's table count.

    These assert CALL COUNTS, not results, because the defect they guard is
    invisible in output. ``check_source_table_exists`` used to reach
    ``read_approved_catalog``, which issues one ``get_asset_forms`` per asset. On
    an 88-table source that measured 0.186s x 88 = 16s of DataZone round-trips,
    and ``POST /metrics`` returned a 504 at API Gateway's 29s ceiling while every
    assertion about the RESULT still passed (job 10909663; reproduced on demand at
    29.74s and 29.93s against two namespaces, versus 10.65s for a 2-table source).

    A regression here reads as latency, not as a wrong answer, so the only test
    that catches it is one that counts the calls.
    """

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_table_exists_fetches_at_most_one_asset_on_a_wide_source(self, mock_names, mock_asset):
        mock_names.return_value = {f"t{i}": f"asset-{i}" for i in range(88)}
        mock_asset.return_value = _table(name="t7")
        lookup = _lookup()

        assert lookup.table_exists("ds-wide", "t7") is True

        mock_names.assert_called_once()
        assert mock_asset.call_count == 1, (
            f"one asset form per existence check, not one per table — got {mock_asset.call_count}"
        )

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_absent_table_costs_no_asset_fetch(self, mock_names, mock_asset):
        """The name index alone proves absence, so nothing needs fetching."""
        mock_names.return_value = {f"t{i}": f"asset-{i}" for i in range(88)}
        lookup = _lookup()

        assert lookup.table_exists("ds-wide", "not_a_table") is False
        mock_asset.assert_not_called()

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_exists_then_columns_shares_one_fetch(self, mock_names, mock_asset):
        """The validator's real sequence on a metric's sourceTable: exists? then
        columns. Both answers come from one asset, so it must be fetched once."""
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table()
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "orders") is True
        assert lookup.get_table_columns("ds-1", "orders") is not None
        assert lookup.get_column_type("ds-1", "orders", "amount") == "decimal"

        assert mock_asset.call_count == 1, f"per-table cache not shared — {mock_asset.call_count} fetches"
        mock_names.assert_called_once()

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_unapproved_table_is_reported_absent(self, mock_names, mock_asset):
        """Approval semantics are preserved. The name index cannot see review
        status, so the ONE candidate's form is what rules it out — the previous
        implementation got this from an index built of approved tables only."""
        mock_names.return_value = _NAMES
        mock_asset.return_value = _table(status="PENDING_REVIEW")
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "orders") is False
        assert lookup.get_table_columns("ds-1", "orders") is None

    @patch("coa_metrics.lookups.read_table_for_asset")
    @patch("coa_metrics.lookups.read_asset_names_for_datasource")
    def test_transient_asset_read_failure_stays_fail_open(self, mock_names, mock_asset):
        """#161: a transient asset-form read error must NOT become provable absence.

        The name index loaded fine, so ``catalog_available()`` would otherwise stay
        True while ``table_exists`` returned False — making a valid, approved table
        look provably absent (a hard 400). The failure must instead flip
        ``catalog_available()`` to False (degrading to the soft warning) and must
        not be cached, so a later lookup retries once the service recovers.
        """
        mock_names.return_value = _NAMES
        mock_asset.side_effect = RuntimeError("datazone down")
        lookup = _lookup()

        assert lookup.table_exists("ds-1", "orders") is False
        # Not cached: the second lookup retries the fetch rather than reusing None.
        assert lookup.table_exists("ds-1", "orders") is False
        assert mock_asset.call_count == 2, "a transient failure must be retried, not cached as absence"
        # The failure poisons availability so absence is not treated as provable.
        assert lookup.catalog_available("ds-1") is False


class TestNeptuneOntologyLookup:
    """Tests for the Neptune SPARQL-backed ontology class check."""

    def _build(self):
        from coa_metrics.lookups import NeptuneOntologyLookup

        return NeptuneOntologyLookup()

    def test_rejects_malformed_class_uri(self):
        lookup = self._build()

        # No prefix separator → rejected before any query.
        assert lookup.class_exists("not a curie", "ns-1") is False

    def test_non_string_class_uri_rejected(self):
        lookup = self._build()

        assert lookup.class_exists(None, "ns-1") is False  # type: ignore[arg-type]

    def test_returns_true_when_sparql_ask_true(self):
        lookup = self._build()

        with patch.object(lookup, "_sparql_query", return_value={"boolean": True}) as q:
            assert lookup.class_exists("ind:Order", "ns-1") is True
        q.assert_called_once()

    def test_returns_false_when_sparql_ask_false(self):
        lookup = self._build()

        with patch.object(lookup, "_sparql_query", return_value={"boolean": False}):
            assert lookup.class_exists("ind:Order", "ns-1") is False

    def test_fails_open_on_sparql_error(self):
        lookup = self._build()

        with patch.object(lookup, "_sparql_query", side_effect=RuntimeError("neptune down")):
            # Fail open — Neptune errors must not block validation.
            assert lookup.class_exists("ind:Order", "ns-1") is False
