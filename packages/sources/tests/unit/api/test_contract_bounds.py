# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Sources API's hand-enforced bounds must equal the ones ``unified-sources.smithy`` declares.

Two halves:

* **Parity** — parses the Smithy model (the source of truth, not the generated
  OpenAPI) and asserts every constant in :mod:`coa_sources.api.contract`
  matches its ``@range`` / ``@length``. It also checks that every sources
  operation with a JSON body is either bounds-checked through its generated
  request model or validated in full by its handler, so a new operation cannot
  slip past both.
* **Enforcement** — an out-of-bound value is rejected with a 400 through the
  real router, the rejected value is not echoed, and no handler runs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# The route modules read their configuration at import; match the other API test modules.
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")

import coa_sources.api.sources_handler  # noqa: E402, F401, I001
from coa_sources.api import contract  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[5]
_SMITHY = _ROOT / "models" / "src" / "main" / "smithy"
_OPENAPI = _ROOT / "smithy-generated" / "openapi" / "openapi" / "ControlPlaneService.openapi.json"

_NS = "550e8400-e29b-41d4-a716-446655440000"
_SRC = "src-1"
_BASE = "/namespaces/{namespaceId}/sources"
_SH = "coa_sources.api.sources_handler"

# Operations whose handler validates the whole body with the generated model,
# so every bound (and type) is already enforced there.
_FULLY_VALIDATED = {"CreateSource", "RescanSource"}


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _text(name: str) -> str:
    path = _SMITHY / name
    assert path.is_file(), f"{path} not found"
    return path.read_text()


def _input_member_traits(text: str, operation: str, member: str) -> list[str]:
    """Trait lines on ``member`` of ``operation``'s inline input (doc comments skipped)."""
    op = re.search(rf"operation\s+{operation}\s*\{{", text)
    assert op, f"operation {operation} not found"
    inp = re.search(r"input\s*:=\s*(?:with\s*\[[^\]]*\]\s*)?\{", text[op.end() :])
    assert inp, f"{operation} input block not found"
    traits: list[str] = []
    depth = 1
    for line in text[op.end() + inp.end() :].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if stripped.startswith("@"):
            traits.append(stripped)
        elif re.match(rf"{member}\s*:", stripped):
            return traits
        elif not stripped.startswith("///"):
            traits = []
    raise AssertionError(f"{operation}.{member} not found")


def _shape_traits(text: str, shape: str) -> list[str]:
    m = re.search(rf"^(?:string|integer|list)\s+{shape}\b", text, re.MULTILINE)
    assert m, f"shape {shape} not found"
    traits: list[str] = []
    for line in reversed(text[: m.start()].splitlines()):
        stripped = line.strip()
        if stripped.startswith("@"):
            traits.append(stripped)
        elif not stripped.startswith("///"):
            break
    return traits


def _max(traits: list[str], trait: str) -> int:
    for t in traits:
        if t.startswith(f"@{trait}(") and (m := re.search(r"max:\s*(\d+)", t)):
            return int(m.group(1))
    raise AssertionError(f"no @{trait} max in {traits}")


# ── Parity ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("operation", "constant"),
    [
        ("ListSources", contract.LIST_SOURCES_MAX_RESULTS),
        ("ListSourceTables", contract.LIST_SOURCE_TABLES_MAX_RESULTS),
        ("ListSourceScanJobs", contract.LIST_SOURCE_SCAN_JOBS_MAX_RESULTS),
    ],
)
def test_max_results_constant_matches_model(operation: str, constant: int) -> None:
    traits = _input_member_traits(_text("unified-sources.smithy"), operation, "maxResults")
    assert _max(traits, "range") == constant


def test_list_source_tables_legacy_tolerance_is_the_old_shared_bound() -> None:
    # Values above the model max up to the old bound are reduced, not rejected.
    assert contract.LIST_SOURCE_TABLES_LEGACY_MAX_RESULTS == 1000
    assert contract.LIST_SOURCE_TABLES_LEGACY_MAX_RESULTS > contract.LIST_SOURCE_TABLES_MAX_RESULTS


def test_pagination_token_constant_matches_model() -> None:
    assert _max(_shape_traits(_text("common.smithy"), "PaginationToken"), "length") == (
        contract.PAGINATION_TOKEN_MAX_LENGTH
    )


