# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_serve.schema_visibility (results-only scrub, ADR)."""

from __future__ import annotations

import pytest
from coa_serve.models import ConfidenceScore, QueryResult, TraceStep
from coa_serve.schema_visibility import (
    FULL_VISIBILITY,
    SchemaVisibility,
    resolve_schema_visibility,
    scrub_graph_traverse_response,
    scrub_query_result,
    scrub_trace_step,
    scrub_translate_response,
)
from coa_serve.tier2.cedar_authorizer import RealCedarAuthorizer
from coa_serve.tier2.sql_firewall import SQLFirewall

_BLIND = SchemaVisibility(source=False, ontology=False)
_NS = "demo"


def _profile(role: str) -> dict:
    return {"userId": "u", "resourceRoles": [{"role": role, "resourceUID": _NS}]}


@pytest.mark.unit
class TestResolveSchemaVisibility:
    """Both rights come from the real seed policies through the fail-closed firewall."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("data-analyst", (False, False)),
            ("data-steward", (True, True)),
            ("namespace-owner", (True, True)),
            ("namespace-maintainer", (True, True)),
        ],
    )
    def test_seed_roles(self, role, expected):
        fw = SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=False))
        vis = resolve_schema_visibility(fw, _NS, _profile(role))
        assert (vis.source, vis.ontology) == expected

    def test_platform_viewer(self):
        fw = SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=False))
        assert resolve_schema_visibility(fw, _NS, {"userId": "v", "globalRoles": ["platform-viewer"]}).full

    def test_no_roles_fails_closed(self):
        fw = SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=False))
        assert resolve_schema_visibility(fw, _NS, {"userId": "x"}) == _BLIND


@pytest.mark.unit
class TestScrubFunctions:
    def test_trace_step_model_and_dict(self):
        step = TraceStep(step="s", status="ok", duration_ms=3, detail={"tables": ["t"]})
        assert scrub_trace_step(step, _BLIND).detail is None
        assert scrub_trace_step(step, _BLIND).duration_ms == 3
        assert scrub_trace_step({"step": "s", "detail": "x"}, _BLIND) == {"step": "s"}
        assert scrub_trace_step(step, FULL_VISIBILITY) is step

    def test_query_result_without_optional_fields(self):
        result = QueryResult(tier=1, confidence=ConfidenceScore(score=1.0, rationale="r"), trace=[])
        before = result.model_dump()
        scrub_query_result(result, _BLIND)
        assert result.model_dump() == before

    def test_error_responses_pass_through(self):
        err = {"error": "AccessDeniedError", "statusCode": 403}
        assert scrub_translate_response(err, _BLIND) is err
        assert scrub_graph_traverse_response(err, _BLIND) is err

    def test_translate_source_only_still_withholds_sparql(self):
        out = scrub_translate_response(
            {"sparqlQuery": "SELECT ?x", "trace": [{"step": "s", "detail": "d"}]},
            SchemaVisibility(source=True, ontology=False),
        )
        assert out["sparqlQuery"] == ""
        assert out["trace"] == [{"step": "s"}]


# ── Error text, answer redaction, replay (independent-review findings) ──────

_BASE = "https://integ.coa/ns-1/kg#"


@pytest.mark.unit
class TestClientErrorMessage:
    def test_ambiguous_reference_is_generic_without_view_source(self):
        from coa_serve.schema_visibility import client_error_message

        msg = "table 'orders' exists in more than one data source or schema ([ds-1/sales, ds-2/archive])"
        out = client_error_message("AmbiguousReferenceError", msg, SchemaVisibility(source=False, ontology=True))
        assert "orders" not in out and "ds-1" not in out

    def test_kept_for_source_viewer_and_other_errors(self):
        from coa_serve.schema_visibility import client_error_message

        msg = "table 'orders' is ambiguous"
        assert client_error_message("AmbiguousReferenceError", msg, SchemaVisibility(True, False)) == msg
        assert client_error_message("NoResultError", "No result", _BLIND) == "No result"


@pytest.mark.unit
class TestAnswerRedaction:
    def _result(self, **kw) -> QueryResult:
        return QueryResult(tier=3, confidence=ConfidenceScore(score=0.5, rationale="r"), trace=[], **kw)

    def test_withheld_identifiers_are_redacted_from_the_answer(self):
        result = self._result(
            synthesized_answer=(
                f"There are 50 customers. Classes: {_BASE}Customer, customers_email; table sales_db.customers."
            ),
            graph_context={
                "entities": [
                    {"uri": "urn:i:1", "type": f"{_BASE}Customer", "relationships": [f"{_BASE}customers_email"]}
                ]
            },
            metadata={"retrieved_tables": ["sales_db.customers"]},
        ).with_raw_artifacts(query_used="SELECT COUNT(*) FROM sales_db.customers")
        scrub_query_result(result, _BLIND)
        answer = result.synthesized_answer
        assert "50 customers" in answer  # plain words untouched
        assert _BASE not in answer and "customers_email" not in answer and "sales_db.customers" not in answer

    def test_fragment_iris_redacted_even_if_not_in_result(self):
        """Planner prompts can carry ontology terms that never reach graphContext."""
        result = self._result(synthesized_answer=f"Edge types: <{_BASE}hasOrder> and https://x.example/onto#Shipment.")
        scrub_query_result(result, SchemaVisibility(source=True, ontology=False))
        assert "#hasOrder" not in result.synthesized_answer and "#Shipment" not in result.synthesized_answer

    def test_constant_row_values_echoing_schema_are_redacted(self):
        result = self._result(result_rows=[{"v": "sales_db.customers"}, {"v": 42}]).with_raw_artifacts(
            query_used="SELECT 'x' FROM sales_db.customers"
        )
        scrub_query_result(result, _BLIND)
        assert result.result_rows == [{"v": "[redacted]"}, {"v": 42}]

    def test_full_visibility_untouched(self):
        result = self._result(
            synthesized_answer=f"{_BASE}Customer", graph_context={"entities": [{"type": f"{_BASE}Customer"}]}
        )
        scrub_query_result(result, SchemaVisibility(True, True))
        assert result.synthesized_answer == f"{_BASE}Customer"


@pytest.mark.unit
class TestSessionReplayScrub:
    def test_stored_turn_rescrubbed_with_current_rights(self):
        from coa_serve.schema_visibility import scrub_session_messages

        stored = [
            {"role": "user", "content": "how many?"},
            {
                "role": "assistant",
                "content": f"50, see {_BASE}Customer in sales_db.customers",
                "metadata": {
                    "queryUsed": "SELECT COUNT(*) FROM sales_db.customers",
                    "sparqlGenerated": f"SELECT ?x WHERE {{ ?x a <{_BASE}Customer> }}",
                    "graphContext": {"entities": [{"type": f"{_BASE}Customer"}]},
                    "metadata": {"retrieved_tables": ["sales_db.customers"], "columns": ["n"]},
                    "trace": [{"step": "t2.sql.generate", "detail": {"tables": ["sales_db.customers"]}}],
                    "resultRows": [{"n": 50}],
                },
            },
        ]
        out = scrub_session_messages(stored, _BLIND)
        assert out[0] == stored[0]
        meta = out[1]["metadata"]
        assert "queryUsed" not in meta and "sparqlGenerated" not in meta
        assert meta["graphContext"] == {"entities": []}
        assert meta["metadata"] == {"retrieved_tables": [], "columns": ["n"]}
        assert meta["trace"] == [{"step": "t2.sql.generate"}]
        assert meta["resultRows"] == [{"n": 50}]
        assert "sales_db.customers" not in out[1]["content"] and _BASE not in out[1]["content"]

    def test_full_visibility_replays_as_stored(self):
        from coa_serve.schema_visibility import scrub_session_messages

        stored = [{"role": "assistant", "content": "x", "metadata": {"queryUsed": "SELECT 1 FROM t"}}]
        assert scrub_session_messages(stored, SchemaVisibility(True, True)) is stored


@pytest.mark.unit
class TestSynthesizerSchemaBlindDirective:
    def test_directive_only_when_schema_blind(self):
        from coa_serve.schema_visibility import SCHEMA_BLIND
        from coa_serve.tier3.synthesizer import Synthesizer

        synth = Synthesizer.__new__(Synthesizer)
        token = SCHEMA_BLIND.set(True)
        try:
            blind = synth._build_system_instruction("nonce", "^")
        finally:
            SCHEMA_BLIND.reset(token)
        normal = synth._build_system_instruction("nonce", "^")
        assert "SCHEMA VISIBILITY" in blind
        assert "SCHEMA VISIBILITY" not in normal


# ── Redaction precision, rows, supporting content, replay parity ─────────────

_SOURCE_ONLY_HIDDEN = SchemaVisibility(source=False, ontology=True)
_ONTOLOGY_ONLY_HIDDEN = SchemaVisibility(source=True, ontology=False)


def _t3(**kw) -> QueryResult:
    return QueryResult(tier=3, confidence=ConfidenceScore(score=0.5, rationale="r"), trace=[], **kw)


@pytest.mark.unit
class TestRowRedaction:
    def test_fragment_iris_in_rows_redacted_without_any_collected_identifier(self):
        """VKG class-listing rows: no identifier was collected, the fragment IRI still goes."""
        rows = [{"c": f"{_BASE}Customer", "n": 3}, {"c": {"nested": [f"<{_BASE}Order>"]}}]
        result = QueryResult(tier=2, confidence=ConfidenceScore(score=1, rationale="r"), trace=[], result_rows=rows)
        scrub_query_result(result, _ONTOLOGY_ONLY_HIDDEN)
        assert result.result_rows == [{"c": "[redacted]", "n": 3}, {"c": {"nested": ["[redacted]"]}}]

    def test_rows_kept_for_full_visibility(self):
        rows = [{"c": f"{_BASE}Customer"}]
        result = QueryResult(tier=2, confidence=ConfidenceScore(score=1, rationale="r"), trace=[], result_rows=rows)
        scrub_query_result(result, FULL_VISIBILITY)
        assert result.result_rows == [{"c": f"{_BASE}Customer"}]

    def test_sql_identifiers_redacted_case_insensitively(self):
        result = _t3(
            synthesized_answer="Counted rows in SALES_DB.Customers.", result_rows=[{"t": "Sales_Db.CUSTOMERS"}]
        ).with_raw_artifacts(query_used="SELECT COUNT(*) FROM sales_db.customers")
        scrub_query_result(result, _SOURCE_ONLY_HIDDEN)
        assert "customers" not in result.synthesized_answer.lower()
        assert result.result_rows == [{"t": "[redacted]"}]

    def test_source_identifiers_kept_for_a_source_viewer(self):
        result = _t3(synthesized_answer="Counted rows in sales_db.customers.").with_raw_artifacts(
            query_used="SELECT COUNT(*) FROM sales_db.customers"
        )
        scrub_query_result(result, _ONTOLOGY_ONLY_HIDDEN)
        assert "sales_db.customers" in result.synthesized_answer


@pytest.mark.unit
class TestGraphContextPrecision:
    def test_instance_local_name_survives_class_iri_redacted(self):
        instance = "https://integ.coa/ns-1/data/Order_1001"
        result = _t3(
            synthesized_answer=f"Order_1001 is an order (class https://integ.coa/ns-1/onto/OrderLine) at {instance}.",
            graph_context={
                "entities": [
                    {
                        "uri": instance,
                        "type": "https://integ.coa/ns-1/onto/OrderLine",
                        "relationships": [
                            {"predicate": "https://integ.coa/ns-1/onto/placed_by", "target_uri": "urn:c:Cust_77"}
                        ],
                    }
                ]
            },
            result_rows=[{"id": "Order_1001", "edge": "placed_by", "who": "Cust_77"}],
        )
        scrub_query_result(result, _ONTOLOGY_ONLY_HIDDEN)
        assert "Order_1001" in result.synthesized_answer
        assert "OrderLine" not in result.synthesized_answer
        assert result.result_rows == [{"id": "Order_1001", "edge": "[redacted]", "who": "Cust_77"}]

    def test_canonical_top_level_relationship_predicate_redacted(self):
        # The assembled graph context lists relationships at the top level with
        # ``predicateUri`` (response_assembler), not nested under each entity.
        instance = "https://integ.coa/ns-1/data/Order_1001"
        result = _t3(
            synthesized_answer="Order_1001 is linked to Cust_77 via placed_by.",
            graph_context={
                "entities": [{"uri": instance, "type": "https://integ.coa/ns-1/onto/OrderLine"}],
                "relationships": [
                    {
                        "sourceUri": instance,
                        "predicateUri": "https://integ.coa/ns-1/onto/placed_by",
                        "targetUri": "urn:c:Cust_77",
                    }
                ],
            },
            result_rows=[{"id": "Order_1001", "edge": "placed_by"}],
        )
        scrub_query_result(result, _ONTOLOGY_ONLY_HIDDEN)
        assert "placed_by" not in result.synthesized_answer
        assert "Order_1001" in result.synthesized_answer
        assert result.result_rows == [{"id": "Order_1001", "edge": "[redacted]"}]


@pytest.mark.unit
class TestSupportingContent:
    _CHUNKS = [
        {"chunkId": f"{_BASE}Customer#stmt0", "text": f"<{_BASE}Customer> has email", "label": f"{_BASE}Customer"},
        {"chunkId": "doc-17", "text": "Plain glossary text.", "label": "Glossary"},
    ]

    def test_blind_caller_gets_redacted_text_and_opaque_graph_chunk_ids(self):
        import json

        result = _t3(supporting_content=[dict(c) for c in self._CHUNKS])
        scrub_query_result(result, _BLIND)
        dumped = json.dumps(result.model_dump(by_alias=True, exclude_none=True))
        assert _BASE not in dumped and "#stmt0" not in dumped
        graph, doc = result.supporting_content
        assert graph["chunkId"] == "graph-chunk-0" and graph["text"] == "[redacted] has email"
        assert doc == self._CHUNKS[1]  # non-graph chunk id and plain text kept

    def test_full_visibility_positive_control(self):
        result = _t3(supporting_content=[dict(c) for c in self._CHUNKS])
        scrub_query_result(result, FULL_VISIBILITY)
        assert result.supporting_content == self._CHUNKS


@pytest.mark.unit
class TestIdentifierCap:
    def test_above_cap_answer_rows_and_support_withheld_whole(self):
        from coa_serve import schema_visibility as sv

        sql = "SELECT 1 FROM " + ", ".join(f"db.table_{i}" for i in range(sv._MAX_IDENTIFIERS + 1))
        result = _t3(
            synthesized_answer="db.table_3 has rows", result_rows=[{"v": "x"}], supporting_content=[{"text": "t"}]
        ).with_raw_artifacts(query_used=sql)
        scrub_query_result(result, _SOURCE_ONLY_HIDDEN)
        assert "db.table_3" not in result.synthesized_answer
        assert result.result_rows == [] and result.supporting_content == []

    def test_redact_identifiers_longest_match_wins(self):
        from coa_serve.schema_visibility import redact_identifiers

        out = redact_identifiers("see sales_db.customers and customers_email", {"sales_db", "sales_db.customers"})
        assert out == "see [redacted] and customers_email"


@pytest.mark.unit
class TestReplayParity:
    def _full_result(self) -> QueryResult:
        return QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=0.5, rationale="r"),
            trace=[TraceStep(step="t2.sql.generate", status="ok", duration_ms=1, detail={"t": "sales_db.c"})],
            result_rows=[{"v": "sales_db.customers", "c": f"{_BASE}Customer"}],
            query_used="SELECT v FROM sales_db.customers",
            sparql_generated=f"SELECT ?x WHERE {{ ?x a <{_BASE}Customer> }}",
            graph_context={"entities": [{"type": f"{_BASE}Customer"}]},
            supporting_content=[{"chunkId": f"{_BASE}Customer#stmt1", "text": "sales_db.customers"}],
            metadata={"retrieved_tables": ["sales_db.customers"], "expanded_tables": [], "columns": ["v"]},
        )

    @pytest.mark.parametrize("vis", [_BLIND, _SOURCE_ONLY_HIDDEN, _ONTOLOGY_ONLY_HIDDEN])
    def test_replay_serializes_exactly_as_the_live_scrub(self, vis):
        from coa_serve.schema_visibility import scrub_session_messages

        stored = self._full_result().model_dump(by_alias=True, exclude_none=True)
        live = self._full_result()
        scrub_query_result(live, vis)
        replayed = scrub_session_messages([{"role": "assistant", "content": "", "metadata": stored}], vis)
        replay_dump = QueryResult.model_validate(replayed[0]["metadata"]).model_dump(by_alias=True, exclude_none=True)
        assert replay_dump == live.model_dump(by_alias=True, exclude_none=True)

    def test_assistant_turn_without_metadata_still_redacted(self):
        from coa_serve.schema_visibility import scrub_session_messages

        msgs = [{"role": "assistant", "content": f"See <{_BASE}Customer>."}, {"role": "user", "content": _BASE + "X"}]
        out = scrub_session_messages(msgs, _ONTOLOGY_ONLY_HIDDEN)
        assert out[0]["content"] == "See [redacted]."
        assert out[1] == msgs[1]  # the caller's own text is not touched

    def test_replayed_rows_get_fragment_iri_redaction(self):
        from coa_serve.schema_visibility import scrub_session_messages

        msgs = [{"role": "assistant", "content": "x", "metadata": {"resultRows": [{"c": f"{_BASE}Customer"}]}}]
        out = scrub_session_messages(msgs, _ONTOLOGY_ONLY_HIDDEN)
        assert out[0]["metadata"]["resultRows"] == [{"c": "[redacted]"}]


@pytest.mark.unit
class TestVisibilityScope:
    def test_scope_sets_and_resets(self):
        from coa_serve.schema_visibility import (
            NO_VISIBILITY,
            SCHEMA_BLIND,
            request_schema_visibility,
            schema_visibility_scope,
        )

        assert request_schema_visibility("ns") is None and SCHEMA_BLIND.get() is False
        with schema_visibility_scope("ns", NO_VISIBILITY):
            assert request_schema_visibility("ns") == NO_VISIBILITY
            assert request_schema_visibility("other") is None
            assert SCHEMA_BLIND.get() is True
        assert request_schema_visibility("ns") is None and SCHEMA_BLIND.get() is False


# A schema context as the SQL writer receives it (class-text / graph-walk line shape).
_PROMPT_SCHEMA = (
    "Table: orders | Database: sales_db (write it as sales_db.orders) | "
    "Columns: order_id:int, status:string (one of: shipped, pending)\n"
    "Table: customers (label: Customers) | Columns: customer_ledger:string [label: Ledger]\n"
    "Table: invoices | Columns: amount:double\n"
    "Table: payments | Columns: method:string"
)
_SOURCE_HIDDEN = SchemaVisibility(source=False, ontology=True)


def _scrub_with_prompt(sql: str, rows: list, answer: str, vis: SchemaVisibility = _SOURCE_HIDDEN) -> QueryResult:
    from coa_serve.schema_visibility import note_prompt_schema, schema_visibility_scope

    result = QueryResult(
        tier=2,
        confidence=ConfidenceScore(score=0.9, rationale="r"),
        trace=[],
        result_rows=rows,
        synthesized_answer=answer,
    ).with_raw_artifacts(query_used=sql)
    with schema_visibility_scope("ns", vis):
        note_prompt_schema(_PROMPT_SCHEMA)
        scrub_query_result(result, vis)
    return result


@pytest.mark.unit
class TestConstantSmuggling:
    """A constant beside a real projection can carry names the model was shown (MR review)."""

    def test_mixed_projection_keeps_the_data_and_redacts_the_names(self):
        result = _scrub_with_prompt(
            "SELECT COUNT(*) AS n, 'customers, invoices, payments' AS t FROM orders",
            [{"n": 1203, "t": "customers, invoices, payments"}],
            "There are 1203 orders. You can also use customers, invoices and Payments.",
        )
        assert result.result_rows == [{"n": 1203, "t": "[redacted], [redacted], [redacted]"}]
        answer = result.synthesized_answer
        assert "1203" in answer
        assert not any(name in answer.lower() for name in ("customers", "invoices", "payments"))

    @pytest.mark.parametrize(
        ("sql", "value"),
        [
            ("SELECT CASE WHEN order_id IS NULL THEN 'invoices' ELSE 'invoices' END AS c FROM orders", "invoices"),
            ("SELECT CONCAT(CAST(order_id AS VARCHAR), ' customer_ledger') AS c FROM orders", "7 customer_ledger"),
            ("SELECT 1 AS k, 'method' AS c FROM orders UNION ALL SELECT order_id, status FROM orders", "method"),
        ],
    )
    def test_constant_derived_variants(self, sql, value):
        result = _scrub_with_prompt(sql, [{"k": 1, "c": value}] if "UNION" in sql else [{"c": value}], "ok")
        cell = result.result_rows[0]["c"]
        assert "[redacted]" in cell
        assert not any(n in cell for n in ("invoices", "customer_ledger", "method"))

    def test_data_column_equal_to_a_schema_name_is_untouched(self):
        rows = [{"status": "payments", "n": 2}]
        result = _scrub_with_prompt("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", rows, "payments: 2")
        assert result.result_rows == rows
        assert result.synthesized_answer == "payments: 2"

    def test_plain_words_in_constant_values_survive(self):
        result = _scrub_with_prompt(
            "SELECT CASE WHEN amount > 10 THEN 'High' ELSE 'Low' END AS band, COUNT(*) AS n FROM invoices GROUP BY 1",
            [{"band": "High", "n": 4}],
            "4 High invoices",
        )
        assert result.result_rows == [{"band": "High", "n": 4}]
        assert result.synthesized_answer == "4 High invoices"

    def test_not_answerable_refusal_keeps_its_prefix_and_redacts_names(self):
        # The refusal row is exempt from the firewall's echo guard; this scrub is
        # what keeps schema names the writer puts in its message from the analyst.
        message = "NOT_ANSWERABLE_RANGE: March 2031 is outside the data; invoices covers 2026-01 to 2029-12"
        result = _scrub_with_prompt(
            f"SELECT '{message}' AS answer",
            [{"answer": message}],
            "March 2031 is outside the data; invoices covers 2026-01 to 2029-12.",
        )
        cell = result.result_rows[0]["answer"]
        assert cell.startswith("NOT_ANSWERABLE_RANGE:")
        assert "invoices" not in cell and "[redacted]" in cell
        assert "invoices" not in result.synthesized_answer

    def test_unmappable_star_beside_a_constant_fails_closed(self):
        result = _scrub_with_prompt(
            "SELECT *, 'payments' AS t FROM orders", [{"order_id": 1, "status": "invoices", "t": "payments"}], "x"
        )
        assert result.result_rows == [{"order_id": 1, "status": "[redacted]", "t": "[redacted]"}]

    def test_unparseable_sql_fails_closed(self):
        result = _scrub_with_prompt("SELEC broken ((", [{"c": "payments"}], "x")
        assert result.result_rows == [{"c": "[redacted]"}]

    def test_caller_with_view_source_sees_everything(self):
        rows = [{"n": 1, "t": "customers"}]
        result = _scrub_with_prompt(
            "SELECT COUNT(*) AS n, 'customers' AS t FROM orders", rows, "customers", vis=SchemaVisibility(True, False)
        )
        assert result.result_rows == rows and result.synthesized_answer == "customers"

    def test_names_in_the_executed_sql_count_without_a_prompt(self):
        """The stored-turn path has no prompt record: the SQL's own references still apply."""
        result = QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=1, rationale="r"),
            trace=[],
            result_rows=[{"n": 1, "t": "order_id"}],
        ).with_raw_artifacts(query_used="SELECT COUNT(order_id) AS n, 'order_id' AS t FROM orders")
        scrub_query_result(result, _SOURCE_HIDDEN)
        assert result.result_rows == [{"n": 1, "t": "[redacted]"}]

    def test_replayed_turn_gets_the_same_redaction(self):
        from coa_serve.schema_visibility import scrub_session_messages

        meta = {
            "queryUsed": "SELECT COUNT(*) AS n, 'customers, invoices' AS t FROM orders",
            "metadata": {"retrieved_tables": ["sales_db.customers", "sales_db.invoices"]},
            "resultRows": [{"n": 3, "t": "customers, invoices"}],
            "synthesizedAnswer": "3 orders; see customers and invoices",
        }
        msgs = [{"role": "assistant", "content": "3 orders; see customers and invoices", "metadata": meta}]
        out = scrub_session_messages(msgs, _SOURCE_HIDDEN)[0]
        assert out["metadata"]["resultRows"] == [{"n": 3, "t": "[redacted], [redacted]"}]
        assert out["metadata"]["synthesizedAnswer"] == "3 orders; see [redacted] and [redacted]"
        assert out["content"] == "3 orders; see [redacted] and [redacted]"
        assert "queryUsed" not in out["metadata"]


