# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Create Namespace handler and service."""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.constants import NamespaceStatus
from coa_common.dao import PaginatedResult
from coa_common.metadata_store import MetadataStoreError
from coa_common.metadata_store.base import ProjectResult

# Set env vars before importing handler
os.environ.update(
    {
        "NAMESPACES_TABLE": "test-namespaces",
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-role-mappings",
        "DATAZONE_DOMAIN_ID": "dzd-test123",
        "PROJECT_ACCESS_ROLE_ARN_SSM": "/coa/smus/dz-project-access-role-arn",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.namespace.create_handler import handler  # noqa: E402
from coa_control_plane.namespace.service import ConflictError, NamespaceService  # noqa: E402
from coa_control_plane_server.models.create_namespace_request_content import (
    CreateNamespaceRequestContent,  # noqa: E402
)
from coa_control_plane_server.models.namespace_detail import NamespaceDetail  # noqa: E402

pytestmark = pytest.mark.unit


def _event(body: dict | None = None, method: str = "POST") -> dict:
    return {
        "httpMethod": method,
        "path": "/namespaces",
        "body": json.dumps(body) if body is not None else None,
        "headers": {},
        "requestContext": {},
    }


VALID_BODY = {"name": "sales", "owner": "alice@co.com"}


# ═══════════════════════════════════════════════════════════════════
# HTTP method validation
# ═══════════════════════════════════════════════════════════════════


class TestHttpMethod:
    def test_get_returns_405(self):
        resp = handler(_event(method="GET"), None)
        assert resp["statusCode"] == 405
        assert resp["headers"]["Allow"] == "POST"

    def test_put_returns_405(self):
        resp = handler(_event(VALID_BODY, method="PUT"), None)
        assert resp["statusCode"] == 405


# ═══════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════


class TestValidation:
    def test_missing_body(self):
        resp = handler(_event(), None)
        assert resp["statusCode"] == 400
        assert "required" in json.loads(resp["body"])["message"].lower()

    def test_missing_name(self):
        resp = handler(_event({"owner": "a@b.com"}), None)
        assert resp["statusCode"] == 400

    def test_missing_owner(self):
        resp = handler(_event({"name": "sales"}), None)
        assert resp["statusCode"] == 400

    def test_invalid_name_format(self):
        resp = handler(_event({"name": "INVALID!", "owner": "a@b.com"}), None)
        assert resp["statusCode"] == 400

    def test_name_too_short(self):
        resp = handler(_event({"name": "ab", "owner": "a@b.com"}), None)
        assert resp["statusCode"] == 400

    def test_invalid_email(self):
        resp = handler(_event({"name": "sales", "owner": "not-email"}), None)
        assert resp["statusCode"] == 400

    def test_invalid_json(self):
        event = _event()
        event["body"] = "not-json"
        resp = handler(event, None)
        assert resp["statusCode"] == 400
        assert "Invalid JSON" in json.loads(resp["body"])["message"]

    def test_display_name_too_long(self):
        resp = handler(_event({"name": "sales", "owner": "a@b.com", "displayName": "x" * 257}), None)
        assert resp["statusCode"] == 400

    def test_description_too_long(self):
        resp = handler(_event({"name": "sales", "owner": "a@b.com", "description": "x" * 1025}), None)
        assert resp["statusCode"] == 400

    def test_display_name_not_string(self):
        resp = handler(_event({"name": "sales", "owner": "a@b.com", "displayName": 123}), None)
        assert resp["statusCode"] == 400

    def test_description_not_string(self):
        resp = handler(_event({"name": "sales", "owner": "a@b.com", "description": []}), None)
        assert resp["statusCode"] == 400

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("owner", "alice@example.com<script>alert(1)</script>"),
            ("owner", 'alice@example.com"onmouseover=1'),
            ("owner", "alice..bob@example.com"),
            ("owner", "a#b@example.com"),
            ("owner", "a`b@example.com"),
            ("owner", ".alice@example.com"),
            ("owner", "alice.@example.com"),
            ("displayName", "Sales<script>alert(1)</script>"),
            ("displayName", "Sales\u0000Ops"),
            ("description", "testing namespace<img src=x onerror=alert(1)>"),
            ("description", "bell\u0007char"),
        ],
    )
    def test_markup_and_control_chars_rejected(self, field, value):
        body = {"name": "sales", "owner": "a@b.com", field: value}
        resp = handler(_event(body), None)
        assert resp["statusCode"] == 400
        message = json.loads(resp["body"])["message"]
        assert message.startswith(field)
        assert "regular expression" not in message

    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_ordinary_punctuation_and_unicode_accepted(self, mock_get_svc):
        mock_get_svc.return_value.create.return_value = NamespaceDetail.model_construct(
            namespace_id="a1b2c3d4-e5f6-4890-abcd-ef1234567890", name="sales"
        )
        body = {
            "name": "sales",
            "owner": "o'brien.first+tag@sub.example.co.uk",
            "displayName": "Sales & Finance (EMEA) — Ünïcode",
            "description": "Line one: revenue, margin & churn.\nLine two\tindented.",
        }
        resp = handler(_event(body), None)
        assert resp["statusCode"] == 201