def test_label_constants_match_model() -> None:
    text = _text("unified-sources.smithy")
    assert _max(_shape_traits(text, "TableId"), "length") == contract.LABEL_MAX_LENGTHS["tableId"]
    assert _max(_shape_traits(text, "ColumnName"), "length") == contract.LABEL_MAX_LENGTHS["columnName"]
    assert (
        _max(_input_member_traits(text, "GetSourceScanJob", "jobId"), "length") == contract.LABEL_MAX_LENGTHS["jobId"]
    )
    assert (
        _max(_input_member_traits(text, "GetSourceTable", "tableId"), "length")
        == contract.GET_SOURCE_TABLE_ID_MAX_LENGTH
    )


def test_every_body_operation_is_bounds_checked_or_fully_validated() -> None:
    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — run `make generate` (CI generates it before unit tests).")
    spec = json.loads(_OPENAPI.read_text())
    seen: set[str] = set()
    for path, item in spec["paths"].items():
        if not path.startswith(_BASE):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            ref = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
            if not ref:
                continue
            seen.add(op["operationId"])
            if op["operationId"] in _FULLY_VALIDATED:
                continue
            model = contract.BODY_MODELS.get((method.upper(), path))
            assert model is not None, f"{op['operationId']} has a JSON body but no entry in contract.BODY_MODELS"
            assert model.__name__ == ref.rsplit("/", 1)[-1], f"{op['operationId']} is checked against the wrong model"
    assert seen >= _FULLY_VALIDATED


# ── Enforcement ──────────────────────────────────────────────────────────────


def _route(method: str, resource: str, *, body: dict | None = None, path: dict | None = None, qs: dict | None = None):
    event = {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"namespaceId": _NS, "sourceId": _SRC, **(path or {})},
        "queryStringParameters": qs,
        "requestContext": {"authorizer": {"email": "steward@example.com"}},
        "body": json.dumps(body) if body is not None else None,
    }
    resp = sys.modules[_SH].handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


_OVERSIZE = "Q" * 70000


@pytest.mark.parametrize(
    ("method", "resource", "body", "member"),
    [
        (
            "PATCH",
            f"{_BASE}/{{sourceId}}/tables/{{tableId}}/metadata",
            {"overrides": {"description": _OVERSIZE}},
            "overrides.description",
        ),
        (
            "PATCH",
            f"{_BASE}/{{sourceId}}/tables/{{tableId}}/metadata",
            {"overrides": {"tags": ["t"] * 1001}},
            "overrides.tags",
        ),
        (
            "PATCH",
            f"{_BASE}/{{sourceId}}/tables/{{tableId}}/keys",
            {"foreignKeys": [{"column": "a", "targetTable": "db.t", "provenance": _OVERSIZE}]},
            "foreignKeys[0].provenance",
        ),
        ("PUT", f"{_BASE}/{{sourceId}}/rescan-schedule", {"enabled": True, "timezone": _OVERSIZE}, "timezone"),
        ("POST", f"{_BASE}/upload-urls", {"files": [{"filename": "a.pdf", "contentType": "x"}] * 101}, "files"),
        ("PUT", f"{_BASE}/{{sourceId}}/metadata", {"name": _OVERSIZE}, "name"),
    ],
)
def test_oversize_body_member_rejected_without_echo(method: str, resource: str, body: dict, member: str) -> None:
    with patch(f"{_SH}._get_dao") as dao:
        status, resp = _route(method, resource, body=body, path={"tableId": "db.t"})
    assert status == 400
    assert resp["error"].startswith(member)
    assert _OVERSIZE not in resp["error"]
    dao.assert_not_called()


def test_body_within_bounds_reaches_handler() -> None:
    resource = f"{_BASE}/{{sourceId}}/tables/{{tableId}}/metadata"
    with patch(f"{_SH}._handle_update_table_metadata", return_value={"statusCode": 200, "body": "{}"}) as handle:
        status, _ = _route(
            "PATCH", resource, body={"overrides": {"description": "x" * 65536}}, path={"tableId": "db.t"}
        )
    assert status == 200
    handle.assert_called_once()


def test_wrong_type_is_left_to_the_handler() -> None:
    """Only upper bounds are checked at the router; type errors keep the handler's own message."""
    assert contract.body_bound_error("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/review", {"decision": 7}) is None


