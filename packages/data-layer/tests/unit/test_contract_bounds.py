# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The data-plane bounds the handler enforces must equal the ones ``serve.smithy`` declares.

Two halves:

* **Parity** — parses the Smithy model (the source of truth, not the generated
  OpenAPI, so this proves the model and the runtime agree rather than that
  codegen ran) and asserts every constant in :mod:`coa_data_layer.contract`
  matches its ``@range`` / ``@length`` / enum. Edit the model and forget the
  constant, or the reverse, and this goes red.
* **Enforcement** — each bounded member is rejected with a 400 when out of
  contract (wrong type or out of range — never clamped), the rejected value is
  not echoed, nothing reaches the backend, and the boundary values are accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(
    "os.environ",
    {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
        "AWS_REGION": "us-east-1",
        "ALLOWED_ORIGIN": "https://app.example.com",
        "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
    },
):
    from coa_data_layer import contract
    from coa_data_layer import handler as handler_module
    from coa_data_layer.handler import handler

pytestmark = pytest.mark.unit

# tests/unit/<this> -> up 4 = repo root
_SERVE_SMITHY = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy" / "serve.smithy"

_QUERY = "/namespaces/{namespaceId}/query"
_KB_SEARCH = "/namespaces/{namespaceId}/kb/search"
_GRAPH_TRAVERSE = "/namespaces/{namespaceId}/graph/traverse"
_SCHEMA = "/namespaces/{namespaceId}/schema"


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _model() -> str:
    assert _SERVE_SMITHY.is_file(), f"serve.smithy not found at {_SERVE_SMITHY}"
    return _SERVE_SMITHY.read_text()


def _input_members(text: str, operation: str) -> dict[str, tuple[str, list[str]]]:
    """Map each member of ``operation``'s inline input to ``(target, trait_lines)``."""
    op = re.search(rf"operation\s+{operation}\s*\{{", text)
    assert op, f"operation {operation} not found"
    inp = re.search(r"input\s*:=\s*\{", text[op.end() :])
    assert inp, f"{operation} input block not found"
    members: dict[str, tuple[str, list[str]]] = {}
    traits: list[str] = []
    depth = 1
    for line in text[op.end() + inp.end() :].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if stripped.startswith("@"):
            traits.append(stripped)
        elif m := re.match(r"([A-Za-z_]\w*)\s*:\s*([A-Za-z_][\w#]*)", stripped):
            members[m.group(1)] = (m.group(2), traits)
            traits = []
    return members


def _shape_traits(text: str, shape: str) -> list[str]:
    """Trait lines applied to a top-level shape definition (doc comments skipped)."""
    m = re.search(rf"^(?:string|integer|long|float|double|list|map)\s+{shape}\b", text, re.MULTILINE)
    assert m, f"shape {shape} not found"
    traits: list[str] = []
    for line in reversed(text[: m.start()].splitlines()):
        stripped = line.strip()
        if stripped.startswith("@"):
            traits.append(stripped)
        elif not stripped.startswith("///"):
            break
    return traits


def _list_member_target(text: str, shape: str) -> str:
    m = re.search(rf"list\s+{shape}\s*\{{\s*member:\s*(\w+)", text)
    assert m, f"list {shape} not found"
    return m.group(1)


def _enum_values(text: str, shape: str) -> set[str]:
    m = re.search(rf"enum\s+{shape}\s*\{{(.*?)\n\}}", text, re.DOTALL)
    assert m, f"enum {shape} not found"
    return set(re.findall(r'=\s*"([^"]+)"', m.group(1)))


def _structure_member_traits(text: str, shape: str, member: str) -> list[str]:
    """Trait lines on ``member`` of a top-level ``structure`` (doc comments skipped)."""
    m = re.search(rf"structure\s+{shape}\s*\{{(.*?)\n\}}", text, re.DOTALL)
    assert m, f"structure {shape} not found"
    traits: list[str] = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("@"):
            traits.append(stripped)
        elif re.match(rf"{member}\s*:", stripped):
            return traits
        elif not stripped.startswith("///"):
            traits = []
    raise AssertionError(f"{shape}.{member} not found")


