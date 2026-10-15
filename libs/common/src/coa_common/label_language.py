# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Choose one display label per resource from the namespace language.

A resource can carry more than one ``rdfs:label``: an untagged legacy label
next to a COA-generated ``"空調設備"@ja``, or a tagged label a trusted ontology
artifact supplied. Every reader scoped to a namespace (Tier-2 graph tools and
T-Box context, Tier-3 ontology and graph readers, catalog graph and schema
reads) picks one the same way, so a class is not named one thing in a query
context and another on screen.

The preference comes from the namespace's immutable language, never from the
request (namespace-language LLD §2.2.5):

1. the tag COA asserts on generated literals in that namespace (§2.2.4);
2. an untagged literal;
3. any remaining literal, in deterministic lexical order.

The tag in (1) is the canonical namespace tag for the reviewed entries —
``ja`` and ``ko`` (optionally with the ``Jpan``/``Kore`` script and a region)
and exactly ``zh``. Every other namespace language asserts no tag, so its
preference starts at (2): an English namespace keeps the untagged label it
shows on ``main``, and a ``zh-Hans`` namespace, whose generated text stays
untagged, prefers untagged too.

Language never filters. A resource whose only label is untagged or in another
language keeps that label. Selection is for display only: physical identifiers
(table and column names) are chosen by the caller and are never replaced by
the selected label.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from coa_common.language_tags import DEFAULT_LANGUAGE, canonicalize_language_tag

# Primary subtag -> script subtag COA may assert with it; ``None`` means the
# entry takes no script or region qualifier at all (exactly the bare tag).
_REVIEWED_SCRIPT: dict[str, str | None] = {"ja": "Jpan", "ko": "Kore", "zh": None}

_PREFERRED, _UNTAGGED, _OTHER = 0, 1, 2


def asserted_language_tag(namespace_language: str) -> str | None:
    """Return the tag COA asserts on generated literals in this namespace.

    This is the shape half of ``languageTagCompatibilityV1`` (LLD §2.2.4): it
    decides which tag a compatible literal would carry, not whether a given
    text is compatible.

    Args:
        namespace_language: The namespace's language tag.

    Returns:
        The canonical namespace tag for a reviewed entry, otherwise ``None``
        (the namespace asserts no tags).

    Raises:
        LanguageTagError: If ``namespace_language`` is not a supported tag.
    """
    tag = canonicalize_language_tag(namespace_language)
    language, *rest = tag.split("-")
    if language == DEFAULT_LANGUAGE or language not in _REVIEWED_SCRIPT:
        return None
    allowed_script = _REVIEWED_SCRIPT[language]
    if allowed_script is None:
        return tag if not rest else None
    script = next((part for part in rest if len(part) == 4 and part.isalpha()), None)
    if script is not None and script != allowed_script:
        return None
    return tag


@dataclass(frozen=True)
class LabelPreference:
    """Label ranking for one namespace. Build with :meth:`for_namespace`.

    Attributes:
        preferred_tag: Canonical tag ranked first, or ``None`` when the
            namespace asserts no tags.
    """

    preferred_tag: str | None = None

    @classmethod
    def for_namespace(cls, namespace_language: str) -> LabelPreference:
        """Build the preference for a namespace's effective language.

        Raises:
            LanguageTagError: If the stored language is not a supported tag.
        """
        return cls(asserted_language_tag(namespace_language))

    @classmethod
    def untagged_first(cls) -> LabelPreference:
        """Preference with no preferred tag: untagged, then lexical order.

        For callers with no namespace metadata at all (a deployment without a
        namespaces table). A missing namespace item or a failed read is an
        error for the caller to report, not a reason to use this.
        """
        return cls(None)

    def rank(self, language: str | None) -> int:
        """Rank a literal's tag; lower is better. ``""``/``None`` is untagged.

        Tags compare case-insensitively (BCP 47 tags are case-insensitive and
        a store may return them as written).
        """
        if not language:
            return _UNTAGGED
        if self.preferred_tag is not None and language.casefold() == self.preferred_tag.casefold():
            return _PREFERRED
        return _OTHER

    def sort_key(self, text: str, language: str | None) -> tuple[int, str, str]:
        """Total order over ``(text, language)`` candidates for this namespace."""
        return (self.rank(language), text, (language or "").casefold())

    def pick(self, candidates: Iterable[tuple[str, str | None]]) -> str:
        """Return the best label text from ``(text, language)`` pairs.

        Empty texts are ignored; returns ``""`` when nothing is usable, which is
        what callers already substitute for a missing label. The result does
        not depend on the order the candidates arrive in.
        """
        usable = [(text, language) for text, language in candidates if text]
        if not usable:
            return ""
        return min(usable, key=lambda pair: self.sort_key(pair[0], pair[1]))[0]

    def order(self, candidates: Iterable[tuple[str, str | None]]) -> list[str]:
        """Return every distinct usable label text, best first.

        For readers that expose all labels (a vertex lookup) and want the
        preferred one first without dropping the rest.
        """
        best: dict[str, tuple[int, str, str]] = {}
        for text, language in candidates:
            if not text:
                continue
            key = self.sort_key(text, language)
            if text not in best or key < best[text]:
                best[text] = key
        return sorted(best, key=best.__getitem__)

    def pick_by_resource(self, rows: Iterable[tuple[str, str, str | None]]) -> dict[str, str]:
        """Group ``(resource_uri, text, language)`` rows and pick one label each.

        Every resource that appears in ``rows`` is in the result, even when
        its labels are all empty (it maps to ``""``), so grouping can never
        make a resource disappear.
        """
        grouped: dict[str, list[tuple[str, str | None]]] = {}
        for uri, text, language in rows:
            grouped.setdefault(uri, []).append((text, language))
        return {uri: self.pick(candidates) for uri, candidates in grouped.items()}
