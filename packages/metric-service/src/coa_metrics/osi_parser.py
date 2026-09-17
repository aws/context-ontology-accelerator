# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OSI v1.0 YAML parser and serializer for metric import/export.

Parses OSI YAML documents into internal metric representations and
serializes internal metrics back to OSI YAML format.

OSI spec version: v1.0 (pinned in manifest as osi_spec_version: "1.0").

Dialect mapping (internal → OSI):
  POSTGRESQL, TRINO, REDSHIFT → ANSI_SQL
  SNOWFLAKE → SNOWFLAKE
  DATABRICKS → DATABRICKS

Dialect mapping (OSI → internal):
  ANSI_SQL → POSTGRESQL (default; caller can override)
  SNOWFLAKE → SNOWFLAKE
  DATABRICKS → DATABRICKS
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
import yaml

from coa_metrics.constants import VALID_SQL_DIALECTS, SqlDialect

logger = structlog.get_logger(__name__)

# ── OSI Spec Constants ──────────────────────────────────────────────────

OSI_SPEC_VERSION = "1.0"
OSI_VENDOR_NAME = "COA"

# ── Dialect Mapping ─────────────────────────────────────────────────────

# Internal dialect → OSI dialect
_INTERNAL_TO_OSI: dict[str, str] = {
    SqlDialect.POSTGRESQL: "ANSI_SQL",
    SqlDialect.TRINO: "ANSI_SQL",
    SqlDialect.REDSHIFT: "ANSI_SQL",
    SqlDialect.SNOWFLAKE: "SNOWFLAKE",
    SqlDialect.DATABRICKS: "DATABRICKS",
    SqlDialect.MYSQL: "ANSI_SQL",
}

# OSI dialect → internal dialect (default mapping for import)
_OSI_TO_INTERNAL: dict[str, str] = {
    "ANSI_SQL": SqlDialect.POSTGRESQL,
    "SNOWFLAKE": SqlDialect.SNOWFLAKE,
    "DATABRICKS": SqlDialect.DATABRICKS,
}


def osi_dialect_to_internal(osi_dialect: str) -> str:
    """Map an OSI dialect identifier to the internal dialect name.

    Args:
        osi_dialect: OSI dialect string (e.g., "ANSI_SQL", "SNOWFLAKE").

    Returns:
        Internal dialect name as SqlDialect value (e.g., "POSTGRESQL", "SNOWFLAKE").

    Raises:
        ValueError: If the OSI dialect is not recognized.
    """
    normalized = osi_dialect.strip().upper()
    # A real internal dialect named directly (REDSHIFT, TRINO, MYSQL, POSTGRESQL,
    # ...) is legitimate OSI input — a hand-authored or third-party document need
    # not go through COA's own ANSI_SQL/SNOWFLAKE/DATABRICKS export aliases. Pass
    # it through case-normalized before consulting the wire-alias map (#140).
    if normalized in VALID_SQL_DIALECTS:
        return normalized
    internal = _OSI_TO_INTERNAL.get(normalized)
    if internal is None:
        raise ValueError(
            f"Unknown OSI dialect '{osi_dialect}'. Supported: {', '.join(sorted(_OSI_TO_INTERNAL.keys()))}"
        )
    return internal


def internal_dialect_to_osi(internal_dialect: str) -> str:
    """Map an internal dialect name to the OSI dialect identifier.

    Args:
        internal_dialect: Internal dialect as SqlDialect value (e.g., "POSTGRESQL", "SNOWFLAKE").

    Returns:
        OSI dialect string (e.g., "ANSI_SQL", "SNOWFLAKE").

    Raises:
        ValueError: If the internal dialect has no OSI mapping.
    """
    normalized = internal_dialect.strip().upper()
    osi = _INTERNAL_TO_OSI.get(normalized)
    if osi is None:
        raise ValueError(
            f"No OSI mapping for internal dialect '{internal_dialect}'. "
            f"Supported: {', '.join(sorted(_INTERNAL_TO_OSI.keys()))}"
        )
    return osi


# ── OSI Data Models ─────────────────────────────────────────────────────


@dataclass
class OsiDialectExpression:
    """A single dialect + SQL expression in OSI format."""

    dialect: str  # OSI dialect (e.g., "ANSI_SQL", "SNOWFLAKE")
    expression: str


@dataclass
class OsiCustomExtension:
    """COA vendor extension data in OSI format."""

    data_source_id: str = ""
    source_table: str = ""
    unit: str = ""
    return_type: str = ""
    time_dimension: str = ""
    ontology_concepts: list[str] = field(default_factory=list)
    defined_by: str = ""
    effective_from: str = ""


@dataclass
class OsiAiContext:
    """AI context in OSI YAML format — structured object with synonyms, instructions, examples."""

    synonyms: list[str] = field(default_factory=list)
    instructions: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class OsiMetric:
    """A single metric in OSI YAML format."""

    name: str
    description: str
    expression: list[OsiDialectExpression]
    ai_context: OsiAiContext | None = None
    custom_extensions: OsiCustomExtension | None = None


