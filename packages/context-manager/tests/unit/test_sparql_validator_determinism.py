# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPARQLValidator Stage-4 projection-alias determinism check (B1b).

Stage 4 fails-closed on the deterministically-detectable half of the NL→SPARQL
non-determinism defect (fashion-findings B1b): an aggregate projection that
carries NO stable, descriptive alias. The SAME question then returns columns
named ``?productCount`` on one run and ``?count`` (or an engine-invented label)
on the next, which breaks typed clients with a KeyError ~1 run in 3.

These tests exercise the check in isolation via the private
``_validate_projection_aliases`` (no Neptune round-trip needed — the check is
pure text analysis), plus a set of NEGATIVE CONTROLS proving it does NOT
over-block legitimate multi-aggregate projections (the prompt's own SUM(IF(...))
ratio/difference patterns). A check that cannot pass a valid query is as useless
as one that cannot fail an invalid one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator

pytestmark = pytest.mark.unit


@pytest.fixture
def validator():
    # Stage 4 makes no graph calls; a bare AsyncMock suffices.
    return SPARQLValidator(
        graph_client=AsyncMock(),
        graph_uri_template="https://ontology-workbench.local/{namespace}",
    )


# ── Queries that MUST PASS (negative controls: legitimate, deterministic) ──────
PASSING = {
    "no_aggregate_plain_vars": "SELECT ?colorName ?productName WHERE { ?p a :Product }",
    "descriptive_count": "SELECT ?colorName (COUNT(DISTINCT ?product) AS ?productCount) WHERE { ?p a :Product }",
    "descriptive_sum": "SELECT (SUM(?amount) AS ?totalRevenue) WHERE { ?o :amount ?amount }",
    # COUNT(?total) is fine here: ?total is NOT a typed class instance, so the
    # entity-count-DISTINCT rule does not apply. (Counting a literal-valued /
    # untyped variable is a row/occurrence count, which is a legitimate choice.)
    "total_alias_untyped": "SELECT (COUNT(?p) AS ?total) WHERE { ?p :listedIn ?cat }",
    # The prompt's recommended SUM(IF(...)) comparison patterns — one alias may
    # legitimately cover a projection expression containing several aggregates.
    "ratio_of_two_sums": (
        "SELECT (xsd:decimal(SUM(IF(?t = 'credit', 1, 0))) "
        "/ SUM(IF(?t = 'debit', 1, 0)) AS ?ratio) WHERE { ?x :type ?t }"
    ),
    "difference_of_two_sums": (
        "SELECT (SUM(IF(?c = 'wet', 1, 0)) - SUM(IF(?c = 'dry', 1, 0)) AS ?difference) WHERE { ?x :condition ?c }"
    ),
    "distinct_form": ("SELECT DISTINCT ?colorName (COUNT(?p) AS ?productCount) WHERE { ?p :color ?colorName }"),
}

# ── Queries that MUST FAIL (the B1b defect shapes) ─────────────────────────────
FAILING = {
    "aggregate_no_alias": "SELECT (COUNT(?product)) WHERE { ?product a :Product }",
    "bare_alias_count": "SELECT ?colorName (COUNT(?product) AS ?count) WHERE { ?product :color ?colorName }",
    "bare_alias_value": "SELECT (SUM(?amount) AS ?value) WHERE { ?o :amount ?amount }",
    "bare_alias_result": "SELECT (AVG(?p) AS ?result) WHERE { ?x :price ?p }",
    "bare_alias_n": "SELECT (COUNT(?x) AS ?n) WHERE { ?x a :Product }",
    # WHERE is grammatically optional in SPARQL; a WHERE-less query must not
    # bypass the check (rdflib accepts it, so Stage 1 is not a backstop).
    "where_less_bare_alias": "SELECT (COUNT(?p) AS ?count) { ?p a :Product }",
    "where_less_no_alias": "SELECT (COUNT(?p)) { ?p a :Product }",
}


