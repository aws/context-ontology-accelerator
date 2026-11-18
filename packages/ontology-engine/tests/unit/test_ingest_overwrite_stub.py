# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit test: ingest_ontology(overwrite_stub=True) treats an "ingesting" stub row
as overwritable, not a conflict.

The async upload path writes an ``ingesting`` stub registry row up-front (so the
ontology shows in the list immediately with a spinner). The background ingest
then runs with ``overwrite_stub=True`` — it must NOT 409 against its own stub,
but a REAL (non-stub) existing row must still conflict.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import coa_ontology.catalog.ingest as ingest_mod
import pytest
from coa_ontology.catalog.ingest import (
    ONTOLOGY_STATUS_INGESTING,
    IngestConflictError,
    IngestMetadata,
    ingest_ontology,
)

pytestmark = pytest.mark.unit

_TTL = "@prefix owl: <http://www.w3.org/2002/07/owl#> . <http://ex/A> a owl:Class ."


def _md() -> IngestMetadata:
    return IngestMetadata(ontology_id="http://ex/onto", title="Onto", ontology_type="user_created", format="turtle")


def _run(monkeypatch, existing_row, overwrite_stub):
    monkeypatch.setattr(ingest_mod.dynamo_store, "get_ontology_registry", lambda ns, oid: existing_row)
    gs = MagicMock()
    monkeypatch.setattr(ingest_mod.dynamo_store, "put_ontology_registry", lambda *a, **k: {"uri": "http://ex/onto"})
    monkeypatch.setattr(ingest_mod.dynamo_store, "put_namespace_meta", lambda *a, **k: None)

    # Short-circuit embedding/graph work: raise a sentinel right after the guard
    # so we don't have to mock the entire pipeline. create_ontology is the first
    # thing the non-append path calls, so raising there proves we passed the guard.
    class _Passed(Exception):
        pass

    gs.create_ontology.side_effect = lambda *a, **k: (_ for _ in ()).throw(_Passed())
    try:
        ingest_ontology(
            graph_store=gs,
            vector_store=MagicMock(),
            content=_TTL,
            namespace="ns-a",
            metadata=_md(),
            overwrite_stub=overwrite_stub,
        )
    except _Passed:
        return "passed_guard"
    return "no_conflict_no_create"


def test_overwrite_stub_passes_guard_on_ingesting_stub(monkeypatch):
    stub = {"status": ONTOLOGY_STATUS_INGESTING, "uri": "http://ex/onto"}
    assert _run(monkeypatch, stub, overwrite_stub=True) == "passed_guard"


def test_stub_still_conflicts_without_overwrite_flag(monkeypatch):
    stub = {"status": ONTOLOGY_STATUS_INGESTING, "uri": "http://ex/onto"}
    with pytest.raises(IngestConflictError):
        _run(monkeypatch, stub, overwrite_stub=False)


def test_real_existing_row_conflicts_even_with_overwrite_flag(monkeypatch):
    # A fully-ingested row (status not "ingesting") is a genuine conflict — the
    # overwrite_stub escape hatch must NOT clobber real user content.
    real = {"status": "active", "parse_status": "ok", "uri": "http://ex/onto", "class_count": 5}
    with pytest.raises(IngestConflictError):
        _run(monkeypatch, real, overwrite_stub=True)


def test_no_existing_row_is_a_normal_fresh_ingest(monkeypatch):
    assert _run(monkeypatch, None, overwrite_stub=True) == "passed_guard"
