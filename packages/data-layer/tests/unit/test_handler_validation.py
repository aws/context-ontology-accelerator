# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for data-layer handler input validation."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from botocore.exceptions import ClientError

# The handler module reads env vars at import time.
with patch.dict(
    "os.environ",
    {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
        "AWS_REGION": "us-east-1",
        "ALLOWED_ORIGIN": "https://app.example.com",
        "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
        "METRIC_SERVICE_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-metric-service",
    },
):
    from coa_data_layer import handler as handler_module
    from coa_data_layer.handler import handler


def _api_event(method: str, resource: str, body: dict | None = None, namespace: str = "demo") -> dict:
    """Build a minimal API Gateway proxy event."""
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"namespaceId": namespace},
        "body": json.dumps(body) if body else None,
        "headers": {"Authorization": "Bearer test-token"},
        "requestContext": {
            "authorizer": {
                "principalId": "user@example.com",
                "email": "user@example.com",
                "groups": "",
                "globalRoles": "",
            }
        },
    }


@pytest.mark.unit
class TestQueryValidation:
    """Validate that the /query endpoint rejects invalid inputs before invoking AgentCore."""

    def test_missing_body_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body=None)
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing required field: query" in result["body"]

    def test_empty_query_returns_400(self):
        """A present-but-empty field reports blankness, not absence."""
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": ""})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must not be blank" in result["body"]

    def test_whitespace_only_query_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "   "})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must not be blank" in result["body"]

    def test_tabs_newlines_only_query_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "\t\n  "})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must not be blank" in result["body"]

    def test_missing_namespace_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hello"})
        event["pathParameters"] = {}
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "namespace" in result["body"].lower()


@pytest.mark.unit
class TestTranslateValidation:
    """Validate that the /translate endpoint rejects invalid inputs."""

    def test_whitespace_only_query_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/translate", body={"query": "   "})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must not be blank" in result["body"]


@pytest.mark.unit
class TestKbSearchValidation:
    """Validate that the /kb/search endpoint rejects invalid inputs."""

    def test_whitespace_only_query_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/kb/search", body={"query": "   "})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must not be blank" in result["body"]


@pytest.mark.unit
class TestNaturalLanguageQueryBoundary:
    """All query-bearing routes reject invalid input before AgentCore I/O."""

    @pytest.mark.parametrize(
        "resource",
        [
            "/namespaces/{namespaceId}/query",
            "/namespaces/{namespaceId}/translate",
            "/namespaces/{namespaceId}/kb/search",
        ],
    )
    @pytest.mark.parametrize("query", [42, "   ", "x" * 4001])
    def test_invalid_query_returns_400_without_backend_call(self, resource, query):
        with patch.object(handler_module, "_invoke_context_manager") as invoke_context_manager:
            event = _api_event("POST", resource, body={"query": query})
            result = handler(event, None)

        assert result["statusCode"] == 400
        invoke_context_manager.assert_not_called()


@pytest.mark.unit
class TestCorsHeaders:
    """Test that CORS headers are set from ALLOWED_ORIGIN environment variable."""

    def test_cors_header_in_error_response(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body=None)
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    def test_cors_header_content_type(self):
        """Ensure CORS_HEADERS includes both CORS and Content-Type."""
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": ""})
        result = handler(event, None)
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert result["headers"]["Content-Type"] == "application/json"

    def test_missing_allowed_origin_defaults_to_empty(self):
        """Missing ALLOWED_ORIGIN defaults to empty string (doesn't crash at import)."""
        env = {
            "AGENTCORE_RUNTIME_ARN": "arn:test",
            "AWS_REGION": "us-east-1",
            "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
            "METRIC_SERVICE_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-metric-service",
        }
        with patch.dict("os.environ", env, clear=True):
            import importlib

            from coa_data_layer import handler as h

            importlib.reload(h)
            try:
                assert h.CORS_HEADERS["Access-Control-Allow-Origin"] == ""
            finally:
                # Reload again under the OUTER (module-scope) env so subsequent
                # tests see the same constants they were imported with. Without
                # this, ``ONTOLOGY_PROXY_LAMBDA_ARN`` (or any other constant
                # captured at import time) would leak an empty value into the
                # rest of the suite via ``handler_module``.
                importlib.reload(h)


