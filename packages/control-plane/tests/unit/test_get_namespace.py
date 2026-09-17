# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for GetNamespace handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

MODULE = "coa_control_plane.namespace.get_handler"

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
    "sourceCount": 2,
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
    import coa_control_plane.namespace.get_handler as mod

    mod._ns_dao = None
    yield
    mod._ns_dao = None


@pytest.mark.unit
class TestDatasourceExternalId:
    """The namespace detail publishes the ExternalId customers must pin.

    A customer cannot write a correct trust policy without it, and the platform
    refuses to assume a cross-account role that does not present one — so if this
    is missing from the response, cross-account onboarding is undocumentable.
    """

    def test_returns_the_derived_external_id(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler({"pathParameters": {"namespaceId": "ns-123"}}, None)

        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["namespace"]["datasourceExternalId"] == "coa-dev-ns-123"

    def test_matches_the_shared_derivation(self, monkeypatch):
        """Must equal what the sources connector sends, or every assume fails."""
        from coa_common.constants import datasource_external_id

        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler({"pathParameters": {"namespaceId": "ns-123"}}, None)

        published = json.loads(resp["body"])["namespace"]["datasourceExternalId"]
        assert published == datasource_external_id("ns-123")


@pytest.mark.unit
class TestGetNamespace:
    def test_returns_namespace(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["namespaceId"] == "ns-123"
        assert body["namespace"]["name"] == "sales"
        assert body["namespace"]["owner"] == "bob@example.com"

    def test_not_found(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = None

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-999"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 404

    def test_missing_namespace_id(self):
        from coa_control_plane.namespace.get_handler import handler

        resp = handler({"pathParameters": {}, "httpMethod": "GET", "resource": "/namespaces/{namespaceId}"}, None)
        assert resp["statusCode"] == 400

    def test_returns_athena_workgroup_name_when_present(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = {**_NS_ITEM, "athenaWorkgroupName": f"{RESOURCE_PREFIX}-dev-ns-123"}

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["athenaWorkgroupName"] == f"{RESOURCE_PREFIX}-dev-ns-123"

    def test_includes_vkg_health(self):
        with (
            patch(f"{MODULE}._get_ns_dao") as mock,
            patch(
                f"{MODULE}.resolve_vkg_health_with_reason",
                return_value=("HEALTHY", None),
            ),
        ):
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["vkgHealth"] == "HEALTHY"
        # HEALTHY carries no reason -> the None value is filtered out of the body.
        assert "vkgHealthReason" not in body["namespace"]

    def test_includes_vkg_health_reason_when_degraded(self):
        with (
            patch(f"{MODULE}._get_ns_dao") as mock,
            patch(
                f"{MODULE}.resolve_vkg_health_with_reason",
                return_value=("DEGRADED", "container health check failing"),
            ),
        ):
            dao = MagicMock()
            mock.return_value = dao
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespace"]["vkgHealth"] == "DEGRADED"
        assert body["namespace"]["vkgHealthReason"] == "container health check failing"

    def test_omits_athena_workgroup_name_when_absent(self):
        with patch(f"{MODULE}._get_ns_dao") as mock:
            dao = MagicMock()
            mock.return_value = dao
            # _NS_ITEM has no athenaWorkgroupName
            dao.get.return_value = _NS_ITEM

            from coa_control_plane.namespace.get_handler import handler

            resp = handler(
                {
                    "pathParameters": {"namespaceId": "ns-123"},
                    "httpMethod": "GET",
                    "resource": "/namespaces/{namespaceId}",
                },
                None,
            )

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        # Field should be filtered out (not None) when not stored on the record
        assert "athenaWorkgroupName" not in body["namespace"]
