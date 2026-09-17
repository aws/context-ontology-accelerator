# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"Unit tests for Context Manager entrypoint logic."

from __future__ import annotations

import importlib.util
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.config import ServiceConfig
from coa_serve.models import ConfidenceScore, InvokeRequest, InvokeResponse, QueryResult, TraceStep
from coa_serve.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

HAS_AGENTCORE = importlib.util.find_spec("bedrock_agentcore") is not None

agentcore_required = pytest.mark.skipif(not HAS_AGENTCORE, reason="bedrock_agentcore SDK not installed")


async def _invoke_collect(payload, context=None):
    """Helper: collect all yielded items from the async generator entrypoint."""
    from coa_serve.main import invoke

    results = []
    async for item in invoke(payload, context):
        results.append(item)
    return results[0] if len(results) == 1 else results


# Real reference to the admission gate, captured before any patching so the
# dedicated gate tests below can restore it over the module-level bypass.
from coa_serve.main import _authorize_namespace_access as _REAL_ADMISSION_GATE  # noqa: E402


@pytest.fixture(autouse=True)
def _bypass_namespace_admission():
    """Bypass the F-2 Cedar admission gate for the pre-existing behavioral tests.

    These tests predate the gate and exercise downstream dispatch/resolution with
    bare ``MagicMock`` orchestrators (whose ``authorize_namespace().denied`` would
    be truthy). Patching the gate to a no-op ALLOW keeps them focused on what they
    assert; the gate itself is covered end-to-end by ``TestNamespaceAdmissionGate``
    (which restores the real gate) and at the decision level in
    ``test_authz_invariants.py``. Returning ``(None, None)`` means "allowed, no
    pre-resolved profile" so the query path still runs its own resolve_profile —
    preserving the profile-resolution tests unchanged.
    """
    with patch("coa_serve.main._authorize_namespace_access", new_callable=AsyncMock, return_value=(None, None)):
        yield


@pytest.fixture
def config():
    return ServiceConfig(
        guardrail_id="test-guardrail",
        vkg_endpoint="http://vkg.local:8080",
        neptune_endpoint="",
        opensearch_endpoint="",
        bedrock_model_id="test-model",
        bedrock_region="us-east-1",
        data_sources_table="",
        metric_definitions_table="",
        memory_id="test-memory",
        session_metadata_table="test-session-metadata",
    )


@pytest.fixture
def orchestrator():
    from unittest.mock import AsyncMock, MagicMock

    from coa_serve.tier1.metric_resolver import MetricMatch
    from coa_serve.tier2.strategy import StrategyOption, StructuredQueryTier
    from coa_serve.tier3.knowledge_retriever import Tier3Result

    metric_resolver = AsyncMock()
    metric_resolver.match.return_value = MetricMatch(found=False)

    mock_strategy = MagicMock()
    mock_strategy.name = StrategyOption.ONTOP
    mock_strategy.resolve = AsyncMock(return_value=None)
    structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

    knowledge_retriever = AsyncMock()
    knowledge_retriever.lexical_enabled = False
    knowledge_retriever.resolve.return_value = Tier3Result(
        synthesized_answer="stub",
        supporting_content=(),
        graph_context=(),
        confidence=0.5,
        trace_steps=({"step": "stub", "status": "success", "durationMs": 0},),
    )
    return Orchestrator(
        metric_resolver=metric_resolver,
        knowledge_retriever=knowledge_retriever,
        structured_query_tier=structured_query_tier,
    )


