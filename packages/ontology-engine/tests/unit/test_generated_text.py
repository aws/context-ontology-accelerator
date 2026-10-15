# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reviewed fixed-text templates (namespace-language LLD, section 2.2.6)."""

from __future__ import annotations

import string

import pytest
from coa_ontology.inducer.services.generated_text import _TEMPLATES, render

pytestmark = pytest.mark.unit


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


@pytest.mark.parametrize("key", sorted(_TEMPLATES))
def test_reviewed_languages_take_the_same_values(key: str) -> None:
    versions = _TEMPLATES[key]
    assert set(versions) == {"en", "ja"}
    assert _fields(versions["ja"]) == _fields(versions["en"])


def test_values_are_substituted_not_parsed() -> None:
    rendered = render("topic_scheme_label", "en", namespace="{namespace.__class__}")
    assert rendered == "{namespace.__class__} Topic Scheme"


def test_missing_value_names_the_template() -> None:
    with pytest.raises(KeyError, match="'fk_note' \\(ja\\): missing 'target_column'"):
        render("fk_note", "ja", table="t", column="c", target_table="u")
