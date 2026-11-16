# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Suite-wide setup and the shared fakes: the caller, its token provider, and a client on them."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from coa_workflow_agents.auth.caller import Caller
from coa_workflow_agents.clients.coa_api_client import CoaApiClient

# LOG_LEVEL is a common shell variable. server.py reads it at import (an
# invalid value fails there), and the config tests assert the default, so a
# developer's own value must not leak into the suite. Tests that need a level
# set it with monkeypatch.
for _name in ("LOG_LEVEL",):
    os.environ.pop(_name, None)

# A token expiry no test outlives (year 2100).
FAR_EXPIRY = 4102444800.0
CALLER_TOKEN = "caller.jwt"
BASE_URL = "https://coa.example.com/prod/"


class FakeTokenProvider:
    """Returns a fixed token and counts how often it was asked."""

    def __init__(self, token: str = CALLER_TOKEN) -> None:
        self.token = token
        self.calls = 0

    def get_id_token(self) -> str:
        self.calls += 1
        return self.token


@pytest.fixture
def make_caller() -> Callable[..., Caller]:
    """Build a verified caller; ``expires_at`` defaults to far in the future."""

    def build(*, expires_at: float = FAR_EXPIRY) -> Caller:
        return Caller(email="a@example.com", groups=("g1",), id_token=CALLER_TOKEN, expires_at=expires_at)

    return build


@pytest.fixture
def caller(make_caller: Callable[..., Caller]) -> Caller:
    """A caller whose token outlives every test."""
    return make_caller()


@pytest.fixture
def token_provider() -> FakeTokenProvider:
    return FakeTokenProvider()


@pytest.fixture
def coa_client(token_provider: FakeTokenProvider) -> Iterator[CoaApiClient]:
    """A client on ``token_provider``; patch its ``_session`` to fake the HTTP layer."""
    with CoaApiClient(base_url=BASE_URL, token_provider=token_provider) as client:
        yield client