@pytest.mark.unit
class TestInvokeEntrypoint:
    """Test the invoke logic independent of AgentCore transport."""

    async def test_valid_payload_returns_response(self, orchestrator):
        request = InvokeRequest(query="What is revenue?", namespace="demo")
        response = await orchestrator.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 3  # falls through to tier3 with stub resolvers
        assert response.result.metadata["namespace"] == "demo"

    async def test_response_includes_trace(self, orchestrator):
        request = InvokeRequest(query="test", namespace="ns1")
        response = await orchestrator.resolve(request)

        assert len(response.result.trace) >= 1

    async def test_response_serialization(self, orchestrator):
        request = InvokeRequest(query="test", namespace="demo")
        response = await orchestrator.resolve(request)

        serialized = response.model_dump(by_alias=True, exclude_none=True)
        assert "result" in serialized
        assert "confidence" in serialized["result"]
        assert "score" in serialized["result"]["confidence"]

    async def test_payload_with_profile_and_options(self, orchestrator):
        request = InvokeRequest(
            query="Show claims",
            namespace="demo",
            profile={"user_id": "u1"},
            options={"startTier": 3},
        )
        response = await orchestrator.resolve(request)
        assert isinstance(response, InvokeResponse)

    async def test_query_max_length_enforced(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InvokeRequest(query="x" * 4001, namespace="demo")

    async def test_empty_query_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InvokeRequest(query="", namespace="demo")

    async def test_whitespace_only_query_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InvokeRequest(query=" ", namespace="demo")

    async def test_missing_namespace_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InvokeRequest(query="test")

    async def test_invalid_namespace_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InvokeRequest(query="test", namespace="bad namespace!")


@pytest.mark.unit
class TestInvokePayloadParsing:
    """Test that raw payload dicts are correctly parsed into InvokeRequest."""

    def test_minimal_payload(self):
        payload = {"query": "What is revenue?", "namespace": "demo"}
        request = InvokeRequest(**payload)
        assert request.query == "What is revenue?"
        assert request.namespace == "demo"
        assert request.profile == {}
        assert request.options == {}

    def test_full_payload(self):
        payload = {
            "query": "test",
            "namespace": "ns1",
            "profile": {"user_id": "u1", "role": "analyst"},
            "options": {"tierOverride": 1, "maxResults": 500},
        }
        request = InvokeRequest(**payload)
        assert request.profile["user_id"] == "u1"
        assert request.options["tierOverride"] == 1


@pytest.mark.unit
@agentcore_required
class TestInvokeFunction:
    """Test the actual invoke() function end-to-end with mocked config."""

    @pytest.fixture(autouse=True)
    def reset_main_state(self):
        """Reset module-level state between tests that call invoke()."""
        import coa_serve.main as main_mod

        main_mod._config = None
        main_mod._orchestrator = None
        main_mod._sources_registry = None
        yield
        main_mod._config = None
        main_mod._orchestrator = None
        main_mod._sources_registry = None

    @pytest.fixture
    def stub_orchestrator(self):
        """Pre-configured mock orchestrator that returns a valid Tier-2 response."""
        import coa_serve.main as main_mod

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=2,
                    confidence=ConfidenceScore(score=0.9, rationale="ok"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={},
                ),
            )
        )
        main_mod._orchestrator = mock_orch
        return mock_orch

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_valid_payload(self, mock_init, config):
        import coa_serve.main as main_mod

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=3,
                    confidence=ConfidenceScore(score=0.5, rationale="stub"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={"namespace": "demo"},
                ),
            )
        )
        main_mod._orchestrator = mock_orch
        mock_init.return_value = None

        result = await _invoke_collect({"query": "What is revenue?", "namespace": "demo"})

        assert "requestId" in result

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_propagates_caller_request_id(self, mock_init, config):
        import coa_serve.main as main_mod

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=3,
                    confidence=ConfidenceScore(score=0.5, rationale="stub"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={},
                ),
            )
        )
        main_mod._orchestrator = mock_orch
        mock_init.return_value = None

        result = await _invoke_collect(
            {
                "query": "test",
                "namespace": "demo",
                "requestId": "caller-req-123",
            }
        )

        assert result["requestId"] == "caller-req-123"

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_generates_request_id_when_missing(self, mock_init, config):
        import coa_serve.main as main_mod

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=3,
                    confidence=ConfidenceScore(score=0.5, rationale="stub"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={},
                ),
            )
        )
        main_mod._orchestrator = mock_orch
        mock_init.return_value = None

        result = await _invoke_collect({"query": "test", "namespace": "demo"})

        assert "requestId" in result
        assert len(result["requestId"]) > 0

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_invalid_payload_returns_400(self, mock_init, config):
        mock_init.return_value = None
        import coa_serve.main as main_mod

        main_mod._orchestrator = MagicMock()

        result = await _invoke_collect({"query": "", "namespace": ""})

        assert result["statusCode"] == 400
        assert result["error"] == "ValidationError"
        assert result["message"] == "Invalid request payload"
        assert "details" in result
        assert "requestId" in result

    @pytest.mark.parametrize("action", [None, "translate", "kbSearch"])
    @pytest.mark.parametrize("query", [42, "x" * 4001])
    @patch("coa_serve.main._ensure_initialized")
    async def test_query_boundary_rejects_before_namespace_or_action_backend(
        self, mock_init, action, query, stub_orchestrator
    ):
        mock_init.return_value = None
        import coa_serve.main as main_mod

        registry = MagicMock()
        registry.namespace_exists = AsyncMock(return_value=True)
        main_mod._sources_registry = registry
        payload = {"query": query, "namespace": "demo"}
        if action is not None:
            payload["action"] = action

        result = await _invoke_collect(payload)

        assert result["statusCode"] == 400
        assert result["error"] == "ValidationError"
        registry.namespace_exists.assert_not_called()
        stub_orchestrator.resolve.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_malformed_namespace_rejected_before_existence_lookup(self, mock_init, config):
        """Path-traversal / special-char namespaces must return 400, never fall through
        to the namespace_exists 404 gate — otherwise callers can't distinguish "bad
        input" from "unknown namespace" and validation tests break.
        """
        mock_init.return_value = None
        import coa_serve.main as main_mod

        main_mod._orchestrator = MagicMock()
        registry = MagicMock()
        registry.namespace_exists = AsyncMock(return_value=False)
        main_mod._sources_registry = registry

        result = await _invoke_collect({"query": "test", "namespace": "../../etc/passwd"})

        assert result["statusCode"] == 400
        assert result["error"] == "ValidationError"
        registry.namespace_exists.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    async def test_indeterminate_existence_fails_closed_when_table_configured(self, mock_init, config):
        """F-2: a lookup error (None) with the namespaces table configured must fail
        CLOSED — never dispatch a query against a namespace we could not verify.
        """
        mock_init.return_value = None
        import coa_serve.main as main_mod

        orch = MagicMock()
        orch.resolve = AsyncMock()
        main_mod._orchestrator = orch
        registry = MagicMock()
        registry.namespace_exists = AsyncMock(return_value=None)
        registry.namespaces_configured = True
        main_mod._sources_registry = registry

        result = await _invoke_collect({"query": "test", "namespace": "demo"})

        assert result["statusCode"] == 502
        orch.resolve.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    async def test_indeterminate_existence_proceeds_when_table_unconfigured(self, mock_init, config):
        """The counterpart: no namespaces table (feature absent) → None means
        "cannot check", not an error, so the request must still proceed.
        """
        mock_init.return_value = None
        import coa_serve.main as main_mod

        orch = MagicMock()
        orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=3,
                    confidence=ConfidenceScore(score=0.5, rationale="ok"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={},
                ),
            )
        )
        main_mod._orchestrator = orch
        registry = MagicMock()
        registry.namespace_exists = AsyncMock(return_value=None)
        registry.namespaces_configured = False
        main_mod._sources_registry = registry

        result = await _invoke_collect({"query": "test", "namespace": "demo"})

        assert result.get("statusCode", 200) not in (404, 502)
        orch.resolve.assert_awaited_once()

    @patch("coa_serve.main._ensure_initialized")
    async def test_invoke_validation_error_does_not_leak_constraints(self, mock_init, config):
        mock_init.return_value = None
        import coa_serve.main as main_mod

        main_mod._orchestrator = MagicMock()

        result = await _invoke_collect({"query": "x", "namespace": "bad namespace!"})

        # A malformed namespace is rejected at the boundary (400) without leaking
        # the internal regex/constraint to the caller.
        assert result["statusCode"] == 400
        assert result["error"] == "ValidationError"
        assert "pattern" not in result["message"]
        assert "^[" not in result["message"]  # raw regex must not leak

    @patch("coa_serve.main.load_config")
    async def test_invoke_orchestrator_exception_returns_500(self, mock_load_config, config):
        mock_load_config.return_value = config
        import coa_serve.main as main_mod

        main_mod._config = config

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(side_effect=RuntimeError("downstream failure"))
        main_mod._orchestrator = mock_orch

        result = await _invoke_collect({"query": "test", "namespace": "demo"})

        assert result["statusCode"] == 500
        assert result["error"] == "InternalError"
        assert "downstream failure" not in result["message"]
        assert "requestId" in result

    @patch("coa_serve.main.load_config")
    async def test_invoke_access_denied_returns_403_no_leak(self, mock_load_config, config):
        # TDD-6: orchestrator raises AccessDeniedError -> 403, no reason leak.
        from coa_serve.exceptions import AccessDeniedError

        mock_load_config.return_value = config
        import coa_serve.main as main_mod

        main_mod._config = config

        mock_orch = MagicMock()
        mock_orch.resolve = AsyncMock(side_effect=AccessDeniedError("payroll table forbidden"))
        main_mod._orchestrator = mock_orch

        result = await _invoke_collect({"query": "show payroll", "namespace": "demo"})

        assert result["statusCode"] == 403
        assert result["error"] == "AccessDeniedError"
        assert "requestId" in result
        # No leak of the denial reason / table / SQL anywhere in the body.
        body = str(result)
        assert "payroll" not in body
        assert "forbidden" not in body
        assert "SELECT" not in body.upper()

    @patch("coa_serve.main.load_config")
    async def test_invoke_timeout_returns_504(self, mock_load_config, config):
        import asyncio

        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod.RESOLVE_TIMEOUT_S = 0.01

        async def slow_resolve(request, **kwargs):
            await asyncio.sleep(1)

        mock_orch = MagicMock()
        mock_orch.resolve = slow_resolve
        main_mod._orchestrator = mock_orch

        result = await _invoke_collect({"query": "test", "namespace": "demo"})

        assert result["statusCode"] == 504
        assert result["error"] == "TimeoutError"
        main_mod.RESOLVE_TIMEOUT_S = 30

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_preserves_identity_for_role_resolution(
        self, mock_resolve, mock_init, stub_orchestrator, config
    ):
        """Regression: upstream identity must survive the isolated-action
        strip so the full-query path can resolve roles from userId/groups."""

        mock_init.return_value = None
        mock_resolved = MagicMock()
        mock_resolved.inject_into = MagicMock()
        mock_resolve.return_value = mock_resolved

        await _invoke_collect(
            {
                "query": "how many orders",
                "namespace": "demo",
                "profile": {
                    "userId": "user@example.com",
                    "groups": ["team-a"],
                    "email": "user@example.com",
                },
            }
        )

        # email="" because there is no JWT on this path — profile.email is
        # attacker-controlled and must NOT be promoted to a grant lookup key.
        mock_resolve.assert_called_once_with("user@example.com", ["team-a"], namespace="demo", email="")
        mock_resolved.inject_into.assert_called_once()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_strips_injectable_roles_before_orchestrator(
        self, mock_resolve, mock_init, stub_orchestrator, config
    ):
        """Verify client-supplied globalRoles/resourceRoles are stripped and don't
        reach the orchestrator — even though identity fields are preserved."""

        mock_init.return_value = None
        mock_resolved = MagicMock()
        mock_resolved.inject_into = MagicMock()
        mock_resolve.return_value = mock_resolved

        await _invoke_collect(
            {
                "query": "test",
                "namespace": "demo",
                "profile": {
                    "userId": "attacker@example.com",
                    "groups": ["team-a"],
                    "globalRoles": ["platform-admin"],
                    "resourceRoles": [{"role": "namespace-owner", "resourceUID": "demo"}],
                    "tableAllowlist": ["*"],
                    "columnDenylist": {},
                },
            }
        )

        request_arg = stub_orchestrator.resolve.call_args[0][0]
        assert "globalRoles" not in request_arg.profile
        assert "resourceRoles" not in request_arg.profile
        assert "tableAllowlist" not in request_arg.profile
        assert "columnDenylist" not in request_arg.profile

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_skips_role_resolution_when_no_identity(
        self, mock_resolve, mock_init, stub_orchestrator, config
    ):
        """When profile has no userId/email, resolve_profile must NOT be called."""

        mock_init.return_value = None

        await _invoke_collect({"query": "test", "namespace": "demo", "profile": {}})

        mock_resolve.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_handles_groups_as_csv_string(self, mock_resolve, mock_init, stub_orchestrator, config):
        """The data-layer may send groups as a comma-separated string — the CM
        must split it before passing to resolve_profile."""

        mock_init.return_value = None
        mock_resolved = MagicMock()
        mock_resolved.inject_into = MagicMock()
        mock_resolve.return_value = mock_resolved

        await _invoke_collect(
            {
                "query": "test",
                "namespace": "demo",
                "profile": {
                    "userId": "user@example.com",
                    "groups": "team-a, team-b, team-c",
                },
            }
        )

        mock_resolve.assert_called_once_with(
            "user@example.com", ["team-a", "team-b", "team-c"], namespace="demo", email=""
        )

    @patch("coa_serve.main.extract_jwt_identity")
    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_threads_jwt_email_when_sub_differs(
        self, mock_resolve, mock_init, mock_jwt, stub_orchestrator, config
    ):
        """Cognito issues a UUID 'sub' while grants are keyed by email. The JWT
        email must be threaded to resolve_profile so the email-keyed grant is
        still found — otherwise Cedar fails closed on a legitimately granted user.
        """

        mock_init.return_value = None
        mock_resolved = MagicMock()
        mock_resolved.inject_into = MagicMock()
        mock_resolve.return_value = mock_resolved
        # (sub, email, groups) as returned from a validated Cognito JWT
        mock_jwt.return_value = ("9f8e7d6c-1234-4abc-9def-0123456789ab", "user@example.com", ["team-a"])

        await _invoke_collect({"query": "how many orders", "namespace": "demo"}, context=MagicMock())

        mock_resolve.assert_called_once_with(
            "9f8e7d6c-1234-4abc-9def-0123456789ab",
            ["team-a"],
            namespace="demo",
            email="user@example.com",
        )

    @patch("coa_serve.main.extract_jwt_identity")
    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_invoke_ignores_profile_email_when_jwt_present(
        self, mock_resolve, mock_init, mock_jwt, stub_orchestrator, config
    ):
        """The request body is attacker-controlled. A client-supplied profile.email
        must never become the grant lookup key when a validated JWT is present —
        otherwise a caller could impersonate another principal's email grants.
        """

        mock_init.return_value = None
        mock_resolved = MagicMock()
        mock_resolved.inject_into = MagicMock()
        mock_resolve.return_value = mock_resolved
        mock_jwt.return_value = ("sub-uuid", "real@example.com", [])

        await _invoke_collect(
            {
                "query": "test",
                "namespace": "demo",
                "profile": {"email": "admin@example.com", "userId": "admin@example.com"},
            },
            context=MagicMock(),
        )

        mock_resolve.assert_called_once_with("sub-uuid", [], namespace="demo", email="real@example.com")


