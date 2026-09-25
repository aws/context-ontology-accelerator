# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for async import: job store, import worker, get_import_job handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.source_status import PERMISSIVE_ENV, SourceValidationUnavailableError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    """Keep unrelated worker tests independent from deployed source metadata."""
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    yield


# ── import_job_store tests ──────────────────────────────────────────────


class TestImportJobStore:
    """Tests for the DynamoDB job store."""

    @staticmethod
    def _offset_plan(*, offset: int = 10):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetPlan, OffsetPlanEntry

        return OffsetPlan(
            offset=offset,
            entries=(OffsetPlanEntry(name="metric_a", disposition=MetricDisposition.CREATE),),
        )

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_create_job(self, mock_table):
        from coa_metrics.api.import_job_store import create_job

        table = MagicMock()
        mock_table.return_value = table

        job = create_job("ns-1", "imports/test.yaml", 100)

        assert job["namespaceId"] == "ns-1"
        assert job["s3Key"] == "imports/test.yaml"
        assert job["metricsTotal"] == 100
        assert job["metricsProcessed"] == 0
        assert job["nextOffset"] == 0
        assert job["chunkSize"] == 50
        assert job["status"] == "IN_PROGRESS"
        assert job["processedOffsets"] == []
        assert "jobId" in job
        table.put_item.assert_called_once()

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_get_job_found(self, mock_table):
        from coa_metrics.api.import_job_store import get_job

        table = MagicMock()
        table.get_item.return_value = {"Item": {"jobId": "j-1", "status": "IN_PROGRESS"}}
        mock_table.return_value = table

        result = get_job("ns-1", "j-1")

        assert result is not None
        assert result["jobId"] == "j-1"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_get_job_not_found(self, mock_table):
        from coa_metrics.api.import_job_store import get_job

        table = MagicMock()
        table.get_item.return_value = {}
        mock_table.return_value = table

        result = get_job("ns-1", "j-missing")

        assert result is None

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_complete_job_only_updates_active_job(self, mock_table):
        from coa_metrics.api.import_job_store import complete_job

        table = MagicMock()
        mock_table.return_value = table

        assert complete_job("ns-1", "j-1", status="COMPLETED") is True

        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["ConditionExpression"] == "#s = :in_progress"
        assert call_kwargs["ExpressionAttributeValues"][":s"] == "COMPLETED"
        assert "REMOVE #plan, #claim_offset, #claim_token, #claim_expires" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeNames"]["#plan"] == "offsetPlan"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_complete_job_does_not_reverse_terminal_failure(self, mock_table):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import complete_job

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "terminal"}},
            "UpdateItem",
        )
        mock_table.return_value = table

        assert complete_job("ns-1", "j-1", status="COMPLETED") is False

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_fail_job_appends_visible_error_only_while_active(self, mock_table):
        from coa_metrics.api.import_job_store import fail_job

        table = MagicMock()
        mock_table.return_value = table

        assert fail_job("ns-1", "j-1", "automatic recovery exhausted") is True

        kwargs = table.update_item.call_args[1]
        assert kwargs["ConditionExpression"] == "#s = :in_progress"
        assert kwargs["ExpressionAttributeValues"][":failed"] == "FAILED"
        assert kwargs["ExpressionAttributeValues"][":errors"] == ["automatic recovery exhausted"]
        assert "REMOVE #plan, #claim_offset, #claim_token, #claim_expires" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeNames"]["#plan"] == "offsetPlan"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_fail_job_ignores_already_terminal_job(self, mock_table):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import fail_job

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "terminal"}},
            "UpdateItem",
        )
        mock_table.return_value = table

        assert fail_job("ns-1", "j-1", "late failure") is False

    @pytest.mark.parametrize("token", [None, ""])
    def test_offset_claim_acquired_requires_non_empty_token(self, token):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState

        with pytest.raises(ValueError, match="ACQUIRED offset claim requires a non-empty token"):
            OffsetClaim(OffsetClaimState.ACQUIRED, token)

    @pytest.mark.parametrize(
        "state_name",
        ["ALREADY_PROCESSED", "STALE", "INVALID_OFFSET", "LEASE_HELD", "NOT_ACTIVE"],
    )
    def test_offset_claim_non_acquired_states_reject_token(self, state_name):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState

        with pytest.raises(ValueError, match="must not contain a token"):
            OffsetClaim(OffsetClaimState[state_name], "unexpected-token")

    @pytest.mark.parametrize(
        ("offset", "end_offset", "expected_state"),
        [
            (5, 7, "CURRENT"),
            (2, 5, "REPLAY"),
            (0, 2, "STALE"),
            (6, 7, "INVALID"),
            (1, 2, "INVALID"),
            (0, 5, "INVALID"),
        ],
    )
    def test_assess_job_offset_enforces_contiguous_exact_intervals(self, offset, end_offset, expected_state):
        from coa_metrics.api.import_job_store import JobOffsetState, assess_job_offset

        job = {
            "metricsTotal": 7,
            "metricsProcessed": 5,
            "nextOffset": 5,
            "processedOffsets": [0, 2],
        }

        assessment = assess_job_offset(job, offset=offset, end_offset=end_offset)

        assert assessment.state == JobOffsetState[expected_state]
        assert assessment.frontier == 5

    @pytest.mark.parametrize(
        "history",
        ["missing", [], [50], [50, 0], "not-a-list"],
        ids=["missing", "empty", "does-not-start-at-zero", "unsorted", "wrong-type"],
    )
    def test_progressed_legacy_job_reports_unverifiable_history(self, history):
        from coa_metrics.api.import_job_store import UnverifiableImportProgressError, job_next_offset

        job = {"metricsTotal": 100, "metricsProcessed": 50}
        if history != "missing":
            job["processedOffsets"] = history

        with pytest.raises(UnverifiableImportProgressError) as raised:
            job_next_offset(job)

        assert raised.value.frontier == 50

    def test_zero_progress_legacy_job_remains_lazily_upgradeable(self):
        from coa_metrics.api.import_job_store import job_next_offset

        assert job_next_offset({"metricsTotal": 100, "metricsProcessed": 0}) == 0

    @patch("coa_metrics.api.import_job_store.uuid.uuid4", return_value="claim-token")
    @patch("coa_metrics.api.import_job_store.time.time", return_value=1_000)
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_acquires_fenced_lease(self, mock_table, _mock_time, _mock_uuid):
        from coa_metrics.api.import_job_store import (
            OffsetClaimState,
            _serialize_offset_plan,
            claim_job_offset,
        )

        plan = self._offset_plan(offset=10)
        table = MagicMock()
        table.update_item.return_value = {"Attributes": {"offsetPlan": _serialize_offset_plan(plan)}}
        mock_table.return_value = table

        claim = claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert claim.state == OffsetClaimState.ACQUIRED
        assert claim.token == "claim-token"
        assert claim.plan == plan
        kwargs = table.update_item.call_args[1]
        assert kwargs["UpdateExpression"] == (
            "SET #next = if_not_exists(#next, :offset), "
            "#chunk_size = if_not_exists(#chunk_size, :chunk_size), "
            "#claim_offset = :offset, #claim_token = :token, "
            "#claim_expires = :expires, #updated = :ts"
        )
        assert kwargs["ConditionExpression"] == (
            "#s = :in_progress AND #metrics_processed = :offset "
            "AND (attribute_not_exists(#next) OR #next = :offset) "
            "AND (attribute_not_exists(#chunk_size) OR #chunk_size = :chunk_size) "
            "AND :end <= #total "
            "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
            "AND (attribute_not_exists(#plan) OR #plan.#plan_offset = :offset) "
            "AND (attribute_not_exists(#claim_offset) OR attribute_not_exists(#claim_expires) "
            "OR #claim_expires <= :now)"
        )
        assert kwargs["ReturnValues"] == "ALL_NEW"
        assert kwargs["ExpressionAttributeNames"]["#plan"] == "offsetPlan"
        values = kwargs["ExpressionAttributeValues"]
        assert values[":offset"] == 10
        assert values[":end"] == 11
        assert values[":chunk_size"] == 1
        assert values[":token"] == "claim-token"
        assert values[":now"] == 1_000
        assert values[":expires"] == 1_060

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_propagates_and_logs_nonconditional_client_error(self, mock_table, mock_logger):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import claim_job_offset

        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )
        table = MagicMock()
        table.update_item.side_effect = error
        mock_table.return_value = table

        with pytest.raises(ClientError) as raised:
            claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_claim_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="ProvisionedThroughputExceededException",
        )
        table.get_item.assert_not_called()

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_classifies_processed_offset(self, mock_table):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "processed"}},
            "UpdateItem",
        )
        table.get_item.return_value = {
            "Item": {
                "jobId": "j-1",
                "status": "IN_PROGRESS",
                "metricsTotal": 20,
                "metricsProcessed": 11,
                "nextOffset": 11,
                "processedOffsets": [0, 10],
            }
        }
        mock_table.return_value = table

        claim = claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert claim.state == OffsetClaimState.ALREADY_PROCESSED
        assert claim.token is None
        table.get_item.assert_called_once_with(
            Key={"PK": "NS#ns-1", "SK": "IMPORT#j-1"},
            ConsistentRead=True,
        )

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_classifies_live_lease(self, mock_table):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "lease held"}},
            "UpdateItem",
        )
        table.get_item.return_value = {
            "Item": {
                "jobId": "j-1",
                "status": "IN_PROGRESS",
                "metricsTotal": 20,
                "metricsProcessed": 10,
                "nextOffset": 10,
                "processedOffsets": [0],
                "claimOffset": 10,
                "claimToken": "other-token",
                "claimLeaseExpiresAt": 2_000,
            }
        }
        mock_table.return_value = table

        claim = claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert claim.state == OffsetClaimState.LEASE_HELD
        assert claim.token is None

    @pytest.mark.parametrize("item", [None, {"jobId": "j-1", "status": "FAILED"}])
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_classifies_inactive_job(self, mock_table, item):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "inactive"}},
            "UpdateItem",
        )
        table.get_item.return_value = {"Item": item} if item else {}
        mock_table.return_value = table

        claim = claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert claim.state == OffsetClaimState.NOT_ACTIVE
        assert claim.token is None

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_finalize_job_offset_atomically_completes_with_fenced_accounting(self, mock_table):
        from coa_metrics.api.import_job_store import finalize_job_offset

        table = MagicMock()
        mock_table.return_value = table

        assert (
            finalize_job_offset(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                metrics_processed=5,
                metrics_created=3,
                metrics_updated=2,
                errors=["e1"],
                warnings=["w1"],
                mark_job_completed=True,
            )
            is True
        )

        table.update_item.assert_called_once()
        kwargs = table.update_item.call_args[1]
        expression = kwargs["UpdateExpression"]
        assert "#next = :next" in expression
        assert "#processed = list_append(if_not_exists(#processed, :empty), :offsets)" in expression
        assert "#errors = list_append(if_not_exists(#errors, :empty), :errors)" in expression
        assert "#warnings = list_append(if_not_exists(#warnings, :empty), :warnings)" in expression
        assert "#s = :completed" in expression
        assert "REMOVE #plan, #claim_offset, #claim_token, #claim_expires" in expression
        assert "ADD metricsProcessed :mp, metricsCreated :mc, metricsUpdated :mu" in expression
        assert kwargs["ConditionExpression"] == (
            "#s = :in_progress AND #metrics_processed = :offset AND #next = :offset "
            "AND #claim_offset = :offset AND #claim_token = :claim_token "
            "AND #plan.#plan_offset = :offset "
            "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
            "AND :next = #total"
        )
        assert kwargs["ExpressionAttributeNames"]["#plan"] == "offsetPlan"
        values = kwargs["ExpressionAttributeValues"]
        assert values[":claim_token"] == "claim-token"
        assert values[":offset"] == 10
        assert values[":next"] == 15
        assert values[":offsets"] == [10]
        assert (values[":mp"], values[":mc"], values[":mu"]) == (5, 3, 2)
        assert values[":errors"] == ["e1"]
        assert values[":warnings"] == ["w1"]
        assert values[":completed"] == "COMPLETED"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_finalize_job_offset_returns_false_when_fence_is_lost(self, mock_table):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import finalize_job_offset

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "token mismatch"}},
            "UpdateItem",
        )
        mock_table.return_value = table

        assert (
            finalize_job_offset(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="stale-token",
                metrics_processed=5,
                metrics_created=3,
                metrics_updated=2,
            )
            is False
        )

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_finalize_job_offset_propagates_and_logs_nonconditional_client_error(self, mock_table, mock_logger):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import finalize_job_offset

        error = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "temporary failure"}},
            "UpdateItem",
        )
        table = MagicMock()
        table.update_item.side_effect = error
        mock_table.return_value = table

        with pytest.raises(ClientError) as raised:
            finalize_job_offset(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                metrics_processed=5,
                metrics_created=3,
                metrics_updated=2,
            )

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_finalize_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="InternalServerError",
        )

    @pytest.mark.parametrize(
        ("name", "disposition_name", "warning", "error", "message"),
        [
            (" ", "CREATE", None, None, "name must be a non-empty string"),
            ("metric", "CREATE", "unexpected", None, "must not contain a warning or error"),
            ("metric", "CREATE", None, "unexpected", "must not contain a warning or error"),
            ("metric", "UPDATE", None, None, "requires a non-empty warning"),
            ("metric", "UPDATE", " ", None, "requires a non-empty warning"),
            ("metric", "UPDATE", "warning", "unexpected", "must not contain an error"),
            ("metric", "ERROR", None, None, "requires a non-empty error"),
            ("metric", "ERROR", None, " ", "requires a non-empty error"),
            ("metric", "ERROR", "unexpected", "error", "must not contain a warning"),
        ],
    )
    def test_offset_plan_entry_rejects_invalid_disposition_messages(
        self,
        name,
        disposition_name,
        warning,
        error,
        message,
    ):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetPlanEntry

        with pytest.raises(ValueError, match=message):
            OffsetPlanEntry(
                name=name,
                disposition=MetricDisposition[disposition_name],
                warning=warning,
                error=error,
            )

    def test_offset_plan_valid_invariants_and_dynamodb_round_trip(self):
        from decimal import Decimal

        from coa_metrics.api.import_job_store import (
            MetricDisposition,
            OffsetPlan,
            OffsetPlanEntry,
            _parse_offset_plan,
            _serialize_offset_plan,
        )

        plan = OffsetPlan(
            offset=10,
            entries=(
                OffsetPlanEntry(name="created", disposition=MetricDisposition.CREATE),
                OffsetPlanEntry(
                    name="updated",
                    disposition=MetricDisposition.UPDATE,
                    warning="existing metric overwritten",
                ),
                OffsetPlanEntry(
                    name="invalid",
                    disposition=MetricDisposition.ERROR,
                    error="metric validation failed",
                ),
            ),
        )

        serialized = _serialize_offset_plan(plan)

        assert serialized == {
            "offset": 10,
            "entries": [
                {"name": "created", "disposition": "CREATE"},
                {
                    "name": "updated",
                    "disposition": "UPDATE",
                    "warning": "existing metric overwritten",
                },
                {
                    "name": "invalid",
                    "disposition": "ERROR",
                    "error": "metric validation failed",
                },
            ],
        }
        assert _parse_offset_plan(serialized) == plan
        assert _parse_offset_plan({**serialized, "offset": Decimal(10)}) == plan

    def test_offset_plan_rejects_invalid_offset_and_entries(self):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetPlan, OffsetPlanEntry

        entry = OffsetPlanEntry(name="metric", disposition=MetricDisposition.CREATE)
        with pytest.raises(ValueError, match="non-negative integer"):
            OffsetPlan(offset=-1, entries=(entry,))
        with pytest.raises(ValueError, match="non-negative integer"):
            OffsetPlan(offset=True, entries=(entry,))
        with pytest.raises(ValueError, match="non-empty tuple"):
            OffsetPlan(offset=0, entries=())
        with pytest.raises(ValueError, match="non-empty tuple"):
            OffsetPlan(offset=0, entries=[entry])
        with pytest.raises(ValueError, match="only OffsetPlanEntry"):
            OffsetPlan(offset=0, entries=("metric",))

    @pytest.mark.parametrize(
        "serialized",
        [
            None,
            {},
            {"offset": 0, "entries": [], "unexpected": True},
            {"offset": "0", "entries": [{"name": "metric", "disposition": "CREATE"}]},
            {"offset": 0, "entries": []},
            {"offset": 0, "entries": ["metric"]},
            {"offset": 0, "entries": [{"name": "metric"}]},
            {"offset": 0, "entries": [{"name": "metric", "disposition": "UNKNOWN"}]},
            {"offset": 0, "entries": [{"name": "metric", "disposition": "UPDATE"}]},
            {
                "offset": 0,
                "entries": [{"name": "metric", "disposition": "CREATE", "unexpected": "field"}],
            },
        ],
    )
    def test_parse_offset_plan_rejects_malformed_dynamodb_data(self, serialized):
        from coa_metrics.api.import_job_store import _parse_offset_plan

        with pytest.raises(ValueError, match="offset plan"):
            _parse_offset_plan(serialized)

    @pytest.mark.parametrize(
        "state_name",
        ["ALREADY_PROCESSED", "STALE", "INVALID_OFFSET", "LEASE_HELD", "NOT_ACTIVE"],
    )
    def test_offset_claim_non_acquired_states_reject_plan(self, state_name):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState

        with pytest.raises(ValueError, match="must not contain a plan"):
            OffsetClaim(OffsetClaimState[state_name], plan=self._offset_plan())

    def test_offset_claim_acquired_rejects_untyped_plan(self):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState

        with pytest.raises(ValueError, match="plan must be an OffsetPlan"):
            OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token", plan="not-a-plan")

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_rejects_missing_response_attributes(self, mock_table):
        from coa_metrics.api.import_job_store import claim_job_offset

        table = MagicMock()
        table.update_item.return_value = {}
        mock_table.return_value = table

        with pytest.raises(ValueError, match="did not contain Attributes"):
            claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

    @pytest.mark.parametrize(
        ("persisted_plan", "message"),
        [
            ({"offset": 10, "entries": []}, "entries must be a non-empty"),
            (
                {"offset": 9, "entries": [{"name": "metric", "disposition": "CREATE"}]},
                "does not match requested offset 10",
            ),
        ],
    )
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_conditional_conflict_rejects_invalid_persisted_plan(
        self,
        mock_table,
        persisted_plan,
        message,
    ):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import claim_job_offset

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "plan fence"}},
            "UpdateItem",
        )
        table.get_item.return_value = {
            "Item": {
                "jobId": "j-1",
                "status": "IN_PROGRESS",
                "metricsTotal": 20,
                "metricsProcessed": 10,
                "nextOffset": 10,
                "processedOffsets": [0],
                "offsetPlan": persisted_plan,
            }
        }
        mock_table.return_value = table

        with pytest.raises(ValueError, match=message):
            claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        table.get_item.assert_called_once_with(
            Key={"PK": "NS#ns-1", "SK": "IMPORT#j-1"},
            ConsistentRead=True,
        )

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_propagates_and_logs_endpoint_connection_error(self, mock_table, mock_logger):
        from botocore.exceptions import EndpointConnectionError
        from coa_metrics.api.import_job_store import claim_job_offset

        error = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
        mock_table.side_effect = error

        with pytest.raises(EndpointConnectionError) as raised:
            claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_claim_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="EndpointConnectionError",
        )

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_claim_job_offset_logs_classification_read_transport_error(self, mock_table, mock_logger):
        from botocore.exceptions import ClientError, ReadTimeoutError
        from coa_metrics.api.import_job_store import claim_job_offset

        conditional_error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
            "UpdateItem",
        )
        read_error = ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
        table = MagicMock()
        table.update_item.side_effect = conditional_error
        mock_table.side_effect = [table, read_error]

        with pytest.raises(ReadTimeoutError) as raised:
            claim_job_offset("ns-1", "j-1", offset=10, end_offset=11, chunk_size=1, lease_seconds=60)

        assert raised.value is read_error
        mock_logger.exception.assert_called_once_with(
            "import_offset_claim_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="ReadTimeoutError",
        )

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_store_job_offset_plan_persists_once_under_token_fence(self, mock_table):
        from coa_metrics.api.import_job_store import _serialize_offset_plan, store_job_offset_plan

        plan = self._offset_plan(offset=10)
        table = MagicMock()
        mock_table.return_value = table

        assert (
            store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                plan=plan,
            )
            is True
        )

        kwargs = table.update_item.call_args[1]
        assert kwargs["UpdateExpression"] == "SET #plan = :plan, #updated = :ts"
        assert kwargs["ConditionExpression"] == (
            "#s = :in_progress AND #next = :offset "
            "AND #claim_offset = :offset AND #claim_token = :claim_token "
            "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
            "AND attribute_not_exists(#plan)"
        )
        assert kwargs["ExpressionAttributeNames"]["#plan"] == "offsetPlan"
        assert kwargs["ExpressionAttributeValues"][":plan"] == _serialize_offset_plan(plan)
        assert kwargs["ExpressionAttributeValues"][":claim_token"] == "claim-token"
        assert kwargs["ExpressionAttributeValues"][":offset"] == 10

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_store_job_offset_plan_rejects_mismatched_offset_before_update(self, mock_table):
        from coa_metrics.api.import_job_store import store_job_offset_plan

        with pytest.raises(ValueError, match="does not match claimed offset 10"):
            store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                plan=self._offset_plan(offset=11),
            )

        mock_table.assert_not_called()

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_store_job_offset_plan_returns_false_without_logging_when_fence_is_lost(
        self,
        mock_table,
        mock_logger,
    ):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import store_job_offset_plan

        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "token mismatch"}},
            "UpdateItem",
        )
        mock_table.return_value = table

        assert (
            store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="stale-token",
                plan=self._offset_plan(offset=10),
            )
            is False
        )
        mock_logger.exception.assert_not_called()

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_store_job_offset_plan_propagates_and_logs_nonconditional_client_error(
        self,
        mock_table,
        mock_logger,
    ):
        from botocore.exceptions import ClientError
        from coa_metrics.api.import_job_store import store_job_offset_plan

        error = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "temporary failure"}},
            "UpdateItem",
        )
        table = MagicMock()
        table.update_item.side_effect = error
        mock_table.return_value = table

        with pytest.raises(ClientError) as raised:
            store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                plan=self._offset_plan(offset=10),
            )

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_plan_store_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="InternalServerError",
        )

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_store_job_offset_plan_propagates_and_logs_endpoint_connection_error(
        self,
        mock_table,
        mock_logger,
    ):
        from botocore.exceptions import EndpointConnectionError
        from coa_metrics.api.import_job_store import store_job_offset_plan

        error = EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
        table = MagicMock()
        table.update_item.side_effect = error
        mock_table.return_value = table

        with pytest.raises(EndpointConnectionError) as raised:
            store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                plan=self._offset_plan(offset=10),
            )

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_plan_store_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="EndpointConnectionError",
        )

    @patch("coa_metrics.api.import_job_store.logger")
    @patch("coa_metrics.api.import_job_store._get_table")
    def test_finalize_job_offset_propagates_and_logs_read_timeout_error(self, mock_table, mock_logger):
        from botocore.exceptions import ReadTimeoutError
        from coa_metrics.api.import_job_store import finalize_job_offset

        error = ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
        table = MagicMock()
        table.update_item.side_effect = error
        mock_table.return_value = table

        with pytest.raises(ReadTimeoutError) as raised:
            finalize_job_offset(
                "ns-1",
                "j-1",
                offset=10,
                claim_token="claim-token",
                metrics_processed=1,
                metrics_created=1,
                metrics_updated=0,
            )

        assert raised.value is error
        mock_logger.exception.assert_called_once_with(
            "import_offset_finalize_dynamodb_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=10,
            error_code="ReadTimeoutError",
        )