@dataclass
class OsiDataset:
    """A dataset reference in OSI YAML format."""

    name: str
    source: str = ""
    data_source_id: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class OsiDocument:
    """Top-level OSI YAML document."""

    osi_spec_version: str = OSI_SPEC_VERSION
    datasets: list[OsiDataset] = field(default_factory=list)
    metrics: list[OsiMetric] = field(default_factory=list)


# ── Parser ──────────────────────────────────────────────────────────────


@dataclass
class ParseError:
    """A single parse error with location context."""

    path: str
    message: str


@dataclass
class ParseResult:
    """Result of parsing an OSI YAML document."""

    document: OsiDocument | None
    errors: list[ParseError]

    @property
    def success(self) -> bool:
        """Whether parsing produced a document with no errors."""
        return self.document is not None and len(self.errors) == 0


def parse_osi_yaml(content: str) -> ParseResult:
    """Parse an OSI YAML string into an OsiDocument.

    Args:
        content: Raw YAML string in OSI v1.0 format.

    Returns:
        ParseResult with the parsed document or errors.
    """
    errors: list[ParseError] = []

    # Parse YAML
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return ParseResult(
            document=None,
            errors=[ParseError(path="$", message=f"Invalid YAML: {exc}")],
        )

    if not isinstance(raw, dict):
        return ParseResult(
            document=None,
            errors=[ParseError(path="$", message="OSI document must be a YAML mapping")],
        )

    # Validate spec version
    spec_version = raw.get("osi_spec_version", "")
    if not spec_version:
        errors.append(ParseError(path="$.osi_spec_version", message="Missing required field 'osi_spec_version'"))
    elif str(spec_version) != OSI_SPEC_VERSION:
        errors.append(
            ParseError(
                path="$.osi_spec_version",
                message=f"Unsupported OSI spec version '{spec_version}'. Expected '{OSI_SPEC_VERSION}'",
            )
        )

    # Parse datasets
    datasets = _parse_datasets(raw.get("datasets", []), errors)

    # Parse metrics
    metrics = _parse_metrics(raw.get("metrics", []), errors)

    if errors:
        return ParseResult(document=None, errors=errors)

    return ParseResult(
        document=OsiDocument(
            osi_spec_version=str(spec_version),
            datasets=datasets,
            metrics=metrics,
        ),
        errors=[],
    )


def _parse_datasets(raw_datasets: Any, errors: list[ParseError]) -> list[OsiDataset]:
    """Parse the datasets section of an OSI document."""
    if not raw_datasets:
        return []

    if not isinstance(raw_datasets, list):
        errors.append(ParseError(path="$.datasets", message="'datasets' must be a list"))
        return []

    datasets: list[OsiDataset] = []
    for i, ds in enumerate(raw_datasets):
        path = f"$.datasets[{i}]"
        if not isinstance(ds, dict):
            errors.append(ParseError(path=path, message="Dataset entry must be a mapping"))
            continue

        name = ds.get("name", "")
        if not name:
            errors.append(ParseError(path=f"{path}.name", message="Dataset 'name' is required"))
            continue

        datasets.append(
            OsiDataset(
                name=str(name),
                source=str(ds.get("source", "")),
                data_source_id=str(ds.get("data_source_id", "")),
                description=str(ds.get("description", "")),
                synonyms=[str(s) for s in ds.get("synonyms", []) if s],
            )
        )

    return datasets


def _parse_metrics(raw_metrics: Any, errors: list[ParseError]) -> list[OsiMetric]:
    """Parse the metrics section of an OSI document."""
    if not raw_metrics:
        errors.append(ParseError(path="$.metrics", message="At least one metric is required"))
        return []

    if not isinstance(raw_metrics, list):
        errors.append(ParseError(path="$.metrics", message="'metrics' must be a list"))
        return []

    metrics: list[OsiMetric] = []
    for i, m in enumerate(raw_metrics):
        path = f"$.metrics[{i}]"
        if not isinstance(m, dict):
            errors.append(ParseError(path=path, message="Metric entry must be a mapping"))
            continue

        metric = _parse_single_metric(m, path, errors)
        if metric:
            metrics.append(metric)

    return metrics


def _parse_single_metric(raw: dict[str, Any], path: str, errors: list[ParseError]) -> OsiMetric | None:
    """Parse a single metric entry."""
    name = raw.get("name", "")
    if not name:
        errors.append(ParseError(path=f"{path}.name", message="Metric 'name' is required"))
        return None

    description = raw.get("description", "")
    if not description:
        errors.append(ParseError(path=f"{path}.description", message="Metric 'description' is required"))
        return None

    # Parse expression dialects
    expression_raw = raw.get("expression", {})
    dialects_raw = expression_raw.get("dialects", []) if isinstance(expression_raw, dict) else []

    if not dialects_raw:
        errors.append(
            ParseError(path=f"{path}.expression.dialects", message="At least one dialect expression is required")
        )
        return None

    dialect_expressions: list[OsiDialectExpression] = []
    for j, d in enumerate(dialects_raw):
        d_path = f"{path}.expression.dialects[{j}]"
        if not isinstance(d, dict):
            errors.append(ParseError(path=d_path, message="Dialect entry must be a mapping"))
            continue

        dialect = d.get("dialect", "")
        expr = d.get("expression", "")
        if not dialect:
            errors.append(ParseError(path=f"{d_path}.dialect", message="'dialect' is required"))
            continue
        if not expr:
            errors.append(ParseError(path=f"{d_path}.expression", message="'expression' is required"))
            continue

        dialect_expressions.append(OsiDialectExpression(dialect=str(dialect), expression=str(expr)))

    if not dialect_expressions:
        return None

    # Parse custom extensions (COA vendor)
    custom_ext = _parse_custom_extensions(raw.get("custom_extensions", []), path)

    return OsiMetric(
        name=str(name),
        description=str(description),
        expression=dialect_expressions,
        ai_context=_parse_ai_context(raw.get("ai_context")),
        custom_extensions=custom_ext,
    )


