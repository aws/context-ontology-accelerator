# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The whole-translation wall clock for the NL->SPARQL route.

Kept in its own module rather than appended to ``test_tier2_nl_to_sparql.py``:
that file's tail is where every pending change to this route adds its class, so a
new class there is a merge conflict with no semantic content.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.nl_to_sparql import (
    _DEFAULT_TIMEOUT_S,
    _MAX_TIMEOUT_S,
    _MIN_TIMEOUT_S,
    NLtoSPARQL,
)

pytestmark = pytest.mark.unit


class TestTranslationTimeoutBudget:
    """``SERVE_VKG_TRANSLATION_TIMEOUT_S``, the whole-translation wall clock.

    It bounds the T-Box build, the LLM call, validation and every
    validate-and-retry attempt together, so on a wide namespace it stops being a
    backstop and becomes the binding constraint: measured over 727 BIRD-Interact
    translations, p50 29.1s / p90 44.7s / max 59.6s against a 60s cap, with 6.9%
    already returning ``translation_timed_out`` — and that was on a T-Box
    truncated to ~66 of its 175 classes. Completing the T-Box lengthens the
    prompt, so an unraisable 60s would charge a strictly better T-Box with a worse
    answer rate. Hence a knob, with the shipped default untouched.
    """

    def _translator(self, **kw) -> NLtoSPARQL:
        return NLtoSPARQL(graph_client=AsyncMock(), llm_client=AsyncMock(), **kw)

    def test_unset_keeps_the_shipped_default(self, monkeypatch) -> None:
        monkeypatch.delenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", raising=False)

        assert self._translator()._timeout_s == _DEFAULT_TIMEOUT_S

    def test_the_env_var_raises_the_budget(self, monkeypatch) -> None:
        monkeypatch.setenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", "300")

        assert self._translator()._timeout_s == 300.0

    def test_it_is_resolved_per_instance_not_frozen_at_import(self, monkeypatch) -> None:
        """A default argument would bind the env value at import time instead."""
        monkeypatch.setenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", "120")
        first = self._translator()._timeout_s
        monkeypatch.setenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", "240")

        assert (first, self._translator()._timeout_s) == (120.0, 240.0)

    def test_an_explicit_argument_still_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", "300")

        assert self._translator(timeout_s=45.0)._timeout_s == 45.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", _MIN_TIMEOUT_S), ("6000", _MAX_TIMEOUT_S), ("", _DEFAULT_TIMEOUT_S), ("abc", _DEFAULT_TIMEOUT_S)],
    )
    def test_a_bad_or_out_of_range_value_degrades_instead_of_raising(
        self, monkeypatch, raw: str, expected: float
    ) -> None:
        """A misconfigured knob must not take the route down."""
        monkeypatch.setenv("SERVE_VKG_TRANSLATION_TIMEOUT_S", raw)

        assert self._translator()._timeout_s == expected