@pytest.mark.unit
class TestPromptSchemaNames:
    def test_parses_tables_databases_and_columns(self):
        from coa_serve.schema_visibility import schema_names_in_text

        names = schema_names_in_text(_PROMPT_SCHEMA)
        assert {
            "orders",
            "sales_db",
            "order_id",
            "status",
            "customers",
            "customer_ledger",
            "invoices",
            "amount",
            "payments",
            "method",
        } <= names
        # Labels, descriptions and type names are not schema identifiers.
        assert not {"ledger", "shipped", "pending", "int", "string", "double"} & names

    def test_parses_agent_tool_json(self):
        from coa_serve.schema_visibility import schema_names_in_text

        text = (
            '{"tables": [{"table": "fin.ledger_entries", "preview": "Table: ledger_entries"}], "column": "posted_at"}'
        )
        assert {"fin", "ledger_entries", "posted_at"} <= schema_names_in_text(text)

    def test_noting_is_a_no_op_unless_source_is_withheld(self):
        from coa_serve.schema_visibility import (
            FULL_VISIBILITY,
            note_prompt_schema,
            prompt_schema_names,
            schema_visibility_scope,
        )

        note_prompt_schema(_PROMPT_SCHEMA)
        assert prompt_schema_names() == frozenset()
        with schema_visibility_scope("ns", SchemaVisibility(source=True, ontology=False)):
            note_prompt_schema(_PROMPT_SCHEMA)
            assert prompt_schema_names() == frozenset()
        with schema_visibility_scope("ns", FULL_VISIBILITY):
            note_prompt_schema(_PROMPT_SCHEMA)
            assert prompt_schema_names() == frozenset()
        with schema_visibility_scope("ns", _SOURCE_HIDDEN):
            note_prompt_schema(_PROMPT_SCHEMA)
            assert "customers" in prompt_schema_names()
        assert prompt_schema_names() == frozenset()

    def test_names_noted_in_a_child_task_reach_the_scrub(self):
        import asyncio

        from coa_serve.schema_visibility import note_prompt_schema, prompt_schema_names, schema_visibility_scope

        async def _run() -> frozenset[str]:
            with schema_visibility_scope("ns", _SOURCE_HIDDEN):
                await asyncio.create_task(asyncio.to_thread(note_prompt_schema, "Table: payments"))
                return prompt_schema_names()

        assert "payments" in asyncio.run(_run())

    def test_bedrock_prompts_are_recorded(self):
        from coa_serve.clients.bedrock import BedrockLLMClient
        from coa_serve.schema_visibility import prompt_schema_names, schema_visibility_scope

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        with schema_visibility_scope("ns", _SOURCE_HIDDEN):
            client._build_converse_request(
                "## Schema\nTable: payments | Columns: method:string", system="Table: invoices"
            )
            assert {"payments", "method", "invoices"} <= prompt_schema_names()