def _parse_ai_context(raw: Any) -> OsiAiContext | None:
    """Parse ai_context from OSI YAML — structured object with synonyms, instructions, examples."""
    if not raw:
        return None
    if isinstance(raw, str):
        # Legacy: plain string → store as instructions
        return OsiAiContext(instructions=raw)
    if isinstance(raw, dict):
        return OsiAiContext(
            synonyms=[str(s) for s in raw.get("synonyms", []) if s],
            instructions=str(raw.get("instructions", "")),
            examples=[str(e) for e in raw.get("examples", []) if e],
        )
    return None


def _parse_custom_extensions(raw_extensions: Any, path: str) -> OsiCustomExtension | None:
    """Parse COA custom extensions from a metric."""
    if not raw_extensions:
        return None

    if not isinstance(raw_extensions, list):
        return None

    # Find the COA vendor extension
    for ext in raw_extensions:
        if not isinstance(ext, dict):
            continue
        if ext.get("vendor_name", "").upper() == OSI_VENDOR_NAME:
            data = ext.get("data", {})
            if not isinstance(data, dict):
                continue
            return OsiCustomExtension(
                data_source_id=str(data.get("data_source_id", "")),
                source_table=str(data.get("source_table", "")),
                unit=str(data.get("unit", "")),
                return_type=str(data.get("return_type", "")),
                time_dimension=str(data.get("time_dimension", "")),
                ontology_concepts=[str(c) for c in data.get("ontology_concepts", []) if c],
                defined_by=str(data.get("defined_by", "")),
                effective_from=str(data.get("effective_from", "")),
            )

    return None


# ── Serializer (Export) ─────────────────────────────────────────────────


def serialize_osi_yaml(document: OsiDocument) -> str:
    """Serialize an OsiDocument to OSI YAML string.

    Args:
        document: The OsiDocument to serialize.

    Returns:
        YAML string in OSI v1.0 format.
    """
    output: dict[str, Any] = {
        "osi_spec_version": document.osi_spec_version,
    }

    # Datasets
    if document.datasets:
        output["datasets"] = [_serialize_dataset(ds) for ds in document.datasets]

    # Metrics
    output["metrics"] = [_serialize_metric(m) for m in document.metrics]

    return yaml.dump(output, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _serialize_dataset(dataset: OsiDataset) -> dict[str, Any]:
    """Serialize a single dataset to dict for YAML output."""
    result: dict[str, Any] = {"name": dataset.name}
    if dataset.source:
        result["source"] = dataset.source
    if dataset.data_source_id:
        result["data_source_id"] = dataset.data_source_id
    if dataset.description:
        result["description"] = dataset.description
    if dataset.synonyms:
        result["synonyms"] = dataset.synonyms
    return result


def _serialize_metric(metric: OsiMetric) -> dict[str, Any]:
    """Serialize a single metric to dict for YAML output."""
    result: dict[str, Any] = {
        "name": metric.name,
        "description": metric.description,
        "expression": {"dialects": [{"dialect": d.dialect, "expression": d.expression} for d in metric.expression]},
    }

    if metric.ai_context:
        ai_ctx: dict[str, Any] = {}
        if metric.ai_context.synonyms:
            ai_ctx["synonyms"] = metric.ai_context.synonyms
        if metric.ai_context.instructions:
            ai_ctx["instructions"] = metric.ai_context.instructions
        if metric.ai_context.examples:
            ai_ctx["examples"] = metric.ai_context.examples
        if ai_ctx:
            result["ai_context"] = ai_ctx

    if metric.custom_extensions:
        ext = metric.custom_extensions
        data: dict[str, Any] = {}
        if ext.data_source_id:
            data["data_source_id"] = ext.data_source_id
        if ext.source_table:
            data["source_table"] = ext.source_table
        if ext.unit:
            data["unit"] = ext.unit
        if ext.return_type:
            data["return_type"] = ext.return_type
        if ext.time_dimension:
            data["time_dimension"] = ext.time_dimension
        if ext.ontology_concepts:
            data["ontology_concepts"] = ext.ontology_concepts
        if ext.defined_by:
            data["defined_by"] = ext.defined_by
        if ext.effective_from:
            data["effective_from"] = ext.effective_from

        if data:
            result["custom_extensions"] = [{"vendor_name": OSI_VENDOR_NAME, "data": data}]

    return result
