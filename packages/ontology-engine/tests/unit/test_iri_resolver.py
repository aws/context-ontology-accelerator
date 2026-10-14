# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Entity-IRI Resolver.

Tests cover:

- :func:`normalize_for_iri_path` (case-preserving normalization)
- :func:`normalize_for_lookup_key` (lowercased normalization for hash equality)
- Resolver lookup / mint / resolve / bulk_resolve operations
- Idempotent mint
- Case-only variants share the same minted IRI (first-seen wins)
- Bulk resolve atomic failure (no partial mints)
- IRI length validation
- Protocol satisfaction

Requirements: 6.1–6.5, 7.1–7.5, 8.1–8.5
"""

from __future__ import annotations

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
    IRIResolver,
    normalize_for_iri_path,
    normalize_for_lookup_key,
    normalize_string,
)


class TestNormalizeForIRIPath:
    """:func:`normalize_for_iri_path` — case-preserving normalization."""

    def test_camel_case_preserved_verbatim(self):
        assert normalize_for_iri_path("FinancialInstrument") == "FinancialInstrument"

    def test_pascal_case_preserved_verbatim(self):
        assert normalize_for_iri_path("Person") == "Person"

    def test_upper_snake_case_preserved_verbatim(self):
        assert normalize_for_iri_path("OPERATES_IN") == "OPERATES_IN"

    def test_spaces_become_hyphens_and_case_kept(self):
        assert normalize_for_iri_path("Hello World") == "Hello-World"

    def test_spaces_to_hyphens_lowercase_unchanged(self):
        assert normalize_for_iri_path("financial instrument") == "financial-instrument"

    def test_special_chars_percent_encoded(self):
        # ``@`` is not in the unreserved set, so it gets percent-encoded.
        # ``!`` likewise.
        assert normalize_for_iri_path("hello@world!") == "hello%40world%21"

    def test_preserves_hyphens(self):
        assert normalize_for_iri_path("well-known") == "well-known"

    def test_preserves_underscores(self):
        assert normalize_for_iri_path("works_for") == "works_for"

    def test_preserves_periods(self):
        assert normalize_for_iri_path("U.S.A.") == "U.S.A."

    def test_collapses_consecutive_hyphens(self):
        assert normalize_for_iri_path("a   b") == "a-b"

    def test_trims_leading_trailing_hyphens(self):
        assert normalize_for_iri_path("-Hello-") == "Hello"
        assert normalize_for_iri_path("---test---") == "test"

    def test_unicode_nfc_normalization(self):
        # café with combining accent vs precomposed
        assert normalize_for_iri_path("caf\u0065\u0301") == normalize_for_iri_path("café")

    def test_unicode_letters_preserved_verbatim(self):
        # Non-ASCII letters are kept (RFC 3987 ucschar).
        assert normalize_for_iri_path("café") == "café"
        assert normalize_for_iri_path("Tōkyō") == "Tōkyō"

    def test_empty_after_stripping(self):
        # Symbols-only input → empty after percent-encoding still
        # leaves percent-encoded chars; the result is non-empty.
        assert normalize_for_iri_path("!!!") == "%21%21%21"

    def test_whitespace_only_yields_empty(self):
        assert normalize_for_iri_path("   ") == ""

    def test_numbers_preserved(self):
        assert normalize_for_iri_path("item 42") == "item-42"

    def test_mixed_case_with_parens_and_year(self):
        # Parens get percent-encoded, spaces collapse to hyphens, case
        # preserved.
        assert normalize_for_iri_path("Treasury Bond (2024)") == "Treasury-Bond-%282024%29"

    def test_already_normalized_pascal_case(self):
        assert normalize_for_iri_path("AlreadyClean") == "AlreadyClean"

    def test_tabs_and_newlines_become_hyphens(self):
        # Tabs/newlines are whitespace per ``\s``.
        assert normalize_for_iri_path("hello\tworld\n") == "hello-world"

    def test_idempotent_for_inputs_that_dont_trigger_pct_encoding(self):
        """Repeated normalization of a string with no characters needing
        percent-encoding is a fixed point.

        Note: idempotence does NOT hold when the input contains
        characters that triggered percent-encoding the first time —
        the resulting ``%XX`` sequences would themselves get encoded
        on a second pass (the ``%`` literally becomes ``%25``). This
        is consistent with how URL encoding works in general: callers
        should not feed already-encoded strings back into the
        normalizer. The natural inputs (PascalCase, spaced text,
        ALL_CAPS_WITH_UNDERSCORES) are idempotent and that's what
        matters in practice.
        """
        for s in [
            "FinancialInstrument",
            "Hello World",
            "OPERATES_IN",
            "well-known",
            "U.S.A.",
            "café",
        ]:
            once = normalize_for_iri_path(s)
            twice = normalize_for_iri_path(once)
            assert once == twice, f"Not idempotent for {s!r}: once={once!r} twice={twice!r}"

    def test_none_returns_empty(self):
        assert normalize_for_iri_path(None) == ""  # type: ignore[arg-type]


class TestNormalizeForLookupKey:
    """:func:`normalize_for_lookup_key` — lowercased variant."""

    def test_camel_case_lowercased(self):
        assert normalize_for_lookup_key("FinancialInstrument") == "financialinstrument"

    def test_upper_snake_case_lowercased(self):
        assert normalize_for_lookup_key("OPERATES_IN") == "operates_in"

    def test_case_only_variants_collide(self):
        a = normalize_for_lookup_key("Person")
        b = normalize_for_lookup_key("PERSON")
        c = normalize_for_lookup_key("person")
        assert a == b == c == "person"

    def test_path_and_key_differ_only_in_case(self):
        path = normalize_for_iri_path("FinancialInstrument")
        key = normalize_for_lookup_key("FinancialInstrument")
        assert path == "FinancialInstrument"
        assert key == "financialinstrument"
        assert key == path.lower()

    def test_distinct_sharp_s_word_does_not_collide(self):
        assert normalize_for_lookup_key("Maße") != normalize_for_lookup_key("Masse")

    def test_simple_case_variants_share_lookup_key(self):
        assert normalize_for_lookup_key("Σ") == normalize_for_lookup_key("σ") == normalize_for_lookup_key("ς")

    def test_long_s_remains_distinct_from_ascii_s(self):
        assert normalize_for_lookup_key("ſ") != normalize_for_lookup_key("s")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("A￣B", "A¯B", id="fullwidth-macron"),
            pytest.param("ﾡￂ", "ㄱㅏ", id="halfwidth-hangul"),
            pytest.param("Ꭰ", "ꭰ", id="cherokee-case"),
            pytest.param("HO\u0308HE", "HÖHE", id="canonical-equivalence"),
        ],
    )
    def test_width_case_and_canonical_variants_share_lookup_key(self, left: str, right: str):
        assert normalize_for_lookup_key(left) == normalize_for_lookup_key(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("👩‍🔬", "👩🔬", id="zwj-sequence"),
            pytest.param("a‌b", "ab", id="zwnj-sequence"),
            pytest.param("µ", "μ", id="micro-sign-vs-greek-mu"),
            pytest.param("ϐ", "β", id="beta-symbol-vs-beta"),
            pytest.param("aͅ", "a", id="greek-combining-mark"),
            pytest.param("ſ", "s", id="long-s"),
        ],
    )
    def test_distinct_unicode_identities_do_not_share_lookup_key(self, left: str, right: str):
        assert normalize_for_lookup_key(left) != normalize_for_lookup_key(right)

    def test_width_variants_share_lookup_key_without_changing_path_spelling(self):
        assert normalize_for_lookup_key("Ａ") == normalize_for_lookup_key("A") == "a"
        assert normalize_for_lookup_key("ﾒｰｶｰ") == normalize_for_lookup_key("メーカー") == "メーカー"
        assert normalize_for_iri_path("Ａ") == "Ａ"

    def test_dotless_i_remains_distinct_and_cherokee_uses_lowercase_identity(self):
        assert normalize_for_lookup_key("ı") != normalize_for_lookup_key("i")
        assert normalize_for_lookup_key("Ꭰ") == normalize_for_lookup_key("ꭰ") == "ꭰ"


class TestNormalizeStringAlias:
    """The legacy :func:`normalize_string` alias delegates to the path form."""

    def test_alias_is_case_preserving(self):
        assert normalize_string is normalize_for_iri_path
        assert normalize_string("FinancialInstrument") == "FinancialInstrument"


class TestInMemoryIRIResolverInit:
    """Constructor validation."""

    def test_requires_namespace(self):
        with pytest.raises(ValueError, match="namespace is required"):
            InMemoryIRIResolver(namespace="", ontology_name="test")

    def test_requires_ontology_name(self):
        with pytest.raises(ValueError, match="ontology_name is required"):
            InMemoryIRIResolver(namespace="test", ontology_name="")

    def test_satisfies_protocol(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        assert isinstance(resolver, IRIResolver)


class TestLookup:
    """``lookup`` operation."""

    def test_returns_none_for_unknown_pair(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        result = resolver.lookup("Person", "class")
        assert result is None

    def test_returns_iri_after_mint(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        minted = resolver.mint("Person", "class")
        looked_up = resolver.lookup("Person", "class")
        assert looked_up == minted

    def test_lookup_finds_via_case_only_variant(self):
        """Lookup with a case-only variant of a previously minted pair
        finds it (lowercased equality on the lookup key)."""
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        resolver.mint("Person", "class")
        # All of these case-only variants find the same IRI.
        assert resolver.lookup("person", "class") is not None
        assert resolver.lookup("PERSON", "class") is not None
        assert resolver.lookup("Person", "CLASS") is not None

    def test_raises_on_empty_after_normalization(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        with pytest.raises(ValueError, match="must not be empty after normalization"):
            # Whitespace-only collapses to empty path segment.
            resolver.lookup("   ", "class")

    def test_raises_on_empty_classification(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        with pytest.raises(ValueError, match="classification must not be empty"):
            resolver.lookup("Person", "")


class TestMint:
    """``mint`` operation."""

    def test_produces_pascal_case_preserving_iri(self):
        resolver = InMemoryIRIResolver(namespace="my-ns", ontology_name="my-onto")
        iri = resolver.mint("Treasury Bond", "Financial Instrument")
        # Spaces become hyphens, case preserved.
        assert iri == (f"{GRAPH_BASE_URI}/namespace/my-ns/ontology/my-onto/Financial-Instrument/Treasury-Bond")

    def test_produces_camel_case_preserving_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri = resolver.mint("FinancialInstrument", "class")
        assert iri == (f"{GRAPH_BASE_URI}/namespace/ns/ontology/onto/class/FinancialInstrument")

    def test_produces_upper_snake_preserving_iri_for_predicate(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri = resolver.mint("OPERATES_IN", "objectProperty")
        assert iri == (f"{GRAPH_BASE_URI}/namespace/ns/ontology/onto/objectProperty/OPERATES_IN")

    def test_idempotent(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri1 = resolver.mint("Person", "class")
        iri2 = resolver.mint("Person", "class")
        assert iri1 == iri2

    def test_idempotent_across_case_variants_first_seen_wins(self):
        """First spelling persists; case-only variants reuse the same IRI.

        This is the intended behaviour described in the
        :class:`InMemoryIRIResolver` docstring: lowercased lookup,
        case-preserving IRI minting, first-seen-wins.
        """
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        first = resolver.mint("Person", "class")
        # Subsequent mints with case-only variants return the SAME IRI;
        # the first-seen ``"Person"`` is preserved in the path segment.
        assert resolver.mint("person", "class") == first
        assert resolver.mint("PERSON", "class") == first
        # The IRI itself contains the first-seen casing.
        assert "/class/Person" in first

    def test_different_pairs_get_different_iris(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri1 = resolver.mint("Person", "class")
        iri2 = resolver.mint("Company", "class")
        assert iri1 != iri2

    def test_distinct_sharp_s_words_get_different_iris(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        assert resolver.mint("Maße", "property") != resolver.mint("Masse", "property")

    def test_greek_sigma_variants_reuse_first_seen_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        first = resolver.mint("Σ", "property")
        assert resolver.mint("σ", "property") == first
        assert resolver.mint("ς", "property") == first

    def test_width_variants_reuse_first_seen_iri_spelling(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        first = resolver.mint("Ａ", "class")
        assert resolver.mint("A", "class") == first
        assert first.endswith("/class/Ａ")

    def test_raises_on_whitespace_only_input(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        with pytest.raises(ValueError, match="must not be empty after normalization"):
            resolver.mint("   ", "class")

    def test_raises_on_empty_classification(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        with pytest.raises(ValueError, match="classification must not be empty"):
            resolver.mint("Person", "")

    def test_raises_on_iri_exceeding_max_length(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        long_string = "a" * 2100
        with pytest.raises(ValueError, match="exceeds 2048 characters"):
            resolver.mint(long_string, "class")


class TestResolve:
    """``resolve`` (lookup-or-mint) operation."""

    def test_mints_when_not_found(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri = resolver.resolve("Person", "class")
        assert iri.endswith("/class/Person")

    def test_returns_existing_on_second_call(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri1 = resolver.resolve("Person", "class")
        iri2 = resolver.resolve("Person", "class")
        assert iri1 == iri2

    def test_lookup_finds_resolved_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        resolved = resolver.resolve("Person", "class")
        looked_up = resolver.lookup("Person", "class")
        assert looked_up == resolved

    def test_case_only_variants_share_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        first = resolver.resolve("Person", "class")
        # PERSON and person resolve to the SAME IRI as Person.
        assert resolver.resolve("PERSON", "class") == first
        assert resolver.resolve("person", "class") == first

    def test_raises_on_invalid_input(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        with pytest.raises(ValueError):
            resolver.resolve("", "class")


class TestBulkResolve:
    """``bulk_resolve`` with atomic failure semantics."""

    def test_resolves_multiple_pairs(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        pairs = [
            ("Person", "class"),
            ("Company", "class"),
            ("Location", "class"),
        ]
        results = resolver.bulk_resolve(pairs)
        assert len(results) == 3
        assert all(r.startswith("http://") for r in results)
        assert results[0].endswith("/class/Person")
        assert results[1].endswith("/class/Company")
        assert results[2].endswith("/class/Location")

    def test_preserves_order(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        pairs = [("Zebra", "animal"), ("Apple", "fruit")]
        results = resolver.bulk_resolve(pairs)
        assert "Zebra" in results[0]
        assert "Apple" in results[1]

    def test_atomic_failure_on_whitespace_only_value(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        pairs = [
            ("ValidOne", "class"),
            ("   ", "class"),  # Whitespace-only → empty after normalization.
            ("ValidTwo", "class"),
        ]
        with pytest.raises(ValueError, match="index 1"):
            resolver.bulk_resolve(pairs)

        # Verify nothing was minted.
        assert resolver.lookup("ValidOne", "class") is None
        assert resolver.lookup("ValidTwo", "class") is None

    def test_atomic_failure_on_empty_classification(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        pairs = [("Person", "class"), ("Company", "")]
        with pytest.raises(ValueError, match="index 1"):
            resolver.bulk_resolve(pairs)

        assert resolver.lookup("Person", "class") is None

    def test_empty_list_returns_empty(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        results = resolver.bulk_resolve([])
        assert results == []

    def test_reuses_existing_iris(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        existing = resolver.mint("Person", "class")
        results = resolver.bulk_resolve([("Person", "class"), ("Company", "class")])
        assert results[0] == existing
        assert results[1] != existing


class TestIRIFormat:
    """The IRI template format end-to-end."""

    def test_iri_template_structure_pascal_value(self):
        resolver = InMemoryIRIResolver(namespace="test-ns", ontology_name="test-onto")
        iri = resolver.resolve("JohnSmith", "Person")
        assert iri == (f"{GRAPH_BASE_URI}/namespace/test-ns/ontology/test-onto/Person/JohnSmith")

    def test_iri_template_structure_spaced_value(self):
        resolver = InMemoryIRIResolver(namespace="test-ns", ontology_name="test-onto")
        iri = resolver.resolve("John Smith", "Person")
        assert iri == (f"{GRAPH_BASE_URI}/namespace/test-ns/ontology/test-onto/Person/John-Smith")

    def test_classification_case_preserved_in_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri = resolver.resolve("BondX", "Financial Instrument")
        assert "/Financial-Instrument/" in iri

    def test_value_case_preserved_in_iri(self):
        resolver = InMemoryIRIResolver(namespace="ns", ontology_name="onto")
        iri = resolver.resolve("Treasury Bond", "instrument")
        assert "/Treasury-Bond" in iri
