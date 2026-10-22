# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""clientToken idempotency at the control-plane create call sites.

CreateNamespace, CreateGrant and CreatePlatformGrant route through
``coa_common.idempotency.idempotent_create``: a retry with the same token
replays the first response instead of re-running the create (which today
returns 409 on the second attempt), and ``clientToken`` never reaches
validation or persistence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import NamespaceStatus
from coa_common.testing import DEFAULT_IDEMPOTENCY_TABLE, mock_idempotency_table

os.environ.update(
    {
        "NAMESPACES_TABLE": "test-namespaces",
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "DATAZONE_DOMAIN_ID": "dzd-test123",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.grants.create_handler import handler as create_grant  # noqa: E402
from coa_control_plane.grants.create_platform_grant_handler import (  # noqa: E402
    handler as create_platform_grant,
)
from coa_control_plane.namespace.create_handler import handler as create_namespace  # noqa: E402
from coa_control_plane_server.models.namespace_detail import NamespaceDetail  # noqa: E402

pytestmark = pytest.mark.unit

_NS_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def idem_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IDEMPOTENCY_TABLE", DEFAULT_IDEMPOTENCY_TABLE)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_idempotency_table():
        yield


def _event(resource: str, body: dict, *, namespace_id: str | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "resource": resource,
        "path": resource,
        "pathParameters": {"namespaceId": namespace_id} if namespace_id else None,
        "requestContext": {"authorizer": {"email": "admin@example.com"}},
        "body": json.dumps(body),
    }


@patch("coa_control_plane.namespace.create_handler._get_service")
def test_create_namespace_retry_with_token_replays_and_strips_token(mock_get_svc: MagicMock, idem_table: None) -> None:
    mock_get_svc.return_value.create.return_value = NamespaceDetail(
        namespaceId="a1b2c3d4-e5f6-4890-abcd-ef1234567890",
        name="sales",
        owner="alice@co.com",
        status=NamespaceStatus.ACTIVE,
        language="en",
        dataZoneProjectId="proj-abc",
        sourceCount=0,
        createdAt="2026-01-01T00:00:00Z",
        updatedAt="2026-01-01T00:00:00Z",
    )
    body = {"name": "sales", "owner": "alice@co.com", "clientToken": "ns-token-1"}

    first = create_namespace(_event("/namespaces", body), None)
    second = create_namespace(_event("/namespaces", body), None)

    assert first["statusCode"] == 201
    assert (second["statusCode"], second["body"]) == (first["statusCode"], first["body"])
    mock_get_svc.return_value.create.assert_called_once()
    request = mock_get_svc.return_value.create.call_args.args[0]
    # Stripped before validation: the generated model's optional member stays unset.
    assert request.model_dump(by_alias=True).get("clientToken") is None


@patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
def test_create_grant_retry_with_token_writes_one_grant(mock_dao_cls: MagicMock, idem_table: None) -> None:
    roles_dao, mappings_dao = MagicMock(), MagicMock()
    mock_dao_cls.side_effect = [roles_dao, mappings_dao]
    roles_dao.get.return_value = {"PK": f"NS#{_NS_ID}", "SK": "ROLE#analyst"}
    body = {"principalType": "User", "principalId": "alice@company.com", "role": "analyst", "clientToken": "g-1"}
    resource = "/namespaces/{namespaceId}/grants"

    first = create_grant(_event(resource, body, namespace_id=_NS_ID), None)
    second = create_grant(_event(resource, body, namespace_id=_NS_ID), None)

    assert first["statusCode"] == 201
    assert (second["statusCode"], second["body"]) == (201, first["body"])
    mappings_dao.put.assert_called_once()
    assert "clientToken" not in mappings_dao.put.call_args.args[0]

    reused = create_grant(_event(resource, {**body, "role": "steward"}, namespace_id=_NS_ID), None)
    assert reused["statusCode"] == 409


@patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
def test_create_platform_grant_retry_with_token_writes_one_grant(mock_dao_cls: MagicMock, idem_table: None) -> None:
    roles_dao, mappings_dao = MagicMock(), MagicMock()
    mock_dao_cls.side_effect = [roles_dao, mappings_dao]
    roles_dao.get.return_value = {"PK": "GLOBAL", "SK": "ROLE#platform-viewer"}
    body = {"principalType": "User", "principalId": "bob@company.com", "role": "platform-viewer", "clientToken": "p-1"}

    first = create_platform_grant(_event("/grants", body), None)
    second = create_platform_grant(_event("/grants", body), None)

    assert first["statusCode"] == 201
    assert (second["statusCode"], second["body"]) == (201, first["body"])
    mappings_dao.put.assert_called_once()
    assert "clientToken" not in mappings_dao.put.call_args.args[0]


def test_create_grant_invalid_token_returns_400_message_shape() -> None:
    resp = create_grant(
        _event("/namespaces/{namespaceId}/grants", {"clientToken": "bad token"}, namespace_id=_NS_ID), None
    )
    assert resp["statusCode"] == 400
    assert "clientToken" in json.loads(resp["body"])["message"]
