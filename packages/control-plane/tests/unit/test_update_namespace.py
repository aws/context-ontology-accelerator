# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UpdateNamespace handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

MODULE = "coa_control_plane.namespace.update_handler"

_NS_ITEM = {
    "PK": "NS#ns-123",
    "SK": "METADATA",
    "namespaceId": "ns-123",
    "name": "sales",
    "displayName": "Sales Domain",
    "description": "Sales data",
    "owner": "bob@example.com",
    "status": "ACTIVE",
    "dataZoneProjectId": "proj-abc",
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-01T10:00:00Z",
}


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ALLOWED_ORIGIN", "*")


@pytest.fixture(autouse=True)
def reset_singletons():
    import coa_control_plane.namespace.update_handler as mod

    mod._ns_dao = None
    yield
    mod._ns_dao = None


@pytest.mark.unit
class TestUpdateNamespace:
    def test_updates_display_name(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.side_effect = [
                _NS_ITEM,
                {**_NS_ITEM, "displayName": "New Name", "updatedAt": "2026-05-18T10:00:00Z"},
            ]

            from coa_control_plane.namespace.update_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "httpMethod": "PUT",
                "resource": "/namespaces/{namespaceId}",
                "body": json.dumps({"displayName": "New Name"}),
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["displayName"] == "New Name"
        dao.update.assert_called_once()

    def test_not_found(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = None

            from coa_control_plane.namespace.update_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-999"},
                "body": json.dumps({"displayName": "x"}),
                "httpMethod": "PUT",
                "resource": "/namespaces/{namespaceId}",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 404

    def test_no_updatable_fields(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.update_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"unknownField": "x"}),
                "httpMethod": "PUT",
                "resource": "/namespaces/{namespaceId}",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 400


@pytest.mark.unit
class TestUpdateNamespaceInputValidation:
    def _call(self, body: dict) -> tuple[dict, MagicMock]:
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.update_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps(body),
                "httpMethod": "PUT",
                "resource": "/namespaces/{namespaceId}",
            }
            resp = handler(event, None)
        return resp, dao

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("displayName", "Sales<script>alert(1)</script>"),
            ("description", "<img src=x onerror=alert(1)>"),
            ("description", "null\u0000byte"),
        ],
    )
    def test_markup_rejected(self, field, value):
        resp, dao = self._call({field: value})
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"].startswith(field)
        dao.update.assert_not_called()

    def test_empty_strings_still_allowed(self):
        resp, dao = self._call({"displayName": "", "description": ""})
        assert resp["statusCode"] == 200
        dao.update.assert_called_once()
