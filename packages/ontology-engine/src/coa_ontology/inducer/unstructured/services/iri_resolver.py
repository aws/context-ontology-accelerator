# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entity-IRI Resolver for deterministic IRI assignment.

This module provides:

- :class:`IRIResolver` Protocol — the abstract interface for IRI resolution,
  allowing backends to be swapped (in-memory, persistent store, etc.).
- :class:`InMemoryIRIResolver` — the default implementation using a plain
  dict, suitable for single pipeline runs.

The resolver maps ``(string, classification)`` pairs to deterministic
canonical IRIs using a configurable IRI template.

IRI template::

    http://coa.amazon.com/namespace/{ns}/ontology/{name}/{class}/{normalized-value}

Normalization (Pattern 1: PascalCase / verbatim preservation)
-------------------------------------------------------------

Following the convention used by published ontologies like schema.org
and FOAF, the IRI's local name segments **preserve the source casing**.
Human readability lives on the ``rdfs:label`` annotation, not in the
IRI itself.

Two normalization passes operate at different stages:

- :func:`normalize_for_iri_path` — case-preserving. Used when minting
  the IRI path segment. Spaces collapse to ``-``, unicode is normalized
  to NFC, characters not allowed unescaped in IRI path segments
  (per RFC 3987) are percent-encoded. Underscores, hyphens, periods,
  and tildes are preserved verbatim. Result examples:

    - ``"FinancialInstrument"`` → ``"FinancialInstrument"`` (verbatim)
    - ``"Financial Instrument"`` → ``"Financial-Instrument"``
    - ``"OPERATES_IN"`` → ``"OPERATES_IN"``
    - ``"acme corp"`` → ``"acme-corp"``
    - ``"Treasury Bond (2024)"`` → ``"Treasury-Bond-2024"``

- :func:`normalize_for_lookup_key` — lowercasing. Used to build the
  hash key for the resolver's internal dict so case-only variants
  ``"Person"`` and ``"PERSON"`` collide on the same canonical IRI.
  The first-seen spelling is preserved in the IRI itself; subsequent
  case-only variants reuse it.

The legacy :func:`normalize_string` alias remains exported and now
delegates to :func:`normalize_for_iri_path`.

Usage::

    resolver = InMemoryIRIResolver(namespace="my-ns", ontology_name="my-onto")
    iri = resolver.resolve("Treasury Bond", "Financial Instrument")
    # → "http://coa.amazon.com/namespace/my-ns/ontology/my-onto/Financial-Instrument/Treasury-Bond"

    # Case-only variant reuses the same IRI:
    same = resolver.resolve("treasury bond", "financial instrument")
    assert same == iri
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from coa_common.constants import GRAPH_BASE_URI

from coa_ontology.inducer.services.unicode_normalization import simple_case_identity, width_only_nfkc

_IRI_TEMPLATE = f"{GRAPH_BASE_URI}/namespace/{{ns}}/ontology/{{name}}/{{cls}}/{{value}}"
_MAX_IRI_LENGTH = 2048

# Regex matching characters that are SAFE to keep verbatim in an IRI
# path segment AND are not whitespace. ASCII letters/digits, plus
# hyphen / underscore / period / tilde (RFC 3986 unreserved set), plus
# any non-ASCII letter/digit (RFC 3987 ucschar). Everything else is
# percent-encoded by ``normalize_for_iri_path``.
_PCT_ENCODE_SAFE = "-_.~"


def normalize_for_iri_path(s: str) -> str:
    """Normalize a string for use as an IRI path segment, preserving case.

    Steps:

    1. Unicode NFC normalization.
    2. Whitespace runs collapse to a single ``-``.
    3. For each remaining character: keep verbatim if it's an ASCII
       letter/digit, hyphen ``-``, underscore ``_``, period ``.``, or
       tilde ``~`` (RFC 3986 unreserved characters), OR a non-ASCII
       unicode letter/digit (RFC 3987 ucschar). Otherwise percent-encode
       it via :func:`urllib.parse.quote`.
    4. Collapse consecutive ``-`` runs into one.
    5. Trim leading/trailing ``-``.

    The output preserves case and is suitable as a path segment in an
    IRI minted by :class:`InMemoryIRIResolver`.

    Args:
        s: Input string.

    Returns:
        Normalized string, case-preserving. May be empty if the input
        contained only whitespace and/or ineligible characters.

    Examples::

        >>> normalize_for_iri_path("FinancialInstrument")
        'FinancialInstrument'
        >>> normalize_for_iri_path("Financial Instrument")
        'Financial-Instrument'
        >>> normalize_for_iri_path("OPERATES_IN")
        'OPERATES_IN'
        >>> normalize_for_iri_path("Treasury Bond (2024)")
        'Treasury-Bond-2024'
    """
    if s is None:
        return ""

    # 1. NFC normalization for canonical unicode equality.
    s = unicodedata.normalize("NFC", s)

    # 2. Collapse internal whitespace runs to a single hyphen. ``\s``
    #    matches any whitespace including unicode forms.
    s = re.sub(r"\s+", "-", s)

    # 3. Build the output character by character. We can't use a single
    #    ``re.sub`` with ``quote`` cleanly because we want to encode
    #    individual non-safe characters but pass unicode letters/digits
    #    through verbatim.
    out_chars: list[str] = []
    for ch in s:
        if ch in _PCT_ENCODE_SAFE:
            out_chars.append(ch)
        elif ch.isascii():
            if ch.isalnum():
                out_chars.append(ch)
            else:
                # Percent-encode the byte. Note: ``quote`` for a
                # single ASCII char never returns the raw byte itself
                # for chars outside ``unreserved``, so this is correct.
                out_chars.append(quote(ch, safe=""))
        else:
            # Non-ASCII: keep verbatim if it's a letter or digit
            # (RFC 3987 ucschar), otherwise percent-encode.
            if ch.isalnum():
                out_chars.append(ch)
            else:
                out_chars.append(quote(ch, safe="", encoding="utf-8"))

    out = "".join(out_chars)

    # 4. Collapse consecutive hyphens (introduced by step 2 or by
    #    encoding, e.g. ``"---"``).
    out = re.sub(r"-{2,}", "-", out)

    # 5. Trim leading/trailing hyphens.
    out = out.strip("-")

    return out


