# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 2 (Ontop) puts only the metrics a caller's ``allowedMetrics`` grant names into the SPARQL prompt."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier1 import metric_resolver
from coa_serve.tier1.metric_resolver import MetricResolver
from coa_serve.tier2.ontop.nl_to_sparql import NLtoSPARQL
from coa_serve.tier2.ontop.strategy import OntopStrategy
from coa_serve.tier2.strategy import StrategyContext

pytestmark = pytest.mark.unit

NS = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
REVENUE_TEXT = "total_revenue Sum of net revenue per region"
MARGIN_TEXT = "gross_margin Gross margin after supplier rebates"


def _metric_hit(name: str, text: str) -> VectorHit:
    uri = f"urn:coa:{NS}:metric:{name}"
    return VectorHit(
        id=f"doc-{name}",
        text=text,
        score=0.9,
        metadata={"entity_uri": uri, "entity_type": "metric", "namespace": NS, "text": text},
    )


class _Graph:
    """A namespace with one mapped class."""

    async def query(self, sparql: str) -> list[dict[str, Any]]:
        if "COUNT(DISTINCT ?class)" in sparql:
            return [{"cnt": "1"}]
        if "SELECT DISTINCT ?class ?label" in sparql:
            return [{"class": "http://ex.org/sales#Customer", "label": "Customer"}]
        return []


SEED = [
    {"metric_id": "m-rev", "name": "total_revenue", "description": "Sum of net revenue", "namespace": NS},
    {"metric_id": "m-margin", "name": "gross_margin", "description": "Gross margin", "namespace": NS},
]


async def _prompts(
    profile: dict[str, Any],
    hits: list[VectorHit] | None = None,
    seed: list[dict] | None = None,
    resolver: MetricResolver | None = None,
) -> list[str]:
    hits = (
        hits
        if hits is not None
        else [_metric_hit("total_revenue", REVENUE_TEXT), _metric_hit("gross_margin", MARGIN_TEXT)]
    )
    resolver = resolver or MetricResolver(seed=SEED if seed is None else seed)
    graph = _Graph()
    llm = AsyncMock()
    llm.converse.return_value = ConverseResult(text="no query")
    vectors = AsyncMock()
    vectors.search.side_effect = lambda *_, entity_type=None, **__: [] if entity_type == "class" else hits
    translator = NLtoSPARQL(graph, llm, graph_uri_template="https://g.local/{namespace}", metric_resolver=resolver)
    strategy = OntopStrategy(translator, AsyncMock(), vector_client=vectors, oss_ontology_index="coa-ontology")
    await strategy.resolve("revenue by region", NS, StrategyContext(embedding=[0.1], profile=profile))
    prompts = [call.kwargs["prompt"] for call in llm.converse.call_args_list]
    assert prompts, "the translator never reached the LLM"
    return prompts


@pytest.mark.parametrize("grant", [["m-rev"], ["total_revenue"]], ids=["by-id", "by-name"])
async def test_a_restricted_caller_sees_only_the_metrics_its_grant_permits(grant: list[str]) -> None:
    prompts = await _prompts({"allowedMetrics": grant})
    assert all(REVENUE_TEXT in p for p in prompts)
    assert not any(MARGIN_TEXT in p for p in prompts)


async def test_an_empty_grant_permits_no_metric() -> None:
    prompts = await _prompts({"allowedMetrics": []})
    assert not any(REVENUE_TEXT in p or MARGIN_TEXT in p for p in prompts)


async def test_a_caller_without_a_metric_grant_sees_every_metric() -> None:
    prompts = await _prompts({})
    assert all(REVENUE_TEXT in p and MARGIN_TEXT in p for p in prompts)


async def test_a_hit_takes_an_id_only_from_a_definition_of_its_exact_name() -> None:
    """Names are case-sensitive; the resolver's name index is not."""
    seed = [{"metric_id": "m-low", "name": "revenue", "description": "Lower-case revenue", "namespace": NS}]
    prompts = await _prompts({"allowedMetrics": ["m-low"]}, [_metric_hit("Revenue", "Revenue Gross receipts")], seed)
    assert not any("Gross receipts" in p for p in prompts)


async def test_a_hit_named_in_its_metadata_is_checked_against_its_namespace() -> None:
    """The resolver's name index holds a list per name, so it can't stand in for one definition."""
    hit = _metric_hit("gross_margin", MARGIN_TEXT)
    hit.metadata["name"] = "gross_margin"
    hit.metadata["metric_id"] = "m-unknown"
    assert all("gross_margin" in p for p in await _prompts({"allowedMetrics": ["m-margin"]}, [hit]))
    assert not any("gross_margin" in p for p in await _prompts({"allowedMetrics": ["m-rev"]}, [hit]))