# ── get_import_job handler tests ────────────────────────────────────────


class TestGetImportJobHandler:
    """Tests for the GET /import-jobs/{jobId} handler."""

    @patch("coa_metrics.api.get_import_job.get_job")
    def test_returns_job(self, mock_get_job):
        from coa_metrics.api.get_import_job import handler

        mock_get_job.return_value = {
            "jobId": "j-1",
            "status": "IN_PROGRESS",
            "metricsTotal": 100,
            "metricsProcessed": 45,
            "metricsCreated": 30,
            "metricsUpdated": 15,
            "errors": [],
            "warnings": [],
        }

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": "j-1"},
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["jobId"] == "j-1"
        assert body["status"] == "IN_PROGRESS"
        assert body["metricsProcessed"] == 45

    @patch("coa_metrics.api.get_import_job.get_job")
    def test_returns_404_when_not_found(self, mock_get_job):
        from coa_metrics.api.get_import_job import handler

        mock_get_job.return_value = None

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": "j-missing"},
        }
        result = handler(event, None)

        assert result["statusCode"] == 404

    def test_returns_400_when_no_job_id(self):
        from coa_metrics.api.get_import_job import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": ""},
        }
        result = handler(event, None)

        assert result["statusCode"] == 400


# ── import_osi async decision tests ─────────────────────────────────────