@agentcore_required
@pytest.mark.asyncio
class TestStreamingSSEMode:
    """Tests for stream: true SSE mode in invoke()."""

    @pytest.fixture(autouse=True)
    def stub_orchestrator(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        mock_orch = MagicMock()

        async def mock_resolve(request, trace=None, on_token=None, conversation_history=None, deadline=None):
            """Simulate orchestrator resolve with trace callback."""
            if trace:
                trace.record(step="t2.sql", status="done", duration_ms=50)
            return InvokeResponse(
                result=QueryResult(
                    tier=2,
                    confidence=ConfidenceScore(score=0.85, rationale="high"),
                    trace=[TraceStep(step="t2.sql", status="done", durationMs=50)],
                    result_rows=[{"col": "val"}],
                ),
                requestId="req-1",
            )

        mock_orch.resolve = mock_resolve
        main_mod._orchestrator = mock_orch
        main_mod._session_manager = None
        main_mod._session_metadata = None
        yield mock_orch

    @patch("coa_serve.main._ensure_initialized")
    async def test_streaming_yields_multiple_events(self, mock_init):
        """stream: true should yield step and done events (at minimum)."""
        mock_init.return_value = None

        results = await _invoke_collect({"query": "show revenue", "namespace": "demo", "stream": True})

        # Should yield multiple items (list, not single dict)
        assert isinstance(results, list)
        # Last event should be a done event
        done_events = [e for e in results if isinstance(e, dict) and e.get("type") == "done"]
        assert len(done_events) == 1
        assert done_events[0]["payload"]["result"]["tier"] == 2

    @patch("coa_serve.main._ensure_initialized")
    async def test_streaming_done_has_request_id(self, mock_init):
        """done event should carry the requestId."""
        mock_init.return_value = None

        results = await _invoke_collect({"query": "test", "namespace": "demo", "stream": True, "requestId": "my-req"})

        assert isinstance(results, list)
        done_events = [e for e in results if isinstance(e, dict) and e.get("type") == "done"]
        assert len(done_events) == 1
        assert done_events[0]["requestId"] == "my-req"

    @patch("coa_serve.main._ensure_initialized")
    async def test_streaming_validation_error_yields_single_dict(self, mock_init):
        """Invalid payload in stream mode should yield a single error dict."""
        mock_init.return_value = None

        result = await _invoke_collect({"query": "", "namespace": "", "stream": True})

        # Single dict (not wrapped in list) per _invoke_collect behavior
        assert isinstance(result, dict)
        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400

    @patch("coa_serve.main._ensure_initialized")
    async def test_non_streaming_yields_single_result(self, mock_init):
        """stream: false (default) should yield a single result dict."""
        mock_init.return_value = None

        result = await _invoke_collect({"query": "show data", "namespace": "demo"})

        assert isinstance(result, dict)
        assert "requestId" in result
        assert result.get("result", {}).get("tier") == 2


@agentcore_required
@pytest.mark.asyncio
class TestResolveUserIdPriority:
    """Test the _resolve_user_id helper priority chain."""

    @pytest.fixture(autouse=True)
    def stub_env(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        main_mod._session_manager = MagicMock()  # needed for namespace branch
        yield

    @patch("coa_serve.main._ensure_initialized")
    async def test_jwt_sub_takes_precedence_over_profile_user_id(self, mock_init):
        """A validated JWT sub is authoritative and must OVERRIDE a client-supplied
        profile.userId — a caller must not be able to act as another user by putting a
        different userId in the (unauthenticated) profile. See resolve_user_id: JWT sub >
        JWT email > profile.userId."""
        import coa_serve.main as main_mod
        import jwt as pyjwt

        mock_init.return_value = None
        token = pyjwt.encode({"sub": "jwt-user", "email": "jwt@x.com"}, "s", algorithm="HS256")

        ctx = MagicMock()
        ctx.request_headers = {"authorization": f"Bearer {token}"}

        mock_metadata = MagicMock()
        mock_metadata.get_recent_for_namespace = AsyncMock(return_value=None)
        mock_metadata.get_recent = AsyncMock(return_value=None)
        main_mod._session_metadata = mock_metadata

        await _invoke_collect(
            {
                "action": "getRecentSession",
                "namespace": "ns1",
                "profile": {"userId": "profile-user"},
            },
            context=ctx,
        )

        # Session lookup must use the JWT sub (jwt-user), NOT the spoofable profile userId.
        mock_metadata.get_recent_for_namespace.assert_called_once_with("jwt-user", "ns1")

    @patch("coa_serve.main._ensure_initialized")
    async def test_jwt_used_when_profile_empty(self, mock_init):
        """When profile.userId is empty, fall back to JWT sub."""
        import coa_serve.main as main_mod
        import jwt as pyjwt

        mock_init.return_value = None
        token = pyjwt.encode({"sub": "jwt-fallback", "email": "e@x.com"}, "s", algorithm="HS256")

        ctx = MagicMock()
        ctx.request_headers = {"authorization": f"Bearer {token}"}

        mock_metadata = MagicMock()
        mock_metadata.get_recent_for_namespace = AsyncMock(return_value=None)
        mock_metadata.get_recent = AsyncMock(return_value=None)
        main_mod._session_metadata = mock_metadata

        await _invoke_collect(
            {
                "action": "getRecentSession",
                "namespace": "ns1",
                "profile": {},
            },
            context=ctx,
        )

        mock_metadata.get_recent_for_namespace.assert_called_once_with("jwt-fallback", "ns1")

    @patch("coa_serve.main._ensure_initialized")
    async def test_no_identity_returns_400(self, mock_init):
        """No profile.userId and no JWT should return validation error."""
        mock_init.return_value = None

        result = await _invoke_collect(
            {
                "action": "getRecentSession",
                "namespace": "ns1",
                "profile": {},
            }
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


@pytest.mark.unit
class TestHandleTranslate:
    """Test the translate-only handler (issue #861 regression guard)."""

    @pytest.mark.asyncio
    async def test_handle_translate_uses_module_level_nl_to_sparql(self):
        """Test that _handle_translate uses the module-level _nl_to_sparql instance."""
        from coa_serve.main import _handle_translate

        mock_nl_to_sparql = AsyncMock()
        mock_nl_to_sparql.translate.return_value = MagicMock(
            sparql="SELECT * WHERE { ?s ?p ?o }",
            confidence=0.95,
            trace_steps=[{"step": "test", "status": "success", "durationMs": 10}],
        )

        with patch("coa_serve.main._nl_to_sparql", mock_nl_to_sparql):
            result = await _handle_translate(
                {"query": "Show all entities", "namespace": "test-ns"},
                "req-123",
            )

            assert result["statusCode"] == 200
            assert result["sparqlQuery"] == "SELECT * WHERE { ?s ?p ?o }"
            assert result["confidence"]["score"] == 0.95
            mock_nl_to_sparql.translate.assert_called_once_with("Show all entities", "test-ns")

    @pytest.mark.asyncio
    async def test_handle_translate_reports_null_ontology_version_by_design(self):
        """Translation never touches the VKG and carries no in-band snapshot
        version, so the response reports null rather than a namespace-level
        approximation (#986)."""
        from coa_serve.main import _handle_translate

        mock_nl_to_sparql = AsyncMock()
        mock_nl_to_sparql.translate.return_value = MagicMock(sparql="SELECT 1", confidence=0.9, trace_steps=[])

        with patch("coa_serve.main._nl_to_sparql", mock_nl_to_sparql):
            result = await _handle_translate({"query": "q", "namespace": "test-ns"}, "req-986")

        assert result["statusCode"] == 200
        assert result["ontologyVersion"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [42, "x" * 4001])
    async def test_handle_translate_rejects_invalid_query_before_translation(self, query):
        from coa_serve.main import _handle_translate

        mock_nl_to_sparql = AsyncMock()
        with patch("coa_serve.main._nl_to_sparql", mock_nl_to_sparql):
            result = await _handle_translate({"query": query, "namespace": "test-ns"}, "req-invalid")

        assert result["statusCode"] == 400
        mock_nl_to_sparql.translate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_translate_none_raises_clean_error(self):
        """Test that uninitialized _nl_to_sparql returns error, not AttributeError."""
        from coa_serve.exceptions import QueryTranslationError
        from coa_serve.main import _handle_translate

        with (
            patch("coa_serve.main._nl_to_sparql", None),
            pytest.raises(QueryTranslationError, match="Translation failed"),
        ):
            await _handle_translate(
                {"query": "test query", "namespace": "test-ns"},
                "req-456",
            )


@pytest.mark.unit
class TestHandleKbSearch:
    """Test defensive query validation on the isolated KB search handler."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [42, "x" * 4001])
    async def test_handle_kb_search_rejects_invalid_query_before_embedding(self, query):
        from coa_serve.main import _handle_kb_search

        mock_orchestrator = MagicMock()
        mock_orchestrator._bedrock_client = AsyncMock()
        with patch("coa_serve.main._orchestrator", mock_orchestrator):
            result = await _handle_kb_search({"query": query, "namespace": "test-ns"}, "req-invalid")

        assert result["statusCode"] == 400
        mock_orchestrator._bedrock_client.embed.assert_not_called()


@pytest.mark.unit
class TestNamespaceAdmissionGate:
    """F-2 (CWE-862): invoke() gates every namespace-scoped surface before dispatch.

    Runs the REAL admission gate (restoring it over the module-level bypass) so a
    denied namespace short-circuits to 403 BEFORE any retrieval or orchestration,
    for both an isolated action (kbSearch) and the full query path.
    """

    @pytest.fixture(autouse=True)
    def _reset_and_restore_gate(self):
        import coa_serve.main as main_mod

        main_mod._config = None
        main_mod._orchestrator = None
        main_mod._sources_registry = None
        # Restore the real gate over the module-level bypass fixture.
        with patch("coa_serve.main._authorize_namespace_access", _REAL_ADMISSION_GATE):
            yield
        main_mod._config = None
        main_mod._orchestrator = None
        main_mod._sources_registry = None

    @staticmethod
    def _orchestrator(*, denied: bool):
        from coa_serve.tier2.sql_firewall import FirewallResult

        orch = MagicMock()
        orch.authorize_namespace.return_value = FirewallResult(
            denied=denied, authorized_sql="", reason="not permitted" if denied else None
        )
        orch.resolve = AsyncMock(
            return_value=InvokeResponse(
                result=QueryResult(
                    tier=3,
                    confidence=ConfidenceScore(score=0.5, rationale="ok"),
                    trace=[TraceStep(step="s", status="success", duration_ms=0)],
                    metadata={},
                ),
            )
        )
        # Isolated-action dependencies — must never be touched on a denied request.
        orch._knowledge_retriever = MagicMock()
        orch._knowledge_retriever._vector = AsyncMock()
        orch._bedrock_client = AsyncMock()
        return orch

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_kb_search_denied_returns_403_without_retrieval(self, mock_resolve, mock_init):
        import coa_serve.main as main_mod
        from coa_serve.role_resolver import ResolvedProfile

        mock_init.return_value = None
        mock_resolve.return_value = ResolvedProfile(user_id="attacker")
        orch = self._orchestrator(denied=True)
        main_mod._orchestrator = orch

        result = await _invoke_collect({"action": "kbSearch", "query": "secret", "namespace": "victim"})

        assert result["statusCode"] == 403
        assert result["error"] == "AccessDeniedError"
        orch.authorize_namespace.assert_called_once()
        # The retriever/embedder must never run for an unauthorized namespace.
        orch._bedrock_client.embed.assert_not_called()
        orch._knowledge_retriever._vector.search.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_query_denied_returns_403_without_orchestration(self, mock_resolve, mock_init):
        import coa_serve.main as main_mod
        from coa_serve.role_resolver import ResolvedProfile

        mock_init.return_value = None
        mock_resolve.return_value = ResolvedProfile(user_id="attacker")
        orch = self._orchestrator(denied=True)
        main_mod._orchestrator = orch

        result = await _invoke_collect({"query": "secret", "namespace": "victim", "options": {"tierOverride": 3}})

        assert result["statusCode"] == 403
        orch.resolve.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_translate_denied_returns_403_without_translation(self, mock_resolve, mock_init):
        """`translate` is one of the three isolated actions that were ungated (F-2)."""
        import coa_serve.main as main_mod
        from coa_serve.role_resolver import ResolvedProfile

        mock_init.return_value = None
        mock_resolve.return_value = ResolvedProfile(user_id="attacker")
        orch = self._orchestrator(denied=True)
        main_mod._orchestrator = orch
        nl = AsyncMock()

        with patch("coa_serve.main._nl_to_sparql", nl):
            result = await _invoke_collect({"action": "translate", "query": "secret", "namespace": "victim"})

        assert result["statusCode"] == 403
        assert result["error"] == "AccessDeniedError"
        orch.authorize_namespace.assert_called_once()
        # Translation must not run for an unauthorized namespace.
        nl.translate.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_graph_traverse_denied_returns_403_without_traversal(self, mock_resolve, mock_init):
        """`graphTraverse` is one of the three isolated actions that were ungated (F-2)."""
        import coa_serve.main as main_mod
        from coa_serve.role_resolver import ResolvedProfile

        mock_init.return_value = None
        mock_resolve.return_value = ResolvedProfile(user_id="attacker")
        orch = self._orchestrator(denied=True)
        graph = AsyncMock()
        orch._knowledge_retriever._graph = graph
        main_mod._orchestrator = orch

        result = await _invoke_collect(
            {
                "action": "graphTraverse",
                "namespace": "victim",
                "options": {"startUri": "https://example.com/ontology#Claim"},
            }
        )

        assert result["statusCode"] == 403
        assert result["error"] == "AccessDeniedError"
        orch.authorize_namespace.assert_called_once()
        # Graph traversal must not run for an unauthorized namespace.
        graph.traverse_from_uris.assert_not_called()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_grant_lookup_failure_fails_closed_as_retryable(self, mock_resolve, mock_init):
        """A DynamoDB fault must reject the request, but as a retryable 502 — not a
        403, which would make a transient outage look like mass access revocation.
        """
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_resolve.side_effect = RuntimeError("dynamodb throttled")
        orch = self._orchestrator(denied=False)
        main_mod._orchestrator = orch

        result = await _invoke_collect({"query": "hello", "namespace": "mine"})

        assert result["statusCode"] == 502
        # Rejected before any authorization decision or orchestration.
        orch.authorize_namespace.assert_not_called()
        orch.resolve.assert_not_called()
        # The internal cause must not reach the caller.
        assert "dynamodb" not in str(result).lower()

    @patch("coa_serve.main._ensure_initialized")
    @patch("coa_serve.main.resolve_profile")
    async def test_allowed_namespace_reaches_orchestrator(self, mock_resolve, mock_init):
        import coa_serve.main as main_mod
        from coa_serve.role_resolver import ResolvedProfile

        mock_init.return_value = None
        mock_resolve.return_value = ResolvedProfile(user_id="owner")
        orch = self._orchestrator(denied=False)
        main_mod._orchestrator = orch

        result = await _invoke_collect({"query": "hello", "namespace": "mine"})

        orch.authorize_namespace.assert_called_once()
        orch.resolve.assert_awaited_once()
        assert result.get("statusCode", 200) != 403


@pytest.mark.unit
class TestResolvePrincipal:
    """resolve_principal centralizes the JWT-authoritative (user_id, groups) rule."""

    def test_jwt_sub_and_groups_win_over_body(self):
        from coa_serve.identity import resolve_principal

        uid, groups = resolve_principal(
            {"userId": "body-attacker", "groups": ["admin"]},
            "jwt-sub",
            "a@x.com",
            ["viewer"],
        )
        assert uid == "jwt-sub"
        assert groups == ["viewer"]

    def test_authenticated_caller_with_no_groups_does_not_fall_back_to_body(self):
        """A JWT with no groups is authenticated-with-no-groups, not "use the body"."""
        from coa_serve.identity import resolve_principal

        uid, groups = resolve_principal({"groups": ["admin"]}, "jwt-sub", "", [])
        assert uid == "jwt-sub"
        assert groups == []

    def test_profile_fallback_only_without_jwt(self):
        from coa_serve.identity import resolve_principal

        uid, groups = resolve_principal({"userId": "u1", "groups": ["g1"]}, "", "", [])
        assert uid == "u1"
        assert groups == ["g1"]

    def test_comma_joined_group_string_is_normalized(self):
        from coa_serve.identity import resolve_principal

        _, groups = resolve_principal({"groups": "g1, g2 ,, g3"}, "", "", [])
        assert groups == ["g1", "g2", "g3"]

    @pytest.mark.parametrize("bad", [42, {"a": 1}, 3.5, True])
    def test_malformed_groups_type_is_discarded(self, bad):
        """`profile` is attacker-controlled on the direct path — a non-list/str
        `groups` must not escape and violate the list[str] contract.
        """
        from coa_serve.identity import resolve_principal

        _, groups = resolve_principal({"groups": bad}, "", "", [])
        assert groups == []

    def test_non_string_group_members_are_dropped(self):
        from coa_serve.identity import resolve_principal

        _, groups = resolve_principal({"groups": ["ok", 7, None, "  ", "also-ok"]}, "", "", [])
        assert groups == ["ok", "also-ok"]
