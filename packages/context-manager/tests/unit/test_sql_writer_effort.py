# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""options.sqlWriterEffort (off by default): the SQL writer's model effort level."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.clients.bedrock import BedrockLLMClient
from coa_serve.models import InvokeRequest
from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator, WriterOptions
from coa_serve.tier2.nl_to_sql.strategy import writer_options

pytestmark = pytest.mark.unit


class TestWriterOptions:
    @pytest.mark.parametrize(
        ("options", "expected"),
        [({}, {}), ({"sqlWriterEffort": "medium"}, {"effort": "medium"}), ({"sqlWriterEffort": "max"}, {})],
    )
    def test_effort_mapped_only_for_known_levels(self, options, expected):
        assert writer_options(options).llm_kwargs() == expected


class TestValidation:
    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_known_levels_accepted(self, level):
        assert InvokeRequest(query="q", namespace="ns", options={"sqlWriterEffort": level})

    @pytest.mark.parametrize("value", ["max", "", True, 2])
    def test_other_values_rejected(self, value):
        with pytest.raises(ValueError, match="sqlWriterEffort"):
            InvokeRequest(query="q", namespace="ns", options={"sqlWriterEffort": value})


class TestBedrockEffort:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", [None, "low"])
    async def test_effort_sent_as_output_config_only_when_set(self, effort):
        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        await client.converse("test", effort=effort)
        sent = mock_client.converse.call_args[1].get("additionalModelRequestFields")
        assert sent == ({"output_config": {"effort": "low"}} if effort else None)


class TestWriterCalls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("effort", "expected"), [("", None), ("high", "high")])
    async def test_generate_and_correct_send_the_effort(self, effort, expected):
        llm = AsyncMock()
        llm.converse.return_value = ConverseResult(text="```sql\nSELECT 1\n```\nConfidence: 0.9")
        gen = SQLGenerator(llm_client=llm, vector_client=AsyncMock())
        writer = WriterOptions(effort=effort)
        await gen._generate_sql("q", "CREATE TABLE t (a int);", None, writer=writer)
        await gen.correct("q", "CREATE TABLE t (a int);", "SELECT x", "error", writer=writer)
        assert llm.converse.await_count == 2
        for call in llm.converse.call_args_list:
            assert call.kwargs.get("effort") == expected
            assert ("effort" in call.kwargs) is bool(expected)

    @pytest.mark.asyncio
    async def test_model_rejecting_effort_falls_back_to_its_default(self, monkeypatch):
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock

        monkeypatch.setattr(bedrock, "_MODELS_REJECTING_EFFORT", set())
        sent = []

        def converse(**kw):
            sent.append(kw.get("additionalModelRequestFields"))
            if "additionalModelRequestFields" in kw:
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "output_config.effort: Extra inputs"}},
                    "Converse",
                )
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = converse
        client = BedrockLLMClient(model_id="old-model", region="us-east-1")
        client._client = mock_client
        assert (await client.converse("q", effort="medium")).text == "ok"
        assert (await client.converse("q", effort="medium")).text == "ok"
        # The first call learns the rejection and retries; the second omits effort up front.
        assert sent == [{"output_config": {"effort": "medium"}}, None, None]