class TestImportOsiAsyncDecision:
    """Tests for the sync/async threshold in import_osi handler."""

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._read_from_s3")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_large_import_returns_202(self, mock_parse, mock_s3_read, mock_sqs):
        from coa_metrics.api.import_osi import StagedImportSource, handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_read.return_value = "yaml content"
        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        with (
            patch(
                "coa_metrics.api.import_osi._write_to_s3",
                return_value=StagedImportSource(
                    key="ns-1/imports/jobs/staged.yaml",
                    version_id="source-version",
                ),
            ),
            patch("coa_metrics.api.import_job_store.create_job") as mock_create,
        ):
            mock_create.return_value = {"jobId": "j-async", "status": "IN_PROGRESS"}

            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"s3Key": "ns-1/imports/big-file.yaml"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["jobId"] == "j-async"
        assert body["status"] == "IN_PROGRESS"
        mock_create.assert_called_once_with(
            namespace_id="ns-1",
            s3_key="ns-1/imports/jobs/staged.yaml",
            metrics_total=50,
            source_version_id="source-version",
        )
        send_kwargs = mock_sqs_client.send_message.call_args[1]
        assert json.loads(send_kwargs["MessageBody"])["s3Key"] == "ns-1/imports/jobs/staged.yaml"
        assert send_kwargs["MessageAttributes"] == {
            "namespaceId": {"DataType": "String", "StringValue": "ns-1"},
            "jobId": {"DataType": "String", "StringValue": "j-async"},
        }

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._read_from_s3")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_enqueue_failure_returns_500_and_fails_job(self, mock_parse, mock_s3_read, mock_sqs):
        from coa_metrics.api.import_osi import StagedImportSource, handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_read.return_value = "yaml content"
        mock_sqs.return_value.send_message.side_effect = Exception("sqs down")

        with (
            patch(
                "coa_metrics.api.import_osi._write_to_s3",
                return_value=StagedImportSource(
                    key="ns-1/imports/jobs/staged.yaml",
                    version_id="source-version",
                ),
            ),
            patch("coa_metrics.api.import_job_store.create_job") as mock_create,
            patch("coa_metrics.api.import_job_store.complete_job") as mock_complete,
        ):
            mock_create.return_value = {"jobId": "j-async", "status": "IN_PROGRESS"}
            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"s3Key": "ns-1/imports/big.yaml"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 500
        mock_create.assert_called_once_with(
            namespace_id="ns-1",
            s3_key="ns-1/imports/jobs/staged.yaml",
            metrics_total=50,
            source_version_id="source-version",
        )
        mock_complete.assert_called_once_with("ns-1", "j-async", status="FAILED")

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_inline_content_above_threshold_writes_s3_and_returns_202(self, mock_parse, mock_bucket, mock_s3, mock_sqs):
        """Inline content with >30 metrics is staged to S3 and processed async."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_client = MagicMock()
        mock_s3_client.put_object.return_value = {"VersionId": "source-version"}
        mock_s3.return_value = mock_s3_client
        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        with patch("coa_metrics.api.import_job_store.create_job") as mock_create:
            mock_create.return_value = {"jobId": "j-inline-async", "status": "IN_PROGRESS"}

            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"content": "metrics:\n" + "  - name: m\n" * 50}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["jobId"] == "j-inline-async"
        assert body["status"] == "IN_PROGRESS"
        put_call = mock_s3_client.put_object.call_args
        assert put_call[1]["Bucket"] == "test-bucket"
        assert put_call[1]["Key"].startswith("ns-1/imports/jobs/")
        mock_create.assert_called_once_with(
            namespace_id="ns-1",
            s3_key=put_call[1]["Key"],
            metrics_total=50,
            source_version_id="source-version",
        )
        mock_sqs_client.send_message.assert_called_once()

    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_inline_s3_write_failure_returns_500(self, mock_parse, mock_bucket, mock_s3):
        """If S3 write fails for inline async, return 500 instead of processing sync."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3.return_value.put_object.side_effect = Exception("S3 throttled")

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"content": "metrics:\n" + "  - name: m\n" * 50}),
            "headers": {},
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "stage content" in body["message"].lower()

    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_empty_import_processes_sync(self, mock_parse, mock_os, mock_neptune):
        """An import with no metrics stays synchronous."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = []
        mock_doc.datasets = []
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"content": "metrics: []"}),
            "headers": {},
        }
        result = handler(event, None)

        assert result["statusCode"] == 200

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_single_metric_import_is_async(self, mock_parse, mock_bucket, mock_s3, mock_sqs):
        """Even a one-metric import goes async."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock()]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result
        mock_s3_client = MagicMock()
        mock_s3_client.put_object.return_value = {"VersionId": "source-version"}
        mock_s3.return_value = mock_s3_client

        with patch("coa_metrics.api.import_job_store.create_job") as mock_create:
            mock_create.return_value = {"jobId": "j-single", "status": "IN_PROGRESS"}
            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"content": "metrics:\n  - name: m\n"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        assert json.loads(result["body"])["jobId"] == "j-single"
        staged_key = mock_s3_client.put_object.call_args.kwargs["Key"]
        mock_create.assert_called_once_with(
            namespace_id="ns-1",
            s3_key=staged_key,
            metrics_total=1,
            source_version_id="source-version",
        )


# ── DLQ recovery tests ──────────────────────────────────────────────────


