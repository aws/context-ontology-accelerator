# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Determinism guard for the parallelized second-pass column-match loop.

The column loop in ``match_concepts`` is parallelized with a bounded
ThreadPoolExecutor (width via ``INDUCER_COLUMN_MATCH_WORKERS``). Correctness
hinges on writing results BY INDEX (``matches[idx] = match``) so the output is
independent of thread completion order. This test locks that invariant: the
returned matches list must be byte-identical whether the pool runs 1-wide
(effectively serial) or 8-wide.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.pipeline import InductionPipeline, SourceConcept

_DIMS = 12


def _onehot(k: int) -> list[float]:
    v = [0.0] * _DIMS
    v[k] = 1.0
    return v


def _fake_search_embeddings(*, vector, embedding_type, model_id, entity_type, ontology_id, top_k):
    """Deterministic per-input candidate generator (NOT order-dependent).

    Keys off the one-hot position of the concept vector so each column gets a
    stable, distinct winner across three buckets:
      hot % 3 == 0 → exact (cosine 1.0)
      hot % 3 == 1 → high_confidence (cosine ~0.86)
      hot % 3 == 2 → novel (no candidates)
    """
    hot = max(range(len(vector)), key=lambda j: vector[j])
    bucket = hot % 3
    if bucket == 2:
        return []
    cand_vec = list(vector)
    if bucket == 1:
        cand_vec[_DIMS - 1] = cand_vec[_DIMS - 1] + 0.6  # perturb an unused dim → ~0.86
    return [{"entity_uri": f"http://onto/prop_{hot}", "ontology_id": "urn:onto:foundational", "vector": cand_vec}]


def _build_concepts() -> list[SourceConcept]:
    """2 table-level + 10 column concepts across 2 tables (12 total)."""
    concepts: list[SourceConcept] = []
    tables = [("orders", range(0, 5)), ("products", range(5, 10))]
    for table_name, cols in tables:
        concepts.append(
            SourceConcept(
                table_fqn=f"db.{table_name}",
                table_name=table_name,
                column_name="",
                description=f"{table_name} table",
                data_type="",
                is_table_level=True,
                vector=[0.0] * _DIMS,
            )
        )
        for k in cols:
            concepts.append(
                SourceConcept(
                    table_fqn=f"db.{table_name}",
                    table_name=table_name,
                    column_name=f"col_{k}",
                    description=f"column {k}",
                    data_type="int",
                    vector=_onehot(k),
                )
            )
    return concepts


def _run_match(worker_count: str, monkeypatch) -> list[ConceptMatch]:
    monkeypatch.setenv("INDUCER_COLUMN_MATCH_WORKERS", worker_count)

    oc = MagicMock()
    oc.search_embeddings.side_effect = _fake_search_embeddings
    pipeline = InductionPipeline(None, MagicMock(), oc)

    # First pass grounds every table as novel (matched_ontology_id=None), so the
    # column loop takes the grounding-pool fallback path and actually searches.
    benign_table_match = ConceptMatch(
        source_column="",
        source_table="",
        matched_class_uri=None,
        matched_ontology_id=None,
        similarity=None,
        match_type="novel",
    )

    with patch("coa_ontology.inducer.services.pipeline.GroundingService") as mock_grounding_cls:
        grounding_svc = mock_grounding_cls.return_value
        grounding_svc.to_concept_match.return_value = benign_table_match

        return pipeline.match_concepts(
            _build_concepts(),
            confidence_threshold=0.8,
            model_id="titan",
            grounding_ontology_ids=["urn:onto:foundational"],
        )


def _project(matches: list[ConceptMatch]) -> list[tuple]:
    return [(m.source_table, m.source_column, m.match_type, m.matched_class_uri) for m in matches]


def test_column_match_parallel_equals_serial(monkeypatch):
    """Parallel (8-wide) output must equal serial (1-wide) output, in order.

    Compares the FULL ordered list — an out-of-order write (append/order-reliant
    instead of ``matches[idx] = match``) would reorder these tuples and fail.
    """
    serial = _run_match("1", monkeypatch)
    parallel = _run_match("8", monkeypatch)

    # Full ordered equality — the real invariant.
    assert _project(parallel) == _project(serial)
    assert parallel == serial

    # Sanity: the fixture actually exercises variety (not all-novel), so the
    # ordered comparison above is a meaningful guard, not vacuously true.
    col_types = {m.match_type for m in serial if m.source_column}
    assert {"exact", "high_confidence", "novel"} <= col_types
    col_uris = [m.matched_class_uri for m in serial if m.source_column and m.matched_class_uri]
    assert len(set(col_uris)) == len(col_uris)  # each matched column has a distinct winner
