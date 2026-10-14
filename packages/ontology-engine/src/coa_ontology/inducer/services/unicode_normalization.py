# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Unicode helpers for ontology identity and comparison keys."""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from urllib.parse import unquote

from coa_ontology.inducer.services.unicode_category_ranges import (
    LETTER_NUMBER_RANGES,
    MARK_RANGES,
    SYMBOL_RANGES,
)

log = logging.getLogger(__name__)

_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_JOIN_CONTROLS = frozenset({"\u200c", "\u200d"})
_TAG_CHARACTER_MIN = 0xE0020
_TAG_CHARACTER_MAX = 0xE007F


def _code_point_in_ranges(code_point: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Return whether ``code_point`` belongs to one of the sorted ranges."""
    low = 0
    high = len(ranges) - 1
    while low <= high:
        middle = (low + high) // 2
        start, end = ranges[middle]
        if code_point < start:
            high = middle - 1
        elif code_point > end:
            low = middle + 1
        else:
            return True
    return False


def _fold_width_character(character: str) -> str:
    """Return the direct ordinary-width counterpart of one U+FFxx form.

    NFKC recursively normalizes the mapped target, which is too broad here:
    FULLWIDTH MACRON becomes SPACE + COMBINING MACRON and halfwidth Hangul
    becomes conjoining Jamo that can compose into a syllable. Preserve the
    direct compatibility-Jamo/macron targets, and use NFKC for the remaining
    width forms where its result is already the direct mapping.
    """
    code_point = ord(character)
    if code_point == 0xFFA0:
        return "\u3164"
    for start, end, target_start in (
        (0xFFA1, 0xFFBE, 0x3131),
        (0xFFC2, 0xFFC7, 0x314F),
        (0xFFCA, 0xFFCF, 0x3155),
        (0xFFD2, 0xFFD7, 0x315B),
        (0xFFDA, 0xFFDC, 0x3161),
    ):
        if start <= code_point <= end:
            return chr(target_start + code_point - start)
    if code_point == 0xFFE3:
        return "\u00af"
    return unicodedata.normalize("NFKC", character)


def width_only_nfkc(value: str) -> str:
    """Fold only Unicode halfwidth/fullwidth forms, then recompose.

    Unrestricted NFKC is destructive for ontology identity: it expands symbols
    such as ``℃`` and ``™`` into ordinary text. Only U+FFxx width forms are
    folded, and exceptional targets such as FULLWIDTH MACRON and halfwidth
    Hangul stop at their direct ordinary-width mapping instead of receiving a
    second compatibility normalization. The final NFC pass composes base
    characters with folded Katakana dakuten marks.
    """
    normalized = unicodedata.normalize("NFC", value)
    folded = "".join(
        _fold_width_character(character) if "\uff00" <= character <= "\uffef" else character for character in normalized
    )
    return unicodedata.normalize("NFC", folded)


def simple_case_identity(value: str) -> str:
    """Lowercase without full case-fold compatibility substitutions.

    Full case-folding merges persisted identities such as German ``Maße`` and
    ``Masse`` (``ß`` → ``ss``), MICRO SIGN and GREEK MU, or a combining Greek
    mark and a letter. Ordinary lowercase preserves those distinctions. The one
    explicit exception is Greek final sigma, which is a contextual lowercase
    form of sigma and must share its caseless identity.
    """
    return "".join("σ" if character == "ς" else character.lower() for character in value)


def filter_letters_marks_numbers(
    value: str,
    *,
    keep_underscore: bool = False,
    keep_symbols: bool = False,
) -> str:
    """Keep identity bases and marks attached to a retained base character.

    Letters and numbers are always retained. ``keep_symbols`` additionally
    retains every Unicode ``S*`` category without compatibility-expanding it;
    this keeps identities such as ``temperature ℃`` and ``brand™`` distinct
    from plain text. Category membership comes from generated Unicode 15.0
    ranges shared with the web app rather than each runtime's bundled Unicode
    release. ZWJ/ZWNJ and Unicode TAG characters are retained after a base
    because they distinguish emoji, joined-script graphemes, and subdivision
    flags. A mark or TAG character without a retained base is discarded,
    preserving meaningful attached characters without creating identical
    format-only keys.
    """
    filtered: list[str] = []
    has_base = False

    for character in value:
        code_point = ord(character)
        if _code_point_in_ranges(code_point, LETTER_NUMBER_RANGES) or (
            keep_symbols and _code_point_in_ranges(code_point, SYMBOL_RANGES)
        ):
            filtered.append(character)
            has_base = True
            continue
        if _code_point_in_ranges(code_point, MARK_RANGES):
            if has_base:
                filtered.append(character)
            continue
        if character in _JOIN_CONTROLS and has_base:
            filtered.append(character)
            continue
        if _TAG_CHARACTER_MIN <= code_point <= _TAG_CHARACTER_MAX and has_base:
            filtered.append(character)
            continue
        if keep_underscore and character == "_":
            filtered.append(character)
        has_base = False

    return "".join(filtered)


def strict_percent_decode(value: str) -> str:
    """Decode ``value`` only when every percent escape and UTF-8 byte is valid.

    :func:`urllib.parse.unquote` partially decodes strings such as
    ``A%20B%ZZ``; browsers' ``decodeURIComponent`` reject the whole value.
    Returning the original string on either malformed syntax or invalid UTF-8
    keeps backend and browser local-name identity consistent and fail-safe.
    """
    if _MALFORMED_PERCENT_ESCAPE.search(value):
        log.debug(
            "strict_percent_decode preserved malformed input (length=%d, sha256=%s)",
            len(value),
            hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
        )
        return value
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        log.debug(
            "strict_percent_decode preserved invalid UTF-8 input (length=%d, sha256=%s)",
            len(value),
            hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
        )
        return value
