# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The whole-translation wall clock for the NL->SPARQL route."""

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
    validate-and-retry attempt together; the shipped default is unchanged.
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
