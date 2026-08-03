# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detect PK-sharing subtype relationships in a relational catalog.

A common relational idiom encodes inheritance by reusing the parent's primary
key as the child's primary key. Concretely: child table ``LossPayment`` has a
single-column PK ``CLAIM_AMOUNT_IDENTIFIER`` that is also a single-column FK
pointing at parent ``ClaimAmount.CLAIM_AMOUNT_IDENTIFIER``. The FK column is
the same column as the PK column — that's the giveaway.

When this pattern is present, the inducer should emit ``rdfs:subClassOf``
instead of an ObjectProperty for the FK. This module provides the detection
helper plus a chain-walker for transitive inheritance.

Out of scope (filed as follow-up):
* Composite-PK subtype patterns — only single-column PKs are supported.
* Heuristic disambiguation between "is-a" and "1:1 association" — we default
  to is-a as the more common and useful case; a UI override is planned.
"""

from __future__ import annotations

import logging

from coa_ontology.inducer.services.data_catalog import CatalogTable, parse_referred_column

logger = logging.getLogger(__name__)


def _single_pk_column(table: CatalogTable) -> str | None:
    """Return the table's PK column iff it is a single-column primary key."""
    if not table.tableConstraints:
        return None
    for tc in table.tableConstraints:
        if tc.constraintType == "PRIMARY_KEY" and tc.columns and len(tc.columns) == 1:
            return tc.columns[0]
    return None


def _single_col_fks(table: CatalogTable) -> list[tuple[str, str, str]]:
    """Return ``[(child_col, parent_table, parent_col)]`` for every single-column FK.

    Parent table/column is parsed out of ``referredColumns[0]`` using the same
    convention the rest of the inducer uses: dotted identifier where the last
    two segments are ``TABLE.COLUMN``.
    """
    fks: list[tuple[str, str, str]] = []
    if not table.tableConstraints:
        return fks
    for tc in table.tableConstraints:
        if tc.constraintType != "FOREIGN_KEY":
            continue
        if not tc.columns or len(tc.columns) != 1:
            continue
        if not tc.referredColumns or len(tc.referredColumns) != 1:
            continue
        parent_table, parent_col = parse_referred_column(tc.referredColumns[0])
        if parent_col is None:
            continue  # no column component — cannot match against the parent PK
        fks.append((tc.columns[0], parent_table, parent_col))
    return fks


#: Minimum number of distinct children PK-sharing into the same parent
#: required to auto-fire ``rdfs:subClassOf``. Below this, the pair becomes
#: a ``coa:suggestedSubClassOf`` annotation and the user confirms via UI.
#:
#: The signal: real disjoint-subtype hierarchies (insurance subtypes of
#: claim_amount, finance subtypes of party) almost always have ≥2
#: children sharing the parent PK. 1:1 attached records (satscores ↔
#: schools, customer_address ↔ customer) almost always have 1. The
#: threshold is intentionally conservative — false positives at the
#: detector level are amplified by every downstream VKG query.
MULTI_CHILD_THRESHOLD = 2


def detect_pk_sharing_subtypes(
    tables: list[CatalogTable],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Detect PK-sharing pairs and split by structural confidence.

    Returns ``(confirmed, suggested)``:

    * **confirmed** — pairs whose parent has ≥``MULTI_CHILD_THRESHOLD``
      children PK-sharing into it. The disjoint-subtype shape is a strong
      structural signal of inheritance; auto-fire ``rdfs:subClassOf``.
    * **suggested** — pairs where the parent has only one PK-sharing
      child. Structurally ambiguous (could be inheritance, could be 1:1
      attached). Don't auto-fire; let the UI surface the pair as a
      ``coa:suggestedSubClassOf`` for the user to confirm or reject.

    Only single-column PK / single-column FK patterns are considered.
    The parent's PK must be exactly the column the child's FK points at
    — composite PKs and PK-FK shape mismatches are skipped.
    """
    parent_pk: dict[str, str] = {}
    for t in tables:
        pk = _single_pk_column(t)
        if pk is not None:
            parent_pk[t.name] = pk

    candidate_pairs: set[tuple[str, str]] = set()
    for child in tables:
        child_pk = _single_pk_column(child)
        if child_pk is None:
            continue
        for fk_col, parent_name, parent_col in _single_col_fks(child):
            if fk_col != child_pk:
                continue
            expected = parent_pk.get(parent_name)
            if expected is None or expected != parent_col:
                continue
            if parent_name == child.name:
                # Self-FK on PK is malformed; skip.
                continue
            candidate_pairs.add((child.name, parent_name))

    # Group candidates by parent → split into confirmed vs suggested.
    children_per_parent: dict[str, set[str]] = {}
    for child_name, parent_name2 in candidate_pairs:
        children_per_parent.setdefault(parent_name2, set()).add(child_name)

    confirmed: set[tuple[str, str]] = set()
    suggested: set[tuple[str, str]] = set()
    for child_name, parent_name2 in candidate_pairs:
        if len(children_per_parent[parent_name2]) >= MULTI_CHILD_THRESHOLD:
            confirmed.add((child_name, parent_name2))
        else:
            suggested.add((child_name, parent_name2))
    return confirmed, suggested


def build_subtype_chains(
    pairs: set[tuple[str, str]],
    max_depth: int = 5,
) -> dict[str, list[str]]:
    """Walk each child's ancestor chain (direct parent first, transitive after).

    * ``max_depth`` bounds the walk; logs a warning and truncates if exceeded.
    * Cycles abort the chain at the first repeat (and log a warning).
    * Multiple immediate parents (rare in practice) keep only the first one
      encountered — multi-inheritance is out of scope.
    """
    direct_parent: dict[str, str] = {}
    for child, parent in pairs:
        direct_parent.setdefault(child, parent)

    chains: dict[str, list[str]] = {}
    for child in direct_parent:
        chain: list[str] = []
        seen: set[str] = {child}
        cursor: str | None = direct_parent.get(child)
        while cursor is not None:
            if cursor in seen:
                logger.warning(
                    "subtype_chain_cycle_detected",
                    extra={"child": child, "cycle_at": cursor, "chain": chain},
                )
                break
            seen.add(cursor)
            chain.append(cursor)
            if len(chain) >= max_depth:
                next_cursor = direct_parent.get(cursor)
                if next_cursor is not None:
                    logger.warning(
                        "subtype_chain_truncated_at_max_depth",
                        extra={"child": child, "max_depth": max_depth},
                    )
                break
            cursor = direct_parent.get(cursor)
        if chain:
            chains[child] = chain
    return chains