def _pattern(traits: list[str]) -> str:
    """The regex of the ``@pattern`` in ``traits``, with Smithy string escapes undone."""
    for t in traits:
        if m := re.fullmatch(r'@pattern\("(.*)"\)', t):
            return m.group(1).encode().decode("unicode_escape")
    raise AssertionError("no @pattern trait")


def _bound(traits: list[str], trait: str) -> tuple[float | None, float | None]:
    """``(min, max)`` from the first ``@range``/``@length`` in ``traits`` (``None`` = unset)."""
    for t in traits:
        if t.startswith(f"@{trait}("):
            lo = re.search(r"min:\s*(-?[\d.]+)", t)
            hi = re.search(r"max:\s*(-?[\d.]+)", t)
            return (float(lo.group(1)) if lo else None, float(hi.group(1)) if hi else None)
    return (None, None)


def _member_bound(text: str, operation: str, member: str, trait: str) -> tuple[float | None, float | None]:
    """A member's bound: on the member itself, else on its target shape."""
    target, traits = _input_members(text, operation)[member]
    bound = _bound(traits, trait)
    if bound == (None, None) and target[0].isupper() and target not in {"Integer", "Long", "Float", "Boolean"}:
        bound = _bound(_shape_traits(text, target), trait)
    return bound


# ── Parity ───────────────────────────────────────────────────────────────────