@pytest.mark.unit
class TestProjectionAliasDeterminism:
    """Stage 4 rejects unstable aggregate projections, passes stable ones."""

    @pytest.mark.parametrize("name", list(PASSING))
    def test_passes_deterministic_projection(self, validator, name):
        result = validator._validate_projection_aliases(PASSING[name])
        assert result.valid, f"{name} should PASS but failed: {result.error}"

    @pytest.mark.parametrize("name", list(FAILING))
    def test_rejects_unstable_projection(self, validator, name):
        result = validator._validate_projection_aliases(FAILING[name])
        assert not result.valid, f"{name} should FAIL but passed"
        assert result.error  # a non-empty, actionable message is fed back to the LLM

    def test_missing_alias_message_is_actionable(self, validator):
        result = validator._validate_projection_aliases(FAILING["aggregate_no_alias"])
        assert "alias" in (result.error or "").lower()
        assert "AS ?" in (result.error or "")

    def test_bare_alias_message_names_the_offender(self, validator):
        result = validator._validate_projection_aliases(FAILING["bare_alias_count"])
        assert "?count" in (result.error or "")

    def test_non_select_passes_conservatively(self, validator):
        # ASK / malformed / non-SELECT must not be false-rejected here;
        # Stage 1 (rdflib) owns true syntax errors.
        assert validator._validate_projection_aliases("ASK { ?s ?p ?o }").valid
        assert validator._validate_projection_aliases("not sparql at all").valid


@pytest.mark.unit
class TestProjectionAliasThroughFullValidate:
    """The check must be reached through the public validate() pipeline, so a
    real generation actually benefits — not just a unit-level private call."""

    async def test_validate_rejects_bare_alias_end_to_end(self, validator):
        # Graph mock: make Stages 1-3 pass (syntactically valid SPARQL; URIs
        # 'found'; no domain/range conflict) so the failure is attributable to
        # Stage 4 specifically.
        validator._graph.query = AsyncMock(return_value=[{"found": "1"}])
        validator._graph.ask = AsyncMock(return_value=True)
        sparql = (
            "PREFIX : <http://example.org/ont#> "
            "SELECT ?colorName (COUNT(?product) AS ?count) "
            "WHERE { ?product :color ?colorName }"
        )
        result = await validator.validate(sparql, "test-ns")
        assert not result.valid
        assert "?count" in (result.error or "")

    async def test_validate_passes_descriptive_alias_end_to_end(self, validator):
        validator._graph.query = AsyncMock(return_value=[{"found": "1"}])
        validator._graph.ask = AsyncMock(return_value=True)
        sparql = (
            "PREFIX : <http://example.org/ont#> "
            "SELECT ?colorName (COUNT(DISTINCT ?product) AS ?productCount) "
            "WHERE { ?product :color ?colorName }"
        )
        result = await validator.validate(sparql, "test-ns")
        assert result.valid, result.error


# ── Entity-count determinism (B1a): COUNT(?entity) must be DISTINCT ────────────
# Measured live N=10 on the HAVING/GROUP-BY family: the SAME question produced
# COUNT(?claim) on 8 runs and COUNT(DISTINCT ?claim) on 2, where ?claim was bound
# via `?claim a ind:Claims`. The answer matched only because the test dataset had
# no duplicate rows per group; on 1-to-many data the plain COUNT inflates and the
# answer flips run-to-run. This is decidable from the SPARQL text alone.
ENTITY_COUNT_FAILING = {
    # ?claim / ?c is a typed class instance -> plain COUNT must be DISTINCT.
    "grouped_entity_count": (
        "SELECT ?status (COUNT(?claim) AS ?claimCount) "
        "WHERE { ?claim a ind:Claims ; ind:claims_status ?status } "
        "GROUP BY ?status HAVING(COUNT(?claim) > 1)"
    ),
    "grouped_entity_count_short_prefix": (
        "SELECT ?status (COUNT(?c) AS ?claimCount) WHERE { ?c a :Claims ; :status ?status } GROUP BY ?status"
    ),
    "rdf_type_long_form": "SELECT (COUNT(?p) AS ?peopleCount) WHERE { ?p rdf:type <http://x/Person> }",
    "typed_via_iri": "SELECT (COUNT(?p) AS ?productCount) WHERE { ?p a <http://x/Product> }",
}

