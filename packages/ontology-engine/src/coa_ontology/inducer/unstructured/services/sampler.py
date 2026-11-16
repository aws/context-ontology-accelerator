# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stratified Sampler for classification-aware entity sampling.

Selects a representative subset of entities per classification using
the √N formula, ensuring rare classes get adequate representation
without over-sampling dominant classes.

The sampler uses the entity-level class values (space-separated format,
e.g. "Financial Instrument") when calling ``get_entities_by_class()``,
not the CamelCase ``__SYS_Class__`` values directly.

Usage::

    from coa_ontology.inducer.unstructured.services.sampler import (
        sample,
    )

    result = sample(class_distribution, store, k_min=5, floor=3)
    # result: {"Company": ["id1", "id2", ...], "Person": ["id3", ...]}
"""

from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coa_ontology.inducer.unstructured.stores.protocol import (
        ClassRecord,
        LexicalGraphStore,
    )

log = logging.getLogger(__name__)


def _camel_to_spaced(s: str) -> str:
    """Convert CamelCase to space-separated format.

    Inserts a space before each uppercase letter that follows a lowercase
    letter or digit. Handles consecutive uppercase (acronyms) by only
    splitting before the last uppercase in a run followed by lowercase.

    Examples:
        "FinancialInstrument" → "Financial Instrument"
        "CreativeWork" → "Creative Work"
        "Company" → "Company"
        "BusinessSegment" → "Business Segment"
        "OperatingSegment" → "Operating Segment"
    """
    if not s:
        return s
    result: list[str] = [s[0]]
    for i in range(1, len(s)):
        ch = s[i]
        prev = s[i - 1]
        if ch.isupper() and prev.islower():
            result.append(" ")
        elif ch.isupper() and prev.isupper() and i + 1 < len(s) and s[i + 1].islower():
            # Handle acronym boundaries like "HTMLParser" → "HTML Parser"
            result.append(" ")
        result.append(ch)
    return "".join(result)


def sample(
    class_distribution: list[ClassRecord],
    store: LexicalGraphStore,
    k_min: int = 5,
    floor: int = 3,
    seed: int | None = None,
) -> dict[str, list[str]]:
    """Sample entities proportionally across classifications.

    For each class in the distribution:
    - Computes sample size: ``k = max(k_min, ceil(sqrt(N)))``
    - If N < floor, skips the class (logs WARNING)
    - If N <= k, samples all entities
    - Otherwise, randomly samples k entities without replacement

    Args:
        class_distribution: List of ClassRecord from get_class_nodes().
            These use CamelCase values (e.g. "FinancialInstrument").
        store: LexicalGraphStore instance for entity retrieval.
        k_min: Minimum sample size per class (default 5).
        floor: Skip classes with fewer than this many entities (default 3).
        seed: Optional random seed for reproducibility.

    Returns:
        Mapping from classification string (space-separated entity class
        format, e.g. "Financial Instrument") to list of sampled entity IDs.
        Entity IDs are in the order returned by the graph store.
    """
    rng = random.Random(seed) if seed is not None else random.Random()

    result: dict[str, list[str]] = {}

    for cls in class_distribution:
        n = cls.count

        if n < floor:
            log.warning(
                "Skipping class '%s' (entity count %d is below floor threshold %d)",
                cls.value,
                n,
                floor,
            )
            continue

        k = max(k_min, math.ceil(math.sqrt(n)))

        # Convert CamelCase SYS_Class value to space-separated entity class value
        entity_class_value = _camel_to_spaced(cls.value)

        log.debug(
            "Sampling class '%s' → '%s': N=%d, k=%d (sqrt=%d, k_min=%d)",
            cls.value,
            entity_class_value,
            n,
            k,
            math.ceil(math.sqrt(n)),
            k_min,
        )

        # Fetch ALL entities so the random sample is drawn from the
        # full population, not from the prefix the store happens to
        # return first. The store has no notion of order beyond the
        # graph's internal traversal order, so capping the fetch at
        # ``min(n, k * 3)`` (or any constant multiple of ``k``) would
        # bias the sample toward whichever entities sit at the front
        # of that traversal — undermining the "stratified random
        # sample" contract.
        fetch_limit = n
        entities = store.get_entities_by_class(
            classification=entity_class_value,
            limit=fetch_limit,
        )

        if not entities:
            log.warning(
                "Class '%s' (mapped to entity class '%s') returned zero entities despite SYS_Class count of %d",
                cls.value,
                entity_class_value,
                n,
            )
            continue

        # Extract entity IDs preserving store ordering
        all_ids = [e.entity_id for e in entities]

        if len(all_ids) <= k:
            # Sample all available entities
            sampled_ids = all_ids
            log.debug(
                "  → sampled ALL %d entities (available <= k=%d)",
                len(sampled_ids),
                k,
            )
        else:
            # Random sample without replacement, then restore original order
            sampled_indices = sorted(rng.sample(range(len(all_ids)), k))
            sampled_ids = [all_ids[i] for i in sampled_indices]
            log.debug(
                "  → sampled %d of %d available entities",
                len(sampled_ids),
                len(all_ids),
            )

        result[entity_class_value] = sampled_ids

    return result
