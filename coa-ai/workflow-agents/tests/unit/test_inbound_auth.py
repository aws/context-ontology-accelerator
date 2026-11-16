# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inbound caller verification (``server.py``) with real signed tokens.

Tokens are signed with a real RSA key and checked by the real ``coa_common.auth``
authorizer (only its JWKS fetch is stubbed), so the access-token and audience rejections
run end to end. The accepted audience is the COA web app client.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import coa_workflow_agents.server as server
import jwt
import pytest
from coa_workflow_agents.agents.supervisor import SupervisorAgents
from coa_workflow_agents.auth.caller import Caller
from coa_workflow_agents.config import WorkflowAgentsConfig
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
FAR = 4102444800.0
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
WEB_CLIENT = "web-client"


def _caller(*, expires_at: float = FAR) -> Caller:
    return Caller(email="a@example.com", groups=("g1",), id_token="caller.jwt", expires_at=expires_at)


def _ok() -> MagicMock:
    ok = MagicMock(status_code=200, content=b"{}", text="{}", headers={})
    ok.json.return_value = {}
    return ok


def _config(**overrides: Any) -> WorkflowAgentsConfig:
    values: dict[str, Any] = {
        "api_endpoint": "https://coa.example.com",
        "jwt_issuer_url": ISSUER,
        "jwt_client_id": WEB_CLIENT,
        "aws_region": "us-east-1",
    }
    values.update(overrides)
    return WorkflowAgentsConfig(**values)


# ── the per-call client ─────────────────────────────────────────────────


def test_caller_client_sends_the_callers_own_token_to_coa():
    with patch.object(server, "config", _config()):
        client = server._caller_client(_caller())
    with patch.object(client._session, "request", return_value=_ok()) as req:
        client.list_sources(NS)
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer caller.jwt"
    assert req.call_args.args[1].startswith("https://coa.example.com/")


def test_caller_client_refuses_a_token_about_to_expire():
    with pytest.raises(ValueError, match="expires"):
        server._caller_client(_caller(expires_at=time.time() + 30))


# ── inbound: verification with real signed tokens ───────────────────────


@pytest.fixture(scope="module")
def signing_key() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def b64(n: int, length: int) -> str:
        return jwt.utils.base64url_encode(n.to_bytes(length, byteorder="big")).decode()

    jwk = {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": b64(numbers.n, 256), "e": b64(numbers.e, 3)}
    return key, {"keys": [jwk]}


def _token(key: rsa.RSAPrivateKey, **claims: Any) -> str:
    body: dict[str, Any] = {
        "iss": ISSUER,
        "aud": WEB_CLIENT,
        "token_use": "id",
        "email": "a@example.com",
        "cognito:groups": ["g1"],
        "sub": "sub-1",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})


def _ctx(auth_header: str | None) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.request.headers = {} if auth_header is None else {"authorization": auth_header}
    return ctx


@pytest.fixture()
def real_authorizer(signing_key):
    """server._verify_inbound_caller against the real coa_common authorizer, JWKS stubbed."""
    _, jwks = signing_key
    with patch.object(server, "config", _config()), patch.object(server, "_inbound_token_authorizer", None):
        authorizer = server._get_inbound_token_authorizer()
        with patch.object(authorizer, "_get_jwks", return_value=jwks):
            yield authorizer


def test_inbound_accepts_a_web_client_id_token_and_keeps_it_for_forwarding(signing_key, real_authorizer):
    key, _ = signing_key
    raw = _token(key)
    caller = server._verify_inbound_caller(_ctx(f"Bearer {raw}"))
    assert caller.email == "a@example.com"
    assert caller.groups == ("g1",)
    assert caller.id_token == raw
    assert raw not in repr(caller)


@pytest.mark.parametrize(
    ("claims", "match"),
    [
        pytest.param(
            {"token_use": "access", "aud": None, "client_id": WEB_CLIENT}, "only an ID token", id="access-token"
        ),
        pytest.param({"token_use": "access"}, "only an ID token", id="access-token-use-with-aud"),
        pytest.param({"aud": "mcp-client"}, "audience mismatch", id="mcp-client-audience"),
        pytest.param({"aud": None}, "audience mismatch", id="no-audience"),
        pytest.param({"iss": "https://evil.example.com"}, "Invalid issuer", id="wrong-issuer"),
        pytest.param({"exp": int(time.time()) - 3600}, "expired", id="expired"),
    ],
)
def test_inbound_rejects_access_tokens_wrong_audience_and_bad_tokens(signing_key, real_authorizer, claims, match):
    key, _ = signing_key
    with pytest.raises(ValueError, match=match):
        server._verify_inbound_caller(_ctx(f"Bearer {_token(key, **claims)}"))


