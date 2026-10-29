# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A metric's display-only ``label`` (#1259) never reaches Tier-1 matching.

metric-service stores the label on ``vocab#displayLabel``, not ``rdfs:label``,
because the Tier-1 metric list reads every ``rdfs:label`` of a metric as a name.
This runs the real metric-list SPARQL over the triples metric-service writes for
a labelled metric and checks it is listed once, under its ``name``.
"""

from __future__ import annotations

import pytest
from coa_common.constants import URN_PREFIX
from coa_serve.tier1.metric_resolver import _metric_list_sparql
from rdflib import Dataset, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

pytestmark = pytest.mark.unit

_TEMPLATE = "https://ontology-workbench.local/{namespace}"
_VOCAB = f"urn:{URN_PREFIX}:vocab#"


def _labelled_metric_graph() -> Dataset:
    """The triples metric-service ``_build_triples`` writes for a metric with a label."""
    dataset = Dataset()
    graph = dataset.graph(URIRef("https://ontology-workbench.local/sales/governed-metrics"))
    metric = URIRef(f"urn:{URN_PREFIX}:sales:metric:revenue_total")
    graph.add((metric, RDF.type, OWL.Class))
    graph.add((metric, RDFS.subClassOf, URIRef(_VOCAB + "GovernedMetric")))
    graph.add((metric, RDFS.label, Literal("revenue_total")))
    graph.add((metric, RDFS.comment, Literal("Total revenue")))
    graph.add((metric, URIRef(_VOCAB + "displayLabel"), Literal("売上合計")))
    graph.add((metric, URIRef(_VOCAB + "dataSourceId"), Literal("ds-1")))
    return dataset


def test_labelled_metric_is_listed_once_under_its_name() -> None:
    rows = list(_labelled_metric_graph().query(_metric_list_sparql(_TEMPLATE)))
    assert [str(row.name) for row in rows] == ["revenue_total"]


def test_metric_list_query_does_not_read_display_label() -> None:
    assert "displayLabel" not in _metric_list_sparql(_TEMPLATE)
