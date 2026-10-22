# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Class Normalizer (in-memory, first-class-wins).

Tests cover Requirement 9.1–9.8:

- Embedding concatenation format (``"{label}: {description}"``)
- Cosine top-1 lookup against a growing in-memory index
- Match band bucketing (exact ≥ 0.95, close_match ≥ 0.80, novel < 0.80)
- Empty-index path (Req 9.6) — first class is novel
- First-class-wins: same description twice → second is exact
- Per-class fault isolation: embed/resolver failures fall back to novel
- ``source_mode`` is threaded through verbatim
- Embed-text caching avoids redundant Bedrock calls

These tests use a deterministic embedding client that returns
controlled unit-length vectors so cosine math is trivially predictable.
"""

from __future__ import annotations

import logging
import math
from unittest import mock
from unittest.mock import MagicMock

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services import class_normalizer
from coa_ontology.inducer.unstructured.services.class_normalizer import (
    CLOSE_MATCH_THRESHOLD,
    EXACT_THRESHOLD,
    InMemoryClassEmbeddingIndex,
    NormalizationResult,
    _band_for_score,
    _build_embedding_text,
    _cosine,
    _IndexEntry,
    normalize,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
    normalize_string,
)

# ── Helpers ─────────────────────────────────────────────────────────────


# Test namespace + ontology used to construct expected IRIs. Kept the
# same across all tests so an assertion failure shows the real expected
# string and the test reads predictably.
_TEST_NS = "test-ns"
_TEST_ONTO = "test-onto"
_TEST_PREFIX = f"{GRAPH_BASE_URI}/namespace/{_TEST_NS}/ontology/{_TEST_ONTO}"


def _unit(*coords: float) -> list[float]:
    """Build a unit-length vector from coordinates."""
    norm = math.sqrt(sum(c * c for c in coords))
    if norm <= 0:
        return list(coords)
    return [c / norm for c in coords]


def _scripted_client(text_to_vector: dict[str, list[float]]) -> MagicMock:
    """Mock embedding client that maps prompt text → vector deterministically."""
    client = MagicMock()
    client.embed_text.side_effect = lambda text: text_to_vector[text]
    return client


def _real_resolver() -> MagicMock:
    """Return a real ``InMemoryIRIResolver`` wrapped in a ``MagicMock`` spy.

    Wrapping with ``MagicMock(wraps=...)`` delegates calls to the real
    resolver (so the IRIs that come back match the actual minting
    template — see Requirement 7.1) AND records every call so tests
    can introspect ``.resolve.call_count``,
    ``.resolve.assert_called_once_with(...)``, etc.

    This is the fix for the "opaque test IRIs masked real bugs" issue:
    using the real resolver ensures every IRI assertion in the suite
    matches the production minting convention, and the spy wrapper
    keeps every existing MagicMock-style assertion working.
    """
    return MagicMock(
        wraps=InMemoryIRIResolver(
            namespace=_TEST_NS,
            ontology_name=_TEST_ONTO,
        )
    )


def _expected_class_iri(label: str) -> str:
    """The IRI a real resolver would mint for class ``label``.

    Per Requirement 10.2, class IRIs use the literal token ``"class"``
    in the classification slot. Both the value and the classification
    are normalized (lowercased, non-alphanumeric stripped, etc.) by the
    resolver, so we apply the same normalization here.
    """
    return f"{_TEST_PREFIX}/class/{normalize_string(label)}"


# ── Pure helpers ────────────────────────────────────────────────────────


class TestBuildEmbeddingText:
    """Req 9.1, 9.8 — concatenation of label and description."""

    def test_label_and_description_concatenated_with_colon_space(self):
        assert _build_embedding_text("Person", "An individual human being.") == "Person: An individual human being."

    def test_blank_description_falls_back_to_label_only(self):
        assert _build_embedding_text("Person", "") == "Person"

    def test_whitespace_only_description_falls_back_to_label_only(self):
        assert _build_embedding_text("Person", "   ") == "Person"

    def test_description_whitespace_is_stripped(self):
        assert _build_embedding_text("Person", "  An individual.  ") == "Person: An individual."


class TestBandForScore:
    """Req 9.3, 9.4, 9.5 — match band thresholds."""

    @pytest.mark.parametrize(
        "score,expected",
        [
            (1.0, "exact"),
            (EXACT_THRESHOLD, "exact"),
            (EXACT_THRESHOLD - 1e-9, "close_match"),
            (0.85, "close_match"),
            (CLOSE_MATCH_THRESHOLD, "close_match"),
            (CLOSE_MATCH_THRESHOLD - 1e-9, "novel"),
            (0.5, "novel"),
            (0.0, "novel"),
            (-1.0, "novel"),
        ],
    )
    def test_thresholds(self, score, expected):
        assert _band_for_score(score) == expected


class TestCosine:
    """The local cosine helper handles unit and non-unit inputs."""

    def test_unit_vectors_dot_product(self):
        a = _unit(1.0, 0.0)
        b = _unit(1.0, 0.0)
        assert _cosine(a, b) == pytest.approx(1.0)

    def test_orthogonal_unit_vectors(self):
        assert _cosine(_unit(1.0, 0.0), _unit(0.0, 1.0)) == pytest.approx(0.0)

    def test_antipodal_unit_vectors(self):
        assert _cosine(_unit(1.0, 0.0), _unit(-1.0, 0.0)) == pytest.approx(-1.0)

    def test_nonunit_inputs_are_normalized(self):
        # Both vectors point along x-axis; magnitudes differ.
        assert _cosine([2.0, 0.0], [5.0, 0.0]) == pytest.approx(1.0)

    def test_empty_or_mismatched_returns_zero(self):
        assert _cosine([], [1.0]) == 0.0
        assert _cosine([1.0, 0.0], [1.0]) == 0.0

    def test_zero_norm_returns_zero(self):
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── In-memory index ─────────────────────────────────────────────────────


class TestInMemoryClassEmbeddingIndex:
    def test_empty_index(self):
        idx = InMemoryClassEmbeddingIndex()
        assert len(idx) == 0
        assert idx.top_k(_unit(1.0, 0.0)) == []

    def test_add_and_top_k(self):
        idx = InMemoryClassEmbeddingIndex()
        idx.add("X", "x desc", _unit(1.0, 0.0), "http://example.org/X")
        idx.add("Y", "y desc", _unit(0.0, 1.0), "http://example.org/Y")

        # Query along x — X should win.
        hits = idx.top_k(_unit(1.0, 0.0), k=1)
        assert len(hits) == 1
        score, entry = hits[0]
        assert entry.label == "X"
        assert entry.iri == "http://example.org/X"
        assert score == pytest.approx(1.0)

    def test_top_k_returns_sorted(self):
        idx = InMemoryClassEmbeddingIndex()
        idx.add("X", "", _unit(1.0, 0.0), "iri-X")
        idx.add("Y", "", _unit(0.0, 1.0), "iri-Y")
        idx.add("XY", "", _unit(1.0, 1.0), "iri-XY")  # 45° between X and Y

        hits = idx.top_k(_unit(1.0, 0.0), k=3)
        scores = [h[0] for h in hits]
        # Sorted descending.
        assert scores == sorted(scores, reverse=True)
        # X is closest, then XY, then Y.
        labels = [h[1].label for h in hits]
        assert labels == ["X", "XY", "Y"]

    def test_skips_entries_with_empty_vector(self):
        idx = InMemoryClassEmbeddingIndex()
        idx.add("Bad", "", [], "iri-Bad")
        idx.add("Good", "", _unit(1.0, 0.0), "iri-Good")
        hits = idx.top_k(_unit(1.0, 0.0), k=2)
        assert [h[1].label for h in hits] == ["Good"]

    def test_find_by_label_matches_lexical_variants(self):
        idx = InMemoryClassEmbeddingIndex()
        idx.add("Financial Instrument", "", _unit(1.0, 0.0), "iri-FI")

        for variant in ("financial instrument", "FINANCIAL INSTRUMENT", "  Financial Instrument  "):
            found = idx.find_by_label(variant)
            assert found is not None, variant
            assert found.iri == "iri-FI"

        assert idx.find_by_label("Unrelated") is None
        assert idx.find_by_label("   ") is None

    def test_find_by_label_is_backed_by_a_lookup_map(self):
        """The lookup must not re-normalize every stored label per call.

        Regression: ``find_by_label`` scanned ``entries`` and normalized
        each label on every call, so a deterministic match cost O(n)
        normalizations and the whole pass over a class population was
        quadratic.
        """
        idx = InMemoryClassEmbeddingIndex()
        for i in range(50):
            idx.add(f"Class {i}", "", _unit(1.0, 0.0), f"iri-{i}")

        assert len(idx.entries_by_lookup_key) == 50
        assert idx.indexed_entry_count == 50

        calls = 0
        real_key = class_normalizer.normalize_for_lookup_key

        def counting_key(label: str) -> str:
            nonlocal calls
            calls += 1
            return real_key(label)

        with mock.patch.object(class_normalizer, "normalize_for_lookup_key", counting_key):
            assert idx.find_by_label("class 49").iri == "iri-49"  # type: ignore[union-attr]
        assert calls == 1

    def test_find_by_label_keeps_the_first_entry_for_a_shared_key(self):
        idx = InMemoryClassEmbeddingIndex()
        idx.add("Compressor", "", _unit(1.0, 0.0), "iri-first")
        idx.add("COMPRESSOR", "", _unit(0.0, 1.0), "iri-second")

        found = idx.find_by_label("compressor")
        assert found is not None
        assert found.iri == "iri-first"

    def test_find_by_label_recovers_from_direct_entry_mutation(self):
        """Entries appended outside ``add`` are still matched."""
        idx = InMemoryClassEmbeddingIndex()
        idx.entries.append(_IndexEntry(label="Injected", description="", vector=_unit(1.0, 0.0), iri="iri-injected"))

        found = idx.find_by_label("injected")
        assert found is not None
        assert found.iri == "iri-injected"
        assert idx.indexed_entry_count == 1


# ── normalize() ─────────────────────────────────────────────────────────


class TestEmptyIndexFirstClassNovel:
    """Req 9.6 — first class added to an empty index is novel."""

    def test_first_class_in_empty_index_is_novel(self):
        client = _scripted_client({"X: x": _unit(1.0, 0.0)})
        resolver = _real_resolver()

        result = normalize(
            classes={"X": "x"},
            embedding_client=client,
            iri_resolver=resolver,
        )

        r = result["X"]
        assert r.band == "novel"
        assert r.class_iri == _expected_class_iri("X")
        assert r.matched_existing_iri is None
        assert r.similarity_score == 0.0


class TestExactBand:
    """Same description appearing twice — second is exact, no new mint."""

    def test_repeated_description_returns_exact(self):
        # Two pseudo-classes with same embed text → identical vectors.
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        # First call: novel.
        normalize(
            classes={"X": "same description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )
        # Second call: exact match against the entry just added.
        result2 = normalize(
            classes={"X": "same description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )
        r = result2["X"]
        assert r.band == "exact"
        assert r.class_iri == _expected_class_iri("X")
        assert r.matched_existing_iri == _expected_class_iri("X")
        assert r.similarity_score == pytest.approx(1.0)
        assert r.preferred_label == "X"
        assert r.alternative_labels == ()
        # Resolver MUST NOT be called the second time.
        assert resolver.resolve.call_count == 1
        # Index should still hold exactly one entry.
        assert len(idx) == 1


class TestVocabularyLabels:
    """Exact matches retain the preferred and observed surface forms."""

    def test_semantic_exact_match_records_alternative_label(self):
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()

        result = normalize(
            classes={
                "Compressor": "Equipment that compresses a gas.",
                "コンプレッサー": "気体を圧縮する装置。",
            },
            embedding_client=client,
            iri_resolver=resolver,
        )

        canonical = result["Compressor"]
        alternative = result["コンプレッサー"]
        assert canonical.preferred_label == "Compressor"
        assert canonical.alternative_labels == ()
        assert alternative.band == "exact"
        assert alternative.class_iri == canonical.class_iri
        assert alternative.preferred_label == "Compressor"
        assert alternative.alternative_labels == ("コンプレッサー",)

    def test_safe_lookup_key_variant_reuses_iri_without_embedding(self):
        client = _scripted_client({"MODEL: identifier": _unit(1.0, 0.0)})
        resolver = _real_resolver()

        result = normalize(
            classes={
                "MODEL": "identifier",
                "model": "different description",
            },
            embedding_client=client,
            iri_resolver=resolver,
        )

        assert result["model"].band == "exact"
        assert result["model"].class_iri == result["MODEL"].class_iri
        assert result["model"].preferred_label == "MODEL"
        assert result["model"].alternative_labels == ("model",)
        client.embed_text.assert_called_once_with("MODEL: identifier")
        resolver.resolve.assert_called_once_with("MODEL", "class")


class TestCloseMatchBand:
    """Class whose description is similar but not identical → close_match."""

    def test_similar_description_close_match_adds_to_index(self):
        # Construct two vectors at ~25° apart: cos(25°) ≈ 0.906 → close_match.
        v_x = _unit(1.0, 0.0)
        v_y = _unit(math.cos(math.radians(25)), math.sin(math.radians(25)))
        client = _scripted_client({"X: x": v_x, "Y: y": v_y})
        resolver = _real_resolver()

        idx = InMemoryClassEmbeddingIndex()
        result = normalize(
            classes={"X": "x", "Y": "y"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )

        # X is novel (empty index).
        assert result["X"].band == "novel"
        assert result["X"].class_iri == _expected_class_iri("X")

        # Y is close_match to X (cos ≈ 0.906).
        ry = result["Y"]
        assert ry.band == "close_match"
        assert ry.class_iri == _expected_class_iri("Y")
        assert ry.matched_existing_iri == _expected_class_iri("X")
        assert ry.similarity_score == pytest.approx(math.cos(math.radians(25)), abs=1e-6)

        # Both classes are in the index now.
        assert len(idx) == 2


class TestNovelBand:
    """Truly different vector → novel even when index is non-empty."""

    def test_orthogonal_class_is_novel(self):
        client = _scripted_client({"X: x": _unit(1.0, 0.0), "Y: y": _unit(0.0, 1.0)})
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        result = normalize(
            classes={"X": "x", "Y": "y"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )

        # X is novel (empty index).
        assert result["X"].band == "novel"
        # Y has a top hit (X) but cos=0.0 → novel.
        ry = result["Y"]
        assert ry.band == "novel"
        # Per spec Req 9.5: novel band MUST NOT surface a matched_existing_iri.
        assert ry.matched_existing_iri is None
        assert ry.similarity_score == pytest.approx(0.0)
        # Both classes in the index.
        assert len(idx) == 2


class TestIndexReuseAcrossCalls:
    """Passing an explicit index lets state survive across normalize() calls."""

    def test_second_call_sees_first_call_entries(self):
        client = MagicMock()
        client.embed_text.side_effect = lambda t: _unit(1.0, 0.0) if "first" in t else _unit(0.0, 1.0)
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        normalize(
            classes={"FirstClass": "the first description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )
        # Second call introduces an orthogonal class — should be novel
        # but the index now has 2 entries afterward.
        result = normalize(
            classes={"SecondClass": "another description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )
        assert result["SecondClass"].band == "novel"
        assert len(idx) == 2

    def test_no_index_passed_uses_fresh(self):
        """Without an explicit index, calls don't share state."""
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()

        # Two independent calls.
        r1 = normalize({"X": "x"}, client, resolver)
        r2 = normalize({"X": "x"}, client, resolver)

        # Both produce novel results — no index reuse.
        assert r1["X"].band == "novel"
        assert r2["X"].band == "novel"


