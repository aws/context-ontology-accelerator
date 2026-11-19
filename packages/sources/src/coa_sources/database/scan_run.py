# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Which db-scan run a database source row belongs to.

A re-scan records its scan-job SK on the source row (``currentScanJobId``) in the
write that takes the scan lock. A run's writes to the row are conditional on no newer
run being recorded, so an older run still being retried cannot fail a newer scan.
Scan-job SKs are UTC ISO timestamps and sort by time. A row with no run recorded (a
first scan, or a row written before this field existed) is not fenced. The SCAN_FAILED
step in ``infra/lib/stacks/services/sources-stack.ts`` uses the same expression.
"""

from __future__ import annotations

from typing import Any

CURRENT_SCAN_JOB_FIELD = "currentScanJobId"

# Also requires the row to exist, so a write for a deleted source does not recreate it.
SCAN_RUN_FENCE = "attribute_exists(PK) AND (attribute_not_exists(#run) OR #run <= :run)"

ROW_EXISTS = "attribute_exists(PK)"


def scan_run_fence(scan_job_sk: str | None) -> dict[str, Any]:
    """``DynamoDBDAO.update`` keyword arguments that apply the fence for this run.

    With no ``scan_job_sk`` the write is only conditional on the row existing.
    """
    if not scan_job_sk:
        return {"condition": ROW_EXISTS}
    return {
        "condition": SCAN_RUN_FENCE,
        "condition_names": {"#run": CURRENT_SCAN_JOB_FIELD},
        "condition_values": {":run": scan_job_sk},
    }
