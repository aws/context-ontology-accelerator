# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP server tool handlers and auth/profile helpers.

The 6 tools are thin protocol adapters: authenticate, resolve profile,
delegate to a backend client, serialize JSON, audit-log. These tests mock
the backend clients and delegate functions to verify the adapter behavior
(JSON output, auth branching, error mapping) without any network calls.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from coa_mcp import server
from coa_mcp.auth.grant_resolver import GrantResolutionError
from coa_mcp.tools.execution import ContextManagerError

from .conftest import _TEST_AUDIENCE, _TEST_ISSUER, _TEST_KID, _private_key, create_test_token


class _FakeRequest:
    """Minimal stand-in for a Starlette request with a headers mapping."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, request: _FakeRequest | None) -> None:
        self.request = request


class _FakeCtx:
    """Fake FastMCP Context exposing request_context.request.headers."""

    def __init__(self, auth_header: str | None) -> None:
        headers = {"authorization": auth_header} if auth_header is not None else {}
        self.request_context = _FakeRequestContext(_FakeRequest(headers))


class _BadCtx:
    """Context whose request_context access raises — exercises the swallow branch."""

    @property
    def request_context(self):  # noqa: ANN201 - deliberate raise for test
        raise AttributeError("no request context")


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset lazy client singletons so each test starts clean."""
    server._lambda_client = None
    server._cm_client = None
    yield
    server._lambda_client = None
    server._cm_client = None


def _serve_profile(namespace: str = "sales", allowed_metrics: list[str] | None = None):
    from coa_serve.role_resolver import ResolvedProfile as ServeProfile

    return ServeProfile(
        user_id="alice",
        groups=["viewer"],
        global_roles=["data-viewer"],
        resource_roles=[{"role": "namespace-reader", "resourceUID": namespace}],
        table_allowlist=["orders"],
        column_denylist={},
        allowed_metrics=allowed_metrics,
    )


# ── _extract_bearer_token ─────────────────────────────────────────────


