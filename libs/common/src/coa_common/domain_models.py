# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Domain models for structured data metadata.

Typed dataclasses representing the full lifecycle of table/column metadata
from discovery through enrichment to steward approval. Designed to serialize
cleanly to JSON for DataZone form storage and API responses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

# ── Enums ─────────────────────────────────────────────────────────────


class EnrichmentSource(StrEnum):
    """Origin of a metadata annotation."""

    AI_GENERATED = "AI_GENERATED"
    AI_INFERRED = "AI_INFERRED"
    CATALOG_EXISTING = "CATALOG_EXISTING"
    DETERMINISTIC = "DETERMINISTIC"
    STEWARD_EDITED = "STEWARD_EDITED"
    STEWARD_SPECIFIED = "STEWARD_SPECIFIED"


class ReviewStatus(StrEnum):
    """Steward review state for enriched metadata."""

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ── Business Metadata ─────────────────────────────────────────────────


@dataclass
class BusinessMetadata:
    """AI-generated or curated business context for a table or column.

    The ``enrichment_source`` field reflects the provenance of the
    ``description`` (the primary, protected field). ``synonyms``,
    ``glossary_terms`` and ``tags`` may have been added by a separate process
    (e.g. AI enrichment) regardless of this value, since sources rarely
    expose those fields. Use git history or asset revisions for fine-grained
    field-level auditing.

    Provenance values (see ``EnrichmentSource``):
      * ``DETERMINISTIC`` — fetched directly from the source system (Glue
        ``Description``/``Comment``, PostgreSQL ``pg_description``, MySQL
        ``COLUMN_COMMENT``, etc.). Authoritative, do not overwrite.
      * ``CATALOG_EXISTING`` — pulled from a 3rd-party catalog tool
        (Collibra, Atlan, etc.). Authoritative, do not overwrite.
      * ``STEWARD_EDITED`` / ``STEWARD_SPECIFIED`` — set by a human reviewer.
        Authoritative, do not overwrite.
      * ``AI_GENERATED`` — generated via LLM. May be re-generated.
    """

    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    glossary_terms: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    enrichment_source: str = ""  # EnrichmentSource value
    review_status: str = "PENDING_REVIEW"  # ReviewStatus value
    confidence: float = 0.0  # AI-inference confidence (0.0-1.0); 0 when not AI-generated


# ── Constraints ───────────────────────────────────────────────────────


@dataclass
class PrimaryKey:
    """Primary key definition for a table."""

    columns: list[str] = field(default_factory=list)
    source: str = ""  # EnrichmentSource value
    confidence: float = 0.0


@dataclass
class ForeignKey:
    """Foreign key relationship between tables."""

    column: str = ""
    target_table: str = ""
    target_column: str = ""
    source: str = ""  # EnrichmentSource value
    confidence: float = 0.0


# ── Column ────────────────────────────────────────────────────────────


@dataclass
class Column:
    """A column within a discovered table.

    The column's description lives on ``business_metadata.description``
    (with provenance via ``business_metadata.enrichment_source``). Source-system
    comments populated by connectors are tagged ``DETERMINISTIC``; AI-generated
    descriptions are tagged ``AI_GENERATED``. There is intentionally no
    separate technical ``description`` field — keep one canonical home so the
    enricher's preservation rules and the catalog UI agree on a single source.
    """

    name: str
    data_type: str
    nullable: bool = True
    is_partition_key: bool = False
    business_metadata: BusinessMetadata = field(default_factory=BusinessMetadata)
    distinct_values: list[str] = field(default_factory=list)
    """Sampled distinct values for low-cardinality categorical columns (capped,
    best-effort). Empty when the column is high-cardinality, non-string, or
    sampling was skipped/unavailable. Used by the serve NL→SQL layer to hint
    the LLM with correct enum literals for WHERE clauses."""


# ── Technical Metadata ────────────────────────────────────────────────


@dataclass
class TechnicalMetadata:
    """Source-level technical metadata for a table."""

    column_count: int = 0
    partition_keys: list[str] = field(default_factory=list)
    format: str = ""  # e.g. "parquet", "iceberg", "redshift"
    location: str = ""  # S3 path or connection URI


# ── Table ─────────────────────────────────────────────────────────────


@dataclass
class Table:
    """A discovered table with technical and business metadata.

    The table's own description lives on ``business_metadata.description``.
    ``database_description`` is reserved for the **database/schema**-level
    description (e.g. Glue ``get_database().Description``, PostgreSQL
    ``obj_description((schema)::regnamespace)``, Snowflake
    ``information_schema.schemata.comment``) — not the table itself. The
    enricher passes ``database_description`` to the LLM as "External
    evidence" extra context, alongside any externally-loaded domain
    knowledge (e.g. BIRD evidence) that is also written to this field.
    """

    name: str
    database: str
    data_source_id: str = ""
    namespace_id: str = ""
    database_description: str = ""
    technical_metadata: TechnicalMetadata = field(default_factory=TechnicalMetadata)
    business_metadata: BusinessMetadata = field(default_factory=BusinessMetadata)
    primary_key: PrimaryKey = field(default_factory=PrimaryKey)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    technical_metadata_hash: str = ""

    @property
    def table_id(self) -> str:
        """Canonical identifier: database.table."""
        return f"{self.database}.{self.name}"


# ── Discovery Result ──────────────────────────────────────────────────


@dataclass
class DiscoveredMetadata:
    """Output of a connector's discover_metadata call."""

    tables: list[Table] = field(default_factory=list)
    failed_tables: list[str] = field(default_factory=list)
    """Tables the connector listed but could not read, as ``database.table``.

    Stays empty for connectors that read a table's schema in the same call that
    lists it, since those cannot partially fail. It is populated by connectors
    that read each table separately and degrade per table — losing that table's
    columns, comments, and keys while the scan as a whole still succeeds. Because
    the source still advances to PENDING_REVIEW and enrichment then backfills
    AI-generated descriptions over the gap, a steward would otherwise review a
    silently incomplete ontology as if it were complete. The pipeline therefore
    records this as a scan-level signal rather than a log line."""

    @property
    def total_columns(self) -> int:
        """Total column count across all discovered tables."""
        return sum(len(t.columns) for t in self.tables)


# ── Serialization ─────────────────────────────────────────────────────


def to_dict(obj: Any) -> Any:
    """Recursively serialize dataclasses to plain dicts for JSON storage."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [to_dict(i) for i in obj]
    if isinstance(obj, Enum):
        return obj.value
    return obj


def compute_schema_hash(columns: list[Column]) -> str:
    """Deterministic 16-char hash of a table's column schema.

    Covers column name, data type, partition-key flag, and nullability — the
    technical shape a re-scan must detect drift in, deliberately ignoring
    descriptions/comments. Nullability matters because ``NOT NULL`` drives
    induced ``owl:minCardinality 1``; it is a no-op for connectors that cannot
    report it (Glue hardcodes ``nullable=True``), so including it never
    false-triggers there and catches real drift where it is reported (JDBC).
    Used two ways, which MUST agree:
      * connectors stamp ``Table.technical_metadata_hash`` at discovery time;
      * re-scan change detection recomputes a stored table's hash from its
        persisted columns and compares it to the freshly discovered hash.
    Because both call this one function, the two hashes are comparable.
    """
    normalized = sorted(
        [{"n": c.name, "t": c.data_type, "p": c.is_partition_key, "u": c.nullable} for c in columns],
        key=lambda x: str(x["n"]),
    )
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:16]
