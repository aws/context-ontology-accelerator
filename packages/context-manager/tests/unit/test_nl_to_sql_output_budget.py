# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the flat NL→SQL writer's output budget and context observability.

Organised by the failure each group prevents rather than by function:

* **the budget** — a generation cut off at the output-token cap. The old 1024 was
  under half of what a real analytic query needs once a reasoning model's thinking
  blocks are paid for out of the same allowance.
* **visibility** — the cap firing is an HTTP 200 with partial text, so the only
  symptom is a query that fails at execution. ``stop_reason`` and the two warnings
  are what turn it from invisible into greppable.
* **the context log** — ``context_preview`` is length-capped and was read as
  "only 4 tables reach the model"; ``context_tables`` states the real answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier2.nl_to_sql.sql_generator import (
    _DEFAULT_MAX_OUTPUT_TOKENS,
    _MAX_MAX_OUTPUT_TOKENS,
    _MIN_MAX_OUTPUT_TOKENS,
    SQLGenerator,
    _build_raw_context,
    _context_tables,
    _extract_table_names,
    _hit_map,
    _resolve_max_output_tokens,
)

TRUNCATED = ConverseResult(
    text="```sql\nWITH t AS (SELECT id, player",
    stop_reason="max_tokens",
)


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.embed.return_value = [0.1] * 1024
    llm.converse.return_value = ConverseResult(
        text="```sql\nSELECT count(*) FROM orders\n```\nConfidence: 0.85",
        stop_reason="end_turn",
    )
    return llm


@pytest.fixture
def mock_vector():
    vector = AsyncMock()
    vector.count_documents.return_value = 2
    vector.search.return_value = [
        VectorHit(
            id="1",
            text="Table: orders | Description: Customer orders | Columns: id (int), total (decimal)",
            score=0.95,
            metadata={"entity_type": "class"},
        ),
        VectorHit(
            id="2",
            text="Table: customers | Description: Customer info | Columns: id (int), name (varchar)",
            score=0.88,
            metadata={"entity_type": "class"},
        ),
    ]
    return vector


@pytest.fixture
def generator(mock_llm, mock_vector):
    return SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)


@pytest.mark.unit
class TestOutputTokenBudget:
    """The cap the writer generates under."""

    @pytest.mark.asyncio
    async def test_generate_uses_the_default_budget(self, generator, mock_llm):
        await generator.generate("how many orders?", namespace="ns")

        assert mock_llm.converse.call_args.kwargs["max_tokens"] == _DEFAULT_MAX_OUTPUT_TOKENS
        assert _DEFAULT_MAX_OUTPUT_TOKENS == 4096

    @pytest.mark.asyncio
    async def test_correct_uses_the_same_budget_as_generate(self, generator, mock_llm):
        # The second shot rewrites the WHOLE query, so a budget that fits the first
        # attempt and not the repair would truncate exactly the retry meant to fix it.
        await generator.correct("q", "Table: orders | Columns: id (int)", "SELECT 1", "boom")

        assert mock_llm.converse.call_args.kwargs["max_tokens"] == _DEFAULT_MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_env_knob_lowers_the_budget(self, generator, mock_llm, monkeypatch):
        monkeypatch.setenv("SERVE_NL2SQL_MAX_TOKENS", "2048")
        await generator.generate("how many orders?", namespace="ns")

        assert mock_llm.converse.call_args.kwargs["max_tokens"] == 2048

    def test_budget_is_floored(self, monkeypatch):
        monkeypatch.setenv("SERVE_NL2SQL_MAX_TOKENS", "16")

        with structlog.testing.capture_logs() as logs:
            assert _resolve_max_output_tokens() == _MIN_MAX_OUTPUT_TOKENS

        assert [log["requested"] for log in logs if log["event"] == "nl_to_sql_max_tokens_clamped"] == [16]

    def test_budget_is_capped(self, monkeypatch):
        # Bedrock rejects a cap above the model's limit outright (Opus 5: 128000),
        # so an absurd value would 400 every request rather than run away — but a
        # clamp keeps that misconfiguration out of the request in the first place,
        # and a large-but-valid cap still removes the bound on one generation's
        # worst-case latency.
        monkeypatch.setenv("SERVE_NL2SQL_MAX_TOKENS", "1000000")

        with structlog.testing.capture_logs() as logs:
            assert _resolve_max_output_tokens() == _MAX_MAX_OUTPUT_TOKENS

        clamped = [log for log in logs if log["event"] == "nl_to_sql_max_tokens_clamped"]
        assert len(clamped) == 1
        assert clamped[0]["requested"] == 1000000
        assert clamped[0]["using"] == _MAX_MAX_OUTPUT_TOKENS

    def test_an_in_range_budget_is_not_flagged(self, monkeypatch):
        monkeypatch.setenv("SERVE_NL2SQL_MAX_TOKENS", "2048")

        with structlog.testing.capture_logs() as logs:
            assert _resolve_max_output_tokens() == 2048

        assert not [log for log in logs if log["event"] == "nl_to_sql_max_tokens_clamped"]

    def test_unparseable_budget_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SERVE_NL2SQL_MAX_TOKENS", "plenty")

        with structlog.testing.capture_logs() as logs:
            assert _resolve_max_output_tokens() == _DEFAULT_MAX_OUTPUT_TOKENS

        assert [log["value"] for log in logs if log["event"] == "nl_to_sql_max_tokens_invalid"] == ["plenty"]


