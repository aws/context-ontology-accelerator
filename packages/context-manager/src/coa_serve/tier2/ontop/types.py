# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared value types for the Ontop (VKG / NL→SPARQL) Tier-2 path.

``VectorHit`` here is the *typed-for-Tier-2* ontology entity hit — carrying the
entity ``type`` (metric / ontology_class / ontology_property) and ``entity_id``
needed to build T-Box context and route metric vs. class hits. It is distinct
from ``clients.base.VectorHit`` (the raw kNN search result: ``id``/``text``/
``score``/``metadata``) — import the intended one for the layer you're in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorHit:
    """A typed vector search result for the Ontop Tier-2 path."""

    type: str  # "metric", "ontology_class", "ontology_property"
    score: float
    entity_id: str
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
