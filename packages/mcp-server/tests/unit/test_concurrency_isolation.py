# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Concurrency isolation tests for the Context Manager client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from coa_mcp.tools.execution import execute_query


@pytest.mark.unit
class TestConcurrencyIsolation:
    """Ensure no identity bleed across concurrent requests."""

    @pytest.mark.asyncio
    async def test_parallel_requests_use_separate_tokens(self):
        """Two concurrent CM invocations with different tokens must not cross-contaminate."""
        captured_calls: list = []

        async def capture_invoke(payload, bearer_token):
            captured_calls.append({"payload": payload, "bearer_token": bearer_token})
            await asyncio.sleep(0.01)
            return {"result": {"tier": 2, "confidence": {"score": 0.9, "rationale": "test"}, "resultRows": []}}

        mock_cm = AsyncMock()
        mock_cm.invoke = AsyncMock(side_effect=capture_invoke)

        profile_a = {"namespace": "sales", "userId": "user-a", "globalRoles": ["viewer"]}
        profile_b = {"namespace": "hr", "userId": "user-b", "globalRoles": ["viewer"]}

        await asyncio.gather(
            execute_query(mock_cm, "revenue", "sales", profile_a, "token-a"),
            execute_query(mock_cm, "headcount", "hr", profile_b, "token-b"),
        )

        assert len(captured_calls) == 2

        pairs = {(c["bearer_token"], c["payload"]["namespace"]) for c in captured_calls}
        assert pairs == {("token-a", "sales"), ("token-b", "hr")}
