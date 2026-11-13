# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests pinning the GetSource field paths the integ suite reads.

Reading a field nested under ``databaseDetails`` off the top level yields ``None``,
which would fail an integ assertion on a scan that succeeded. The sample payload is
shaped like a GetSource response for a completed Oracle scan.
"""

from __future__ import annotations

import pytest

from tests.shared.detail_helpers import database_details, source_scan_error

pytestmark = pytest.mark.unit

_ORACLE_DETAIL = {
    "sourceId": "45a125cf-730e-4680-82d8-f713f299d60c",
    "namespaceId": "0006eda7-4cee-4d57-bfaf-3ef0089546c8",
    "name": "integ-catalog-oracle-c994498b",
    "sourceType": "DATABASE",
    "sourceSubType": "JDBC_DATABASE",
    "status": "PENDING_REVIEW",
    "databaseDetails": {
        "jdbcConfiguration": {
            "engine": "ORACLE",
            "host": "oracle.example.com",
            "port": 1521,
            "databaseName": "FREEPDB1",
            "schemaFilter": "SCL_TEST",
        },
        "metadataEnrichmentEnabled": False,
        "tablesDiscovered": 8.0,
        "lastScanJobId": "2026-08-05T15:59:00Z",
        "glueConnectionName": "coadevds_0123456789abcdef",
        "athenaDataCatalogName": "coadevds_0123456789abcdef",
    },
}


def test_federation_refs_are_read_from_database_details() -> None:
    """The regression: these live under databaseDetails, never at the top level."""
    details = database_details(_ORACLE_DETAIL)
    assert details["athenaDataCatalogName"] == "coadevds_0123456789abcdef"
    assert details["glueConnectionName"] == "coadevds_0123456789abcdef"
    assert details["lastScanJobId"] == "2026-08-05T15:59:00Z"
    assert _ORACLE_DETAIL.get("athenaDataCatalogName") is None


def test_jdbc_configuration_is_read_from_database_details() -> None:
    """`databaseSource` is the CREATE shape; responses use `databaseDetails`."""
    jdbc = database_details(_ORACLE_DETAIL)["jdbcConfiguration"]
    assert jdbc["engine"] == "ORACLE"
    assert _ORACLE_DETAIL.get("databaseSource") is None


def test_database_details_missing_or_null_is_an_empty_dict() -> None:
    """A pre-scan source has no details yet — callers must still be able to .get()."""
    assert database_details({}) == {}
    assert database_details({"databaseDetails": None}) == {}


def test_scan_error_returns_empty_without_a_scan_job_id() -> None:
    """No job id means no lookup: classification degrades to "unknown", not a crash."""
    assert source_scan_error("https://api.example.com", {}, "ns", "src", {}) == ""
