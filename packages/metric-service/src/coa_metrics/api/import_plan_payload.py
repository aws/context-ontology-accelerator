# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical, immutable S3 payload for one async import offset plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from coa_metrics.api.import_job_store import MetricDisposition
from coa_metrics.neptune_client import MetricAiContext, MetricDefinition, MetricDialect

_PLAN_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class PlannedMetric:
    """One source metric's exact normalized payload and durable logical outcome."""

    source_index: int
    name: str
    disposition: MetricDisposition
    definition: MetricDefinition | None = None
    warning: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """Validate disposition-specific payload invariants."""
        if isinstance(self.source_index, bool) or not isinstance(self.source_index, int) or self.source_index < 0:
            raise ValueError("planned metric source_index must be a non-negative integer")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("planned metric name must be a non-empty string")
        if not isinstance(self.disposition, MetricDisposition):
            raise ValueError("planned metric disposition must be a MetricDisposition")

        if self.disposition == MetricDisposition.ERROR:
            if self.definition is not None:
                raise ValueError("ERROR planned metric must not contain a definition")
            if not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("ERROR planned metric requires a non-empty error")
            if self.warning is not None:
                raise ValueError("ERROR planned metric must not contain a warning")
            return

        if not isinstance(self.definition, MetricDefinition):
            raise ValueError(f"{self.disposition.value} planned metric requires a MetricDefinition")
        _validate_metric_definition(self.definition)
        if self.definition.name != self.name:
            raise ValueError("planned metric name must match its definition name")
        if self.error is not None:
            raise ValueError(f"{self.disposition.value} planned metric must not contain an error")

        if self.disposition == MetricDisposition.CREATE:
            if self.warning is not None:
                raise ValueError("CREATE planned metric must not contain a warning")
            return

        if not isinstance(self.warning, str) or not self.warning.strip():
            raise ValueError("UPDATE planned metric requires a non-empty warning")


@dataclass(frozen=True)
class OffsetPlanPayload:
    """Exact normalized definitions and logical outcomes for one import offset."""

    offset: int
    entries: tuple[PlannedMetric, ...]

    def __post_init__(self) -> None:
        """Require a non-empty contiguous source range."""
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("offset plan payload offset must be a non-negative integer")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise ValueError("offset plan payload entries must be a non-empty tuple")
        for index, entry in enumerate(self.entries):
            if not isinstance(entry, PlannedMetric):
                raise ValueError("offset plan payload entries must contain only PlannedMetric values")
            expected_source_index = self.offset + index
            if entry.source_index != expected_source_index:
                raise ValueError(
                    f"offset plan payload entry {index} has source index {entry.source_index}, "
                    f"expected {expected_source_index}"
                )


