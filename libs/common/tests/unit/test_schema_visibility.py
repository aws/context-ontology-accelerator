# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared schema-visibility flag contract and batch evaluation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_authorization.policy_evaluator import evaluate, evaluate_batch, load_seed_policies
from coa_authorization.schema_visibility import (
    CAN_VIEW_ONTOLOGY,
    CAN_VIEW_SOURCE,
    FULL_VISIBILITY,
    NO_VISIBILITY,
    VIEW_ONTOLOGY_ACTION,
    VIEW_SOURCE_ACTION,
    VIEW_SOURCE_NAMESPACES,
    SchemaVisibility,
    from_authorizer_context,
    from_namespace_list,
    to_authorizer_flags,
    to_namespace_list,
)

pytestmark = pytest.mark.unit


class TestConstants:
    def test_wire_names_are_stable(self) -> None:
        # Producers (authorizer, MCP) and consumers (grants, metric service) read these
        # names off the wire; changing one is a cross-package contract break.
        assert (CAN_VIEW_SOURCE, CAN_VIEW_ONTOLOGY, VIEW_SOURCE_NAMESPACES) == (
            "canViewSource",
            "canViewOntology",
            "viewSourceNamespaces",
        )
        assert (VIEW_SOURCE_ACTION, VIEW_ONTOLOGY_ACTION) == ("viewSource", "viewOntology")


class TestSchemaVisibility:
    def test_frozen(self) -> None:
        vis = SchemaVisibility(source=True, ontology=False)
        with pytest.raises(AttributeError):
            vis.source = False  # type: ignore[misc]

    def test_full(self) -> None:
        assert FULL_VISIBILITY.full is True
        assert SchemaVisibility(source=True, ontology=False).full is False
        assert SchemaVisibility(source=False, ontology=False) == NO_VISIBILITY


class TestToAuthorizerFlags:
    @pytest.mark.parametrize(
        ("vis", "expected"),
        [
            (SchemaVisibility(True, True), {"canViewSource": "true", "canViewOntology": "true"}),
            (SchemaVisibility(True, False), {"canViewSource": "true", "canViewOntology": "false"}),
            (SchemaVisibility(False, True), {"canViewSource": "false", "canViewOntology": "true"}),
            (SchemaVisibility(False, False), {"canViewSource": "false", "canViewOntology": "false"}),
        ],
    )
    def test_serializes_as_strings(self, vis: SchemaVisibility, expected: dict[str, str]) -> None:
        assert to_authorizer_flags(vis) == expected

    @pytest.mark.parametrize("missing", ["full", "blind"])
    @pytest.mark.parametrize("vis", [SchemaVisibility(a, b) for a in (True, False) for b in (True, False)])
    def test_round_trip(self, vis: SchemaVisibility, missing: str) -> None:
        assert from_authorizer_context(to_authorizer_flags(vis), missing=missing) == vis  # type: ignore[arg-type]


class TestFromAuthorizerContext:
    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "tRuE"])
    def test_true_is_case_insensitive(self, value: str) -> None:
        ctx = {CAN_VIEW_SOURCE: value, CAN_VIEW_ONTOLOGY: value}
        assert from_authorizer_context(ctx, missing="blind") == FULL_VISIBILITY

    @pytest.mark.parametrize("value", ["false", "FALSE", "False", "", "1", "yes", "none"])
    def test_anything_else_withholds(self, value: str) -> None:
        ctx = {CAN_VIEW_SOURCE: value, CAN_VIEW_ONTOLOGY: value}
        assert from_authorizer_context(ctx, missing="full") == NO_VISIBILITY

    def test_non_string_values_withhold(self) -> None:
        ctx = {CAN_VIEW_SOURCE: 1, CAN_VIEW_ONTOLOGY: ["true"]}
        assert from_authorizer_context(ctx, missing="full") == NO_VISIBILITY

    @pytest.mark.parametrize("ctx", [None, {}, {"userId": "u"}])
    def test_missing_both_blind_withholds(self, ctx: dict | None) -> None:
        assert from_authorizer_context(ctx, missing="blind") == NO_VISIBILITY

    @pytest.mark.parametrize("ctx", [None, {}, {"userId": "u"}])
    def test_missing_both_full_keeps_full(self, ctx: dict | None) -> None:
        assert from_authorizer_context(ctx, missing="full") == FULL_VISIBILITY

    def test_missing_one_withholds_that_one_under_either_rule(self) -> None:
        ctx = {CAN_VIEW_SOURCE: "true"}
        expected = SchemaVisibility(source=True, ontology=False)
        assert from_authorizer_context(ctx, missing="full") == expected
        assert from_authorizer_context(ctx, missing="blind") == expected


class TestNamespaceList:
    def test_round_trip_skips_empty(self) -> None:
        raw = to_namespace_list(["ns-1", "", "*"])
        assert raw == "ns-1,*"
        assert from_namespace_list({VIEW_SOURCE_NAMESPACES: raw}) == frozenset({"ns-1", "*"})

    @pytest.mark.parametrize("ctx", [None, {}, {VIEW_SOURCE_NAMESPACES: ""}, {VIEW_SOURCE_NAMESPACES: 7}])
    def test_absent_or_empty_is_empty(self, ctx: dict | None) -> None:
        assert from_namespace_list(ctx) == frozenset()


class TestEvaluateBatch:
    _CHECKS = [
        ("viewSource", "Source", "ns-1"),
        ("viewOntology", "Ontology", "ns-1"),
        ("viewSource", "Source", "ns-2"),
        ("viewSource", "Source", "*"),
    ]

    def test_matches_single_evaluation(self) -> None:
        policies = load_seed_policies()
        roles = [{"role": "data-steward", "resourceUID": "ns-1"}, {"role": "data-analyst", "resourceUID": "ns-2"}]
        batch = [r.allowed for r in evaluate_batch(policies, "u", self._CHECKS, [], roles)]
        single = [
            evaluate(
                policies=policies,
                user_id="u",
                action=a,
                resource_type=t,
                resource_id=i,
                global_roles=[],
                resource_roles=roles,
                resource_attrs={"namespace": i},
            ).allowed
            for a, t, i in self._CHECKS
        ]
        assert batch == single == [True, True, False, False]

    def test_platform_viewer_sees_everything(self) -> None:
        results = evaluate_batch(load_seed_policies(), "u", self._CHECKS, ["platform-viewer"], [])
        assert [r.allowed for r in results] == [True, True, True, True]

    def test_empty_checks(self) -> None:
        assert evaluate_batch(load_seed_policies(), "u", []) == []

    def test_malformed_policies_fail_closed(self) -> None:
        results = evaluate_batch("this is not cedar", "u", self._CHECKS, ["platform-admin"], [])
        assert [r.allowed for r in results] == [False] * len(self._CHECKS)

    def test_engine_error_fails_closed(self) -> None:
        with patch("coa_authorization.policy_evaluator.is_authorized_batch", side_effect=RuntimeError("boom")):
            results = evaluate_batch(load_seed_policies(), "u", self._CHECKS, ["platform-admin"], [])
        assert [r.allowed for r in results] == [False] * len(self._CHECKS)

    def test_result_count_mismatch_fails_closed(self) -> None:
        allowed = evaluate_batch(load_seed_policies(), "u", self._CHECKS[:1], ["platform-admin"], [])
        assert allowed[0].allowed is True  # positive control
        with patch("coa_authorization.policy_evaluator.is_authorized_batch", return_value=allowed):
            results = evaluate_batch(load_seed_policies(), "u", self._CHECKS, ["platform-admin"], [])
        assert [r.allowed for r in results] == [False] * len(self._CHECKS)
