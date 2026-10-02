# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Asynchronous database-source deletion.

A database source's teardown includes one DataZone ``delete_asset`` per
discovered table — work proportional to source size — and it ran inside the
``DELETE`` request on a 30-second Lambda. An 860-table source could not finish,
so the request was killed mid-cleanup, and because the row was deleted regardless
of progress the leftover assets were orphaned with nothing left to retry them.

These tests pin the new contract:
  * with a queue wired, DELETE answers 202/DELETING and enqueues;
  * the worker runs the same shared cleanup with a 15-minute envelope;
  * the row is deleted LAST, so an incomplete delete stays retryable;
  * with no queue wired, the old inline behaviour is unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO
from moto import mock_aws

_SH = "coa_sources.api.sources_handler"
_WK = "coa_sources.api.source_deletion_worker"
_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "11111111-2222-4333-8444-555555555555"
_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/scl-dev-sources-delete-queue"


def _worker():
    """Import the worker lazily.

    Deliberately not a module-level import. Several handler modules in this
    package capture env vars at import time, so a test module that imports one
    during collection changes what the *other* test modules see at their cold
    start — importing here keeps this file from reordering anyone else's.
    """
    from coa_sources.api import source_deletion_worker

    return source_deletion_worker


def _parse(result):
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else {}


def _db_source_item(status="APPROVED"):
    return {
        "PK": f"NS#{_NAMESPACE_ID}",
        "SK": f"SRC#{_SOURCE_ID}",
        "sourceId": _SOURCE_ID,
        "namespaceId": _NAMESPACE_ID,
        "name": "my-db",
        "sourceType": "DATABASE",
        "sourceSubType": "GLUE_DATABASE",
        "status": status,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _conditional_check_failed():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


class TestDeleteHandsOffToTheWorker:
    @pytest.fixture(autouse=True)
    def _no_real_cleanup(self):
        with (
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(0, True)),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
            patch(f"{_SH}.adjust_namespace_source_count"),
        ):
            yield

    def test_returns_202_deleting_and_enqueues_without_touching_the_row(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.get.return_value = _db_source_item("APPROVED")
        mock_sqs = MagicMock()

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", _QUEUE_URL),
            patch(f"{_SH}._get_sqs", return_value=mock_sqs),
        ):
            status, body = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 202
        assert body == {"sourceId": _SOURCE_ID, "status": "DELETING"}
        # The row must SURVIVE the request: it is the only handle the worker (or a
        # retry) has on this source's remaining assets.
        mock_dao.delete.assert_not_called()
        mock_dao.update.assert_called_once()
        assert mock_dao.update.call_args.args[1]["status"] == "DELETING"

        sent = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert sent["namespace_id"] == _NAMESPACE_ID
        assert sent["source_id"] == _SOURCE_ID
        assert mock_sqs.send_message.call_args.kwargs["QueueUrl"] == _QUEUE_URL

    def test_second_delete_while_deleting_is_idempotent_202(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.get.return_value = _db_source_item("APPROVED")
        mock_dao.update.side_effect = _conditional_check_failed()
        mock_sqs = MagicMock()

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", _QUEUE_URL),
            patch(f"{_SH}._get_sqs", return_value=mock_sqs),
        ):
            status, body = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 202
        assert body["status"] == "DELETING"
        # No second message: the first hand-off is still in flight.
        mock_sqs.send_message.assert_not_called()

    def test_enqueue_failure_marks_delete_failed_rather_than_stranding_deleting(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.get.return_value = _db_source_item("APPROVED")
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "SendMessage")

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", _QUEUE_URL),
            patch(f"{_SH}._get_sqs", return_value=mock_sqs),
        ):
            status, _ = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 500
        # DELETING with no worker coming is a lie the UI would show forever.
        statuses = [c.args[1]["status"] for c in mock_dao.update.call_args_list]
        assert statuses == ["DELETING", "DELETE_FAILED"]

    def test_without_a_queue_the_delete_still_completes_inline(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.get.return_value = _db_source_item("APPROVED")

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", ""),
        ):
            status, body = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert body["status"] == "DELETED"
        mock_dao.delete.assert_called_once()