class TestSourceModePropagation:
    """source_mode is threaded through verbatim."""

    @pytest.mark.parametrize("mode", ["none", "instance", "schema", None, "future_mode"])
    def test_mode_round_trips_through_result(self, mode):
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()

        result = normalize(
            classes={"X": "x"},
            embedding_client=client,
            iri_resolver=resolver,
            source_mode=mode,
        )
        assert result["X"].source_mode == mode


class TestPerClassFaultIsolation:
    """One class's failure must not abort the batch."""

    def test_embed_failure_is_novel_but_not_added_to_index(self, caplog):
        client = MagicMock()
        client.embed_text.side_effect = [
            RuntimeError("Bedrock unavailable"),  # for "Bad"
            _unit(1.0, 0.0),  # for "Good"
        ]
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        with caplog.at_level(logging.WARNING):
            result = normalize(
                classes={"Bad": "b", "Good": "g"},
                embedding_client=client,
                iri_resolver=resolver,
                index=idx,
            )

        assert result["Bad"].band == "novel"
        assert result["Bad"].class_iri == _expected_class_iri("Bad")
        assert result["Good"].band == "novel"
        assert result["Good"].class_iri == _expected_class_iri("Good")
        # Bad has no vector to add to the index. Good does.
        assert len(idx) == 1
        assert idx.entries[0].label == "Good"
        # WARNING logged for Bad.
        assert "Bad" in caplog.text

    def test_resolver_failure_returns_empty_class_iri(self, caplog):
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = MagicMock()
        resolver.resolve.side_effect = RuntimeError("resolver kaput")

        with caplog.at_level(logging.WARNING):
            result = normalize(
                classes={"X": "x"},
                embedding_client=client,
                iri_resolver=resolver,
            )

        r = result["X"]
        assert r.band == "novel"
        assert r.class_iri == ""
        assert "resolver" in caplog.text.lower()


