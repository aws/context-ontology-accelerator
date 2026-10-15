# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for APPROVED-source enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_metrics.lookups import TABLE_NO_METADATA, TABLE_UNAPPROVED
from coa_metrics.source_status import (
    PERMISSIVE_ENV,
    SourceValidationUnavailableError,
    build_validation_lookup,
    check_source_approved,
    check_source_table_exists,
    permissive_lookup_enabled,
)

pytestmark = pytest.mark.unit


def _dao_returning(item: dict | None) -> MagicMock:
    dao = MagicMock()
    dao.get.return_value = item
    return dao


class TestCheckSourceApproved:
    def test_approved_source_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value = _dao_returning({"status": "APPROVED"})
            assert check_source_approved("ns-1", "ds-1") is None

    def test_completed_source_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value = _dao_returning({"status": "COMPLETED"})
            assert check_source_approved("ns-1", "ds-1") is None

    @pytest.mark.parametrize("status", ["PENDING_REVIEW", "SCAN_FAILED", "SCANNING", "REJECTED", ""])
    def test_non_approved_source_rejected(self, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value = _dao_returning({"status": status})
            error = check_source_approved("ns-1", "ds-1")
        assert error is not None
        assert "APPROVED" in error

    def test_missing_source_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value = _dao_returning(None)
            error = check_source_approved("ns-1", "ds-missing")
        assert error is not None
        assert "does not exist" in error

    def test_empty_data_source_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        assert check_source_approved("ns-1", "") == "dataSourceId is required"

    @pytest.mark.parametrize("data_source_id", ["<script>alert(1)</script>", "ds 1", "ds/../x", "SRC#ds-1", "x" * 129])
    def test_malformed_data_source_id_rejected_before_lookup(
        self, monkeypatch: pytest.MonkeyPatch, data_source_id: str
    ) -> None:
        # Enforced even in permissive mode: the format check never depends on the table.
        monkeypatch.setenv(PERMISSIVE_ENV, "true")
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            error = check_source_approved("ns-1", data_source_id)
        assert error is not None
        assert error.startswith("dataSourceId must be")
        dao_cls.assert_not_called()

    def test_missing_table_config_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATA_SOURCES_TABLE", raising=False)
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with pytest.raises(SourceValidationUnavailableError):
            check_source_approved("ns-1", "ds-1")

    def test_ddb_client_error_fails_closed_with_error_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """boto3 API errors surface the AWS error code in the 503 message."""
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value.get.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
                "GetItem",
            )
            with pytest.raises(SourceValidationUnavailableError, match="ProvisionedThroughputExceededException"):
                check_source_approved("ns-1", "ds-1")

    def test_ddb_access_denied_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value.get.side_effect = ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetItem",
            )
            with pytest.raises(SourceValidationUnavailableError, match="AccessDeniedException"):
                check_source_approved("ns-1", "ds-1")

    def test_ddb_connection_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BotoCoreError (non-API failures, e.g. connection) also fails closed."""
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value.get.side_effect = EndpointConnectionError(endpoint_url="https://ddb.local")
            with pytest.raises(SourceValidationUnavailableError, match="EndpointConnectionError"):
                check_source_approved("ns-1", "ds-1")

    def test_ddb_unexpected_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-boto exceptions still fail closed (503) rather than raw 500."""
        monkeypatch.setenv("DATA_SOURCES_TABLE", "sources")
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.source_status.DynamoDBDAO") as dao_cls:
            dao_cls.return_value.get.side_effect = RuntimeError("throttled")
            with pytest.raises(SourceValidationUnavailableError):
                check_source_approved("ns-1", "ds-1")

    def test_permissive_mode_skips_enforcement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dev-only escape hatch: no table config needed, everything passes."""
        monkeypatch.delenv("DATA_SOURCES_TABLE", raising=False)
        monkeypatch.setenv(PERMISSIVE_ENV, "true")
        assert check_source_approved("ns-1", "ds-anything") is None


class TestCheckSourceTableExists:
    """#161: sourceTable is a hard block, but ONLY when its absence is provable.

    The catalog lookup fails OPEN (an unreachable catalog looks identical to an
    empty one), so a naive "table not found → 400" would reject every metric on
    a COMPLETED source whose assets aren't steward-approved yet. These tests
    pin the three-way split: provable absence → 400, unknown → 503, and
    can't-tell → fall through to today's soft warning.
    """

    def _lookup(
        self,
        *,
        available: bool = True,
        tables: set[str] | None = None,
        approved_tables: set[str] | None = None,
        no_metadata_tables: set[str] | None = None,
        records_reason: bool = True,
    ) -> MagicMock:
        lookup = MagicMock()
        lookup.catalog_available.return_value = available
        known = {table.lower() for table in (tables or set())}
        approved = set(known) if approved_tables is None else {table.lower() for table in approved_tables}
        no_metadata = {table.lower() for table in (no_metadata_tables or set())}
        approved -= no_metadata
        lookup.known_tables.return_value = known

        def resolve(requested: str) -> set[str]:
            if "." in requested:
                return {requested} & known
            return {name for name in known if name == requested or name.rsplit(".", 1)[-1] == requested}

        def table_exists(_data_source_id: str, table_name: str) -> bool:
            candidates = resolve(table_name.lower())
            return len(candidates) == 1 and candidates <= approved

        def table_rejection_reason(_data_source_id: str, table_name: str) -> str | None:
            # Like SmusCatalogDataSourceLookup: a reason only for a name that
            # resolved to ONE asset whose form was read and rejected.
            candidates = resolve(table_name.lower())
            if not records_reason or len(candidates) != 1 or candidates <= approved:
                return None
            return TABLE_NO_METADATA if candidates <= no_metadata else TABLE_UNAPPROVED

        lookup.table_exists.side_effect = table_exists
        lookup.table_rejection_reason.side_effect = table_rejection_reason
        return lookup

    def _patch_build(self, lookup):
        return patch(
            "coa_metrics.data_source_lookup_factory.build_data_source_lookup",
            return_value=lookup,
        )

    def test_provable_absence_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"orders", "customers"})):
            error = check_source_table_exists("ns-1", "ds-1", "no_such_table")
        assert error is not None
        assert "no_such_table" in error

    def test_present_table_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"orders"})):
            assert check_source_table_exists("ns-1", "ds-1", "Orders") is None

    def test_injected_lookup_is_reused_without_building(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"orders"})
        with patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup") as mock_build:
            assert (
                check_source_table_exists(
                    "ns-1",
                    "ds-1",
                    "orders",
                    data_source_lookup=lookup,
                )
                is None
            )
        mock_build.assert_not_called()

    def test_injected_none_degrades_without_rebuilding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup") as mock_build:
            assert (
                check_source_table_exists(
                    "ns-1",
                    "ds-1",
                    "orders",
                    data_source_lookup=None,
                )
                is None
            )
        mock_build.assert_not_called()

    def test_injected_unavailable_catalog_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(available=False, tables={"orders"})
        with (
            patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup") as mock_build,
            pytest.raises(SourceValidationUnavailableError),
        ):
            check_source_table_exists(
                "ns-1",
                "ds-1",
                "orders",
                data_source_lookup=lookup,
            )
        mock_build.assert_not_called()

    def test_empty_catalog_falls_back_to_soft_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A COMPLETED source with no steward-approved assets yet knows zero
        tables — absence is NOT provable, so this must not 400."""
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables=set())):
            assert check_source_table_exists("ns-1", "ds-1", "orders") is None

    def test_catalog_read_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with (
            self._patch_build(self._lookup(available=False, tables={"orders"})),
            pytest.raises(SourceValidationUnavailableError),
        ):
            check_source_table_exists("ns-1", "ds-1", "no_such_table")

    def test_unconfigured_lookup_degrades_to_soft_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``None`` lookup means "cannot configure", NOT "read failed".

        ``build_data_source_lookup`` returns None for benign reasons — missing
        env vars, namespace row absent, ``dataZoneProjectId`` unset. Raising 503
        here made every sourceTable metric un-creatable in any namespace without
        a provisioned DataZone project (pre-#161 that degraded to a soft
        warning). Only a *configured-but-unreadable* catalog is a 503.
        """
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(None):
            assert check_source_table_exists("ns-1", "ds-1", "orders") is None

    def test_schema_qualified_table_matches_bare_catalog_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The documented example uses ``sourceTable: "public.orders"`` but the
        catalog enumerates bare names — comparing the whole dotted string 400'd
        a table that exists."""
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"orders", "customers"})):
            assert check_source_table_exists("ns-1", "ds-1", "public.orders") is None

    def test_schema_qualified_absent_table_still_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"orders"})):
            error = check_source_table_exists("ns-1", "ds-1", "public.no_such_table")
        assert error is not None
        assert "no_such_table" in error

    def test_fully_qualified_catalog_name_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A catalog that enumerates dotted names must still match a dotted
        declaration — the full string is tried before the last segment."""
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"public.orders"})):
            assert check_source_table_exists("ns-1", "ds-1", "public.orders") is None

    def test_unapproved_table_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"sales.orders"}, approved_tables=set())
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", "sales.orders")
        assert error is not None
        assert "sales.orders" in error
        assert "not approved" in error

    def test_qualified_name_does_not_match_another_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables={"archive.orders"})):
            error = check_source_table_exists("ns-1", "ds-1", "sales.orders")
        assert error is not None
        assert "sales.orders" in error

    def test_ambiguous_bare_name_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"sales.orders", "archive.orders"})
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", "orders")
        assert error is not None
        assert "orders" in error
        assert "more than one table" in error
        assert "archive.orders, sales.orders" in error

    @pytest.mark.parametrize("declared", ["orders", "sales.orders"])
    def test_legacy_bare_name_does_not_override_qualified_ambiguity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        declared: str,
    ) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"orders", "archive.orders"})
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", declared)
        assert error is not None
        assert declared in error

    def test_asset_read_failure_after_name_lookup_returns_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"sales.orders"}, approved_tables=set())
        lookup.catalog_available.side_effect = [True, False]
        with self._patch_build(lookup), pytest.raises(SourceValidationUnavailableError):
            check_source_table_exists("ns-1", "ds-1", "sales.orders")

    def test_lookup_build_error_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with (
            patch(
                "coa_metrics.data_source_lookup_factory.build_data_source_lookup",
                side_effect=RuntimeError("datazone down"),
            ),
            pytest.raises(SourceValidationUnavailableError),
        ):
            check_source_table_exists("ns-1", "ds-1", "orders")

    def test_no_source_table_declared_skips_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup") as mock_build:
            assert check_source_table_exists("ns-1", "ds-1", "") is None
        mock_build.assert_not_called()

    def test_permissive_mode_skips_enforcement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PERMISSIVE_ENV, "true")
        with patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup") as mock_build:
            assert check_source_table_exists("ns-1", "ds-1", "no_such_table") is None
        mock_build.assert_not_called()

    # ── The 400 names WHY table_exists said no ─────────────────────────────
    # A known-but-unapproved table used to get "not found in data source …
    # known tables: <a list containing that very table>", which sent the
    # pipeline triage hunting for a table-matching bug.

    def _error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        declared: str,
        tables: set[str],
        approved_tables: set[str] | None = None,
    ) -> str:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with self._patch_build(self._lookup(tables=tables, approved_tables=approved_tables)):
            error = check_source_table_exists("ns-1", "ds-1", declared)
        assert error is not None
        return error

    def test_known_unapproved_table_says_not_approved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact shape of the original failure: the table is listed as known."""
        error = self._error(
            monkeypatch,
            "public.customers",
            tables={"public.customers", "public.orders"},
            approved_tables={"public.orders"},
        )
        assert error == (
            "Source table 'public.customers' exists in data source 'ds-1' "
            "but is not approved — approve the table on the data source, then retry"
        )

    def test_declared_case_does_not_add_catalog_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error(monkeypatch, "Public.Customers", tables={"public.customers"}, approved_tables=set())
        assert "not approved" in error
        assert "catalog name" not in error

    def test_bare_name_names_the_unapproved_catalog_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error(monkeypatch, "customers", tables={"public.customers"}, approved_tables=set())
        assert "Source table 'customers' (catalog name 'public.customers') exists" in error
        assert "not approved" in error

    def test_legacy_bare_entry_unapproved_names_the_catalog_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error(monkeypatch, "public.orders", tables={"orders"}, approved_tables=set())
        assert "Source table 'public.orders' (catalog name 'orders') exists" in error
        assert "not approved" in error

    def test_ambiguous_bare_name_asks_for_qualified_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error(monkeypatch, "orders", tables={"sales.orders", "archive.orders", "customers"})
        assert error == (
            "Source table 'orders' matches more than one table in data source 'ds-1' "
            "(archive.orders, sales.orders) — use the qualified name"
        )

    def test_qualified_name_over_ambiguous_bare_fallback_is_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sales.orders`` is not in the catalog. The bare fallback is ambiguous,
        but the caller already qualified the name, so "use the qualified name"
        would be wrong advice: this is a missing table."""
        error = self._error(monkeypatch, "sales.orders", tables={"orders", "archive.orders"})
        assert error.startswith("Source table 'sales.orders' not found in data source 'ds-1'")

    def test_missing_table_keeps_not_found_with_known_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error(monkeypatch, "public.no_such_table", tables={"public.orders", "public.customers"})
        assert error == (
            "Source table 'public.no_such_table' not found in data source 'ds-1' — "
            "known tables: public.customers, public.orders"
        )

    def test_asset_without_metadata_form_says_re_scan_not_approve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The catalog lists the asset, but it has no readable SCL metadata form.

        Telling the steward to approve it would be wrong: it may already be
        approved, or not in the review list at all."""
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"public.customers", "public.orders"}, no_metadata_tables={"public.customers"})
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", "public.customers")
        assert error == (
            "Source table 'public.customers' exists in data source 'ds-1' but its catalog metadata "
            "is missing or could not be read — re-scan the data source, then retry"
        )
        assert "not approved" not in error

    def test_legacy_bare_entry_without_metadata_asks_the_bare_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reason is read for the name the bare-name fallback looked up."""
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"orders"}, no_metadata_tables={"orders"})
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", "public.orders")
        assert error is not None
        assert "Source table 'public.orders' (catalog name 'orders') exists" in error
        assert "re-scan the data source" in error
        lookup.table_rejection_reason.assert_called_once_with("ds-1", "orders")

    def test_lookup_without_a_reason_covers_both_causes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = self._error_without_reason(monkeypatch)
        assert error == (
            "Source table 'public.customers' exists in data source 'ds-1' but is not approved or its "
            "catalog metadata could not be read — approve the table on the data source (or re-scan it "
            "if it is already approved), then retry"
        )

    def _error_without_reason(self, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = self._lookup(tables={"public.customers"}, approved_tables=set(), records_reason=False)
        with self._patch_build(lookup):
            error = check_source_table_exists("ns-1", "ds-1", "public.customers")
        assert error is not None
        return error


class TestBuildValidationLookup:
    def test_returns_built_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = MagicMock()
        with patch(
            "coa_metrics.data_source_lookup_factory.build_data_source_lookup",
            return_value=lookup,
        ):
            assert build_validation_lookup("ns-1") is lookup

    def test_permissive_mode_degrades_build_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PERMISSIVE_ENV, "true")
        with patch(
            "coa_metrics.data_source_lookup_factory.build_data_source_lookup",
            side_effect=RuntimeError("catalog unavailable"),
        ):
            assert build_validation_lookup("ns-1") is None

    def test_strict_mode_fails_closed_on_build_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with (
            patch(
                "coa_metrics.data_source_lookup_factory.build_data_source_lookup",
                side_effect=RuntimeError("catalog unavailable"),
            ),
            pytest.raises(SourceValidationUnavailableError, match="catalog unavailable"),
        ):
            build_validation_lookup("ns-1")


class TestPermissiveLookupEnabled:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        assert permissive_lookup_enabled() is False

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("false", False), ("1", False)])
    def test_env_values(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
        monkeypatch.setenv(PERMISSIVE_ENV, value)
        assert permissive_lookup_enabled() is expected
