# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for request/response models."""

import json

import pytest
from coa_serve.models import (
    ConfidenceScore,
    InvokeRequest,
    InvokeResponse,
    QueryResult,
    TraceStep,
)
from pydantic import ValidationError


@pytest.mark.unit
class TestInvokeRequest:
    def test_valid_request(self):
        req = InvokeRequest(query="What is revenue?", namespace="demo")
        assert req.query == "What is revenue?"
        assert req.namespace == "demo"
        assert req.profile == {}
        assert req.options == {}

    def test_request_with_all_fields(self):
        req = InvokeRequest(
            query="test",
            namespace="ns1",
            profile={"user_id": "u1"},
            options={"tierOverride": 1},
        )
        assert req.profile == {"user_id": "u1"}
        assert req.options == {"tierOverride": 1}

    def test_query_max_length(self):
        req = InvokeRequest(query="x" * 4000, namespace="demo")
        assert len(req.query) == 4000

    def test_query_exceeds_max_length(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="x" * 4001, namespace="demo")

    def test_empty_query_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="", namespace="demo")

    def test_whitespace_only_query_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="   ", namespace="demo")

    def test_tabs_and_newlines_only_query_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="\t\n  \r\n", namespace="demo")

    def test_query_with_leading_trailing_whitespace_stripped(self):
        req = InvokeRequest(query="  hello world  ", namespace="demo")
        assert req.query == "hello world"

    def test_missing_query_raises(self):
        with pytest.raises(ValidationError):
            InvokeRequest(namespace="demo")

    def test_missing_namespace_raises(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test")

    def test_empty_namespace_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="")

    def test_namespace_with_spaces_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="bad namespace")

    def test_namespace_with_special_chars_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="ns/../../etc")

    def test_valid_namespace_patterns(self):
        for ns in ["demo", "my-namespace", "ns_123", "Production-01"]:
            req = InvokeRequest(query="test", namespace=ns)
            assert req.namespace == ns

    def test_namespace_at_max_length(self):
        ns = "a" * 128
        req = InvokeRequest(query="test", namespace=ns)
        assert len(req.namespace) == 128

    def test_namespace_exceeds_max_length(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="a" * 129)

    def test_namespace_leading_hyphen_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="-leading")

    def test_namespace_leading_underscore_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="_leading")

    def test_namespace_with_null_byte_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="ns\x00evil")

    def test_namespace_with_unicode_rejected(self):
        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="ns​evil")


@pytest.mark.unit
class TestRetrieverStrategyValidation:
    """Model-layer validation of ``options.retrieverStrategy`` (task 4)."""

    def test_valid_retriever_strategy_accepted(self):
        from coa_serve.lexical.strategies import RetrieverStrategy

        for strategy in RetrieverStrategy:
            req = InvokeRequest(
                query="test",
                namespace="demo",
                options={"retrieverStrategy": strategy.value},
            )
            assert req.options["retrieverStrategy"] == strategy.value

    def test_default_strategy_value_accepted(self):
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"retrieverStrategy": "chunk_based_semantic"},
        )
        assert req.options["retrieverStrategy"] == "chunk_based_semantic"

    def test_invalid_retriever_strategy_raises(self):
        with pytest.raises(ValidationError):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"retrieverStrategy": "not-a-real-strategy"},
            )

    def test_empty_retriever_strategy_raises(self):
        with pytest.raises(ValidationError):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"retrieverStrategy": ""},
            )

    def test_absent_retriever_strategy_is_valid(self):
        # Key absent entirely -> valid.
        req = InvokeRequest(query="test", namespace="demo", options={})
        assert "retrieverStrategy" not in req.options

    def test_other_options_keys_unaffected(self):
        # Absence of retrieverStrategy among other option keys is still valid,
        # and the key stays inside ``options`` (no new top-level field).
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"tierOverride": 3},
        )
        assert req.options == {"tierOverride": 3}