class TestImportDlqHandler:
    """Tests for bounded automatic redrive and terminal job failure."""

    @staticmethod
    def _event(
        redrive_count: int | None = None,
        *,
        body_namespace: str = "ns-1",
        body_job: str = "j-1",
        attribute_namespace: str = "ns-1",
        attribute_job: str = "j-1",
        include_attributes: bool = True,
    ) -> dict:
        message = {
            "namespaceId": body_namespace,
            "jobId": body_job,
            "s3Key": f"{body_namespace}/imports/jobs/source.yaml",
            "offset": 50,
            "chunkSize": 50,
        }
        if redrive_count is not None:
            message["automaticRedriveCount"] = redrive_count
        record = {"body": json.dumps(message)}
        if include_attributes:
            record["messageAttributes"] = {
                "namespaceId": {"dataType": "String", "stringValue": attribute_namespace},
                "jobId": {"dataType": "String", "stringValue": attribute_job},
            }
        return {"Records": [record]}

    @staticmethod
    def _active_job(**overrides) -> dict:
        job = {
            "status": "IN_PROGRESS",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "metricsTotal": 100,
            "metricsProcessed": 50,
            "nextOffset": 50,
            "chunkSize": 50,
            "processedOffsets": [0],
        }
        job.update(overrides)
        return job

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_first_dlq_delivery_is_redriven_once(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        mock_get_job.return_value = TestImportDlqHandler._active_job()

        handler(self._event(), None)

        kwargs = mock_sqs.return_value.send_message.call_args[1]
        assert kwargs["DelaySeconds"] == 60
        assert json.loads(kwargs["MessageBody"])["automaticRedriveCount"] == 1
        assert kwargs["MessageAttributes"] == {
            "namespaceId": {"DataType": "String", "StringValue": "ns-1"},
            "jobId": {"DataType": "String", "StringValue": "j-1"},
        }
        mock_fail_job.assert_not_called()

    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=True)
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_second_dlq_delivery_fails_job(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        mock_get_job.return_value = TestImportDlqHandler._active_job()

        handler(self._event(redrive_count=1), None)

        mock_sqs.assert_not_called()
        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "Import failed after exhausting queue retries and automatic recovery",
            require_idle=True,
            expected_next_offset=50,
        )

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_terminal_job_is_not_redriven(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        mock_get_job.return_value = {"status": "COMPLETED"}

        handler(self._event(), None)

        mock_sqs.assert_not_called()
        mock_fail_job.assert_not_called()

    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_legacy_body_only_message_is_redriven(self, mock_get_job, mock_sqs):
        from coa_metrics.api.import_dlq_handler import handler

        mock_get_job.return_value = TestImportDlqHandler._active_job()

        handler(self._event(include_attributes=False), None)

        mock_sqs.return_value.send_message.assert_called_once()

    @pytest.mark.parametrize("redrive_count", [None, 1], ids=["first-delivery", "exhausted"])
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=True)
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_progressed_pre_upgrade_job_is_failed_for_safe_rerun(
        self,
        mock_get_job,
        mock_sqs,
        mock_fail_job,
        redrive_count,
    ):
        from coa_metrics.api.import_dlq_handler import handler
        from coa_metrics.api.import_job_store import UNVERIFIABLE_IMPORT_PROGRESS_ERROR

        mock_get_job.return_value = {
            "status": "IN_PROGRESS",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "metricsTotal": 100,
            "metricsProcessed": 50,
        }

        handler(self._event(redrive_count=redrive_count), None)

        mock_sqs.assert_not_called()
        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
            require_idle=True,
            expected_next_offset=50,
        )

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler.logger")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_redrive_send_failure_leaves_dlq_message_for_retry(
        self,
        mock_get_job,
        mock_sqs,
        mock_logger,
        mock_fail_job,
    ):
        from coa_metrics.api.import_dlq_handler import handler

        error = RuntimeError("SQS unavailable")
        mock_get_job.return_value = TestImportDlqHandler._active_job()
        mock_sqs.return_value.send_message.side_effect = error

        with pytest.raises(RuntimeError) as raised:
            handler(self._event(), None)

        assert raised.value is error
        mock_sqs.return_value.send_message.assert_called_once()
        mock_logger.exception.assert_called_once_with(
            "import_dlq_redrive_send_failed",
            namespace="ns-1",
            job_id="j-1",
            offset=50,
            error="SQS unavailable",
        )
        mock_fail_job.assert_not_called()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("offset", -1),
            ("chunkSize", 0),
            ("automaticRedriveCount", True),
            ("s3Key", "other-ns/imports/abc/file.yaml"),
        ],
    )
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=True)
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_recoverable_invalid_message_fails_active_job(
        self,
        mock_get_job,
        mock_sqs,
        mock_fail_job,
        field: str,
        value: object,
    ):
        from coa_metrics.api.import_dlq_handler import handler

        event = self._event()
        message = json.loads(event["Records"][0]["body"])
        message[field] = value
        event["Records"][0]["body"] = json.dumps(message)

        handler(event, None)

        mock_get_job.assert_not_called()
        mock_sqs.assert_not_called()
        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )

    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=True)
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_malformed_body_uses_message_attributes_to_fail_job(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        event = {
            "Records": [
                {
                    "body": "not-json",
                    "messageAttributes": {
                        "namespaceId": {"dataType": "String", "stringValue": "ns-1"},
                        "jobId": {"dataType": "String", "stringValue": "j-1"},
                    },
                }
            ]
        }

        handler(event, None)

        mock_get_job.assert_not_called()
        mock_sqs.assert_not_called()
        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )

    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=True)
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_identity_mismatch_is_attributed_to_trusted_attributes(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        event = self._event(
            body_namespace="body-ns",
            body_job="body-job",
            attribute_namespace="trusted-ns",
            attribute_job="trusted-job",
        )

        handler(event, None)

        mock_get_job.assert_not_called()
        mock_sqs.assert_not_called()
        mock_fail_job.assert_called_once_with(
            "trusted-ns",
            "trusted-job",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )

    @patch("coa_metrics.api.import_dlq_handler.get_job", return_value={"status": "IN_PROGRESS"})
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=False)
    def test_fail_attributed_job_retries_when_conditional_failure_leaves_job_active(
        self,
        mock_fail_job,
        mock_get_job,
    ):
        from coa_metrics.api.import_dlq_handler import ActiveImportOffsetError, _fail_attributed_job
        from coa_metrics.api.import_queue_message import ImportMessageIdentity

        identity = ImportMessageIdentity(namespaceId="ns-1", jobId="j-1")

        with pytest.raises(ActiveImportOffsetError, match="offset lease is active"):
            _fail_attributed_job(identity, "recovery failed")

        mock_fail_job.assert_called_once_with("ns-1", "j-1", "recovery failed", require_idle=True)
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)

    @pytest.mark.parametrize("observed_job", [{"status": "COMPLETED"}, {"status": "FAILED"}, None])
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=False)
    def test_fail_attributed_job_consumes_terminal_or_missing_job(
        self,
        mock_fail_job,
        mock_get_job,
        observed_job,
    ):
        from coa_metrics.api.import_dlq_handler import _fail_attributed_job
        from coa_metrics.api.import_queue_message import ImportMessageIdentity

        identity = ImportMessageIdentity(namespaceId="ns-1", jobId="j-1")
        mock_get_job.return_value = observed_job

        _fail_attributed_job(identity, "recovery failed")

        mock_fail_job.assert_called_once_with("ns-1", "j-1", "recovery failed", require_idle=True)
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_active_offset_lease_retries_dlq_message(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import ActiveImportOffsetError, handler

        mock_get_job.return_value = self._active_job(
            claimOffset=50,
            claimLeaseExpiresAt=4_102_444_800,
        )

        with pytest.raises(ActiveImportOffsetError, match="offset lease is active"):
            handler(self._event(), None)

        mock_sqs.assert_not_called()
        mock_fail_job.assert_not_called()

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_stale_exhausted_delivery_does_not_fail_advanced_job_with_live_claim(
        self,
        mock_get_job,
        mock_sqs,
        mock_fail_job,
    ):
        from coa_metrics.api.import_dlq_handler import handler

        event = self._event(redrive_count=1)
        message = json.loads(event["Records"][0]["body"])
        message["offset"] = 0
        event["Records"][0]["body"] = json.dumps(message)
        mock_get_job.return_value = self._active_job(
            metricsTotal=150,
            metricsProcessed=100,
            nextOffset=100,
            processedOffsets=[0, 50],
            claimOffset=100,
            claimLeaseExpiresAt=4_102_444_800,
        )

        handler(event, None)

        mock_sqs.assert_not_called()
        mock_fail_job.assert_not_called()

    @patch("coa_metrics.api.import_dlq_handler.get_job")
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=False)
    def test_exhausted_failure_cas_consumes_job_that_advanced_during_race(
        self,
        mock_fail_job,
        mock_get_job,
    ):
        from coa_metrics.api.import_dlq_handler import _fail_attributed_job
        from coa_metrics.api.import_queue_message import ImportMessageIdentity

        identity = ImportMessageIdentity(namespaceId="ns-1", jobId="j-1")
        mock_get_job.return_value = self._active_job(
            metricsTotal=150,
            metricsProcessed=100,
            nextOffset=100,
            processedOffsets=[0, 50],
        )

        _fail_attributed_job(identity, "recovery failed", expected_next_offset=50)

        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "recovery failed",
            require_idle=True,
            expected_next_offset=50,
        )
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)

    @patch("coa_metrics.api.import_dlq_handler.get_job")
    @patch("coa_metrics.api.import_dlq_handler.fail_job", return_value=False)
    def test_progressed_pre_upgrade_failure_retries_when_frontier_is_still_active(
        self,
        mock_fail_job,
        mock_get_job,
    ):
        from coa_metrics.api.import_dlq_handler import ActiveImportOffsetError, _fail_attributed_job
        from coa_metrics.api.import_queue_message import ImportMessageIdentity

        identity = ImportMessageIdentity(namespaceId="ns-1", jobId="j-1")
        mock_get_job.return_value = {
            "status": "IN_PROGRESS",
            "metricsTotal": 100,
            "metricsProcessed": 50,
        }

        with pytest.raises(ActiveImportOffsetError, match="relevant frontier"):
            _fail_attributed_job(identity, "rerun required", expected_next_offset=50)

        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "rerun required",
            require_idle=True,
            expected_next_offset=50,
        )
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)

    @patch("coa_metrics.api.import_dlq_handler.fail_job")
    @patch("coa_metrics.api.import_dlq_handler._get_sqs")
    @patch("coa_metrics.api.import_dlq_handler.get_job")
    def test_unidentifiable_invalid_message_is_consumed(self, mock_get_job, mock_sqs, mock_fail_job):
        from coa_metrics.api.import_dlq_handler import handler

        handler({"Records": [{"body": "not-json"}]}, None)

        mock_get_job.assert_not_called()
        mock_sqs.assert_not_called()
        mock_fail_job.assert_not_called()


# ── import worker tests ─────────────────────────────────────────────────