@pytest.mark.unit
class TestTruncationIsVisible:
    """A generation cut short must not pass silently."""

    def test_converse_result_reports_truncation(self):
        assert TRUNCATED.truncated is True
        assert ConverseResult(text="ok", stop_reason="end_turn").truncated is False
        # Absent stop_reason (any caller constructing a result by hand) is not a claim
        # of truncation.
        assert ConverseResult(text="ok").truncated is False

    @pytest.mark.asyncio
    async def test_generate_warns_when_the_cap_fires(self, generator, mock_llm):
        mock_llm.converse.return_value = TRUNCATED

        with structlog.testing.capture_logs() as logs:
            await generator.generate("how many orders?", namespace="ns")

        warnings = [log for log in logs if log["event"] == "nl_to_sql_generation_truncated"]
        assert len(warnings) == 1
        assert warnings[0]["step"] == "generate"
        assert warnings[0]["max_tokens"] == _DEFAULT_MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_correct_warns_when_the_cap_fires(self, generator, mock_llm):
        mock_llm.converse.return_value = TRUNCATED

        with structlog.testing.capture_logs() as logs:
            await generator.correct("q", "Table: orders | Columns: id (int)", "SELECT 1", "boom")

        assert [log["step"] for log in logs if log["event"] == "nl_to_sql_generation_truncated"] == ["correct"]

    @pytest.mark.asyncio
    async def test_a_complete_generation_warns_about_nothing(self, generator):
        with structlog.testing.capture_logs() as logs:
            await generator.generate("how many orders?", namespace="ns")

        assert not [log for log in logs if log["event"] == "nl_to_sql_generation_truncated"]

    @pytest.mark.asyncio
    async def test_bedrock_carries_the_stop_reason_and_warns(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "stopReason": "max_tokens",
            "output": {"message": {"content": [{"text": "SELECT id, player"}]}},
        }
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        with structlog.testing.capture_logs() as logs:
            result = await client.converse("write a long query", max_tokens=1024)

        assert result.stop_reason == "max_tokens"
        assert result.truncated is True
        warnings = [log for log in logs if log["event"] == "bedrock_output_truncated"]
        assert len(warnings) == 1
        assert warnings[0]["max_tokens"] == 1024
        assert warnings[0]["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_bedrock_normal_stop_is_reported_verbatim(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": "ok"}]}},
        }
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        with structlog.testing.capture_logs() as logs:
            result = await client.converse("hi")

        assert result.stop_reason == "end_turn"
        assert result.truncated is False
        assert not [log for log in logs if log["event"] == "bedrock_output_truncated"]

    @pytest.mark.asyncio
    async def test_stream_warns_when_the_cap_fires(self):
        # A truncated stream just stops arriving, so the messageStop event is the
        # only place the consumer could ever learn why.
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"contentBlockDelta": {"delta": {"text": "SELECT id, player"}}},
            {"messageStop": {"stopReason": "max_tokens"}},
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        with structlog.testing.capture_logs() as logs:
            tokens = [token async for token in client.converse_stream("test", max_tokens=1024)]

        assert tokens == ["SELECT id, player"]
        warnings = [log for log in logs if log["event"] == "bedrock_output_truncated"]
        assert len(warnings) == 1
        assert warnings[0]["streaming"] is True


