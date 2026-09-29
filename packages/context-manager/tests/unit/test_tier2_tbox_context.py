# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 2 T-Box context builder."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.tbox_context import (
    MetricContext,
    TBoxContext,
    TBoxContextBuilder,
)
from coa_serve.tier2.ontop.types import VectorHit


def _make_graph_client(results=None):
    """Create a mock GraphClient that returns the given results."""
    client = AsyncMock()
    client.query.return_value = results or []
    return client


def _make_ontology_hit(uri: str, score: float = 0.85, entity_id: str = "e1") -> VectorHit:
    return VectorHit(
        type="ontology_class",
        score=score,
        entity_id=entity_id,
        uri=uri,
        metadata={"type": "ontology_class", "entity_id": entity_id, "uri": uri},
    )


def _make_metric_hit(name: str = "revenue", score: float = 0.9) -> VectorHit:
    return VectorHit(
        type="metric",
        score=score,
        entity_id=f"m-{name}",
        metadata={
            "type": "metric",
            "entity_id": f"m-{name}",
            "name": name,
            "description": f"Total {name}",
            "formula": f"SUM({name})",
            "dimensions": ["region", "quarter"],
        },
    )


@pytest.mark.unit
class TestTBoxContextBuilder:
    async def test_build_with_ontology_hits(self):
        graph_results = [
            {
                "class": "http://example.org/ontology#Order",
                "label": "Order",
                "parentClass": None,
                "property": "http://example.org/ontology#hasAmount",
                "propLabel": "hasAmount",
                "range": "xsd:decimal",
            },
            {
                "class": "http://example.org/ontology#Customer",
                "label": "Customer",
                "parentClass": None,
                "property": "http://example.org/ontology#hasName",
                "propLabel": "hasName",
                "range": "xsd:string",
            },
        ]
        client = _make_graph_client(graph_results)
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        hits = [
            _make_ontology_hit("http://example.org/ontology#Order"),
            _make_ontology_hit("http://example.org/ontology#Customer"),
        ]

        context = await builder.build(hits, "demo")

        assert len(context.classes) == 2
        assert len(context.properties) == 2
        assert context.classes[0]["uri"] == "http://example.org/ontology#Order"
        assert context.classes[0]["label"] == "Order"

    async def test_build_with_metric_hits(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        hits = [_make_metric_hit("revenue"), _make_metric_hit("orders")]

        context = await builder.build(hits, "demo", query="show revenue")

        assert len(context.metrics) == 2
        assert context.metrics[0].name == "revenue"
        assert context.metrics[0].formula == "SUM(revenue)"
        assert context.metrics[0].dimensions == ["region", "quarter"]

    async def test_build_with_no_hits_uses_entity_fallback(self):
        graph_results = [
            {
                "class": "http://example.org/ontology#Policy",
                "label": "Policy",
                "parentClass": None,
                "property": None,
                "propLabel": None,
                "range": None,
            }
        ]
        client = _make_graph_client(graph_results)
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = await builder.build([], "demo", query="How many policies exist?")

        assert len(context.classes) >= 1
        # named-graph resolution + count query + entity fallback, which is now TWO
        # queries (matched classes, then their datatype properties) so property-row
        # volume can no longer evict a matched class. OP skipped: 1 class; aiContext
        # skipped: no URIs. The isMapped bridge probe uses the separate .ask method,
        # so it does not add to .query's count. The leading resolution query is what
        # binds every later query to this namespace's graphs instead of filtering a
        # cluster-wide scan; it is cached per namespace, so it is once per build at
        # most, not once per query.
        assert client.query.call_count == 4

    async def test_build_empty_result_from_neptune(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        hits = [_make_ontology_hit("http://example.org/ontology#Missing")]
        context = await builder.build(hits, "demo")

        assert context.classes == []
        assert context.properties == []

    async def test_build_handles_neptune_error(self):
        client = AsyncMock()
        client.query.side_effect = RuntimeError("Connection failed")
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        hits = [_make_ontology_hit("http://example.org/ontology#Order")]
        context = await builder.build(hits, "demo")

        assert context.classes == []
        assert context.properties == []

    async def test_build_truncates_when_over_budget(self):
        # Generate enough results to exceed token budget
        graph_results = []
        for i in range(100):
            graph_results.append(
                {
                    "class": f"http://example.org/ontology#Class{i}",
                    "label": f"Class{i}",
                    "parentClass": None,
                    "property": f"http://example.org/ontology#prop{i}",
                    "propLabel": f"prop{i}",
                    "range": "xsd:string",
                }
            )
        client = _make_graph_client(graph_results)
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        hits = [_make_ontology_hit(f"http://example.org/ontology#Class{i}") for i in range(20)]
        context = await builder.build(hits, "demo", max_tokens=500)

        # Should be truncated
        assert context.token_estimate <= 500

    async def test_token_estimation(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        estimate = builder._estimate_tokens(
            [{"uri": "x", "label": "X"}] * 5,
            [{"uri": "y", "label": "Y", "domain": "x", "range": "z"}] * 10,
            [MetricContext(name="m1")] * 3,
        )
        # 5*20 + 10*30 + 3*40 = 100 + 300 + 120 = 520
        assert estimate == 520


@pytest.mark.unit
class TestTBoxContextFormatting:
    async def test_format_for_prompt_includes_classes(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = TBoxContext(
            classes=[
                {"uri": "http://example.org/ontology#Order", "label": "Order", "parent": None},
            ],
            properties=[
                {
                    "uri": "http://example.org/ontology#hasAmount",
                    "label": "hasAmount",
                    "domain": "http://example.org/ontology#Order",
                    "range": "xsd:decimal",
                },
            ],
            metrics=[
                MetricContext(
                    name="revenue",
                    description="Total revenue",
                    formula="SUM(amount)",
                    dimensions=["region"],
                ),
            ],
        )

        text = builder.format_for_prompt(context, "demo")

        assert "namespace: demo" in text
        assert "Order" in text
        assert "hasAmount" in text
        assert "revenue" in text
        assert "SUM(amount)" in text
        assert "region" in text
        assert "PREFIX ind: <http://example.org/ontology#>" in text

    async def test_format_for_prompt_prefix_from_slash_uri(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = TBoxContext(
            classes=[
                {"uri": "http://example.org/ontology/Order", "label": "Order", "parent": None},
            ],
            properties=[],
        )
        text = builder.format_for_prompt(context, "demo")
        assert "PREFIX ind: <http://example.org/ontology/>" in text

    async def test_format_for_prompt_empty_context(self):
        client = _make_graph_client([])
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = TBoxContext(classes=[], properties=[], metrics=[])
        text = builder.format_for_prompt(context, "test")

        assert "namespace: test" in text


@pytest.mark.unit
class TestTBoxTruncationPriority:
    """The token budget is spent on classes first.

    A class missing from the prompt cannot be queried at all; a class that
    arrives with only some of its column hints is still answerable. A single
    proportional ratio across all four lists got its value from whichever list
    dominates the estimate — the datatype properties, at ~10 per class — so
    property volume alone evicted mapped classes: on a 75-class namespace the
    class list reached the prompt with 60 entries, a fifth of the schema gone,
    dropped in Neptune's arbitrary row order.
    """

    @staticmethod
    def _builder():
        return TBoxContextBuilder(_make_graph_client([]), graph_uri_template="https://test.local/{namespace}")

    @staticmethod
    def _context(n_classes: int, props_per_class: int) -> TBoxContext:
        classes = [{"uri": f"https://ex.org/o#C{i}", "label": f"C{i}"} for i in range(n_classes)]
        properties = [
            {"uri": f"https://ex.org/o#C{i}_p{j}", "label": f"p{j}", "domain": f"https://ex.org/o#C{i}"}
            for i in range(n_classes)
            for j in range(props_per_class)
        ]
        return TBoxContext(classes=classes, properties=properties, metrics=[], token_estimate=10**9)

    def test_property_volume_does_not_evict_classes(self):
        """The BIRD-Full shape: 75 classes, ~10 properties each, 20k budget."""
        builder = self._builder()
        context = self._context(75, 10)

        result = builder._truncate(context, 20_000)

        assert len(result.classes) == 75
        assert len(result.properties) < len(context.properties)  # properties absorbed the cut
        assert result.token_estimate <= 20_000

    def test_classes_cut_only_when_they_alone_overrun(self):
        builder = self._builder()
        context = self._context(100, 1)

        # 20 tokens per class → a 600-token budget holds 30 classes and nothing else.
        result = builder._truncate(context, 600)

        assert len(result.classes) == 30
        assert result.properties == []

    def test_metrics_are_charged_before_classes(self):
        builder = self._builder()
        context = self._context(10, 0)
        context.metrics = [MetricContext(name="m1"), MetricContext(name="m2")]

        # 2 metrics * 40 = 80, leaving 120 of the 200 budget → 6 classes.
        result = builder._truncate(context, 200)

        assert len(result.metrics) == 2
        assert len(result.classes) == 6

    def test_surviving_properties_spread_across_classes(self):
        """A flat slice in Neptune's row order can hand one class forty columns
        and leave the next with none, which reads to the LLM as a class with no
        queryable columns. Every class must keep its first hint first."""
        builder = self._builder()
        context = self._context(10, 10)

        kept = builder._spread_properties(context.properties, 20)

        assert len(kept) == 20
        by_domain: dict[str, int] = {}
        for prop in kept:
            by_domain[prop["domain"]] = by_domain.get(prop["domain"], 0) + 1
        assert len(by_domain) == 10  # no class left with zero column hints
        assert set(by_domain.values()) == {2}

    def test_spread_regroups_by_class(self):
        """format_for_prompt renders properties per class, so interleaved rows
        would read as noise even though the set is correct."""
        builder = self._builder()
        context = self._context(3, 3)

        kept = builder._spread_properties(context.properties, 6)

        domains = [p["domain"] for p in kept]
        assert domains == sorted(domains, key=domains.index)  # each class contiguous
        assert len(set(domains)) == 3

    def test_spread_handles_uneven_class_property_counts(self):
        """A class with fewer properties than the round depth is skipped, not
        double-counted, and the leftover budget goes to classes that have more."""
        builder = self._builder()
        properties = [
            {"uri": "p1", "domain": "A"},
            {"uri": "p2", "domain": "A"},
            {"uri": "p3", "domain": "A"},
            {"uri": "p4", "domain": "B"},
        ]

        kept = builder._spread_properties(properties, 3)

        assert len(kept) == 3
        assert {p["domain"] for p in kept} == {"A", "B"}
        assert sum(1 for p in kept if p["domain"] == "B") == 1

    def test_spread_is_a_noop_when_everything_fits(self):
        builder = self._builder()
        properties = [{"uri": "p1", "domain": "A"}, {"uri": "p2", "domain": "B"}]

        assert builder._spread_properties(properties, 2) is properties
        assert builder._spread_properties(properties, 5) is properties
        assert builder._spread_properties(properties, 0) == []

    def test_budget_smaller_than_the_metrics_empties_the_hint_lists(self):
        """A budget the metrics alone overrun must not KEEP hints.

        Metrics are charged first and are never cut here, so a caller that sets
        max_tokens below their cost drives the remaining budget negative. The
        hint ratio is then negative, and ``object_properties[: int(n * ratio)]``
        is a NEGATIVE slice — it keeps all but the last few instead of none, so
        the one path that is supposed to shed the most context sheds the least
        and blows the budget it was handed. Both hint lists must come back empty.
        """
        builder = self._builder()
        context = TBoxContext(
            classes=[{"uri": f"https://ex.org/o#C{i}", "label": f"C{i}"} for i in range(5)],
            properties=[{"uri": f"https://ex.org/o#p{i}", "domain": "https://ex.org/o#C0"} for i in range(100)],
            metrics=[MetricContext(name=f"m{i}") for i in range(6)],
            token_estimate=10**9,
        )
        context.object_properties = [{"uri": f"https://ex.org/o#op{i}"} for i in range(100)]

        # 6 metrics * 40 = 240 against a 200 budget.
        result = builder._truncate(context, 200)

        assert result.properties == []
        assert result.object_properties == []
        # The metrics themselves still overrun — they are not cut here — so this
        # pins "nothing ELSE is added on top", not "the budget is met".
        assert result.token_estimate == 6 * 40 + len(result.classes) * 20
