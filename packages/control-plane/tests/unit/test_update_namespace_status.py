# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for UpdateNamespaceStatus handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

MODULE = "coa_control_plane.namespace.update_status_handler"

_NS_ITEM = {
    "PK": "NS#ns-123",
    "SK": "METADATA",
    "namespaceId": "ns-123",
    "name": "sales",
    "displayName": "Sales Domain",
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
    import coa_control_plane.namespace.update_status_handler as mod

    mod._ns_dao = None
    yield
    mod._ns_dao = None


@pytest.mark.unit
class TestUpdateNamespaceStatus:
    def test_active_to_archived(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.side_effect = [_NS_ITEM, {**_NS_ITEM, "status": "ARCHIVED"}]

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "ARCHIVED"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["status"] == "ARCHIVED"

    def test_archived_to_active(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            archived_item = {**_NS_ITEM, "status": "ARCHIVED"}
            dao.get.side_effect = [archived_item, {**_NS_ITEM, "status": "ACTIVE"}]

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "ACTIVE"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 200

    def test_invalid_transition_active_to_deleted(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "DELETED"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 409

    def test_archived_to_deleted_rejected(self):
        """ARCHIVED cannot transition to DELETED via status update — must use DELETE endpoint."""
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "DELETED"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 409

    def test_delete_failed_to_deleted_rejected(self):
        """DELETE_FAILED cannot transition to DELETED via status update — must use DELETE endpoint."""
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = {**_NS_ITEM, "status": "DELETE_FAILED"}

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "DELETED"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 409

    def test_delete_failed_to_active_rejected(self):
        """DELETE_FAILED cannot transition to ACTIVE."""
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = {**_NS_ITEM, "status": "DELETE_FAILED"}

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "ACTIVE"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 409

    def test_invalid_status_value(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-123"},
                "body": json.dumps({"status": "INVALID"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 400

    def test_not_found(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = None

            from coa_control_plane.namespace.update_status_handler import handler

            event = {
                "pathParameters": {"namespaceId": "ns-999"},
                "body": json.dumps({"status": "ARCHIVED"}),
                "httpMethod": "PATCH",
                "resource": "/namespaces/{namespaceId}/status",
            }
            resp = handler(event, None)

        assert resp["statusCode"] == 404


@pytest.mark.unit
class TestNamespaceLanguage:
    @pytest.mark.parametrize(("stored", "expected"), [({}, "en"), ({"language": "ja"}, "ja")])
    def test_status_change_returns_effective_language_and_does_not_write_it(self, stored, expected):
        from coa_control_plane.namespace.update_status_handler import handler

        item = {**_NS_ITEM, **stored}
        dao = MagicMock()
        dao.get.side_effect = [item, {**item, "status": "ARCHIVED"}]
        with patch(f"{MODULE}._get_ns_dao", return_value=dao):
            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "body": json.dumps({"status": "ARCHIVED"}),
                    "httpMethod": "PATCH",
                    "resource": "/namespaces/{namespaceId}/status",
                },
                None,
            )

        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["namespace"]["language"] == expected
        assert dao.update.call_args.kwargs["update_fields"] == {"status": "ARCHIVED"}
