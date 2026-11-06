# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic `<function>_<column>` names for aggregate projections in generated SQL."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

_SQLGLOT_DIALECTS = {"athena": "trino", "postgresql": "postgres"}

_SENTINEL = "NOT_ANSWERABLE_"


def normalize_aggregate_aliases(sql: str, dialect: str) -> str:
    """Return ``sql`` with top-level aggregates aliased ``<function>_<column>``, or unchanged when unsafe."""
    if not sql or _SENTINEL in sql:
        return sql
    sqlglot_dialect = _SQLGLOT_DIALECTS.get(dialect, dialect)
    try:
        statements = sqlglot.parse(sql, read=sqlglot_dialect)
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return sql
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return sql
    select = statements[0]

    renames: dict[str, str] = {}
    projections: list[exp.Expression] = []
    changed = False
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(inner, exp.AggFunc):
            projections.append(projection)
            continue
        name = _alias_name(inner, sqlglot_dialect)
        old = projection.alias if isinstance(projection, exp.Alias) else ""
        if old != name:
            changed = True
            if old:
                renames[old.lower()] = name
        projections.append(exp.alias_(inner.copy(), name))
    if not changed:
        return sql

    output_names = [p.alias_or_name.lower() for p in projections]
    if len(set(output_names)) != len(output_names):
        return sql

    order = select.args.get("order")
    bare_order_keys = [o.this for o in order.expressions if isinstance(o.this, exp.Column)] if order else []
    for column in select.find_all(exp.Column):
        if column.table or column.name.lower() not in renames:
            continue
        # Inside an aggregate the name can only be an input column. A bare
        # ORDER BY key resolves to the output alias, so it follows the rename.
        # Elsewhere (GROUP BY, HAVING, Redshift lateral aliases) engines differ
        # on alias-vs-column resolution, so the rename could change the query.
        if column.find_ancestor(exp.AggFunc) is None and not any(column is key for key in bare_order_keys):
            return sql

    select.set("expressions", projections)
    for key in bare_order_keys:
        if not key.table and key.name.lower() in renames:
            key.replace(exp.column(renames[key.name.lower()]))
    return select.sql(dialect=sqlglot_dialect)


def _alias_name(aggregate: exp.AggFunc, dialect: str) -> str:
    # The rendered name, not sqlglot's expression key: ``APPROX_PERCENTILE``
    # parses to ``ApproxQuantile`` but the model and the prompt rule say
    # ``approx_percentile``.
    function = aggregate.sql(dialect=dialect).split("(", 1)[0].strip().lower()
    column = next(aggregate.find_all(exp.Column), None)
    suffix = column.name.lower() if column is not None else "all"
    return f"{function}_{suffix}"
