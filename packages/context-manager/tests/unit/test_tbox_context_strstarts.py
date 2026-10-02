# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for TBoxContextBuilder._fetch_ontology_context STRSTARTS fix.

Verifies that _fetch_ontology_context uses GRAPH ?g + STRSTARTS (not
GRAPH <exact_uri>) so that hash-namespaced ontology URIs match correctly
against Neptune's percent-encoded graph names.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_serve.tier2.ontop.types import VectorHit


def _hit(uri: str, hit_type: str = "ontology_class", score: float = 0.9) -> VectorHit:
    return VectorHit(
        type=hit_type,
        score=score,
        entity_id=uri,
        uri=uri,
        metadata={"entity_uri": uri, "entity_type": "class"},
    )


@pytest.fixture
def graph_client():
    """Mock GraphClient returning predictable results."""
    client = AsyncMock()
    client.query = AsyncMock(return_value=[])
    return client


@pytest.fixture
def tbox_builder(graph_client):
    """TBoxContextBuilder with a known graph URI template."""
    with patch.dict(os.environ, {"GRAPH_URI_TEMPLATE": "https://ontology-workbench.local/{namespace}"}):
        from coa_serve.tier2.ontop.tbox_context import TBoxContextBuilder

        return TBoxContextBuilder(graph_client, "https://ontology-workbench.local/{namespace}")


@pytest.mark.unit
@pytest.mark.asyncio
class TestFetchOntologyContextSTRSTARTS:
    """Verify STRSTARTS pattern in _fetch_ontology_context."""

    async def test_uses_strstarts_not_exact_graph(self, tbox_builder, graph_client):
        """Query must use GRAPH ?g + STRSTARTS, not GRAPH <exact_uri>."""
        hits = [_hit("https://example.org/ontology#Employee")]
        await tbox_builder._fetch_ontology_context(hits, "test-ns")

        graph_client.query.assert_called_once()
        sparql = graph_client.query.call_args[0][0]

        # Must use STRSTARTS pattern
        assert "STRSTARTS(STR(?g)" in sparql
        assert "GRAPH ?g" in sparql
        # Must NOT use exact graph URI pattern
        assert "GRAPH <" not in sparql

    async def test_includes_namespace_prefix_in_filter(self, tbox_builder, graph_client):
        """The STRSTARTS filter should include the namespace in the prefix."""
        hits = [_hit("https://example.org/retail/Product")]
        await tbox_builder._fetch_ontology_context(hits, "my-namespace")

        sparql = graph_client.query.call_args[0][0]
        assert "https://ontology-workbench.local/my-namespace/" in sparql

    async def test_handles_multi_ontology_hits(self, tbox_builder, graph_client):
        """Multi-ontology hits should still use STRSTARTS (covering all sub-graphs)."""
        hits = [
            _hit("https://example.org/hr#Employee"),
            _hit("https://example.org/retail#Product", score=0.8),
        ]
        await tbox_builder._fetch_ontology_context(hits, "test-ns")

        sparql = graph_client.query.call_args[0][0]
        # STRSTARTS covers ALL sub-graphs under the namespace prefix
        assert "STRSTARTS" in sparql
        assert "GRAPH <" not in sparql

    async def test_hash_namespace_not_in_graph_uri(self, tbox_builder, graph_client):
        """The derived ontology base (with #) must NOT appear as a percent-encoded graph URI.

        Previously the code would produce GRAPH <.../ontology%23> which never matches
        the actual graph at GRAPH <.../ontology> (without %23).
        """
        hits = [_hit("https://example.org/ontology#Employee")]
        await tbox_builder._fetch_ontology_context(hits, "test-ns")

        sparql = graph_client.query.call_args[0][0]
        # The old bug would encode the hash as %23 in the graph URI
        assert "%23" not in sparql
        # And should not use a fixed graph name at all
        assert "GRAPH <https://ontology-workbench.local" not in sparql