ENTITY_COUNT_PASSING = {
    # The deterministic fix: DISTINCT on the typed entity.
    "grouped_entity_count_distinct": (
        "SELECT ?status (COUNT(DISTINCT ?claim) AS ?claimCount) "
        "WHERE { ?claim a ind:Claims ; ind:claims_status ?status } "
        "GROUP BY ?status HAVING(COUNT(DISTINCT ?claim) > 1)"
    ),
    # Counting a LITERAL-valued (untyped) variable is a legitimate row/value count.
    "count_literal_value": (
        "SELECT (COUNT(DISTINCT ?status) AS ?statusCount) WHERE { ?c a ind:Claims ; ind:claims_status ?status }"
    ),
    "count_untyped_object": ("SELECT (COUNT(?order) AS ?orderCount) WHERE { ?c a :Customer ; :placed ?order }"),
    # COUNT(*) is not a variable count.
    "count_star": "SELECT (COUNT(*) AS ?rowCount) WHERE { ?c a :Claims }",
}


@pytest.mark.unit
class TestEntityCountDeterminism:
    """Stage 4 rejects plain COUNT(?entity) over a typed class instance and steers
    to COUNT(DISTINCT ?entity); leaves literal/untyped counts and COUNT(*) alone."""

    @pytest.mark.parametrize("name", list(ENTITY_COUNT_FAILING))
    def test_rejects_plain_count_of_typed_entity(self, validator, name):
        result = validator._validate_projection_aliases(ENTITY_COUNT_FAILING[name])
        assert not result.valid, f"{name} should FAIL (plain COUNT of typed entity)"
        assert "DISTINCT" in (result.error or ""), "message must steer to COUNT(DISTINCT ...)"

    @pytest.mark.parametrize("name", list(ENTITY_COUNT_PASSING))
    def test_passes_distinct_or_non_entity_count(self, validator, name):
        result = validator._validate_projection_aliases(ENTITY_COUNT_PASSING[name])
        assert result.valid, f"{name} should PASS but failed: {result.error}"

    async def test_rejects_entity_count_end_to_end(self, validator):
        # Reached through the public validate() pipeline, not just the private call.
        validator._graph.query = AsyncMock(return_value=[{"found": "1"}])
        validator._graph.ask = AsyncMock(return_value=True)
        sparql = (
            "PREFIX ind: <http://example.org/ont#> "
            "SELECT ?status (COUNT(?claim) AS ?claimCount) "
            "WHERE { ?claim a ind:Claims ; ind:status ?status } GROUP BY ?status"
        )
        result = await validator.validate(sparql, "test-ns")
        assert not result.valid
        assert "DISTINCT" in (result.error or "")

    def test_non_vacuous_guard_would_pass_without_type(self, validator):
        # Control: the SAME COUNT(?claim) WITHOUT the `a :Class` typing must PASS —
        # proving the rejection is driven by the entity-typing signal, not by the
        # mere presence of COUNT. (If this failed, the rule would be over-blocking.)
        sparql = "SELECT ?status (COUNT(?claim) AS ?claimCount) WHERE { ?claim ind:hasStatus ?status } GROUP BY ?status"
        assert validator._validate_projection_aliases(sparql).valid


# ── Bot-finding regressions (MR !1102 review) ────────────────────────────────


