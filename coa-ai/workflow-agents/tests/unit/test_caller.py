# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Caller-token forwarding (``auth/caller.py``) through ``CoaApiClient``."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_workflow_agents.auth.caller import Caller, CallerTokenExpiredError, CallerTokenProvider, token_expired
from coa_workflow_agents.clients.coa_api_client import CoaApiClient
from coa_workflow_agents.tools.polling import poll_to_terminal

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
FAR = 4102444800.0


def _caller(*, expires_at: float = FAR) -> Caller:
    return Caller(email="a@example.com", groups=("g1",), id_token="caller.jwt", expires_at=expires_at)


def _ok() -> MagicMock:
    ok = MagicMock(status_code=200, content=b"{}", text="{}", headers={})
    ok.json.return_value = {}
    return ok


def test_caller_token_provider_forwards_the_token_while_valid():
    provider = CallerTokenProvider(_caller(expires_at=1000.0), _clock=lambda: 500.0)
    assert provider.get_id_token() == "caller.jwt"


@pytest.mark.parametrize("now", [941.0, 990.0, 2000.0])
def test_caller_token_provider_refuses_within_a_minute_of_expiry(now):
    provider = CallerTokenProvider(_caller(expires_at=1000.0), _clock=lambda: now)
    with pytest.raises(CallerTokenExpiredError, match="fresh token"):
        provider.get_id_token()


def test_token_expired_is_false_just_outside_the_margin():
    assert not token_expired(_caller(expires_at=1000.0), now=939.0)


def test_caller_repr_omits_the_token():
    assert "caller.jwt" not in repr(_caller())


def test_an_expired_token_inside_a_poll_is_final_not_retried():
    calls = {"n": 0}

    def fetch() -> dict[str, Any]:
        calls["n"] += 1
        raise CallerTokenExpiredError("expired")

    with pytest.raises(CallerTokenExpiredError):
        poll_to_terminal(
            "x", fetch, is_terminal=lambda s: True, is_failure=lambda s: False, timeout_s=5, operation="test_op"
        )
    assert calls["n"] == 1


def test_client_sends_the_callers_own_token_to_coa():
    client = CoaApiClient(base_url="https://coa.example.com/", token_provider=CallerTokenProvider(_caller()))
    with patch.object(client._session, "request", return_value=_ok()) as req:
        client.list_sources(NS)
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer caller.jwt"
    assert req.call_args.args[1].startswith(f"https://coa.example.com/namespaces/{NS}/sources")


def test_client_sends_nothing_once_the_token_expired():
    provider = CallerTokenProvider(_caller(expires_at=1000.0), _clock=lambda: 2000.0)
    client = CoaApiClient(base_url="https://coa.example.com", token_provider=provider)
    with patch.object(client._session, "request") as req, pytest.raises(CallerTokenExpiredError):
        client.list_sources(NS)
    req.assert_not_called()


@pytest.mark.parametrize("bad", ["..", ".", ""])
def test_client_refuses_dot_and_empty_path_segments_before_sending(bad):
    client = CoaApiClient(base_url="https://coa.example.com", token_provider=CallerTokenProvider(_caller()))
    with patch.object(client._session, "request") as req, pytest.raises(ValueError, match="path segment"):
        client.delete_source(NS, bad)
    req.assert_not_called()