def _fake_urlopen(response_body: dict):
    """Build a urlopen replacement whose context-manager yields JSON bytes."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(response_body).encode("utf-8")
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


@pytest.mark.unit
class TestRouting:
    """Routing behavior: unknown routes and missing namespace."""

    def test_missing_namespace_path_param_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
        event["pathParameters"] = None
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing namespace path parameter" in result["body"]

    def test_unknown_route_returns_501(self):
        event = _api_event("DELETE", "/namespaces/{namespaceId}/query", body={"query": "hi"})
        result = handler(event, None)
        assert result["statusCode"] == 501
        assert "Not implemented" in result["body"]


@pytest.mark.unit
class TestQuerySuccess:
    """Query endpoint success path, option passthrough, and profile extraction."""

    def test_query_success_returns_200_with_result_shape(self):
        upstream = {
            "statusCode": 200,
            "result": {"answer": "42"},
            "requestId": "req-1",
            "sessionId": "sess-1",
        }
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = _fake_urlopen(upstream)
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hello"})
            result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["result"] == {"answer": "42"}
        assert body["requestId"] == "req-1"
        assert body["sessionId"] == "sess-1"

    def test_query_forwards_all_options_including_timeout_ms(self):
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            captured["token"] = token
            return {"statusCode": 200, "result": {}}

        body = {
            "query": "  spaced  ",
            "execute": False,
            "tierOverride": "premium",
            "mode": "standard",
            # Smithy wire shape: list of DimensionFilter objects. Data-layer
            # normalizes it to CM's ``{name: value}`` mapping via
            # ``normalize_dimensions`` before forwarding (fixes the Tier-1
            # ``AttributeError``/500 caught in MR !939 review). ``operator``
            # is dropped — CM's Tier-1 resolver supports equality only.
            "dimensions": [{"name": "region", "value": "EMEA", "operator": "="}],
            "timeoutMs": 999_999,
            "strategy": "ontop",
            "includeSupporting": True,
            "maxResults": 5,
        }
        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body=body, namespace="ns1")
            event["requestContext"]["authorizer"]["groups"] = "g1, g2"
            event["requestContext"]["authorizer"]["globalRoles"] = "admin"
            result = handler(event, None)

        assert result["statusCode"] == 200
        payload = captured["payload"]
        assert payload["query"] == "spaced"  # stripped
        assert payload["namespace"] == "ns1"
        assert payload["options"]["execute"] is False
        assert payload["options"]["tierOverride"] == "premium"
        assert payload["options"]["mode"] == "standard"
        # Normalized to the mapping shape CM's Tier-1 resolver expects. If this
        # ever regresses back to the list, every dimensioned Tier-1 metric
        # query 500s with AttributeError on ``.items()``.
        assert payload["options"]["dimensions"] == {"region": "EMEA"}
        # ``timeoutMs`` IS now forwarded to CM — the Context Manager builds a
        # request-scoped deadline from ``options.timeoutMs``, so it is no longer a
        # silent no-op. The data-layer forwards the raw value; serve clamps it to
        # its own transport budget (a caller cannot widen past the ceiling), so no
        # cap is applied here. The urllib ``REST_TIMEOUT_MS`` still bounds this
        # handler's own call.
        assert payload["options"]["timeoutMs"] == 999_999
        # ``strategy`` IS forwarded too: CM reads ``options["strategy"]`` in
        # ``Orchestrator._resolve_strategy_selection``, so it is not a phantom
        # field. Dropping it here is what made the Ontop / VKG path unreachable by
        # any REST caller — it ran only as an automatic fallback, never on request.
        assert payload["options"]["strategy"] == "ontop"
        assert payload["options"]["includeSupporting"] is True
        assert payload["options"]["maxResults"] == 5
        assert payload["profile"]["groups"] == ["g1", "g2"]
        assert payload["profile"]["globalRoles"] == ["admin"]
        assert captured["token"] == "test-token"

    @pytest.mark.parametrize("bad_timeout", [0, -5, "abc", 1.5])
    def test_query_drops_invalid_or_nonpositive_timeout_ms(self, bad_timeout, caplog):
        # Negative control for timeoutMs forwarding: a zero/negative/non-integer
        # timeoutMs is meaningless as a budget and must NOT be forwarded (a 0 would
        # otherwise degenerate the serve deadline). Distinguishes "supports
        # forwarding" from "forwards blindly".
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            return {"statusCode": 200, "result": {}}

        body = {"query": "q", "timeoutMs": bad_timeout}
        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body=body, namespace="ns1")
            with caplog.at_level("INFO"):
                result = handler(event, None)

        assert result["statusCode"] == 200
        assert "timeoutMs" not in captured["payload"]["options"]
        # The drop is surfaced so a caller can tell it apart from "no timeout sent".
        assert "query_timeout_ms_dropped" in caplog.text

    def test_query_omits_strategy_when_not_supplied(self):
        """An unset ``strategy`` must not appear in options at all.

        Sending ``strategy: None`` would reach ``normalize_strategy_option``, fall
        out of ``_EXPLICIT_STRATEGY_OPTIONS``, and resolve to ``DEFAULT_STRATEGY``
        anyway — but it would also make the request differ from a pre-change one
        and would show up in traces as an explicit pin that the caller never made.
        """
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            return {"statusCode": 200, "result": {}}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event(
                "POST",
                "/namespaces/{namespaceId}/query",
                body={"query": "revenue by region"},
                namespace="ns1",
            )
            result = handler(event, None)

        assert result["statusCode"] == 200
        assert "strategy" not in captured["payload"]["options"]

    def test_query_rejects_unknown_strategy_with_400(self):
        """An unrecognised strategy must 400, not be forwarded.

        Nothing else validates it — the handler does not check the body against the
        generated Smithy model and API Gateway has no RequestValidator — so without
        this check the value reaches serve, drops out of
        ``Orchestrator._EXPLICIT_STRATEGY_OPTIONS`` and silently resolves to
        DEFAULT_STRATEGY. The caller would get the cheap fallback chain while
        believing it had pinned an engine.
        """
        called = {"n": 0}

        def _fake_invoke(payload: dict, token: str) -> dict:
            called["n"] += 1
            return {"statusCode": 200, "result": {}}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event(
                "POST",
                "/namespaces/{namespaceId}/query",
                body={"query": "revenue by region", "strategy": "ontopp"},
                namespace="ns1",
            )
            result = handler(event, None)

        assert result["statusCode"] == 400, result
        # The rejected value must NOT be echoed — it is unbounded caller-controlled
        # input, and reflecting it back is what AppSec objects to.
        assert "ontopp" not in result["body"]
        # Names the valid values, so the caller can fix it without reading the model.
        assert "ontop_first" in result["body"]
        # Must fail before reaching CM — a rejected request costs nothing upstream.
        assert called["n"] == 0

    @pytest.mark.parametrize(
        "strategy",
        ["best", "ontop", "nl_to_sql", "ontop_first", "nl_to_sql_first", "deep-reasoning"],
    )
    def test_query_accepts_every_declared_strategy(self, strategy):
        """Every value the contract advertises must pass validation here.

        Parametrised so a set that drifts out of step with the Smithy enum names the
        offending value instead of failing as one opaque case.
        """
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            return {"statusCode": 200, "result": {}}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event(
                "POST",
                "/namespaces/{namespaceId}/query",
                body={"query": "revenue by region", "strategy": strategy},
                namespace="ns1",
            )
            result = handler(event, None)

        assert result["statusCode"] == 200, result
        assert captured["payload"]["options"]["strategy"] == strategy

    @pytest.mark.parametrize(
        ("value", "label"),
        [
            (["ontop"], "list"),
            ({"engine": "ontop"}, "dict"),
            (42, "int"),
            (True, "bool"),
        ],
    )
    def test_query_rejects_non_string_strategy_with_400(self, value, label):
        """A non-string strategy must 400, never 502.

        ``body`` is caller-controlled and ``QUERY_STRATEGIES`` is a frozenset, so
        ``value not in QUERY_STRATEGIES`` raises TypeError for an unhashable value.
        Unguarded, that escapes to the top-level ``except Exception`` and is reported
        as 502 "Context Manager invocation failed" — an upstream-failure code for a
        client mistake, on a request that never left the handler.
        """
        called = {"n": 0}

        def _fake_invoke(payload: dict, token: str) -> dict:
            called["n"] += 1
            return {"statusCode": 200, "result": {}}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event(
                "POST",
                "/namespaces/{namespaceId}/query",
                body={"query": "revenue by region", "strategy": value},
                namespace="ns1",
            )
            result = handler(event, None)

        assert result["statusCode"] == 400, f"{label} gave {result['statusCode']}: {result}"
        assert "Context Manager" not in result["body"], f"{label} leaked an upstream error: {result['body']}"
        assert called["n"] == 0, f"{label} reached CM"

    def test_query_error_does_not_reflect_a_large_strategy_value(self):
        """A rejected value must not be able to inflate the response.

        ``strategy`` is unbounded caller-controlled input. Echoing it — especially via
        ``!r``, which serialises whatever arrived — lets a client turn a 400 into a
        response as large as its own request, and reflects its content back verbatim.
        The error names the valid values instead, so its size is fixed regardless of
        what was sent.
        """
        huge = "x" * 100_000

        with patch.object(handler_module, "_invoke_context_manager", side_effect=AssertionError):
            event = _api_event(
                "POST",
                "/namespaces/{namespaceId}/query",
                body={"query": "revenue by region", "strategy": huge},
                namespace="ns1",
            )
            result = handler(event, None)

        assert result["statusCode"] == 400
        assert huge not in result["body"]
        # Bounded: the message is the fixed valid-values list, not a function of input.
        assert len(result["body"]) < 500, f"response grew to {len(result['body'])} bytes"

    def test_query_upstream_error_status_is_propagated(self):
        with patch.object(
            handler_module,
            "_invoke_context_manager",
            return_value={"statusCode": 503, "message": "downstream down"},
        ):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 503
        assert "downstream down" in result["body"]


@pytest.mark.unit
class TestTranslateSuccess:
    """Translate endpoint success path shape."""

    def test_translate_success_returns_sparql_shape(self):
        upstream = {
            "statusCode": 200,
            "sparqlQuery": "SELECT ?s WHERE {}",
            "confidence": {"score": 0.9},
            "trace": ["step1"],
            "ontologyVersion": "v2",
        }
        with patch.object(handler_module, "_invoke_context_manager", return_value=upstream):
            event = _api_event("POST", "/namespaces/{namespaceId}/translate", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["sparqlQuery"] == "SELECT ?s WHERE {}"
        assert body["confidence"] == {"score": 0.9}
        assert body["ontologyVersion"] == "v2"


@pytest.mark.unit
class TestKbSearchSuccess:
    """KB search endpoint success path and topK passthrough."""

    def test_kb_search_success_returns_chunks(self):
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            return {"statusCode": 200, "chunks": [{"id": "c1"}], "trace": [], "queryEmbeddingModel": "m1"}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event("POST", "/namespaces/{namespaceId}/kb/search", body={"query": "hi", "topK": 3})
            result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["chunks"] == [{"id": "c1"}]
        assert body["queryEmbeddingModel"] == "m1"
        assert captured["payload"]["options"]["topK"] == 3


@pytest.mark.unit
class TestGraphTraverse:
    """Graph traverse validation and success behavior."""

    def test_missing_start_uri_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/graph/traverse", body={"maxDepth": 3})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing required field: startUri" in result["body"]

    def test_graph_traverse_success_forwards_options(self):
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["payload"] = payload
            return {"statusCode": 200, "entities": [{"uri": "x"}], "relationships": [], "trace": []}

        body = {
            "startUri": "urn:node:1",
            "maxDepth": 4,
            "direction": "out",
            "maxResults": 50,
            "relationshipFilter": ["knows"],
        }
        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event("POST", "/namespaces/{namespaceId}/graph/traverse", body=body)
            result = handler(event, None)
        assert result["statusCode"] == 200
        opts = captured["payload"]["options"]
        assert opts["startUri"] == "urn:node:1"
        assert opts["maxDepth"] == 4
        assert opts["direction"] == "out"
        assert opts["relationshipFilter"] == ["knows"]
        assert json.loads(result["body"])["entities"] == [{"uri": "x"}]


@pytest.mark.unit
class TestBodyParsing:
    """Malformed request bodies are treated as missing."""

    def test_invalid_json_body_returns_400(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query")
        event["body"] = "{not valid json"
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing required field: query" in result["body"]


@pytest.mark.unit
class TestBearerToken:
    """Authorization header parsing forwards the correct raw token."""

    def test_non_bearer_authorization_forwarded_verbatim(self):
        captured: dict = {}

        def _fake_invoke(payload: dict, token: str) -> dict:
            captured["token"] = token
            return {"statusCode": 200, "result": {}}

        with patch.object(handler_module, "_invoke_context_manager", side_effect=_fake_invoke):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            event["headers"] = {"authorization": "raw-token-value"}
            handler(event, None)
        assert captured["token"] == "raw-token-value"


@pytest.mark.unit
class TestUpstreamErrorHandling:
    """Downstream transport errors map to appropriate HTTP responses."""

    def test_http_error_maps_code_and_error_field(self):
        detail = json.dumps({"message": "boom", "error": "ValidationError"}).encode("utf-8")
        err = HTTPError(
            url="http://x",
            code=422,
            msg="Unprocessable",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(detail),
        )
        with patch.object(handler_module, "_invoke_context_manager", side_effect=err):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 422
        body = json.loads(result["body"])
        assert body["message"] == "boom"
        assert body["error"] == "ValidationError"

    def test_http_error_non_json_body_truncated(self):
        err = HTTPError(
            url="http://x",
            code=500,
            msg="Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"plain text failure"),
        )
        with patch.object(handler_module, "_invoke_context_manager", side_effect=err):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 500
        assert "plain text failure" in result["body"]

    def test_url_error_maps_to_502(self):
        with patch.object(handler_module, "_invoke_context_manager", side_effect=URLError("connection refused")):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Cannot reach Context Manager" in result["body"]

    def test_rest_timeout_maps_to_diagnosable_504(self):
        # The 29s urlopen budget surfaces as URLError whose .reason is a socket
        # timeout. It must NOT be reported as a 502 "cannot reach" (which reads as a
        # connectivity/down failure and sends builders chasing a phantom bug) --
        # the request reached the Context Manager and is likely still completing
        # server-side. Assert the diagnosable 504 that names the streaming path.
        timeout_err = URLError(TimeoutError("timed out"))
        with patch.object(handler_module, "_invoke_context_manager", side_effect=timeout_err):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 504
        body = json.loads(result["body"])
        assert body["error"] == "rest_deadline_exceeded"
        assert body["limitMs"] == handler_module.REST_TIMEOUT_MS
        assert "streaming" in body["message"].lower()

    def test_connectivity_url_error_still_502_not_504(self):
        # Negative control for the 504 branch: a genuine connectivity failure
        # (reason is not a timeout) must remain a 502, not be misclassified as the
        # deadline-exceeded case.
        with patch.object(
            handler_module,
            "_invoke_context_manager",
            side_effect=URLError(ConnectionRefusedError("refused")),
        ):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Cannot reach Context Manager" in result["body"]
        assert "rest_deadline_exceeded" not in result["body"]

    def test_unexpected_exception_maps_to_502(self):
        with patch.object(handler_module, "_invoke_context_manager", side_effect=ValueError("oops")):
            event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": "hi"})
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "ValueError" in result["body"]


@pytest.mark.unit
class TestDescribeSchema:
    """GET /namespaces/{ns}/schema proxies to the ontology-api-proxy Lambda.

    Regression scope: MR !911 removed the DescribeSchema operation entirely on
    the assumption that it was unrouted. It is used by the UI/data-layer surface
    (parallel to MCP's ``describe_schema``), so a subsequent revert of this fix
    would break the surface again and must fail here.
    """

    def _fake_proxy_response(self, body: dict, status: int = 200) -> dict:
        """Shape a synthetic Lambda invoke response with an API GW proxy body."""
        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(
                json.dumps(
                    {
                        "statusCode": status,
                        "body": json.dumps(body),
                        "headers": {},
                    }
                ).encode("utf-8")
            ),
        }

    def test_returns_classes_from_proxy(self):
        proxy_body = {
            "classes": [
                {"uri": "urn:c:1", "label": "Policy", "properties": []},
            ],
            "ontologyVersion": "2026-08-12",
        }
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["classes"] == proxy_body["classes"]
        assert body["ontologyVersion"] == "2026-08-12"

    def test_forwards_camelcase_query_params_verbatim(self):
        """The ontology-api-proxy renames maxResults→max_results per path; the
        data layer must pass the camelCase form through unchanged so the alias
        is what runs."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response({"classes": [], "ontologyVersion": ""})
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            event["queryStringParameters"] = {"maxResults": "50", "includeProperties": "false"}
            handler(event, None)
        forwarded = json.loads(mock_client.invoke.call_args.kwargs["Payload"].decode())
        assert forwarded["queryStringParameters"] == {"maxResults": "50", "includeProperties": "false"}
        # Canonical Smithy path; api-proxy's alias table routes to /graph/schema.
        assert forwarded["resource"] == "/namespaces/{namespaceId}/schema"

    def test_bearer_token_forwarded(self):
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response({"classes": [], "ontologyVersion": ""})
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            event["headers"] = {"Authorization": "Bearer forwarded-token"}
            handler(event, None)
        forwarded = json.loads(mock_client.invoke.call_args.kwargs["Payload"].decode())
        assert forwarded["headers"]["Authorization"] == "Bearer forwarded-token"

    def test_proxy_error_status_propagates(self):
        proxy_body = {"message": "namespace not found"}
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body, status=404)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 404
        assert "namespace not found" in result["body"]

    def test_function_error_maps_to_502(self):
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(b'"boom"'),
        }
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Schema backend failed" in result["body"]

    def test_missing_env_var_returns_501(self):
        with patch.object(handler_module, "ONTOLOGY_PROXY_LAMBDA_ARN", ""):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 501
        assert "ONTOLOGY_PROXY_LAMBDA_ARN" in result["body"]

    def test_response_does_not_leak_extra_fields(self):
        """The Smithy output is `classes` + `ontologyVersion` only. A future
        backend change must not add fields to the customer response by accident."""
        proxy_body = {
            "classes": [],
            "ontologyVersion": "v1",
            "namespace": "9142f178-...",  # ontology-engine echoes this today
            "internal_debug": "should never surface",
        }
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        body = json.loads(result["body"])
        assert set(body.keys()) == {"classes", "ontologyVersion"}

    # ── Error-handling regressions from the !921 code review ─────────────

    def test_client_error_on_invoke_maps_to_502(self):
        """boto3 ClientError from lambda:Invoke (throttle / permissions / timeout)
        must produce a stable 502 with a schema-specific message. Unhandled it
        would escape to the router's generic ``except Exception`` and come back
        as ``Context Manager invocation failed`` — misleading, since CM was
        never in the path."""
        mock_client = MagicMock()
        mock_client.invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Invoke",
        )
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Failed to invoke schema backend Lambda" in result["body"]
        # And crucially not misdiagnosed as a CM failure.
        assert "Context Manager" not in result["body"]

    def test_malformed_envelope_json_returns_502(self):
        """An invoke that succeeded at the control-plane level can still return
        bytes that aren't valid JSON at the outer envelope (proxy crashed
        inside its Lambda wrapper). Must produce a clear 502, not raise
        through the router."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "StatusCode": 200,
            "Payload": io.BytesIO(b"not-valid-json{{{"),
        }
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Schema backend returned invalid JSON" in result["body"]

    def test_function_error_does_not_leak_lambda_internals(self):
        """The FunctionError payload can carry ARNs, request IDs, stack frames,
        or model-supplied text. The response body MUST be a generic message;
        the truncated preview is logged server-side, never returned."""
        secret_payload = (
            b'{"errorMessage": "Traceback... at /var/task/handler.py line 42: '
            b'arn:aws:iam::123456789012:role/internal-role", "errorType": "RuntimeError"}'
        )
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(secret_payload),
        }
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/schema", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        body_text = result["body"]
        assert "Schema backend failed" in body_text
        # Nothing from the Lambda error payload may reach the customer.
        assert "Traceback" not in body_text
        assert "arn:aws:iam" not in body_text
        assert "handler.py" not in body_text
        assert "RuntimeError" not in body_text


@pytest.mark.unit
class TestListMetrics:
    """GET /namespaces/{ns}/metrics proxies to the metric-service Lambda.

    Same rationale as DescribeSchema: pure catalog read, no tier orchestration,
    both surfaces (UI/data-layer and MCP ``discovery.list_metrics``) must return
    the same body for the same input.
    """

    def _fake_proxy_response(self, body: dict, status: int = 200) -> dict:
        """Shape a synthetic Lambda invoke response with an API GW proxy body."""
        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(
                json.dumps(
                    {
                        "statusCode": status,
                        "body": json.dumps(body),
                        "headers": {},
                    }
                ).encode("utf-8")
            ),
        }

    def test_returns_metrics_from_service(self):
        proxy_body = {
            "metrics": [
                {"metricId": "m1", "name": "Revenue", "description": "Total revenue"},
                {"metricId": "m2", "name": "Churn", "description": "Monthly churn"},
            ],
            "nextToken": None,
        }
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["metrics"] == proxy_body["metrics"]
        assert body["nextToken"] is None

    def test_forwards_camelcase_query_params_verbatim(self):
        """The metric-service Lambda expects the Smithy camelCase names —
        ``maxResults`` and ``nextToken`` — passed through unchanged.

        ``status`` is intentionally NOT forwarded: the Smithy contract
        declares it but the metric-service backend does not filter on it
        today, so forwarding would make an unfiltered response look filtered.
        This test sends ``status`` in the incoming request and pins that
        it does not reach the backend, so a future revert of the drop
        surfaces here rather than as a silent-wrong-answer bug (MR !939
        comment ``b6dea1de``).
        """
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response({"metrics": [], "nextToken": None})
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            event["queryStringParameters"] = {
                "maxResults": "50",
                "nextToken": "opaque-cursor",
                "status": "ACTIVE",
            }
            handler(event, None)
        forwarded = json.loads(mock_client.invoke.call_args.kwargs["Payload"].decode())
        assert forwarded["queryStringParameters"] == {
            "maxResults": "50",
            "nextToken": "opaque-cursor",
        }
        assert forwarded["resource"] == "/namespaces/{namespaceId}/metrics"

    def test_bearer_token_forwarded(self):
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response({"metrics": [], "nextToken": None})
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            event["headers"] = {"Authorization": "Bearer forwarded-token"}
            handler(event, None)
        forwarded = json.loads(mock_client.invoke.call_args.kwargs["Payload"].decode())
        assert forwarded["headers"]["Authorization"] == "Bearer forwarded-token"

    def test_missing_env_var_returns_501(self):
        with patch.object(handler_module, "METRIC_SERVICE_LAMBDA_ARN", ""):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 501
        assert "METRIC_SERVICE_LAMBDA_ARN" in result["body"]

    def test_proxy_error_status_propagates(self):
        proxy_body = {"message": "namespace not found"}
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body, status=404)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 404
        assert "namespace not found" in result["body"]

    def test_client_error_on_invoke_maps_to_502(self):
        """boto3 ClientError (throttle / permissions / timeout) becomes a 502
        with a stable, metric-specific message — must NOT be misdiagnosed as a
        Context Manager failure by the router's generic exception path."""
        mock_client = MagicMock()
        mock_client.invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Invoke",
        )
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Failed to invoke metric-service Lambda" in result["body"]
        assert "Context Manager" not in result["body"]

    def test_malformed_envelope_json_returns_502(self):
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "StatusCode": 200,
            "Payload": io.BytesIO(b"not-valid-json{{{"),
        }
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        assert "Metric service returned invalid JSON" in result["body"]

    def test_function_error_does_not_leak_lambda_internals(self):
        """FunctionError payloads can carry ARNs / tracebacks / stack frames.
        Only a generic message may surface; details go to server-side logs."""
        secret_payload = (
            b'{"errorMessage": "Traceback... at /var/task/handler.py: '
            b'arn:aws:iam::123456789012:role/internal-role", "errorType": "RuntimeError"}'
        )
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": io.BytesIO(secret_payload),
        }
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        assert result["statusCode"] == 502
        body_text = result["body"]
        assert "Metric service backend failed" in body_text
        assert "Traceback" not in body_text
        assert "arn:aws:iam" not in body_text
        assert "handler.py" not in body_text
        assert "RuntimeError" not in body_text

    def test_response_does_not_leak_extra_fields(self):
        """The Smithy output is ``metrics`` + ``nextToken`` only. A future
        backend change must not add fields to the customer response by accident
        (the metric-service Lambda echoes ``namespace`` today — same case as
        DescribeSchema)."""
        proxy_body = {
            "metrics": [{"metricId": "m1", "name": "Revenue"}],
            "nextToken": "cursor-2",
            "namespace": "9142f178-...",  # echoed today
            "internal_debug": "should never surface",
        }
        mock_client = MagicMock()
        mock_client.invoke.return_value = self._fake_proxy_response(proxy_body)
        with patch.object(handler_module, "_get_lambda_client", return_value=mock_client):
            event = _api_event("GET", "/namespaces/{namespaceId}/metrics", body=None)
            result = handler(event, None)
        body = json.loads(result["body"])
        assert set(body.keys()) == {"metrics", "nextToken"}