class TestTopLevelParenGroups:
    """Isolated tests for the paren-parsing helper that decides accept/reject.

    (CRITICAL bot finding: this accept/reject-controlling helper had zero
    dedicated unit coverage.)
    """

    def test_flat_single_group(self, validator):
        groups, spans = validator._top_level_paren_groups("(COUNT(?p) AS ?n)")
        assert groups == ["COUNT(?p) AS ?n"]
        assert spans == [(0, 17)]

    def test_two_top_level_groups(self, validator):
        groups, _ = validator._top_level_paren_groups("(SUM(?a) AS ?x) (AVG(?b) AS ?y)")
        assert groups == ["SUM(?a) AS ?x", "AVG(?b) AS ?y"]

    def test_deeply_nested_stays_one_group(self, validator):
        # 3+ levels of nesting must collapse into a single top-level group.
        text = "(IF(BOUND(COALESCE(?a, ?b)), 1, 0) AS ?flag)"
        groups, _ = validator._top_level_paren_groups(text)
        assert len(groups) == 1
        assert groups[0] == "IF(BOUND(COALESCE(?a, ?b)), 1, 0) AS ?flag"

    def test_empty_group(self, validator):
        groups, spans = validator._top_level_paren_groups("()")
        assert groups == [""]
        assert spans == [(0, 2)]

    def test_unbalanced_extra_open_yields_no_closed_group(self, validator):
        # A dangling outer '(' never closes → no complete top-level group is
        # emitted (the inner '(?p)' is nested at depth 2, not a top-level close).
        # Must not raise, must not fabricate a group spanning to EOF.
        groups, spans = validator._top_level_paren_groups("(COUNT(?p)")
        assert groups == []
        assert spans == []

    def test_unbalanced_extra_close_ignored(self, validator):
        # A stray ')' with depth 0 is ignored rather than crashing.
        groups, _ = validator._top_level_paren_groups("(SUM(?a) AS ?x))")
        assert groups == ["SUM(?a) AS ?x"]

    def test_no_parens(self, validator):
        groups, spans = validator._top_level_paren_groups("?a ?b ?c")
        assert groups == []
        assert spans == []


class TestMaskStringLiterals:
    """The SELECT-boundary regex must not be fooled by braces/keywords inside
    string literals (MEDIUM bot finding)."""

    def test_mask_is_length_preserving(self, validator):
        src = 'BIND(CONCAT("{a}", ?x) AS ?y)'
        masked = validator._mask_string_literals(src)
        assert len(masked) == len(src)

    def test_mask_blanks_interior_keeps_quotes(self, validator):
        masked = validator._mask_string_literals('"{WHERE}"')
        assert masked == '"       "'  # 7 interior chars blanked, quotes kept
        assert "{" not in masked and "WHERE" not in masked

    def test_select_with_embedded_brace_in_bind_not_truncated(self, validator):
        # A '{' inside a BIND string literal in the projection, PLUS a real WHERE.
        # Must still validate correctly (the bad aggregate below must be rejected,
        # proving the projection was captured past the embedded brace, not cut at it).
        sparql = 'SELECT (CONCAT("{", ?x) AS ?label) (COUNT(?p) AS ?count) WHERE { ?p a :Product }'
        # ?count is a bare drift-prone alias → must be rejected. If the mask failed
        # and the clause was truncated at the embedded '{', the COUNT would be
        # missed and this would wrongly PASS.
        result = validator._validate_projection_aliases(sparql)
        assert not result.valid

    def test_embedded_brace_does_not_break_valid_query(self, validator):
        sparql = 'SELECT (CONCAT("{x}", ?x) AS ?label) WHERE { ?p a :Product }'
        assert validator._validate_projection_aliases(sparql).valid


class TestTypedBlankNodeSubject:
    """Entity-count-DISTINCT must also catch typed blank-node subjects (MEDIUM)."""

    def test_typed_blank_node_count_rejected(self, validator):
        sparql = "SELECT (COUNT(?claim) AS ?claimCount) WHERE { ?claim a :Claim . _:b1 a :Claim }"
        # ?claim is typed → plain COUNT rejected regardless; ensure blank-node
        # typing is also recognized as an entity typing.
        assert not validator._validate_entity_count_distinct(sparql).valid

    def test_blank_node_typing_recognized(self, validator):
        # Only a blank node is typed; a plain COUNT over a var bound to it via the
        # blank node label is unusual, but the regex must at least SEE the typing
        # without crashing and without corrupting the var set.
        sparql = "SELECT (COUNT(DISTINCT ?x) AS ?xCount) WHERE { _:b1 a :Thing }"
        # COUNT is DISTINCT → passes; the point is no crash on blank-node typing.
        assert validator._validate_entity_count_distinct(sparql).valid