class TestBoundsMatchSmithyModel:
    """Every enforced bound equals the trait the model declares for it."""

    @pytest.mark.parametrize(
        ("operation", "member", "lo", "hi"),
        [
            ("Query", "tierOverride", contract.QUERY_TIER_OVERRIDE_MIN, contract.QUERY_TIER_OVERRIDE_MAX),
            ("Query", "timeoutMs", contract.QUERY_TIMEOUT_MS_MIN, contract.QUERY_TIMEOUT_MS_MAX),
            ("Query", "maxResults", contract.QUERY_MAX_RESULTS_MIN, contract.QUERY_MAX_RESULTS_MAX),
            ("KBSearch", "topK", contract.KB_SEARCH_TOP_K_MIN, contract.KB_SEARCH_TOP_K_MAX),
            ("KBSearch", "minScore", contract.KB_SEARCH_MIN_SCORE_MIN, contract.KB_SEARCH_MIN_SCORE_MAX),
            (
                "DescribeSchema",
                "maxResults",
                contract.DESCRIBE_SCHEMA_MAX_RESULTS_MIN,
                contract.DESCRIBE_SCHEMA_MAX_RESULTS_MAX,
            ),
            (
                "GraphTraverse",
                "maxDepth",
                contract.GRAPH_TRAVERSE_MAX_DEPTH_MIN,
                contract.GRAPH_TRAVERSE_MAX_DEPTH_MAX,
            ),
            (
                "GraphTraverse",
                "maxResults",
                contract.GRAPH_TRAVERSE_MAX_RESULTS_MIN,
                contract.GRAPH_TRAVERSE_MAX_RESULTS_MAX,
            ),
        ],
    )
    def test_range_matches(self, operation, member, lo, hi):
        declared = _member_bound(_model(), operation, member, "range")
        assert declared == (lo, hi), (
            f"{operation}.{member}: serve.smithy declares @range {declared}, "
            f"coa_data_layer.contract enforces {(lo, hi)}. Update the constant to match the model."
        )

    def test_mode_enum_matches(self):
        text = _model()
        target, _ = _input_members(text, "Query")["mode"]
        assert _enum_values(text, target) == contract.QUERY_MODES

    @pytest.mark.parametrize(
        ("operation", "member"),
        [("KBSearch", "sourceFilter"), ("KBSearch", "entityFilter"), ("GraphTraverse", "relationshipFilter")],
    )
    def test_filter_list_bounds_match(self, operation, member):
        text = _model()
        target, _ = _input_members(text, operation)[member]
        assert _bound(_shape_traits(text, target), "length") == (None, contract.FILTER_VALUE_LIST_MAX_ITEMS)
        value_shape = _list_member_target(text, target)
        assert _bound(_shape_traits(text, value_shape), "length") == (
            contract.FILTER_VALUE_MIN_LENGTH,
            contract.FILTER_VALUE_MAX_LENGTH,
        )

    def test_graph_traverse_direction_enum_matches(self):
        text = _model()
        target, _ = _input_members(text, "GraphTraverse")["direction"]
        assert _enum_values(text, target) == contract.GRAPH_TRAVERSE_DIRECTIONS
        assert contract.GRAPH_TRAVERSE_DIRECTION_DEFAULT in contract.GRAPH_TRAVERSE_DIRECTIONS

    def test_graph_traverse_start_uri_matches(self):
        text = _model()
        target, _ = _input_members(text, "GraphTraverse")["startUri"]
        traits = _shape_traits(text, target)
        assert _bound(traits, "length") == (
            contract.GRAPH_TRAVERSE_START_URI_MIN_LENGTH,
            contract.GRAPH_TRAVERSE_START_URI_MAX_LENGTH,
        )
        assert _pattern(traits) == contract.GRAPH_TRAVERSE_START_URI_PATTERN

    def test_graph_traverse_defaults_are_within_bounds(self):
        c = contract
        assert c.GRAPH_TRAVERSE_MAX_DEPTH_MIN <= c.GRAPH_TRAVERSE_MAX_DEPTH_DEFAULT <= c.GRAPH_TRAVERSE_MAX_DEPTH_MAX
        assert (
            c.GRAPH_TRAVERSE_MAX_RESULTS_MIN <= c.GRAPH_TRAVERSE_MAX_RESULTS_DEFAULT <= c.GRAPH_TRAVERSE_MAX_RESULTS_MAX
        )
        assert c.KB_SEARCH_TOP_K_MIN <= c.KB_SEARCH_TOP_K_DEFAULT <= c.KB_SEARCH_TOP_K_MAX

    def test_dimension_bounds_match(self):
        text = _model()
        target, traits = _input_members(text, "Query")["dimensions"]
        assert _bound(traits, "length") == (None, contract.DIMENSION_FILTER_LIST_MAX_ITEMS)
        entry = _list_member_target(text, target)
        assert _bound(_structure_member_traits(text, entry, "name"), "length") == (
            contract.DIMENSION_NAME_MIN_LENGTH,
            contract.DIMENSION_NAME_MAX_LENGTH,
        )
        assert _bound(_structure_member_traits(text, entry, "value"), "length") == (
            None,
            contract.DIMENSION_VALUE_MAX_LENGTH,
        )
        assert _enum_values(text, "DimensionOperator") == contract.DIMENSION_OPERATORS

    def test_rest_budget_matches_timeout_bound(self):
        """The model's ``timeoutMs`` ceiling is this handler's own REST budget."""
        assert contract.QUERY_TIMEOUT_MS_MAX == handler_module.REST_TIMEOUT_MS

    def test_parser_reads_a_known_bound(self):
        """Negative control: the parser must actually find traits, or parity passes vacuously."""
        assert _member_bound(_model(), "Query", "tierOverride", "range") != (None, None)


# ── Enforcement ──────────────────────────────────────────────────────────────


def _event(method: str, resource: str, body: dict | None = None, query: dict | None = None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "headers": {"Authorization": "Bearer t"},
        "requestContext": {"authorizer": {"principalId": "u"}},
    }


def _post(resource: str, body: dict) -> tuple[dict, MagicMock]:
    with patch.object(handler_module, "_invoke_context_manager", return_value={"statusCode": 200}) as invoke:
        return handler(_event("POST", resource, body), None), invoke


