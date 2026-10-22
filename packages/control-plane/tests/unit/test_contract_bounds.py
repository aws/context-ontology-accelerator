# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The bounds the control-plane handlers enforce by hand must equal the Smithy traits.

Two halves:

* **Parity** — parses the Smithy model (the source of truth, not the generated
  OpenAPI) and asserts every hand-enforced runtime constant matches its
  ``@length`` / ``@range`` / ``@pattern`` trait. Edit the model and forget the
  constant, or the reverse, and this goes red.
* **Enforcement** — each bounded grant request member is rejected with a 400
  when out of contract, the rejected value is not echoed, nothing is written,
  and the boundary values are accepted.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

os.environ.update(
    {
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_common import constants as common_constants  # noqa: E402
from coa_control_plane.grants import contract, grant_id, list_principal_grants_handler  # noqa: E402
from coa_control_plane.grants.create_handler import handler as create_grant  # noqa: E402
from coa_control_plane.grants.create_platform_grant_handler import handler as create_platform_grant  # noqa: E402
from coa_control_plane.grants.list_handler import handler as list_grants  # noqa: E402
from coa_control_plane.grants.list_platform_grants_handler import handler as list_platform_grants  # noqa: E402
from coa_control_plane.namespace import update_handler  # noqa: E402

pytestmark = pytest.mark.unit

_SMITHY_DIR = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy"
_NS_ID = "550e8400-e29b-41d4-a716-446655440000"
_SENTINEL = "zz-rejected-value-zz"


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _model(name: str) -> str:
    path = _SMITHY_DIR / f"{name}.smithy"
    assert path.is_file(), f"{path} not found"
    return path.read_text()


def _block(text: str, header: str) -> str:
    """Body of the first ``{ ... }`` block following ``header`` (a regex)."""
    m = re.search(header + r"[^{]*\{", text)
    assert m, f"{header} not found"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _member_traits(body: str, member: str) -> list[str]:
    """Trait lines directly above ``member:`` in a structure/input body."""
    traits: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("@"):
            traits.append(stripped)
        elif re.match(rf"{member}\s*:", stripped):
            return traits
        elif stripped and not stripped.startswith("//"):
            traits = []
    raise AssertionError(f"member {member} not found")


def _shape_traits(text: str, kind: str, shape: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"{kind}\s+{shape}\b", line.strip()):
            traits = []
            for prev in reversed(lines[:i]):
                if not prev.strip().startswith("@"):
                    break
                traits.append(prev.strip())
            return traits
    raise AssertionError(f"{kind} {shape} not found")


def _bound(traits: list[str], trait: str) -> tuple[int | None, int | None]:
    for t in traits:
        if t.startswith(f"@{trait}("):
            lo = re.search(r"min:\s*(\d+)", t)
            hi = re.search(r"max:\s*(\d+)", t)
            return (int(lo.group(1)) if lo else None, int(hi.group(1)) if hi else None)
    raise AssertionError(f"@{trait} not in {traits}")


def _pattern(traits: list[str]) -> str:
    for t in traits:
        if t.startswith("@pattern("):
            return json.loads(t[len("@pattern(") : -1])
    raise AssertionError(f"@pattern not in {traits}")


def _op_input(text: str, operation: str) -> str:
    return _block(_block(text, rf"operation\s+{operation}\b"), r"input\s*:=")


# ── Parity ───────────────────────────────────────────────────────────────────


class TestGrantContractParity:
    def test_create_grant_request_bounds(self) -> None:
        text = _model("grant")
        body = _block(text, r"structure\s+CreateGrantRequest\b")
        assert _bound(_member_traits(body, "role"), "length") == (1, contract.ROLE_MAX_LENGTH)
        for member in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
            assert _bound(_member_traits(body, member), "length")[1] == contract.OVERRIDE_MAX_ITEMS

    def test_create_platform_grant_request_role_bound(self) -> None:
        body = _block(_model("grant"), r"structure\s+CreatePlatformGrantRequest\b")
        assert _bound(_member_traits(body, "role"), "length") == (1, contract.ROLE_MAX_LENGTH)

    def test_override_member_bounds(self) -> None:
        text = _model("grant")
        table = _block(text, r"list\s+TableAllowlist\b")
        metrics = _block(text, r"list\s+MetricsList\b")
        columns = _block(text, r"list\s+ColumnList\b")
        denylist = _block(text, r"map\s+ColumnDenylist\b")
        assert _bound(_member_traits(table, "member"), "length")[1] == contract.TABLE_NAME_MAX_LENGTH
        assert _bound(_member_traits(metrics, "member"), "length")[1] == contract.METRIC_NAME_MAX_LENGTH
        assert _bound(_member_traits(columns, "member"), "length")[1] == contract.COLUMN_NAME_MAX_LENGTH
        assert _bound(_member_traits(denylist, "key"), "length")[1] == contract.COLUMN_DENYLIST_KEY_MAX_LENGTH
        assert _bound(_member_traits(denylist, "value"), "length")[1] == contract.COLUMN_LIST_MAX_ITEMS

    def test_principal_id_matches_runtime_validator(self) -> None:
        traits = _shape_traits(_model("grant"), "string", "PrincipalId")
        assert "@sensitive" in traits
        lo, hi = _bound(traits, "length")
        # PRINCIPAL_ID_RE carries the same bound as its {1,256} quantifier.
        assert f"{{{lo},{hi}}}" in common_constants.PRINCIPAL_ID_RE.pattern

    def test_grant_id_bounds(self) -> None:
        traits = _shape_traits(_model("grant"), "string", "GrantId")
        assert _bound(traits, "length")[1] == grant_id.GRANT_ID_MAX_LENGTH
        assert _pattern(traits) == grant_id.GRANT_ID_PATTERN

    @pytest.mark.parametrize("operation", ["ListNamespaceGrants", "ListPlatformGrants"])
    def test_grant_list_query_bounds(self, operation: str) -> None:
        inp = _op_input(_model("grant"), operation)
        assert _bound(_member_traits(inp, "maxResults"), "range") == (1, common_constants.MAX_PAGE_SIZE)
        assert _bound(_member_traits(inp, "principal"), "length")[1] == contract.PRINCIPAL_FILTER_MAX_LENGTH

    def test_list_principal_grants_page_size(self) -> None:
        inp = _op_input(_model("grant"), "ListPrincipalGrants")
        assert _bound(_member_traits(inp, "maxResults"), "range") == (1, list_principal_grants_handler.MAX_PAGE_SIZE)

    def test_pagination_token_bound(self) -> None:
        traits = _shape_traits(_model("common"), "string", "PaginationToken")
        assert _bound(traits, "length")[1] == common_constants.PAGINATION_TOKEN_MAX_LENGTH

    def test_list_namespaces_page_size(self) -> None:
        inp = _op_input(_model("namespace"), "ListNamespaces")
        assert _bound(_member_traits(inp, "maxResults"), "range") == (1, common_constants.MAX_PAGE_SIZE)

    def test_update_namespace_bounds(self) -> None:
        inp = _op_input(_model("namespace"), "UpdateNamespace")
        assert _bound(_member_traits(inp, "displayName"), "length")[1] == update_handler._MAX_DISPLAY_NAME
        assert _bound(_member_traits(inp, "description"), "length")[1] == update_handler._MAX_DESCRIPTION

    def test_role_listing_bound_is_documented(self) -> None:
        traits = _shape_traits(_model("role"), "list", "RoleSummaryList")
        assert _bound(traits, "length")[1] == 100


# ── Enforcement ──────────────────────────────────────────────────────────────


def _create_event(body: Any, *, namespace_id: str | None = _NS_ID) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/grants" if namespace_id else "/grants",
        "pathParameters": {"namespaceId": namespace_id} if namespace_id else None,
        "requestContext": {"authorizer": {"email": "admin@example.com"}},
        "body": json.dumps(body),
    }


def _valid_body(**overrides: Any) -> dict[str, Any]:
    return {"principalType": "User", "principalId": "alice@example.com", "role": "data-analyst", **overrides}


def _too_many(n: int) -> list[str]:
    return [f"t{i}" for i in range(n + 1)]


_LONG = _SENTINEL + "x" * 2000


@pytest.mark.parametrize(
    "overrides",
    [
        {"principalType": _SENTINEL},
        {"principalType": ["User"]},
        {"principalId": _SENTINEL + "#"},
        {"role": _SENTINEL * 10},
        {"role": 123},
        {"tableAllowlist": _too_many(contract.OVERRIDE_MAX_ITEMS)},
        {"tableAllowlist": [_LONG]},
        {"tableAllowlist": _SENTINEL},
        {"allowedMetrics": _too_many(contract.OVERRIDE_MAX_ITEMS)},
        {"allowedMetrics": [_SENTINEL * 20]},
        {"columnDenylist": {f"t{i}": [] for i in range(contract.OVERRIDE_MAX_ITEMS + 1)}},
        {"columnDenylist": {_LONG: ["c"]}},
        {"columnDenylist": {_SENTINEL: _SENTINEL}},
        {"columnDenylist": {"orders": _too_many(contract.COLUMN_LIST_MAX_ITEMS)}},
        {"columnDenylist": {"orders": [_LONG]}},
    ],
)
@patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
def test_create_grant_rejects_out_of_contract_member(mock_dao: MagicMock, overrides: dict[str, Any]) -> None:
    resp = create_grant(_create_event(_valid_body(**overrides)), None)
    assert resp["statusCode"] == 400, resp["body"]
    assert _SENTINEL not in resp["body"]
    mock_dao.return_value.put.assert_not_called()


@patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
def test_create_grant_accepts_boundary_values(mock_dao: MagicMock) -> None:
    mock_dao.return_value.get.return_value = {"PK": f"NS#{_NS_ID}", "SK": "ROLE#r"}
    body = _valid_body(
        role="r" * contract.ROLE_MAX_LENGTH,
        tableAllowlist=["t" * contract.TABLE_NAME_MAX_LENGTH] * contract.OVERRIDE_MAX_ITEMS,
        allowedMetrics=["m" * contract.METRIC_NAME_MAX_LENGTH],
        columnDenylist={"k" * contract.COLUMN_DENYLIST_KEY_MAX_LENGTH: ["c" * contract.COLUMN_NAME_MAX_LENGTH]},
    )
    resp = create_grant(_create_event(body), None)
    assert resp["statusCode"] == 201, resp["body"]


@patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
def test_create_grant_unknown_role_does_not_echo_role(mock_dao: MagicMock) -> None:
    mock_dao.return_value.get.return_value = None
    resp = create_grant(_create_event(_valid_body(role=_SENTINEL)), None)
    assert resp["statusCode"] == 404
    assert _SENTINEL not in resp["body"]


def test_create_grant_invalid_principal_type_lists_allowed_values() -> None:
    resp = create_grant(_create_event(_valid_body(principalType=_SENTINEL)), None)
    message = json.loads(resp["body"])["message"]
    assert all(v in message for v in ("User", "Group", "Agent"))


def test_create_grant_non_object_body_returns_400() -> None:
    assert create_grant(_create_event(["x"]), None)["statusCode"] == 400


@pytest.mark.parametrize("overrides", [{"principalType": _SENTINEL}, {"role": _SENTINEL * 10}, {"role": 1}])
@patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
def test_create_platform_grant_rejects_out_of_contract_member(mock_dao: MagicMock, overrides: dict) -> None:
    resp = create_platform_grant(_create_event(_valid_body(**overrides), namespace_id=None), None)
    assert resp["statusCode"] == 400
    assert _SENTINEL not in resp["body"]
    mock_dao.return_value.put.assert_not_called()


@patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
def test_create_platform_grant_unknown_role_does_not_echo_role(mock_dao: MagicMock) -> None:
    mock_dao.return_value.get.return_value = None
    resp = create_platform_grant(_create_event(_valid_body(role=_SENTINEL), namespace_id=None), None)
    assert resp["statusCode"] == 404
    assert _SENTINEL not in resp["body"]


def _list_event(query: dict[str, str], *, namespace_id: str | None = _NS_ID) -> dict[str, Any]:
    return {
        "httpMethod": "GET",
        "pathParameters": {"namespaceId": namespace_id} if namespace_id else None,
        "queryStringParameters": query,
        "requestContext": {"authorizer": {"email": "admin@example.com"}},
    }


@pytest.mark.parametrize(
    "query",
    [
        {"principal": "p" * (contract.PRINCIPAL_FILTER_MAX_LENGTH + 1)},
        {"nextToken": "a" * (common_constants.PAGINATION_TOKEN_MAX_LENGTH + 1)},
        {"maxResults": str(common_constants.MAX_PAGE_SIZE + 1)},
    ],
)
@pytest.mark.parametrize("platform", [False, True])
def test_grant_list_rejects_out_of_contract_query(query: dict[str, str], platform: bool) -> None:
    target = "list_platform_grants_handler" if platform else "list_handler"
    with patch(f"coa_control_plane.grants.{target}.DynamoDBDAO") as mock_dao:
        fn = list_platform_grants if platform else list_grants
        resp = fn(_list_event(query, namespace_id=None if platform else _NS_ID), None)
    assert resp["statusCode"] == 400
    mock_dao.return_value.query.assert_not_called()


@pytest.mark.parametrize("bad", ["x" * 1025, "abc=", "a/b", "a+b"])
def test_grant_id_outside_contract_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        grant_id.decode_grant_id(bad)
