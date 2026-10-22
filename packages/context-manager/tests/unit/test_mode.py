# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the mode/composition contract.

The load-bearing property is the *no-op* one: ``resolve_mode`` must reproduce the
original ``_is_agentic_engaged`` precedence exactly, so adopting it cannot move a
benchmark number. ``test_matches_legacy_engagement_predicate`` asserts that against
a transcription of the original logic over the full input cross-product — run over
both the current ``deep-reasoning`` spelling and the deprecated ``agentic`` one, so
the rename is provably behavior-preserving for a caller that never updated.
"""

from __future__ import annotations

import itertools

import pytest
from coa_serve.clients.sources_registry import SourceComposition
from coa_serve.mode import (
    LEGACY_MODE_ALIASES,
    Composition,
    Mode,
    ResolutionPlan,
    build_plan,
    resolve_mode,
)

pytestmark = pytest.mark.unit

# Every spelling of "run the reasoning loop" a caller may send on options.mode.
LOOP_SPELLINGS = ["deep-reasoning", *LEGACY_MODE_ALIASES]


def _legacy_is_agentic_engaged(request_mode, *, retriever_exists, deployment_default_is_loop):
    """Transcription of Orchestrator._is_agentic_engaged (pre-mode-module)."""
    if not retriever_exists:
        return False
    if request_mode in LOOP_SPELLINGS:
        return True
    if request_mode == "standard":
        return False
    return request_mode is None and deployment_default_is_loop


@pytest.mark.parametrize(
    ("request_mode", "available", "deployment_is_loop"),
    itertools.product([*LOOP_SPELLINGS, "standard", None], [True, False], [True, False]),
)
def test_matches_legacy_engagement_predicate(request_mode, available, deployment_is_loop):
    """resolve_mode must be a behavior-preserving replacement for the old predicate."""
    mode, _ = resolve_mode(
        request_mode,
        deployment_default=Mode.DEEP_REASONING if deployment_is_loop else Mode.STANDARD,
        deep_reasoning_available=available,
    )
    expected = _legacy_is_agentic_engaged(
        request_mode,
        retriever_exists=available,
        deployment_default_is_loop=deployment_is_loop,
    )
    assert (mode is Mode.DEEP_REASONING) == expected


@pytest.mark.parametrize("spelling", LOOP_SPELLINGS)
def test_request_overrides_deployment_default_both_directions(spelling):
    loop, src = resolve_mode(spelling, deployment_default=Mode.STANDARD, deep_reasoning_available=True)
    assert (loop, src) == (Mode.DEEP_REASONING, "request")

    standard, src = resolve_mode("standard", deployment_default=Mode.DEEP_REASONING, deep_reasoning_available=True)
    assert (standard, src) == (Mode.STANDARD, "request")


def test_deprecated_agentic_spelling_resolves_to_deep_reasoning():
    """The pre-rename wire value must not silently degrade to standard.

    Fails if the alias is dropped: an unrecognized value falls through to the
    deployment default, which ships as standard — so a caller pinned to "agentic"
    would quietly get the single-shot path instead of an error.
    """
    mode, src = resolve_mode("agentic", deployment_default=Mode.STANDARD, deep_reasoning_available=True)
    assert (mode, src) == (Mode.DEEP_REASONING, "request")


def test_absent_request_uses_deployment_default():
    mode, src = resolve_mode(None, deployment_default=Mode.DEEP_REASONING, deep_reasoning_available=True)
    assert (mode, src) == (Mode.DEEP_REASONING, "deployment")


def test_unrecognized_request_mode_falls_through_without_raising():
    mode, src = resolve_mode("turbo", deployment_default=Mode.STANDARD, deep_reasoning_available=True)
    assert (mode, src) == (Mode.STANDARD, "deployment")


def test_deep_reasoning_unavailable_forces_standard_even_when_requested():
    """Cannot run an engine that was never built."""
    mode, _ = resolve_mode("deep-reasoning", deployment_default=Mode.DEEP_REASONING, deep_reasoning_available=False)
    assert mode is Mode.STANDARD


def test_auto_never_leaks_to_callers():
    """AUTO must collapse to a concrete mode; dispatch never sees it."""
    mode, src = resolve_mode("auto", deployment_default=Mode.STANDARD, deep_reasoning_available=True)
    assert mode is not Mode.AUTO
    assert src == "auto"


def test_auto_falls_back_to_standard_when_deep_reasoning_unavailable():
    mode, _ = resolve_mode("auto", deployment_default=Mode.STANDARD, deep_reasoning_available=False)
    assert mode is Mode.STANDARD


@pytest.mark.parametrize(
    ("structured", "unstructured", "expected"),
    [
        (True, False, Composition.STRUCTURED),
        (False, True, Composition.UNSTRUCTURED),
        (True, True, Composition.MIXED),
        (False, False, Composition.MIXED),
    ],
)
def test_composition_mapping(structured, unstructured, expected):
    got = Composition.from_source_composition(
        SourceComposition(has_structured_source=structured, has_unstructured_source=unstructured)
    )
    assert got is expected


def test_unknown_composition_fails_open_to_mixed():
    """A registry miss must not withhold any capability."""
    got = Composition.from_source_composition(SourceComposition.unknown_composition())
    assert got is Composition.MIXED


def test_build_plan_is_frozen_and_carries_provenance():
    plan = build_plan(
        "deep-reasoning",
        SourceComposition(has_structured_source=False, has_unstructured_source=True),
        deployment_default=Mode.STANDARD,
        deep_reasoning_available=True,
    )
    assert plan == ResolutionPlan(mode=Mode.DEEP_REASONING, composition=Composition.UNSTRUCTURED, mode_source="request")
    with pytest.raises(AttributeError):
        plan.mode = Mode.STANDARD


def test_mode_values_are_wire_stable():
    """These strings are the public API (options.mode); pinning guards a rename."""
    assert (Mode.STANDARD, Mode.DEEP_REASONING, Mode.AUTO) == ("standard", "deep-reasoning", "auto")
    assert LEGACY_MODE_ALIASES == {"agentic": Mode.DEEP_REASONING}


def _smithy_query_mode_values() -> set[str]:
    """Wire values of the Smithy ``QueryMode`` enum, parsed from the source model.

    Parsing the model rather than the generated client proves the source of truth
    agrees with the runtime, not merely that codegen ran.
    """
    import re
    from pathlib import Path

    # tests/unit/ -> tests/ -> context-manager/ -> packages/ -> repo root
    model = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy" / "serve.smithy"
    assert model.is_file(), f"Smithy model not found at {model}"
    match = re.search(r"enum QueryMode \{(.*?)\n\}", model.read_text(), re.DOTALL)
    assert match, "enum QueryMode not found in serve.smithy"
    return set(re.findall(r'=\s*"([^"]+)"', match.group(1)))


def test_smithy_query_mode_matches_accepted_wire_values():
    """The contract must advertise exactly the modes ``InvokeRequest`` accepts.

    Advertising a value serve rejects 400s a contract-conformant caller; accepting a
    value the contract omits leaves it reachable only by callers who read the code.
    ``auto`` is internal (the absent-mode resolution) and is not a wire value.
    """
    from coa_serve.models import InvokeRequest
    from pydantic import ValidationError

    declared = _smithy_query_mode_values()
    assert declared == {Mode.STANDARD.value, Mode.DEEP_REASONING.value, *LEGACY_MODE_ALIASES}
    for value in sorted(declared):
        InvokeRequest(query="q", namespace="ns", options={"mode": value})
    with pytest.raises(ValidationError):
        InvokeRequest(query="q", namespace="ns", options={"mode": Mode.AUTO.value})
