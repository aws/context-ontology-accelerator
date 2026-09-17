# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.domain_models.compute_schema_hash.

This is the single hash both connectors stamp onto ``Table.technical_metadata_hash``
at discovery and re-scan change detection recomputes from a stored table's
columns. The tests pin the properties re-scan relies on: it reflects the
technical shape (column name / type / partition flag), ignores description and
column order, and is stable and comparable across calls.
"""

from __future__ import annotations

import pytest
from coa_common.domain_models import BusinessMetadata, Column, compute_schema_hash

pytestmark = pytest.mark.unit


def _col(
    name: str,
    data_type: str = "int",
    *,
    is_partition_key: bool = False,
    nullable: bool = True,
    description: str = "",
) -> Column:
    return Column(
        name=name,
        data_type=data_type,
        is_partition_key=is_partition_key,
        nullable=nullable,
        business_metadata=BusinessMetadata(description=description),
    )


def test_hash_is_order_independent():
    forward = [_col("id"), _col("name", "varchar")]
    reversed_ = [_col("name", "varchar"), _col("id")]
    assert compute_schema_hash(forward) == compute_schema_hash(reversed_)


def test_hash_ignores_description():
    # Only technical shape feeds the hash, so a steward editing a description
    # must NOT read as a schema change on the next re-scan.
    plain = [_col("id", "int")]
    described = [_col("id", "int", description="the primary key")]
    assert compute_schema_hash(plain) == compute_schema_hash(described)


def test_hash_changes_on_type_change():
    assert compute_schema_hash([_col("id", "int")]) != compute_schema_hash([_col("id", "bigint")])


def test_hash_changes_on_name_change():
    assert compute_schema_hash([_col("id")]) != compute_schema_hash([_col("renamed_id")])


def test_hash_changes_on_partition_flag():
    plain = [_col("dt", "date")]
    partitioned = [_col("dt", "date", is_partition_key=True)]
    assert compute_schema_hash(plain) != compute_schema_hash(partitioned)


def test_hash_changes_on_nullability():
    # NOT NULL -> nullable is a real schema change (drives induced cardinality).
    nullable = [_col("id", "int", nullable=True)]
    not_null = [_col("id", "int", nullable=False)]
    assert compute_schema_hash(nullable) != compute_schema_hash(not_null)


def test_hash_is_16_hex_chars():
    digest = compute_schema_hash([_col("id"), _col("name", "varchar")])
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