def normalize_for_lookup_key(s: str) -> str:
    """Simple-case identity variant of :func:`normalize_for_iri_path`.

    Used by :class:`InMemoryIRIResolver` to build hash keys so ordinary
    case-only variants share the first-seen IRI. Ordinary lowercase plus an
    explicit final-sigma mapping unifies Greek ``Σ``/``σ``/``ς`` without
    compatibility substitutions or expansions: persisted identities such as
    German ``Maße``/``Masse``, ``µ``/``μ``, and ``ſ``/``s`` remain distinct.
    The lookup key folds only U+FF00–U+FFEF width forms before path
    normalization, so fullwidth ASCII and halfwidth Katakana reuse the
    first-seen IRI spelling without applying destructive compatibility folds.
    """
    return simple_case_identity(normalize_for_iri_path(width_only_nfkc(s)))


# Legacy alias for backwards compatibility. Now case-preserving.
normalize_string = normalize_for_iri_path


@runtime_checkable
class IRIResolver(Protocol):
    """Protocol for Entity-IRI resolution.

    Implementations map ``(string, classification)`` pairs to
    deterministic canonical IRIs. The interface supports lookup, mint,
    resolve (lookup-or-mint), and bulk operations.

    This protocol allows swapping the backend — e.g. from in-memory
    (single pipeline run) to a persistent store (cross-run IRI reuse).

    Implementations preserve the **first-seen** case of the input
    string in the resulting IRI; case-only variants of the same input
    pair (e.g. "Person" vs "PERSON") MUST resolve to the same IRI.
    """

    def lookup(self, normalized_string: str, classification: str) -> str | None:
        """Look up an existing IRI for the given pair.

        Args:
            normalized_string: The entity/class string. May contain
                arbitrary case and whitespace; the resolver normalizes
                internally.
            classification: The classification category. Same input
                rules as ``normalized_string``.

        Returns:
            The canonical IRI if known, or None.
        """
        ...

    def mint(self, normalized_string: str, classification: str) -> str:
        """Mint a new deterministic IRI for the given pair.

        The minted IRI is stored so subsequent lookups return it.
        Minting the same pair twice (modulo case) returns the same IRI
        (idempotent).

        Returns:
            The newly minted (or previously minted) canonical IRI.
        """
        ...

    def resolve(self, normalized_string: str, classification: str) -> str:
        """Resolve a pair to an IRI: lookup first, mint if not found."""
        ...

    def bulk_resolve(self, pairs: list[tuple[str, str]]) -> list[str]:
        """Resolve multiple pairs atomically.

        If any pair fails validation, no IRIs are minted for any pair
        in the batch.

        Args:
            pairs: List of (string, classification) tuples.

        Returns:
            List of IRIs in the same order as input.

        Raises:
            ValueError: If any pair fails validation, identifying the
                failing pair by its zero-based index.
        """
        ...


