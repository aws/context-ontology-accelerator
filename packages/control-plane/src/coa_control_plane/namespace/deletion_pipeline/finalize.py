# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: finalize namespace deletion.

Atomically deletes the namespace DDB record + name reservation. This is the
terminal, must-succeed step.
"""

from __future__ import annotations

from typing import Any

import structlog

from coa_control_plane.namespace.cleanup import delete_namespace_records

logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: delete the namespace record + name reservation.

    Input: {"namespaceId": str, "namespaceName": str | None}
    Output: {"namespaceId": str, "status": "DELETED"}
    """
    namespace_id = event["namespaceId"]
    namespace_name = event.get("namespaceName")

    delete_namespace_records(namespace_id, namespace_name)

    logger.info("namespace_deletion_finalized", namespace_id=namespace_id)
    return {"namespaceId": namespace_id, "status": "DELETED"}
