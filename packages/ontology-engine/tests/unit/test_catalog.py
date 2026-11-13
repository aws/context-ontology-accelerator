# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ontology-catalog Pydantic schemas."""

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.catalog.schemas import (
    EmbeddingCreate,
    EmbeddingQuery,
    OntologyCreate,
    OntologyResponse,
    OntologyUpdate,
)
from pydantic import ValidationError


def test_ontology_create_minimal():
    o = OntologyCreate(uri="http://ex.org/ont", title="Test")
    assert o.ontology_type == "foundational"
    assert o.domain_tags == []
    assert o.imports == []


def test_ontology_create_full():
    o = OntologyCreate(
        uri="http://ex.org/ont",
        title="Test",
        ontology_type="induced",
        domain_tags=["finance"],
        imports=["http://schema.org/"],
    )
    assert o.ontology_type == "induced"
    assert o.domain_tags == ["finance"]


def test_ontology_create_missing_required():
    with pytest.raises(ValidationError):
        OntologyCreate(title="No URI")


def test_ontology_update_partial():
    u = OntologyUpdate(description="Updated")
    dump = u.model_dump(exclude_unset=True)
    assert dump == {"description": "Updated"}


def test_ontology_response_id_is_str():
    r = OntologyResponse(
        ontology_id="https://schema.org/",
        uri="https://schema.org/",
        title="Schema.org",
        ontology_type="foundational",
        description=None,
        version=None,
        version_iri=None,
        license=None,
        license_tier=None,
        source_registry=None,
        source_url=None,
        format=None,
        class_count=None,
        property_count=None,
        axiom_count=None,
    )
    assert r.ontology_id == "https://schema.org/"


def test_embedding_create():
    e = EmbeddingCreate(
        ontology_id="https://schema.org/",
        entity_uri="http://schema.org/Product",
        entity_type="class",
        embedding_type="lexical",
        model_id="nomic-embed-text",
        vector=[0.1, 0.2, 0.3],
        dimensions=3,
    )
    assert e.ontology_id == "https://schema.org/"


def test_embedding_query_defaults():
    q = EmbeddingQuery(
        vector=[0.1],
        embedding_type="lexical",
        model_id="test",
    )
    assert q.entity_type == "class"
    assert q.ontology_id is None
    assert q.top_k == 10
