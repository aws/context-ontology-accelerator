# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Soft validation — shared by create and update handlers.

Runs Checks 1-6 from the Metric Onboarding Service LLD §5.1.
SQL findings are returned for diagnostic parity with the explicit validate
endpoint. Persistence handlers independently enforce syntax, read-only
semantics, and executable statement shape before calling this best-effort
advisory path. Catalog and ontology lookup failures therefore cannot discard
the deterministic SQL checks.
"""

from __future__ import annotations

from typing import Any, cast

import structlog

from coa_metrics.lookups import LOOKUP_NOT_PROVIDED, DataSourceLookup, LookupArgument

logger = structlog.get_logger(__name__)


def validate_soft(
    metric_body: dict[str, Any],
    namespace: str,
    *,
    data_source_lookup: LookupArgument = LOOKUP_NOT_PROVIDED,
) -> list[dict[str, str]]:
    """Run soft validation checks that produce warnings but don't block creation/update.

    Args:
        metric_body: Dict with keys: expression.dialects, dataSourceId, sourceTable, ontologyConcepts.
        namespace: The namespace context.
        data_source_lookup: Optional request-scoped catalog lookup. When
            omitted, soft validation builds its own lookup.

    Returns:
        List of warning dicts with field, message, severity keys.
    """
    try:
        from coa_metrics.data_source_lookup_factory import build_data_source_lookup
        from coa_metrics.lookups import NeptuneOntologyLookup
        from coa_metrics.validator import validate_metric
    except ImportError as exc:
        logger.warning("validator_import_failed", error=str(exc))
        return []

    resolved_data_source_lookup = None
    ontology_lookup = None

    if data_source_lookup is LOOKUP_NOT_PROVIDED:
        try:
            resolved_data_source_lookup = build_data_source_lookup(namespace)
        except Exception as exc:
            logger.warning("data_source_lookup_init_failed", error=str(exc))
    else:
        resolved_data_source_lookup = cast("DataSourceLookup | None", data_source_lookup)

    try:
        ontology_lookup = NeptuneOntologyLookup()
    except Exception as exc:
        logger.warning("ontology_lookup_init_failed", error=str(exc))

    try:
        result = validate_metric(
            metric_body=metric_body,
            data_sources_lookup=resolved_data_source_lookup,
            ontology_lookup=ontology_lookup,
            namespace=namespace,
        )
        warnings: list[dict[str, str]] = []
        for err in result.errors:
            warnings.append({"field": err.get("check", "sql_syntax"), "message": err["message"], "severity": "ERROR"})
        for warn in result.warnings:
            warnings.append(
                {"field": warn.get("check", "validation"), "message": warn["message"], "severity": "WARNING"}
            )
        return warnings
    except Exception as exc:
        logger.warning("validation_failed_non_blocking", error=str(exc))
        return []
