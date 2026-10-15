# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-runtime contract tests for ontology grounding name keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coa_ontology.inducer.unstructured.services.class_grounding import _normalize_name_key

pytestmark = pytest.mark.unit


def _find_repo_root(start: Path) -> Path:
    """Walk up to the workspace root that owns packages/ and tests/."""
    for candidate in (start, *start.parents):
        if (candidate / "packages").is_dir() and (candidate / "tests").is_dir():
            return candidate
    raise RuntimeError(f"Could not locate repository root above {start}")


def _load_shared_cases() -> list[tuple[str, str, str]]:
    """Load and validate the fixture consumed by both Python and TypeScript."""
    fixture_path = _find_repo_root(Path(__file__).resolve()) / "tests/unit/fixtures/normalize-name-key.json"
    payload: object = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("normalize-name-key fixture must be a JSON array")

    cases: list[tuple[str, str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each normalize-name-key fixture entry must be an object")
        case_id = item.get("id")
        input_value = item.get("input")
        expected = item.get("expected")
        if not isinstance(case_id, str) or not isinstance(input_value, str) or not isinstance(expected, str):
            raise ValueError("normalize-name-key fixture fields id/input/expected must be strings")
        cases.append((case_id, input_value, expected))
    return cases


_SHARED_CASES = _load_shared_cases()


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [(input_value, expected) for _, input_value, expected in _SHARED_CASES],
    ids=[case_id for case_id, _, _ in _SHARED_CASES],
)
def test_python_name_key_matches_shared_typescript_contract(input_value: str, expected: str) -> None:
    """Python must produce every expected key also asserted by the web test."""
    assert _normalize_name_key(input_value) == expected
