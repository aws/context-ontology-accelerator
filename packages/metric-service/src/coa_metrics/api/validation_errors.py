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
- ``label`` length, type and @pattern failures get a message that names the
  field; Pydantic's defaults do not, and the pattern one is the raw regex.
"""

from __future__ import annotations

from pydantic import ValidationError

from coa_metrics.api.contract import METRIC_LABEL_MAX_LENGTH, METRIC_LABEL_MIN_LENGTH, label_character_message
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

    # Pydantic's length/type/pattern messages for the label do not name it.
    # Never echo the value.
    if loc == ("label",):
        return _label_message(first.get("type", ""), first.get("input"))

    return msg


def _label_message(error_type: str, value: object) -> str:
    if error_type in ("string_too_short", "string_too_long"):
        return f"label must be {METRIC_LABEL_MIN_LENGTH}-{METRIC_LABEL_MAX_LENGTH} characters"
    if error_type == "value_error":
        # The @pattern rejects '<', '>', C0 controls and DEL.
        rejected = None
        if isinstance(value, str):
            rejected = next((ord(c) for c in value if c in "<>" or ord(c) < 0x20 or ord(c) == 0x7F), None)
        return label_character_message("label", rejected)
    if error_type == "string_unicode":
        # A lone surrogate (e.g. "\ud800" in the JSON body) is a string that is
        # not valid Unicode; it breaks the character rule, not the type rule.
        return label_character_message("label")
    return "label must be a string"