@pytest.mark.unit
class TestContextObservability:
    """What the log says about the prompt has to match the prompt."""

    HITS = [
        VectorHit(id="1", text="Table: orders | Columns: id (int)", score=0.9, metadata={}),
        VectorHit(id="2", text="Table: customers | Columns: id (int)", score=0.8, metadata={}),
    ]

    def test_context_tables_lists_exactly_what_the_prompt_carries(self):
        # "products" is FK-expanded with no hit behind it, so it contributes no text
        # — and must not be reported as a table the writer saw.
        tables = ["orders", "customers", "products"]
        context = _build_raw_context(self.HITS, tables)

        assert _context_tables(self.HITS, tables) == ["orders", "customers"]
        assert context.count("Table: ") == 2

    def test_context_tables_follows_prompt_order(self):
        assert _context_tables(self.HITS, ["customers", "orders"]) == ["customers", "orders"]

    @pytest.mark.asyncio
    async def test_context_log_states_the_tables_and_flags_the_preview_cap(self, generator, mock_vector):
        long_text = "Table: wide | Columns: " + ", ".join(f"c{i} (int)" for i in range(400))
        mock_vector.search.return_value = [VectorHit(id="1", text=long_text, score=0.9, metadata={})]

        with structlog.testing.capture_logs() as logs:
            await generator.generate("anything", namespace="ns")

        entry = next(log for log in logs if log["event"] == "nl_to_sql_context")
        assert entry["context_tables"] == ["wide"]
        assert entry["n_context_tables"] == 1
        assert entry["context_chars"] == len(long_text)
        assert entry["context_preview_truncated"] is True
        assert len(entry["context_preview"]) == 2000

    @pytest.mark.asyncio
    async def test_short_context_is_not_flagged_as_capped(self, generator):
        with structlog.testing.capture_logs() as logs:
            await generator.generate("how many orders?", namespace="ns")

        entry = next(log for log in logs if log["event"] == "nl_to_sql_context")
        assert entry["context_preview_truncated"] is False
        assert entry["context_tables"] == ["orders", "customers"]


@pytest.mark.unit
class TestClassTextNameParsing:
    """A malformed hit must not take the request down."""

    BLANK_HITS = [
        VectorHit(id="1", text="   ", score=0.9, metadata={}),
        VectorHit(id="2", text="", score=0.8, metadata={}),
        VectorHit(id="3", text="Table: orders | Columns: id (int)", score=0.7, metadata={}),
    ]

    def test_whitespace_only_class_text_is_skipped_not_raised(self):
        # `if text` is TRUE for "   " while `text.split()` yields no tokens, so the
        # old `text.split()[0]` raised IndexError here and failed the whole request.
        assert _hit_map(self.BLANK_HITS) == {"orders": {"3": "Table: orders | Columns: id (int)"}}
        assert _extract_table_names(self.BLANK_HITS) == ["orders"]
        assert _build_raw_context(self.BLANK_HITS, ["orders"]) == "Table: orders | Columns: id (int)"

    def test_a_whitespace_only_context_text_drops_the_hit_rather_than_raising(self):
        hits = [VectorHit(id="1", text="Table: orders | Columns: id (int)", score=0.9, metadata={"context_text": "  "})]

        # `context_text or hit.text` keeps "  " (truthy), so the name is unresolvable
        # and the hit is dropped. Losing one hit is the documented behaviour; the point
        # is that it no longer raises IndexError and fails the request.
        assert _hit_map(hits) == {}

    def test_text_without_the_table_prefix_uses_its_first_token(self):
        hits = [VectorHit(id="1", text="Orders  extra words", score=0.9, metadata={})]

        assert list(_hit_map(hits)) == ["orders"]