_SENTINEL = "zz-rejected-value-zz"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("tierOverride", 0),
        ("tierOverride", 4),
        ("tierOverride", "2"),
        ("tierOverride", 2.0),
        ("tierOverride", True),
        ("mode", "turbo"),
        ("mode", ["standard"]),
        ("mode", _SENTINEL),
        ("timeoutMs", 0),
        ("timeoutMs", 29_001),
        ("timeoutMs", "1000"),
        ("maxResults", 0),
        ("maxResults", 10_001),
        ("maxResults", 5.5),
        ("maxResults", False),
        ("execute", "false"),
        ("execute", 0),
        ("includeSupporting", 1),
        ("includeDebugInfo", "true"),
        ("dimensions", "region=EU"),
        ("dimensions", 0),
        ("dimensions", [{"name": "region"}]),
        ("dimensions", [{"value": "EU"}]),
        ("dimensions", [{"name": "", "value": "EU"}]),
        ("dimensions", [{"name": "   ", "value": "EU"}]),
        ("dimensions", [{"name": "r" * 257, "value": "EU"}]),
        ("dimensions", [{"name": "region", "value": "v" * 4001}]),
        ("dimensions", [{"name": "region", "value": None}]),
        ("dimensions", [{"name": "region", "value": {"eq": "EU"}}]),
        ("dimensions", [{"name": "region", "value": ["EU"]}]),
        ("dimensions", [{"name": "region", "value": float("nan")}]),
        ("dimensions", [{"name": "region", "value": float("inf")}]),
        ("dimensions", [{"name": "region", "value": float("-inf")}]),
        ("dimensions", [{"name": "region", "value": "EU", "operator": "!="}]),
        ("dimensions", [{"name": "region", "value": "EU", "operator": ">"}]),
        ("dimensions", [{"name": "region", "value": "EU", "operator": "=="}]),
        ("dimensions", [{"name": "region", "value": "EU", "operator": 1}]),
        ("dimensions", [{"name": "region", "value": "EU"}, {"name": "Region", "value": "US"}]),
        ("dimensions", [{"name": f"d{i}", "value": "v"} for i in range(101)]),
        ("dimensions", ["region"]),
        ("dimensions", {"region": None}),
        ("dimensions", {"region": ["EU"]}),
        ("dimensions", {"region": float("nan")}),
        ("dimensions", {"region": "v" * 4001}),
    ],
)
def test_query_rejects_out_of_contract_member(field, bad):
    result, invoke = _post(_QUERY, {"query": "q", field: bad})
    assert result["statusCode"] == 400
    message = json.loads(result["body"])["message"]
    assert field in message
    assert _SENTINEL not in result["body"]
    invoke.assert_not_called()


def test_query_accepts_boundary_values():
    body = {
        "query": "q",
        "execute": False,
        "tierOverride": 3,
        "mode": "agentic",
        "timeoutMs": 1,
        "includeSupporting": False,
        "includeDebugInfo": True,
        "maxResults": 10_000,
    }
    result, invoke = _post(_QUERY, body)
    assert result["statusCode"] == 200
    options = invoke.call_args.args[0]["options"]
    assert options == {
        "execute": False,
        "tierOverride": 3,
        "mode": "agentic",
        "timeoutMs": 1,
        "includeSupporting": False,
        "includeDebugInfo": True,
        "maxResults": 10_000,
    }


def test_query_accepts_boundary_dimensions():
    dims = [{"name": "r" * 256, "value": "v" * 4000, "operator": "="}] + [
        {"name": f"d{i}", "value": ""} for i in range(99)
    ]
    result, invoke = _post(_QUERY, {"query": "q", "dimensions": dims})
    assert result["statusCode"] == 200
    forwarded = invoke.call_args.args[0]["options"]["dimensions"]
    assert len(forwarded) == 100
    assert forwarded["r" * 256] == "v" * 4000


def test_query_accepts_legacy_dimension_mapping():
    result, invoke = _post(_QUERY, {"query": "q", "dimensions": {"region": "EU"}})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"]["dimensions"] == {"region": "EU"}


@pytest.mark.parametrize("dim_value", [7, 0, -3, 10**400, 2.5, -0.0, True, False])
def test_query_accepts_numeric_and_boolean_dimension_values(dim_value):
    """Numbers and booleans pass through for the Context Manager to bind as typed literals."""
    for dims in ([{"name": "region", "value": dim_value}], {"region": dim_value}):
        result, invoke = _post(_QUERY, {"query": "q", "dimensions": dims})
        assert result["statusCode"] == 200
        assert invoke.call_args.args[0]["options"]["dimensions"] == {"region": dim_value}


