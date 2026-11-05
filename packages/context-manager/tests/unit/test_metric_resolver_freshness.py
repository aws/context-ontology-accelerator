# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A warm Tier 1 resolver sees new and updated metrics within the lookup TTL.

Before: a namespace whose on-miss lookup found no metrics was marked loaded for the
whole hourly refresh cycle, so a metric created a minute later kept missing Tier 1
on that instance, and a cached hit never re-read Neptune, so an updated expression
kept running the old SQL for up to an hour.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from coa_serve.tier1 import metric_resolver
from coa_serve.tier1.metric_resolver import MetricResolver

pytestmark = pytest.mark.unit

_QUESTION = "How many open claims do we have?"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _binding(expression: str, namespace: str = "insurance", name: str = "open_claims") -> dict:
    return {
        "name": name,
        "aiContext": json.dumps({"synonyms": [name.replace("_", " ")]}),
        "expressionDialects": json.dumps([{"dialect": "TRINO", "expression": expression}]),
        "g": f"https://ontology-workbench.local/{namespace}/urn%3Aont",
    }


@pytest.fixture(autouse=True)
def _default_graph_template(monkeypatch):
    monkeypatch.delenv("GRAPH_URI_TEMPLATE", raising=False)


async def _warm_resolver(neptune: AsyncMock, clock: _Clock) -> MetricResolver:
    resolver = MetricResolver(neptune_client=neptune, clock=clock)
    await resolver.start()
    return resolver


async def test_a_metric_created_after_an_empty_lookup_is_matched_after_the_ttl():
    neptune = AsyncMock()
    neptune.query.return_value = []
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    assert not (await resolver.match(_QUESTION, "insurance")).found

    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim WHERE claim_status_code = 'O'")]
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    result = await resolver.match(_QUESTION, "insurance")

    assert result.found
    assert result.metric_name == "open_claims"


async def test_an_updated_expression_reaches_a_warm_instance_after_the_ttl():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    assert "WHERE" not in (await resolver.match(_QUESTION, "insurance")).sql_template

    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim WHERE claim_status_code = 'O'")]
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    result = await resolver.match(_QUESTION, "insurance")

    assert "claim_status_code = 'O'" in result.sql_template


async def test_a_deleted_metric_stops_matching_after_the_ttl():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)

    neptune.query.return_value = []
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    assert not (await resolver.match(_QUESTION, "insurance")).found


async def test_neptune_is_read_at_most_once_per_namespace_per_ttl():
    neptune = AsyncMock()
    neptune.query.return_value = []
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    after_start = neptune.query.await_count

    await asyncio.gather(*(resolver.match(_QUESTION, "insurance") for _ in range(5)))
    clock.now += metric_resolver.LOOKUP_TTL_S / 2
    await resolver.match(_QUESTION, "insurance")
    assert neptune.query.await_count == after_start + 1

    clock.now += metric_resolver.LOOKUP_TTL_S
    await asyncio.gather(*(resolver.match(_QUESTION, "insurance") for _ in range(5)))
    assert neptune.query.await_count == after_start + 2


async def test_a_failed_lookup_keeps_serving_the_cache_and_waits_a_ttl_before_retrying():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)

    neptune.query.side_effect = RuntimeError("throttled")
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    assert (await resolver.match(_QUESTION, "insurance")).found
    calls = neptune.query.await_count
    assert (await resolver.match(_QUESTION, "insurance")).found
    assert neptune.query.await_count == calls

    neptune.query.side_effect = None
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim WHERE claim_status_code = 'O'")]
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    assert "claim_status_code" in (await resolver.match(_QUESTION, "insurance")).sql_template


