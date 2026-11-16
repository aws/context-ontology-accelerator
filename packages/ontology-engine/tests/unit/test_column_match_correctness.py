# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A propagated search failure from AOSS must NOT silently degrade to an
all-novel ontology.

Search failures must propagate from the column match, the grounding recall and
the strategy's outer handler; a successful-but-empty search stays novel.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.grounding import GroundingService
from coa_ontology.inducer.services.pipeline import InductionPipeline, SourceConcept
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import TransportError

pytestmark = pytest.mark.unit


def _table_concept() -> SourceConcept:
    return SourceConcept(
        table_fqn="db.orders",
        table_name="orders",
        column_name="",
        description="orders table",
        data_type="",
        is_table_level=True,
    )


def _column_concept(vector: list[float] | None = None) -> SourceConcept:
    return SourceConcept(
        table_fqn="db.orders",
        table_name="orders",
        column_name="total",
        description="order total",
        data_type="int",
        vector=vector if vector is not None else [0.1] * 8,
    )


def _benign_novel_table_match() -> ConceptMatch:
    """A table-level grounding result with no scoped ontology, so the column pass
    takes the grounding-union fallback and actually invokes search_embeddings."""
    return ConceptMatch(
        source_column="",
        source_table="orders",
        matched_class_uri=None,
        matched_ontology_id=None,
        similarity=None,
        match_type="novel",
    )


@pytest.mark.parametrize(
    "exc",
    [
        TransportError(429, "throttle"),
        OSConnectionError("N/A", "refused", Exception()),
    ],
    ids=["transport_429", "connection_error"],
)
def test_search_error_does_not_become_novel(exc):
    """When the property search raises (exhausted-retry / connection fault),
    match_concepts must RAISE — NOT swallow the error and classify the column
    'novel'. GroundingService is patched so the table pass yields no scoped
    ontology; the column pass then invokes the grounding-union search, which
    raises. grounding_ontology_ids is non-empty so search_embeddings is actually
    called (an empty pool would opt out and prove nothing)."""
    oc = MagicMock()
    oc.search_embeddings.side_effect = exc
    pipeline = InductionPipeline(None, None, oc)

    with patch("coa_ontology.inducer.services.pipeline.GroundingService") as mock_grounding_cls:
        mock_grounding_cls.return_value.to_concept_match.return_value = _benign_novel_table_match()
        with pytest.raises(type(exc)):
            pipeline.match_concepts(
                [_table_concept(), _column_concept()],
                confidence_threshold=0.8,
                model_id="titan",
                grounding_ontology_ids=["some-onto"],
            )


def test_empty_search_result_is_novel():
    """A SUCCESSFUL-but-empty search (returns []) must STILL classify the column 'novel'."""
    oc = MagicMock()
    oc.search_embeddings.return_value = []
    pipeline = InductionPipeline(None, None, oc)

    with patch("coa_ontology.inducer.services.pipeline.GroundingService") as mock_grounding_cls:
        mock_grounding_cls.return_value.to_concept_match.return_value = _benign_novel_table_match()
        matches = pipeline.match_concepts(
            [_table_concept(), _column_concept()],
            confidence_threshold=0.8,
            model_id="titan",
            grounding_ontology_ids=["some-onto"],
        )

    col_matches = [m for m in matches if m.source_column == "total"]
    assert len(col_matches) == 1
    assert col_matches[0].match_type == "novel"


@pytest.mark.parametrize(
    "exc",
    [
        TransportError(429, "throttle"),
        OSConnectionError("N/A", "refused", Exception()),
    ],
    ids=["transport_429", "connection_error"],
)
def test_recall_error_propagates(exc):
    """A per-ontology recall failure must RAISE, not be logged-and-continued
    into a partial (possibly empty) candidate set that misclassifies the subject
    'novel' with reason 'No candidates returned from embedding search'."""
    oc = MagicMock()
    oc.search_embeddings.side_effect = exc
    svc = GroundingService(ontology_catalog=oc, embedding_generator=MagicMock(), namespace="ns")

    with pytest.raises(type(exc)):
        svc.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["onto-1"],
        )


@pytest.mark.parametrize(
    "exc",
    [
        TransportError(429, "throttle"),
        OSConnectionError("N/A", "refused", Exception()),
        httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)),
    ],
    ids=["transport_429", "connection_error", "httpx_500"],
)
def test_strategy_reraises_transport_error_not_all_novel(exc):
    """When match_concepts raises a genuine search/transport error, the
    strategy's outer handler must RE-RAISE (so _run_induction marks the job
    FAILED) rather than fall back to an all-novel matches list."""
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    class _RaisingPipeline:
        def extract_concepts(self, tables):
            return InductionPipeline(None, None, None).extract_concepts(tables)

        def embed_concepts(self, concepts, backend=None):
            return concepts, "model-x"

        def match_concepts(self, *a, **k):
            raise exc

    from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable

    table = CatalogTable(
        id="test.orders",
        name="orders",
        fullyQualifiedName="test_db.public.orders",
        description="orders",
        columns=[CatalogColumn(name="order_id", dataType="INT", constraint="PRIMARY_KEY")],
    )

    with pytest.raises(type(exc)):
        TableToOntologyStrategy().induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/induced#",
            config={},
            pipeline=_RaisingPipeline(),
            grounding_ontology_ids=["urn:ontology:foundational"],
            grounding_mode="ENHANCED",
        )


def test_strategy_still_falls_back_on_non_transport_error():
    """The handler must preserve the distinction: a NON-transport programming error (e.g.
    RuntimeError) still falls back to all-novel — only genuine search/transport
    failures fail the job loud. Locks in the narrow re-raise vs full removal."""
    from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    class _BuggyPipeline:
        def extract_concepts(self, tables):
            return InductionPipeline(None, None, None).extract_concepts(tables)

        def embed_concepts(self, concepts, backend=None):
            return concepts, "model-x"

        def match_concepts(self, *a, **k):
            raise RuntimeError("programming bug, not a transport fault")

    table = CatalogTable(
        id="test.orders",
        name="orders",
        fullyQualifiedName="test_db.public.orders",
        description="orders",
        columns=[CatalogColumn(name="order_id", dataType="INT", constraint="PRIMARY_KEY")],
    )

    with patch("coa_ontology.inducer.strategies.table_to_ontology.log") as mock_log:
        _graph, novel, matches, dropped = TableToOntologyStrategy().induce(
            tables=[table],
            ontology_uri_prefix="http://ex.org/induced#",
            config={},
            pipeline=_BuggyPipeline(),
            grounding_ontology_ids=["urn:ontology:foundational"],
            grounding_mode="ENHANCED",
        )
    assert all(m.match_type == "novel" for m in matches)
    assert "orders" in novel
    mock_log.exception.assert_called_once()
