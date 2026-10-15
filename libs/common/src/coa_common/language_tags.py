# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validation and canonicalization for the namespace-language tag subset.

COA accepts a small, repository-owned subset of BCP 47:

    language-tag = language [ "-" script ] [ "-" region ]
    language     = 2*8ALPHA
    script       = 4ALPHA
    region       = 2ALPHA / 3DIGIT

Variants, extensions, private-use and grandfathered tags are rejected. This
is syntax validation only — there is no IANA registry lookup, so a
structurally valid but unregistered tag is accepted.

The TypeScript twin lives in ``libs/ts-shared/src/language-tags.ts``; both
run the vectors in ``libs/common/tests/data/language_tag_conformance.json``.

The module also names a namespace language for a model's output-language
instruction (:func:`needs_language_instruction`, :func:`describe_language`).
Those helpers are Python-only: only the Python workers build prompts. The
instruction they feed is trusted application control text, so it interpolates
only a canonical tag and a name from a fixed table, never a stored string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

DEFAULT_LANGUAGE = "en"
"""Effective language of a namespace created without one (including legacy rows)."""

# Explicit ASCII classes: ``str.isalpha()`` and ``\w`` accept non-ASCII letters.
_TAG = re.compile(r"([A-Za-z]{2,8})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?")

# English names for a model instruction, keyed by canonical primary subtag. A
# tag whose primary subtag is absent is named by its canonical tag alone.
_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "zh": "Chinese",
}


class LanguageTagError(ValueError):
    """Raised when a value is not a language tag in COA's supported subset."""


def canonicalize_language_tag(value: str) -> str:
    """Return the canonical form of a supported language tag.

    Surrounding ASCII spaces and tabs are trimmed; any other whitespace is
    rejected. The language subtag is lower-cased, the script title-cased and an
    alphabetic region upper-cased; a numeric region is unchanged.

    Args:
        value: The tag as supplied by the caller.

    Returns:
        The canonical tag, e.g. ``"pt-BR"`` for ``" PT-br "``.

    Raises:
        LanguageTagError: If the value is empty or outside the supported
            grammar.
    """
    tag = value.strip(" \t")
    if not tag:
        raise LanguageTagError("language tag must not be empty")
    # fullmatch rather than match: ``$`` would accept a trailing newline.
    match = _TAG.fullmatch(tag)
    if match is None:
        raise LanguageTagError("language tag must have the form language[-Script][-REGION], e.g. 'en', 'ja', 'pt-BR'")
    language, script, region = match.groups()
    parts = [language.lower()]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


def primary_language(value: str) -> str:
    """Return the canonical primary language subtag of a supported tag.

    Raises:
        LanguageTagError: If the value is not a supported language tag.
    """
    return canonicalize_language_tag(value).split("-", 1)[0]


def effective_namespace_language(namespace_item: Mapping[str, Any]) -> str:
    """Return the effective language of a stored namespace metadata item.

    A namespace item written before the language field existed has no
    ``language`` attribute and reads as :data:`DEFAULT_LANGUAGE`. The caller
    must have already loaded the item: a missing item or a failed read is an
    error the caller reports, never an English default.

    Args:
        namespace_item: The ``NS#<id>`` / ``METADATA`` item from the
            namespaces table.

    Returns:
        The stored canonical language, or ``"en"`` for a legacy item.

    Raises:
        TypeError: If no item was supplied.
    """
    if namespace_item is None:
        raise TypeError("namespace item is required; a missing namespace has no language")
    return namespace_item.get("language", DEFAULT_LANGUAGE)


def needs_language_instruction(value: str) -> bool:
    """Return whether natural-language generation needs an output-language instruction.

    English namespaces, including legacy ones, keep the prompts they used
    before the namespace language existed, so any tag whose primary subtag is
    ``en`` needs none.

    Raises:
        LanguageTagError: If ``value`` is not a supported language tag.
    """
    return primary_language(value) != DEFAULT_LANGUAGE


def describe_language(value: str) -> str:
    """Name a language for a prompt, e.g. ``Japanese (BCP 47 tag `ja`)``.

    A tag whose primary subtag has no entry in the fixed name table is named
    by its canonical tag alone.

    Raises:
        LanguageTagError: If ``value`` is not a supported language tag.
    """
    tag = canonicalize_language_tag(value)
    name = _LANGUAGE_NAMES.get(primary_language(tag))
    if name:
        return f"{name} (BCP 47 tag `{tag}`)"
    return f"the language identified by the BCP 47 tag `{tag}`"