class TestImportWorker:
    """Tests for the SQS import worker."""

    @staticmethod
    def _msg(**overrides):
        message = {
            "namespaceId": "ns-1",
            "jobId": "j-1",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "offset": 0,
            "chunkSize": 50,
        }
        message.update(overrides)
        return message

    @staticmethod
    def _active_job(metrics_total: int, **overrides) -> dict:
        job = {
            "status": "IN_PROGRESS",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "s3VersionId": "source-version",
            "metricsTotal": metrics_total,
            "metricsProcessed": 0,
            "nextOffset": 0,
            "processedOffsets": [],
        }
        job.update(overrides)
        return job

    @classmethod
    def _event(
        cls,
        *,
        body_overrides: dict | None = None,
        attribute_namespace: str = "ns-1",
        attribute_job: str = "j-1",
    ) -> dict:
        message = cls._msg(**(body_overrides or {}))
        return {
            "Records": [
                {
                    "body": json.dumps(message),
                    "messageAttributes": {
                        "namespaceId": {"dataType": "String", "stringValue": attribute_namespace},
                        "jobId": {"dataType": "String", "stringValue": attribute_job},
                    },
                }
            ]
        }

    @pytest.fixture(autouse=True)
    def mock_store_job_offset_plan(self):
        """Keep mock-based worker tests focused while exposing the v2 checkpoint boundary."""
        with patch("coa_metrics.api.import_worker._checkpoint_offset_plan", return_value=True) as mock_store:
            yield mock_store

    @staticmethod
    def _mock_document(mock_s3, mock_parse, metric_count: int, *, datasets: list | None = None):
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        document = MagicMock()
        document.datasets = datasets or []
        document.metrics = []
        for index in range(metric_count):
            metric = MagicMock()
            metric.name = f"metric_{index}"
            document.metrics.append(metric)
        mock_parse.return_value = MagicMock(success=True, document=document)
        return document

    @staticmethod
    def _mock_metric_definition(name: str, *, ontology_concepts: list[str] | None = None):
        from coa_metrics.neptune_client import MetricDefinition, MetricDialect

        return MetricDefinition(
            name=name,
            description=f"Definition for {name}",
            expression_dialects=[MetricDialect(dialect="trino", expression=f"SELECT '{name}'")],
            data_source_id="source-1",
            source_table="orders",
            ontology_concepts=list(ontology_concepts or []),
            defined_by="import-worker",
            effective_from="2026-08-24",
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("chunkSize", 0),
            ("offset", -1),
            ("s3Key", "other-ns/imports/abc/poison.yaml"),
        ],
    )
    @patch("coa_metrics.api.import_worker._process_chunk")
    @patch("coa_metrics.api.import_worker.get_job")
    @patch("coa_metrics.api.import_worker.fail_job", return_value=True)
    def test_handler_invalid_message_is_attributed_to_trusted_attributes(
        self,
        mock_fail_job,
        mock_get_job,
        mock_process_chunk,
        field,
        value,
    ):
        from coa_metrics.api.import_worker import handler

        handler(self._event(body_overrides={field: value}), None)

        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )
        mock_get_job.assert_not_called()
        mock_process_chunk.assert_not_called()

    @patch("coa_metrics.api.import_worker._process_chunk")
    @patch("coa_metrics.api.import_worker.get_job")
    @patch("coa_metrics.api.import_worker.fail_job", return_value=True)
    def test_handler_identity_mismatch_is_attributed_to_trusted_attributes(
        self,
        mock_fail_job,
        mock_get_job,
        mock_process_chunk,
    ):
        from coa_metrics.api.import_worker import handler

        handler(
            self._event(
                body_overrides={
                    "namespaceId": "body-ns",
                    "jobId": "body-job",
                    "s3Key": "body-ns/imports/abc/file.yaml",
                },
                attribute_namespace="trusted-ns",
                attribute_job="trusted-job",
            ),
            None,
        )

        mock_fail_job.assert_called_once_with(
            "trusted-ns",
            "trusted-job",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )
        mock_get_job.assert_not_called()
        mock_process_chunk.assert_not_called()

    @patch("coa_metrics.api.import_worker._process_chunk")
    def test_handler_valid_message_reaches_chunk_processor(self, mock_process_chunk):
        from coa_metrics.api.import_worker import handler

        handler(self._event(), None)

        message = mock_process_chunk.call_args.args[0]
        assert message.namespace_id == "ns-1"
        assert message.job_id == "j-1"
        assert message.offset == 0
        assert message.chunk_size == 50

    @patch("coa_metrics.api.import_worker._process_chunk")
    @patch("coa_metrics.api.import_worker.get_job")
    @patch("coa_metrics.api.import_worker.fail_job", return_value=False)
    def test_handler_active_lease_keeps_invalid_message_retryable(
        self,
        mock_fail_job,
        mock_get_job,
        mock_process_chunk,
    ):
        from coa_metrics.api.import_worker import OffsetLeaseHeldError, handler

        mock_get_job.return_value = TestImportDlqHandler._active_job()

        with pytest.raises(OffsetLeaseHeldError, match="offset lease is active"):
            handler(self._event(body_overrides={"chunkSize": 0}), None)

        mock_fail_job.assert_called_once()
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)
        mock_process_chunk.assert_not_called()

    @pytest.mark.parametrize("stored_s3_key", [None, 7, "ns-1/imports/jobs/other.yaml"])
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.fail_job", return_value=True)
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_key_mismatch_fails_closed_before_claim(
        self,
        mock_get_job,
        mock_fail_job,
        mock_claim,
        stored_s3_key,
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1, s3Key=stored_s3_key)

        _process_chunk(self._msg())

        mock_fail_job.assert_called_once_with(
            "ns-1",
            "j-1",
            "Import recovery failed because its queue message was invalid",
            require_idle=True,
        )
        mock_claim.assert_not_called()

    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.fail_job", return_value=False)
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_key_mismatch_with_active_lease_remains_retryable(
        self,
        mock_get_job,
        mock_fail_job,
        mock_claim,
    ):
        from coa_metrics.api.import_worker import OffsetLeaseHeldError, _process_chunk

        mock_get_job.side_effect = [
            self._active_job(1, s3Key="ns-1/imports/jobs/other.yaml"),
            {"status": "IN_PROGRESS"},
        ]

        with pytest.raises(OffsetLeaseHeldError, match="mismatched queue message"):
            _process_chunk(self._msg())

        mock_fail_job.assert_called_once()
        assert mock_get_job.call_count == 2
        mock_claim.assert_not_called()

    @patch("coa_metrics.api.import_worker._get_lookup")
    @patch("coa_metrics.api.import_worker.resolve_datasets")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.fail_claimed_job", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_dataset_resolution_failure_marks_failed(
        self,
        mock_get_job,
        mock_claim,
        mock_fail_claimed_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_resolve,
        mock_lookup,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        self._mock_document(mock_s3, mock_parse, 1, datasets=[MagicMock()])
        mock_resolve.return_value = MagicMock(success=False, errors=["bad"])

        _process_chunk(self._msg())

        mock_fail_claimed_job.assert_called_once_with(
            "ns-1",
            "j-1",
            offset=0,
            claim_token="claim-token",
            error="dataset resolution failed: ['bad']",
        )
        mock_complete.assert_not_called()

    def test_source_metric_count_mismatch_fails_owned_job_before_neptune(
        self,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        with (
            patch("coa_metrics.api.import_worker.get_job", return_value=self._active_job(2)),
            patch(
                "coa_metrics.api.import_worker.claim_job_offset",
                return_value=OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token"),
            ),
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch("coa_metrics.api.import_worker.fail_claimed_job", return_value=True) as mock_fail,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
        ):
            self._mock_document(mock_s3, mock_parse, 1)

            _process_chunk(self._msg())

        mock_fail.assert_called_once_with(
            "ns-1",
            "j-1",
            offset=0,
            claim_token="claim-token",
            error="pinned import source contains 1 metrics, expected 2",
        )
        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_final_chunk_atomically_completes_with_delta_counts(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(2)
        self._mock_document(mock_s3, mock_parse, 2)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.side_effect = lambda metric, *_args: self._mock_metric_definition(metric.name)

        def checkpoint_before_writes(*_args, **_kwargs):
            mock_neptune.return_value.create_metric.assert_not_called()
            mock_neptune.return_value.update_metric.assert_not_called()
            return True

        def create_after_checkpoint(*_args, **_kwargs):
            assert mock_store_job_offset_plan.call_count == 1

        mock_store_job_offset_plan.side_effect = checkpoint_before_writes
        mock_neptune.return_value.create_metric.side_effect = create_after_checkpoint

        with patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs:
            _process_chunk(self._msg(chunkSize=50))

        assert mock_claim.call_args.args == ("ns-1", "j-1")
        assert mock_claim.call_args.kwargs["offset"] == 0
        assert mock_claim.call_args.kwargs["end_offset"] == 2
        assert mock_claim.call_args.kwargs["chunk_size"] == 50
        assert mock_claim.call_args.kwargs["lease_seconds"] > 0
        mock_store_job_offset_plan.assert_called_once()
        checkpoint = mock_store_job_offset_plan.call_args
        assert checkpoint.args == ("ns-1", "j-1")
        assert checkpoint.kwargs["offset"] == 0
        assert checkpoint.kwargs["claim_token"] == "claim-token"
        assert [entry.name for entry in checkpoint.kwargs["plan"].entries] == ["metric_0", "metric_1"]
        assert [entry.disposition for entry in checkpoint.kwargs["plan"].entries] == [
            MetricDisposition.CREATE,
            MetricDisposition.CREATE,
        ]
        mock_finalize.assert_called_once_with(
            "ns-1",
            "j-1",
            offset=0,
            claim_token="claim-token",
            metrics_processed=2,
            metrics_created=2,
            metrics_updated=0,
            errors=None,
            warnings=None,
            mark_job_completed=True,
        )
        mock_complete.assert_not_called()
        mock_sqs.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_unapproved_source_is_recorded_and_not_persisted(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        document = self._mock_document(mock_s3, mock_parse, 1)
        document.metrics[0].name = "blocked_metric"
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        definition = self._mock_metric_definition("blocked_metric")
        definition.data_source_id = "ds-pending"
        mock_to_def.return_value = definition

        with (
            patch(
                "coa_metrics.api.import_worker.check_source_approved",
                return_value="Data source 'ds-pending' has status 'PENDING'",
            ) as mock_approved,
            patch("coa_metrics.api.import_worker.check_source_table_exists") as mock_table,
        ):
            _process_chunk(self._msg())

        mock_approved.assert_called_once_with("ns-1", "ds-pending")
        mock_table.assert_not_called()
        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        plan = mock_store_job_offset_plan.call_args.kwargs["plan"]
        assert plan.entries[0].disposition == MetricDisposition.ERROR
        assert "PENDING" in plan.entries[0].error
        assert mock_finalize.call_args.kwargs["metrics_processed"] == 1
        assert mock_finalize.call_args.kwargs["metrics_created"] == 0
        assert "PENDING" in mock_finalize.call_args.kwargs["errors"][0]
        assert mock_finalize.call_args.kwargs["mark_job_completed"] is True
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_table_is_checked_before_persistence(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        document = self._mock_document(mock_s3, mock_parse, 1)
        document.metrics[0].name = "bad_table_metric"
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        definition = self._mock_metric_definition("bad_table_metric")
        definition.data_source_id = "ds-approved"
        definition.source_table = "missing_table"
        mock_to_def.return_value = definition

        with (
            patch("coa_metrics.api.import_worker.check_source_approved", return_value=None),
            patch(
                "coa_metrics.api.import_worker.check_source_table_exists",
                return_value="Source table 'missing_table' not found",
            ) as mock_table,
        ):
            _process_chunk(self._msg())

        mock_table.assert_called_once_with("ns-1", "ds-approved", "missing_table")
        mock_neptune.return_value.create_metric.assert_not_called()
        plan = mock_store_job_offset_plan.call_args.kwargs["plan"]
        assert plan.entries[0].disposition == MetricDisposition.ERROR
        assert "missing_table" in plan.entries[0].error
        assert mock_finalize.call_args.kwargs["metrics_created"] == 0
        assert "missing_table" in mock_finalize.call_args.kwargs["errors"][0]
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_duplicate_source_references_are_validated_once_per_chunk(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(2)
        document = self._mock_document(mock_s3, mock_parse, 2)
        document.metrics[0].name = "one"
        document.metrics[1].name = "two"
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        first = self._mock_metric_definition("one")
        first.data_source_id = "ds-approved"
        first.source_table = "Orders"
        second = self._mock_metric_definition("two")
        second.data_source_id = "ds-approved"
        second.source_table = "orders"
        mock_to_def.side_effect = [first, second]
        mock_neptune.return_value.get_metric.return_value = None

        with (
            patch("coa_metrics.api.import_worker.check_source_approved", return_value=None) as mock_approved,
            patch("coa_metrics.api.import_worker.check_source_table_exists", return_value=None) as mock_table,
        ):
            _process_chunk(self._msg())

        mock_approved.assert_called_once_with("ns-1", "ds-approved")
        mock_table.assert_called_once_with("ns-1", "ds-approved", "Orders")
        assert mock_neptune.return_value.create_metric.call_count == 2
        assert mock_finalize.call_args.kwargs["metrics_created"] == 2
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.fail_claimed_job", return_value=True)
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_validation_outage_fails_job_before_checkpoint_or_write(
        self,
        mock_get_job,
        mock_fail_claimed,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        document = self._mock_document(mock_s3, mock_parse, 1)
        document.metrics[0].name = "metric"
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        definition = self._mock_metric_definition("metric")
        definition.data_source_id = "ds-approved"
        mock_to_def.return_value = definition

        with patch(
            "coa_metrics.api.import_worker.check_source_approved",
            side_effect=SourceValidationUnavailableError("sources table unavailable"),
        ):
            _process_chunk(self._msg())

        mock_fail_claimed.assert_called_once_with(
            "ns-1",
            "j-1",
            offset=0,
            claim_token="claim-token",
            error="sources table unavailable",
        )
        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_finalize.assert_not_called()
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_existing_metric_adds_overwrite_warning(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_to_def,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        document = self._mock_document(mock_s3, mock_parse, 1)
        document.metrics[0].name = "simple_count"
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        metric_definition = self._mock_metric_definition("simple_count")
        mock_to_def.return_value = metric_definition
        mock_neptune.return_value.get_metric.return_value = MagicMock(defined_by="steward@example.com")

        _process_chunk(self._msg())

        plan = mock_store_job_offset_plan.call_args.kwargs["plan"]
        assert plan.entries[0].disposition == MetricDisposition.UPDATE
        assert plan.entries[0].warning == (
            "Metric 'simple_count' overwritten (had existing metadata authored by steward@example.com)"
        )
        kwargs = mock_finalize.call_args.kwargs
        assert kwargs["metrics_updated"] == 1
        assert kwargs["warnings"] == [
            "Metric 'simple_count' overwritten (had existing metadata authored by steward@example.com)"
        ]
        assert kwargs["mark_job_completed"] is True
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_nonfinal_chunk_records_progress_then_sends_actual_next_offset(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
        mock_to_def,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(
            5,
            metricsProcessed=2,
            nextOffset=2,
            chunkSize=2,
            processedOffsets=[0],
        )
        self._mock_document(mock_s3, mock_parse, 5)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.side_effect = lambda metric, *_args: self._mock_metric_definition(metric.name)

        _process_chunk(self._msg(offset=2, chunkSize=2))

        mock_finalize.assert_called_once_with(
            "ns-1",
            "j-1",
            offset=2,
            claim_token="claim-token",
            metrics_processed=2,
            metrics_created=2,
            metrics_updated=0,
            errors=None,
            warnings=None,
            mark_job_completed=False,
        )
        continuation = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
        assert continuation["offset"] == 4
        assert continuation["chunkSize"] == 2
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_continuation_send_failure_propagates_without_terminalizing_job(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
        mock_to_def,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(3)
        self._mock_document(mock_s3, mock_parse, 3)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.side_effect = lambda metric, *_args: self._mock_metric_definition(metric.name)
        mock_sqs.return_value.send_message.side_effect = RuntimeError("sqs down")

        with pytest.raises(RuntimeError, match="sqs down"):
            _process_chunk(self._msg(chunkSize=2))

        mock_finalize.assert_called_once()
        assert mock_finalize.call_args.kwargs["mark_job_completed"] is False
        send_kwargs = mock_sqs.return_value.send_message.call_args[1]
        assert json.loads(send_kwargs["MessageBody"])["offset"] == 2
        assert send_kwargs["MessageAttributes"] == {
            "namespaceId": {"DataType": "String", "StringValue": "ns-1"},
            "jobId": {"DataType": "String", "StringValue": "j-1"},
        }
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_already_processed_chunk_resumes_continuation_without_neptune_calls(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(3)
        self._mock_document(mock_s3, mock_parse, 3)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ALREADY_PROCESSED)

        _process_chunk(self._msg(chunkSize=2))

        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_complete.assert_not_called()
        body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
        assert body["offset"] == 2
        assert body["chunkSize"] == 2

    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_legacy_already_processed_final_chunk_resumes_completion_without_neptune_calls(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(2)
        mock_complete.return_value = True
        self._mock_document(mock_s3, mock_parse, 2)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ALREADY_PROCESSED)

        _process_chunk(self._msg(chunkSize=2))

        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()
        mock_complete.assert_called_once_with(
            "ns-1",
            "j-1",
            status="COMPLETED",
            expected_next_offset=2,
        )

    @pytest.mark.parametrize("observed_status", ["COMPLETED", "FAILED"])
    @patch("coa_metrics.api.import_worker.logger")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker.complete_job", return_value=False)
    @patch("coa_metrics.api.import_worker.get_job")
    def test_legacy_final_completion_condition_loss_consumes_terminal_job(
        self,
        mock_get_job,
        mock_complete,
        mock_sqs,
        mock_logger,
        observed_status,
    ):
        from coa_metrics.api.import_queue_message import ImportQueueMessage
        from coa_metrics.api.import_worker import _resume_post_accounting

        message = ImportQueueMessage.model_validate(self._msg(chunkSize=2))
        mock_get_job.return_value = {"status": observed_status}

        _resume_post_accounting(message, metrics_total=2, end_offset=2)

        mock_complete.assert_called_once_with(
            "ns-1",
            "j-1",
            status="COMPLETED",
            expected_next_offset=2,
        )
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)
        mock_sqs.assert_not_called()
        mock_logger.warning.assert_called_once_with(
            "import_legacy_completion_state_observed",
            namespace="ns-1",
            job_id="j-1",
            offset=0,
            observed_status=observed_status,
        )

    @patch("coa_metrics.api.import_worker.logger")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker.complete_job", return_value=False)
    @patch("coa_metrics.api.import_worker.get_job", return_value={"status": "IN_PROGRESS"})
    def test_legacy_final_completion_condition_loss_retries_active_job(
        self,
        mock_get_job,
        mock_complete,
        mock_sqs,
        mock_logger,
    ):
        from coa_metrics.api.import_queue_message import ImportQueueMessage
        from coa_metrics.api.import_worker import _resume_post_accounting

        message = ImportQueueMessage.model_validate(self._msg(chunkSize=2))

        with pytest.raises(RuntimeError, match="remained IN_PROGRESS"):
            _resume_post_accounting(message, metrics_total=2, end_offset=2)

        mock_complete.assert_called_once_with(
            "ns-1",
            "j-1",
            status="COMPLETED",
            expected_next_offset=2,
        )
        mock_get_job.assert_called_once_with("ns-1", "j-1", consistent_read=True)
        mock_sqs.assert_not_called()
        mock_logger.warning.assert_called_once_with(
            "import_legacy_completion_state_observed",
            namespace="ns-1",
            job_id="j-1",
            offset=0,
            observed_status="IN_PROGRESS",
        )

    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_lease_held_chunk_raises_retryably_without_neptune_calls(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import OffsetLeaseHeldError, _process_chunk

        mock_get_job.return_value = self._active_job(3)
        self._mock_document(mock_s3, mock_parse, 3)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.LEASE_HELD)

        with pytest.raises(OffsetLeaseHeldError, match="already being processed"):
            _process_chunk(self._msg(chunkSize=2))

        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_inactive_claim_stops_without_neptune_or_transition(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(3)
        self._mock_document(mock_s3, mock_parse, 3)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.NOT_ACTIVE)

        _process_chunk(self._msg(chunkSize=2))

        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()
        mock_complete.assert_not_called()

    @pytest.mark.parametrize("claim_state", ["STALE", "INVALID_OFFSET"])
    def test_noncurrent_interval_is_consumed_without_side_effects(self, claim_state):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        with (
            patch("coa_metrics.api.import_worker.get_job", return_value=self._active_job(3)),
            patch(
                "coa_metrics.api.import_worker.claim_job_offset",
                return_value=OffsetClaim(OffsetClaimState[claim_state]),
            ) as mock_claim,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
        ):
            _process_chunk(self._msg(chunkSize=2))

        mock_claim.assert_called_once()
        mock_s3.assert_not_called()
        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()

    @pytest.mark.parametrize("offset", [0, 50], ids=["old-replay", "old-frontier"])
    def test_progressed_pre_upgrade_job_fails_before_external_io(self, offset, mock_store_job_offset_plan):
        from coa_metrics.api.import_job_store import UNVERIFIABLE_IMPORT_PROGRESS_ERROR
        from coa_metrics.api.import_worker import _process_chunk

        legacy_job = {
            "status": "IN_PROGRESS",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "metricsTotal": 100,
            "metricsProcessed": 50,
        }
        with (
            patch("coa_metrics.api.import_worker.get_job", return_value=legacy_job),
            patch("coa_metrics.api.import_worker.fail_job", return_value=True) as mock_fail,
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
        ):
            _process_chunk(self._msg(offset=offset))

        mock_fail.assert_called_once_with(
            "ns-1",
            "j-1",
            UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
            require_idle=True,
            expected_next_offset=50,
        )
        mock_claim.assert_not_called()
        mock_s3.assert_not_called()
        mock_neptune.assert_not_called()
        mock_store_job_offset_plan.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()

    def test_progressed_pre_upgrade_failure_retries_when_frontier_is_still_active(
        self,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_worker import OffsetLeaseHeldError, _process_chunk

        legacy_job = {
            "status": "IN_PROGRESS",
            "s3Key": "ns-1/imports/jobs/source.yaml",
            "metricsTotal": 100,
            "metricsProcessed": 50,
        }
        with (
            patch("coa_metrics.api.import_worker.get_job", side_effect=[legacy_job, legacy_job]),
            patch("coa_metrics.api.import_worker.fail_job", return_value=False),
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            pytest.raises(OffsetLeaseHeldError, match="frontier is active"),
        ):
            _process_chunk(self._msg(offset=50))

        mock_claim.assert_not_called()
        mock_s3.assert_not_called()
        mock_neptune.assert_not_called()
        mock_store_job_offset_plan.assert_not_called()

    def test_durable_chunk_size_mismatch_is_consumed_before_claim(self):
        from coa_metrics.api.import_worker import _process_chunk

        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(3, chunkSize=50),
            ),
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
        ):
            _process_chunk(self._msg(chunkSize=2))

        mock_claim.assert_not_called()
        mock_s3.assert_not_called()
        mock_neptune.assert_not_called()

    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset")
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_acquired_claim_without_token_stops_before_external_writes_or_transition(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
    ):
        from coa_metrics.api.import_job_store import OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(1)
        self._mock_document(mock_s3, mock_parse, 1)
        malformed_claim = MagicMock()
        malformed_claim.state = OffsetClaimState.ACQUIRED
        malformed_claim.token = None
        mock_claim.return_value = malformed_claim

        with pytest.raises(RuntimeError, match="did not contain a fencing token"):
            _process_chunk(self._msg())

        mock_neptune.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()
        mock_complete.assert_not_called()

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=False)
    @patch("coa_metrics.api.import_worker.claim_job_offset")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_finalize_false_sends_no_continuation_or_completion(
        self,
        mock_get_job,
        mock_complete,
        mock_s3,
        mock_parse,
        mock_claim,
        mock_finalize,
        mock_neptune,
        mock_sqs,
        mock_to_def,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = self._active_job(3)
        self._mock_document(mock_s3, mock_parse, 3)
        mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.side_effect = lambda metric, *_args: self._mock_metric_definition(metric.name)

        _process_chunk(self._msg(chunkSize=2))

        mock_finalize.assert_called_once()
        assert mock_finalize.call_args.kwargs["mark_job_completed"] is False
        mock_sqs.assert_not_called()
        mock_complete.assert_not_called()

    def test_checkpoint_loss_stops_before_neptune_mutation_or_transition(self, mock_store_job_offset_plan):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        mock_store_job_offset_plan.return_value = False
        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker.complete_job") as mock_complete,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)
            mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
            mock_to_definition.return_value = self._mock_metric_definition("metric_0")
            mock_neptune.return_value.get_metric.return_value = None

            _process_chunk(self._msg(chunkSize=1))

        mock_store_job_offset_plan.assert_called_once()
        mock_neptune.return_value.get_metric.assert_called_once_with("ns-1", "metric_0")
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_neptune.return_value.update_metric.assert_not_called()
        mock_finalize.assert_not_called()
        mock_complete.assert_not_called()
        mock_sqs.assert_not_called()

    @pytest.mark.parametrize("operation", ["ontology_resolution", "existence_lookup"])
    def test_planning_infrastructure_failure_propagates_before_checkpoint(
        self,
        operation,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        error = RuntimeError(f"exact {operation} failure")
        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker.complete_job") as mock_complete,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)
            mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
            concepts = ["Revenue"] if operation == "ontology_resolution" else []
            mock_to_definition.return_value = self._mock_metric_definition(
                "metric_0",
                ontology_concepts=concepts,
            )
            if operation == "ontology_resolution":
                mock_neptune.return_value.resolve_class_uris.side_effect = error
            else:
                mock_neptune.return_value.get_metric.side_effect = error

            with pytest.raises(RuntimeError) as raised:
                _process_chunk(self._msg(chunkSize=1))

        assert raised.value is error
        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_neptune.return_value.update_metric.assert_not_called()
        mock_finalize.assert_not_called()
        mock_complete.assert_not_called()
        mock_sqs.assert_not_called()

    def test_neptune_write_failure_propagates_without_finalize_or_continuation(self):
        from coa_metrics.api.import_job_store import OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        error = RuntimeError("exact Neptune write failure")
        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker.complete_job") as mock_complete,
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)
            mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
            mock_to_definition.return_value = self._mock_metric_definition("metric_0")
            mock_neptune.return_value.get_metric.return_value = None
            mock_neptune.return_value.create_metric.side_effect = error

            with pytest.raises(RuntimeError) as raised:
                _process_chunk(self._msg(chunkSize=1))

        assert raised.value is error
        mock_finalize.assert_not_called()
        mock_complete.assert_not_called()
        mock_sqs.assert_not_called()

    def test_conversion_value_error_is_persisted_and_applied_as_durable_error(self, mock_store_job_offset_plan):
        from coa_metrics.api.import_job_store import MetricDisposition, OffsetClaim, OffsetClaimState
        from coa_metrics.api.import_worker import _process_chunk

        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch("coa_metrics.api.import_worker.claim_job_offset") as mock_claim,
            patch("coa_metrics.api.import_worker.finalize_job_offset", return_value=True) as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)
            mock_claim.return_value = OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token")
            mock_to_definition.side_effect = ValueError("data-modifying SQL is not allowed")

            _process_chunk(self._msg(chunkSize=1))

        plan = mock_store_job_offset_plan.call_args.kwargs["plan"]
        assert plan.entries[0].name == "metric_0"
        assert plan.entries[0].disposition == MetricDisposition.ERROR
        assert plan.entries[0].error == "metric_0: data-modifying SQL is not allowed"
        mock_neptune.return_value.resolve_class_uris.assert_not_called()
        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_neptune.return_value.update_metric.assert_not_called()
        assert mock_finalize.call_args.kwargs["metrics_processed"] == 1
        assert mock_finalize.call_args.kwargs["metrics_created"] == 0
        assert mock_finalize.call_args.kwargs["metrics_updated"] == 0
        assert mock_finalize.call_args.kwargs["errors"] == ["metric_0: data-modifying SQL is not allowed"]

    @pytest.mark.parametrize("mismatch", ["offset", "entry_count", "name"])
    def test_recovered_plan_mismatch_fails_closed_before_neptune_io(self, mismatch, mock_store_job_offset_plan):
        from coa_metrics.api.import_job_store import (
            MetricDisposition,
            OffsetClaim,
            OffsetClaimState,
            OffsetPlan,
            OffsetPlanEntry,
        )
        from coa_metrics.api.import_worker import _process_chunk

        entries = (OffsetPlanEntry(name="metric_0", disposition=MetricDisposition.CREATE),)
        plan_offset = 0
        if mismatch == "offset":
            plan_offset = 1
        elif mismatch == "entry_count":
            entries += (OffsetPlanEntry(name="metric_1", disposition=MetricDisposition.CREATE),)
        elif mismatch == "name":
            entries = (OffsetPlanEntry(name="different", disposition=MetricDisposition.CREATE),)
        plan = OffsetPlan(offset=plan_offset, entries=entries)

        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch(
                "coa_metrics.api.import_worker.claim_job_offset",
                return_value=OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token", plan),
            ),
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)

            with pytest.raises(RuntimeError, match="durable offset plan"):
                _process_chunk(self._msg(chunkSize=1))

        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.assert_not_called()
        mock_to_definition.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()

    def test_recovered_valid_entry_conversion_error_is_retryable_without_plan_rewrite(
        self,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import (
            MetricDisposition,
            OffsetClaim,
            OffsetClaimState,
            OffsetPlan,
            OffsetPlanEntry,
        )
        from coa_metrics.api.import_worker import _process_chunk

        plan = OffsetPlan(
            offset=0,
            entries=(OffsetPlanEntry(name="metric_0", disposition=MetricDisposition.CREATE),),
        )
        conversion_error = ValueError("definition changed")
        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch(
                "coa_metrics.api.import_worker.claim_job_offset",
                return_value=OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token", plan),
            ),
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch(
                "coa_metrics.api.import_worker._osi_metric_to_definition",
                side_effect=conversion_error,
            ),
        ):
            self._mock_document(mock_s3, mock_parse, 1)

            with pytest.raises(RuntimeError, match="no longer matches its legacy durable offset plan") as raised:
                _process_chunk(self._msg(chunkSize=1))

        assert raised.value.__cause__ is conversion_error
        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.return_value.resolve_class_uris.assert_not_called()
        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_neptune.return_value.update_metric.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()

    def test_recovered_legacy_plan_propagates_resolver_protocol_value_error(
        self,
        mock_store_job_offset_plan,
    ):
        from coa_metrics.api.import_job_store import (
            MetricDisposition,
            OffsetClaim,
            OffsetClaimState,
            OffsetPlan,
            OffsetPlanEntry,
        )
        from coa_metrics.api.import_worker import _process_chunk

        plan = OffsetPlan(
            offset=0,
            entries=(OffsetPlanEntry(name="metric_0", disposition=MetricDisposition.CREATE),),
        )
        resolver_error = ValueError("malformed Neptune response")
        with (
            patch(
                "coa_metrics.api.import_worker.get_job",
                return_value=self._active_job(1),
            ),
            patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
            patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
            patch(
                "coa_metrics.api.import_worker.claim_job_offset",
                return_value=OffsetClaim(OffsetClaimState.ACQUIRED, "claim-token", plan),
            ),
            patch("coa_metrics.api.import_worker.finalize_job_offset") as mock_finalize,
            patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
            patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        ):
            self._mock_document(mock_s3, mock_parse, 1)
            mock_to_definition.return_value = self._mock_metric_definition(
                "metric_0",
                ontology_concepts=["Revenue"],
            )
            mock_neptune.return_value.resolve_class_uris.side_effect = resolver_error

            with pytest.raises(ValueError) as raised:
                _process_chunk(self._msg(chunkSize=1))

        assert raised.value is resolver_error
        mock_store_job_offset_plan.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        mock_neptune.return_value.update_metric.assert_not_called()
        mock_finalize.assert_not_called()
        mock_sqs.assert_not_called()


def test_claim_and_atomic_finalization_complete_job_against_dynamodb():
    """Prove durable planning, final accounting, and terminal status share fenced updates."""
    import boto3
    from coa_metrics.api.import_job_store import (
        MetricDisposition,
        OffsetClaimState,
        OffsetPlan,
        OffsetPlanEntry,
        _serialize_offset_plan,
        claim_job_offset,
        fail_job,
        finalize_job_offset,
        store_job_offset_plan,
    )
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="import-jobs",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        key = {"PK": "NS#ns-1", "SK": "IMPORT#j-1"}
        table.put_item(
            Item={
                **key,
                "status": "IN_PROGRESS",
                "metricsTotal": 2,
                "processedOffsets": [],
                "metricsProcessed": 0,
                "metricsCreated": 0,
                "metricsUpdated": 0,
            }
        )
        plan = OffsetPlan(
            offset=0,
            entries=(
                OffsetPlanEntry(name="created", disposition=MetricDisposition.CREATE),
                OffsetPlanEntry(
                    name="updated",
                    disposition=MetricDisposition.UPDATE,
                    warning="existing metric overwritten",
                ),
            ),
        )

        with patch("coa_metrics.api.import_job_store._get_table", return_value=table):
            claim = claim_job_offset("ns-1", "j-1", offset=0, end_offset=2, chunk_size=50, lease_seconds=60)
            assert claim.state == OffsetClaimState.ACQUIRED
            assert claim.token is not None
            assert claim.plan is None
            assert store_job_offset_plan(
                "ns-1",
                "j-1",
                offset=0,
                claim_token=claim.token,
                plan=plan,
            )
            planned_item = table.get_item(Key=key, ConsistentRead=True)["Item"]
            assert planned_item["offsetPlan"] == _serialize_offset_plan(plan)

            assert finalize_job_offset(
                "ns-1",
                "j-1",
                offset=0,
                claim_token=claim.token,
                metrics_processed=2,
                metrics_created=1,
                metrics_updated=1,
                mark_job_completed=True,
            )

            item = table.get_item(Key=key, ConsistentRead=True)["Item"]
            assert item["status"] == "COMPLETED"
            assert item["processedOffsets"] == [0]
            assert item["metricsProcessed"] == 2
            assert item["nextOffset"] == 2
            assert item["metricsCreated"] == 1
            assert item["metricsUpdated"] == 1
            assert "offsetPlan" not in item
            assert "claimOffset" not in item
            assert "claimToken" not in item
            assert "claimLeaseExpiresAt" not in item

            assert fail_job("ns-1", "j-1", "late DLQ failure", require_idle=True) is False
            terminal_item = table.get_item(Key=key, ConsistentRead=True)["Item"]
            assert terminal_item["status"] == "COMPLETED"
            assert terminal_item["processedOffsets"] == [0]
            assert terminal_item["metricsProcessed"] == 2
            assert terminal_item["metricsCreated"] == 1
            assert terminal_item["metricsUpdated"] == 1


@pytest.fixture
def moto_import_jobs_table():
    """Provide a real Moto DynamoDB table through the production store accessor."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="import-jobs",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("coa_metrics.api.import_job_store._get_table", return_value=table):
            yield table


def _seed_import_job(table, job_id: str, **overrides):
    key = {"PK": "NS#ns-1", "SK": f"IMPORT#{job_id}"}
    item = {
        **key,
        "jobId": job_id,
        "namespaceId": "ns-1",
        "status": "IN_PROGRESS",
        "s3Key": "ns-1/imports/jobs/source.yaml",
        "s3VersionId": "source-version",
        "metricsTotal": 2,
        "metricsProcessed": 0,
        "nextOffset": 0,
        "chunkSize": 50,
        "metricsCreated": 0,
        "metricsUpdated": 0,
        "processedOffsets": [],
        "errors": [],
        "warnings": [],
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-01T00:00:00+00:00",
    }
    for name, value in overrides.items():
        if value is None:
            item.pop(name, None)
        else:
            item[name] = value
    table.put_item(Item=item)
    return key


def _read_import_job(table, key: dict[str, str]) -> dict:
    return table.get_item(Key=key, ConsistentRead=True)["Item"]


def test_gap_claim_is_rejected_without_writing_a_lease(moto_import_jobs_table):
    from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

    job_id = "j-gap"
    key = _seed_import_job(moto_import_jobs_table, job_id, metricsTotal=100)

    claim = claim_job_offset(
        "ns-1",
        job_id,
        offset=50,
        end_offset=100,
        chunk_size=50,
        lease_seconds=60,
    )

    assert claim.state == OffsetClaimState.INVALID_OFFSET
    item = _read_import_job(moto_import_jobs_table, key)
    assert item["nextOffset"] == 0
    assert item["metricsProcessed"] == 0
    assert "claimOffset" not in item
    assert "claimToken" not in item
    assert "claimLeaseExpiresAt" not in item


def test_expired_claim_replacement_fences_stale_finalize_and_accounts_once(moto_import_jobs_table):
    """A replacement lease recovers the durable plan and owns the only valid accounting token."""
    from coa_metrics.api.import_job_store import (
        MetricDisposition,
        OffsetClaimState,
        OffsetPlan,
        OffsetPlanEntry,
        _serialize_offset_plan,
        claim_job_offset,
        finalize_job_offset,
        store_job_offset_plan,
    )

    job_id = "j-expired-claim"
    key = _seed_import_job(moto_import_jobs_table, job_id)
    original_plan = OffsetPlan(
        offset=0,
        entries=(
            OffsetPlanEntry(name="created", disposition=MetricDisposition.CREATE),
            OffsetPlanEntry(
                name="updated",
                disposition=MetricDisposition.UPDATE,
                warning="existing metric overwritten",
            ),
        ),
    )
    replacement_plan = OffsetPlan(
        offset=0,
        entries=(
            OffsetPlanEntry(
                name="created",
                disposition=MetricDisposition.UPDATE,
                warning="replacement observed prior write",
            ),
            OffsetPlanEntry(name="updated", disposition=MetricDisposition.CREATE),
        ),
    )

    with (
        patch("coa_metrics.api.import_job_store.time") as mock_time,
        patch("coa_metrics.api.import_job_store.uuid") as mock_uuid,
    ):
        mock_time.time.side_effect = [1_000, 1_061]
        mock_uuid.uuid4.side_effect = ["claim-a", "claim-b"]
        claim_a = claim_job_offset("ns-1", job_id, offset=0, end_offset=2, chunk_size=50, lease_seconds=60)
        assert claim_a.plan is None
        assert store_job_offset_plan(
            "ns-1",
            job_id,
            offset=0,
            claim_token="claim-a",
            plan=original_plan,
        )
        claim_b = claim_job_offset("ns-1", job_id, offset=0, end_offset=2, chunk_size=50, lease_seconds=60)

    assert claim_a.state == OffsetClaimState.ACQUIRED
    assert claim_a.token == "claim-a"
    assert claim_b.state == OffsetClaimState.ACQUIRED
    assert claim_b.token == "claim-b"
    assert claim_b.plan == original_plan

    claimed_by_b = _read_import_job(moto_import_jobs_table, key)
    assert claimed_by_b["status"] == "IN_PROGRESS"
    assert claimed_by_b["processedOffsets"] == []
    assert claimed_by_b["metricsProcessed"] == 0
    assert claimed_by_b["metricsCreated"] == 0
    assert claimed_by_b["metricsUpdated"] == 0
    assert claimed_by_b["claimOffset"] == 0
    assert claimed_by_b["claimToken"] == "claim-b"
    assert claimed_by_b["claimLeaseExpiresAt"] == 1_121
    assert claimed_by_b["offsetPlan"] == _serialize_offset_plan(original_plan)

    assert (
        store_job_offset_plan(
            "ns-1",
            job_id,
            offset=0,
            claim_token="claim-a",
            plan=replacement_plan,
        )
        is False
    )
    assert _read_import_job(moto_import_jobs_table, key) == claimed_by_b

    assert (
        finalize_job_offset(
            "ns-1",
            job_id,
            offset=0,
            claim_token="claim-a",
            metrics_processed=99,
            metrics_created=98,
            metrics_updated=97,
            mark_job_completed=True,
        )
        is False
    )
    assert _read_import_job(moto_import_jobs_table, key) == claimed_by_b

    assert (
        finalize_job_offset(
            "ns-1",
            job_id,
            offset=0,
            claim_token="claim-b",
            metrics_processed=2,
            metrics_created=1,
            metrics_updated=1,
            mark_job_completed=True,
        )
        is True
    )
    completed = _read_import_job(moto_import_jobs_table, key)
    assert completed["status"] == "COMPLETED"
    assert completed["processedOffsets"] == [0]
    assert completed["metricsProcessed"] == 2
    assert completed["metricsCreated"] == 1
    assert completed["metricsUpdated"] == 1
    assert "offsetPlan" not in completed
    assert "claimOffset" not in completed
    assert "claimToken" not in completed
    assert "claimLeaseExpiresAt" not in completed

    assert (
        finalize_job_offset(
            "ns-1",
            job_id,
            offset=0,
            claim_token="claim-b",
            metrics_processed=2,
            metrics_created=1,
            metrics_updated=1,
            mark_job_completed=True,
        )
        is False
    )
    assert _read_import_job(moto_import_jobs_table, key) == completed


@pytest.mark.parametrize("initially_present", [False, True], ids=["planned-create", "planned-update"])
def test_import_worker_replays_durable_plan_and_accounts_once(moto_import_jobs_table, initially_present):
    """A replacement owner converges Neptune from the original plan and accounts it exactly once."""
    from coa_metrics.api.import_job_store import finalize_job_offset
    from coa_metrics.api.import_plan_payload import parse_plan_payload
    from coa_metrics.api.import_worker import _process_chunk

    job_id = f"j-worker-replay-{initially_present}"
    key = _seed_import_job(moto_import_jobs_table, job_id, metricsTotal=1, chunkSize=1)
    neptune = MagicMock()
    physical_metrics: dict[str, object] = {}
    if initially_present:
        physical_metrics["metric_0"] = MagicMock(defined_by="original steward")

    def get_metric(_namespace, name):
        return physical_metrics.get(name)

    def create_metric(_namespace, definition):
        physical_metrics[definition.name] = MagicMock(defined_by="import-worker")

    def update_metric(_namespace, name, _definition):
        physical_metrics[name] = MagicMock(defined_by="import-worker")

    neptune.get_metric.side_effect = get_metric
    neptune.create_metric.side_effect = create_metric
    neptune.update_metric.side_effect = update_metric
    neptune.resolve_class_uris.side_effect = lambda _namespace, concepts: [f"urn:resolved:{concepts[0]}"]

    finalize_error = RuntimeError("simulated crash after Neptune write")
    finalize_attempt = 0

    def crash_then_finalize(*args, **kwargs):
        nonlocal finalize_attempt
        finalize_attempt += 1
        if finalize_attempt == 1:
            raise finalize_error
        return finalize_job_offset(*args, **kwargs)

    checkpoint_payload = b""

    def put_checkpoint(**kwargs):
        nonlocal checkpoint_payload
        checkpoint_payload = kwargs["Body"]
        return {"VersionId": "plan-version"}

    def get_versioned_object(**kwargs):
        body = MagicMock()
        if "/imports/checkpoints/" in kwargs["Key"]:
            body.read.return_value = checkpoint_payload
            return {"VersionId": "plan-version", "Body": body}
        body.read.return_value = b"yaml"
        return {"VersionId": "source-version", "Body": body}

    with (
        patch("coa_metrics.api.import_job_store.time.time", side_effect=[2_000, 2_002]),
        patch("coa_metrics.api.import_worker._OFFSET_LEASE_SECONDS", 1),
        patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
        patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
        patch("coa_metrics.api.import_worker._get_neptune", return_value=neptune),
        patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
        patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
        patch("coa_metrics.api.import_worker.finalize_job_offset", side_effect=crash_then_finalize),
    ):
        TestImportWorker._mock_document(mock_s3, mock_parse, metric_count=1)
        mock_s3.return_value.put_object.side_effect = put_checkpoint
        mock_s3.return_value.get_object.side_effect = get_versioned_object
        mock_to_definition.side_effect = lambda *_args: TestImportWorker._mock_metric_definition(
            "metric_0",
            ontology_concepts=["Revenue"],
        )

        with pytest.raises(RuntimeError) as raised:
            _process_chunk(TestImportWorker._msg(jobId=job_id, chunkSize=1))

        assert raised.value is finalize_error
        after_crash = _read_import_job(moto_import_jobs_table, key)
        assert after_crash["status"] == "IN_PROGRESS"
        assert after_crash["processedOffsets"] == []
        assert after_crash["metricsProcessed"] == 0
        assert after_crash["metricsCreated"] == 0
        assert after_crash["metricsUpdated"] == 0
        assert after_crash["offsetPlan"]["schemaVersion"] == 2
        assert after_crash["offsetPlan"]["payloadVersionId"] == "plan-version"
        checkpoint_plan = parse_plan_payload(checkpoint_payload)
        checkpoint_entry = checkpoint_plan.entries[0]
        assert checkpoint_entry.name == "metric_0"
        assert checkpoint_entry.disposition.value == ("UPDATE" if initially_present else "CREATE")
        if initially_present:
            assert checkpoint_entry.warning == (
                "Metric 'metric_0' overwritten (had existing metadata authored by original steward)"
            )
            assert neptune.create_metric.call_count == 0
            assert neptune.update_metric.call_count == 1
        else:
            assert checkpoint_entry.warning is None
            assert neptune.create_metric.call_count == 1
            assert neptune.update_metric.call_count == 0
        assert after_crash["claimOffset"] == 0
        assert after_crash["claimLeaseExpiresAt"] == 2_001

        _process_chunk(TestImportWorker._msg(jobId=job_id, chunkSize=1))

    completed = _read_import_job(moto_import_jobs_table, key)
    assert completed["status"] == "COMPLETED"
    assert completed["processedOffsets"] == [0]
    assert completed["metricsProcessed"] == 1
    assert completed["nextOffset"] == 1
    assert completed["metricsCreated"] == (0 if initially_present else 1)
    assert completed["metricsUpdated"] == (1 if initially_present else 0)
    assert completed["errors"] == []
    assert completed["warnings"] == (
        ["Metric 'metric_0' overwritten (had existing metadata authored by original steward)"]
        if initially_present
        else []
    )
    assert "offsetPlan" not in completed
    assert "claimOffset" not in completed
    assert "claimToken" not in completed
    assert "claimLeaseExpiresAt" not in completed
    assert neptune.get_metric.call_count == 3
    assert neptune.resolve_class_uris.call_count == 1
    assert neptune.create_metric.call_count == (0 if initially_present else 1)
    assert neptune.update_metric.call_count == (2 if initially_present else 1)
    mock_sqs.assert_not_called()


def test_failed_terminal_transition_fences_stale_finalize(moto_import_jobs_table):
    """The real exhausted-DLQ path waits for lease expiry, fails the job, and fences stale finalize."""
    from coa_metrics.api.import_dlq_handler import ActiveImportOffsetError, handler
    from coa_metrics.api.import_job_store import (
        MetricDisposition,
        OffsetClaimState,
        OffsetPlan,
        OffsetPlanEntry,
        claim_job_offset,
        fail_job,
        finalize_job_offset,
        store_job_offset_plan,
    )

    job_id = "j-terminal-failure"
    key = _seed_import_job(
        moto_import_jobs_table,
        job_id,
        metricsTotal=100,
        metricsProcessed=50,
        nextOffset=50,
        processedOffsets=[0],
    )
    plan = OffsetPlan(
        offset=50,
        entries=(OffsetPlanEntry(name="metric", disposition=MetricDisposition.CREATE),),
    )

    with (
        patch("coa_metrics.api.import_job_store.time.time", return_value=2_000),
        patch("coa_metrics.api.import_job_store.uuid.uuid4", return_value="stale-claim"),
    ):
        claim = claim_job_offset("ns-1", job_id, offset=50, end_offset=100, chunk_size=50, lease_seconds=60)

    assert claim.state == OffsetClaimState.ACQUIRED
    assert claim.token == "stale-claim"
    assert store_job_offset_plan(
        "ns-1",
        job_id,
        offset=50,
        claim_token="stale-claim",
        plan=plan,
    )
    claimed = _read_import_job(moto_import_jobs_table, key)
    event = TestImportDlqHandler._event(
        redrive_count=1,
        body_job=job_id,
        attribute_job=job_id,
    )

    with (
        patch("coa_metrics.api.import_dlq_handler._get_sqs") as mock_sqs,
        patch("coa_metrics.api.import_dlq_handler.fail_job", wraps=fail_job) as mock_fail_job,
    ):
        with (
            patch("coa_metrics.api.import_job_store.time.time", return_value=2_059),
            pytest.raises(ActiveImportOffsetError, match="offset lease is active"),
        ):
            handler(event, None)

        assert _read_import_job(moto_import_jobs_table, key) == claimed
        mock_fail_job.assert_not_called()

        with patch("coa_metrics.api.import_job_store.time.time", return_value=2_061):
            handler(event, None)

    mock_sqs.assert_not_called()
    mock_fail_job.assert_called_once_with(
        "ns-1",
        job_id,
        "Import failed after exhausting queue retries and automatic recovery",
        require_idle=True,
        expected_next_offset=50,
    )
    failed = _read_import_job(moto_import_jobs_table, key)
    assert failed["status"] == "FAILED"
    assert failed["errors"] == ["Import failed after exhausting queue retries and automatic recovery"]
    assert failed["processedOffsets"] == [0]
    assert failed["metricsProcessed"] == 50
    assert failed["nextOffset"] == 50
    assert failed["metricsCreated"] == 0
    assert failed["metricsUpdated"] == 0
    assert "offsetPlan" not in failed
    assert "claimOffset" not in failed
    assert "claimToken" not in failed
    assert "claimLeaseExpiresAt" not in failed

    assert (
        finalize_job_offset(
            "ns-1",
            job_id,
            offset=50,
            claim_token="stale-claim",
            metrics_processed=1,
            metrics_created=1,
            metrics_updated=0,
            mark_job_completed=True,
        )
        is False
    )
    assert _read_import_job(moto_import_jobs_table, key) == failed


def test_legacy_accounted_final_chunk_completes_real_job_without_neptune_or_sqs(moto_import_jobs_table):
    """A legacy accounted final chunk resumes only the real terminal transition."""
    from coa_metrics.api.import_worker import _process_chunk

    job_id = "j-legacy-final"
    key = _seed_import_job(
        moto_import_jobs_table,
        job_id,
        metricsProcessed=2,
        nextOffset=None,
        chunkSize=None,
        metricsCreated=1,
        metricsUpdated=1,
        processedOffsets=[0],
    )

    with (
        patch("coa_metrics.api.import_worker._get_s3") as mock_s3,
        patch("coa_metrics.api.import_worker.parse_osi_yaml") as mock_parse,
        patch("coa_metrics.api.import_worker._get_neptune") as mock_neptune,
        patch("coa_metrics.api.import_worker._get_sqs") as mock_sqs,
        patch("coa_metrics.api.import_worker._osi_metric_to_definition") as mock_to_definition,
    ):
        TestImportWorker._mock_document(mock_s3, mock_parse, metric_count=2)
        _process_chunk(TestImportWorker._msg(jobId=job_id, chunkSize=2))

    mock_neptune.assert_not_called()
    mock_sqs.assert_not_called()
    mock_to_definition.assert_not_called()
    completed = _read_import_job(moto_import_jobs_table, key)
    assert completed["status"] == "COMPLETED"
    assert completed["processedOffsets"] == [0]
    assert completed["metricsProcessed"] == 2
    assert completed["metricsCreated"] == 1
    assert completed["metricsUpdated"] == 1
    assert "claimOffset" not in completed
    assert "claimToken" not in completed
    assert "claimLeaseExpiresAt" not in completed


@patch("coa_metrics.api.import_worker._process_chunk")
def test_handler_propagates_exact_chunk_exception(mock_process_chunk):
    """An unhandled chunk failure escapes so Lambda leaves the SQS record retryable."""
    from coa_metrics.api.import_worker import handler

    error = RuntimeError("exact chunk failure")
    mock_process_chunk.side_effect = error

    with pytest.raises(RuntimeError) as raised:
        handler(TestImportWorker._event(), None)

    assert raised.value is error
    mock_process_chunk.assert_called_once()
