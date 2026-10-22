# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Control-plane error responses never reflect caller-supplied values."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_control_plane.grants import delete_handler, delete_platform_grant_handler, grants_handler
from coa_control_plane.grants.grant_id import encode_grant_id
from coa_control_plane.namespace import (
    delete_handler as ns_delete_handler,
)
from coa_control_plane.namespace import (
    get_handler,
    namespace_api_handler,
    update_handler,
    update_status_handler,
)
from coa_control_plane.roles import namespace_roles_handler

pytestmark = pytest.mark.unit

_NS_ID = "550e8400-e29b-41d4-a716-446655440000"
_PROBE = "zz<probe>zz"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("ROLES_TABLE", "test-roles")
    monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE", "test-mappings")
    monkeypatch.setenv("ALLOWED_ORIGIN", "*")


def _event(method: str, path_params: dict[str, str] | None, body: Any = None, resource: str = "") -> dict[str, Any]:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"email": "admin@example.com"}},
    }


@pytest.mark.parametrize(
    ("module", "method", "body"),
    [
        (get_handler, "GET", None),
        (update_handler, "PUT", {"displayName": "x"}),
        (update_status_handler, "PATCH", {"status": "ARCHIVED"}),
        (ns_delete_handler, "DELETE", None),
    ],
)
def test_namespace_handlers_reject_malformed_id_without_echo(module: Any, method: str, body: Any) -> None:
    with patch.object(module, "_get_ns_dao") as dao:
        resp = module.handler(_event(method, {"namespaceId": _PROBE}, body), None)
    assert resp["statusCode"] == 400
    assert "probe" not in resp["body"]
    dao.assert_not_called()


@pytest.mark.parametrize(
    ("module", "method", "body"),
    [
        (get_handler, "GET", None),
        (update_handler, "PUT", {"displayName": "x"}),
        (update_status_handler, "PATCH", {"status": "ARCHIVED"}),
        (ns_delete_handler, "DELETE", None),
    ],
)
def test_namespace_not_found_does_not_echo_id(module: Any, method: str, body: Any) -> None:
    with patch.object(module, "_get_ns_dao") as dao:
        dao.return_value.get.return_value = None
        resp = module.handler(_event(method, {"namespaceId": _NS_ID}, body), None)
    assert resp["statusCode"] == 404
    assert _NS_ID not in resp["body"]


def test_update_status_non_object_body_returns_400() -> None:
    with patch.object(update_status_handler, "_get_ns_dao"):
        resp = update_status_handler.handler(_event("PATCH", {"namespaceId": _NS_ID}, ["ARCHIVED"]), None)
    assert resp["statusCode"] == 400


def test_update_status_unhashable_status_returns_400() -> None:
    with patch.object(update_status_handler, "_get_ns_dao"):
        resp = update_status_handler.handler(_event("PATCH", {"namespaceId": _NS_ID}, {"status": ["x"]}), None)
    assert resp["statusCode"] == 400


def test_delete_grant_outside_namespace_does_not_echo_grant_id() -> None:
    other = encode_grant_id(f"Namespace::{'1' * 8}-1111-4111-8111-111111111111#User::x", "ROLE#r")
    resp = delete_handler.handler(_event("DELETE", {"namespaceId": _NS_ID, "grantId": other}), None)
    assert resp["statusCode"] == 404
    assert other not in resp["body"]


@patch("coa_control_plane.grants.delete_platform_grant_handler.DynamoDBDAO")
def test_delete_platform_grant_not_found_does_not_echo_grant_id(mock_dao: MagicMock) -> None:
    mock_dao.return_value.get.return_value = None
    gid = encode_grant_id("Platform::GLOBAL#User::x", "ROLE#r")
    resp = delete_platform_grant_handler.handler(_event("DELETE", {"grantId": gid}), None)
    assert resp["statusCode"] == 404
    assert gid not in resp["body"]


@pytest.mark.parametrize("router", [grants_handler.handler, namespace_api_handler.handler])
def test_unknown_route_does_not_echo_route(router: Any) -> None:
    resp = router(_event("GET", None, resource=f"/{_PROBE}"), None)
    assert resp["statusCode"] == 404
    assert "probe" not in resp["body"]


@patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
def test_get_role_not_found_does_not_echo_role_id(mock_dao: MagicMock) -> None:
    mock_dao.return_value.get.return_value = None
    resp = namespace_roles_handler.handler(_event("GET", {"namespaceId": _NS_ID, "roleId": "zzprobezz"}), None)
    assert resp["statusCode"] == 404
    assert "zzprobezz" not in resp["body"]
