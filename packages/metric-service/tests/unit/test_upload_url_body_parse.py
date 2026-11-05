# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The upload URL handler returns a client error when the JSON decoder cannot read the body."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_metrics.api import get_osi_upload_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("error", [ValueError("too many digits"), RecursionError("too deep")])
def test_unreadable_body_returns_a_client_error_without_requesting_s3(error: Exception) -> None:
    event = {"pathParameters": {"namespaceId": "namespace"}, "body": "unreadable"}
    with (
        patch.object(get_osi_upload_url, "get_caller_identity", return_value="caller"),
        patch.object(get_osi_upload_url.json, "loads", side_effect=error),
        patch.object(get_osi_upload_url, "_get_s3_client") as client,
    ):
        response = get_osi_upload_url.handler(event, None)
    assert response["statusCode"] == 400
    assert "Invalid JSON" in response["body"]
    client.assert_not_called()