# ═══════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════


class TestCreateNamespace:
    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_returns_201(self, mock_get_svc):
        ns = NamespaceDetail(
            namespaceId="a1b2c3d4-e5f6-4890-abcd-ef1234567890",
            name="sales",
            owner="alice@co.com",
            status=NamespaceStatus.ACTIVE,
            dataZoneProjectId="proj-abc",
            sourceCount=0,
            createdAt="2026-01-01T00:00:00Z",
            updatedAt="2026-01-01T00:00:00Z",
        )
        mock_get_svc.return_value.create.return_value = ns
        resp = handler(_event(VALID_BODY), None)
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["namespace"]["namespaceId"] == "a1b2c3d4-e5f6-4890-abcd-ef1234567890"
        assert body["namespace"]["status"] == "ACTIVE"

    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_passes_optional_fields(self, mock_get_svc):
        ns = NamespaceDetail(
            namespaceId="a1b2c3d4-e5f6-4890-abcd-ef1234567890",
            name="sales",
            displayName="Sales Domain",
            description="Sales data",
            owner="alice@co.com",
            status=NamespaceStatus.ACTIVE,
            createdAt="2026-01-01T00:00:00Z",
        )
        mock_get_svc.return_value.create.return_value = ns
        resp = handler(
            _event(
                {"name": "sales", "owner": "alice@co.com", "displayName": "Sales Domain", "description": "Sales data"}
            ),
            None,
        )
        assert resp["statusCode"] == 201
        assert json.loads(resp["body"])["namespace"]["displayName"] == "Sales Domain"


# ═══════════════════════════════════════════════════════════════════
# Conflict
# ═══════════════════════════════════════════════════════════════════


class TestConflict:
    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_returns_409(self, mock_get_svc):
        mock_get_svc.return_value.create.side_effect = ConflictError("Namespace 'sales' already exists")
        resp = handler(_event(VALID_BODY), None)
        assert resp["statusCode"] == 409
        assert "already exists" in json.loads(resp["body"])["message"]


# ═══════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════


class TestErrorHandling:
    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_datazone_failure_returns_500(self, mock_get_svc):
        mock_get_svc.return_value.create.side_effect = MetadataStoreError("DataZone unavailable")
        resp = handler(_event(VALID_BODY), None)
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        # Curated, actionable message — no internal DataZone detail leaked.
        assert "rolled back" in body["message"]
        assert "DataZone unavailable" not in body["message"]

    @patch("coa_control_plane.namespace.create_handler._get_service")
    def test_unexpected_error_returns_500(self, mock_get_svc):
        mock_get_svc.return_value.create.side_effect = RuntimeError("boom")
        resp = handler(_event(VALID_BODY), None)
        assert resp["statusCode"] == 500


# ═══════════════════════════════════════════════════════════════════
# Service unit tests (using DynamoDBDAO)
# ═══════════════════════════════════════════════════════════════════


