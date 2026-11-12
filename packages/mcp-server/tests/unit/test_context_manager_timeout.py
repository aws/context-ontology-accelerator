# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP->ContextManager httpx client uses a split connect/read timeout."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.unit


def test_cm_client_split_timeout():
    from coa_mcp.clients import context_manager as cm

    client = cm.ContextManagerClient(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/abc",
        region="us-east-1",
    )
    assert isinstance(client._client.timeout, httpx.Timeout)
    assert client._client.timeout.connect == 2.0
    assert client._client.timeout.read == cm._INVOKE_TIMEOUT_S
