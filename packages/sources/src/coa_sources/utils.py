# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared utilities for the unified sources package."""

from __future__ import annotations

from typing import Any

from coa_common.constants import EXTRACTION_DEFAULTS, MAX_VOCABULARY_ENTRIES, ExtractionMode

_VALID_EXTRACTION_MODES: frozenset[str] = frozenset(ExtractionMode)


def _clean_vocabulary(values: list[Any], *, field: str) -> list[str]:
    """Validate one vocabulary list from the API. Entries are stored verbatim.

    Trims entries, drops empties, and rejects duplicates and over-long lists.
    Spelling is **not** rewritten: graphrag-toolkit joins the preferred list
    straight into the prompt (``format_list`` is a plain newline join) and imposes
    no format of its own, so the caller's spelling is the caller's choice.

    Note what this means downstream: the toolkit *does* title-case the
    classification the LLM emits (``format_classification`` at
    ``topic_utils.py:112``), so a label like ``"Style archetype"`` reaches the
    graph as ``"Style Archetype"``, and ``"StyleArchetype"`` reaches it as
    ``"Stylearchetype"``. That difference is surfaced in the console rather than
    corrected here — see ``predictStoredForm`` in
    ``packages/web-app/src/utils/extraction-vocabulary.ts``.

    Duplicates are compared case-insensitively because two spellings that differ
    only in case cannot be told apart once the parser has title-cased them, so
    accepting both would configure a distinction that cannot exist.

    The comparison uses ``str.lower()``, NOT ``str.casefold()``, to stay in exact
    step with the console's ``toLowerCase()``. ``casefold()`` folds more than
    JavaScript does — it maps German "ß" to "ss", so ``["Straße", "STRASSE"]``
    would be rejected here while the console accepted it, and the caller would
    hit a 400 only after filling in the whole form. Mirrored validation is the
    reason both checks exist, so they have to agree.

    Raises:
        ValueError: with a caller-actionable message. The document create route
            turns this into a 400.
    """
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list of strings.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{field} must contain only strings; got {type(raw).__name__}.")
        entry = raw.strip()
        if not entry:
            continue
        key = entry.lower()
        if key in seen:
            raise ValueError(f"{field} contains duplicate entry {entry!r}.")
        seen.add(key)
        cleaned.append(entry)

    if len(cleaned) > MAX_VOCABULARY_ENTRIES:
        raise ValueError(
            f"{field} has {len(cleaned)} entries; the maximum is {MAX_VOCABULARY_ENTRIES}. "
            "Long lists crowd the extraction prompt and reduce how closely the model follows them."
        )
    return cleaned


def merge_extraction_config(config: object) -> dict[str, Any]:
    """Merge user-supplied extraction config with defaults.

    Accepts a Pydantic model (with ``model_dump``), a plain dict, or ``None``.
    Returns a snake_case dict suitable for SQS message bodies and DDB storage.

    Raises:
        ValueError: for an unrecognised ``extraction_mode``, or for a
            ``preferred_entity_classifications`` / ``preferred_topics`` list that
            is malformed, duplicated, or over-long.
    """
    result: dict[str, Any] = dict(EXTRACTION_DEFAULTS)
    if config is None:
        return result
    if hasattr(config, "model_dump"):
        overrides: dict[str, Any] = config.model_dump(exclude_none=True)  # type: ignore[union-attr]
    elif isinstance(config, dict):
        overrides = {k: v for k, v in config.items() if v is not None}
    else:
        return result
    if "extraction_mode" in overrides and overrides["extraction_mode"] not in _VALID_EXTRACTION_MODES:
        raise ValueError(
            f"Invalid extractionMode: {overrides['extraction_mode']!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_EXTRACTION_MODES))}."
        )
    # Both lists get identical treatment: the toolkit imposes no format on either.
    if "preferred_entity_classifications" in overrides:
        overrides["preferred_entity_classifications"] = _clean_vocabulary(
            overrides["preferred_entity_classifications"],
            field="preferredEntityClassifications",
        )
    if "preferred_topics" in overrides:
        overrides["preferred_topics"] = _clean_vocabulary(
            overrides["preferred_topics"],
            field="preferredTopics",
        )
    result.update(overrides)
    return result
