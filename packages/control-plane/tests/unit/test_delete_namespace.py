# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the async delete_handler (mark DELETING, start SFN, 202)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

MODULE = "coa_control_plane.namespace.delete_handler"

_NS_ITEM = {
    "PK": "NS#11111111-1111-4111-8111-111111111111",
    "SK": "METADATA",
    "namespaceId": "11111111-1111-4111-8111-111111111111",
    "name": "sales",
    "status": "ARCHIVED",
    "athenaWorkgroupName": f"{RESOURCE_PREFIX}-dev-sales",
    "dataZoneProjectId": "proj-abc",
}


def _make_event(namespace_id: str = "11111111-1111-4111-8111-111111111111") -> dict:
    return {
        "pathParameters": {"namespaceId": namespace_id},
        "httpMethod": "DELETE",
        "resource": "/namespaces/{namespaceId}",
    }


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("SOURCES_TABLE", "test-sources")
    monkeypatch.setenv("METRIC_IMPORT_JOBS_TABLE", "test-metric-import-jobs")
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "https://ontology.internal")
    monkeypatch.setenv("DELETION_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:ns-delete")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def reset_singletons():
    yield
    import coa_control_plane.namespace.delete_handler as m

    m._ns_dao = None
    m._sfn_client = None


@pytest.mark.unit
class TestDeleteNamespaceAsync:
    def test_successful_delete_starts_sfn_and_returns_202(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}
        mock_sfn = MagicMock()

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._get_sfn_client", return_value=mock_sfn),
            patch(f"{MODULE}._check_active_operations", return_value=[]),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 202
        body = json.loads(resp["body"])
        assert body["namespaceId"] == "11111111-1111-4111-8111-111111111111"
        assert body["status"] == "DELETING"

        # Status set to DELETING before starting the execution.
        mock_dao.update.assert_called_once_with(
            key={"PK": "NS#11111111-1111-4111-8111-111111111111", "SK": "METADATA"},
            update_fields={"status": "DELETING"},
        )

        mock_sfn.start_execution.assert_called_once()
        kwargs = mock_sfn.start_execution.call_args.kwargs
        assert kwargs["name"] == "delete-11111111-1111-4111-8111-111111111111"
        sfn_input = json.loads(kwargs["input"])
        assert sfn_input["namespaceId"] == "11111111-1111-4111-8111-111111111111"
        assert sfn_input["namespaceName"] == "sales"
        assert sfn_input["athenaWorkgroupName"] == f"{RESOURCE_PREFIX}-dev-sales"
        assert sfn_input["dataZoneProjectId"] == "proj-abc"

    def test_retry_from_delete_failed_uses_unique_execution_name(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "DELETE_FAILED"}
        mock_sfn = MagicMock()

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._get_sfn_client", return_value=mock_sfn),
            patch(f"{MODULE}._check_active_operations", return_value=[]),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 202
        kwargs = mock_sfn.start_execution.call_args.kwargs
        assert kwargs["name"].startswith("delete-11111111-1111-4111-8111-111111111111-retry-")
        assert kwargs["name"] != "delete-11111111-1111-4111-8111-111111111111"

    def test_blocked_by_active_operation_returns_409(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._check_active_operations", return_value=["2 metric import job(s) in progress"]),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert "2 metric import job(s) in progress" in body["message"]
        assert "2 metric import job(s) in progress" in body["blockers"]
        mock_dao.update.assert_not_called()

    def test_blocked_by_multiple_active_operations_returns_all_blockers(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}
        blockers = [
            "1 source(s) actively scanning or enriching",
            "2 ontology induction job(s) in progress",
            "1 metric import job(s) in progress",
        ]

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._check_active_operations", return_value=blockers),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert len(body["blockers"]) == 3

    def test_not_found_returns_404(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None

        with patch(f"{MODULE}._get_ns_dao", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 404

    def test_active_namespace_returns_409(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ACTIVE"}

        with patch(f"{MODULE}._get_ns_dao", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 409
        assert "ARCHIVED or DELETE_FAILED" in json.loads(resp["body"])["message"]

    def test_already_deleted_returns_409(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "DELETED"}

        with patch(f"{MODULE}._get_ns_dao", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 409
        assert "already deleted" in json.loads(resp["body"])["message"]

    def test_already_deleting_returns_202_idempotently(self):
        """A second DELETE call while deletion is in progress is idempotent, not an error."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "DELETING"}

        with patch(f"{MODULE}._get_ns_dao", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 202
        body = json.loads(resp["body"])
        assert body["status"] == "DELETING"
        mock_dao.update.assert_not_called()

    def test_sfn_execution_already_exists_returns_202(self):
        """Race: two concurrent DELETE calls both pass the DELETING check before either
        starts the execution. The second start_execution call hits
        ExecutionAlreadyExists — this must be treated as success (202), not 500."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}
        mock_sfn = MagicMock()

        class _ExecutionAlreadyExists(Exception):
            pass

        mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
        mock_sfn.start_execution.side_effect = _ExecutionAlreadyExists()

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._get_sfn_client", return_value=mock_sfn),
            patch(f"{MODULE}._check_active_operations", return_value=[]),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 202

    def test_sfn_start_failure_returns_500_without_status_change(self):
        """When start_execution fails, status stays unchanged (ARCHIVED) so
        the user can retry. No DELETING or DELETE_FAILED is set."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = {**_NS_ITEM, "status": "ARCHIVED"}
        mock_sfn = MagicMock()

        class _ExecutionAlreadyExists(Exception):
            pass

        mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
        mock_sfn.start_execution.side_effect = RuntimeError("SFN unavailable")

        with (
            patch(f"{MODULE}._get_ns_dao", return_value=mock_dao),
            patch(f"{MODULE}._get_sfn_client", return_value=mock_sfn),
            patch(f"{MODULE}._check_active_operations", return_value=[]),
        ):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler(_make_event(), None)

        assert resp["statusCode"] == 500
        # No status update — namespace stays ARCHIVED, user can retry.
        assert mock_dao.update.call_count == 0

    def test_missing_namespace_id_returns_400(self):
        mock_dao = MagicMock()

        with patch(f"{MODULE}._get_ns_dao", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import handler

            resp = handler({"pathParameters": {}}, None)

        assert resp["statusCode"] == 400


@pytest.mark.unit
class TestCheckActiveOperations:
    """_check_active_operations aggregates blockers from sources, induction jobs,
    and import jobs."""

    def test_aggregates_all_three_checks(self):
        from coa_control_plane.namespace.delete_handler import _check_active_operations

        with (
            patch(f"{MODULE}._check_active_sources", return_value=["a"]),
            patch(f"{MODULE}._check_active_induction_jobs", return_value=["b"]),
            patch(f"{MODULE}._check_active_import_jobs", return_value=["c"]),
        ):
            result = _check_active_operations("11111111-1111-4111-8111-111111111111")

        assert result == ["a", "b", "c"]

    def test_empty_when_nothing_active(self):
        from coa_control_plane.namespace.delete_handler import _check_active_operations

        with (
            patch(f"{MODULE}._check_active_sources", return_value=[]),
            patch(f"{MODULE}._check_active_induction_jobs", return_value=[]),
            patch(f"{MODULE}._check_active_import_jobs", return_value=[]),
        ):
            result = _check_active_operations("11111111-1111-4111-8111-111111111111")

        assert result == []


@pytest.mark.unit
class TestCheckActiveSources:
    """Only SOURCE_ACTIVE_STATUSES (minus DELETING) should block deletion."""

    def test_no_sources_returns_empty(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_sources

            result = _check_active_sources("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_scanning_source_blocks(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(
            items=[{"status": "SCANNING"}, {"status": "COMPLETED"}],
            last_evaluated_key=None,
        )

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_sources

            result = _check_active_sources("11111111-1111-4111-8111-111111111111")

        assert len(result) == 1
        assert "1 source(s) actively scanning or enriching" in result[0]

    def test_deleting_source_does_not_block(self):
        """A source already being individually deleted is not a reason to block
        namespace deletion — DELETING is excluded from the blocking set."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(
            items=[{"status": "DELETING"}],
            last_evaluated_key=None,
        )

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_sources

            result = _check_active_sources("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_queries_by_namespace_gsi(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_sources

            _check_active_sources("11111111-1111-4111-8111-111111111111")

        params = mock_dao.query.call_args[0][0]
        assert params.index_name == "ByNamespace"
        assert params.expression_values[":ns"] == "11111111-1111-4111-8111-111111111111"

    def test_no_table_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("SOURCES_TABLE", raising=False)
        from coa_control_plane.namespace.delete_handler import _check_active_sources

        assert _check_active_sources("11111111-1111-4111-8111-111111111111") == []


@pytest.mark.unit
class TestCheckActiveInductionJobs:
    """Calls the ontology-engine's GET /induce/active-jobs endpoint."""

    def test_no_active_jobs_returns_empty(self):
        from coa_control_plane.namespace.delete_handler import _check_active_induction_jobs

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"activeJobs": 0}).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp

        with patch(f"{MODULE}.urllib_request.urlopen", return_value=mock_cm):
            result = _check_active_induction_jobs("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_active_jobs_block(self):
        from coa_control_plane.namespace.delete_handler import _check_active_induction_jobs

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"activeJobs": 3}).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp

        with patch(f"{MODULE}.urllib_request.urlopen", return_value=mock_cm):
            result = _check_active_induction_jobs("11111111-1111-4111-8111-111111111111")

        assert len(result) == 1
        assert "3 ontology induction/ingest job(s) or proposal accept(s) in progress" in result[0]

    def test_endpoint_not_configured_skips_check(self, monkeypatch):
        monkeypatch.delenv("ONTOLOGY_ENGINE_ENDPOINT", raising=False)
        from coa_control_plane.namespace.delete_handler import _check_active_induction_jobs

        assert _check_active_induction_jobs("11111111-1111-4111-8111-111111111111") == []

    def test_unreachable_endpoint_fails_open_after_retries(self, monkeypatch):
        """If the ontology-engine can't be reached after retries, ALLOW deletion
        (fail open) rather than blocking forever. The delete path is only reached
        for an already-ARCHIVED namespace (no new induction can start), so a
        transient unreachable checker must not permanently brick every delete —
        it previously did, leaking resources indefinitely. The async deletion
        pipeline is itself race-tolerant (genuine races land in DELETE_FAILED).
        """
        from urllib.error import URLError

        from coa_control_plane.namespace.delete_handler import _check_active_induction_jobs

        monkeypatch.setenv("DELETE_ACTIVE_JOBS_CHECK_ATTEMPTS", "2")
        with (
            patch(f"{MODULE}.urllib_request.urlopen", side_effect=URLError("unreachable")),
            patch(f"{MODULE}.time.sleep"),  # don't actually sleep in tests
        ):
            result = _check_active_induction_jobs("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_transient_error_then_success_honors_endpoint(self, monkeypatch):
        """A retry that eventually succeeds honors the endpoint's answer rather
        than failing open early: recovers on the 2nd attempt and blocks on a
        real activeJobs count."""
        from urllib.error import URLError

        from coa_control_plane.namespace.delete_handler import _check_active_induction_jobs

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"activeJobs": 2}).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp

        monkeypatch.setenv("DELETE_ACTIVE_JOBS_CHECK_ATTEMPTS", "3")
        with (
            patch(f"{MODULE}.urllib_request.urlopen", side_effect=[URLError("transient"), mock_cm]),
            patch(f"{MODULE}.time.sleep"),
        ):
            result = _check_active_induction_jobs("11111111-1111-4111-8111-111111111111")

        assert len(result) == 1
        assert "2 ontology induction/ingest job(s) or proposal accept(s) in progress" in result[0]


@pytest.mark.unit
class TestCheckActiveImportJobs:
    """Only IN_PROGRESS metric import jobs should block deletion."""

    def test_no_jobs_returns_empty(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_import_jobs

            result = _check_active_import_jobs("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_completed_and_failed_jobs_do_not_block(self):
        """Regression for #215: terminal job records must never block deletion."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(
            items=[
                {"status": "COMPLETED"},
                {"status": "COMPLETED"},
                {"status": "FAILED"},
            ],
            last_evaluated_key=None,
        )

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_import_jobs

            result = _check_active_import_jobs("11111111-1111-4111-8111-111111111111")

        assert result == []

    def test_in_progress_job_blocks(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(
            items=[{"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
            last_evaluated_key=None,
        )

        with patch(f"{MODULE}.DynamoDBDAO", return_value=mock_dao):
            from coa_control_plane.namespace.delete_handler import _check_active_import_jobs

            result = _check_active_import_jobs("11111111-1111-4111-8111-111111111111")

        assert len(result) == 1
        assert "1 metric import job(s) in progress" in result[0]

    def test_no_table_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("METRIC_IMPORT_JOBS_TABLE", raising=False)
        from coa_control_plane.namespace.delete_handler import _check_active_import_jobs

        assert _check_active_import_jobs("11111111-1111-4111-8111-111111111111") == []