class InMemoryIRIResolver:
    """In-memory IRI resolver using a dict backend.

    Suitable for single pipeline runs. All state is lost when the
    instance is garbage collected.

    For cross-run persistence, implement the :class:`IRIResolver`
    protocol with a persistent backend (e.g. DynamoDB, Neptune graph,
    file).

    The resolver uses two parallel normalizations of every input:

    - :func:`normalize_for_iri_path` builds the case-preserving path
      segment used in the minted IRI.
    - :func:`normalize_for_lookup_key` builds the lowercased hash key
      used to look up an existing IRI for the same logical pair.

    The first call to ``resolve`` (or ``mint``) for a given lowercased
    pair "wins" — its case-preserving form goes into the IRI, and
    subsequent calls with case-only variants reuse that IRI.

    Args:
        namespace: The namespace segment for the IRI template.
        ontology_name: The ontology name segment for the IRI template.
    """

    def __init__(self, namespace: str, ontology_name: str) -> None:
        """Initialize the resolver with the namespace and ontology-name IRI segments.

        Args:
            namespace: The namespace segment for the IRI template.
            ontology_name: The ontology name segment for the IRI template.

        Raises:
            ValueError: If either ``namespace`` or ``ontology_name`` is empty.
        """
        if not namespace:
            raise ValueError("namespace is required for InMemoryIRIResolver")
        if not ontology_name:
            raise ValueError("ontology_name is required for InMemoryIRIResolver")

        self._namespace = namespace
        self._ontology_name = ontology_name
        # Maps (lowercased value, lowercased classification) → minted IRI.
        # The IRI itself contains the first-seen case-preserving forms.
        self._store: dict[tuple[str, str], str] = {}

    # ── Internal helpers ────────────────────────────────────────────

    def _normalize_pair(self, raw_value: str, raw_classification: str) -> tuple[str, str, str, str]:
        """Apply both normalizations to a (value, classification) pair.

        Returns a 4-tuple ``(path_value, path_classification,
        key_value, key_classification)``.

        Raises:
            ValueError: If either normalized form is empty.
        """
        path_value = normalize_for_iri_path(raw_value)
        path_classification = normalize_for_iri_path(raw_classification)
        key_value = normalize_for_lookup_key(raw_value)
        key_classification = normalize_for_lookup_key(raw_classification)

        if not raw_classification:
            raise ValueError(f"classification must not be empty or None (got: {raw_classification!r})")
        if not path_classification:
            raise ValueError(f"classification must not be empty after normalization (got: {raw_classification!r})")
        if not path_value:
            raise ValueError(f"normalized_string must not be empty after normalization (got: {raw_value!r})")
        return path_value, path_classification, key_value, key_classification

    def _build_iri(self, path_value: str, path_classification: str) -> str:
        """Build an IRI from the template using case-preserving segments.

        Raises:
            ValueError: If the resulting IRI exceeds ``_MAX_IRI_LENGTH``.
        """
        iri = _IRI_TEMPLATE.format(
            ns=self._namespace,
            name=self._ontology_name,
            cls=path_classification,
            value=path_value,
        )
        if len(iri) > _MAX_IRI_LENGTH:
            raise ValueError(
                f"Resulting IRI exceeds {_MAX_IRI_LENGTH} characters "
                f"(length={len(iri)}) for pair "
                f"({path_value!r}, {path_classification!r})"
            )
        return iri

    # ── Public API ─────────────────────────────────────────────────

    def lookup(self, normalized_string: str, classification: str) -> str | None:
        """Look up an existing IRI for the given pair.

        Returns:
            The canonical IRI if known, or None.

        Raises:
            ValueError: If inputs are empty after normalization.
        """
        _, _, key_value, key_classification = self._normalize_pair(normalized_string, classification)
        return self._store.get((key_value, key_classification))

    def mint(self, normalized_string: str, classification: str) -> str:
        """Mint a new deterministic IRI for the given pair.

        Idempotent on case-only variants: minting ``"Person"`` then
        ``"PERSON"`` returns the same IRI both times, with the
        first-seen casing preserved in the path.

        Raises:
            ValueError: If inputs are invalid or IRI exceeds max length.
        """
        path_value, path_classification, key_value, key_classification = self._normalize_pair(
            normalized_string, classification
        )
        key = (key_value, key_classification)
        existing = self._store.get(key)
        if existing is not None:
            return existing

        iri = self._build_iri(path_value, path_classification)
        self._store[key] = iri
        return iri

    def resolve(self, normalized_string: str, classification: str) -> str:
        """Resolve a pair to an IRI: lookup first, mint if not found."""
        path_value, path_classification, key_value, key_classification = self._normalize_pair(
            normalized_string, classification
        )
        key = (key_value, key_classification)
        existing = self._store.get(key)
        if existing is not None:
            return existing

        iri = self._build_iri(path_value, path_classification)
        self._store[key] = iri
        return iri

    def bulk_resolve(self, pairs: list[tuple[str, str]]) -> list[str]:
        """Resolve multiple pairs atomically.

        Validates all pairs first. If any pair fails, no IRIs are minted.

        Returns:
            List of IRIs in the same order as input.

        Raises:
            ValueError: Identifying the failing pair by zero-based index.
        """
        # Phase 1: validate and normalize all pairs.
        normalized: list[tuple[str, str, str, str]] = []
        for i, (raw_value, raw_cls) in enumerate(pairs):
            try:
                pv, pc, kv, kc = self._normalize_pair(raw_value, raw_cls)
                # Also validate IRI length.
                self._build_iri(pv, pc)
                normalized.append((pv, pc, kv, kc))
            except ValueError as e:
                raise ValueError(f"Validation failed for pair at index {i} ({raw_value!r}, {raw_cls!r}): {e}") from e

        # Phase 2: resolve all (safe — validation passed).
        results: list[str] = []
        for path_value, path_classification, key_value, key_classification in normalized:
            key = (key_value, key_classification)
            existing = self._store.get(key)
            if existing is not None:
                results.append(existing)
            else:
                iri = self._build_iri(path_value, path_classification)
                self._store[key] = iri
                results.append(iri)

        return results
