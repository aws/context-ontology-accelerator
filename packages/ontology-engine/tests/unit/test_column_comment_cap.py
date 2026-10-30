# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The class text keeps 255 characters of each column note for the SQL writer.

A Glue column comment allows 255 characters. The old 100-character cut dropped
the deciding clause of longer steward notes, so the SQL writer never saw it. The
embedded text stays at 100: the embedding input is cut at 8000 characters, and
longer notes would push a wide table's last columns out of the vector.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog import ingest
from coa_ontology.catalog.ingest import _accumulate_embeddings
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace

pytestmark = pytest.mark.unit

NS = Namespace("http://example.org/onto/")


def _class_texts(note: str) -> tuple[str, str]:
    """Return ``(context_text, embed_text)`` for a one-column class carrying ``note``."""
    g = Graph()
    g.add((NS.Orders, RDF.type, OWL.Class))
    g.add((NS.Orders, RDFS.label, Literal("orders")))
    g.add((NS.amount, RDF.type, OWL.DatatypeProperty))
    g.add((NS.amount, RDFS.domain, NS.Orders))
    g.add((NS.amount, RDFS.range, XSD.double))
    g.add((NS.amount, RDFS.label, Literal("amount")))
    g.add((NS.amount, RDFS.comment, Literal(note)))
    with patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient") as bedrock_cls:
        bedrock = MagicMock()
        bedrock.model_id = "amazon.titan-embed-text-v2:0"
        bedrock.embed_texts.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
        bedrock_cls.return_value = bedrock
        store = MagicMock()
        _accumulate_embeddings(
            vector_store=store,
            graph=g,
            ontology_id=str(NS),
            classes={NS.Orders},
            obj_props=set(),
            dt_props={NS.amount},
            namespace="ns",
            class_to_datasource={str(NS.Orders): "ds1"},
        )
        items = store.store_embeddings_batch.call_args[0][0]
        embedded = [t for call in bedrock.embed_texts.call_args_list for t in call[0][0]]
    context_text = next(it["context_text"] for it in items if it["entity_uri"] == str(NS.Orders))
    embed_text = next(t for t in embedded if "Columns:" in t)
    return context_text, embed_text


def test_caps_are_the_documented_values():
    assert ingest._MAX_COLUMN_COMMENT_CHARS == 255
    assert ingest._MAX_COLUMN_COMMENT_EMBED_CHARS == 100


def test_230_character_note_is_whole_for_the_writer_and_cut_at_100_for_the_embedding():
    note = "a" * 219 + " END-CLAUSE"
    assert len(note) == 230
    context_text, embed_text = _class_texts(note)
    assert f"({note})" in context_text
    assert "END-CLAUSE" not in embed_text
    assert "(" + note[:100] + ")" in embed_text
    assert note[:101] not in embed_text


def test_writer_text_keeps_a_note_of_exactly_255_characters():
    note = "b" * 254 + "Z"
    context_text, _ = _class_texts(note)
    assert f"({note})" in context_text


def test_writer_text_loses_the_256th_character():
    context_text, _ = _class_texts("c" * 255 + "Z")
    assert "(" + "c" * 255 + ")" in context_text
    assert "Z" not in context_text.split("Columns:")[1]


def test_embedding_text_keeps_exactly_100_and_loses_the_101st_character():
    _, embed_text = _class_texts("d" * 100 + "Z")
    assert "(" + "d" * 100 + ")" in embed_text
    assert "Z" not in embed_text.split("Columns:")[1]
