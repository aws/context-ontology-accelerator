# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Engine selection and lifecycle of per-source SPARQL data-endpoint clients."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from coa_common.sparql_config import SparqlConfigurationError, SparqlStore, parse_sparql_configuration
from coa_serve.clients.neptune import NeptuneGraphClient
from coa_serve.clients.sparql_clients import (
    SparqlClientCache,
    UnsupportedSparqlEngineError,
    build_sparql_client,
)

pytestmark = pytest.mark.unit

_HOST = "db.cluster-abc.us-east-1.neptune.amazonaws.com"


def _config(**overrides):
    return parse_sparql_configuration({"dataEndpoint": _HOST, **overrides})


class TestBuildSparqlClient:
    def test_neptune_store_builds_a_sigv4_neptune_client(self):
        client = build_sparql_client(_config(region="us-east-1", port=8182))
        assert isinstance(client, NeptuneGraphClient)
        assert client._base_url == f"https://{_HOST}:8182"
        assert client._region == "us-east-1"

    @pytest.mark.parametrize("overrides", [{"region": "eu-west-1"}, {"port": 8183}])
    def test_the_ssrf_confinement_still_holds_for_a_client(self, overrides):
        """The region confinement: a client is only ever built for a same-region endpoint on 8182."""
        with pytest.raises(SparqlConfigurationError):
            build_sparql_client(_config(**overrides))

    def test_region_defaults_to_the_given_runtime_region(self):
        assert build_sparql_client(_config(), default_region="ap-southeast-2")._region == "ap-southeast-2"

    def test_data_client_leaves_query_safety_to_the_firewall(self):
        assert build_sparql_client(_config())._enforce_read_only is False

    def test_an_engine_without_a_client_is_refused(self):
        config = _config().model_copy(update={"store": "stardog"})
        with pytest.raises(UnsupportedSparqlEngineError, match="stardog"):
            build_sparql_client(config)

    def test_every_store_has_a_client(self):
        """A store the API accepts but serve cannot reach would fail every query."""
        for store in SparqlStore:
            build_sparql_client(_config().model_copy(update={"store": store}))


def _fake_client():
    client = MagicMock()
    client.close = AsyncMock()
    return client


def _get(cache, namespace, source_id, config):
    with cache.lease(namespace, source_id, config) as client:
        return client


class TestSparqlClientCache:
    def test_one_client_per_source_and_configuration(self):
        factory = MagicMock(side_effect=lambda cfg: _fake_client())
        cache = SparqlClientCache(factory=factory)
        a1 = _get(cache, "ns", "src-a", _config())
        assert _get(cache, "ns", "src-a", _config()) is a1
        assert _get(cache, "ns", "src-b", _config()) is not a1
        assert _get(cache, "other-ns", "src-a", _config()) is not a1
        assert factory.call_count == 3

    async def test_an_edited_configuration_gets_a_new_client(self):
        cache = SparqlClientCache(factory=lambda cfg: _fake_client())
        before = _get(cache, "ns", "src-a", _config())
        after = _get(cache, "ns", "src-a", _config(dataNamedGraph="https://example.test/ns/graph2"))
        assert after is not before

    async def test_an_evicted_client_is_closed(self):
        cache = SparqlClientCache(max_size=1, factory=lambda cfg: _fake_client())
        first = _get(cache, "ns", "src-a", _config())
        _get(cache, "ns", "src-b", _config())
        await asyncio.sleep(0)
        first.close.assert_awaited_once()

    async def test_an_expired_client_is_closed_and_rebuilt(self):
        cache = SparqlClientCache(ttl_s=1e-9, factory=lambda cfg: _fake_client())
        first = _get(cache, "ns", "src-a", _config())
        await asyncio.sleep(0.001)
        second = _get(cache, "ns", "src-a", _config())
        await asyncio.sleep(0)
        assert second is not first
        first.close.assert_awaited_once()

    async def test_an_evicted_client_stays_open_until_its_last_lease_ends(self):
        cache = SparqlClientCache(max_size=1, factory=lambda cfg: _fake_client())
        with cache.lease("ns", "src-a", _config()) as first:
            with cache.lease("ns", "src-a", _config()) as again:
                assert again is first
                _get(cache, "ns", "src-b", _config())  # evicts src-a's client
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            first.close.assert_not_awaited()  # one lease still holds it
        await asyncio.sleep(0)
        first.close.assert_awaited_once()

    async def test_a_lease_ending_on_a_cached_client_does_not_close_it(self):
        cache = SparqlClientCache(factory=lambda cfg: _fake_client())
        client = _get(cache, "ns", "src-a", _config())
        await asyncio.sleep(0)
        client.close.assert_not_awaited()
        assert _get(cache, "ns", "src-a", _config()) is client

    def test_an_unknown_engine_is_refused_before_any_lease(self):
        cache = SparqlClientCache()
        with pytest.raises(UnsupportedSparqlEngineError):
            cache.lease("ns", "src-a", _config().model_copy(update={"store": "stardog"}))
        assert cache._leases == {}

    async def test_aclose_closes_every_client(self):
        cache = SparqlClientCache(factory=lambda cfg: _fake_client())
        clients = [_get(cache, "ns", f"src-{i}", _config()) for i in range(3)]
        await cache.aclose()
        for client in clients:
            client.close.assert_awaited_once()
        assert len(cache) == 0

    async def test_aclose_also_closes_a_dropped_client_still_leased(self):
        cache = SparqlClientCache(max_size=1, factory=lambda cfg: _fake_client())
        with cache.lease("ns", "src-a", _config()) as first:
            _get(cache, "ns", "src-b", _config())
            await cache.aclose()
            first.close.assert_awaited_once()

    async def test_a_failing_close_does_not_propagate(self):
        broken = _fake_client()
        broken.close.side_effect = RuntimeError("pool already gone")
        cache = SparqlClientCache(factory=lambda cfg: broken)
        _get(cache, "ns", "src-a", _config())
        await cache.aclose()  # no raise


def _timing_out(client) -> AsyncMock:
    http = AsyncMock()
    http.post.side_effect = httpx.ReadTimeout("read timed out")
    client._get_http = AsyncMock(return_value=http)
    client._sign_request = AsyncMock(return_value={})
    return http


@pytest.mark.unit
async def test_a_graph_source_query_that_times_out_is_not_retried():
    # The data client's request already spends the caller's deadline; a second attempt doubles it on a slow endpoint.
    config = parse_sparql_configuration({"dataEndpoint": "db.cluster-abc.us-east-1.neptune.amazonaws.com"})
    client = build_sparql_client(config)
    http = _timing_out(client)
    with pytest.raises(httpx.ReadTimeout):
        await client._post_query("SELECT * WHERE { ?s ?p ?o } LIMIT 1", accept=None)
    assert http.post.await_count == 1


@pytest.mark.unit
async def test_the_platform_client_still_retries_a_timeout_once():
    client = NeptuneGraphClient(endpoint="db.cluster-abc.us-east-1.neptune.amazonaws.com")
    http = _timing_out(client)
    with pytest.raises(Exception):  # noqa: B017, PT011  (the retry decorator wraps the last timeout)
        await client._post_query("SELECT * WHERE { ?s ?p ?o } LIMIT 1", accept=None)
    assert http.post.await_count == 2
