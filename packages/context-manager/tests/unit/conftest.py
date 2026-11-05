# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared test fixtures for serve unit tests."""

from __future__ import annotations

SEED_METRICS_FINANCE = [
    {
        "metric_id": "m-revenue",
        "name": "total_revenue",
        "display_name": "Total Revenue",
        "description": "Total revenue across all channels",
        "sql_template": "SELECT SUM(amount) AS total_revenue FROM orders",
        "dimensions": ["channel", "region"],
        "synonyms": ["revenue", "sales total"],
        "namespace": "finance",
        "data_source_id": "ds-orders",
        "columns": ["total_revenue"],
    },
    {
        "metric_id": "m-cost",
        "name": "total_cost",
        "display_name": "Total Cost",
        "description": "Total cost from all expense categories",
        "sql_template": "SELECT SUM(cost) FROM expenses",
        "dimensions": [],
        "synonyms": ["cost", "expenses"],
        "namespace": "finance",
        "data_source_id": "ds-expenses",
        "columns": ["total_cost"],
    },
]

SEED_METRICS_INSURANCE = [
    {
        "metric_id": "m-claims",
        "name": "claims_count",
        "display_name": "Claims Count",
        "description": "Number of insurance claims filed",
        "sql_template": "SELECT COUNT(*) AS claims_count FROM claims",
        "dimensions": ["status", "type"],
        "synonyms": ["number of claims", "claim count"],
        "namespace": "insurance",
        "data_source_id": "ds-claims",
        "columns": ["claims_count"],
    },
    {
        "metric_id": "m-premium",
        "name": "total_premium",
        "display_name": "Total Premium",
        "description": "Total premium collected",
        "sql_template": "SELECT SUM(premium) AS total_premium FROM policies",
        "dimensions": ["risk_tier"],
        "synonyms": ["premium amount", "premium total"],
        "namespace": "insurance",
        "data_source_id": "ds-policies",
        "columns": ["total_premium"],
    },
]

SEED_METRICS_ALL = SEED_METRICS_FINANCE + SEED_METRICS_INSURANCE


# ── Lexical-baseline shared fixtures (tasks 1.a–9) ──────────────────
#
# Thin pytest-fixture wrappers over ``tests/unit/lexical_helpers.py`` so the
# co-located lexical suites can inject one source of truth for the StrategySpec
# factory, NDB/NA endpoint constants, and the fake toolkit-node builder. The
# helpers remain importable as plain functions for inline use; these fixtures
# are provided for ergonomic injection. Importing the helpers does NOT import
# graphrag_toolkit (botocore sync-only invariant).

import pytest
from coa_common.sparql_config import GRAPH_SOURCES_ENV  # noqa: E402

from .lexical_helpers import (  # noqa: E402
    AOSS_ENDPOINT,
    AOSS_VECTOR_STORE_URI,
    NA_ENDPOINT,
    NA_GRAPH_ID,
    NA_GRAPH_STORE_URI,
    NDB_ENDPOINT,
    NDB_GRAPH_STORE_URI,
)
from .lexical_helpers import entity_contexts as _entity_contexts  # noqa: E402
from .lexical_helpers import make_strategy_spec as _make_strategy_spec  # noqa: E402
from .lexical_helpers import make_toolkit_node as _make_toolkit_node  # noqa: E402
from .lexical_helpers import source_metadata as _source_metadata  # noqa: E402


@pytest.fixture
def strategy_spec_factory():
    """Factory fixture returning ``StrategySpec`` instances (registry-backed)."""
    return _make_strategy_spec


@pytest.fixture
def toolkit_node_factory():
    """Factory fixture for fake toolkit ``NodeWithScore`` objects."""
    return _make_toolkit_node


@pytest.fixture
def source_metadata_factory():
    """Factory fixture for the nested toolkit ``Source`` metadata shape."""
    return _source_metadata


@pytest.fixture
def entity_contexts_factory():
    """Factory fixture for the nested toolkit ``EntityContexts`` metadata shape."""
    return _entity_contexts


@pytest.fixture
def ndb_endpoint():
    """A representative Neptune Database endpoint + its derived graph-store URI."""
    return {"endpoint": NDB_ENDPOINT, "graph_store_uri": NDB_GRAPH_STORE_URI}


@pytest.fixture
def na_endpoint():
    """A representative Neptune Analytics endpoint + its derived graph-store URI."""
    return {
        "endpoint": NA_ENDPOINT,
        "graph_id": NA_GRAPH_ID,
        "graph_store_uri": NA_GRAPH_STORE_URI,
    }


@pytest.fixture
def aoss_endpoint():
    """A representative AOSS endpoint + its derived vector-store URI."""
    return {"endpoint": AOSS_ENDPOINT, "vector_store_uri": AOSS_VECTOR_STORE_URI}


@pytest.fixture
def graph_sources_on(monkeypatch):
    """Deploy-time graph-sources switch on: serve behaves as it does with graph sources."""
    monkeypatch.setenv(GRAPH_SOURCES_ENV, "true")


@pytest.fixture
def graph_sources_off(monkeypatch):
    """Deploy-time graph-sources switch off (the default): serve behaves as main."""
    monkeypatch.delenv(GRAPH_SOURCES_ENV, raising=False)