async def test_a_namespace_reread_leaves_other_namespaces_and_shared_metrics_alone():
    neptune = AsyncMock()
    neptune.query.return_value = [
        _binding("SELECT COUNT(*) FROM claim"),
        _binding("SELECT SUM(amount) FROM sale", namespace="retail", name="total_sales"),
        {
            "name": "fx_rate",
            "namespace": "",
            "expressionDialects": json.dumps([{"dialect": "TRINO", "expression": "SELECT 1"}]),
        },
    ]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    neptune.query.return_value = []
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    assert not (await resolver.match(_QUESTION, "insurance")).found
    assert sorted(d.name for d in resolver._snapshot.by_id.values()) == ["fx_rate", "total_sales"]


async def test_a_reread_under_another_casing_keeps_the_cached_metrics():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim", namespace="Insurance")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    assert (await resolver.match(_QUESTION, "insurance")).found

    neptune.query.return_value = []
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    assert (await resolver.match(_QUESTION, "insurance")).found


async def test_an_unchanged_reread_does_not_rebuild_the_index(monkeypatch):
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    rebuilds = []
    original = resolver._build_indexes
    monkeypatch.setattr(resolver, "_build_indexes", lambda *a, **k: (rebuilds.append(1), original(*a, **k)))

    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    assert (await resolver.match(_QUESTION, "insurance")).found

    assert rebuilds == []


async def test_a_slow_read_does_not_hold_up_another_namespace():
    gate = asyncio.Event()

    async def query(sparql, **_kwargs):
        if "slow/" in sparql:
            await gate.wait()
        return []

    neptune = AsyncMock()
    neptune.query.side_effect = query
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    slow = asyncio.create_task(resolver.match(_QUESTION, "slow"))
    await asyncio.sleep(0)
    fast = await asyncio.wait_for(resolver.match(_QUESTION, "fast"), timeout=1)

    assert not fast.found
    gate.set()
    await slow


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 300.0),
        ("15", 15.0),
        ("1", 1.0),
        ("3600", 3600.0),
        ("3600.1", 300.0),
        ("0", 300.0),
        ("abc", 300.0),
        ("nan", 300.0),
    ],
)
def test_lookup_ttl_reads_its_env_var_within_bounds(monkeypatch, raw, expected):
    monkeypatch.setenv("SERVE_TIER1_LOOKUP_TTL_S", raw)
    assert metric_resolver.lookup_ttl_s() == expected


def _binding_with(namespace: str, name: str, synonyms: list) -> dict:
    binding = _binding("SELECT 1", namespace=namespace, name=name)
    binding["aiContext"] = json.dumps({"synonyms": synonyms})
    return binding


async def test_a_malformed_synonym_neither_fails_its_namespace_nor_blocks_others():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)

    neptune.query.return_value = [_binding_with("bad", "x_metric", [1, "x metric"])]
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    await resolver.match("x metric value", "bad")

    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim WHERE claim_status_code = 'O'")]
    result = await resolver.match(_QUESTION, "insurance")

    assert "claim_status_code" in result.sql_template