_IS_MAPPED_IRI = f"{GRAPH_BASE_URI}/vocab/coa#isMapped"


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxContextIsMappedFilter:
    """Tier-2 answerability filter: every class SELECT requires coa:isMapped true.

    Unmapped (unstructured / foundational) classes must not enter the
    structured-query T-Box context. The marker is REQUIRED (not OPTIONAL), so
    absence == hidden.
    """

    async def test_full_namespace_count_and_fetch_require_ismapped(self, tbox_builder, graph_client):
        # count returns a small mapped count so the full-fetch path runs.
        graph_client.query = AsyncMock(side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], []])
        await tbox_builder._try_full_namespace_context("ns-x")

        count_sparql = graph_client.query.call_args_list[0][0][0]
        fetch_sparql = graph_client.query.call_args_list[1][0][0]
        # Both the threshold count and the class fetch gate on the mapped marker.
        assert f"<{_IS_MAPPED_IRI}> true" in count_sparql
        assert f"<{_IS_MAPPED_IRI}> true" in fetch_sparql

    async def test_fetch_ontology_context_requires_ismapped(self, tbox_builder, graph_client):
        await tbox_builder._fetch_ontology_context([_hit("https://example.org/o#Employee")], "ns-x")
        sparql = graph_client.query.call_args[0][0]
        assert f"<{_IS_MAPPED_IRI}> true" in sparql

    async def test_fetch_by_entities_requires_ismapped(self, tbox_builder, graph_client):
        await tbox_builder._fetch_by_entities("how many employees", "ns-x")
        sparql = graph_client.query.call_args[0][0]
        assert f"<{_IS_MAPPED_IRI}> true" in sparql

    async def test_fetch_by_entities_preserves_unicode_graphemes(self, tbox_builder, graph_client):
        await tbox_builder._fetch_by_entities("İstanbul कर्मचारी เครื่องปรับอากาศ", "ns-x")

        sparql = graph_client.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "i̇stanbul")' in sparql
        assert 'CONTAINS(LCASE(?label), "कर्मचारी")' in sparql
        assert 'CONTAINS(LCASE(?label), "เครื่องปรับอากาศ")' in sparql
        assert f"<{_IS_MAPPED_IRI}> true" in sparql

    async def test_fetch_by_entities_uses_bounded_reverse_containment_for_no_space_scripts(
        self, tbox_builder, graph_client
    ):
        await tbox_builder._fetch_by_entities("请告诉我这台空调设备的制造商和型号", "ns-x")

        sparql = graph_client.query.call_args[0][0]
        assert 'CONTAINS("请告诉我这台空调设备的制造商和型号", LCASE(STR(?label)))' in sparql
        assert "STRLEN(STR(?label)) >= 2" in sparql
        assert 'CONTAINS(LCASE(?label), "请告")' not in sparql

    async def test_fetch_by_entities_keeps_japanese_suffix_concepts(self, tbox_builder, graph_client):
        await tbox_builder._fetch_by_entities(
            "この施設に設置されているエレベーターのメーカーと型番を教えて",
            "ns-x",
        )

        sparql = graph_client.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "エレベーター")' in sparql
        assert 'CONTAINS(LCASE(?label), "メーカー")' in sparql
        assert 'CONTAINS(LCASE(?label), "型番")' in sparql

    async def test_fetch_by_entities_skips_the_query_when_no_candidate_survives(self, tbox_builder, graph_client):
        classes, properties = await tbox_builder._fetch_by_entities("of the", "ns-x")

        assert (classes, properties) == ([], [])
        graph_client.query.assert_not_called()

    async def test_fetch_by_entities_keeps_matched_classes_when_property_rows_would_truncate(
        self, tbox_builder, graph_client
    ):
        """A matched class must survive property-row volume.

        Joined against the datatype OPTIONAL, one class fans out to
        (properties x distinct values) rows; past the row cap, with no ORDER BY, a
        class vanished from the parsed result and therefore from the prompt.
        Reverse containment made that reachable by matching far more classes.
        """
        class_rows = [
            {"class": f"https://ex.org/o#C{index}", "label": f"C{index}", "parentClass": None} for index in range(40)
        ]
        prop_rows = [
            {"class": "https://ex.org/o#C0", "property": f"https://ex.org/o#p{index}", "propLabel": f"p{index}"}
            for index in range(2000)
        ]
        graph_client.query = AsyncMock(side_effect=[class_rows, prop_rows])

        classes, _properties = await tbox_builder._fetch_by_entities("空調設備型番", "ns-x")

        assert {c["uri"] for c in classes} == {f"https://ex.org/o#C{index}" for index in range(40)}

    async def test_fetch_by_entities_degrades_to_classes_when_property_fetch_fails(self, tbox_builder, graph_client):
        graph_client.query = AsyncMock(
            side_effect=[
                [{"class": "https://ex.org/o#Emp", "label": "Emp", "parentClass": None}],
                RuntimeError("neptune down"),
            ]
        )

        classes, properties = await tbox_builder._fetch_by_entities("how many employees", "ns-x")

        assert [c["uri"] for c in classes] == ["https://ex.org/o#Emp"]
        assert properties == []

    async def test_fetch_by_entities_deduplicates_classes_before_the_values_cap(self, tbox_builder, graph_client):
        """Multilingual labels must not collapse the property-fetch class list.

        The class query is DISTINCT over (class, label, parent), so a class with
        three language labels yields three rows. Capping raw rows at 50 covered only
        17 distinct classes and left the other 23 with no column hints — on exactly
        the namespaces this feature serves.
        """
        class_rows = [
            {"class": f"https://ex.org/o#C{index}", "label": label, "parentClass": None}
            for index in range(40)
            for label in (f"C{index}", f"クラス{index}", f"클래스{index}")
        ]
        graph_client.query = AsyncMock(side_effect=[class_rows, []])

        await tbox_builder._fetch_by_entities("空調設備型番", "ns-x")

        props_sparql = graph_client.query.call_args_list[1][0][0]
        distinct_in_values = {f"https://ex.org/o#C{index}" for index in range(40)}
        assert sum(uri in props_sparql for uri in distinct_in_values) == 40

    async def test_fetch_by_entities_logs_class_row_cap_truncation(self, tbox_builder, graph_client):
        """Past the class-query row cap, Neptune's row order decides what reaches
        the prompt, so the truncation must be observable."""
        from coa_serve.tier2.ontop import tbox_context as module

        class_rows = [
            {"class": f"https://ex.org/o#C{index}", "label": f"C{index}", "parentClass": None}
            for index in range(module._SPARQL_RESULT_LIMIT)
        ]
        graph_client.query = AsyncMock(side_effect=[class_rows, []])

        with patch.object(module, "logger") as log:
            await tbox_builder._fetch_by_entities("空調設備型番", "ns-x")

        assert any(call[0][0] == "tbox_entity_classes_truncated" for call in log.warning.call_args_list)

    async def test_fetch_by_entities_logs_property_uri_truncation(self, tbox_builder, graph_client):
        from coa_serve.tier2.ontop import tbox_context as module

        class_rows = [
            {"class": f"https://ex.org/o#C{index}", "label": f"C{index}", "parentClass": None}
            for index in range(module._MAX_SPARQL_VALUES_URIS + 10)
        ]
        graph_client.query = AsyncMock(side_effect=[class_rows, []])

        with patch.object(module, "logger") as log:
            classes, _properties = await tbox_builder._fetch_by_entities("空調設備型番", "ns-x")

        # Every matched class still surfaces; only the column hints are capped.
        assert len(classes) == module._MAX_SPARQL_VALUES_URIS + 10
        truncation = [c for c in log.info.call_args_list if c[0][0] == "tbox_entity_property_uris_truncated"]
        assert truncation and truncation[0][1]["matched"] == module._MAX_SPARQL_VALUES_URIS + 10

    async def test_object_properties_require_both_ends_mapped(self, tbox_builder, graph_client):
        await tbox_builder._fetch_object_properties("ns-x")
        sparql = graph_client.query.call_args[0][0]
        # Both domain and range must carry the mapped marker (mapped<->mapped only).
        assert f"?domain <{_IS_MAPPED_IRI}> true" in sparql
        assert f"?range <{_IS_MAPPED_IRI}> true" in sparql


