# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared formatting for Pydantic ValidationError -> API 400 message.

Known friction points, all surfaced by adding hints to Pydantic's
default (unhelpful) validation messages rather than changing the request
models:

- The metric request models require a NESTED expression
  shape (``expression: { dialects: [{ dialect, expression }] }``), not a flat
  top-level ``dialects`` array. Pydantic's default message for that mistake
  is the generic ``"Field required"`` (missing ``expression`` key) or
  ``"Input should be a valid dictionary or instance of MetricExpression"``
  (``expression`` present but wrong shape) — neither hints at the correct
  shape, so callers have to read the Smithy-generated model to discover it.
- ``SqlDialect`` has no ``ATHENA`` value (and per team
  decision will not get one — Athena's engine roadmap may drop its current
  Trino backend, so aliasing ``ATHENA`` -> ``TRINO`` would become wrong).
  Pydantic's enum-validation error for an unrecognized ``dialect`` value
  doesn't say what execution engines map to which dialect today. The
  generated ``dialect: SqlDialect`` enum rejects it BEFORE the handler-level
  ``VALID_SQL_DIALECTS`` check ever runs, so the hint has to live here too.
- A ``dataSourceId`` outside the ``DataSourceId`` @pattern fails with the
  generated "must validate the regular expression" text, which does not name
  the field; it gets the same message ``check_source_approved`` returns.
"""

from __future__ import annotations

from pydantic import ValidationError

from coa_metrics.constants import dialect_hint
from coa_metrics.source_status import DATA_SOURCE_ID_FORMAT_MESSAGE

_EXPRESSION_SHAPE_HINT = (
    'Expected shape: "expression": {"dialects": [{"dialect": <SqlDialect>, '
    '"expression": <SQL>}]} — a top-level "dialects" array is not valid; '
    'it must be nested under "expression".'
)


def format_validation_error(exc: ValidationError) -> str:
    """Return a 400 message for the first Pydantic validation error.

    Appends a targeted hint for the two known friction points where the
    payload shape is commonly mistaken.
    """
    errors = exc.errors()
    if not errors:
        return str(exc)

    first = errors[0]
    msg = first.get("msg", str(exc))
    loc = first.get("loc", ())
    if not loc:
        return msg

    # Check the more specific "invalid dialect enum" case FIRST: its loc also
    # starts with "expression" (e.g. ('expression', 'dialects', 0, 'dialect')),
    # so it must not be shadowed by the top-level expression-shape check below.
    if first.get("type") == "enum" and loc[-1] == "dialect":
        hint = dialect_hint(str(first.get("input", "")))
        if hint:
            return f"{msg} ({hint})"
        return msg

    if loc == ("expression",):
        return f"{msg}. {_EXPRESSION_SHAPE_HINT}"

    # The generated @pattern error is the raw regex and does not name the field.
    if loc == ("dataSourceId",) and first.get("type") == "value_error":
        return DATA_SOURCE_ID_FORMAT_MESSAGE

    return msg
