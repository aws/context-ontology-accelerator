# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Accessors for the GetSource response shape, shared by the sources integ tests.

Kept in a plain module (not conftest) so the unit suite can import it by path and
pin the field paths without a deployed environment.
"""

from __future__ import annotations

from urllib.parse import quote

import requests

_TIMEOUT = 30


def database_details(detail: dict) -> dict:
    """Return a DATABASE source's ``databaseDetails`` sub-object.

    GetSource nests every system-managed federation reference here
    (``glueConnectionName``, ``athenaDataCatalogName``, ``lastScanJobId``) — see
    ``_build_database_detail`` in api/sources_handler.py. ``databaseSource`` is
    the CREATE request shape and never appears in a response.
    """
    return detail.get("databaseDetails") or {}


def source_scan_error(
    api_endpoint: str,
    api_headers: dict,
    namespace_id: str,
    source_id: str,
    detail: dict,
) -> str:
    """Return the failed scan's ``errorMessage``, or ``""`` when unavailable.

    For a DATABASE source the message lives on the scan job keyed by
    ``databaseDetails.lastScanJobId``, which contains ``:`` and so is
    percent-encoded here. Returns ``""`` rather than raising so a lookup problem
    does not replace the real assertion failure.
    """
    job_id = database_details(detail).get("lastScanJobId")
    if not job_id:
        return ""
    resp = requests.get(
        f"{api_endpoint}/namespaces/{namespace_id}/sources/{source_id}/scan/{quote(str(job_id), safe='')}",
        headers=api_headers,
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        return ""
    return str(resp.json().get("errorMessage") or "")