@pytest.mark.unit
class TestQueryLengthBoundReportsItsOwnReason:
    """An over-long query must say so, not claim the field is missing.

    ``validate_query_text`` bounds the query in CODE POINTS, which is the correct
    unit for a multilingual API: a byte bound would give a CJK caller a third of
    the Latin allowance for the same character count. The handler used to collapse
    every ``ValueError`` from it into "Missing required field: query", so a caller
    who sent a 5000-character query was told the field was absent and had nothing
    to act on.
    """

    ROUTES = (
        "/namespaces/{namespaceId}/query",
        "/namespaces/{namespaceId}/translate",
        "/namespaces/{namespaceId}/kb/search",
    )

    @staticmethod
    def _limit() -> int:
        from coa_common.constants import MAX_QUERY_CODEPOINTS

        return MAX_QUERY_CODEPOINTS

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize(
        ("label", "char"),
        [("latin", "a"), ("cjk", "あ"), ("astral", "𠮷")],
    )
    def test_over_limit_query_reports_the_length_bound(self, route, label, char):
        event = _api_event("POST", route, body={"query": char * (self._limit() + 1)})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing required field" not in result["body"], (
            f"{label} over-limit query on {route} still reports the field as missing"
        )
        assert str(self._limit()) in result["body"], (
            "the message should name the limit so the caller knows what to trim to"
        )

    @pytest.mark.parametrize(
        ("label", "char"),
        [("latin", "a"), ("cjk", "あ"), ("astral", "𠮷")],
    )
    def test_at_the_limit_is_accepted_in_every_script(self, label, char):
        """The bound is code points, so the limit is reached at the same COUNT.

        Astral characters are one code point each in this count even though Python
        stores them as one character and UTF-8 spends four bytes on them, which is
        the property that keeps the allowance script-independent.
        """
        query = char * self._limit()
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": query})
        with patch.object(handler_module, "_invoke_context_manager") as invoke:
            invoke.return_value = {"result": {}, "requestId": "r", "sessionId": "s"}
            result = handler(event, None)
        assert result["statusCode"] == 200, f"{label} query of exactly {self._limit()} code points was rejected"
        assert invoke.call_args.args[0]["query"] == query

    def test_non_string_query_reports_the_type(self):
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"query": 42})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "must be a string" in result["body"]

    def test_absent_field_still_reports_absence(self):
        """The original message must survive for the case it actually describes."""
        event = _api_event("POST", "/namespaces/{namespaceId}/query", body={"tierOverride": 2})
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "Missing required field: query" in result["body"]