def _hit_named_without_a_known_id() -> VectorHit:
    hit = _metric_hit("gross_margin", MARGIN_TEXT)
    hit.metadata["name"] = "gross_margin"
    hit.metadata["metric_id"] = "m-unknown"
    return hit


async def test_a_hit_named_in_its_metadata_takes_its_namespaces_definition() -> None:
    seed = [
        {
            "metric_id": "m-a",
            "name": "gross_margin",
            "description": "Margin as this namespace defines it",
            "namespace": NS,
        }
    ]
    prompts = await _prompts({}, [_hit_named_without_a_known_id()], seed)
    assert all("Margin as this namespace defines it" in p for p in prompts)


async def test_a_hit_named_in_its_metadata_never_takes_another_namespaces_definition() -> None:
    seed = [
        {
            "metric_id": "m-b",
            "name": "gross_margin",
            "description": "Margin as another team defines it",
            "namespace": "other-ns",
        }
    ]
    prompts = await _prompts({}, [_hit_named_without_a_known_id()], seed)
    assert not any("Margin as another team defines it" in p for p in prompts)


async def test_a_hit_is_named_by_its_whole_metric_uri_suffix() -> None:
    hit = _metric_hit("legacy:total_revenue", "legacy:total_revenue Older revenue rollup")
    prompts = await _prompts({"allowedMetrics": ["total_revenue"]}, [hit])
    assert not any("Older revenue rollup" in p for p in prompts)


def _published(*names: str) -> list[dict[str, str]]:
    return [{"name": name, "description": f"{name} as published", "namespace": NS} for name in names]


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def _warm_resolver(neptune: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> tuple[MetricResolver, _Clock]:
    monkeypatch.delenv("GRAPH_URI_TEMPLATE", raising=False)
    monkeypatch.setattr(metric_resolver, "LOOKUP_TTL_S", 300.0)
    clock = _Clock()
    resolver = MetricResolver(neptune_client=neptune, clock=clock)
    await resolver.start()
    await resolver.stop()
    return resolver, clock


async def test_an_id_grant_reaches_a_metric_published_after_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2 alone never runs the Tier 1 lookup that re-reads the namespace, so the grant check reads it itself."""
    neptune = AsyncMock()
    neptune.query.return_value = _published("total_revenue")
    resolver, clock = await _warm_resolver(neptune, monkeypatch)
    neptune.query.return_value = _published("total_revenue", "gross_margin")
    clock.now = 301.0

    prompts = await _prompts(
        {"allowedMetrics": [f"{NS}:gross_margin"]}, [_metric_hit("gross_margin", MARGIN_TEXT)], resolver=resolver
    )

    assert all(MARGIN_TEXT in p for p in prompts)


async def test_an_id_grant_is_left_out_when_the_metric_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cache can't confirm the grant: the metric is left out rather than shown on old data."""
    neptune = AsyncMock()
    neptune.query.return_value = _published("total_revenue", "gross_margin")
    resolver, clock = await _warm_resolver(neptune, monkeypatch)
    neptune.query.side_effect = ConnectionError("neptune unreachable")
    clock.now = 301.0

    prompts = await _prompts(
        {"allowedMetrics": [f"{NS}:gross_margin"]}, [_metric_hit("gross_margin", MARGIN_TEXT)], resolver=resolver
    )

    assert not any(MARGIN_TEXT in p for p in prompts)


async def _hang(*_: Any, **__: Any) -> list[dict[str, str]]:
    await asyncio.sleep(30)
    return []


async def test_a_slow_metric_read_delays_the_prompt_once_not_once_per_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    neptune = AsyncMock()
    neptune.query.return_value = _published("total_revenue")
    resolver, clock = await _warm_resolver(neptune, monkeypatch)
    neptune.query.side_effect = _hang
    monkeypatch.setattr(metric_resolver, "_INLINE_READ_TIMEOUT_S", 0.3)
    clock.now = 301.0
    hits = [_metric_hit(f"metric_{i}", f"metric_{i} Published after the snapshot") for i in range(5)]

    started = time.monotonic()
    prompts = await _prompts({"allowedMetrics": [f"{NS}:metric_{i}" for i in range(5)]}, hits, resolver=resolver)

    assert time.monotonic() - started < 1.2
    assert not any("Published after the snapshot" in p for p in prompts)
