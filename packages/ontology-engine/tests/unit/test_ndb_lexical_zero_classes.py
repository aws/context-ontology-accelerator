# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Zero-class error from ``NeptuneDatabaseLexicalStore.get_graph_schema``."""

from __future__ import annotations

import pytest
from coa_ontology.inducer.unstructured.stores.ndb_lexical import NeptuneDatabaseLexicalStore

pytestmark = pytest.mark.unit


def test_get_graph_schema_zero_classes_error_says_to_reingest(monkeypatch):
    store = NeptuneDatabaseLexicalStore(endpoint="neptune.example.com")
    monkeypatch.setattr(store, "get_class_nodes", list)

    with pytest.raises(ValueError, match="zero class entries") as excinfo:
        store.get_graph_schema()

    assert "not a valid lexical graph" in str(excinfo.value)
    assert "re-ingest the document source" in str(excinfo.value)
