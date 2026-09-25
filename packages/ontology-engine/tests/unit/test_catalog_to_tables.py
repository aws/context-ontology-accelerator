# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_catalog_to_tables``.

The catalog dict comes off ``catalog_reader.py`` (Sources side) carrying
per-FK / per-PK ``source`` tags from the ``EnrichmentSource`` enum. This
helper flattens that catalog into ``CatalogTable``-compatible dicts the
inducer consumes, and must propagate the ``source`` tag into
``CatalogConstraint.relationshipType`` so downstream emission of
``coa:fkProvenance`` annotations on the proposal Turtle works.
"""

from __future__ import annotations

import pytest
from coa_ontology.induce_catalog import _catalog_to_tables

pytestmark = pytest.mark.unit


def _catalog(*, fk_source: str | None = "DETERMINISTIC", pk_source: str | None = "DETERMINISTIC") -> dict:
    return {
        "databases": [
            {
                "name": "bench",
                "tables": [
                    {
                        "name": "loss_payment",
                        "businessMetadata": {"description": "Payments per claim"},
                        "primaryKey": {
                            "columns": ["claim_amount_identifier"],
                            "source": pk_source,
                        },
                        "foreignKeys": [
                            {
                                "column": "claim_amount_identifier",
                                "targetTable": "claim_amount",
                                "targetColumn": "claim_amount_identifier",
                                "source": fk_source,
                            }
                        ],
                        "columns": [
                            {"name": "claim_amount_identifier", "type": "INT"},
                            {"name": "paid_at", "type": "TIMESTAMP"},
                        ],
                    }
                ],
            }
        ]
    }


def test_fk_source_propagates_to_relationship_type():
    out = _catalog_to_tables(_catalog(fk_source="DETERMINISTIC"))
    fk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "FOREIGN_KEY")
    assert fk["relationshipType"] == "DETERMINISTIC"


def test_ai_inferred_fk_carries_through():
    out = _catalog_to_tables(_catalog(fk_source="AI_INFERRED"))
    fk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "FOREIGN_KEY")
    assert fk["relationshipType"] == "AI_INFERRED"


def test_missing_source_yields_none():
    out = _catalog_to_tables(_catalog(fk_source=None, pk_source=None))
    fk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "FOREIGN_KEY")
    pk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "PRIMARY_KEY")
    assert fk["relationshipType"] is None
    assert pk["relationshipType"] is None


def test_pk_source_propagates_to_relationship_type():
    out = _catalog_to_tables(_catalog(pk_source="STEWARD_SPECIFIED"))
    pk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "PRIMARY_KEY")
    assert pk["relationshipType"] == "STEWARD_SPECIFIED"


def test_distinct_values_propagate_to_columns():
    """Low-cardinality sampled distinct values flow through to the column dict
    so the inducer can emit coa:distinctValues on the datatype property."""
    catalog = _catalog()
    # Attach sampled distinct values to one column (as catalog_reader emits them).
    for col in catalog["databases"][0]["tables"][0]["columns"]:
        if col["name"] == "paid_at":
            col["distinctValues"] = ["RETURNED", "RETURN_REQUESTED"]
    out = _catalog_to_tables(catalog)
    paid_at = next(c for c in out[0]["columns"] if c["name"] == "paid_at")
    assert paid_at["distinctValues"] == ["RETURNED", "RETURN_REQUESTED"]
    # Columns without sampling get an empty list, never a missing key.
    other = next(c for c in out[0]["columns"] if c["name"] == "claim_amount_identifier")
    assert other["distinctValues"] == []


def test_fk_governance_fields_propagate_to_constraint():
    # #1088: reviewStatus + targetDatasourceId must reach CatalogConstraint so the
    # induction gate can withhold unapproved FKs and cross-source targets resolve.
    catalog = {
        "databases": [
            {
                "name": "bench",
                "tables": [
                    {
                        "name": "loss_payment",
                        "foreignKeys": [
                            {
                                "column": "cust_id",
                                "targetTable": "customers",
                                "targetColumn": "id",
                                "source": "AI_INFERRED",
                                "reviewStatus": "PENDING_REVIEW",
                                "targetDatasourceId": "DS#2",
                            }
                        ],
                        "columns": [{"name": "cust_id", "type": "INT"}],
                    }
                ],
            }
        ]
    }
    out = _catalog_to_tables(catalog)
    fk = next(c for c in out[0]["tableConstraints"] if c["constraintType"] == "FOREIGN_KEY")
    assert fk["reviewStatus"] == "PENDING_REVIEW"
    assert fk["targetDatasourceId"] == "DS#2"