@pytest.mark.parametrize("operator", [None, "", "   ", "=", " = "])
def test_query_treats_blank_or_equals_operator_as_equality(operator):
    filt = {"name": "region", "value": "EU"}
    if operator is not None:
        filt["operator"] = operator
    result, invoke = _post(_QUERY, {"query": "q", "dimensions": [filt]})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"]["dimensions"] == {"region": "EU"}


def test_query_empty_dimensions_are_not_forwarded():
    result, invoke = _post(_QUERY, {"query": "q", "dimensions": []})
    assert result["statusCode"] == 200
    assert "dimensions" not in invoke.call_args.args[0]["options"]


def test_query_null_members_are_absent():
    result, invoke = _post(_QUERY, {"query": "q", "tierOverride": None, "mode": None, "timeoutMs": None})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"] == {}


def test_query_execute_true_is_the_default_and_not_forwarded():
    result, invoke = _post(_QUERY, {"query": "q", "execute": True})
    assert result["statusCode"] == 200
    assert "execute" not in invoke.call_args.args[0]["options"]


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("topK", 0),
        ("topK", 101),
        ("topK", "10"),
        ("topK", 10.0),
        ("topK", True),
        ("minScore", "0.5"),
        ("minScore", True),
        ("minScore", [0.5]),
        ("minScore", -0.1),
        ("sourceFilter", "doc-1"),
        ("sourceFilter", [""]),
        ("sourceFilter", [1]),
        ("sourceFilter", ["d"] * 51),
        ("sourceFilter", ["x" * 2049]),
        ("entityFilter", {"a": 1}),
        ("entityFilter", [None]),
    ],
)
def test_kb_search_rejects_out_of_contract_member(field, bad):
    result, invoke = _post(_KB_SEARCH, {"query": "q", field: bad})
    assert result["statusCode"] == 400
    assert field in json.loads(result["body"])["message"]
    invoke.assert_not_called()


def test_kb_search_does_not_echo_rejected_filter_value():
    result, _ = _post(_KB_SEARCH, {"query": "q", "sourceFilter": [_SENTINEL * 200]})
    assert result["statusCode"] == 400
    assert _SENTINEL not in result["body"]


def test_kb_search_defaults_top_k_when_absent():
    result, invoke = _post(_KB_SEARCH, {"query": "q"})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"] == {"topK": contract.KB_SEARCH_TOP_K_DEFAULT}


def test_kb_search_forwards_all_valid_members():
    body = {"query": "q", "topK": 100, "minScore": 0.25, "sourceFilter": ["doc-1"], "entityFilter": ["Acme"]}
    result, invoke = _post(_KB_SEARCH, body)
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"] == {
        "topK": 100,
        "minScore": 0.25,
        "sourceFilter": ["doc-1"],
        "entityFilter": ["Acme"],
    }


def test_kb_search_accepts_integer_min_score():
    """JSON has one number type: ``1`` is a valid Float."""
    result, invoke = _post(_KB_SEARCH, {"query": "q", "minScore": 1})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"]["minScore"] == 1.0


_URI = "https://example.com/ontology#Claim"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("startUri", 42),
        ("startUri", ["urn:a"]),
        ("startUri", "not-an-iri"),
        ("startUri", "ftp://example.com/x"),
        ("startUri", "https://example.com/a b"),
        ("startUri", "https://example.com/a>"),
        ("startUri", 'https://example.com/a"'),
        ("startUri", "https://example.com/a\n"),
        ("startUri", "urn:-x"),
        ("startUri", "https://example.com/" + "a" * 2048),
        ("startUri", "https://example.com/" + _SENTINEL + " "),
        ("maxDepth", 0),
        ("maxDepth", -1),
        ("maxDepth", 6),
        ("maxDepth", "2"),
        ("maxDepth", 2.0),
        ("maxDepth", True),
        ("maxResults", 0),
        ("maxResults", 1001),
        ("maxResults", "100"),
        ("direction", "out"),
        ("direction", ["both"]),
        ("direction", _SENTINEL),
        ("relationshipFilter", "urn:p"),
        ("relationshipFilter", [""]),
        ("relationshipFilter", [1]),
        ("relationshipFilter", ["urn:p"] * 51),
        ("relationshipFilter", ["x" * 2049]),
    ],
)
def test_graph_traverse_rejects_out_of_contract_member(field, bad):
    body = {"startUri": _URI, field: bad}
    result, invoke = _post(_GRAPH_TRAVERSE, body)
    assert result["statusCode"] == 400
    assert field in json.loads(result["body"])["message"]
    assert _SENTINEL not in result["body"]
    invoke.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"startUri": None}, {"startUri": ""}, {"maxDepth": 2}])
