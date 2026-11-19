# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The scan-run fence: a db-scan run's writes skip a source row a newer run has taken.

Evaluated by DynamoDB (moto) rather than read back as a string, because the ordering
of scan-job SKs and the ``attribute_not_exists`` branch are what decide each case. The
state machine's SCAN_FAILED step uses the same expression, pinned by the parity test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from coa_common.dao import DynamoDBDAO
from coa_sources.database.scan_run import CURRENT_SCAN_JOB_FIELD, ROW_EXISTS, SCAN_RUN_FENCE, scan_run_fence
from moto import mock_aws

pytestmark = pytest.mark.unit

_KEY = {"PK": "NS#ns-1", "SK": "SRC#src-1"}
_OLDER = "2026-11-06T01:00:00.000000Z"
_THIS = "2026-11-06T01:00:30.000000Z"
_NEWER = "2026-11-06T01:01:00.000000Z"
_STACK = Path(__file__).resolve().parents[5] / "infra" / "lib" / "stacks" / "services" / "sources-stack.ts"


@pytest.fixture()
def table() -> Iterator[Any]:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        yield ddb.create_table(
            TableName="sources",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )


def _fail_this_run() -> bool:
    return DynamoDBDAO("sources", region="us-east-1").update(
        _KEY, {"status": "SCAN_FAILED", "lastScanJobId": _THIS}, **scan_run_fence(_THIS), raise_on_error=False
    )


@pytest.mark.parametrize(
    ("recorded", "written"),
    [
        (_NEWER, False),  # a newer run took the source: this run's late write is skipped
        (_THIS, True),  # this run still owns it
        (_OLDER, True),  # the row names an older run: a run started without recording
        (None, True),  # a first scan, or a row written before the field existed
    ],
)
def test_scan_run_fence_skips_only_a_newer_run(table: Any, recorded: str | None, written: bool) -> None:
    item = {**_KEY, "status": "SCANNING"}
    if recorded is not None:
        item[CURRENT_SCAN_JOB_FIELD] = recorded
    table.put_item(Item=item)

    assert _fail_this_run() is written

    stored = table.get_item(Key=_KEY)["Item"]
    assert stored["status"] == ("SCAN_FAILED" if written else "SCANNING")
    assert stored.get(CURRENT_SCAN_JOB_FIELD) == recorded


def test_scan_run_fence_does_not_recreate_a_deleted_source(table: Any) -> None:
    assert _fail_this_run() is False
    assert "Item" not in table.get_item(Key=_KEY)


@pytest.mark.parametrize("scan_job_sk", [None, ""])
def test_scan_run_fence_without_a_run_only_needs_the_row(table: Any, scan_job_sk: str | None) -> None:
    # An execution with no scanJobSK cannot be matched to a run (the reaper's case).
    assert scan_run_fence(scan_job_sk) == {"condition": ROW_EXISTS}
    dao = DynamoDBDAO("sources", region="us-east-1")

    assert dao.update(_KEY, {"status": "SCAN_FAILED"}, **scan_run_fence(scan_job_sk), raise_on_error=False) is False
    assert "Item" not in table.get_item(Key=_KEY)

    table.put_item(Item={**_KEY, "status": "SCANNING", CURRENT_SCAN_JOB_FIELD: _NEWER})
    assert dao.update(_KEY, {"status": "SCAN_FAILED"}, **scan_run_fence(scan_job_sk), raise_on_error=False) is True


def test_scan_run_fence_is_the_state_machine_condition() -> None:
    # The SCAN_FAILED step in the db-scan state machine cannot import this module.
    stack = _STACK.read_text(encoding="utf-8")
    assert f'"{SCAN_RUN_FENCE}"' in stack
    assert f'"#run": "{CURRENT_SCAN_JOB_FIELD}"' in stack