class TestFinishDatabaseSourceDeletionOrdering:
    def test_the_row_is_deleted_after_its_dependents(self):
        import coa_sources.api.sources_handler as sh

        order: list[str] = []
        mock_dao = MagicMock()
        mock_dao.delete.side_effect = lambda *a, **k: order.append("row")

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._delete_source_datazone_assets", side_effect=lambda *a: order.append("assets") or (0, True)),
            patch(f"{_SH}._delete_source_scan_jobs", side_effect=lambda *a: order.append("jobs") or 0),
        ):
            assert sh.finish_database_source_deletion(_NAMESPACE_ID, _SOURCE_ID, "GLUE_DATABASE", "cat") is True

        assert order == ["assets", "jobs", "row"]

    def test_asset_cleanup_raising_keeps_the_row(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._delete_source_datazone_assets", side_effect=RuntimeError("datazone down")),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
        ):
            # A raised cleanup means we cannot know what survived — the row is
            # the only handle on any leftover assets, so it must NOT be deleted.
            # Return False so the worker redrives instead of orphaning them.
            assert sh.finish_database_source_deletion(_NAMESPACE_ID, _SOURCE_ID, "GLUE_DATABASE", "cat") is False
        mock_dao.delete.assert_not_called()

    def test_asset_cleanup_incomplete_keeps_the_row(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            # Deleted some, but stopped early / a delete failed → complete=False.
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(7, False)),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
        ):
            assert sh.finish_database_source_deletion(_NAMESPACE_ID, _SOURCE_ID, "GLUE_DATABASE", "cat") is False
        mock_dao.delete.assert_not_called()

    def test_scan_job_cleanup_failure_does_not_strand_the_row(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            # Assets cleaned completely; only scan-job cleanup fails.
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(3, True)),
            patch(f"{_SH}._delete_source_scan_jobs", side_effect=RuntimeError("scan jobs down")),
        ):
            # Scan jobs are swept at namespace deletion off the ByNamespace GSI,
            # independent of the row, so their failure must not block the delete.
            assert sh.finish_database_source_deletion(_NAMESPACE_ID, _SOURCE_ID, "GLUE_DATABASE", "cat") is True
        mock_dao.delete.assert_called_once()

    def test_row_delete_failure_reports_incomplete(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.delete.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteItem")
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(0, True)),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
        ):
            assert sh.finish_database_source_deletion(_NAMESPACE_ID, _SOURCE_ID, "GLUE_DATABASE", "cat") is False