def test_graph_traverse_requires_start_uri(body):
    result, invoke = _post(_GRAPH_TRAVERSE, body)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["message"] == "Missing required field: startUri"
    invoke.assert_not_called()


def test_graph_traverse_applies_defaults():
    result, invoke = _post(_GRAPH_TRAVERSE, {"startUri": _URI})
    assert result["statusCode"] == 200
    assert invoke.call_args.args[0]["options"] == {
        "startUri": _URI,
        "maxDepth": contract.GRAPH_TRAVERSE_MAX_DEPTH_DEFAULT,
        "direction": contract.GRAPH_TRAVERSE_DIRECTION_DEFAULT,
        "maxResults": contract.GRAPH_TRAVERSE_MAX_RESULTS_DEFAULT,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"startUri": "urn:coa:customer:acme-corp", "maxDepth": 1, "maxResults": 1, "direction": "incoming"},
        {
            "startUri": "https://example.com/" + "a" * (2048 - len("https://example.com/")),
            "maxDepth": 5,
            "maxResults": 1000,
            "direction": "outgoing",
            "relationshipFilter": ["urn:p"] * 50,
        },
    ],
)
def test_graph_traverse_accepts_boundary_values(body):
    result, invoke = _post(_GRAPH_TRAVERSE, body)
    assert result["statusCode"] == 200
    options = invoke.call_args.args[0]["options"]
    assert {k: options[k] for k in body} == body


def test_graph_traverse_empty_relationship_filter_is_not_forwarded():
    result, invoke = _post(_GRAPH_TRAVERSE, {"startUri": _URI, "relationshipFilter": []})
    assert result["statusCode"] == 200
    assert "relationshipFilter" not in invoke.call_args.args[0]["options"]


def _schema(query: dict) -> tuple[dict, MagicMock]:
    client = MagicMock()
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": MagicMock(read=lambda: json.dumps({"statusCode": 200, "body": "{}"}).encode()),
    }
    with patch.object(handler_module, "_get_lambda_client", return_value=client):
        return handler(_event("GET", _SCHEMA, query=query), None), client


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("maxResults", "0"),
        ("maxResults", "1001"),
        ("maxResults", "-1"),
        ("maxResults", "1.5"),
        ("maxResults", "ten"),
        ("maxResults", "9" * 40),
        ("includeProperties", "yes"),
        ("includeProperties", "1"),
    ],
)
def test_describe_schema_rejects_out_of_contract_query(field, bad):
    result, client = _schema({field: bad})
    assert result["statusCode"] == 400
    assert field in json.loads(result["body"])["message"]
    client.invoke.assert_not_called()


def test_describe_schema_accepts_boundary_values():
    result, client = _schema({"maxResults": "1000", "includeProperties": "TRUE"})
    assert result["statusCode"] == 200
    forwarded = json.loads(client.invoke.call_args.kwargs["Payload"].decode())
    assert forwarded["queryStringParameters"] == {"maxResults": "1000", "includeProperties": "true"}


@pytest.mark.parametrize(
    ("lo", "hi", "value", "expected"),
    [
        (0.0, None, -0.1, "Invalid minScore. Must be a number of at least 0."),
        (None, 1.0, 2.0, "Invalid minScore. Must be a number of at most 1."),
        (0.0, 1.0, 2.0, "Invalid minScore. Must be a number from 0 to 1."),
    ],
)
def test_number_range_message_omits_an_unbounded_side(lo, hi, value, expected):
    with pytest.raises(contract.ContractViolation, match=re.escape(expected)):
        contract.require_number(value, "minScore", lo, hi)
