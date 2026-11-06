# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A graph source's data client bounds what one query costs: a server-side timeout and a response byte cap."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from coa_common.sparql_config import parse_sparql_configuration
from coa_serve.clients import neptune
from coa_serve.clients.neptune import NeptuneGraphClient, SPARQLResponseTooLargeError
from coa_serve.clients.sparql_clients import MAX_RESPONSE_BYTES, build_sparql_client

pytestmark = pytest.mark.unit

_HOST = "db.cluster-abc.us-east-1.neptune.amazonaws.com"


def _data_client(**kwargs) -> NeptuneGraphClient:
    return build_sparql_client(parse_sparql_configuration({"dataEndpoint": _HOST}), **kwargs)


def _serving(client: NeptuneGraphClient, body: bytes) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def chunks():
        for at in range(0, len(body), 1024):
            yield body[at : at + 1024]

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/sparql-results+json"}, content=chunks())

    client._sign_request = AsyncMock(return_value={})
    client._transport = httpx.MockTransport(respond)
    return seen


class TestResponseByteCap:
    def test_a_graph_source_client_caps_responses_at_the_metric_graph_bound(self):
        assert MAX_RESPONSE_BYTES == 16 * 1024 * 1024
        assert _data_client()._max_body_bytes == MAX_RESPONSE_BYTES

    def test_the_shared_ontology_client_has_no_cap(self):
        assert NeptuneGraphClient(endpoint=_HOST)._max_body_bytes is None

    async def test_a_response_past_the_cap_is_refused_while_it_streams(self):
        client = _data_client()
        client._max_body_bytes = 4096
        _serving(client, b'{"results": {"bindings": [' + b" " * 8192 + b"]}}")
        with pytest.raises(SPARQLResponseTooLargeError):
            await client.query("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1")

    async def test_a_response_under_the_cap_is_read(self):
        client = _data_client()
        client._max_body_bytes = 4096
        _serving(client, b'{"results": {"bindings": [{"s": {"value": "http://ex.org/a"}}]}}')
        assert await client.query("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1") == [{"s": "http://ex.org/a"}]

    async def test_the_cap_counts_decoded_bytes_because_compression_is_never_asked_for(self):
        client = _data_client()
        seen = _serving(client, b'{"boolean": true}')
        await client.ask("ASK { ?s ?p ?o }")
        assert seen[0].headers["accept-encoding"] == "identity"


class TestServerTimeout:
    def test_the_server_gives_up_before_the_client_does(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_TIMEOUT", "8")
        assert neptune.server_timeout_ms() == 7000
        assert neptune.server_timeout_ms() < neptune.read_timeout_s() * 1000

    def test_a_short_client_timeout_still_leaves_the_server_a_bound(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_TIMEOUT", "1")
        assert 0 < neptune.server_timeout_ms() < 1000