@pytest.mark.unit
class TestDimensionsNormalization:
    """``options.dimensions`` wire-shape normalization (issue #53).

    The contract declares a LIST of ``DimensionFilter`` objects; Tier-1 binds
    placeholders from a name->value MAPPING. These pin the conversion at the
    boundary, and that malformed input is a 400-path ValueError rather than an
    AttributeError deeper in (which surfaced as a blanket 500).
    """

    def test_contract_shape_list_normalized_to_mapping(self):
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA", "operator": "="}]},
        )
        assert req.options["dimensions"] == {"region": "EMEA"}

    def test_multiple_filters_normalized(self):
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA"}, {"name": "year", "value": "2024"}]},
        )
        assert req.options["dimensions"] == {"region": "EMEA", "year": "2024"}

    def test_operator_omitted_defaults_to_equality(self):
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA"}]},
        )
        assert req.options["dimensions"] == {"region": "EMEA"}

    def test_dict_shape_passed_through_unchanged(self):
        # The internal shape stays valid — substitute_dimensions' own contract.
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": {"region": "EMEA"}},
        )
        assert req.options["dimensions"] == {"region": "EMEA"}

    def test_non_string_value_preserved(self):
        # substitute_dimensions renders ints/bools as typed SQL literals.
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": [{"name": "year", "value": 2024}]},
        )
        assert req.options["dimensions"] == {"year": 2024}

    def test_empty_list_drops_the_key(self):
        # Absent-equivalent: Tier-1 must take the no-dimensions path.
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": []})
        assert "dimensions" not in req.options

    def test_none_drops_the_key(self):
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": None})
        assert "dimensions" not in req.options

    def test_absent_dimensions_is_valid(self):
        req = InvokeRequest(query="test", namespace="demo", options={})
        assert "dimensions" not in req.options

    def test_non_equality_operator_rejected(self):
        # Silently treating '>' as '=' would return a WRONG answer with a 200.
        with pytest.raises(ValidationError, match="operator"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "year", "value": "2024", "operator": ">"}]},
            )

    def test_duplicate_dimension_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "region", "value": "EMEA"}, {"name": "region", "value": "APAC"}]},
            )

    def test_duplicate_dimension_case_insensitive_rejected(self):
        # substitute_dimensions lowercases keys, so these collide downstream.
        with pytest.raises(ValidationError, match="Duplicate"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "Region", "value": "EMEA"}, {"name": "region", "value": "APAC"}]},
            )

    def test_list_of_bare_strings_rejected(self):
        with pytest.raises(ValidationError, match="dimension filter"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": ["region", "year"]})

    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"value": "EMEA"}]})

    def test_blank_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "  ", "value": "EMEA"}]})

    def test_missing_value_rejected(self):
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "region"}]})

    def test_scalar_dimensions_rejected(self):
        with pytest.raises(ValidationError, match="dimensions"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": "region=EMEA"})

    def test_null_value_rejected(self):
        # substitute_dimensions would bind the literal string 'None' — a filter on
        # the wrong value, returned with a 200.
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "region", "value": None}]})

    def test_object_value_rejected(self):
        # A Python repr would otherwise leak into the SQL literal.
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(
                query="test", namespace="demo", options={"dimensions": [{"name": "region", "value": {"a": 1}}]}
            )

    def test_array_value_rejected(self):
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(
                query="test", namespace="demo", options={"dimensions": [{"name": "region", "value": ["A", "B"]}]}
            )

    def test_name_is_trimmed(self):
        # An untrimmed key never matches the ':region' placeholder, so the filter
        # would be silently dropped and Tier-1 bypassed as if unfiltered.
        req = InvokeRequest(
            query="test", namespace="demo", options={"dimensions": [{"name": "  region  ", "value": "EMEA"}]}
        )
        assert req.options["dimensions"] == {"region": "EMEA"}

    def test_duplicate_detected_after_trimming(self):
        with pytest.raises(ValidationError, match="Duplicate"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": " region", "value": "EMEA"}, {"name": "region ", "value": "APAC"}]},
            )

    def test_empty_dict_drops_the_key(self):
        # Absent-equivalent, same as an empty list.
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": {}})
        assert "dimensions" not in req.options

    # --- the dict shape gets the SAME per-entry checks as the list shape ---
    # A dict reaches here from internal callers AND over both transports (the SSE
    # path forwards options verbatim; the REST handler forwards a top-level
    # "dimensions" object unchanged, and there is no API Gateway request
    # validator). A free pass would let it bind 'None' or a Python repr into SQL.

    def test_dict_shape_null_value_rejected(self):
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"region": None}})

    def test_dict_shape_object_value_rejected(self):
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"region": {"a": 1}}})

    def test_dict_shape_array_value_rejected(self):
        with pytest.raises(ValidationError, match="value"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"region": ["A", "B"]}})

    def test_dict_shape_duplicate_case_insensitive_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"Region": "EMEA", "region": "APAC"}})

    def test_dict_shape_blank_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"  ": "EMEA"}})

    def test_dict_shape_name_is_trimmed(self):
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": {"  region  ": "EMEA"}})
        assert req.options["dimensions"] == {"region": "EMEA"}

    # --- numeric edge cases ---

    def test_nan_value_rejected(self):
        # json.loads accepts the non-standard NaN/Infinity tokens, and sqlglot
        # renders them as the BARE words nan/inf — which SQL engines parse as a
        # COLUMN reference, silently comparing two columns instead of filtering.
        for token in ("NaN", "Infinity", "-Infinity"):
            value = json.loads(f'{{"v": {token}}}')["v"]
            with pytest.raises(ValidationError, match="finite"):
                InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "n", "value": value}]})

    def test_finite_float_value_accepted(self):
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "rate", "value": 1.5}]})
        assert req.options["dimensions"] == {"rate": 1.5}

    def test_bool_value_accepted(self):
        # bool is checked BEFORE int/float: bool IS an int in Python, and
        # isfinite() must not run on it. substitute_dimensions renders TRUE/FALSE.
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "active", "value": True}]})
        assert req.options["dimensions"] == {"active": True}

    # --- operator ---

    def test_blank_operator_treated_as_absent(self):
        # The contract documents operator as optional (defaulting to equality), and
        # a generated client may serialize an unset optional string as "" — that
        # must not 400.
        for operator in ("", "  ", "=", " = "):
            req = InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "region", "value": "EMEA", "operator": operator}]},
            )
            assert req.options["dimensions"] == {"region": "EMEA"}

    def test_non_string_operator_rejected(self):
        with pytest.raises(ValidationError, match="operator"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "region", "value": "EMEA", "operator": 5}]},
            )

    def test_word_operator_rejected(self):
        with pytest.raises(ValidationError, match="operator"):
            InvokeRequest(
                query="test",
                namespace="demo",
                options={"dimensions": [{"name": "region", "value": "EMEA", "operator": "eq"}]},
            )

    def test_null_operator_treated_as_absent(self):
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA", "operator": None}]},
        )
        assert req.options["dimensions"] == {"region": "EMEA"}

    def test_coexists_with_other_option_validators(self):
        # Four @field_validator("options") hooks chain on the same field; the
        # dimensions one must not drop the keys the others validate.
        req = InvokeRequest(
            query="test",
            namespace="demo",
            options={
                "dimensions": [{"name": "region", "value": "EMEA"}],
                "mode": "deep-reasoning",
                "excludeTools": ["vector_search"],
                "retrieverStrategy": "chunk_based_semantic",
                "tierOverride": 1,
            },
        )
        assert req.options["dimensions"] == {"region": "EMEA"}
        assert req.options["mode"] == "deep-reasoning"
        assert req.options["excludeTools"] == ["vector_search"]
        assert req.options["retrieverStrategy"] == "chunk_based_semantic"
        assert req.options["tierOverride"] == 1

    # --- bounds on a single filter (options is a free-form dict) ---

    def test_oversized_name_rejected(self):
        with pytest.raises(ValidationError, match="at most 128 characters"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "n" * 129, "value": "v"}]})

    def test_name_at_max_length_accepted(self):
        name = "n" * 128
        req = InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": name, "value": "v"}]})
        assert req.options["dimensions"] == {name: "v"}

    def test_oversized_value_rejected(self):
        with pytest.raises(ValidationError, match="at most 4000 characters"):
            InvokeRequest(
                query="test", namespace="demo", options={"dimensions": [{"name": "region", "value": "v" * 4001}]}
            )

    def test_value_at_max_length_accepted(self):
        value = "v" * 4000
        req = InvokeRequest(
            query="test", namespace="demo", options={"dimensions": [{"name": "region", "value": value}]}
        )
        assert req.options["dimensions"] == {"region": value}

    def test_oversized_name_rejected_in_dict_shape(self):
        with pytest.raises(ValidationError, match="at most 128 characters"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": {"n" * 129: "v"}})

    def test_error_message_does_not_echo_unbounded_input(self):
        # The rejected value is attacker-controlled and unbounded, so interpolating
        # it whole would let a caller inflate the exception — and anything derived
        # from it — to the size of their payload.
        payload = {"dimensions": [{"name": "region", "value": {"k": "A" * 100_000}}]}
        with pytest.raises(ValidationError) as exc:
            InvokeRequest(query="test", namespace="demo", options=payload)
        rendered = str(exc.value)
        assert "truncated" in rendered
        # Bounded well below the input size. Pydantic appends its own input_value
        # echo, so this asserts an order of magnitude, not an exact ceiling.
        assert len(rendered) < 5_000, len(rendered)

    def test_missing_name_and_missing_value_are_distinct_messages(self):
        # An ABSENT field must not be reported as a malformed one: "'name' is
        # required" tells the caller to add it, where "must be a non-empty string"
        # would suggest what they sent was the wrong shape.
        with pytest.raises(ValidationError, match="'name' is required"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"value": "EMEA"}]})
        with pytest.raises(ValidationError, match="'value' is required"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": "region"}]})

    def test_present_but_invalid_name_reports_shape_not_absence(self):
        with pytest.raises(ValidationError, match="must be a non-empty string"):
            InvokeRequest(query="test", namespace="demo", options={"dimensions": [{"name": 5, "value": "EMEA"}]})

    def test_callers_options_dict_not_mutated(self):
        # The validator must not rewrite the dict the caller passed in: main.py
        # hands over payload["options"], and a retry or a log of the original
        # payload would otherwise see the normalized shape.
        caller_options = {"dimensions": [{"name": "region", "value": "EMEA"}]}
        req = InvokeRequest(query="test", namespace="demo", options=caller_options)
        assert caller_options == {"dimensions": [{"name": "region", "value": "EMEA"}]}
        assert req.options["dimensions"] == {"region": "EMEA"}


@pytest.mark.unit
class TestConfidenceScore:
    def test_valid_score(self):
        cs = ConfidenceScore(score=0.85, rationale="NL-to-SPARQL")
        assert cs.score == 0.85

    def test_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            ConfidenceScore(score=1.5, rationale="too high")

    def test_negative_score_rejected(self):
        with pytest.raises(ValidationError):
            ConfidenceScore(score=-0.1, rationale="negative")

    def test_boundary_scores(self):
        assert ConfidenceScore(score=0.0, rationale="").score == 0.0
        assert ConfidenceScore(score=1.0, rationale="").score == 1.0


@pytest.mark.unit
class TestTraceStep:
    def test_minimal_trace(self):
        ts = TraceStep(step="metric_match", status="success")
        assert ts.duration_ms == 0
        assert ts.detail is None

    def test_full_trace(self):
        ts = TraceStep(
            step="llm_call",
            status="success",
            duration_ms=1500,
            detail="confidence=0.85",
            tool_used="bedrock_converse",
        )
        assert ts.duration_ms == 1500
        assert ts.tool_used == "bedrock_converse"

    def test_alias_serialization(self):
        ts = TraceStep(step="test", status="ok", duration_ms=10)
        data = ts.model_dump(by_alias=True)
        assert "durationMs" in data


@pytest.mark.unit
class TestQueryResult:
    def test_minimal_result(self):
        result = QueryResult(
            tier=1,
            confidence=ConfidenceScore(score=1.0, rationale="exact match"),
            trace=[TraceStep(step="metric_match", status="success", duration_ms=5)],
        )
        assert result.tier == 1
        assert result.partial is False

    def test_tier2_result_with_all_fields(self):
        result = QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=0.85, rationale="NL-to-SPARQL"),
            result_rows=[{"id": "1", "amount": "5000"}],
            query_used="SELECT id, amount FROM claims",
            sparql_generated="SELECT ?x WHERE { ?x a :Claim }",
            trace=[TraceStep(step="vkg", status="success")],
            ontology_version="v1",
            data_sources=["claims"],
        )
        assert result.result_rows == [{"id": "1", "amount": "5000"}]
        assert result.ontology_version == "v1"

    def test_tier3_result_with_synthesis(self):
        result = QueryResult(
            tier=3,
            confidence=ConfidenceScore(score=0.7, rationale="agentic synthesis"),
            synthesized_answer="The SLA is 10 days.",
            supporting_content=[{"chunkId": "c1", "text": "..."}],
            graph_context={"entities": []},
            trace=[TraceStep(step="synthesize", status="success")],
        )
        assert result.synthesized_answer == "The SLA is 10 days."

    def test_populate_by_name(self):
        result = QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=0.9),
            resultRows=[{"a": "b"}],
            queryUsed="SELECT 1",
            trace=[TraceStep(step="t", status="s")],
        )
        assert result.result_rows == [{"a": "b"}]
        assert result.query_used == "SELECT 1"


@pytest.mark.unit
class TestInvokeResponse:
    def test_response_wraps_result(self):
        result = QueryResult(
            tier=0,
            confidence=ConfidenceScore(score=0.0),
            trace=[TraceStep(step="stub", status="success")],
        )
        response = InvokeResponse(result=result, requestId="req-123")
        assert response.requestId == "req-123"
        assert response.result.tier == 0

    def test_optional_fields_default_none(self):
        result = QueryResult(
            tier=0,
            confidence=ConfidenceScore(score=0.0),
            trace=[],
        )
        response = InvokeResponse(result=result)
        assert response.requestId is None
        assert response.sessionId is None


@pytest.mark.unit
def test_dimension_integer_beyond_float_range_is_accepted_not_overflow():
    """An int too large for a float is still a finite number; it must not raise OverflowError (a 500)."""
    req = InvokeRequest(query="q", namespace="ns", options={"dimensions": [{"name": "year", "value": 10**400}]})
    assert req.options["dimensions"] == {"year": 10**400}
