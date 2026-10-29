# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for session.py rich history features.

Tests ULID session ID generation, message envelope building/parsing,
and the get_rich_history method.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
import structlog
from coa_serve import session as session_module
from coa_serve.graph_context import (
    MAX_SUPPORTING_STATEMENT_BYTES,
    MAX_SUPPORTING_STATEMENT_CHARS,
    SUPPORTING_STATEMENTS_KEY,
)
from coa_serve.session import (
    SessionManager,
    _build_message_envelope,
    _parse_assistant_content,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# SessionManager.generate_session_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateSessionId:
    def test_returns_non_empty_string(self):
        sid = SessionManager.generate_session_id()
        assert sid
        assert isinstance(sid, str)

    def test_returns_unique_ids(self):
        sid1 = SessionManager.generate_session_id()
        sid2 = SessionManager.generate_session_id()
        assert sid1 != sid2

    def test_returns_valid_ulid_length(self):
        sid = SessionManager.generate_session_id()
        assert len(sid) == 26  # ULID string is always 26 chars


# ---------------------------------------------------------------------------
# _build_message_envelope
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildMessageEnvelope:
    @staticmethod
    def assert_bounded(result: str) -> None:
        assert len(result) <= 10_000
        assert len(result.encode("utf-8")) <= 10_000

    def test_basic_envelope(self):
        result = _build_message_envelope("Hello world", result_metadata={"tier": 2})
        obj = json.loads(result)
        assert obj["text"] == "Hello world"
        assert obj["tier"] == 2

    def test_full_envelope(self):
        result = _build_message_envelope(
            answer="Revenue is $4.2M",
            result_metadata={
                "tier": 1,
                "confidence": {"score": 0.95, "rationale": "Exact metric match"},
                "trace": [{"step": "metric_match", "status": "success", "durationMs": 50}],
                "queryUsed": "SELECT sum(revenue) FROM facts",
            },
        )
        obj = json.loads(result)
        assert obj["text"] == "Revenue is $4.2M"
        assert obj["tier"] == 1
        assert obj["confidence"]["score"] == 0.95
        assert obj["confidence"]["rationale"] == "Exact metric match"
        assert len(obj["trace"]) == 1
        assert obj["queryUsed"] == "SELECT sum(revenue) FROM facts"

    def test_truncates_long_trace_details(self):
        # Build trace with huge detail fields
        trace = [{"step": f"step_{i}", "status": "success", "durationMs": 100, "detail": "x" * 5000} for i in range(5)]
        result = _build_message_envelope("answer", result_metadata={"tier": 2, "trace": trace})
        assert len(result) <= 10_000
        obj = json.loads(result)
        # All steps preserved (only details truncated, not steps removed)
        assert len(obj["trace"]) == 5
        # At least some details were stripped to fit within limit
        details_remaining = sum(1 for s in obj["trace"] if "detail" in s)
        assert details_remaining < 5

    def test_trace_details_are_stripped_earliest_first_and_only_as_needed(self):
        trace = [{"step": f"step_{i}", "status": "success", "durationMs": 100, "detail": "x" * 2500} for i in range(6)]
        result = _build_message_envelope("answer", result_metadata={"tier": 2, "trace": trace})
        obj = json.loads(result)
        kept = ["detail" in step for step in obj["trace"]]
        # Later steps keep their details; only a leading run is stripped.
        assert kept == sorted(kept)
        assert 0 < sum(kept) < len(kept)
        # Stripping one more leading step than necessary would have been wasteful:
        # restoring the first stripped detail must overflow the metadata budget.
        first_kept = kept.index(True)
        restored = [dict(step) for step in obj["trace"]]
        restored[first_kept - 1]["detail"] = "x" * 2500
        assert session_module._metadata_too_large({**obj, "trace": restored})

    def test_preserves_step_status_duration_after_truncation(self):
        trace = [{"step": "big_step", "status": "success", "durationMs": 200, "detail": "y" * 9000}]
        result = _build_message_envelope("ans", result_metadata={"tier": 3, "trace": trace})
        obj = json.loads(result)
        assert obj["trace"][0]["step"] == "big_step"
        assert obj["trace"][0]["status"] == "success"
        assert obj["trace"][0]["durationMs"] == 200

    def test_no_metadata_produces_text_only(self):
        result = _build_message_envelope("just text")
        obj = json.loads(result)
        assert obj == {"text": "just text"}

    def test_new_fields_pass_through(self):
        """Any new field on QueryResult flows through without code changes."""
        result = _build_message_envelope(
            "ans",
            result_metadata={
                "tier": 2,
                "confidence": {"score": 0.8, "rationale": "good"},
                "sparqlGenerated": "SELECT ?x WHERE { ?x a :Foo }",
                "guardrailBlocked": False,
            },
        )
        obj = json.loads(result)
        assert obj["sparqlGenerated"] == "SELECT ?x WHERE { ?x a :Foo }"
        assert obj["guardrailBlocked"] is False

    def test_unserializable_optional_metadata_preserves_tier3_contract(self):
        result = _build_message_envelope(
            "Partial agentic answer",
            result_metadata={
                "tier": 3,
                "confidence": {"score": 0.61, "rationale": "Some sources degraded"},
                "partial": True,
                "metadata": {"namespace": "demo", "bad": object()},
            },
        )

        self.assert_bounded(result)
        text, metadata = _parse_assistant_content(result)
        assert text == "Partial agentic answer"
        assert metadata["tier"] == 3
        assert metadata["confidence"] == {
            "score": 0.61,
            "rationale": "Some sources degraded",
        }
        assert metadata["partial"] is True
        assert metadata["metadata"] == {"namespace": "demo"}

    def test_nested_cyclic_and_unserializable_values_drop_only_invalid_branches(self):
        cyclic: dict[str, object] = {"kept": "cycle value"}
        cyclic["self"] = cyclic
        result = _build_message_envelope(
            "answer",
            result_metadata={
                "tier": 3,
                "partial": True,
                "metadata": {
                    "bad": object(),
                    "nested": {
                        "items": [
                            "first",
                            object(),
                            {"kept": "nested value", "bad": object()},
                        ],
                        "cycle": cyclic,
                        "tuple": ("tuple value", object()),
                    },
                },
            },
        )

        self.assert_bounded(result)
        obj = json.loads(result)
        assert obj["tier"] == 3
        assert obj["partial"] is True
        assert obj["metadata"] == {
            "nested": {
                "items": ["first", {"kept": "nested value"}],
                "cycle": {"kept": "cycle value"},
                "tuple": ["tuple value"],
            }
        }

    def test_result_rows_included_in_envelope(self):
        """resultRows are preserved in the envelope for UI table restore."""
        rows = [
            {"claim_id": "CL-44201", "status": "Open", "amount": "$14,200"},
            {"claim_id": "CL-44318", "status": "Open", "amount": "$6,800"},
        ]
        result = _build_message_envelope(
            "",
            result_metadata={
                "tier": 1,
                "confidence": {"score": 0.92, "rationale": "Exact match"},
                "resultRows": rows,
                "queryUsed": "SELECT * FROM claims WHERE status = 'Open'",
                "trace": [{"step": "metric_match", "status": "success", "durationMs": 50}],
            },
        )
        obj = json.loads(result)
        assert obj["text"] == ""
        assert obj["resultRows"] == rows
        assert len(obj["resultRows"]) == 2
        assert obj["resultRows"][0]["claim_id"] == "CL-44201"

    def test_compacts_multibyte_graph_context_and_round_trips_restored_evidence(self):
        oversized = "証拠" * 4_000
        result = _build_message_envelope(
            "answer",
            result_metadata={
                "tier": 3,
                "trace": [],
                "graphContext": {
                    "entities": [
                        {
                            "uri": "urn:first",
                            "label": "First",
                            "properties": {"supportingStatements": json.dumps([oversized, "first-extra"])},
                        },
                        {
                            "uri": "urn:second",
                            "label": "Second",
                            "properties": {"supportingStatements": json.dumps([oversized, "second-extra"])},
                        },
                    ],
                    "relationships": [],
                },
            },
        )

        assert len(result) <= 10_000
        assert len(result.encode("utf-8")) <= 10_000
        text, metadata = _parse_assistant_content(result)
        assert text == "answer"
        restored_entities = metadata["graphContext"]["entities"]
        assert {entity["uri"] for entity in restored_entities} == {"urn:first", "urn:second"}
        assert all(json.loads(entity["properties"]["supportingStatements"]) for entity in restored_entities)

    def test_bounds_graph_statement_even_when_total_envelope_is_under_limit(self):
        result = _build_message_envelope(
            "answer",
            result_metadata={
                "graphContext": {
                    "entities": [
                        {
                            "uri": "urn:entity",
                            "label": "Entity",
                            "properties": {"supportingStatements": json.dumps(["x" * 5_000])},
                        }
                    ],
                    "relationships": [],
                }
            },
        )

        _, metadata = _parse_assistant_content(result)
        statements = json.loads(metadata["graphContext"]["entities"][0]["properties"]["supportingStatements"])
        assert len(statements[0]) <= 1_000
        assert len(statements[0].encode("utf-8")) <= 2_000

    def test_parse_rebounds_legacy_oversized_graph_context_on_the_read_path(self):
        # The documented statement bounds belong to the CONTRACT, so a record
        # persisted before they existed must not be returned unbounded. The legacy
        # top-level-list shape is still canonicalized and the legacy property KEY is
        # still preserved — only the oversized VALUE is re-bounded.
        legacy_graph_context = [
            {
                "uri": "urn:legacy",
                "label": "Legacy",
                "properties": {"supporting_statements": ["旧" * 12_000]},
            }
        ]
        raw = json.dumps({"text": "legacy answer", "graphContext": legacy_graph_context}, ensure_ascii=False)

        text, metadata = _parse_assistant_content(raw)

        assert text == "legacy answer"
        graph_context = metadata["graphContext"]
        assert graph_context["relationships"] == []
        (entity,) = graph_context["entities"]
        assert entity["uri"] == "urn:legacy"
        assert entity["label"] == "Legacy"
        assert SUPPORTING_STATEMENTS_KEY not in entity["properties"]
        statements = json.loads(entity["properties"]["supporting_statements"])
        assert len(statements) == 1
        assert len(statements[0]) <= MAX_SUPPORTING_STATEMENT_CHARS
        assert len(statements[0].encode("utf-8")) <= MAX_SUPPORTING_STATEMENT_BYTES
        assert statements[0].startswith("旧")

    def test_parse_preserves_bounded_legacy_plain_text_statement(self):
        raw = json.dumps(
            {
                "text": "legacy answer",
                "graphContext": {
                    "entities": [
                        {
                            "uri": "urn:legacy",
                            "label": "Legacy",
                            "properties": {"supporting_statements": "one plain statement"},
                        }
                    ],
                    "relationships": [],
                },
            }
        )

        _, metadata = _parse_assistant_content(raw)

        properties = metadata["graphContext"]["entities"][0]["properties"]
        assert json.loads(properties["supporting_statements"]) == ["one plain statement"]

    def test_72kb_answer_is_truncated_with_core_contract_fields_preserved(self):
        answer = "a" * (72 * 1_024)
        result = _build_message_envelope(
            answer,
            result_metadata={
                "tier": 3,
                "confidence": {"score": 0.8, "rationale": "Agentic synthesis"},
                "partial": True,
                "guardrailBlocked": False,
            },
        )

        self.assert_bounded(result)
        obj = json.loads(result)
        assert obj["text"].startswith("a" * 100)
        assert obj["text"].endswith("…")
        assert obj["tier"] == 3
        assert obj["confidence"]["score"] == 0.8
        assert obj["partial"] is True
        assert obj["guardrailBlocked"] is False

    def test_30kb_result_rows_are_progressively_compacted(self):
        rows = [{"row": index, "value": "x" * 1_000} for index in range(30)]
        result = _build_message_envelope(
            "Rows answer",
            result_metadata={
                "tier": 1,
                "confidence": {"score": 1.0, "rationale": "Exact metric"},
                "resultRows": rows,
            },
        )

        self.assert_bounded(result)
        obj = json.loads(result)
        assert obj["text"] == "Rows answer"
        assert obj["tier"] == 1
        assert 0 < len(obj["resultRows"]) < len(rows)
        assert [row["row"] for row in obj["resultRows"]] == list(range(len(obj["resultRows"])))

    def test_oversized_graph_context_is_trimmed_to_a_connected_subgraph(self):
        entities = [
            {
                "uri": f"https://example.com/entities/e-{index}",
                "label": f"Entity Name {index}",
                "type": "https://example.com/classes/Company",
            }
            for index in range(200)
        ]
        relationships = [
            {
                "sourceUri": f"https://example.com/entities/e-{index % 200}",
                "predicateUri": "https://example.com/predicates/relates",
                "targetUri": f"https://example.com/entities/e-{(index + 1) % 200}",
                "predicateLabel": "relates to",
            }
            for index in range(1_000)
        ]

        result = _build_message_envelope(
            "answer",
            result_metadata={"tier": 3, "graphContext": {"entities": entities, "relationships": relationships}},
        )

        self.assert_bounded(result)
        graph_context = json.loads(result)["graphContext"]
        # Trimming must not sacrifice EVERY edge before dropping one entity: the
        # restored context has to remain a graph, not a list of orphaned names.
        assert graph_context["entities"]
        assert graph_context["relationships"]
        retained_uris = {entity["uri"] for entity in graph_context["entities"]}
        for relationship in graph_context["relationships"]:
            assert relationship["sourceUri"] in retained_uris
            assert relationship["targetUri"] in retained_uris
        obj = json.loads(result)
        assert obj["partial"] is True
        reasons = {entry["reason"] for entry in obj["metadata"]["degradedSources"]}
        assert "graph_relationship_cap_reached" in reasons
        assert "graph_relationship_size_compaction" in reasons

    def test_relationship_count_cap_is_persisted_as_partial_degradation(self):
        relationships = [{"id": index} for index in range(201)]

        result = _build_message_envelope(
            "answer",
            result_metadata={
                "tier": 3,
                "partial": False,
                "graphContext": {"entities": [], "relationships": relationships},
            },
        )

        obj = json.loads(result)
        assert len(obj["graphContext"]["relationships"]) == 200
        assert obj["partial"] is True
        assert {(entry["source"], entry["reason"]) for entry in obj["metadata"]["degradedSources"]} == {
            ("session_persistence", "graph_relationship_cap_reached")
        }

    def test_relationship_size_compaction_is_persisted_as_partial_degradation(self):
        entities = [{"uri": "a"}, {"uri": "b"}]
        relationships = [
            {
                "sourceUri": "a",
                "predicateUri": f"urn:predicate:{index}",
                "targetUri": "b",
                "predicateLabel": "relationship-" + ("x" * 300),
            }
            for index in range(100)
        ]

        result = _build_message_envelope(
            "answer",
            result_metadata={
                "tier": 3,
                "partial": False,
                "graphContext": {"entities": entities, "relationships": relationships},
            },
        )

        obj = json.loads(result)
        retained = obj["graphContext"]["relationships"]
        assert 0 < len(retained) < len(relationships)
        assert obj["partial"] is True
        assert any(
            entry["source"] == "session_persistence" and entry["reason"] == "graph_relationship_size_compaction"
            for entry in obj["metadata"]["degradedSources"]
        )

    def test_restore_caps_legacy_relationships_and_adds_partial_degradation(self):
        raw = json.dumps(
            {
                "text": "legacy answer",
                "tier": 3,
                "partial": False,
                "graphContext": {
                    "entities": [],
                    "relationships": [{"id": index} for index in range(201)],
                },
            }
        )

        text, metadata = _parse_assistant_content(raw)

        assert text == "legacy answer"
        assert len(metadata["graphContext"]["relationships"]) == 200
        assert metadata["partial"] is True
        assert metadata["metadata"]["degradedSources"] == [
            {
                "source": "session_persistence",
                "reason": "graph_relationship_cap_reached",
                "detail": "graph relationship evidence was truncated while persisting or restoring the turn",
            }
        ]

    def test_multibyte_answer_respects_utf8_byte_and_character_limits(self):
        result = _build_message_envelope(
            "界" * 30_000,
            result_metadata={"tier": 3, "partial": False},
        )

        self.assert_bounded(result)
        obj = json.loads(result)
        assert obj["text"].startswith("界")
        assert obj["text"].endswith("…")
        assert obj["tier"] == 3

    def test_compaction_continues_after_graph_context_is_removed(self):
        result = _build_message_envelope(
            "answer-" * 12_000,
            result_metadata={
                "tier": 3,
                "query": "q" * 30_000,
                "graphContext": {
                    "entities": [
                        {
                            "uri": "urn:oversized",
                            "label": "Oversized",
                            "properties": {"blob": "g" * 30_000},
                        }
                    ],
                    "relationships": [],
                },
            },
        )

        self.assert_bounded(result)
        obj = json.loads(result)
        assert "graphContext" not in obj
        assert "query" not in obj
        assert obj["tier"] == 3
        assert obj["text"].startswith("answer-")


# ---------------------------------------------------------------------------
# _build_message_envelope — cost bounds
#
# These assert a BOUND on the work, not how the bound is achieved. Envelope
# construction used to pop one element per full envelope re-serialization, which
# made a caller-requestable 10,000-row Tier-2 result cost ~15 s of pure CPU on the
# request path (and graph context with nested relationships ~27 s).
# ---------------------------------------------------------------------------


def _wide_result_rows(count: int) -> list[dict]:
    return [{"id": index, "name": f"row-{index}", "value": index * 1.5, "desc": "x" * 40} for index in range(count)]


def _graph_entities_with_edges(count: int, edges_per_entity: int = 10) -> list[dict]:
    return [
        {
            "uri": f"https://example.com/entities/e-{index}",
            "label": f"Entity {index}",
            "type": "https://example.com/classes/Company",
            "relationships": [
                {
                    "sourceUri": f"https://example.com/entities/e-{index}",
                    "predicateUri": "https://example.com/predicates/relates",
                    "targetUri": f"https://example.com/entities/e-{(index + offset) % count}",
                    "predicateLabel": "relates to",
                }
                for offset in range(1, edges_per_entity + 1)
            ],
            "properties": {"supportingStatements": json.dumps([f"Statement about entity {index}: " + "z" * 300])},
        }
        for index in range(count)
    ]


@pytest.mark.unit
class TestBuildMessageEnvelopeCostBounds:
    @staticmethod
    def _build_with_serialization_count(metadata: dict) -> tuple[str, int]:
        original = session_module._serialize_envelope
        with patch.object(session_module, "_serialize_envelope", wraps=original) as serialize:
            result = _build_message_envelope("answer text", result_metadata=metadata)
        return result, serialize.call_count

    def test_trace_detail_serializations_do_not_grow_with_step_count(self):
        # Trace details are stripped before result rows are compacted, so every size
        # check re-serializes the full row payload. One check per stripped step made
        # a 100-step trace beside large rows cost one full serialization per step.
        def _metadata(step_count: int) -> dict:
            trace = [
                {"step": f"step_{index}", "status": "success", "durationMs": 1, "detail": "d" * 200}
                for index in range(step_count)
            ]
            return {"tier": 2, "resultRows": _wide_result_rows(2_000), "trace": trace}

        _, small = self._build_with_serialization_count(_metadata(10))
        result, large = self._build_with_serialization_count(_metadata(100))

        assert len(result) <= 10_000
        assert large <= small + 4, f"{small} serializations for 10 trace steps, {large} for 100"

    def test_result_row_serializations_do_not_grow_with_caller_input(self):
        # MAX_RESULT_ROWS in tier2/strategy.py is 10,000, so this size is reachable
        # from a single caller request.
        _, small = self._build_with_serialization_count({"tier": 2, "resultRows": _wide_result_rows(400)})
        result, large = self._build_with_serialization_count({"tier": 2, "resultRows": _wide_result_rows(10_000)})

        assert len(result) <= 10_000
        # 25x the input must not cost anything like 25x the serializations. The old
        # one-pop-per-serialization loop scaled 1:1 with the row count.
        assert large <= small + 4, f"{small} serializations for 400 rows, {large} for 10,000"
        assert large < 60, f"{large} serializations for 10,000 rows"

    def test_graph_serializations_do_not_grow_with_caller_input(self):
        _, small = self._build_with_serialization_count(
            {
                "tier": 3,
                "graphContext": {
                    "entities": _graph_entities_with_edges(400),
                    "relationships": [],
                },
            }
        )
        result, large = self._build_with_serialization_count(
            {
                "tier": 3,
                "graphContext": {
                    "entities": _graph_entities_with_edges(2_000),
                    "relationships": [],
                },
            }
        )

        assert len(result) <= 10_000
        # Both inputs exceed the 200-entity/statement persistence caps, so growing
        # caller input 5x must not increase size-driven full-envelope work.
        assert large <= small + 4, f"{small} serializations for 400 entities, {large} for 2,000"
        assert large < 260, f"{large} serializations for 2,000 graph entities"


# ---------------------------------------------------------------------------
# _parse_assistant_content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseAssistantContent:
    def test_plain_string(self):
        text, meta = _parse_assistant_content("Hello world")
        assert text == "Hello world"
        assert meta is None

    def test_envelope_with_metadata(self):
        envelope = json.dumps({"text": "The answer", "tier": 2, "confidence": {"score": 0.9, "rationale": "ok"}})
        text, meta = _parse_assistant_content(envelope)
        assert text == "The answer"
        assert meta["tier"] == 2
        assert meta["confidence"]["score"] == 0.9

    def test_envelope_text_only(self):
        envelope = json.dumps({"text": "Just text"})
        text, meta = _parse_assistant_content(envelope)
        assert text == "Just text"
        assert meta is None  # no other fields -> None

    def test_malformed_json(self):
        text, meta = _parse_assistant_content("{broken json")
        assert text == "{broken json"
        assert meta is None

    def test_json_without_text_key(self):
        # Valid JSON but not our envelope format
        text, meta = _parse_assistant_content('{"foo": "bar"}')
        assert text == '{"foo": "bar"}'
        assert meta is None

    def test_non_object_json(self):
        text, meta = _parse_assistant_content("[1, 2, 3]")
        assert text == "[1, 2, 3]"
        assert meta is None

    # The guard used to render a serialized envelope as the answer the user reads.
    # Envelopes are now near-universal on this path, so malformed
    # envelope-looking content gets a neutral restoration message instead.
    @pytest.mark.parametrize(
        "raw",
        [
            '{"text":"unterminated',
            '  {"text":',
            '{"text": null}',
            '{"text": 42}',
            '{"text": {"nested": "object"}}',
        ],
    )
    def test_unusable_envelope_never_surfaces_raw_json(self, raw):
        text, meta = _parse_assistant_content(raw)

        assert text == "This response could not be restored."
        assert raw not in text
        assert meta is None

    @pytest.mark.parametrize(
        "raw",
        [
            "{broken json",
            "{" * 2_000,
            '{"notext": "value"}',
            '{"answer": "ordinary JSON plaintext"}',
        ],
    )
    def test_non_envelope_plaintext_that_starts_with_brace_is_preserved(self, raw):
        text, meta = _parse_assistant_content(raw)

        assert text == raw
        assert meta is None

    def test_a_well_formed_envelope_still_parses(self):
        raw = json.dumps({"text": "The answer", "tier": 3})

        text, meta = _parse_assistant_content(raw)

        assert text == "The answer"
        assert meta == {"tier": 3}


# ---------------------------------------------------------------------------
# SessionManager.create_or_resume (envelope handling)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateOrResumeEnvelope:
    @pytest.mark.asyncio
    async def test_extracts_text_from_envelope(self):
        envelope = json.dumps({"text": "Answer", "tier": 2, "trace": []})
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "What?"}},
                {"role": "ASSISTANT", "content": {"text": envelope}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            sid, history = await mgr.create_or_resume("user1", "ns1")

            # Should extract only text for orchestrator
            assert history[1]["content"] == "Answer"
            assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_plain_string_passes_through(self):
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "Hi"}},
                {"role": "ASSISTANT", "content": {"text": "Hello there"}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            _, history = await mgr.create_or_resume("user1", "ns1")

            assert history[1]["content"] == "Hello there"


# ---------------------------------------------------------------------------
# SessionManager.get_rich_history
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetRichHistory:
    @pytest.mark.asyncio
    async def test_parses_envelope_metadata(self):
        envelope = json.dumps(
            {
                "text": "Revenue is $4.2M",
                "tier": 1,
                "confidence": {"score": 0.92, "rationale": "Exact match"},
                "trace": [{"step": "metric_match", "status": "success", "durationMs": 50}],
                "queryUsed": "SELECT sum(revenue)",
            }
        )
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "What is revenue?"}},
                {"role": "ASSISTANT", "content": {"text": envelope}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            history = await mgr.get_rich_history("user1", "session-abc")

            assert len(history) == 2
            user_msg = history[0]
            assistant_msg = history[1]

            assert user_msg["role"] == "user"
            assert user_msg["content"] == "What is revenue?"
            assert "metadata" not in user_msg

            assert assistant_msg["role"] == "assistant"
            assert assistant_msg["content"] == "Revenue is $4.2M"
            assert assistant_msg["metadata"]["tier"] == 1
            assert assistant_msg["metadata"]["confidence"]["score"] == 0.92
            assert len(assistant_msg["metadata"]["trace"]) == 1

    @pytest.mark.asyncio
    async def test_legacy_messages_no_metadata(self):
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "Hi"}},
                {"role": "ASSISTANT", "content": {"text": "Hello!"}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            history = await mgr.get_rich_history("user1", "session-abc")

            assert history[1]["content"] == "Hello!"
            assert "metadata" not in history[1]

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back(self):
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "Q"}},
                {"role": "ASSISTANT", "content": {"text": "{bad json here"}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            history = await mgr.get_rich_history("user1", "session-abc")

            assert history[1]["content"] == "{bad json here"
            assert "metadata" not in history[1]

    @pytest.mark.asyncio
    async def test_malformed_envelope_is_replaced_in_history(self):
        raw = '{"text":"truncated'
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "Q"}},
                {"role": "ASSISTANT", "content": {"text": raw}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            history = await mgr.get_rich_history("user1", "session-abc")

            assert history[1]["content"] == "This response could not be restored."
            assert raw not in history[1]["content"]
            assert "metadata" not in history[1]

    @pytest.mark.asyncio
    async def test_result_rows_preserved_in_rich_history(self):
        """resultRows stored in envelope are available in metadata on restore."""
        rows = [{"total_accounts": "47"}]
        envelope = json.dumps(
            {
                "text": "",
                "tier": 1,
                "confidence": {"score": 0.95, "rationale": "Metric match"},
                "resultRows": rows,
                "queryUsed": "SELECT count(*) FROM accounts",
                "trace": [],
            }
        )
        mock_turns = [
            [
                {"role": "USER", "content": {"text": "total accounts"}},
                {"role": "ASSISTANT", "content": {"text": envelope}},
            ]
        ]
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = mock_turns
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            history = await mgr.get_rich_history("user1", "session-abc")

            assistant_msg = history[1]
            assert assistant_msg["content"] == ""
            assert assistant_msg["metadata"]["resultRows"] == rows
            assert assistant_msg["metadata"]["queryUsed"] == "SELECT count(*) FROM accounts"


# ---------------------------------------------------------------------------
# SessionManager.append_turn (with metadata)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAppendTurnWithMetadata:
    @pytest.mark.asyncio
    async def test_builds_envelope_when_metadata_provided(self):
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.add_turns = MagicMock()
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            await mgr.append_turn(
                user_id="user1",
                session_id="sess1",
                query="What is revenue?",
                answer_summary="$4.2M",
                result_metadata={"tier": 1, "confidence": {"score": 0.9, "rationale": "ok"}},
            )

            # Verify add_turns was called
            mock_session.add_turns.assert_called_once()
            call_args = mock_session.add_turns.call_args
            messages = call_args[0][0]
            assert messages[0].text == "What is revenue?"
            # Assistant message should be an envelope JSON
            assistant_text = messages[1].text
            obj = json.loads(assistant_text)
            assert obj["text"] == "$4.2M"
            assert obj["tier"] == 1

    @pytest.mark.asyncio
    async def test_unserializable_optional_metadata_preserves_contract_in_stored_envelope(self):
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.add_turns = MagicMock()
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            await mgr.append_turn(
                user_id="user1",
                session_id="sess1",
                query="Q",
                answer_summary="界" * 30_000,
                result_metadata={
                    "tier": 3,
                    "confidence": {"score": 0.47, "rationale": "Partial result"},
                    "partial": True,
                    "metadata": {"namespace": "demo", "bad": object()},
                },
            )

            mock_session.add_turns.assert_called_once()
            messages = mock_session.add_turns.call_args[0][0]
            assistant_text = messages[1].text
            assert len(assistant_text) <= 10_000
            assert len(assistant_text.encode("utf-8")) <= 10_000
            obj = json.loads(assistant_text)
            assert obj["text"].startswith("界")
            assert obj["tier"] == 3
            assert obj["confidence"] == {"score": 0.47, "rationale": "Partial result"}
            assert obj["partial"] is True
            assert obj["metadata"] == {"namespace": "demo"}

    @pytest.mark.asyncio
    async def test_no_metadata_stores_plain_text(self):
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.add_turns = MagicMock()
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            await mgr.append_turn(
                user_id="user1",
                session_id="sess1",
                query="Hi",
                answer_summary="Hello!",
            )

            messages = mock_session.add_turns.call_args[0][0]
            assert messages[1].text == "Hello!"

    @pytest.mark.asyncio
    async def test_no_metadata_oversized_answer_is_stored_as_bounded_envelope(self):
        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.add_turns = MagicMock()
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            await mgr.append_turn(
                user_id="user1",
                session_id="sess1",
                query="Q",
                answer_summary="a" * (72 * 1_024),
            )

            assistant_text = mock_session.add_turns.call_args[0][0][1].text
            assert len(assistant_text) <= 10_000
            assert len(assistant_text.encode("utf-8")) <= 10_000
            assert json.loads(assistant_text)["text"].endswith("…")

    @pytest.mark.asyncio
    async def test_envelope_is_built_off_the_event_loop_thread(self):
        # Envelope construction is synchronous CPU work over caller-sized
        # collections. Inline in the coroutine it blocks every in-flight request on
        # the loop, so it has to run on the session executor.
        build_threads: list[int] = []
        original = session_module._build_message_envelope

        def recording_build(*args, **kwargs):
            build_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with (
            patch("coa_serve.session.MemorySessionManager") as MockMgr,
            patch("coa_serve.session._build_message_envelope", side_effect=recording_build),
        ):
            MockMgr.return_value.create_memory_session.return_value = MagicMock()
            mgr = SessionManager(memory_id="mem-123")
            await mgr.append_turn(
                user_id="user1",
                session_id="sess1",
                query="Q",
                answer_summary="A",
                result_metadata={"tier": 2, "resultRows": [{"id": 1}]},
            )

        assert build_threads, "envelope builder was not called"
        assert threading.get_ident() not in build_threads


@pytest.mark.unit
class TestEnvelopeTelemetry:
    """Lossy persistence steps must leave an operator-visible signal."""

    def test_answer_truncation_is_logged(self):
        with structlog.testing.capture_logs() as logs:
            result = _build_message_envelope("a" * (72 * 1_024), result_metadata={"tier": 3})

        assert json.loads(result)["text"].endswith("…")
        (entry,) = [log for log in logs if log["event"] == "session_envelope_answer_truncated"]
        assert entry["original_chars"] == 72 * 1_024
        assert 0 < entry["retained_chars"] < 72 * 1_024

    def test_an_answer_that_fits_is_not_reported_as_truncated(self):
        with structlog.testing.capture_logs() as logs:
            _build_message_envelope("short answer", result_metadata={"tier": 3})

        assert "session_envelope_answer_truncated" not in [log["event"] for log in logs]
        assert "session_envelope_backstop_used" not in [log["event"] for log in logs]

    def test_backstop_is_logged_when_it_replaces_the_turn(self, monkeypatch):
        # The backstop cannot be reached with real inputs (the essential envelope is
        # far below the limits), so force the size check to fail everywhere.
        monkeypatch.setattr(session_module, "_envelope_too_large", lambda serialized: True)

        with structlog.testing.capture_logs() as logs:
            result = _build_message_envelope("the answer", result_metadata={"tier": 3})

        assert result == '{"text":""}'
        (entry,) = [log for log in logs if log["event"] == "session_envelope_backstop_used"]
        assert entry["answer_chars"] == len("the answer")
        assert "tier" in entry["metadata_keys"]


# ---------------------------------------------------------------------------
# SessionMetadataStore — namespace-scoped operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestSessionMetadataNamespaceScoping:
    """Tests for namespace-aware metadata store methods."""

    @pytest.fixture
    def mock_table(self):
        with patch("coa_serve.session_metadata.boto3") as mock_boto:
            mock_resource = MagicMock()
            mock_table = MagicMock()
            mock_resource.Table.return_value = mock_table
            mock_boto.resource.return_value = mock_resource
            # Mock the exceptions for contextlib.suppress
            mock_resource.meta.client.exceptions.ConditionalCheckFailedException = type(
                "ConditionalCheckFailedException", (Exception,), {}
            )
            yield mock_table

    async def test_create_includes_namespace_and_composite_key(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.create("user-1", namespace_id="ns-birds")

        assert result.namespace_id == "ns-birds"
        assert result.session_id  # ULID generated

        # Verify the DDB item was written with composite key
        put_call = mock_table.put_item.call_args
        item = put_call[1]["Item"]
        assert item["namespaceId"] == "ns-birds"
        assert item["nsLastActiveAt"].startswith("ns-birds#")

    async def test_create_with_title(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.create("user-1", namespace_id="ns-birds", title="What are birds")

        assert result.title == "What are birds"
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["title"] == "What are birds"

    async def test_create_truncates_long_title(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        long_title = "x" * 200
        result = await store.create("user-1", namespace_id="ns", title=long_title)

        assert len(result.title) == 100
        item = mock_table.put_item.call_args[1]["Item"]
        assert len(item["title"]) == 100

    async def test_get_recent_for_namespace_queries_gsi(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {
            "Items": [
                {
                    "userId": "user-1",
                    "sessionId": "01ABC",
                    "namespaceId": "ns-birds",
                    "nsLastActiveAt": "ns-birds#2026-06-24T10:00:00Z",
                    "createdAt": "2026-06-24T09:00:00Z",
                    "lastActiveAt": "2026-06-24T10:00:00Z",
                    "status": "active",
                    "messageCount": 5,
                    "ttl": 9999999,
                    "title": "Bird questions",
                }
            ]
        }

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.get_recent_for_namespace("user-1", "ns-birds")

        assert result is not None
        assert result.session_id == "01ABC"
        assert result.title == "Bird questions"

        # Verify GSI query used begins_with
        query_kwargs = mock_table.query.call_args[1]
        assert query_kwargs["IndexName"] == "userId-nsLastActiveAt-index"
        assert ":nsp" in query_kwargs["ExpressionAttributeValues"]
        assert query_kwargs["ExpressionAttributeValues"][":nsp"] == "ns-birds#"

    async def test_get_recent_for_namespace_returns_none_when_empty(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {"Items": []}

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.get_recent_for_namespace("user-1", "ns-empty")

        assert result is None

    async def test_update_activity_maintains_composite_key(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        await store.update_activity("user-1", "session-abc", namespace_id="ns-birds")

        update_kwargs = mock_table.update_item.call_args[1]
        assert ":nsc" in update_kwargs["ExpressionAttributeValues"]
        assert update_kwargs["ExpressionAttributeValues"][":nsc"].startswith("ns-birds#")

    async def test_list_for_namespace_paginated(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {
            "Items": [
                {
                    "userId": "user-1",
                    "sessionId": "01ABC",
                    "namespaceId": "ns-birds",
                    "nsLastActiveAt": "ns-birds#2026-06-24T10:00:00Z",
                    "createdAt": "2026-06-24T09:00:00Z",
                    "lastActiveAt": "2026-06-24T10:00:00Z",
                    "status": "active",
                    "messageCount": 5,
                    "ttl": 9999999,
                    "title": "Bird questions",
                }
            ],
            "LastEvaluatedKey": {"userId": "user-1", "nsLastActiveAt": "ns-birds#2026-06-24T10:00:00Z"},
        }

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        sessions, next_token = await store.list_for_namespace("user-1", "ns-birds", limit=1)

        assert len(sessions) == 1
        assert sessions[0].title == "Bird questions"
        assert next_token is not None

    async def test_list_for_namespace_empty(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {"Items": []}

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        sessions, next_token = await store.list_for_namespace("user-1", "ns-empty")

        assert sessions == []
        assert next_token is None

    async def test_validate_ownership_found(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.get_item.return_value = {"Item": {"userId": "user-1"}}

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.validate_ownership("user-1", "session-abc")

        assert result is True

    async def test_validate_ownership_not_found(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.get_item.return_value = {}

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.validate_ownership("user-1", "session-xyz")

        assert result is False

    async def test_delete_session(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        await store.delete("user-1", "session-abc")

        mock_table.delete_item.assert_called_once_with(Key={"userId": "user-1", "sessionId": "session-abc"})

    async def test_get_recent_returns_most_recent(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {
            "Items": [
                {
                    "userId": "user-1",
                    "sessionId": "01DEF",
                    "createdAt": "2026-06-24T08:00:00Z",
                    "lastActiveAt": "2026-06-24T12:00:00Z",
                    "status": "active",
                    "messageCount": 10,
                    "ttl": 9999999,
                }
            ]
        }

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.get_recent("user-1")

        assert result is not None
        assert result.session_id == "01DEF"

    async def test_get_recent_returns_none_when_empty(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        mock_table.query.return_value = {"Items": []}

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        result = await store.get_recent("user-1")

        assert result is None

    async def test_update_title_idempotent(self, mock_table):
        from coa_serve.session_metadata import SessionMetadataStore

        store = SessionMetadataStore(table_name="test-table", region="us-east-1")
        await store.update_title("user-1", "session-abc", "My title")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":t"] == "My title"


# ---------------------------------------------------------------------------
# SessionManager.get_rich_history_page — pagination edge cases
# ---------------------------------------------------------------------------


class TestGetRichHistoryPage:
    @pytest.mark.asyncio
    async def test_first_page_of_many(self):
        """First page returns last N messages with hasMore=True."""
        from unittest.mock import MagicMock, patch

        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            # 10 turns = 20 messages (user + assistant pairs)
            turns = [
                [
                    {"role": "user", "content": {"text": f"q{i}"}},
                    {"role": "assistant", "content": {"text": f"a{i}"}},
                ]
                for i in range(10)
            ]
            mock_session.get_last_k_turns.return_value = list(reversed(turns))
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            page, total, has_more = await mgr.get_rich_history_page("u1", "s1", limit=5, offset=0)

            assert total == 20
            assert len(page) == 5
            assert has_more is True
            # Last 5 messages should be from turns 8-9
            assert page[-1]["content"] == "a9"

    @pytest.mark.asyncio
    async def test_last_page(self):
        """Offset reaching the beginning returns remaining messages with hasMore=False."""
        from unittest.mock import MagicMock, patch

        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            turns = [
                [
                    {"role": "user", "content": {"text": f"q{i}"}},
                    {"role": "assistant", "content": {"text": f"a{i}"}},
                ]
                for i in range(3)
            ]
            mock_session.get_last_k_turns.return_value = list(reversed(turns))
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            # 6 total messages, offset=5 means skip last 5, take up to 5 from start
            page, total, has_more = await mgr.get_rich_history_page("u1", "s1", limit=5, offset=5)

            assert total == 6
            assert len(page) == 1  # Only 1 message remains
            assert has_more is False
            assert page[0]["content"] == "q0"

    @pytest.mark.asyncio
    async def test_empty_session(self):
        """Empty session returns empty page with hasMore=False."""
        from unittest.mock import MagicMock, patch

        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            mock_session.get_last_k_turns.return_value = []
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            page, total, has_more = await mgr.get_rich_history_page("u1", "s1", limit=5, offset=0)

            assert total == 0
            assert len(page) == 0
            assert has_more is False

    @pytest.mark.asyncio
    async def test_offset_beyond_total(self):
        """Offset larger than total returns empty page."""
        from unittest.mock import MagicMock, patch

        with patch("coa_serve.session.MemorySessionManager") as MockMgr:
            mock_session = MagicMock()
            turns = [
                [
                    {"role": "user", "content": {"text": "q"}},
                    {"role": "assistant", "content": {"text": "a"}},
                ]
            ]
            mock_session.get_last_k_turns.return_value = list(reversed(turns))
            MockMgr.return_value.create_memory_session.return_value = mock_session

            mgr = SessionManager(memory_id="mem-123")
            page, total, has_more = await mgr.get_rich_history_page("u1", "s1", limit=5, offset=100)

            assert total == 2
            assert len(page) == 0
            assert has_more is False