def test_inbound_rejects_a_token_signed_by_another_key(real_authorizer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="Invalid token: Signature verification failed"):
        server._verify_inbound_caller(_ctx(f"Bearer {_token(other)}"))


def test_inbound_rejects_a_missing_header():
    with pytest.raises(ValueError, match="Missing Authorization header"):
        server._verify_inbound_caller(_ctx(None))


def test_inbound_handles_a_context_without_a_request():
    ctx = MagicMock()
    del ctx.request_context.request
    with pytest.raises(ValueError, match="Missing Authorization header"):
        server._verify_inbound_caller(ctx)


def test_inbound_rejects_a_token_without_expiry():
    authorizer = MagicMock()
    authorizer.validate.return_value.claims = {"aud": WEB_CLIENT, "token_use": "id"}
    with (
        patch.object(server, "_get_inbound_token_authorizer", return_value=authorizer),
        pytest.raises(ValueError, match="expiry"),
    ):
        server._verify_inbound_caller(_ctx("Bearer x"))


def test_inbound_authorizer_accepts_only_the_web_client_and_id_tokens():
    with (
        patch.object(server, "config", _config()),
        patch.object(server, "_inbound_token_authorizer", None),
        patch.object(server, "build_token_authorizer") as build,
    ):
        first = server._get_inbound_token_authorizer()
    build.assert_called_once_with(
        issuer=ISSUER, region="us-east-1", client_id=[WEB_CLIENT], group_claim_name="groups", require_id_token=True
    )
    assert first is build.return_value


@pytest.mark.parametrize("missing", ["jwt_client_id", "jwt_issuer_url", "api_endpoint"])
def test_inbound_authorizer_fails_closed_when_config_is_missing(missing):
    with (
        patch.object(server, "config", _config(**{missing: ""})),
        patch.object(server, "_inbound_token_authorizer", None),
        pytest.raises(RuntimeError, match="Missing required"),
    ):
        server._get_inbound_token_authorizer()


def test_inbound_authorizer_is_cached():
    sentinel = MagicMock()
    with patch.object(server, "_inbound_token_authorizer", sentinel):
        assert server._get_inbound_token_authorizer() is sentinel


def test_inbound_authorizer_is_built_once_when_first_calls_race():
    # The first thread builds while the second waits on the lock; the second
    # must find the built authorizer on its re-check, not build another.
    building = threading.Event()
    release = threading.Event()

    def slow_build(**kwargs: Any) -> MagicMock:
        building.set()
        assert release.wait(timeout=5)
        return MagicMock()

    results: list[Any] = []
    with (
        patch.object(server, "config", _config()),
        patch.object(server, "_inbound_token_authorizer", None),
        patch.object(server, "build_token_authorizer", side_effect=slow_build) as build,
    ):
        first = threading.Thread(target=lambda: results.append(server._get_inbound_token_authorizer()))
        first.start()
        assert building.wait(timeout=5)
        second = threading.Thread(target=lambda: results.append(server._get_inbound_token_authorizer()))
        second.start()
        time.sleep(0.05)  # let the second thread reach the lock
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
    assert build.call_count == 1
    assert len(results) == 2
    assert results[0] is results[1]


# ── run_workflow wiring ─────────────────────────────────────────────────


async def test_run_workflow_verifies_the_caller_off_the_event_loop():
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def verify(ctx: Any) -> Caller:
        seen.append(threading.current_thread())
        return _caller()

    with (
        patch.object(server, "_verify_inbound_caller", side_effect=verify),
        patch.object(server, "_run_supervisor_sync", return_value="done"),
    ):
        await server.run_workflow(MagicMock(), "list sources", NS)
    assert len(seen) == 1
    assert seen[0] is not loop_thread


async def test_run_workflow_builds_the_caller_client_for_the_verified_caller_and_closes_it():
    caller = _caller()
    per_call = MagicMock()
    built: list[Any] = []

    def fake_build(client, namespace_id, config, confirmed_tools, **kwargs):
        built.append((client, namespace_id, kwargs))
        supervisor = MagicMock(side_effect=lambda instr: "done")
        supervisor.event_loop_metrics.accumulated_usage = {}
        return SupervisorAgents(supervisor=supervisor, sub_agents=())

    with (
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        patch.object(server, "_caller_client", return_value=per_call) as make_client,
        patch.object(server, "build_supervisor_agent", side_effect=fake_build),
    ):
        result = await server.run_workflow(MagicMock(), "list sources", NS)

    assert result == f"Namespace: {NS}\n\ndone"
    make_client.assert_called_once_with(caller)
    assert built == [(per_call.__enter__.return_value, NS, {})]
    per_call.__exit__.assert_called_once()