@pytest.mark.parametrize(
    ("resource", "label", "limit"),
    [
        (f"{_BASE}/{{sourceId}}/tables/{{tableId}}/review", "tableId", 512),
        (f"{_BASE}/{{sourceId}}/tables/{{tableId}}", "tableId", 1024),
        (f"{_BASE}/{{sourceId}}/scan/{{jobId}}", "jobId", 128),
        (f"{_BASE}/{{sourceId}}/tables/{{tableId}}/columns/{{columnName}}/review", "columnName", 256),
    ],
)
def test_oversize_label_rejected(resource: str, label: str, limit: int) -> None:
    method = "GET" if resource.endswith(("{tableId}", "{jobId}")) else "PUT"
    labels = {"tableId": "db.t", "jobId": "j", "columnName": "c"}
    assert contract.label_bound_error(method, resource, {**labels, label: "a" * limit}) is None
    with patch(f"{_SH}._get_dao") as dao:
        status, resp = _route(
            method, resource, path={**labels, label: "a" * (limit + 1)}, body={"decision": "APPROVED"}
        )
    assert status == 400
    assert resp["error"] == f"{label} exceeds the maximum length of {limit}"
    dao.assert_not_called()


@pytest.mark.parametrize("resource", [_BASE, f"{_BASE}/{{sourceId}}/tables", f"{_BASE}/{{sourceId}}/scan"])
def test_oversize_next_token_rejected(resource: str) -> None:
    with patch("coa_sources.api.database_routes._SMUS_DOMAIN_ID", "dzd"):
        status, resp = _route("GET", resource, qs={"nextToken": "t" * 8193})
    assert status == 400
    assert resp["error"] == "nextToken exceeds the maximum length of 8192"


@pytest.mark.parametrize(("value", "expected_status"), [("0", 400), ("1001", 400), ("1000", None), ("50", None)])
def test_list_source_tables_max_results(value: str, expected_status: int | None) -> None:
    with (
        patch("coa_sources.api.database_routes._SMUS_DOMAIN_ID", "dzd"),
        patch("coa_sources.api.database_routes._resolve_project_id", return_value=None),
    ):
        status, _ = _route("GET", f"{_BASE}/{{sourceId}}/tables", qs={"maxResults": value})
    # In range (including the legacy 51-1000 tolerance) the handler proceeds to
    # the project lookup, which this test stubs to 404.
    assert status == (expected_status or 404)


_BODY_ROUTES = [
    ("POST", _BASE),
    ("POST", f"{_BASE}/upload-urls"),
    ("POST", f"{_BASE}/{{sourceId}}/rescan"),
    ("POST", f"{_BASE}/{{sourceId}}/approve"),
    ("POST", f"{_BASE}/{{sourceId}}/reject"),
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/review"),
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/keep"),
    ("PATCH", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/metadata"),
    ("PATCH", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/keys"),
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/columns/{{columnName}}/review"),
    ("PATCH", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/columns/{{columnName}}/metadata"),
    ("PUT", f"{_BASE}/{{sourceId}}/rescan-schedule"),
    ("PUT", f"{_BASE}/{{sourceId}}/event-rescan"),
    ("PUT", f"{_BASE}/{{sourceId}}/metadata"),
]


@pytest.mark.parametrize("raw_body", ["null", "[]", '[{"files": []}]', '"x"', "7", "true"])
@pytest.mark.parametrize(("method", "resource"), _BODY_ROUTES)
def test_non_object_json_body_is_400_before_any_handler(method: str, resource: str, raw_body: str) -> None:
    """Every body in the contract is a JSON object; anything else used to reach ``.get`` and 500."""
    event = {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"namespaceId": _NS, "sourceId": _SRC, "tableId": "db.t", "columnName": "c"},
        "queryStringParameters": None,
        "requestContext": {"authorizer": {"email": "steward@example.com"}},
        "body": raw_body,
    }
    with patch(f"{_SH}._get_dao") as dao, patch(f"{_SH}._get_ns_dao") as ns_dao:
        resp = sys.modules[_SH].handler(event, None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"]) == {"error": "Request body must be a JSON object"}
    dao.assert_not_called()
    ns_dao.assert_not_called()


def test_absent_body_is_left_to_the_handler() -> None:
    """An absent body is not a non-object body: optional-body routes still accept it."""
    with patch(f"{_SH}._handle_upload_urls", return_value={"statusCode": 200, "body": "{}"}) as handle:
        status, _ = _route("POST", f"{_BASE}/upload-urls")
    assert status == 200
    handle.assert_called_once()
