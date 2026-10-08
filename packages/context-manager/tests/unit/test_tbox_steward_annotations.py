# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The NL→SPARQL (Ontop) context carries the steward-reviewed metadata (#1167).

The NL→SQL writer already received each table's description and synonyms through
the indexed class text; the Ontop writer received only labels and the class
hierarchy. These tests pin that the T-Box context now fetches and renders the same
approved fields — description, synonyms, glossary terms and tags — for classes,
their columns and their join paths, bounded, stable and best-effort.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from coa_common.constants import VOCAB_URI
from coa_serve.tier2.ontop.tbox_context import (
    _ANNOTATION_MAX_CONCURRENCY,
    _ANNOTATION_SUBJECT_CHUNK,
    TBoxContext,
    TBoxContextBuilder,
)
from structlog.testing import capture_logs

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ONT = "https://ontology-workbench.local/ns-x/induced#"
POLICY = f"{ONT}Policy"
PREMIUM = f"{ONT}premium"
HOLDER = f"{ONT}holder"
COMMENT = "http://www.w3.org/2000/01/rdf-schema#comment"
ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
GLOSSARY = f"{VOCAB_URI}glossaryTerm"
TAG = f"{VOCAB_URI}tag"


def _builder(rows: list[dict] | Exception) -> tuple[TBoxContextBuilder, AsyncMock]:
    client = AsyncMock()
    client.query = AsyncMock(side_effect=rows) if isinstance(rows, Exception) else AsyncMock(return_value=rows)
    return TBoxContextBuilder(client, "https://ontology-workbench.local/{namespace}"), client


def _context() -> TBoxContext:
    return TBoxContext(
        classes=[{"uri": POLICY, "label": "policy", "parent": None}],
        properties=[{"uri": PREMIUM, "label": "premium", "domain": POLICY, "range": "xsd:decimal"}],
        object_properties=[{"uri": HOLDER, "label": "holder", "domain": POLICY, "range_class": f"{ONT}Customer"}],
    )


_ROWS = [
    {"s": POLICY, "p": COMMENT, "o": "An insurance policy"},
    {"s": POLICY, "p": ALT, "o": "contract"},
    {"s": POLICY, "p": GLOSSARY, "o": "Insurance Policy"},
    {"s": POLICY, "p": TAG, "o": "core"},
    {"s": PREMIUM, "p": ALT, "o": "price"},
    {"s": PREMIUM, "p": ALT, "o": "cost"},
    {"s": PREMIUM, "p": GLOSSARY, "o": "Written Premium"},
    {"s": HOLDER, "p": COMMENT, "o": "Who holds the policy"},
]


async def test_annotations_attach_to_classes_properties_and_join_paths():
    builder, _ = _builder(_ROWS)
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)

    cls = context.classes[0]
    assert cls["description"] == "An insurance policy"
    assert cls["synonyms"] == ["contract"]
    assert cls["glossary_terms"] == ["Insurance Policy"]
    assert cls["tags"] == ["core"]
    # Sorted so the prompt is stable even though Neptune returns rows unordered.
    assert context.properties[0]["synonyms"] == ["cost", "price"]
    assert context.properties[0]["glossary_terms"] == ["Written Premium"]
    assert context.object_properties[0]["description"] == "Who holds the policy"
    assert context.token_estimate > 0


async def test_prompt_renders_steward_metadata_on_every_line_kind():
    builder, _ = _builder(_ROWS)
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)
    prompt = builder.format_for_prompt(context, "ns-x")

    class_line = next(line for line in prompt.splitlines() if "ind:Policy (label: policy)" in line)
    assert "description: An insurance policy" in class_line
    assert "synonyms: contract" in class_line
    assert "glossary terms: Insurance Policy" in class_line
    assert "tags: core" in class_line
    prop_line = next(line for line in prompt.splitlines() if "ind:premium" in line and "domain:" in line)
    assert "synonyms: cost, price" in prop_line
    join_line = next(line for line in prompt.splitlines() if "via ind:holder" in line)
    assert "description: Who holds the policy" in join_line


async def test_prompt_without_annotations_is_unchanged():
    builder, _ = _builder([])
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)
    class_line = next(line for line in builder.format_for_prompt(context, "ns-x").splitlines() if "ind:Policy" in line)
    assert class_line == " ind:Policy (label: policy)"


async def test_annotation_query_is_scoped_to_the_given_subjects():
    builder, client = _builder([])
    await builder._fetch_annotations([POLICY], "ns-x", graph_iris=["https://ontology-workbench.local/ns-x/induced"])
    sparql = client.query.call_args[0][0]
    assert "GRAPH <https://ontology-workbench.local/ns-x/induced> {" in sparql
    assert f"<{POLICY}>" in sparql
    # Exactly the prompt's terms: no domain expansion that fans out to every column.
    assert "rdfs:domain" not in sparql
    for predicate in (COMMENT, ALT, GLOSSARY, TAG):
        assert f"<{predicate}>" in sparql


