# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request bounds from ``metric-service.smithy`` for members read without a generated model.

CreateMetric, UpdateMetric and ValidateMetric bodies are validated by their
Smithy-generated Pydantic models, which carry the model's constraints. Every
other member — path labels, query parameters, and the bodies parsed as raw JSON
(ImportOsi, GetOsiUploadUrl, BulkDeleteMetrics) — is checked here against the
same bounds. ``tests/unit/test_contract_bounds.py`` asserts each constant equals
the trait in the model, so the two cannot drift.

Every check returns ``None`` when valid, or a 400 message that names the member
and never echoes the rejected value.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coa_metrics.neptune_client import MetricDefinition

# MetricName (path label `name`, BulkDeleteMetrics.names / ExportOsi.names members).
# Only the length is enforced on lookups by name: metrics imported from OSI before
# the name pattern was applied may not match it, and must stay reachable (and
# deletable) by name.
METRIC_NAME_MIN_LENGTH = 1
METRIC_NAME_MAX_LENGTH = 128

# ListMetrics
LIST_METRICS_MAX_RESULTS_MIN = 1
LIST_METRICS_MAX_RESULTS_MAX = 200
# PaginationToken (common.smithy)
PAGINATION_TOKEN_MAX_LENGTH = 8192
# DataSourceId
DATA_SOURCE_ID_MAX_LENGTH = 128
DATA_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# SourceTableName
SOURCE_TABLE_MIN_LENGTH = 1
SOURCE_TABLE_MAX_LENGTH = 256

# GetImportJob.jobId
JOB_ID_MIN_LENGTH = 1
JOB_ID_MAX_LENGTH = 128

# GetOsiUploadUrl.filename
FILENAME_MIN_LENGTH = 1
FILENAME_MAX_LENGTH = 255

# ImportOsi
IMPORT_CONTENT_MAX_LENGTH = 5242880
IMPORT_S3_KEY_MIN_LENGTH = 1
IMPORT_S3_KEY_MAX_LENGTH = 1024

# BulkDeleteMetrics
BULK_DELETE_NAMES_MAX_ITEMS = 2000
BULK_DELETE_FILTER_DATA_SOURCE_ID_MAX_LENGTH = 128
BULK_DELETE_FILTER_SOURCE_TABLE_MAX_LENGTH = 256
BULK_DELETE_FILTER_NAME_PREFIX_MAX_LENGTH = 128

# ExportOsi.names
EXPORT_NAMES_MAX_ITEMS = 512

# CreateMetric / UpdateMetric body members. The generated models enforce these
# on those operations; ImportOsi builds definitions from parsed YAML instead, so
# :func:`check_metric_definition` applies the same bounds to each imported metric.
METRIC_DESCRIPTION_MAX_LENGTH = 10000
METRIC_UNIT_MAX_LENGTH = 1024
METRIC_RETURN_TYPE_MAX_LENGTH = 1024
# OntologyConceptList (list bound on the operation member, member bound on the list)
ONTOLOGY_CONCEPTS_MAX_ITEMS = 1000
ONTOLOGY_CONCEPT_MAX_LENGTH = 2048
# MetricAiContext
AI_CONTEXT_SYNONYMS_MAX_ITEMS = 1000
AI_CONTEXT_SYNONYM_MAX_LENGTH = 2048
AI_CONTEXT_INSTRUCTIONS_MAX_LENGTH = 10000
AI_CONTEXT_EXAMPLES_MAX_ITEMS = 1000
AI_CONTEXT_EXAMPLE_MAX_LENGTH = 4096


def check_string(value: Any, field: str, *, min_length: int = 0, max_length: int) -> str | None:
    """Type and length check for a string member."""
    if not isinstance(value, str):
        return f"{field} must be a string"
    if not min_length <= len(value) <= max_length:
        if min_length:
            return f"{field} must be {min_length}-{max_length} characters"
        return f"{field} must be at most {max_length} characters"
    return None


def check_metric_name(value: Any, field: str = "name") -> str | None:
    """Length check for a metric name (see the note on METRIC_NAME_MAX_LENGTH)."""
    return check_string(value, field, min_length=METRIC_NAME_MIN_LENGTH, max_length=METRIC_NAME_MAX_LENGTH)


def check_metric_name_list(value: Any, field: str, *, max_items: int) -> str | None:
    """A list of at most ``max_items`` metric names."""
    if not isinstance(value, list):
        return f"{field} must be a list of metric names"
    if len(value) > max_items:
        return f"{field} must contain at most {max_items} items"
    for item in value:
        error = check_metric_name(item, f"{field} entries")
        if error:
            return error
    return None


def check_data_source_id(value: Any, field: str = "dataSourceId") -> str | None:
    """DataSourceId: 1-128 characters of letters, digits, '.', '_' or '-'."""
    error = check_string(value, field, min_length=1, max_length=DATA_SOURCE_ID_MAX_LENGTH)
    if error:
        return error
    if not DATA_SOURCE_ID_PATTERN.fullmatch(value):
        return f"{field} must contain only letters, digits, '.', '_', or '-'"
    return None


def check_string_list(value: Any, field: str, *, max_items: int, max_member_length: int) -> str | None:
    """A list of at most ``max_items`` strings, each at most ``max_member_length`` characters."""
    if not isinstance(value, list):
        return f"{field} must be a list of strings"
    if len(value) > max_items:
        return f"{field} must contain at most {max_items} items"
    for item in value:
        error = check_string(item, f"{field} entries", max_length=max_member_length)
        if error:
            return error
    return None


def check_metric_definition(metric: MetricDefinition) -> str | None:
    """The CreateMetric bounds for a definition built outside the generated models (OSI import).

    A metric stored outside these bounds cannot be fetched, updated or deleted
    through the API afterwards, so import rejects it per metric instead.
    ``dataSourceId`` is not repeated here: import checks it with the source
    approval (``coa_metrics.source_status``).
    """
    if (error := check_metric_name(metric.name)) is not None:
        return error
    checks = [
        check_string(metric.description, "description", max_length=METRIC_DESCRIPTION_MAX_LENGTH),
        check_string(
            metric.source_table,
            "sourceTable",
            min_length=SOURCE_TABLE_MIN_LENGTH,
            max_length=SOURCE_TABLE_MAX_LENGTH,
        ),
        None if metric.unit is None else check_string(metric.unit, "unit", max_length=METRIC_UNIT_MAX_LENGTH),
        None
        if metric.return_type is None
        else check_string(metric.return_type, "returnType", max_length=METRIC_RETURN_TYPE_MAX_LENGTH),
        check_string_list(
            metric.ontology_concepts,
            "ontologyConcepts",
            max_items=ONTOLOGY_CONCEPTS_MAX_ITEMS,
            max_member_length=ONTOLOGY_CONCEPT_MAX_LENGTH,
        ),
    ]
    ai = metric.ai_context
    if ai is not None:
        checks += [
            check_string_list(
                ai.synonyms,
                "aiContext.synonyms",
                max_items=AI_CONTEXT_SYNONYMS_MAX_ITEMS,
                max_member_length=AI_CONTEXT_SYNONYM_MAX_LENGTH,
            ),
            check_string(ai.instructions, "aiContext.instructions", max_length=AI_CONTEXT_INSTRUCTIONS_MAX_LENGTH),
            check_string_list(
                ai.examples,
                "aiContext.examples",
                max_items=AI_CONTEXT_EXAMPLES_MAX_ITEMS,
                max_member_length=AI_CONTEXT_EXAMPLE_MAX_LENGTH,
            ),
        ]
    return next((error for error in checks if error is not None), None)