def _class_gate_is_required(sparql: str) -> bool:
    """True iff the class-level ``?class <isMapped> true`` gate is a REQUIRED
    pattern (NOT wrapped in an OPTIONAL block).

    A plain substring check for ``<isMapped> true`` cannot tell a required gate
    from a fail-open ``OPTIONAL {{ ?class <isMapped> true }}``. This walks the
    brace structure: for each occurrence of the class-gate triple, it is required
    unless it sits inside an ``OPTIONAL {{ ... }}`` group. The parent gate
    intentionally lives inside an OPTIONAL (``?parentClass <isMapped> true``), so
    we match on the ``?class``-subject occurrence specifically.
    """
    import re

    gate = f"?class <{_IS_MAPPED_IRI}> true"
    # Find every OPTIONAL { ... } span (non-nested is sufficient for these queries).
    optional_spans = [
        (m.start(), _matching_brace_end(sparql, m.end() - 1)) for m in re.finditer(r"OPTIONAL\s*\{", sparql)
    ]
    for m in re.finditer(re.escape(gate), sparql):
        pos = m.start()
        inside_optional = any(start <= pos < end for start, end in optional_spans if end != -1)
        if not inside_optional:
            return True  # at least one occurrence is a required gate
    return False


def _matching_brace_end(s: str, open_idx: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_idx`` (or -1)."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxContextIsMappedRequiredNotOptional:
    """Regression guard: the class-level isMapped gate must be REQUIRED, not
    OPTIONAL. A substring assertion (``'<isMapped> true' in sparql``) passes even
    for a fail-open ``OPTIONAL {{ ?class <isMapped> true }}`` — which would silently
    re-expose every unmapped class to the structured prompt. These assert on the
    brace structure so that fail-open weakening is caught.
    """

    async def test_full_fetch_class_gate_is_required_not_optional(self, tbox_builder, graph_client):
        graph_client.query = AsyncMock(side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], []])
        await tbox_builder._try_full_namespace_context("ns-x")
        fetch_sparql = graph_client.query.call_args_list[1][0][0]
        assert _class_gate_is_required(fetch_sparql), (
            "class isMapped gate must be a REQUIRED pattern; an OPTIONAL-wrapped "
            "gate is fail-open and re-exposes unmapped classes"
        )

    async def test_fetch_ontology_context_class_gate_is_required(self, tbox_builder, graph_client):
        await tbox_builder._fetch_ontology_context([_hit("https://example.org/o#Employee")], "ns-x")
        assert _class_gate_is_required(graph_client.query.call_args[0][0])

    async def test_fetch_by_entities_class_gate_is_required(self, tbox_builder, graph_client):
        await tbox_builder._fetch_by_entities("how many employees", "ns-x")
        assert _class_gate_is_required(graph_client.query.call_args[0][0])


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxContextSubClassOfParentGated:
    """The rdfs:subClassOf parent is surfaced to the prompt (``subClassOf: X``),
    so it must be gated to mapped-only — otherwise a grounded class leaks its
    unmapped foundational parent IRI into the structured prompt.
    """

    async def test_parent_is_gated_on_ismapped_in_all_class_fetches(self, tbox_builder, graph_client):
        # full-namespace fetch
        graph_client.query = AsyncMock(side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], []])
        await tbox_builder._try_full_namespace_context("ns-x")
        fetch_sparql = graph_client.query.call_args_list[1][0][0]
        # The subClassOf OPTIONAL must ALSO require the parent to be mapped.
        assert "rdfs:subClassOf ?parentClass" in fetch_sparql
        assert f"?parentClass <{_IS_MAPPED_IRI}> true" in fetch_sparql

    async def test_parent_gate_present_in_entity_fetch(self, tbox_builder, graph_client):
        await tbox_builder._fetch_by_entities("how many employees", "ns-x")
        sparql = graph_client.query.call_args[0][0]
        assert f"?parentClass <{_IS_MAPPED_IRI}> true" in sparql


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxFullContextNoClassTruncation:
    """Full-namespace context must not silently drop mapped classes.

    The old single joined query (?class × OPTIONAL ?property) capped at
    LIMIT 2000 rows: with ~200 classes each having >10 datatype properties, the
    joined cardinality exceeded 2000 and — with no ORDER BY — whichever classes'
    rows landed past the cut vanished from the parsed result. The split (classes
    query bounded by the ≤200 threshold, properties query separate) fixes this.
    """

    async def test_all_classes_survive_when_join_would_exceed_row_cap(self, tbox_builder, graph_client):
        n_classes = 200
        # Class query: 200 distinct mapped classes (one row each — cannot exceed 2000).
        class_rows = [{"class": f"https://ex.org/o#C{i}", "label": f"C{i}"} for i in range(n_classes)]
        # Property query: the joined-shape rows that WOULD have pushed classes past
        # the old LIMIT 2000 (200 classes × 11 props = 2200 rows). In the OLD code
        # these shared one result set with the class rows and truncated it; now they
        # are a SEPARATE query, so no class row can be evicted by property volume.
        prop_rows = [
            {"class": f"https://ex.org/o#C{i}", "property": f"https://ex.org/o#p{i}_{j}", "propLabel": f"p{j}"}
            for i in range(n_classes)
            for j in range(11)
        ]
        graph_client.query = AsyncMock(side_effect=[[{"cnt": str(n_classes)}], class_rows, prop_rows])

        result = await tbox_builder._try_full_namespace_context("ns-x")
        assert result is not None
        classes, _properties = result
        # EVERY mapped class is present — none dropped by property-row volume.
        assert len(classes) == n_classes
        returned_uris = {c["uri"] for c in classes}
        assert returned_uris == {f"https://ex.org/o#C{i}" for i in range(n_classes)}

    async def test_class_query_is_separate_from_property_query(self, tbox_builder, graph_client):
        """Regression guard: the class list and property list are fetched by TWO
        distinct queries (a single joined query is what caused truncation)."""
        graph_client.query = AsyncMock(
            side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], [{"class": "c", "property": "p"}]]
        )
        await tbox_builder._try_full_namespace_context("ns-x")
        # count + classes + properties == 3 queries (was 2: count + joined-fetch).
        assert graph_client.query.call_count == 3
        classes_q = graph_client.query.call_args_list[1][0][0]
        props_q = graph_client.query.call_args_list[2][0][0]
        # The class query does NOT join properties (no ?property); the property
        # query is the one that carries ?property.
        assert "?property" not in classes_q
        assert "?property" in props_q


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxContextDatatypePropertyGate:
    """The per-class property OPTIONAL must be restricted to owl:DatatypeProperty.

    An object property's rdfs:range is a class IRI; if that target class is
    unmapped, an ungated ``?property rdfs:range ?range`` leaks the unmapped class
    name into the prompt's Properties section. Datatype-property ranges are xsd:*
    terms, which can never name a class. Object-property join paths are surfaced
    separately (mapped-gated on both ends) by _fetch_object_properties.
    """

    async def test_full_fetch_property_gated_to_datatype(self, tbox_builder, graph_client):
        # Full-context path now issues THREE queries: count, classes, properties.
        # The datatype-property gate lives in the third (properties) query.
        graph_client.query = AsyncMock(side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], []])
        await tbox_builder._try_full_namespace_context("ns-x")
        props_sparql = graph_client.query.call_args_list[2][0][0]
        assert "?property a owl:DatatypeProperty" in props_sparql

    async def test_full_fetch_property_query_gates_domain_class_on_ismapped(self, tbox_builder, graph_client):
        """The separate property query must ALSO require ?class isMapped — else a
        datatype property whose domain is an UNMAPPED class would make
        _parse_results register that unmapped class (with an empty label) in the
        class dict, reintroducing the unmapped-class leak."""
        graph_client.query = AsyncMock(side_effect=[[{"cnt": "3"}], [{"class": "c", "label": "C"}], []])
        await tbox_builder._try_full_namespace_context("ns-x")
        props_sparql = graph_client.query.call_args_list[2][0][0]
        assert f"<{_IS_MAPPED_IRI}> true" in props_sparql

    async def test_fetch_ontology_context_class_branch_gated_to_datatype(self, tbox_builder, graph_client):
        await tbox_builder._fetch_ontology_context([_hit("https://example.org/o#Employee")], "ns-x")
        sparql = graph_client.query.call_args[0][0]
        assert "?property a owl:DatatypeProperty" in sparql

    async def test_fetch_by_entities_gated_to_datatype(self, tbox_builder, graph_client):
        graph_client.query = AsyncMock(side_effect=[[{"class": "https://example.org/o#Emp", "label": "Emp"}], []])
        await tbox_builder._fetch_by_entities("how many employees", "ns-x")

        # The datatype gate lives in the second (property) query since the fetch
        # was split to stop property rows from evicting matched classes.
        props_sparql = graph_client.query.call_args_list[1][0][0]
        assert "?property a owl:DatatypeProperty" in props_sparql

    async def test_property_seeded_branch_gated_to_datatype(self, tbox_builder, graph_client):
        """A property-seeded hit branch must also require owl:DatatypeProperty, so a
        retrieved object property can't leak its class-IRI range into the prompt."""
        prop_hit = VectorHit(
            type="ontology_property",
            score=0.9,
            entity_id="p",
            uri="https://example.org/o#policyNumber",
            metadata={"entity_uri": "https://example.org/o#policyNumber", "entity_type": "property"},
        )
        await tbox_builder._fetch_ontology_context([prop_hit], "ns-x")
        sparql = graph_client.query.call_args[0][0]
        assert "?property a owl:DatatypeProperty" in sparql


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxGlossaryMappedGate:
    """The :aiContext glossary must be fetched only for nodes that entered the
    mapped context (mapped classes / datatype properties) plus metric hits — never
    for unmapped classes, which would ground a business term to an unqueryable node.
    """

    async def test_glossary_fetch_excludes_unmapped_hits(self, tbox_builder, graph_client):
        # Neptune returns ONE mapped class (Employee); the vector hits also include
        # an unmapped class (Ghost) that must not reach the aiContext fetch.
        mapped_uri = "https://example.org/o#Employee"
        unmapped_uri = "https://example.org/o#Ghost"
        # isMapped bridge probe (separate .ask method) has markers → keep strict
        # gate. Then via .query: full-context count → _fetch_ontology_context →
        # object-property fetch (skipped, single class) → _fetch_ai_context.
        graph_client.ask = AsyncMock(return_value=True)  # has markers → strict gate
        graph_client.query = AsyncMock(
            side_effect=[
                [],  # named-graph resolution: none found → prefix-filter fallback
                [{"cnt": "500"}],  # full-context count over threshold → skip full path
                [{"class": mapped_uri, "label": "Employee"}],  # _fetch_ontology_context
                [],  # _fetch_ai_context result
            ]
        )
        hits = [_hit(mapped_uri), _hit(unmapped_uri)]
        await tbox_builder.build(hits, "ns-x", query="employees")

        # The last query is the aiContext fetch — its VALUES clause must contain the
        # mapped URI and NOT the unmapped one.
        aicontext_sparql = graph_client.query.call_args_list[-1][0][0]
        assert "aiContext" in aicontext_sparql
        assert mapped_uri in aicontext_sparql
        assert unmapped_uri not in aicontext_sparql

    async def test_metric_hits_always_included_in_glossary_fetch(self, tbox_builder, graph_client):
        """Metric hits are answerable via the Tier-1 metric path, so their aiContext
        is fetched even though they aren't ontology classes."""
        metric_uri = "https://example.org/o#revenue_metric"
        graph_client.query = AsyncMock(
            side_effect=[
                [],  # named-graph resolution: none found → prefix-filter fallback
                [{"cnt": "500"}],  # skip full path
                [],  # _fetch_by_entities (no ontology hits, query present)
                [],  # _fetch_ai_context
            ]
        )
        metric_hit = VectorHit(
            type="metric", score=0.9, entity_id="m", uri=metric_uri, metadata={"entity_uri": metric_uri}
        )
        await tbox_builder.build([metric_hit], "ns-x", query="revenue")
        aicontext_sparql = graph_client.query.call_args_list[-1][0][0]
        assert metric_uri in aicontext_sparql


