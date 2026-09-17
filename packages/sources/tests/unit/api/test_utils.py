# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_sources.utils — merge_extraction_config."""

from __future__ import annotations

import pytest
from coa_common.constants import EXTRACTION_DEFAULTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal Pydantic-like model stub with model_dump."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self, exclude_none: bool = False):
        if exclude_none:
            return {k: v for k, v in self._data.items() if v is not None}
        return dict(self._data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMergeExtractionConfigNone:
    def test_none_returns_defaults(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config(None)
        assert result == dict(EXTRACTION_DEFAULTS)

    def test_returns_copy_not_original(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config(None)
        result["extraction_mode"] = "mutated"
        # Calling again should still return the original defaults
        result2 = merge_extraction_config(None)
        assert result2["extraction_mode"] == EXTRACTION_DEFAULTS["extraction_mode"]


@pytest.mark.unit
class TestMergeExtractionConfigDict:
    def test_empty_dict_returns_defaults(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({})
        assert result == dict(EXTRACTION_DEFAULTS)

    def test_dict_with_none_values_ignored(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"extraction_mode": None})
        assert result["extraction_mode"] == EXTRACTION_DEFAULTS["extraction_mode"]

    def test_dict_overrides_extraction_mode_continuous(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"extraction_mode": "continuous"})
        assert result["extraction_mode"] == "continuous"

    def test_dict_overrides_extraction_mode_separated(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"extraction_mode": "separated"})
        assert result["extraction_mode"] == "separated"

    def test_dict_overrides_use_batch_inference(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"use_batch_inference": True})
        assert result["use_batch_inference"] is True

    def test_dict_overrides_enable_versioning(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"enable_versioning": False})
        assert result["enable_versioning"] is False

    def test_dict_overrides_multiple_fields(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config(
            {
                "extraction_mode": "separated",
                "use_batch_inference": True,
                "enable_versioning": False,
            }
        )
        assert result["extraction_mode"] == "separated"
        assert result["use_batch_inference"] is True
        assert result["enable_versioning"] is False

    def test_dict_invalid_extraction_mode_raises(self):
        from coa_sources.utils import merge_extraction_config

        with pytest.raises(ValueError, match="Invalid extractionMode"):
            merge_extraction_config({"extraction_mode": "invalid-mode"})

    def test_dict_preserves_unrelated_defaults(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config({"extraction_mode": "separated"})
        # All other defaults should still be present
        for key in EXTRACTION_DEFAULTS:
            assert key in result


@pytest.mark.unit
class TestMergeExtractionConfigPydanticModel:
    def test_model_with_no_overrides_returns_defaults(self):
        from coa_sources.utils import merge_extraction_config

        model = _FakeConfig({})
        result = merge_extraction_config(model)
        assert result == dict(EXTRACTION_DEFAULTS)

    def test_model_overrides_extraction_mode(self):
        from coa_sources.utils import merge_extraction_config

        model = _FakeConfig({"extraction_mode": "separated"})
        result = merge_extraction_config(model)
        assert result["extraction_mode"] == "separated"

    def test_model_none_values_excluded(self):
        from coa_sources.utils import merge_extraction_config

        model = _FakeConfig({"extraction_mode": None, "use_batch_inference": True})
        result = merge_extraction_config(model)
        # None value should be excluded, so extraction_mode stays as default
        assert result["extraction_mode"] == EXTRACTION_DEFAULTS["extraction_mode"]
        assert result["use_batch_inference"] is True

    def test_model_invalid_extraction_mode_raises(self):
        from coa_sources.utils import merge_extraction_config

        model = _FakeConfig({"extraction_mode": "bad-mode"})
        with pytest.raises(ValueError, match="Invalid extractionMode"):
            merge_extraction_config(model)


@pytest.mark.unit
class TestMergeExtractionConfigUnknownType:
    def test_unknown_type_returns_defaults(self):
        from coa_sources.utils import merge_extraction_config

        # An object that is neither None, dict, nor has model_dump
        result = merge_extraction_config(42)
        assert result == dict(EXTRACTION_DEFAULTS)

    def test_string_returns_defaults(self):
        from coa_sources.utils import merge_extraction_config

        result = merge_extraction_config("some-string")
        assert result == dict(EXTRACTION_DEFAULTS)


# ---------------------------------------------------------------------------
# Vocabulary validation (preferredEntityClassifications / preferredTopics)
# ---------------------------------------------------------------------------
# The web console applies the same rules before submitting, but a direct API
# caller bypasses the console entirely, so they have to hold here to mean
# anything. Spelling is deliberately NOT validated or rewritten: graphrag-toolkit
# joins the preferred list straight into the prompt and imposes no format of its
# own, so the caller's spelling is the caller's choice.

_VOCAB_FIELDS = ["preferred_entity_classifications", "preferred_topics"]


@pytest.mark.unit
class TestVocabularyStoredVerbatim:
    def test_classifications_are_not_rewritten(self):
        from coa_sources.utils import merge_extraction_config

        raw = ["DRESS", "loss ratio", "Style_Archetype", "StyleArchetype"]
        cfg = merge_extraction_config({"preferred_entity_classifications": raw})
        assert cfg["preferred_entity_classifications"] == raw

    def test_topics_are_not_rewritten(self):
        from coa_sources.utils import merge_extraction_config

        raw = ["Black Tie Gala", "everyday elegance", "QuietLuxury"]
        cfg = merge_extraction_config({"preferred_topics": raw})
        assert cfg["preferred_topics"] == raw

    def test_entries_are_trimmed_and_empties_dropped(self):
        from coa_sources.utils import merge_extraction_config

        cfg = merge_extraction_config({"preferred_topics": ["  Black Tie Gala  ", "", "   "]})
        assert cfg["preferred_topics"] == ["Black Tie Gala"]


@pytest.mark.unit
class TestVocabularyValidation:
    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_rejects_non_list(self, field):
        from coa_sources.utils import merge_extraction_config

        with pytest.raises(ValueError, match="must be a list of strings"):
            merge_extraction_config({field: "Policy"})

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_rejects_non_string_member(self, field):
        from coa_sources.utils import merge_extraction_config

        with pytest.raises(ValueError, match="only strings"):
            merge_extraction_config({field: ["ok", 42]})

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_rejects_duplicates_case_insensitively(self, field):
        """Two spellings differing only in case cannot be told apart once the
        recorded class is title-cased, so accepting both would configure a
        distinction that cannot exist."""
        from coa_sources.utils import merge_extraction_config

        with pytest.raises(ValueError, match="duplicate entry"):
            merge_extraction_config({field: ["Policy", "policy"]})

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_rejects_over_long_list(self, field):
        from coa_common.constants import MAX_VOCABULARY_ENTRIES
        from coa_sources.utils import merge_extraction_config

        too_many = [f"Entry {i}" for i in range(MAX_VOCABULARY_ENTRIES + 1)]
        with pytest.raises(ValueError, match=f"maximum is {MAX_VOCABULARY_ENTRIES}"):
            merge_extraction_config({field: too_many})

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_accepts_list_at_the_cap(self, field):
        from coa_common.constants import MAX_VOCABULARY_ENTRIES
        from coa_sources.utils import merge_extraction_config

        at_cap = [f"Entry {i}" for i in range(MAX_VOCABULARY_ENTRIES)]
        assert len(merge_extraction_config({field: at_cap})[field]) == MAX_VOCABULARY_ENTRIES


@pytest.mark.unit
class TestVocabularyDedupeMatchesTheBrowser:
    """Dedupe folding must agree with the console's ``toLowerCase()``.

    The console applies the same rules before submitting, so a pair it accepts
    must not 400 here — that is the whole point of duplicating the check. Python's
    ``casefold()`` folds MORE than JavaScript does (it maps "ß" to "ss"), so the
    comparison deliberately uses ``lower()``.
    """

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_sharp_s_is_not_folded_to_ss(self, field):
        from coa_sources.utils import merge_extraction_config

        # casefold() would treat these as duplicates and raise; toLowerCase() in
        # the browser does not, so neither may this.
        cfg = merge_extraction_config({field: ["Stra\u00dfe", "STRASSE"]})
        assert cfg[field] == ["Stra\u00dfe", "STRASSE"]

    @pytest.mark.parametrize("field", _VOCAB_FIELDS)
    def test_plain_case_differences_are_still_duplicates(self, field):
        from coa_sources.utils import merge_extraction_config

        with pytest.raises(ValueError, match="duplicate entry"):
            merge_extraction_config({field: ["Policy", "POLICY"]})
