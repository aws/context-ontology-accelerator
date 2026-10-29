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

``MetricLabel`` is the exception to "the generated model is enough": its
Unicode rules cannot be written as a ``@pattern`` every generated validator
understands, so :func:`check_metric_label` is called on every write path:
CreateMetric, UpdateMetric, ValidateMetric, :func:`check_metric_definition`
(OSI import) and the import worker's plan-payload parser, which re-checks the
label when it replays a stored v3 plan. A stricter rule therefore also applies
to plans already in flight.
"""

from __future__ import annotations

import re
import unicodedata
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

# MetricLabel. The model's @length/@pattern cover the length and the ASCII
# rejections, except a trailing LF: the generated validator uses re.match with
# "$", which matches just before a final "\n", so "abc\n" passes the model and
# check_metric_label rejects it. check_metric_label also adds the Unicode rules
# that a regex cannot express for every generated validator (the Python models
# use ``re``, which has no ``\p{...}``).
METRIC_LABEL_MIN_LENGTH = 1
METRIC_LABEL_MAX_LENGTH = 256
# The MetricLabel @pattern body: no angle brackets, no C0 controls, no DEL.
METRIC_LABEL_PATTERN = re.compile(r"[^<>\x00-\x1F\x7F]+")

# Format (Cf) characters a label must not contain: every Cf character that is
# Default_Ignorable_Code_Point (so renderers hide it), except ZWJ, ZWNJ, LRM,
# RLM and ALM, which other namespace languages need; plus the interlinear
# annotation characters, which some renderers also hide. Listed by code point,
# not by category, so a Python upgrade cannot change the set. The same list is
# in the MetricLabel doc in metric-service.smithy and in metrics.md.
_LABEL_REJECTED_FORMAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR
    (0x200B, 0x200B),  # ZERO WIDTH SPACE
    (0x202A, 0x202E),  # bidi embeddings and overrides: LRE, RLE, PDF, LRO, RLO
    (0x2060, 0x206F),  # WORD JOINER .. invisible operators, bidi isolates, deprecated format characters
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE (BOM)
    (0xFFF9, 0xFFFB),  # interlinear annotation anchor, separator, terminator
    (0x1BCA0, 0x1BCA3),  # shorthand format controls
    (0x1D173, 0x1D17A),  # musical symbol format controls
    (0xE0000, 0xE007F),  # tag characters
)
# Characters that render blank but are not White_Space, so a label made only of
# them (with or without whitespace and marks) has nothing visible to show.
_LABEL_BLANK_CHARACTERS = frozenset({0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0})
# The Unicode White_Space property (PropList.txt), listed explicitly so the rule
# does not depend on how ``str.isspace`` is defined in a given Python version.
_WHITE_SPACE = frozenset(
    {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x0020, 0x0085, 0x00A0, 0x1680}
    | set(range(0x2000, 0x200B))
    | {0x2028, 0x2029, 0x202F, 0x205F, 0x3000}
)
_LABEL_REJECTED_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
_LABEL_CHARACTER_MESSAGE = (
    "label must not contain control characters, line or paragraph separators, unpaired surrogates, "
    "noncharacters, bidi embeddings, overrides or isolates, invisible format characters, '<' or '>'"
)
_LABEL_NO_VISIBLE_MESSAGE = "label must contain at least one visible character"


def label_character_message(field: str, code_point: int | None = None) -> str:
    """The rejected-character message, naming the first rejected code point when it is known.

    Only the code point is named (``U+200B``), never the character itself, so a
    bidi override in the input cannot reorder the message around it.
    """
    message = _LABEL_CHARACTER_MESSAGE.replace("label", field, 1)
    if code_point is None:
        return message
    return f"{message} (found U+{code_point:04X})"


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


def normalize_metric_label(value: str) -> str:
    """The stored form of a label: NFC. Labels are otherwise stored as sent (no trimming)."""
    return unicodedata.normalize("NFC", value)


def normalize_label_member(body: Any) -> None:
    """NFC-normalize ``body["label"]`` in place, so the generated model counts it after normalization.

    Create, update and validate call this before ``model_validate``; the length
    limit is counted on the normalized value (NFD text uses more code points).
    Anything that is not a string is left for the model to reject.
    """
    if isinstance(body, dict) and isinstance(body.get("label"), str):
        body["label"] = normalize_metric_label(body["label"])


def _is_noncharacter(code_point: int) -> bool:
    return 0xFDD0 <= code_point <= 0xFDEF or (code_point & 0xFFFE) == 0xFFFE


def _is_rejected_label_character(char: str) -> bool:
    code_point = ord(char)
    if char in "<>" or unicodedata.category(char) in _LABEL_REJECTED_CATEGORIES or _is_noncharacter(code_point):
        return True
    return any(low <= code_point <= high for low, high in _LABEL_REJECTED_FORMAT_RANGES)


def _is_visible_base_character(char: str) -> bool:
    """A character that shows something on its own.

    Not whitespace, a mark, a format character, a blank, or unassigned (Cn).
    Counting Cn as not visible means a Python upgrade that assigns a code point
    can only make this check looser, never reject a label it accepted before.
    """
    code_point = ord(char)
    if code_point in _WHITE_SPACE or code_point in _LABEL_BLANK_CHARACTERS:
        return False
    category = unicodedata.category(char)
    # Mn/Mc/Me cover combining marks and the variation selectors; Cf covers the
    # format characters a label may contain (ZWJ, ZWNJ, LRM, RLM, ALM).
    return not category.startswith("M") and category not in {"Cf", "Cn"}


def check_metric_label(value: Any, field: str = "label") -> str | None:
    """The MetricLabel rules, applied on every write path (create, update, validate, OSI import).

    ``None`` means "no label" and is valid. Otherwise the value must be a string
    (never converted to one), 1-256 code points after NFC normalization, free of
    the rejected characters, and must contain at least one visible base
    character. The message names the field, the rule and the first rejected
    code point, never the value. Callers store :func:`normalize_metric_label`
    of a value this accepts.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return f"{field} must be a string"
    normalized = normalize_metric_label(value)
    if not METRIC_LABEL_MIN_LENGTH <= len(normalized) <= METRIC_LABEL_MAX_LENGTH:
        return f"{field} must be {METRIC_LABEL_MIN_LENGTH}-{METRIC_LABEL_MAX_LENGTH} characters"
    rejected = next((char for char in normalized if _is_rejected_label_character(char)), None)
    if rejected is not None:
        return label_character_message(field, ord(rejected))
    if not any(_is_visible_base_character(char) for char in normalized):
        return _LABEL_NO_VISIBLE_MESSAGE.replace("label", field, 1)
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
        check_metric_label(metric.label),
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
