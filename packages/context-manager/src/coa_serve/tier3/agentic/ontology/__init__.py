# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Agentic Retriever ontology-source abstraction (graph or ``.ttl`` file).

Importing this package never imports ``graphrag_toolkit``; ``rdflib`` is imported
lazily by :class:`OntologyFileSource`.
"""

from __future__ import annotations

from .source import (
    Ontology,
    OntologyFileSource,
    OntologyGraphSource,
    OntologySource,
)

__all__ = [
    "Ontology",
    "OntologySource",
    "OntologyGraphSource",
    "OntologyFileSource",
]