class TestEmbedTextCache:
    """The index caches embed text so we don't pay Bedrock twice for the same input."""

    def test_repeated_text_uses_cache(self):
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        # Same description appearing twice in the same call (different labels).
        normalize(
            classes={"X": "shared description", "X-alias": "shared description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )

        # Wait — the embed text differs between them because the label is
        # part of the embed text. So this will result in two embed calls.
        # The cache hit case is when the SAME (label, description) pair is
        # passed twice in one call (or across calls).
        # Test that case explicitly:
        client.reset_mock()
        normalize(
            classes={"X": "shared description"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
        )
        # Second pass with identical embed text: cache hit. Also, since
        # the index already has X, we should get exact match back.
        assert client.embed_text.call_count == 0  # served from cache

    def test_cache_persists_across_calls_when_index_shared(self):
        client = MagicMock()
        client.embed_text.return_value = _unit(1.0, 0.0)
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        normalize({"X": "x"}, client, resolver, index=idx)
        # Second call with same (label, description): cache hit.
        client.reset_mock()
        normalize({"X": "x"}, client, resolver, index=idx)
        assert client.embed_text.call_count == 0


class TestEmptyInput:
    """No classes → empty result, no calls made."""

    def test_empty_dict(self):
        client = MagicMock()
        resolver = _real_resolver()
        result = normalize({}, client, resolver)
        assert result == {}
        client.embed_text.assert_not_called()
        resolver.resolve.assert_not_called()


class TestNormalizationResultDataclass:
    """Light sanity check on the dataclass surface itself."""

    def test_required_fields(self):
        r = NormalizationResult(
            class_iri="http://example.org/X",
            band="novel",
            similarity_score=0.0,
        )
        assert r.matched_existing_iri is None
        assert r.source_mode is None
        assert r.preferred_label is None
        assert r.alternative_labels == ()

    def test_full_construction(self):
        r = NormalizationResult(
            class_iri="http://example.org/X",
            band="close_match",
            similarity_score=0.85,
            matched_existing_iri=_expected_class_iri("X"),
            source_mode="schema",
        )
        assert r.band == "close_match"
        assert r.matched_existing_iri == _expected_class_iri("X")
        assert r.source_mode == "schema"
        assert r.preferred_label is None
        assert r.alternative_labels == ()


class TestIterationOrderEffect:
    """First-class-wins: iteration order determines who is the canonical IRI."""

    def test_alphabetical_ordering_first_wins(self):
        """If two near-identical classes appear in different orders,
        the first one in iteration order is the one minted, and the
        second one matches it as exact."""
        client = MagicMock()
        # Both classes have IDENTICAL embed text since the labels happen
        # to share the description prefix... actually let me make this
        # cleaner: two truly distinct labels with very close vectors.
        client.embed_text.side_effect = lambda t: _unit(1.0, 0.0) if t.startswith("A:") else _unit(1.0, 0.0)
        # Both vectors are identical → cosine=1.0 → exact band.
        resolver = _real_resolver()

        # Order A → B: A novel, B exact (matches A).
        idx_ab = InMemoryClassEmbeddingIndex()
        result_ab = normalize(
            classes={"A": "x", "B": "x"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx_ab,
        )
        assert result_ab["A"].band == "novel"
        assert result_ab["A"].class_iri == _expected_class_iri("A")
        assert result_ab["B"].band == "exact"
        assert result_ab["B"].class_iri == _expected_class_iri("A")  # reuses A's IRI

        # Reverse order B → A: now B is novel and A is exact.
        idx_ba = InMemoryClassEmbeddingIndex()
        result_ba = normalize(
            classes={"B": "x", "A": "x"},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx_ba,
        )
        assert result_ba["B"].band == "novel"
        assert result_ba["B"].class_iri == _expected_class_iri("B")
        assert result_ba["A"].band == "exact"
        assert result_ba["A"].class_iri == _expected_class_iri("B")  # reuses B's IRI


class TestResolverArgumentConvention:
    """The normalizer mints class IRIs via ``resolve(label, "class")``.

    Per Requirement 10.2 (revised), class IRIs use the literal token
    ``"class"`` in the classification slot to disambiguate them from
    A-Box individual IRIs (which use the entity's class label as the
    classification slot — Req 11.2).

    The IRI string itself comes from the resolver and is opaque from
    the normalizer's point of view; what matters is that the right
    arguments are passed.
    """

    def test_novel_path_calls_resolve_with_class_keyword(self):
        from coa_ontology.inducer.unstructured.services.class_normalizer import (
            InMemoryClassEmbeddingIndex,
            normalize,
        )

        client = _scripted_client({"Person: An individual human being.": _unit(1.0, 0.0)})
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        normalize(
            classes={"Person": "An individual human being."},
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
            source_mode="schema",
        )

        # Resolver MUST have been called exactly once with the label
        # and the literal ``"class"`` token.
        resolver.resolve.assert_called_once_with("Person", "class")

    def test_close_match_path_calls_resolve_with_class_keyword(self):
        from coa_ontology.inducer.unstructured.services.class_normalizer import (
            InMemoryClassEmbeddingIndex,
            normalize,
        )

        # Close vectors (cos ≈ 0.906) put Human in close_match band
        # against Person, so the close_match mint path is exercised.
        client = _scripted_client(
            {
                "Person: First version.": _unit(1.0, 0.0),
                "Human: Slightly different version.": _unit(math.cos(math.radians(25)), math.sin(math.radians(25))),
            }
        )
        resolver = _real_resolver()
        idx = InMemoryClassEmbeddingIndex()

        normalize(
            classes={
                "Person": "First version.",
                "Human": "Slightly different version.",
            },
            embedding_client=client,
            iri_resolver=resolver,
            index=idx,
            source_mode="schema",
        )

        # Both label-mint calls should pass the literal ``"class"`` token.
        calls = resolver.resolve.call_args_list
        assert len(calls) == 2
        assert calls[0].args == ("Person", "class")
        assert calls[1].args == ("Human", "class")
