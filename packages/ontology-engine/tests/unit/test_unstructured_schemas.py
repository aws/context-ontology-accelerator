# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``UnstructuredInductionRequest`` schema validation.

Validates the request-side Pydantic model declared in
``coa_ontology.inducer.unstructured.schemas``. The
``UnstructuredInductionConfig`` and ``UnstructuredInductionReport``
models are exercised by other tests (the orchestrator and the router
suites) — this file scopes to the HTTP-input model since that's the
one Requirements 4.1 / 4.2 enumerate field-by-field.

References:
    Requirements 4.1, 4.2 of
    ``.kiro/specs/unstructured-ontology-induction-api/requirements.md``.
"""

from __future__ import annotations

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.schemas import (
    SamplingParams,
    UnstructuredInductionRequest,
)
from pydantic import ValidationError

# A minimal valid kwargs dict shared across tests. Each negative test
# pops one required key to assert the rejection.
_VALID_KWARGS: dict = {
    "name": "test-business-news",
    "graph_arn": "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc1234567",
    "ontology_uri_prefix": (f"{GRAPH_BASE_URI}/namespace/test-e2e/ontology/test-business-news"),
}


# ── Required-field enforcement (Requirement 4.2) ────────────────────────


@pytest.mark.parametrize("missing_field", ["name", "graph_arn", "ontology_uri_prefix"])
def test_required_fields_enforced(missing_field: str):
    """Each of ``name``, ``graph_arn``, ``ontology_uri_prefix`` is required.

    Pydantic surfaces missing required fields as ``ValidationError``;
    when used by FastAPI this becomes HTTP 422 — that's what
    "HTTP 422-equivalent" means in the task spec. We exercise the
    model directly so we don't need an HTTP layer.
    """
    kwargs = {k: v for k, v in _VALID_KWARGS.items() if k != missing_field}
    with pytest.raises(ValidationError) as exc_info:
        UnstructuredInductionRequest(**kwargs)

    # The error message should name the missing field so a developer
    # reading a 422 response can act on it.
    err_str = str(exc_info.value)
    assert missing_field in err_str, (
        f"ValidationError message did not mention the missing field {missing_field!r}. Full error:\n{err_str}"
    )


# ── Defaults applied to a minimal valid request (Requirement 4.2) ───────


def test_minimal_request_applies_defaults():
    """A request with only the three required fields fills in every default."""
    req = UnstructuredInductionRequest(**_VALID_KWARGS)

    # Required fields round-tripped intact.
    assert req.name == _VALID_KWARGS["name"]
    assert req.graph_arn == _VALID_KWARGS["graph_arn"]
    assert req.ontology_uri_prefix == _VALID_KWARGS["ontology_uri_prefix"]

    # Documented defaults from Requirement 4.2.
    assert req.namespace == "default"
    assert req.label is None
    assert req.confidence_threshold == 0.80
    assert isinstance(req.sampling, SamplingParams)
    assert req.sampling.k_min == 5
    assert req.sampling.floor == 3
    assert req.description_mode == "schema"
    assert req.include_individuals is False
    assert req.entailment_enabled is False


# ── ``sampling`` coercion (Requirement 4.2) ─────────────────────────────


def test_sampling_coercion_from_dict():
    """Pydantic coerces a plain ``sampling`` dict into ``SamplingParams``."""
    req = UnstructuredInductionRequest(
        sampling={"k_min": 10, "floor": 4},
        **_VALID_KWARGS,
    )
    assert isinstance(req.sampling, SamplingParams)
    assert req.sampling.k_min == 10
    assert req.sampling.floor == 4


def test_sampling_coercion_from_instance():
    """An already-constructed ``SamplingParams`` is accepted as-is."""
    sampling = SamplingParams(k_min=10, floor=4)
    req = UnstructuredInductionRequest(sampling=sampling, **_VALID_KWARGS)
    assert isinstance(req.sampling, SamplingParams)
    assert req.sampling.k_min == 10
    assert req.sampling.floor == 4


# ── ``description_mode`` Literal validation (Requirement 4.2) ───────────


@pytest.mark.parametrize("mode", ["label_only", "instance", "schema"])
def test_description_mode_accepts_valid_literals(mode: str):
    """All three documented Literal values for ``description_mode`` are accepted."""
    req = UnstructuredInductionRequest(description_mode=mode, **_VALID_KWARGS)
    assert req.description_mode == mode


@pytest.mark.parametrize("invalid_mode", ["freeform", "", "Schema", "label-only"])
def test_description_mode_rejects_invalid_literal(invalid_mode: str):
    """Values outside the Literal set raise ``ValidationError``."""
    with pytest.raises(ValidationError) as exc_info:
        UnstructuredInductionRequest(description_mode=invalid_mode, **_VALID_KWARGS)
    # The error message should reference the offending field.
    assert "description_mode" in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_prefix",
    ["http://<script>alert(1)</script>", 'https://example.com/a"x', "https://example.com/o?q=1", "javascript:alert(1)"],
)
def test_ontology_uri_prefix_rejects_unsafe_values(bad_prefix: str):
    with pytest.raises(ValidationError, match="ontology_uri_prefix"):
        UnstructuredInductionRequest(**{**_VALID_KWARGS, "ontology_uri_prefix": bad_prefix})
