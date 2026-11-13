# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""build_http_app and the entrypoint: the dev-only CORS middleware is added for exactly the configured origins."""

from __future__ import annotations

from typing import Any

import pytest
from coa_workflow_agents import __main__ as entrypoint
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.server import _CORS_HEADERS, build_http_app
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

_DEMO = "http://127.0.0.1:3000"
_REQUIRED: dict[str, Any] = {
    "api_endpoint": "https://coa.example.com",
    "jwt_issuer_url": "https://idp.example.com",
    "jwt_client_id": "web",
}


def _preflight(origin: str) -> dict[str, str]:
    client = TestClient(build_http_app((_DEMO,)))
    resp = client.options(
        "/mcp",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id,mcp-protocol-version",
        },
    )
    return {k.lower(): v for k, v in resp.headers.items()}


def test_no_cors_middleware_by_default():
    app = build_http_app(())
    assert not [m for m in app.user_middleware if m.cls is CORSMiddleware]


def test_cors_middleware_allows_only_the_configured_origins():
    app = build_http_app((_DEMO, "http://localhost:3000"))
    [cors] = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors.kwargs["allow_origins"] == [_DEMO, "http://localhost:3000"]
    assert cors.kwargs["allow_methods"] == ["GET", "POST", "DELETE", "OPTIONS"]
    assert cors.kwargs["expose_headers"] == ["Mcp-Session-Id"]
    assert cors.kwargs["allow_headers"] == list(_CORS_HEADERS)
    assert "Mcp-Protocol-Version" in cors.kwargs["allow_headers"]
    # No credentials (cookies) mode: the docs state it as a security property.
    assert cors.kwargs.get("allow_credentials", False) is False


def test_preflight_from_a_listed_origin_is_allowed():
    headers = _preflight(_DEMO)
    assert headers["access-control-allow-origin"] == _DEMO
    assert "authorization" in headers["access-control-allow-headers"].lower()
    assert "mcp-protocol-version" in headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in headers


def test_preflight_from_an_unlisted_origin_gets_no_allow_origin_header():
    assert "access-control-allow-origin" not in _preflight("https://evil.example.com")


def test_main_serves_the_cors_app_on_the_configured_host_and_port(monkeypatch):
    config = WorkflowAgentsConfig(
        allow_cors_origins=_DEMO, mcp_host="127.0.0.1", mcp_port=8123, log_level="DEBUG", **_REQUIRED
    )
    built: list[tuple[str, ...]] = []
    served: dict[str, Any] = {}
    returned: list[Starlette] = []

    def spy_build(cors_origins: tuple[str, ...]) -> Starlette:
        built.append(tuple(cors_origins))
        returned.append(build_http_app(cors_origins))
        return returned[-1]

    def fake_run(app: object, **kwargs: object) -> None:
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(entrypoint, "config", config)
    monkeypatch.setattr(entrypoint, "build_http_app", spy_build)
    monkeypatch.setattr(entrypoint, "setup_service_logging", lambda level: None)
    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)

    entrypoint.main()

    assert built == [(_DEMO,)]
    assert served.pop("app") is returned[0]
    # log_config=None: uvicorn's loggers propagate to the root handler and its content policy.
    assert served == {
        "host": "127.0.0.1",
        "port": 8123,
        "log_level": "debug",
        "log_config": None,
        "access_log": False,
    }


def test_a_configured_origin_can_initialize_through_the_served_app():
    """A real request through the session manager and FastMCP's transport security, not just the preflight.

    The module-level FastMCP session manager can run only once per process,
    so this is the suite's only test that enters the app's lifespan.
    """
    app = build_http_app((_DEMO,))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/mcp",
            headers={
                "Origin": _DEMO,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        # The rebinding check is in play: a loopback origin without a port is
        # refused by FastMCP's own Origin allowlist, not by CORS.
        refused = client.post(
            "/mcp",
            headers={
                "Origin": "http://127.0.0.1",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("mcp-session-id")
    assert resp.headers["access-control-allow-origin"] == _DEMO
    assert refused.status_code == 403, refused.text


def test_main_sets_up_logging_before_serving(monkeypatch):
    """The log policy is in place before uvicorn or any library logs a line."""
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "config", WorkflowAgentsConfig(**_REQUIRED))
    monkeypatch.setattr(entrypoint, "setup_service_logging", lambda level: calls.append("logging"))
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.append("serve"))
    entrypoint.main()
    assert calls == ["logging", "serve"]


def test_main_fails_at_startup_without_its_required_settings(monkeypatch):
    """A misconfigured server must not start and look healthy; the log names what is missing."""
    served: list[object] = []
    monkeypatch.setattr(entrypoint, "config", WorkflowAgentsConfig(api_endpoint="https://coa.example.com"))
    monkeypatch.setattr(entrypoint, "setup_service_logging", lambda level: None)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: served.append(app))
    with capture_logs() as logs, pytest.raises(RuntimeError, match="JWT_ISSUER_URL, JWT_CLIENT_ID"):
        entrypoint.main()
    assert served == []
    assert [(e["event"], e["missing"]) for e in logs] == [
        ("server_config_missing", ["JWT_ISSUER_URL", "JWT_CLIENT_ID"])
    ]