def canonical_plan_payload_bytes(plan: OffsetPlanPayload) -> bytes:
    """Return the schema-v2 plan as deterministic UTF-8 JSON bytes."""
    if not isinstance(plan, OffsetPlanPayload):
        raise ValueError("plan must be an OffsetPlanPayload")
    value = {
        "schemaVersion": _PLAN_SCHEMA_VERSION,
        "offset": plan.offset,
        "entries": [_serialize_entry(entry) for entry in plan.entries],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_plan_payload(data: bytes) -> OffsetPlanPayload:
    """Strictly parse canonical-plan JSON without accepting duplicate or unknown fields."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("offset plan payload must be non-empty bytes")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_mapping_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("offset plan payload must be valid UTF-8 JSON") from exc

    root = _exact_mapping(raw, {"schemaVersion", "offset", "entries"}, "offset plan payload")
    schema_version = _integer(root["schemaVersion"], "offset plan payload schemaVersion")
    if schema_version != _PLAN_SCHEMA_VERSION:
        raise ValueError(f"unsupported offset plan payload schema version {schema_version}")
    offset = _non_negative_integer(root["offset"], "offset plan payload offset")
    raw_entries = root["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("offset plan payload entries must be a non-empty list")

    entries = tuple(_parse_entry(raw_entry, index) for index, raw_entry in enumerate(raw_entries))
    return OffsetPlanPayload(offset=offset, entries=entries)


def plan_payload_sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 digest for canonical payload bytes."""
    if not isinstance(data, bytes):
        raise ValueError("payload digest input must be bytes")
    return hashlib.sha256(data).hexdigest()


def _serialize_entry(entry: PlannedMetric) -> dict[str, Any]:
    return {
        "sourceIndex": entry.source_index,
        "name": entry.name,
        "disposition": entry.disposition.value,
        "definition": _serialize_definition(entry.definition) if entry.definition is not None else None,
        "warning": entry.warning,
        "error": entry.error,
    }


def _serialize_definition(definition: MetricDefinition) -> dict[str, Any]:
    _validate_metric_definition(definition)
    ai_context = None
    if definition.ai_context is not None:
        ai_context = {
            "synonyms": list(definition.ai_context.synonyms),
            "instructions": definition.ai_context.instructions,
            "examples": list(definition.ai_context.examples),
        }
    return {
        "name": definition.name,
        "description": definition.description,
        "expressionDialects": [
            {"dialect": dialect.dialect, "expression": dialect.expression} for dialect in definition.expression_dialects
        ],
        "dataSourceId": definition.data_source_id,
        "sourceTable": definition.source_table,
        "defaultTimeGrain": definition.default_time_grain,
        "unit": definition.unit,
        "returnType": definition.return_type,
        "aiContext": ai_context,
        "ontologyConcepts": list(definition.ontology_concepts),
        "definedBy": definition.defined_by,
        "effectiveFrom": definition.effective_from,
    }


def _parse_entry(value: object, index: int) -> PlannedMetric:
    entry = _exact_mapping(
        value,
        {"sourceIndex", "name", "disposition", "definition", "warning", "error"},
        f"offset plan payload entry {index}",
    )
    source_index = _non_negative_integer(entry["sourceIndex"], f"offset plan payload entry {index} sourceIndex")
    name = _string(entry["name"], f"offset plan payload entry {index} name", non_empty=True)
    raw_disposition = _string(
        entry["disposition"],
        f"offset plan payload entry {index} disposition",
        non_empty=True,
    )
    try:
        disposition = MetricDisposition(raw_disposition)
    except ValueError as exc:
        raise ValueError(f"offset plan payload entry {index} has unknown disposition {raw_disposition!r}") from exc

    raw_definition = entry["definition"]
    definition = None if raw_definition is None else _parse_definition(raw_definition, index)
    warning = _optional_string(entry["warning"], f"offset plan payload entry {index} warning")
    error = _optional_string(entry["error"], f"offset plan payload entry {index} error")
    try:
        return PlannedMetric(
            source_index=source_index,
            name=name,
            disposition=disposition,
            definition=definition,
            warning=warning,
            error=error,
        )
    except ValueError as exc:
        raise ValueError(f"offset plan payload entry {index} is invalid: {exc}") from exc


def _parse_definition(value: object, entry_index: int) -> MetricDefinition:
    description = f"offset plan payload entry {entry_index} definition"
    definition = _exact_mapping(
        value,
        {
            "name",
            "description",
            "expressionDialects",
            "dataSourceId",
            "sourceTable",
            "defaultTimeGrain",
            "unit",
            "returnType",
            "aiContext",
            "ontologyConcepts",
            "definedBy",
            "effectiveFrom",
        },
        description,
    )

    raw_dialects = definition["expressionDialects"]
    if not isinstance(raw_dialects, list) or not raw_dialects:
        raise ValueError(f"{description} expressionDialects must be a non-empty list")
    dialects: list[MetricDialect] = []
    for dialect_index, raw_dialect in enumerate(raw_dialects):
        dialect = _exact_mapping(
            raw_dialect,
            {"dialect", "expression"},
            f"{description} expressionDialects entry {dialect_index}",
        )
        dialects.append(
            MetricDialect(
                dialect=_string(
                    dialect["dialect"],
                    f"{description} expressionDialects entry {dialect_index} dialect",
                    non_empty=True,
                ),
                expression=_string(
                    dialect["expression"],
                    f"{description} expressionDialects entry {dialect_index} expression",
                    non_empty=True,
                ),
            )
        )

    raw_concepts = definition["ontologyConcepts"]
    if not isinstance(raw_concepts, list):
        raise ValueError(f"{description} ontologyConcepts must be a list")
    concepts = [
        _string(concept, f"{description} ontologyConcepts entry {concept_index}", non_empty=True)
        for concept_index, concept in enumerate(raw_concepts)
    ]

    raw_ai_context = definition["aiContext"]
    ai_context = None
    if raw_ai_context is not None:
        ai = _exact_mapping(raw_ai_context, {"synonyms", "instructions", "examples"}, f"{description} aiContext")
        ai_context = MetricAiContext(
            synonyms=_string_list(ai["synonyms"], f"{description} aiContext synonyms"),
            instructions=_string(ai["instructions"], f"{description} aiContext instructions"),
            examples=_string_list(ai["examples"], f"{description} aiContext examples"),
        )

    metric = MetricDefinition(
        name=_string(definition["name"], f"{description} name", non_empty=True),
        description=_string(definition["description"], f"{description} description"),
        expression_dialects=dialects,
        data_source_id=_string(definition["dataSourceId"], f"{description} dataSourceId", non_empty=True),
        source_table=_string(definition["sourceTable"], f"{description} sourceTable", non_empty=True),
        default_time_grain=_optional_string(definition["defaultTimeGrain"], f"{description} defaultTimeGrain"),
        unit=_optional_string(definition["unit"], f"{description} unit"),
        return_type=_optional_string(definition["returnType"], f"{description} returnType"),
        ai_context=ai_context,
        ontology_concepts=concepts,
        defined_by=_optional_string(definition["definedBy"], f"{description} definedBy"),
        effective_from=_optional_string(definition["effectiveFrom"], f"{description} effectiveFrom"),
    )
    _validate_metric_definition(metric)
    return metric


def _validate_metric_definition(definition: MetricDefinition) -> None:
    if not isinstance(definition, MetricDefinition):
        raise ValueError("definition must be a MetricDefinition")
    _string(definition.name, "definition name", non_empty=True)
    _string(definition.description, "definition description")
    _string(definition.data_source_id, "definition data_source_id", non_empty=True)
    _string(definition.source_table, "definition source_table", non_empty=True)
    if not isinstance(definition.expression_dialects, list) or not definition.expression_dialects:
        raise ValueError("definition expression_dialects must be a non-empty list")
    for index, dialect in enumerate(definition.expression_dialects):
        if not isinstance(dialect, MetricDialect):
            raise ValueError(f"definition expression_dialects entry {index} must be a MetricDialect")
        _string(dialect.dialect, f"definition expression_dialects entry {index} dialect", non_empty=True)
        _string(dialect.expression, f"definition expression_dialects entry {index} expression", non_empty=True)
    for field_name in ("default_time_grain", "unit", "return_type", "defined_by", "effective_from"):
        _optional_string(getattr(definition, field_name), f"definition {field_name}")
    if not isinstance(definition.ontology_concepts, list):
        raise ValueError("definition ontology_concepts must be a list")
    for index, concept in enumerate(definition.ontology_concepts):
        _string(concept, f"definition ontology_concepts entry {index}", non_empty=True)
    if definition.ai_context is not None:
        if not isinstance(definition.ai_context, MetricAiContext):
            raise ValueError("definition ai_context must be a MetricAiContext")
        _string_list(definition.ai_context.synonyms, "definition ai_context synonyms")
        _string(definition.ai_context.instructions, "definition ai_context instructions")
        _string_list(definition.ai_context.examples, "definition ai_context examples")


def _mapping_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"offset plan payload contains duplicate key {key!r}")
        value[key] = item
    return value


def _exact_mapping(value: object, keys: set[str], description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} must be an object")
    if set(value) != keys:
        raise ValueError(f"{description} must contain exactly {sorted(keys)}")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{description} must be an integer")
    return value


def _non_negative_integer(value: object, description: str) -> int:
    result = _integer(value, description)
    if result < 0:
        raise ValueError(f"{description} must be non-negative")
    return result


def _string(value: object, description: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{description} must be a string")
    if non_empty and not value.strip():
        raise ValueError(f"{description} must be non-empty")
    return value


def _optional_string(value: object, description: str) -> str | None:
    if value is None:
        return None
    return _string(value, description)


def _string_list(value: object, description: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{description} must be a list")
    return [_string(item, f"{description} entry {index}") for index, item in enumerate(value)]
