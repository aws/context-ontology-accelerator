# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""RDF language tags for COA-generated literals (``languageTagCompatibilityV1``).

A language tag claims that one lexical form is written in that language. The
namespace language is the only authority for which tag to use; this module never
detects a language from text. It only decides whether a literal COA generated is
lexically compatible with the configured namespace language, and it fails closed:
a false tag is worse than an omitted one, and untagged literals stay readable.

The rules (namespace-language LLD, section 2.2.4):

1. Callers pass only COA-generated natural-language text. Physical identifiers,
   copied source-authored values and values with missing or mixed provenance
   never reach :func:`generated_literal`; structured metadata needs a
   ``generatedFields`` proof (:func:`catalog_literal`).
2. A namespace whose primary subtag is ``en`` asserts no new tags, so English
   and legacy namespaces keep the untagged output they had before.
3. The text must contain a character of a script that is evidence for the
   language, and every letter must belong to a permitted script. Digits,
   punctuation, symbols and spaces belong to no script and are neutral.
4. ASCII letters are Latin, and Latin is not permitted for any reviewed entry,
   so an identifier embedded in prose (``PLC制御盤``) leaves the literal untagged.
5. Only reviewed entries are tagged: ``ja``, ``ko`` and exactly ``zh``. For
   ``ja`` and ``ko`` a script subtag must be ``Jpan`` or ``Kore``; ``zh-Hans``,
   ``zh-Hant`` and region-qualified Chinese stay untagged.

Language tags already present in a trusted ontology artifact are not this
module's concern: nothing here rewrites an existing literal.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from coa_common.language_tags import DEFAULT_LANGUAGE, LanguageTagError, canonicalize_language_tag
from rdflib import Literal, URIRef

_HAN = "Han"
_HIRAGANA = "Hiragana"
_KATAKANA = "Katakana"
_HANGUL = "Hangul"
_LATIN = "Latin"


@dataclass(frozen=True)
class _ReviewedEntry:
    evidence: frozenset[str]
    permitted: frozenset[str]
    # Script subtag the tag may carry; ``None`` means no script subtag is allowed.
    script: str | None
    # Whether a region subtag is allowed.
    region: bool


_REVIEWED: dict[str, _ReviewedEntry] = {
    "ja": _ReviewedEntry(
        evidence=frozenset({_HAN, _HIRAGANA, _KATAKANA}),
        permitted=frozenset({_HAN, _HIRAGANA, _KATAKANA}),
        script="Jpan",
        region=True,
    ),
    "ko": _ReviewedEntry(
        evidence=frozenset({_HANGUL}),
        permitted=frozenset({_HANGUL, _HAN}),
        script="Kore",
        region=True,
    ),
    # Han alone cannot prove a Chinese script or region qualifier.
    "zh": _ReviewedEntry(evidence=frozenset({_HAN}), permitted=frozenset({_HAN}), script=None, region=False),
}

# Letters whose Unicode names do not start with their script's name.
_HAN_LETTERS = frozenset("々〆〻")


def _script_of(char: str) -> str | None:
    """Return the script of a letter, or ``None`` for a script-neutral character.

    Only letters carry script evidence. Digits, punctuation, symbols, spaces and
    combining marks (such as a combining voiced-sound mark) are neutral. A
    letter of any script this module does not name is reported by the first
    word of its Unicode name, which no reviewed entry permits.
    """
    if not char.isalpha():
        return None
    if char in _HAN_LETTERS:
        return _HAN
    name = unicodedata.name(char, "")
    if name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")):
        return _HAN
    if name.startswith("HIRAGANA"):
        return _HIRAGANA
    # Includes the prolonged sound mark ("KATAKANA-HIRAGANA PROLONGED SOUND MARK").
    if name.startswith(("KATAKANA", "HALFWIDTH KATAKANA")):
        return _KATAKANA
    if name.startswith(("HANGUL", "HALFWIDTH HANGUL")):
        return _HANGUL
    if name.startswith(("LATIN", "FULLWIDTH LATIN")):
        return _LATIN
    return name.split(" ", 1)[0] if name else "Unknown"


def _split_tag(tag: str) -> tuple[str, str | None, str | None]:
    """Split a canonical tag into ``(language, script, region)``."""
    language, *rest = tag.split("-")
    script = next((part for part in rest if len(part) == 4 and part.isalpha()), None)
    region = next((part for part in rest if part != script), None)
    return language, script, region


def compatible_language_tag(text: str, namespace_language: str) -> str | None:
    """Return the tag to assert on a COA-generated literal, or ``None``.

    Args:
        text: The literal's lexical form. The caller has already established
            that COA generated it.
        namespace_language: The namespace's canonical language tag.

    Returns:
        The canonical namespace-language tag when the text is compatible with a
        reviewed entry, otherwise ``None`` (emit the literal untagged).
    """
    try:
        tag = canonicalize_language_tag(namespace_language)
    except LanguageTagError:
        # Not reachable from a route: the namespace language was canonicalized
        # when it was resolved. Fail closed without a per-literal log line.
        return None
    language, script, region = _split_tag(tag)
    if language == DEFAULT_LANGUAGE:
        return None
    entry = _REVIEWED.get(language)
    if entry is None:
        return None
    if script is not None and script != entry.script:
        return None
    if region is not None and not entry.region:
        return None
    scripts = {s for s in map(_script_of, text) if s is not None}
    has_evidence = bool(scripts & entry.evidence)
    only_permitted = scripts <= entry.permitted
    return tag if has_evidence and only_permitted else None


def generated_literal(text: str, namespace_language: str, *, datatype: URIRef | None = None) -> Literal:
    """Build the literal for COA-generated natural-language text.

    A compatible text becomes a language-tagged literal. Otherwise the literal
    keeps exactly the shape it has without namespace language: ``datatype`` when
    given, else a plain literal.
    """
    tag = compatible_language_tag(text, namespace_language)
    if tag is not None:
        return Literal(text, lang=tag)
    return Literal(text, datatype=datatype) if datatype is not None else Literal(text)


def catalog_literal(text: str, namespace_language: str, *, generated: bool) -> Literal:
    """Build the literal for a structured metadata value.

    Args:
        text: A value copied from catalog metadata.
        namespace_language: The namespace's canonical language tag.
        generated: Whether the field's ``generatedFields`` proof covers this
            value. Without it the value may be source- or steward-authored and
            stays untagged whatever its script.
    """
    return generated_literal(text, namespace_language) if generated else Literal(text)