async def test_concurrent_requests_for_a_new_metric_share_one_read_and_all_match():
    neptune = AsyncMock()
    neptune.query.return_value = []
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    after_start = neptune.query.await_count

    async def slow_read(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return [_binding("SELECT COUNT(*) FROM claim")]

    neptune.query.side_effect = slow_read
    results = await asyncio.gather(*(resolver.match(_QUESTION, "insurance") for _ in range(5)))

    assert [r.found for r in results] == [True] * 5
    assert neptune.query.await_count == after_start + 1


async def test_a_mis_cased_request_does_not_hold_back_the_namespaces_refresh():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT OLD")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)

    async def scoped(sparql, **_kwargs):
        return [_binding("SELECT NEW")] if "/insurance/" in sparql else []

    neptune.query.side_effect = scoped
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    await resolver.match(_QUESTION, "INSURANCE")

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT NEW"


async def test_a_slow_read_serves_the_cached_metric_and_refreshes_in_the_background(monkeypatch):
    monkeypatch.setattr(metric_resolver, "_INLINE_READ_TIMEOUT_S", 0.01)
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT OLD")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    released = asyncio.Event()

    async def slow_read(*_args, **_kwargs):
        await released.wait()
        return [_binding("SELECT NEW")]

    neptune.query.side_effect = slow_read
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT OLD"
    released.set()
    await asyncio.sleep(0.05)
    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT NEW"


async def test_a_stale_miss_waits_for_the_namespace_read_only_once(monkeypatch):
    monkeypatch.setattr(metric_resolver, "_INLINE_READ_TIMEOUT_S", 0.01)
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT COUNT(*) FROM claim")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    never = asyncio.Event()

    async def hanging_read(*_args, **_kwargs):
        await never.wait()
        return []

    neptune.query.side_effect = hanging_read
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    waits = []
    original = resolver._lazy_namespace_lookup

    async def counted(namespace):
        waits.append(namespace)
        return await original(namespace)

    monkeypatch.setattr(resolver, "_lazy_namespace_lookup", counted)

    assert not (await resolver.match("what is the weather", "insurance")).found
    assert waits == ["insurance"]
    never.set()


async def test_a_namespace_read_issued_before_a_full_refresh_does_not_overwrite_it(monkeypatch):
    monkeypatch.setattr(metric_resolver, "_INLINE_READ_TIMEOUT_S", 0.01)
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT V1")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    released = asyncio.Event()

    async def query(sparql, **_kwargs):
        if "/insurance/" in sparql:
            await released.wait()
            return [_binding("SELECT V1")]
        return [_binding("SELECT V2")]

    neptune.query.side_effect = query
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    await resolver.match(_QUESTION, "insurance")
    clock.now += 1
    await resolver._refresh_from_neptune()
    released.set()
    await asyncio.gather(*resolver._lazy_reads.values())

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V2"


async def test_a_malformed_synonym_in_a_full_refresh_does_not_freeze_the_cache():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT V1")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)

    neptune.query.return_value = [_binding("SELECT V2"), _binding_with("insurance", "x_metric", [1, "x metric"])]
    await resolver._refresh_from_neptune()

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V2"


def test_a_namespace_read_names_its_metric_graph_instead_of_filtering_every_graph():
    sparql = metric_resolver._metric_list_sparql("https://graphs.example/{namespace}", namespace="insurance")

    graph = "https://graphs.example/insurance/urn%3Acoa%3Avocab%23GovernedMetrics"
    assert "STRSTARTS" not in sparql
    assert f"GRAPH <{graph}>" in sparql
    assert f"BIND(<{graph}> AS ?g)" in sparql


def test_a_full_refresh_still_filters_by_the_graph_prefix():
    sparql = metric_resolver._metric_list_sparql("https://graphs.example/{namespace}")

    assert 'FILTER(STRSTARTS(STR(?g), "https://graphs.example/"))' in sparql


def test_a_template_with_text_after_the_namespace_keeps_the_prefix_filter():
    sparql = metric_resolver._metric_list_sparql("https://graphs.example/{namespace}/v2", namespace="insurance")

    assert 'FILTER(STRSTARTS(STR(?g), "https://graphs.example/insurance/"))' in sparql
    assert "BIND(" not in sparql


async def test_a_namespace_that_is_not_safe_in_an_iri_is_not_read():
    neptune = AsyncMock()
    neptune.query.return_value = []
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    after_start = neptune.query.await_count

    assert not (await resolver.match(_QUESTION, "bad> namespace")).found
    assert neptune.query.await_count == after_start


async def test_namespace_reads_across_namespaces_are_capped(monkeypatch):
    monkeypatch.setenv("SERVE_TIER1_MAX_CONCURRENT_READS", "2")
    in_flight = 0
    peak = 0

    async def query(sparql, **_kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return []

    neptune = AsyncMock()
    neptune.query.side_effect = query
    resolver = await _warm_resolver(neptune, _Clock())
    peak = 0

    await asyncio.gather(*(resolver.match(_QUESTION, f"ns{i}") for i in range(6)))

    assert peak == 2
    assert neptune.query.await_count == 1 + 6


async def test_an_older_full_refresh_landing_late_keeps_a_newer_namespace_read():
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT V1")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    released = asyncio.Event()

    async def query(sparql, **_kwargs):
        if "STRSTARTS" in sparql:
            await released.wait()
            return [_binding("SELECT V1")]
        return [_binding("SELECT V2")]

    neptune.query.side_effect = query
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    refresh = asyncio.create_task(resolver._refresh_from_neptune())
    await asyncio.sleep(0)
    clock.now += 1
    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V2"
    released.set()
    await refresh

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V2"


async def test_a_failed_index_rebuild_keeps_serving_the_previous_metrics(monkeypatch):
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT V1")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    original = resolver._build_indexes

    def failing(items, *, replace=True):
        if not replace:
            raise KeyError("metric_id")
        return original(items, replace=replace)

    monkeypatch.setattr(resolver, "_build_indexes", failing)
    neptune.query.return_value = [_binding("SELECT V2")]
    clock.now += metric_resolver.LOOKUP_TTL_S + 1

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V1"
    monkeypatch.setattr(resolver, "_build_indexes", original)
    neptune.query.return_value = [_binding("SELECT SUM(amount) FROM sale", namespace="retail", name="total_sales")]
    assert (await resolver.match("total sales", "retail")).found

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V1"


async def test_a_successful_namespace_read_stops_retrying_a_failed_startup_refresh():
    full_scans = 0

    async def query(sparql, **_kwargs):
        nonlocal full_scans
        if "STRSTARTS" in sparql:
            full_scans += 1
            raise RuntimeError("store-wide scan keeps failing")
        return []

    neptune = AsyncMock()
    neptune.query.side_effect = query
    resolver = MetricResolver(neptune_client=neptune, clock=_Clock())
    await resolver.start()

    await resolver.match(_QUESTION, "insurance")
    scans_after_first_question = full_scans
    await resolver.match(_QUESTION, "insurance")
    await resolver.match(_QUESTION, "retail")

    assert full_scans == scans_after_first_question


async def test_a_refresh_landing_before_a_newer_read_that_then_fails_keeps_the_refresh_data(monkeypatch):
    monkeypatch.setattr(metric_resolver, "_INLINE_READ_TIMEOUT_S", 0.01)
    neptune = AsyncMock()
    neptune.query.return_value = [_binding("SELECT V1")]
    clock = _Clock()
    resolver = await _warm_resolver(neptune, clock)
    refresh_gate, read_gate = asyncio.Event(), asyncio.Event()

    async def query(sparql, **_kwargs):
        if "STRSTARTS" in sparql:
            await refresh_gate.wait()
            return [_binding("SELECT V3")]
        await read_gate.wait()
        raise RuntimeError("namespace read failed")

    neptune.query.side_effect = query
    clock.now += metric_resolver.LOOKUP_TTL_S + 1
    refresh = asyncio.create_task(resolver._refresh_from_neptune())
    await asyncio.sleep(0)
    clock.now += 1
    await resolver.match(_QUESTION, "insurance")
    refresh_gate.set()
    await refresh
    read_gate.set()
    await asyncio.gather(*resolver._lazy_reads.values())

    assert (await resolver.match(_QUESTION, "insurance")).sql_template == "SELECT V3"


async def test_an_api_valid_namespace_name_is_read():
    neptune = AsyncMock()
    neptune.query.return_value = []
    resolver = await _warm_resolver(neptune, _Clock())
    after_start = neptune.query.await_count

    await resolver.match(_QUESTION, "my-ns_1")

    assert neptune.query.await_count == after_start + 1


@pytest.mark.parametrize(("raw", "expected"), [("", 2), ("1", 1), ("16", 16), ("3.5", 2), ("0", 2), ("17", 2)])
def test_max_concurrent_reads_accepts_whole_numbers_in_range(monkeypatch, raw, expected):
    monkeypatch.setenv("SERVE_TIER1_MAX_CONCURRENT_READS", raw)
    assert metric_resolver.max_concurrent_reads() == expected
