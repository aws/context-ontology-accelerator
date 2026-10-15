# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Predicate normalization and XSD type inference utilities.

This module provides:
- ``normalize_predicate(s)`` — normalizes predicate labels for consistent
  property naming in the induced ontology.
- ``infer_xsd_type(values)`` — infers the most specific XSD datatype from
  a list of observed literal values.
- ``select_canonical_label(originals)`` — picks the canonical label from
  a frequency-counted set of original predicate strings.

Usage::

    from coa_ontology.inducer.unstructured.services.normalizers import (
        normalize_predicate,
        infer_xsd_type,
        select_canonical_label,
    )

    normalize_predicate("OPERATES_IN")  # → "operates_in"
    infer_xsd_type(["42", "7", "100"])  # → "xsd:integer"
    select_canonical_label({"OPERATES_IN": 5, "operates_in": 2})  # → "OPERATES_IN"
"""

from __future__ import annotations

import re
import unicodedata

from coa_ontology.inducer.services.unicode_normalization import (
    filter_letters_marks_numbers,
    simple_case_identity,
    width_only_nfkc,
)


def normalize_predicate(s: str) -> str:
    """Normalize a predicate label into a non-destructive aggregation key.

    Letters, numbers, and marks attached to them are retained in every script;
    whitespace becomes underscores and other characters are removed. Fullwidth
    and halfwidth forms are normalized so legacy Katakana and fullwidth ASCII
    share their ordinary spelling.

    Compatibility folding is deliberately limited to the Unicode Halfwidth and
    Fullwidth Forms block. Applying NFKC to the whole value turns symbols such as
    ``℃`` and ``™`` into the letters ``C`` and ``TM`` and silently merges distinct
    predicates. Ordinary lowercase is likewise deliberate: it preserves
    compatibility distinctions such as MICRO SIGN versus GREEK MU and avoids
    expansions that would merge distinct words such as German ``Maße`` and
    ``Masse``. Greek final sigma is the only explicit caseless mapping. The
    resolver uses the same identity contract.

    Idempotent: ``normalize_predicate(normalize_predicate(s)) == normalize_predicate(s)``.

    Args:
        s: Raw predicate string.

    Returns:
        Normalized predicate string.
    """
    s = width_only_nfkc(s)
    # Any whitespace to underscores, not just U+0020: an ideographic space is an
    # ordinary word separator in Japanese and Chinese text.
    s = re.sub(r"\s+", "_", s)
    s = filter_letters_marks_numbers(s, keep_underscore=True, keep_symbols=True)
    # Ordinary lowercase plus the explicit final-sigma mapping avoids full
    # case-fold compatibility substitutions and expansions such as ß → ss.
    # Lowercasing can emit a combining mark (for example, Turkish capital dotted
    # I), so recompose and filter once more to enforce the attached-mark invariant.
    s = unicodedata.normalize("NFC", simple_case_identity(s))
    s = filter_letters_marks_numbers(s, keep_underscore=True, keep_symbols=True)
    s = re.sub(r"_{2,}", "_", s)
    # Recompose after punctuation removal. For example, removing the hyphen from
    # Hangul Jamo ``ᄀ-ᅡ`` must yield ``가`` in the same pass.
    return unicodedata.normalize("NFC", s.strip("_"))


# ── XSD type inference ──────────────────────────────────────────────────

# Patterns for type detection (order matters — more specific first)
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_RE = re.compile(r"^[+-]?\d+\.\d+$")
_BOOLEAN_RE = re.compile(r"^(true|false)$", re.IGNORECASE)
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _detect_type(value: str) -> str:
    """Detect the XSD type of a single string value.

    Returns one of: xsd:integer, xsd:decimal, xsd:boolean,
    xsd:dateTime, xsd:date, xsd:string.
    """
    value = value.strip()
    if not value:
        return "xsd:string"
    if _BOOLEAN_RE.match(value):
        return "xsd:boolean"
    if _INTEGER_RE.match(value):
        return "xsd:integer"
    if _DECIMAL_RE.match(value):
        return "xsd:decimal"
    if _DATETIME_RE.match(value):
        return "xsd:dateTime"
    if _DATE_RE.match(value):
        return "xsd:date"
    return "xsd:string"


def infer_xsd_type(values: list[str]) -> str:
    """Infer the most specific XSD datatype from observed literal values.

    Checks each value against regex patterns for integer, decimal,
    boolean, date, and dateTime. Returns the unanimous type if all
    values match the same type, otherwise returns xsd:string.

    Args:
        values: List of literal string values to analyze.

    Returns:
        XSD type URI string (e.g. "xsd:integer", "xsd:string").
    """
    if not values:
        return "xsd:string"

    types = {_detect_type(v) for v in values if v.strip()}

    if not types:
        return "xsd:string"

    if len(types) == 1:
        return types.pop()

    # Mixed types → default to string
    return "xsd:string"


# ── Canonical label selection ───────────────────────────────────────────


def select_canonical_label(originals: dict[str, int]) -> str:
    """Select the canonical label from a frequency-counted set.

    Picks the original string with the highest count. Breaks ties
    by lexicographic order (smallest string wins).

    Args:
        originals: Mapping from original predicate string to its
            observation count.

    Returns:
        The canonical label string.

    Raises:
        ValueError: If originals is empty.
    """
    if not originals:
        raise ValueError("Cannot select canonical label from empty dict")

    # Sort by (-count, label) to get highest count first, then lexicographic
    return sorted(originals.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