class TestDeletionWorker:
    def _record(self, body: dict | str, message_id="m1"):
        return {"messageId": message_id, "body": body if isinstance(body, str) else json.dumps(body)}

    def test_successful_cleanup_reports_no_failures(self):
        with patch(f"{_WK}.finish_database_source_deletion", return_value=True) as finish:
            out = _worker().handler(
                {
                    "Records": [
                        self._record(
                            {
                                "namespace_id": _NAMESPACE_ID,
                                "source_id": _SOURCE_ID,
                                "sub_type": "JDBC_DATABASE",
                                "catalog_name": "cat",
                            }
                        )
                    ]
                },
                None,
            )

        assert out == {"batchItemFailures": []}
        assert finish.call_args.args[:4] == (_NAMESPACE_ID, _SOURCE_ID, "JDBC_DATABASE", "cat")

    def test_incomplete_cleanup_marks_failed_and_redrives_that_message(self):
        mock_dao = MagicMock()
        with (
            patch(f"{_WK}.finish_database_source_deletion", return_value=False),
            patch(f"{_WK}._get_dao", return_value=mock_dao),
        ):
            out = _worker().handler(
                {"Records": [self._record({"namespace_id": _NAMESPACE_ID, "source_id": _SOURCE_ID})]},
                None,
            )

        assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
        assert mock_dao.update.call_args.args[1]["status"] == "DELETE_FAILED"

    def test_a_raising_cleanup_is_reported_not_swallowed(self):
        mock_dao = MagicMock()
        with (
            patch(f"{_WK}.finish_database_source_deletion", side_effect=RuntimeError("boom")),
            patch(f"{_WK}._get_dao", return_value=mock_dao),
        ):
            out = _worker().handler(
                {"Records": [self._record({"namespace_id": _NAMESPACE_ID, "source_id": _SOURCE_ID})]},
                None,
            )

        assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_unusable_messages_are_failed_to_the_dlq(self):
        # Neither can ever succeed, but omitting them from batchItemFailures would
        # make SQS delete them as successes; failing them sends them to the DLQ.
        with patch(f"{_WK}.finish_database_source_deletion") as finish:
            out = _worker().handler(
                {
                    "Records": [
                        self._record("{not json", message_id="bad"),
                        self._record({"source_id": _SOURCE_ID}, message_id="nons"),
                    ]
                },
                None,
            )

        assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}, {"itemIdentifier": "nons"}]}
        finish.assert_not_called()

    def test_malformed_ids_are_failed_to_the_dlq_not_acted_on(self):
        # A body whose ids are not server-generated UUIDs must not flow into DDB
        # keys / DataZone / catalog names. It is failed to the DLQ, like an
        # unparseable body.
        with patch(f"{_WK}.finish_database_source_deletion") as finish:
            out = _worker().handler(
                {
                    "Records": [
                        self._record(
                            {"namespace_id": _NAMESPACE_ID, "source_id": "bad/id with space"},
                            message_id="badid",
                        ),
                    ]
                },
                None,
            )

        assert out == {"batchItemFailures": [{"itemIdentifier": "badid"}]}
        finish.assert_not_called()

    def test_one_failure_does_not_redrive_its_healthy_batch_siblings(self):
        mock_dao = MagicMock()
        with (
            patch(f"{_WK}.finish_database_source_deletion", side_effect=[True, False]),
            patch(f"{_WK}._get_dao", return_value=mock_dao),
        ):
            out = _worker().handler(
                {
                    "Records": [
                        self._record(
                            {"namespace_id": _NAMESPACE_ID, "source_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
                            message_id="ok",
                        ),
                        self._record(
                            {"namespace_id": _NAMESPACE_ID, "source_id": "ffffffff-1111-4222-8333-444444444444"},
                            message_id="bad",
                        ),
                    ]
                },
                None,
            )

        assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


class TestSourceCountNotDoubleDecrementedOnRetry:
    """The namespace sourceCount decrements exactly once across delete retries.

    The count drops when the source first leaves the active set for DELETING.
    A DELETE retried after DELETE_FAILED re-enters ``_handle_delete`` (that status
    is neither active nor DELETING, so it passes the 409 guard and the
    ``.ne(DELETING)`` conditional update). Without a guard on the prior status it
    would decrement a second time, drifting the count one low per retry.
    """

    def test_first_delete_decrements_once(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        mock_dao.get.return_value = _db_source_item("APPROVED")

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", _QUEUE_URL),
            patch(f"{_SH}._get_sqs", return_value=MagicMock()),
            patch(f"{_SH}.adjust_namespace_source_count") as adjust,
        ):
            status, _ = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 202
        adjust.assert_called_once_with(_NAMESPACE_ID, "DATABASE", -1)

    def test_retry_after_delete_failed_does_not_decrement_again(self):
        import coa_sources.api.sources_handler as sh

        mock_dao = MagicMock()
        # The source already reached DELETE_FAILED on its first attempt, so it was
        # already counted out. This retry re-enters and must NOT decrement again.
        mock_dao.get.return_value = _db_source_item("DELETE_FAILED")

        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._SOURCE_DELETE_QUEUE_URL", _QUEUE_URL),
            patch(f"{_SH}._get_sqs", return_value=MagicMock()),
            patch(f"{_SH}.adjust_namespace_source_count") as adjust,
        ):
            status, body = _parse(sh._handle_delete(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 202
        assert body["status"] == "DELETING"
        adjust.assert_not_called()


class TestFlipToDeletingGuard:
    """The DELETING flip must never recreate a row a concurrent worker just deleted."""

    @pytest.fixture
    def table(self):
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            yield ddb.create_table(
                TableName="sources",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )

    def _flip(self):
        import coa_sources.api.sources_handler as sh

        key = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        DynamoDBDAO("sources", region="us-east-1").update(key, {"status": "DELETING"}, **sh._TO_DELETING_GUARD)
        return key

    def test_missing_row_fails_the_condition_instead_of_upserting(self, table):
        with pytest.raises(ClientError) as exc:
            self._flip()
        assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert "Item" not in table.get_item(Key={"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"})

    def test_existing_row_flips_to_deleting(self, table):
        table.put_item(Item=_db_source_item("DELETE_FAILED"))
        key = self._flip()
        assert table.get_item(Key=key)["Item"]["status"] == "DELETING"

    def test_already_deleting_fails_the_condition(self, table):
        table.put_item(Item=_db_source_item("DELETING"))
        with pytest.raises(ClientError) as exc:
            self._flip()
        assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