@pytest.mark.unit
class TestTBoxFormatForPrompt:
    """format_for_prompt rendering of the (now mapped-only) subClassOf parent."""

    def test_format_for_prompt_emits_parent_only_when_present(self, tbox_builder):
        """format_for_prompt renders subClassOf only when parent is populated —
        and after the gate, parent is populated only for mapped parents."""
        from coa_serve.tier2.ontop.tbox_context import TBoxContext

        ctx = TBoxContext(
            classes=[
                {"uri": "https://example.org/o#Orders", "label": "Orders", "parent": "https://example.org/o#Sales"},
                {"uri": "https://example.org/o#Refund", "label": "Refund", "parent": None},
            ],
            properties=[],
            object_properties=[],
            metrics=[],
        )
        out = tbox_builder.format_for_prompt(ctx, "ns-x")
        assert "subClassOf: ind:Sales" in out  # mapped parent surfaced
        # The parent-less class must NOT emit a subClassOf hint.
        refund_line = next(line for line in out.splitlines() if "Refund" in line)
        assert "subClassOf" not in refund_line


@pytest.mark.unit
@pytest.mark.asyncio
class TestIsMappedLegacyBridge:
    """The legacy-namespace bridge: when a namespace has ZERO coa:isMapped
    markers, the mapped-class gate is dropped so Tier-2 keeps working (pre-filter
    behavior) instead of silently returning no context."""

    _IS_MAPPED_IRI = f"{GRAPH_BASE_URI}/vocab/coa#isMapped"

    async def test_probe_runs_ask_scoped_to_namespace(self, tbox_builder, graph_client):
        """The bridge issues an ASK for the isMapped marker, scoped to the ns graph."""
        graph_client.ask = AsyncMock(return_value=False)
        has = await tbox_builder._namespace_has_mapped_markers("ns-x")
        assert has is False
        sparql = graph_client.ask.call_args[0][0]
        assert "ASK" in sparql
        assert f"<{self._IS_MAPPED_IRI}> true" in sparql
        assert "STRSTARTS(STR(?g)" in sparql  # namespace-scoped, not cross-namespace

    async def test_legacy_namespace_drops_gate_and_exposes_all_classes(self, tbox_builder, graph_client):
        """No markers anywhere → gate dropped → an UNMAPPED class still surfaces."""
        unmapped = "https://example.org/o#LegacyClass"
        graph_client.ask = AsyncMock(return_value=False)  # probe: NO isMapped markers
        graph_client.query = AsyncMock(
            side_effect=[
                [],  # named-graph resolution: none found → prefix-filter fallback
                [{"cnt": "3"}],  # full-context count (gate dropped → counts all)
                [{"class": unmapped, "label": "LegacyClass", "parentClass": None}],  # classes
                [],  # props
                [],  # aiContext
            ]
        )
        ctx = await tbox_builder.build([_hit(unmapped)], "legacy-ns", query="legacy things")
        # The unmapped class is present — pre-filter behavior restored.
        assert any(c["uri"] == unmapped for c in ctx.classes)
        # And the classes query must NOT carry the isMapped gate.
        classes_sparql = graph_client.query.call_args_list[2][0][0]
        assert f"<{self._IS_MAPPED_IRI}> true" not in classes_sparql

    async def test_mapped_namespace_keeps_gate(self, tbox_builder, graph_client):
        """Namespace WITH markers keeps the strict gate on the full-context query."""
        graph_client.ask = AsyncMock(return_value=True)  # probe: has markers
        graph_client.query = AsyncMock(
            side_effect=[
                [],  # named-graph resolution: none found → prefix-filter fallback
                [{"cnt": "2"}],  # count
                [{"class": "https://example.org/o#Emp", "label": "Emp", "parentClass": None}],
                [],  # props
                [],  # aiContext
            ]
        )
        await tbox_builder.build([_hit("https://example.org/o#Emp")], "mapped-ns", query="employees")
        classes_sparql = graph_client.query.call_args_list[2][0][0]
        assert f"<{self._IS_MAPPED_IRI}> true" in classes_sparql

    async def test_probe_failure_defaults_to_gate(self, tbox_builder, graph_client):
        """A probe error keeps the strict gate (conservative: never leak unmapped)."""
        graph_client.ask = AsyncMock(side_effect=RuntimeError("neptune down"))
        has = await tbox_builder._namespace_has_mapped_markers("ns-x")
        assert has is True

    async def test_object_properties_ungated_in_fallback(self, tbox_builder, graph_client):
        """mapped=False → object-property join query drops BOTH end gates."""
        graph_client.query = AsyncMock(return_value=[])
        await tbox_builder._fetch_object_properties("ns-x", mapped=False)
        sparql = graph_client.query.call_args[0][0]
        assert f"?domain <{self._IS_MAPPED_IRI}> true" not in sparql
        assert f"?range <{self._IS_MAPPED_IRI}> true" not in sparql
        # sanity: the strict counterpart DOES gate both ends
        graph_client.query = AsyncMock(return_value=[])
        await tbox_builder._fetch_object_properties("ns-x", mapped=True)
        strict = graph_client.query.call_args[0][0]
        assert f"?domain <{self._IS_MAPPED_IRI}> true" in strict
        assert f"?range <{self._IS_MAPPED_IRI}> true" in strict

    async def test_fetch_ontology_context_ungated_in_fallback(self, tbox_builder, graph_client):
        """mapped=False → vector-hit fetch drops the class gate and the parent gate."""
        graph_client.query = AsyncMock(return_value=[])
        await tbox_builder._fetch_ontology_context([_hit("https://example.org/o#Emp")], "ns-x", mapped=False)
        sparql = graph_client.query.call_args[0][0]
        assert f"<{self._IS_MAPPED_IRI}> true" not in sparql  # neither ?class nor ?parentClass gated
        assert "OPTIONAL { ?class rdfs:subClassOf ?parentClass . }" in sparql  # ungated parent OPTIONAL

    async def test_fetch_by_entities_ungated_in_fallback(self, tbox_builder, graph_client):
        """mapped=False → entity-fallback fetch drops the class gate."""
        graph_client.query = AsyncMock(return_value=[])
        await tbox_builder._fetch_by_entities("how many employees", "ns-x", mapped=False)
        sparql = graph_client.query.call_args[0][0]
        assert f"<{self._IS_MAPPED_IRI}> true" not in sparql

    async def test_probe_failure_keeps_gate_through_build(self, tbox_builder, graph_client):
        """End-to-end: a raising probe still yields GATED class queries via build()."""
        graph_client.ask = AsyncMock(side_effect=RuntimeError("neptune down"))
        graph_client.query = AsyncMock(
            side_effect=[
                [],  # named-graph resolution: none found → prefix-filter fallback
                [{"cnt": "2"}],  # count
                [{"class": "https://example.org/o#Emp", "label": "Emp", "parentClass": None}],
                [],  # props
                [],  # aiContext
            ]
        )
        await tbox_builder.build([_hit("https://example.org/o#Emp")], "ns-x", query="employees")
        classes_sparql = graph_client.query.call_args_list[2][0][0]
        assert f"<{self._IS_MAPPED_IRI}> true" in classes_sparql  # gate present despite probe failure


