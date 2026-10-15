# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reviewed versions of the fixed text COA writes into induced ontologies.

Some ontology text is not model output but a fixed template: foreign-key notes,
deterministic fallback class descriptions, default proposal and topic-scheme
titles. That text describes customer data, so it follows the namespace language
(namespace-language LLD, section 2.2.6). V1 has reviewed ``en`` and ``ja``
versions. Any other namespace language keeps the English text; the
compatibility check then leaves it untagged because English text carries no
evidence for that language.

Platform vocabulary (``matchConfidence``, ``PK_SHARING``, provenance codes) and
service messages are not templates here: they stay English.

The output-language instruction helpers (``needs_language_instruction``,
``describe_language``) are not ontology-engine specific and live in
``coa_common.language_tags``.
"""

from __future__ import annotations

from coa_common.language_tags import DEFAULT_LANGUAGE, LanguageTagError, primary_language

_TEMPLATES: dict[str, dict[str, str]] = {
    # ── Proposal titles (proposal metadata, not RDF) ─────────────────────
    "structured_proposal_title": {
        "en": "Induced Structured Ontology",
        "ja": "構造化データから生成したオントロジー",
    },
    "unstructured_proposal_title": {
        "en": "Induced Unstructured Ontology",
        "ja": "非構造化データから生成したオントロジー",
    },
    # The structured route's dispatch to unstructured induction has always used
    # this shorter title.
    "unstructured_dispatch_title": {
        "en": "Unstructured Induction",
        "ja": "非構造化データから生成したオントロジー",
    },
    # ── Structured ontology text ─────────────────────────────────────────
    "structured_ontology_label": {
        "en": "Proposal Ontology",
        "ja": "提案オントロジー",
    },
    "fk_note": {
        "en": "Foreign key: {table}.{column} references {target_table}.{target_column}",
        "ja": "外部キー: {table}.{column} は {target_table}.{target_column} を参照します",
    },
    "composite_fk_member_note": {
        "en": "Part of a composite foreign key on {table}; the relationship is carried by {carrier}",
        "ja": "{table} の複合外部キーの一部です。関係は {carrier} が表します",
    },
    "malformed_composite_fk_note": {
        "en": (
            "Part of a malformed composite foreign key on {table} "
            "(column/target counts differ, or targets span several tables); "
            "mapped as a literal because no join can be derived"
        ),
        "ja": (
            "{table} の不正な複合外部キーの一部です（列と参照先の数が一致しないか、参照先が複数のテーブルに"
            "またがっています）。結合条件を導出できないため、リテラルとしてマッピングしています"
        ),
    },
    # ── Unstructured ontology text ───────────────────────────────────────
    "datatype_property_observation": {
        "en": "Observed across {count} fact(s).",
        "ja": "{count} 件のファクトで観測されました。",
    },
    "topic_scheme_label": {
        "en": "{namespace} Topic Scheme",
        "ja": "{namespace} トピック体系",
    },
    "class_fallback_entities": {
        "en": "Class representing {count} entities including: {labels}",
        "ja": "{count} 件のエンティティを表すクラスです。例: {labels}",
    },
    "class_fallback_no_relationships": {
        "en": "Class {label!r} with no observed inter-class relationships.",
        "ja": "クラス「{label}」には、他のクラスとの関係が観測されていません。",
    },
    "class_fallback_edges": {
        "en": "Class {label!r} with edges {edges}.",
        "ja": "クラス「{label}」は、{edges}他のクラスと接続しています。",
    },
    "class_fallback_outgoing": {
        "en": "connected outgoing via {predicates}",
        "ja": "出力側で {predicates} により",
    },
    "class_fallback_incoming": {
        "en": "and connected incoming via {predicates}",
        "ja": "入力側で {predicates} により",
    },
}

_LIST_SEPARATORS: dict[str, str] = {"en": ", ", "ja": "、"}
_PART_SEPARATORS: dict[str, str] = {"en": " ", "ja": "、"}


def text_language(namespace_language: str) -> str:
    """Return the reviewed template language (``en`` or ``ja``) for a namespace."""
    try:
        primary = primary_language(namespace_language)
    except LanguageTagError:
        return DEFAULT_LANGUAGE
    return primary if primary in _LIST_SEPARATORS else DEFAULT_LANGUAGE


def render(key: str, namespace_language: str, **values: object) -> str:
    """Render a fixed-text template in the namespace's reviewed language.

    The template is always a constant from ``_TEMPLATES``; ``values`` are only
    substituted into it and never parsed as format strings themselves.

    Raises:
        KeyError: If ``key`` is not a template, or the template needs a value
            the caller did not pass. Both are programming errors; the message
            names the template.
    """
    language = text_language(namespace_language)
    try:
        return _TEMPLATES[key][language].format(**values)
    except KeyError as e:
        raise KeyError(f"generated-text template {key!r} ({language}): missing {e.args[0]!r}") from e


def join_list(items: list[str], namespace_language: str) -> str:
    """Join list items with the separator of the namespace's reviewed language."""
    return _LIST_SEPARATORS[text_language(namespace_language)].join(items)


def join_parts(parts: list[str], namespace_language: str) -> str:
    """Join clause fragments of one sentence in the namespace's reviewed language."""
    return _PART_SEPARATORS[text_language(namespace_language)].join(parts)
