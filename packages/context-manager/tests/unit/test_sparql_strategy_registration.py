# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The graph-sources switch (GRAPH_SOURCES_ENABLED) ships the native SPARQL strategy dark."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve import main
from coa_serve.tier2.sparql.strategy import SparqlStrategy
from coa_serve.tier2.strategy import StrategyOption

pytestmark = pytest.mark.unit


_FIREWALL = MagicMock()


def _register(strategies):
    return main._register_sparql_strategy(
        strategies,
        graph_client=AsyncMock(),
        llm_client=AsyncMock(),
        guardrail_id="",
        guardrail_id_provider=lambda: "",
        sources_registry=MagicMock(),
        vector_client=MagicMock(),
        oss_ontology_index="ont",
        firewall=_FIREWALL,
    )


@pytest.mark.usefixtures("graph_sources_on")
def test_the_registered_strategy_gates_on_the_sql_paths_firewall():
    """One Cedar policy source: the graph path admits exactly the principals the SQL path admits."""
    strategy = _register([])
    assert strategy is not None and strategy._namespace_authorizer is _FIREWALL


def test_serve_passes_its_sql_firewall_to_the_sparql_strategy():
    import inspect

    source = inspect.getsource(main)
    call = source[source.index("sparql_strategy = _register_sparql_strategy(") :]
    assert "firewall=firewall," in call[: call.index("\n        )\n")]


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "anything"])
def test_flag_is_off_unless_explicitly_enabled(monkeypatch, value):
    monkeypatch.setenv("GRAPH_SOURCES_ENABLED", value)
    assert _register([]) is None


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " on ", "yes"])
def test_flag_accepts_the_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("GRAPH_SOURCES_ENABLED", value)
    assert isinstance(_register([]), SparqlStrategy)


@pytest.mark.usefixtures("graph_sources_off")
def test_the_retired_serve_sparql_strategy_flag_no_longer_registers(monkeypatch):
    """No alias: the old name was never on main, so nothing deployed depends on it."""
    monkeypatch.setenv("SERVE_SPARQL_STRATEGY", "true")
    assert _register([]) is None


@pytest.mark.usefixtures("graph_sources_off")
def test_flag_off_leaves_the_strategy_list_untouched():
    strategies = ["ontop", "nl_to_sql", "agentic"]
    assert _register(strategies) is None
    assert strategies == ["ontop", "nl_to_sql", "agentic"]


def _named(name):
    strategy = MagicMock()
    strategy.name = name
    return strategy


@pytest.mark.usefixtures("graph_sources_on")
def test_flag_on_inserts_the_sparql_strategy_before_ontop():
    """Ontop answers from SQL mappings with low accuracy; running it first left graph
    questions in a mixed namespace to a weak SQL answer."""
    ontop, nl_to_sql, agentic = _named("ontop"), _named("nl_to_sql"), _named("agentic")
    strategies = [ontop, nl_to_sql, agentic]
    registered = _register(strategies)
    assert isinstance(registered, SparqlStrategy)
    assert strategies == [registered, ontop, nl_to_sql, agentic]
    assert registered.name == StrategyOption.NL_TO_SPARQL


@pytest.mark.usefixtures("graph_sources_on")
def test_flag_on_without_ontop_appends_the_sparql_strategy():
    nl_to_sql = _named("nl_to_sql")
    strategies = [nl_to_sql]
    registered = _register(strategies)
    assert strategies == [nl_to_sql, registered]


async def test_shutdown_closes_the_sparql_clients():
    cache = MagicMock(aclose=AsyncMock())
    with patch.object(main, "_sparql_client_cache", cache):
        async with main._lifespan(main.app):
            cache.aclose.assert_not_awaited()
    cache.aclose.assert_awaited_once()


async def test_shutdown_without_the_strategy_is_a_no_op():
    with patch.object(main, "_sparql_client_cache", None):
        async with main._lifespan(main.app):
            pass
