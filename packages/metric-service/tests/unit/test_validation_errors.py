# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for validation_errors.format_validation_error."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import pytest
from coa_metrics.api.validation_errors import format_validation_error
from pydantic import BaseModel, Field, ValidationError

pytestmark = pytest.mark.unit


class _SqlDialect(StrEnum):
    POSTGRESQL = "POSTGRESQL"
    TRINO = "TRINO"


class _Dialect(BaseModel):
    dialect: _SqlDialect
    expression: str


class _Expression(BaseModel):
    # min_length mirrors the generated MetricExpression so an empty list raises too_short.
    dialects: Annotated[list[_Dialect], Field(min_length=1)]


class _Metric(BaseModel):
    name: str
    expression: _Expression


def _validation_error(body: dict) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        _Metric.model_validate(body)
    return exc_info.value


class TestFormatValidationError:
    def test_missing_expression_field_gets_shape_hint(self):
        """Flat top-level 'dialects' (no 'expression' wrapper) gets the shape hint."""
        exc = _validation_error({"name": "m1", "dialects": [{"dialect": "TRINO", "expression": "SUM(x)"}]})
        msg = format_validation_error(exc)
        assert "Field required" in msg
        assert "expression" in msg
        assert '"dialects"' in msg
        assert "not valid" in msg or "must be nested" in msg

    def test_expression_wrong_type_gets_shape_hint(self):
        """'expression' present but the wrong shape (a list, not an object)."""
        exc = _validation_error({"name": "m1", "expression": [{"dialect": "TRINO", "expression": "SUM(x)"}]})
        msg = format_validation_error(exc)
        assert "expression" in msg.lower()
        assert "must be nested" in msg

    def test_unrelated_field_error_is_unchanged(self):
        """An error on a field OTHER than 'expression' gets no hint appended."""
        exc = _validation_error({"expression": {"dialects": [{"dialect": "TRINO", "expression": "SUM(x)"}]}})
        msg = format_validation_error(exc)
        assert "Field required" in msg
        assert "must be nested" not in msg

    def test_empty_dialects_list_is_unchanged(self):
        """A structurally-correct but empty dialects list is a different error
        (min_length) — not an 'expression' shape mistake, so no hint."""
        exc = _validation_error({"name": "m1", "expression": {"dialects": []}})
        msg = format_validation_error(exc)
        assert "must be nested" not in msg

    def test_invalid_dialect_enum_athena_gets_trino_hint(self):
        """ATHENA fails the enum check before the handler check; the hint is matched on loc ending in 'dialect'."""
        exc = _validation_error(
            {"name": "m1", "expression": {"dialects": [{"dialect": "ATHENA", "expression": "SUM(x)"}]}}
        )
        msg = format_validation_error(exc)
        assert "TRINO" in msg

    def test_invalid_dialect_enum_unknown_value_is_unchanged(self):
        """A dialect value we have no hint for passes through unchanged."""
        exc = _validation_error(
            {"name": "m1", "expression": {"dialects": [{"dialect": "ORACLE", "expression": "SUM(x)"}]}}
        )
        msg = format_validation_error(exc)
        # Pydantic's enum error already lists TRINO, so assert on the hint instead.
        assert "use TRINO" not in msg
        assert "Athena" not in msg