@pytest.mark.unit
class TestExtractBearerToken:
    def test_auth_disabled_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", False)
        assert server._extract_bearer_token(_FakeCtx("Bearer abc")) == ""

    def test_none_context_when_auth_enabled_raises(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        with pytest.raises(ValueError, match="request context is None"):
            server._extract_bearer_token(None)

    def test_strips_bearer_prefix(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        assert server._extract_bearer_token(_FakeCtx("Bearer my-jwt")) == "my-jwt"

    def test_returns_raw_header_without_bearer_prefix(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        assert server._extract_bearer_token(_FakeCtx("raw-token-value")) == "raw-token-value"

    def test_missing_header_raises(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        with pytest.raises(ValueError, match="no Authorization header"):
            server._extract_bearer_token(_FakeCtx(None))

    def test_context_access_error_raises(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        with pytest.raises(ValueError, match="no Authorization header"):
            server._extract_bearer_token(_BadCtx())


# ── _build_authorizer_context ─────────────────────────────────────────


@pytest.mark.unit
class TestBuildAuthorizerContext:
    def test_builds_context_from_caller_and_profile(self):
        caller = server.CallerIdentity(
            user_id="alice",
            agent_id="agent-1",
            namespaces=["sales"],
            roles=["viewer", "editor"],
            raw_claims={"email": "alice@example.com"},
        )
        ctx = server._build_authorizer_context(caller, {"globalRoles": ["data-viewer"]})
        assert ctx["principalId"] == "alice"
        assert ctx["userId"] == "alice"
        assert ctx["email"] == "alice@example.com"
        assert ctx["groups"] == "viewer,editor"
        assert ctx["globalRoles"] == "data-viewer"

    def test_email_falls_back_to_user_id(self):
        caller = server.CallerIdentity(
            user_id="bob",
            agent_id=None,
            namespaces=[],
            roles=[],
            raw_claims={},
        )
        ctx = server._build_authorizer_context(caller, {})
        assert ctx["email"] == "bob"
        assert ctx["groups"] == ""


# ── _resolve_caller_and_profile ───────────────────────────────────────


@pytest.mark.unit
class TestResolveCallerAndProfile:
    @pytest.mark.asyncio
    async def test_auth_disabled_returns_local_dev(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", False)
        caller, profile = await server._resolve_caller_and_profile("ns1", None)
        assert caller.user_id == "local-dev"
        assert profile == {"namespace": "ns1"}

    @pytest.mark.asyncio
    async def test_auth_disabled_swallows_context_error(self, monkeypatch):
        """A context that raises during header access must not break local-dev mode."""
        monkeypatch.setattr(server.config, "auth_enabled", False)
        caller, _ = await server._resolve_caller_and_profile("ns1", _BadCtx())
        assert caller.user_id == "local-dev"

    @pytest.mark.asyncio
    async def test_auth_enabled_resolves_profile(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        monkeypatch.setattr(
            "coa_serve.role_resolver.resolve_profile",
            lambda **kwargs: _serve_profile(),
        )
        token = create_test_token(user_id="alice", namespaces=["sales"], roles=["viewer"])
        caller, profile = await server._resolve_caller_and_profile(
            "sales", _FakeCtx(f"Bearer {token}"), cedar_action="viewNamespace"
        )
        assert caller.user_id == "alice"
        assert profile["namespace"] == "sales"
        assert profile["idpGroups"] == ["viewer"]

    @pytest.mark.asyncio
    async def test_email_claim_overrides_user_id(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        monkeypatch.setattr(
            "coa_serve.role_resolver.resolve_profile",
            lambda **kwargs: _serve_profile(),
        )
        payload = {
            "sub": "alice",
            "email": "alice@example.com",
            "namespaces": ["sales"],
            "cognito:groups": ["viewer"],
            "iss": _TEST_ISSUER,
            "aud": _TEST_AUDIENCE,
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, _private_key, algorithm="RS256", headers={"kid": _TEST_KID})
        _, profile = await server._resolve_caller_and_profile("sales", _FakeCtx(f"Bearer {token}"))
        assert profile["userId"] == "alice@example.com"


# ── Lazy client singletons ────────────────────────────────────────────


@pytest.mark.unit
class TestClientSingletons:
    def test_lambda_client_is_cached(self, monkeypatch):
        created: list[str] = []

        def fake_ctor(region):
            created.append(region)
            return object()

        monkeypatch.setattr(server, "LambdaClient", fake_ctor)
        first = server._get_lambda_client()
        second = server._get_lambda_client()
        assert first is second
        assert created == [server.config.aws_region]

    def test_cm_client_missing_arn_raises(self, monkeypatch):
        monkeypatch.delenv("CM_RUNTIME_ARN", raising=False)
        with pytest.raises(RuntimeError, match="CM_RUNTIME_ARN"):
            server._get_cm_client()

    def test_cm_client_is_cached(self, monkeypatch):
        monkeypatch.setenv("CM_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/cm")
        created: list[dict] = []

        def fake_ctor(runtime_arn, region):
            created.append({"arn": runtime_arn, "region": region})
            return object()

        monkeypatch.setattr(server, "ContextManagerClient", fake_ctor)
        first = server._get_cm_client()
        second = server._get_cm_client()
        assert first is second
        assert len(created) == 1


# ── Tool handlers ─────────────────────────────────────────────────────


@pytest.fixture
def _local_dev(monkeypatch):
    """Run tools in local-dev (auth-disabled) mode with a mocked Lambda client."""
    monkeypatch.setattr(server.config, "auth_enabled", False)
    monkeypatch.setattr(server, "_get_lambda_client", lambda: object())
    monkeypatch.setattr(server, "_get_cm_client", lambda: object())


@pytest.mark.unit
class TestListMetricsTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_metrics(self, monkeypatch, _local_dev):
        # Use **kwargs so the fake tolerates the added Smithy-parity kwargs
        # (next_token, status) without every test being updated in lockstep.
        async def fake_delegate(lc, ns, token, **kwargs):
            return {"metrics": [{"name": "Revenue"}], "namespace": ns}

        monkeypatch.setattr(server.discovery, "list_metrics", fake_delegate)
        out = await server.list_metrics(None, "sales")
        parsed = json.loads(out)
        assert parsed["metrics"][0]["name"] == "Revenue"
        assert parsed["namespace"] == "sales"

    @pytest.mark.asyncio
    async def test_local_dev_passes_no_authorizer_context(self, monkeypatch, _local_dev):
        captured: dict = {}

        async def fake_delegate(lc, ns, token, **kwargs):
            captured["authorizer_context"] = kwargs.get("authorizer_context")
            return {"metrics": []}

        monkeypatch.setattr(server.discovery, "list_metrics", fake_delegate)
        await server.list_metrics(None, "sales")
        assert captured["authorizer_context"] is None

    @pytest.mark.asyncio
    async def test_auth_error_maps_to_value_error(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        with pytest.raises(ValueError, match="Missing Authorization header"):
            await server.list_metrics(_FakeCtx(None), "sales")

    @pytest.mark.asyncio
    async def test_backend_error_reraised(self, monkeypatch, _local_dev):
        async def boom(lc, ns, token, **kwargs):
            raise RuntimeError("lambda exploded")

        monkeypatch.setattr(server.discovery, "list_metrics", boom)
        with pytest.raises(RuntimeError, match="lambda exploded"):
            await server.list_metrics(None, "sales")

    @pytest.mark.asyncio
    async def test_auth_enabled_builds_authorizer_context(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)
        monkeypatch.setattr(server, "_get_lambda_client", lambda: object())
        monkeypatch.setattr(
            "coa_serve.role_resolver.resolve_profile",
            lambda **kwargs: _serve_profile(),
        )
        captured: dict = {}

        async def fake_delegate(lc, ns, token, **kwargs):
            captured["authorizer_context"] = kwargs.get("authorizer_context")
            captured["token"] = token
            return {"metrics": []}

        monkeypatch.setattr(server.discovery, "list_metrics", fake_delegate)
        token = create_test_token(user_id="alice", namespaces=["sales"], roles=["viewer"])
        await server.list_metrics(_FakeCtx(f"Bearer {token}"), "sales")
        assert captured["authorizer_context"] is not None
        assert captured["authorizer_context"]["principalId"] == "alice"
        assert captured["token"] == token

    @pytest.mark.asyncio
    async def test_forwards_next_token(self, monkeypatch, _local_dev):
        """Regression for the pagination gap: nextToken must reach the
        discovery helper. The MCP tool accepts it in camelCase (matching
        Smithy) and hands it to the helper as snake_case.

        ``status`` is intentionally not part of the MCP tool signature — the
        metric-service backend does not filter on it today, so accepting it
        here would silently return an unfiltered response as if filtered.
        Pin its absence so a future revert reintroduces the failure here
        rather than as a silent-wrong-answer bug in production.
        """
        captured: dict = {}

        async def fake_delegate(lc, ns, token, **kwargs):
            captured.update(kwargs)
            return {"metrics": [], "nextToken": None}

        monkeypatch.setattr(server.discovery, "list_metrics", fake_delegate)
        await server.list_metrics(None, "sales", nextToken="cursor-abc")
        assert captured["next_token"] == "cursor-abc"
        assert "status" not in captured


@pytest.mark.unit
class TestDescribeSchemaTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_classes(self, monkeypatch, _local_dev):
        # **kwargs to absorb newly-added class_filter without churn on
        # unrelated tests.
        async def fake_delegate(lc, ns, token, **kwargs):
            return {"classes": [{"label": "Order"}], "ontologyVersion": "v2"}

        monkeypatch.setattr(server.discovery, "describe_schema", fake_delegate)
        out = await server.describe_schema(None, "sales")
        parsed = json.loads(out)
        assert parsed["classes"][0]["label"] == "Order"

    @pytest.mark.asyncio
    async def test_forwards_class_filter(self, monkeypatch, _local_dev):
        """Missing Smithy input the previous tool signature dropped — now
        reaches the discovery helper."""
        captured: dict = {}

        async def fake_delegate(lc, ns, token, **kwargs):
            captured.update(kwargs)
            return {"classes": []}

        monkeypatch.setattr(server.discovery, "describe_schema", fake_delegate)
        await server.describe_schema(None, "sales", classFilter="https://schema.org/Order")
        assert captured["class_filter"] == "https://schema.org/Order"

    @pytest.mark.asyncio
    async def test_grant_error_maps_to_value_error(self, monkeypatch):
        monkeypatch.setattr(server.config, "auth_enabled", True)

        async def deny(*args, **kwargs):
            raise GrantResolutionError("nope")

        monkeypatch.setattr(server, "resolve_grant", deny)
        token = create_test_token(user_id="alice", namespaces=["sales"])
        with pytest.raises(ValueError, match="nope"):
            await server.describe_schema(_FakeCtx(f"Bearer {token}"), "sales")

    @pytest.mark.asyncio
    async def test_backend_error_reraised(self, monkeypatch, _local_dev):
        async def boom(*args, **kwargs):
            raise RuntimeError("ontology proxy down")

        monkeypatch.setattr(server.discovery, "describe_schema", boom)
        with pytest.raises(RuntimeError, match="ontology proxy down"):
            await server.describe_schema(None, "sales")


@pytest.mark.unit
class TestQueryTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_result(self, monkeypatch, _local_dev):
        # Return the Smithy envelope shape so the tool can pass it through.
        async def fake_delegate(cm, query_text, ns, profile, token, **kwargs):
            return {
                "result": {"tier": 2, "confidence": {"score": 0.9}, "resultRows": [{"n": 1}]},
                "requestId": "req-1",
                "sessionId": "sess-1",
            }

        monkeypatch.setattr(server.execution, "execute_query", fake_delegate)
        out = await server.query(None, "revenue?", "sales")
        parsed = json.loads(out)
        # Envelope now surfaces top-level, matching data-layer's Query output.
        assert parsed["result"]["tier"] == 2
        assert parsed["result"]["resultRows"] == [{"n": 1}]
        assert parsed["requestId"] == "req-1"
        assert parsed["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_forwards_tool_args(self, monkeypatch, _local_dev):
        """The tool accepts Smithy camelCase from callers (tierOverride,
        maxResults, includeSupporting) and hands the helper snake_case."""
        captured: dict = {}

        async def fake_delegate(cm, query_text, ns, profile, token, **kwargs):
            captured.update(kwargs)
            captured["query"] = query_text
            return {"result": {"tier": 1}}

        monkeypatch.setattr(server.execution, "execute_query", fake_delegate)
        await server.query(None, "hi", "sales", tierOverride=1, maxResults=5, includeSupporting=False)
        assert captured["query"] == "hi"
        assert captured["tier_override"] == 1
        assert captured["max_results"] == 5
        assert captured["include_supporting"] is False

    @pytest.mark.asyncio
    async def test_cm_error_preserves_status_code(self, monkeypatch, _local_dev):
        """When CM returns a >=400 the tool must surface the code, not
        collapse it into a generic Python exception the way ``raise`` used to.
        Same posture as data-layer's ``_error_response(status_code, message)``."""

        async def deny(cm, query_text, ns, profile, token, **kwargs):
            raise ContextManagerError(403, "Access denied on namespace 'sales'")

        monkeypatch.setattr(server.execution, "execute_query", deny)
        with pytest.raises(ValueError, match=r"CM 403: Access denied"):
            await server.query(None, "hi", "sales")

    @pytest.mark.asyncio
    async def test_forwards_new_smithy_inputs(self, monkeypatch, _local_dev):
        """execute + dimensions + mode — Smithy inputs the previous signature
        dropped — now reach the helper.

        ``timeoutMs`` is intentionally not in the MCP tool signature — CM has
        no reader for ``options.timeoutMs`` today, so accepting it would
        silently no-op. Pin its absence so a future revert doesn't reintroduce
        the silent-ignore that MR !939 comment ``b6dea1de`` flagged.
        """
        captured: dict = {}

        async def fake_delegate(cm, query_text, ns, profile, token, **kwargs):
            captured.update(kwargs)
            return {"result": {}}

        monkeypatch.setattr(server.execution, "execute_query", fake_delegate)
        await server.query(
            None,
            "hi",
            "sales",
            execute=False,
            mode="agentic",
            dimensions=[{"name": "region", "value": "us-east"}],
        )
        assert captured["execute"] is False
        assert captured["mode"] == "agentic"
        assert captured["dimensions"] == [{"name": "region", "value": "us-east"}]
        assert "timeout_ms" not in captured

    @pytest.mark.asyncio
    async def test_backend_error_reraised(self, monkeypatch, _local_dev):
        async def boom(*args, **kwargs):
            raise RuntimeError("cm down")

        monkeypatch.setattr(server.execution, "execute_query", boom)
        with pytest.raises(RuntimeError, match="cm down"):
            await server.query(None, "hi", "sales")


@pytest.mark.unit
class TestTranslateSparqlTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_sparql(self, monkeypatch, _local_dev):
        async def fake_delegate(cm, text, ns, profile, token):
            return {"sparqlQuery": "SELECT ?x", "confidence": {"score": 0.9}}

        monkeypatch.setattr(server.execution, "execute_translate_sparql", fake_delegate)
        out = await server.translate_sparql(None, "revenue", "sales")
        parsed = json.loads(out)
        assert parsed["sparqlQuery"] == "SELECT ?x"

    @pytest.mark.asyncio
    async def test_backend_error_reraised(self, monkeypatch, _local_dev):
        async def boom(*args, **kwargs):
            raise RuntimeError("translate failed")

        monkeypatch.setattr(server.execution, "execute_translate_sparql", boom)
        with pytest.raises(RuntimeError, match="translate failed"):
            await server.translate_sparql(None, "revenue", "sales")


@pytest.mark.unit
class TestRagRetrievalTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_chunks(self, monkeypatch, _local_dev):
        captured: dict = {}

        async def fake_delegate(cm, query_text, ns, profile, token, **kwargs):
            captured.update(kwargs)
            return {"chunks": [{"text": "chunk"}]}

        monkeypatch.setattr(server.execution, "execute_rag_retrieval", fake_delegate)
        # Smithy camelCase at the tool boundary (topK, minScore).
        out = await server.rag_retrieval(None, "revenue", "sales", topK=3, minScore=0.5)
        parsed = json.loads(out)
        assert parsed["chunks"][0]["text"] == "chunk"
        assert captured["top_k"] == 3
        assert captured["min_score"] == 0.5

    @pytest.mark.asyncio
    async def test_forwards_source_and_entity_filters(self, monkeypatch, _local_dev):
        """Missing Smithy inputs the previous tool signature dropped — now reach the helper."""
        captured: dict = {}

        async def fake_delegate(cm, query_text, ns, profile, token, **kwargs):
            captured.update(kwargs)
            return {"chunks": []}

        monkeypatch.setattr(server.execution, "execute_rag_retrieval", fake_delegate)
        await server.rag_retrieval(
            None,
            "revenue",
            "sales",
            sourceFilter=["doc-1", "doc-2"],
            entityFilter=["urn:e:1"],
        )
        assert captured["source_filter"] == ["doc-1", "doc-2"]
        assert captured["entity_filter"] == ["urn:e:1"]

    @pytest.mark.asyncio
    async def test_backend_error_reraised(self, monkeypatch, _local_dev):
        async def boom(*args, **kwargs):
            raise RuntimeError("kb search failed")

        monkeypatch.setattr(server.execution, "execute_rag_retrieval", boom)
        with pytest.raises(RuntimeError, match="kb search failed"):
            await server.rag_retrieval(None, "revenue", "sales")


@pytest.mark.unit
class TestGraphTraversalTool:
    @pytest.mark.asyncio
    async def test_returns_serialized_graph(self, monkeypatch, _local_dev):
        async def fake_delegate(cm, start_uri, ns, profile, token, **kwargs):
            return {"entities": [{"uri": start_uri}], "relationships": []}

        monkeypatch.setattr(server.execution, "execute_graph_traversal", fake_delegate)
        out = await server.graph_traversal(None, "urn:c:1", "sales")
        parsed = json.loads(out)
        assert parsed["entities"][0]["uri"] == "urn:c:1"

    @pytest.mark.asyncio
    async def test_invalid_direction_reraised(self, monkeypatch, _local_dev):
        async def bad_direction(*args, **kwargs):
            raise ValueError("Invalid direction 'x'")

        monkeypatch.setattr(server.execution, "execute_graph_traversal", bad_direction)
        with pytest.raises(ValueError, match="Invalid direction"):
            await server.graph_traversal(None, "urn:c:1", "sales", direction="x")

    @pytest.mark.asyncio
    async def test_forwards_relationship_filter(self, monkeypatch, _local_dev):
        """relationshipFilter — Smithy input the previous tool signature dropped."""
        captured: dict = {}

        async def fake_delegate(cm, start_uri, ns, profile, token, **kwargs):
            captured.update(kwargs)
            return {"entities": [], "relationships": []}

        monkeypatch.setattr(server.execution, "execute_graph_traversal", fake_delegate)
        await server.graph_traversal(
            None,
            "urn:c:1",
            "sales",
            relationshipFilter=["skos:related"],
        )
        assert captured["relationship_filter"] == ["skos:related"]