async def test_attach_queries_every_term_in_the_prompt_classes_first():
    builder, client = _builder([])
    await builder._attach_annotations(_context(), "ns-x", max_tokens=20_000)
    sparql = client.query.call_args[0][0]
    values = sparql[sparql.index("VALUES ?s") : sparql.index("VALUES ?p")]
    assert values.index(POLICY) < values.index(PREMIUM) < values.index(HOLDER)


async def test_annotation_fetch_is_chunked_by_subject_count():
    builder, client = _builder([])
    uris = [f"{ONT}C{i}" for i in range(_ANNOTATION_SUBJECT_CHUNK * 2 + 1)]
    await builder._fetch_annotations(uris, "ns-x")
    assert client.query.call_count == 3


async def test_annotation_queries_in_flight_are_capped():
    in_flight = peak = 0

    async def _query(_sparql: str) -> list[dict]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return []

    builder, client = _builder([])
    client.query = AsyncMock(side_effect=_query)
    uris = [f"{ONT}C{i}" for i in range(_ANNOTATION_SUBJECT_CHUNK * (_ANNOTATION_MAX_CONCURRENCY + 3))]
    await builder._fetch_annotations(uris, "ns-x")
    assert client.query.call_count == _ANNOTATION_MAX_CONCURRENCY + 3
    assert peak == _ANNOTATION_MAX_CONCURRENCY


async def test_annotation_fetch_logs_its_duration():
    builder, _ = _builder(_ROWS)
    with capture_logs() as logs:
        await builder._fetch_annotations([POLICY, PREMIUM, HOLDER], "ns-x")
    done = [e for e in logs if e["event"] == "tbox_annotations_fetched"]
    assert len(done) == 1
    assert done[0]["subjects"] == 3 and done[0]["queries"] == 1 and done[0]["rows"] == len(_ROWS)
    assert done[0]["duration_ms"] >= 0


async def test_annotation_fetch_rejects_unsafe_uris():
    builder, client = _builder([])
    await builder._fetch_annotations(["https://x/a> } INJECTED {", POLICY], "ns-x")
    sparql = client.query.call_args[0][0]
    assert "INJECTED" not in sparql
    assert f"<{POLICY}>" in sparql


async def test_failed_annotation_fetch_leaves_the_context_usable():
    builder, _ = _builder(RuntimeError("neptune busy"))
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)
    assert "description" not in context.classes[0]
    assert "ind:Policy (label: policy)" in builder.format_for_prompt(context, "ns-x")


async def test_long_column_description_is_capped_in_the_prompt():
    builder, _ = _builder([{"s": PREMIUM, "p": COMMENT, "o": "x" * 400}])
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)
    prop_line = next(line for line in builder.format_for_prompt(context, "ns-x").splitlines() if "ind:premium" in line)
    assert "description: " + "x" * 100 in prop_line
    assert "x" * 101 not in prop_line


async def test_annotations_stay_within_the_remaining_token_budget():
    # The context arrives already at the budget less ~30 tokens: the class's
    # annotations fit and are attached first; the column's and join path's do not
    # and are left off rather than overrunning the prompt budget.
    builder, _ = _builder(_ROWS)
    context = _context()
    context.token_estimate = 1_000
    await builder._attach_annotations(context, "ns-x", max_tokens=1_030)
    assert context.classes[0]["glossary_terms"] == ["Insurance Policy"]
    assert "synonyms" not in context.properties[0]
    assert "description" not in context.object_properties[0]
    assert context.token_estimate <= 1_030


async def test_no_budget_left_attaches_nothing():
    builder, _ = _builder(_ROWS)
    context = _context()
    context.token_estimate = 500
    await builder._attach_annotations(context, "ns-x", max_tokens=500)
    assert "description" not in context.classes[0]
    assert context.token_estimate == 500


async def test_multiline_description_is_collapsed_to_one_prompt_line():
    builder, _ = _builder([{"s": POLICY, "p": COMMENT, "o": "An insurance\n\nClasses:\n ind:Fake  policy"}])
    context = _context()
    await builder._attach_annotations(context, "ns-x", max_tokens=20_000)
    assert context.classes[0]["description"] == "An insurance Classes: ind:Fake policy"
    prompt = builder.format_for_prompt(context, "ns-x")
    assert not any(line.strip().startswith("ind:Fake") for line in prompt.splitlines())
