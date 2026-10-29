# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract with the GraphRAG toolkit's topic-extraction reply parser.

The parser matches the response-format labels literally and case-sensitively and
drops every other line, so a model reply with translated labels yields zero topics
and no error. That is why the language instruction pins the labels to English.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

topic_utils = pytest.importorskip("graphrag_toolkit.lexical_graph.indexing.utils.topic_utils")

_REPLY = """{topic}: Equipos de soldadura

  {entities}:

    máquina de soldar WD-42|Equipo
    Sato|Persona

  {proposition}: La persona a cargo de la máquina de soldar WD-42 es Sato.

    {relationships}:
    Sato|RESPONSABLE_DE|máquina de soldar WD-42
"""

_ENGLISH = {
    "topic": "topic",
    "entities": "entities",
    "proposition": "proposition",
    "relationships": "entity-entity relationships",
}
_SPANISH = {
    "topic": "tema",
    "entities": "entidades",
    "proposition": "proposición",
    "relationships": "entidad-entidad relaciones",
}


def _count(reply: str) -> int:
    topics, _ = topic_utils.parse_extracted_topics(reply)
    return len(topics.topics)


def test_parse_extracted_topics_english_labels_yield_topics():
    assert _count(_REPLY.format(**_ENGLISH)) >= 1


def test_parse_extracted_topics_all_translated_labels_yield_zero_topics():
    # The realistic es-419 case: every label is translated, so the parser matches
    # none of them and drops to zero.
    assert _count(_REPLY.format(**_SPANISH)) == 0


@pytest.mark.parametrize("label", sorted(_ENGLISH))
def test_parse_extracted_topics_single_translated_label_is_tolerated(label):
    # Translating only one label does not zero the count on this fixture: the
    # parser seeds current_topic=DEFAULT_TOPIC and still emits a topic from the
    # lines whose labels it does recognize. This pins the per-label behaviour so a
    # future parser change that made any single mismatch fatal would be caught,
    # and documents that the drop in the all-translated case is cumulative, not
    # attributable to one label in isolation.
    reply = dict(_ENGLISH)
    reply[label] = _SPANISH[label]
    assert _count(_REPLY.format(**reply)) >= 1


def test_parse_extracted_topics_fullwidth_colon_on_single_label_is_tolerated():
    # A single full-width colon (`topic：`) is tolerated for the same reason a
    # single translated label is.
    reply = _REPLY.format(**_ENGLISH).replace("topic:", "topic：", 1)
    assert _count(reply) >= 1


def test_parse_extracted_topics_all_fullwidth_colons_yield_zero_topics():
    # The full-width-colon failure the commit names: when every label's colon is
    # full-width (`：`), the parser matches no label and drops to zero.
    reply = _REPLY.format(**_ENGLISH).replace(":", "：")
    assert _count(reply) == 0