class TestNamespaceService:
    ROLE_TEMPLATES = [
        {
            "PK": "NAMESPACE_TEMPLATE",
            "SK": "ROLE#namespace-owner",
            "name": "Namespace Owner",
            "description": "Full control within a namespace",
            "scope": "NAMESPACE",
            "cedarPolicy": "permit(...);",
        },
        {
            "PK": "NAMESPACE_TEMPLATE",
            "SK": "ROLE#data-steward",
            "name": "Data Steward",
            "description": "Manage data sources",
            "scope": "NAMESPACE",
            "cedarPolicy": "permit(...);",
        },
        {
            "PK": "NAMESPACE_TEMPLATE",
            "SK": "ROLE#data-analyst",
            "name": "Data Analyst",
            "description": "Query and read access",
            "scope": "NAMESPACE",
            "cedarPolicy": "permit(...);",
        },
        {
            "PK": "NAMESPACE_TEMPLATE",
            "SK": "ROLE#namespace-maintainer",
            "name": "Namespace Maintainer",
            "description": "Maintain namespace config",
            "scope": "NAMESPACE",
            "cedarPolicy": "permit(...);",
        },
    ]

    def _make_service(self, *, query_items=None, project_id="proj-abc", reserve_error=None, metadata_error=None):
        mock_metadata = MagicMock()
        if metadata_error:
            mock_metadata.create_project.side_effect = metadata_error
        else:
            mock_metadata.create_project.return_value = ProjectResult(
                project_id=project_id, name="550e8400-e29b-41d4-a716-446655440000t"
            )

        ns_dao = MagicMock(spec=["query", "put", "delete", "update", "_table_name"])
        ns_dao._table_name = "test-namespaces"
        ns_dao.query.return_value = PaginatedResult(items=query_items or [])
        if reserve_error:
            ns_dao.put.side_effect = reserve_error

        roles_dao = MagicMock(spec=["query", "put", "_table_name"])
        roles_dao._table_name = "test-roles"
        roles_dao.query.return_value = PaginatedResult(items=self.ROLE_TEMPLATES)

        mappings_dao = MagicMock(spec=["put", "_table_name"])
        mappings_dao._table_name = "test-role-mappings"

        svc = NamespaceService(
            namespaces_dao=ns_dao,
            roles_dao=roles_dao,
            mappings_dao=mappings_dao,
            metadata_store=mock_metadata,
        )
        return svc

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_returns_namespace_detail(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        result = svc.create(req)

        assert result.name == "sales"
        assert result.owner == "alice@co.com"
        assert result.status == NamespaceStatus.ACTIVE
        uuid.UUID(result.namespace_id)  # validates it's a valid UUID
        assert result.data_zone_project_id == "proj-abc"

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_copies_role_templates(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)

        # Should query NAMESPACE_TEMPLATE roles
        roles_query = svc._roles_dao.query.call_args[0][0]
        assert roles_query.expression_values[":pk"] == "NAMESPACE_TEMPLATE"

        # Should write 4 role items (one per template)
        role_puts = svc._roles_dao.put.call_args_list
        assert len(role_puts) == 4
        role_sks = {call.args[0]["SK"] for call in role_puts}
        assert role_sks == {
            "ROLE#namespace-owner",
            "ROLE#data-steward",
            "ROLE#data-analyst",
            "ROLE#namespace-maintainer",
        }

        # Roles should not have 'actions' attribute
        for call in role_puts:
            assert "actions" not in call.args[0]

    @patch("coa_control_plane.namespace.service.boto3")
    def test_owner_grant_uses_encoded_principal_key(self, mock_boto3):
        """The auto-created owner grant must URL-encode the owner in key fields.

        Otherwise a namespace owner whose email contains special characters
        (e.g. ``foo+123@co.com``) is written with a raw principalKey but queried
        with an encoded one by the authorizer — locking the owner out of the
        namespace they just created.
        """
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="foo+123@co.com")
        svc.create(req)

        grant = svc._mappings_dao.put.call_args.args[0]
        assert grant["principalKey"] == "User::foo%2B123@co.com"
        assert grant["PK"].endswith("#User::foo%2B123@co.com")
        assert grant["principalRoleKey"] == "User::foo%2B123@co.com#ROLE#namespace-owner"
        # Raw value preserved for display / audit fields.
        assert grant["principalId"] == "foo+123@co.com"
        assert grant["grantedBy"] == "foo+123@co.com"

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_confirms_reservation(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)

        # update confirms the reservation and removes TTL
        svc._ns_dao.update.assert_called_once()
        call_args = svc._ns_dao.update.call_args
        assert call_args.args[0]["PK"] == "NS_NAME#sales"
        assert call_args.args[1]["status"] == "CONFIRMED"
        assert call_args.kwargs["remove_fields"] == ["ttl"]

    def test_reservation_conflict_raises(self):
        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}
        svc = self._make_service(reserve_error=ClientError(error_response, "PutItem"))
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        with pytest.raises(ConflictError, match="already exists"):
            svc.create(req)

    def test_datazone_failure_cleans_up_reservation(self):
        svc = self._make_service(metadata_error=MetadataStoreError("boom"))
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        with pytest.raises(MetadataStoreError):
            svc.create(req)

        # Reservation should be deleted on failure
        svc._ns_dao.delete.assert_called_once()
        key = svc._ns_dao.delete.call_args.args[0]
        assert key["PK"] == "NS_NAME#sales"

    def test_pre_flight_conflict(self):
        svc = self._make_service(query_items=[{"PK": "NS#existing"}])
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        with pytest.raises(ConflictError, match="already exists"):
            svc.create(req)

    @patch("coa_control_plane.namespace.service.boto3")
    def test_query_uses_name_by_index(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)

        query_call = svc._ns_dao.query.call_args[0][0]
        assert query_call.index_name == "NameByIndex"
        assert query_call.expression_values == {":name": "sales"}

    @patch("coa_control_plane.namespace.service.boto3")
    def test_empty_role_templates_raises(self, mock_boto3):
        mock_boto3.client.return_value = MagicMock(
            get_parameter=MagicMock(return_value={"Parameter": {"Value": "arn:aws:iam::123:role/access"}})
        )
        svc = self._make_service()
        svc._roles_dao.query.return_value = PaginatedResult(items=[])
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        with pytest.raises(RuntimeError, match="No role templates found"):
            svc.create(req)

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_adds_project_access_role_membership(self, mock_boto3):
        mock_ssm = MagicMock()
        mock_boto3.client.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "arn:aws:iam::123:role/project-access"}}

        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")

        with patch.dict(
            os.environ,
            {
                "PROJECT_ACCESS_ROLE_ARN_SSM": "/coa/smus/dz-project-access-role-arn",
            },
        ):
            svc.create(req)

        # Should NOT call ensure_user_profile (profile created at deploy time)

        # Should call create_project_membership for project-access role only
        membership_calls = svc._metadata_store.create_project_membership.call_args_list
        assert len(membership_calls) == 1
        assert membership_calls[0].kwargs["project_id"] == "proj-abc"
        assert membership_calls[0].kwargs["user_identifier"] == "arn:aws:iam::123:role/project-access"

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_fails_if_project_membership_fails(self, mock_boto3):
        mock_ssm = MagicMock()
        mock_boto3.client.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "arn:aws:iam::123:role/project-access"}}

        svc = self._make_service()
        svc._metadata_store.create_project_membership.side_effect = MetadataStoreError("membership failed")

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")

        with (
            patch.dict(
                os.environ,
                {
                    "PROJECT_ACCESS_ROLE_ARN_SSM": "/coa/smus/dz-project-access-role-arn",
                },
            ),
            pytest.raises(MetadataStoreError, match="membership failed"),
        ):
            svc.create(req)

    @patch("coa_control_plane.namespace.service.boto3")
    def test_membership_failure_rolls_back_project_and_reservation(self, mock_boto3):
        """A membership failure must not orphan the DataZone project.

        create_project succeeds, then membership registration fails. The
        half-created project must be deleted and the name reservation released
        so the failure leaves no orphaned project and does not block the name.
        """
        mock_ssm = MagicMock()
        mock_boto3.client.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "arn:aws:iam::123:role/project-access"}}

        svc = self._make_service()
        svc._metadata_store.create_project_membership.side_effect = MetadataStoreError("membership failed")

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")

        with (
            patch.dict(os.environ, {"PROJECT_ACCESS_ROLE_ARN_SSM": "/coa/smus/dz-project-access-role-arn"}),
            pytest.raises(MetadataStoreError, match="membership failed"),
        ):
            svc.create(req)

        # DataZone project torn down.
        svc._metadata_store.delete_project.assert_called_once_with(project_id="proj-abc")
        # Name reservation released.
        svc._ns_dao.delete.assert_called_once()
        assert svc._ns_dao.delete.call_args.args[0]["PK"] == "NS_NAME#sales"

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_fails_when_project_access_role_ssm_not_set(self, mock_boto3):
        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")

        with (
            patch.dict(
                os.environ,
                {
                    "PROJECT_ACCESS_ROLE_ARN_SSM": "",
                },
            ),
            pytest.raises(MetadataStoreError, match="PROJECT_ACCESS_ROLE_ARN_SSM"),
        ):
            svc.create(req)

    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_fails_when_ssm_parameter_not_found(self, mock_boto3):
        """Regression test — missing SSM param must fail namespace creation.

        Previously this would log a warning and silent-return, creating a
        namespace that can never scan (DataZone Search requires project
        membership).
        """
        mock_ssm = MagicMock()
        mock_boto3.client.return_value = mock_ssm
        error_response = {"Error": {"Code": "ParameterNotFound", "Message": "not found"}}
        mock_ssm.get_parameter.side_effect = ClientError(error_response, "GetParameter")

        svc = self._make_service()
        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")

        with (
            patch.dict(
                os.environ,
                {
                    "PROJECT_ACCESS_ROLE_ARN_SSM": "/coa/smus/dz-project-access-role-arn",
                },
            ),
            pytest.raises(MetadataStoreError, match="not found"),
        ):
            svc.create(req)