@pytest.mark.unit
@pytest.mark.asyncio
class TestTBoxGraphBinding:
    """The T-Box builder binds ``?g`` to the namespace's graphs.

    Every query here used to be the bare ``GRAPH ?g`` + ``STRSTARTS`` form, which
    matches in EVERY graph on the cluster and filters afterwards, so its cost was
    set by the whole store rather than by the namespace. On a Spider-2-sized
    cluster the builder's object-property (join-path) query timed out outright —
    leaving the SPARQL writer with no join paths and, on the widest namespaces,
    no T-Box at all.

    The assertions below check for constant ``GRAPH <iri>`` blocks and the ABSENCE
    of ``VALUES ?g``. That is not stylistic: the traversal tool's earlier
    ``VALUES``-bound form names the same graphs but is a materialised solution
    sequence joined against the patterns, so Neptune stops using ``?g`` to select
    quads — invisible on a one-graph namespace, a three-way ReadTimeout on a
    two-graph one. Both callers now emit the constant form.
    """

    _EMP = "https://example.org/o#Emp"

    @staticmethod
    def _build_sequence(graphs: list[dict]) -> list[list[dict]]:
        """build()'s query sequence: resolve, count, classes, props, aiContext."""
        return [
            graphs,
            [{"cnt": "2"}],
            [{"class": TestTBoxGraphBinding._EMP, "label": "Emp", "parentClass": None}],
            [],
            [],
        ]

    async def test_resolved_graphs_are_named_in_every_query(self, tbox_builder, graph_client):
        graph_iri = "https://ontology-workbench.local/ns-x/induced"
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=self._build_sequence([{"g": graph_iri}]))

        await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        # The resolution query itself is necessarily prefix-filtered — it is the
        # one that discovers the graphs — so skip it and check its beneficiaries.
        scoped = [c[0][0] for c in graph_client.query.call_args_list[1:]]
        assert scoped, "build() issued no scoped queries"
        for sparql in scoped:
            assert f"GRAPH <{graph_iri}> {{" in sparql
            assert "VALUES ?g" not in sparql
            assert "STRSTARTS(STR(?g)" not in sparql

    async def test_unresolved_graphs_fall_back_to_prefix_filter(self, tbox_builder, graph_client):
        """An unexpected graph layout must degrade to SLOW, never to no T-Box."""
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=self._build_sequence([]))

        ctx = await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        assert any(c["uri"] == self._EMP for c in ctx.classes)
        for call in graph_client.query.call_args_list[1:]:
            assert "STRSTARTS(STR(?g)" in call[0][0]
            assert "VALUES ?g" not in call[0][0]

    async def test_resolution_failure_falls_back_and_still_builds(self, tbox_builder, graph_client):
        """A raising resolution is caught: the build continues on the fallback."""
        seq = self._build_sequence([])
        seq[0] = RuntimeError("neptune down")
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=seq)

        ctx = await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        assert any(c["uri"] == self._EMP for c in ctx.classes)
        assert "STRSTARTS(STR(?g)" in graph_client.query.call_args_list[1][0][0]

    async def test_unsafe_graph_iris_are_dropped(self, tbox_builder, graph_client):
        """A graph IRI that cannot be safely interpolated is not interpolated."""
        safe = "https://ontology-workbench.local/ns-x/induced"
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(
            side_effect=self._build_sequence(
                [{"g": safe}, {"g": "https://ontology-workbench.local/ns-x/a> } INJECTED {"}, {"g": ""}]
            )
        )

        await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        classes_sparql = graph_client.query.call_args_list[2][0][0]
        assert f"GRAPH <{safe}> {{" in classes_sparql
        assert "INJECTED" not in classes_sparql

    async def test_graphs_resolved_once_per_namespace(self, tbox_builder, graph_client):
        """The builder is process-lived, so resolution is cached, not per query.

        Two builds for one namespace must issue ONE resolution query; a second
        namespace must not reuse the first one's graphs.
        """
        graph_a = "https://ontology-workbench.local/ns-a/induced"
        graph_b = "https://ontology-workbench.local/ns-b/induced"
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(
            side_effect=[
                *self._build_sequence([{"g": graph_a}]),
                *self._build_sequence([])[1:],  # second build on ns-a: NO resolution
                *self._build_sequence([{"g": graph_b}]),
            ]
        )

        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees")
        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees again")
        await tbox_builder.build([_hit(self._EMP)], "ns-b", query="employees")

        resolutions = [c[0][0] for c in graph_client.query.call_args_list if "owl:Ontology" in c[0][0]]
        assert len(resolutions) == 2  # once for ns-a, once for ns-b
        # The cached ns-a graphs did not leak into ns-b's queries.
        last_classes = graph_client.query.call_args_list[-2][0][0]
        assert graph_b in last_classes
        assert graph_a not in last_classes

    async def test_a_failed_resolution_is_not_cached(self, tbox_builder, graph_client):
        """Caching a FAILURE is self-amplifying, so only success is cached.

        The fallback is a cluster-wide scan, and cluster-wide scans are what
        overload the graph — so a container that caches one transient 5s timeout
        spends the whole TTL generating the load that causes the next timeout.
        Observed collapsing a BIRD-Interact cell: ``graphs=0``, then ReadTimeouts
        on the join-path and aiContext queries, then ``classes=0`` — requests
        answered with no ontology at all. The retry costs one cheap query.
        """
        graph_iri = "https://ontology-workbench.local/ns-a/induced"
        failing = self._build_sequence([])
        failing[0] = RuntimeError("neptune busy")
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=[*failing, *self._build_sequence([{"g": graph_iri}])])

        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees")
        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees again")

        resolutions = [c[0][0] for c in graph_client.query.call_args_list if "owl:Ontology" in c[0][0]]
        assert len(resolutions) == 2, "the failure was cached — the second build did not retry"
        # And the retry's result is actually used, not just fetched.
        assert f"GRAPH <{graph_iri}> {{" in graph_client.query.call_args_list[-1][0][0]

    async def test_a_stale_cache_entry_is_re_resolved_after_ttl(self, tbox_builder, graph_client, monkeypatch):
        """Past the TTL, a namespace is re-resolved — the branch that catches a
        newly-published graph. Cache-hit and failure-not-cached are covered
        elsewhere; this is the expiry path between them.
        """
        import coa_serve.tier2.ontop.tbox_context as tb

        graph_iri = "https://ontology-workbench.local/ns-a/induced"
        clock = {"t": 1_000.0}
        monkeypatch.setattr(tb.time, "monotonic", lambda: clock["t"])
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(
            side_effect=[*self._build_sequence([{"g": graph_iri}]), *self._build_sequence([{"g": graph_iri}])]
        )

        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees")
        clock["t"] += tb._GRAPH_IRI_CACHE_TTL_S + 1  # let the entry lapse
        await tbox_builder.build([_hit(self._EMP)], "ns-a", query="employees again")

        resolutions = [c[0][0] for c in graph_client.query.call_args_list if "owl:Ontology" in c[0][0]]
        assert len(resolutions) == 2, "the stale entry was not re-resolved after the TTL"

    async def test_resolved_graphs_are_carried_out_on_the_context(self, tbox_builder, graph_client):
        """The build's own queries are not the only ones that need the graphs.

        ``SPARQLValidator`` runs AFTER this build, on the same namespace, and its
        checks are the only graph queries re-paid on every validate-and-retry
        attempt. It cannot resolve them itself without a second resolution query,
        so the build hands them over.
        """
        graph_iri = "https://ontology-workbench.local/ns-x/induced"
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=self._build_sequence([{"g": graph_iri}]))

        ctx = await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        assert ctx.graph_iris == [graph_iri]

    async def test_unresolved_graphs_leave_the_context_empty(self, tbox_builder, graph_client):
        """Empty is the input ``graph_scoped_body`` reads as "use the prefix filter"."""
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=self._build_sequence([]))

        ctx = await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        assert ctx.graph_iris == []

    async def test_truncation_preserves_the_graphs(self, tbox_builder):
        """A prompt budget does not change which graphs exist."""
        from coa_serve.tier2.ontop.tbox_context import TBoxContext

        graph_iri = "https://ontology-workbench.local/ns-x/induced"
        ctx = TBoxContext(
            classes=[{"uri": f"{self._EMP}{i}", "label": f"Emp{i}", "parent": None} for i in range(200)],
            properties=[
                {"uri": f"https://example.org/o#p{i}", "label": f"p{i}", "domain": self._EMP, "range": "xsd:string"}
                for i in range(2000)
            ],
            graph_iris=[graph_iri],
        )

        trimmed = tbox_builder._truncate(ctx, max_tokens=500)

        assert len(trimmed.classes) < len(ctx.classes) or len(trimmed.properties) < len(ctx.properties)
        assert trimmed.graph_iris == [graph_iri]


