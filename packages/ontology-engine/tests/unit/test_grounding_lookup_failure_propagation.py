# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A failed lexical grounding lookup fails induction on both callers.

The lexical recall builds a bounded metadata index per grounding pool. When the
listing fails, or the pool exceeds a declared bound, the candidate set is
incomplete. Both induction callers used to catch the resulting error in their
fail-open handlers and continue without grounding, so a table or class whose
exact-name class dense recall had already found came out novel (for example
``customers`` became ``proposal:Customers`` with no link to the existing
``business:Customers``).

These tests drive the real ``GroundingService`` through the structured
strategy (``TableToOntologyStrategy.induce`` with a real ``InductionPipeline``)
and the unstructured stage (``ground_classes``). Only the catalog and embedding
clients are fakes. Each failure case has a healthy control proving the same
fixture grounds ``customers`` to ``business:Customers`` when the lookup works.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_ontology.inducer.services.data_catalog import CatalogTable
from coa_ontology.inducer.services.grounding import (
    _LEXICAL_INDEX_MAX_ENTITIES,
    GroundingLookupError,
    GroundingRecallLimitError,
    GroundingService,
)
from coa_ontology.inducer.services.pipeline import InductionPipeline
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.inducer.unstructured.services.class_grounding import (
    ClassGroundingInput,
    ground_classes,
)
from rdflib import URIRef
from rdflib.namespace import SKOS

pytestmark = pytest.mark.unit

_ONTOLOGY_ID = "business"
_CUSTOMERS = "http://example.org/business#Customers"
_PREFIX = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#"
_VECTOR = [0.5] * 8

# Dense recall returns the exact-name class. Without a lookup failure, STANDARD
# mode grounds ``customers`` to it with confidence 1.0.
_DENSE_HITS = [
    {"entity_uri": _CUSTOMERS, "ontology_id": _ONTOLOGY_ID, "score": 0.91, "text": "Customers people who buy"},
]
_LISTED = [{"entity_uri": _CUSTOMERS, "ontology_id": _ONTOLOGY_ID, "text": "Customers people who buy"}]


def _listing_fails(*_args, **_kwargs):
    raise ConnectionError("vector store unavailable")


def _listing_overflows(_ontology_id, *, limit, **_kwargs):
    # One row more than the remaining capacity: the index would be truncated.
    return [_LISTED[0]] * limit


def _listing_ok(*_args, **_kwargs):
    return list(_LISTED)


FAILURES = [
    pytest.param(_listing_fails, GroundingLookupError, "metadata unavailable", id="listing-fails"),
    pytest.param(
        _listing_overflows,
        GroundingRecallLimitError,
        f"exceeds {_LEXICAL_INDEX_MAX_ENTITIES} entities",
        id="entity-limit",
    ),
]


def _catalog(lister) -> MagicMock:
    catalog = MagicMock()
    catalog.search_embeddings.return_value = list(_DENSE_HITS)
    catalog.list_embeddings_for_ontology.side_effect = lister
    return catalog


# ── structured caller: TableToOntologyStrategy batch loop ───────────────────


def _induce_structured(lister):
    catalog = _catalog(lister)
    embeddings = MagicMock()
    embeddings.generate_lexical.side_effect = lambda *, classes, **_kw: (
        [{"vector": list(_VECTOR)} for _ in classes],
        "model-x",
    )
    pipeline = InductionPipeline(MagicMock(), embeddings, catalog)
    table = CatalogTable(
        id="t-customers",
        name="customers",
        fullyQualifiedName="public.customers",
        datasourceId="ds-1",
        sourceSchema="public",
        columns=[],
    )
    return TableToOntologyStrategy().induce(
        tables=[table],
        ontology_uri_prefix=_PREFIX,
        config={},
        pipeline=pipeline,
        grounding_ontology_ids=[_ONTOLOGY_ID],
        grounding_mode="STANDARD",
    )


def test_structured_control_grounds_customers_when_lookup_works():
    _graph, novel, matches, _ = _induce_structured(_listing_ok)

    table_matches = [m for m in matches if not m.source_column]
    assert [m.matched_class_uri for m in table_matches] == [_CUSTOMERS]
    assert "customers" not in novel


@pytest.mark.parametrize(("lister", "error", "message"), FAILURES)
def test_structured_lookup_failure_fails_induction(lister, error, message):
    with pytest.raises(error, match=message):
        _induce_structured(lister)


# ── unstructured caller: ground_classes per-class loop ─────────────────────


def _ground_unstructured(lister):
    service = GroundingService(
        ontology_catalog=_catalog(lister),
        embedding_generator=MagicMock(),
        llm_region="us-east-1",
        llm_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        namespace="ns",
    )
    inputs = [
        ClassGroundingInput(
            class_iri=f"{_PREFIX}Customers",
            label="Customers",
            description="People who buy.",
            vector=list(_VECTOR),
        )
    ]
    return ground_classes(
        inputs,
        grounding_service=service,
        ontology_ids=[_ONTOLOGY_ID],
        model_id="model-x",
        grounding_mode="STANDARD",
        uri_prefix=_PREFIX,
    )


def test_unstructured_control_grounds_customers_when_lookup_works():
    graph, grounded, matches = _ground_unstructured(_listing_ok)

    assert grounded == 1
    assert (URIRef(f"{_PREFIX}Customers"), SKOS.exactMatch, URIRef(_CUSTOMERS)) in graph
    assert [m.matched_class_uri for m in matches] == [_CUSTOMERS]


@pytest.mark.parametrize(("lister", "error", "message"), FAILURES)
def test_unstructured_lookup_failure_fails_induction(lister, error, message):
    with pytest.raises(error, match=message):
        _ground_unstructured(lister)