@pytest.mark.unit
class TestObjectPropertyDomainAnchor:
    """The join-path query is anchored to the classes already in the T-Box.

    Binding ``?g`` was necessary but not sufficient: measured on the deployed
    412-class Spider 2.0 namespace at parallelism 1, the graph-scoped query still
    ReadTimeout at 16.6s, because ``?domain rdfs:label ?domainLabel`` with
    ``?domain`` unbound scans every label in the graph. The builder already knows
    which classes reached the prompt, and a join path leaving a class the writer
    never sees is unusable — so anchoring is both the fix and the right scope.
    """

    _EMP = "https://example.org/o#Emp"
    _DEPT = "https://example.org/o#Dept"

    @classmethod
    def _sequence(cls) -> list[list[dict]]:
        """resolve, count, classes (TWO, so join paths are fetched), props, OP, aiContext."""
        return [
            [{"g": "https://ontology-workbench.local/ns-x/induced"}],
            [{"cnt": "2"}],
            [
                {"class": cls._EMP, "label": "Emp", "parentClass": None},
                {"class": cls._DEPT, "label": "Dept", "parentClass": None},
            ],
            [],
            [],
            [],
        ]

    @staticmethod
    def _op_query(graph_client) -> str:
        matches = [c[0][0] for c in graph_client.query.call_args_list if "owl:ObjectProperty" in c[0][0]]
        assert len(matches) == 1, f"expected one join-path query, got {len(matches)}"
        return matches[0]

    async def test_selected_classes_anchor_the_query(self, tbox_builder, graph_client):
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=self._sequence())

        await tbox_builder.build([_hit(self._EMP), _hit(self._DEPT)], "ns-x", query="employees by dept")

        sparql = self._op_query(graph_client)
        assert f"VALUES ?domain {{ <{self._EMP}> <{self._DEPT}> }}" in sparql
        # Still graph-scoped: the anchor is in addition to, not instead of, the graph.
        assert "GRAPH <" in sparql

    async def test_join_paths_still_reach_the_context(self, tbox_builder, graph_client):
        """The anchor must not filter out the edges between the anchored classes."""
        seq = self._sequence()
        seq[4] = [
            {
                "op": "https://example.org/o#worksIn",
                "opLabel": "works_in",
                "domain": self._EMP,
                "domainLabel": "Emp",
                "range": self._DEPT,
                "rangeLabel": "Dept",
            }
        ]
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=seq)

        ctx = await tbox_builder.build([_hit(self._EMP), _hit(self._DEPT)], "ns-x", query="employees by dept")

        assert [op["label"] for op in ctx.object_properties] == ["works_in"]

    async def test_unsafe_class_uris_are_dropped_not_interpolated(self, tbox_builder, graph_client):
        """Class URIs come from Neptune, but they land inside SPARQL text."""
        seq = self._sequence()
        seq[2] = [
            {"class": self._EMP, "label": "Emp", "parentClass": None},
            {"class": "https://x#A> } INJECTED { <y", "label": "Bad", "parentClass": None},
        ]
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=seq)

        await tbox_builder.build([_hit(self._EMP)], "ns-x", query="employees")

        sparql = self._op_query(graph_client)
        assert "INJECTED" not in sparql
        assert f"VALUES ?domain {{ <{self._EMP}> }}" in sparql

    async def test_a_full_context_tbox_anchors_every_class_not_the_first_fifty(self, tbox_builder, graph_client):
        """The anchor must cover the WHOLE class list the writer will be shown.

        This anchor is a filter over classes already chosen for the prompt, not a
        result set being asked about, so capping it at the generic
        ``_MAX_SPARQL_VALUES_URIS`` (50) does not return fewer rows of the same
        kind — it deletes the join paths of every class past the cut. The
        full-context path routinely exceeds 50: BIRD-Interact's namespace builds a
        175-class T-Box, so 125 classes would reach the writer with no way to join
        them, which is worse than the unanchored query the anchor replaced.
        """
        classes = [{"class": f"https://example.org/o#C{i}", "label": f"c{i}", "parentClass": None} for i in range(175)]
        seq = self._sequence()
        seq[2] = classes
        graph_client.ask = AsyncMock(return_value=True)
        graph_client.query = AsyncMock(side_effect=seq)

        await tbox_builder.build([], "ns-x", query="anything")

        sparql = self._op_query(graph_client)
        for row in classes:
            assert f"<{row['class']}>" in sparql, f"{row['label']} lost its join paths to the anchor cap"


@pytest.mark.unit
class TestIsSafeSparqlUri:
    """`_is_safe_sparql_uri` backs the angle-bracket-interpolation guard used in
    ~9 places in tbox_context, so its rejection behaviour is worth pinning."""

    def test_rejects_a_trailing_newline(self):
        from coa_serve.tier2.ontop.tbox_context import _is_safe_sparql_uri

        # `\Z`, not `$`: `$` would match before the trailing newline and pass.
        assert _is_safe_sparql_uri("https://ontology-workbench.local/ns-x/induced")
        assert not _is_safe_sparql_uri("https://ontology-workbench.local/ns-x/induced\n")

    def test_rejects_an_angle_bracket_breakout(self):
        from coa_serve.tier2.ontop.tbox_context import _is_safe_sparql_uri

        assert not _is_safe_sparql_uri("https://g.local/o> } INJECT {")
