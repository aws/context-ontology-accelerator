# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detect descriptions that carry no business meaning.

A description is boilerplate when, once the words that only restate the
identifier, its data type or its storage constraints (including quoted default
or check values) are set aside, no informative word is left — e.g. an empty
string, ``"varchar(255); not null"``, ``"Order date"`` on a column named
``order_date``. A single remaining word is enough to keep a description:
short texts such as ``"Amount in cents"`` or ``"In UTC"`` carry meaning. The
rule is generic on purpose: it knows type and constraint vocabulary, not any
source's stamp. Descriptions that pass this check can still be uninformative (a
provenance stamp naming a system and a load date); the bridge asks the LLM to
judge those.
Text in a script written without spaces between words (Chinese, Japanese, Thai,
...) cannot be split into words here, so the rule never calls it boilerplate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Data-type vocabulary across SQL dialects, XSD and common programming types.
_TYPE_WORDS = frozenset(
    {
        "string", "str", "varchar", "nvarchar", "char", "nchar", "character", "varying", "text", "clob",
        "int", "int2", "int4", "int8", "integer", "bigint", "smallint", "tinyint", "mediumint", "serial",
        "bigserial", "long", "short", "byte", "decimal", "numeric", "number", "float", "float4", "float8",
        "double", "precision", "real", "money", "boolean", "bool", "bit", "date", "datetime", "datetime2",
        "timestamp", "timestamptz", "time", "timetz", "interval", "year", "json", "jsonb", "xml", "uuid",
        "guid", "binary", "varbinary", "blob", "bytea", "bytes", "array", "list", "map", "struct", "enum",
        "set", "object", "geometry", "geography", "point", "inet", "cidr", "xsd", "anyuri",
        "type", "datatype", "dtype",
    }
)  # fmt: skip

# Storage / constraint vocabulary and generic schema nouns.
_CONSTRAINT_WORDS = frozenset(
    {
        "not", "null", "nullable", "default", "primary", "foreign", "key", "keys", "unique", "index",
        "indexed", "references", "reference", "constraint", "auto", "increment", "autoincrement", "identity",
        "generated", "length", "limit", "size", "scale", "max", "min", "true", "false", "none", "pk", "fk",
        "column", "columns", "field", "fields", "attribute", "attributes", "property", "value", "values",
        "table", "tables", "record", "records", "row", "rows", "data", "flag", "id", "identifier",
    }
)  # fmt: skip

_STOP_WORDS = frozenset(
    {
        "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "with", "and", "or", "is", "are", "was",
        "were", "be", "this", "that", "it", "its", "as", "from", "when", "which", "who", "what", "under",
        "indicating", "indicates", "represents", "stores", "contains", "holds",
    }
)  # fmt: skip

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# Quoted literals ('pending', "N/A") are constraint VALUES (defaults, checks), not description.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# Scripts written without spaces between words: CJK ideographs, kana, Thai, Lao,
# Myanmar, Khmer. A whole sentence in them is one regex "word".
_UNSPACED_SCRIPT = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    """Lower-cased word tokens of ``text``, splitting camelCase, snake_case and punctuation."""
    return [w.lower() for w in _WORD.findall(_CAMEL.sub(" ", text or ""))]


def name_tokens(names: Iterable[str]) -> set[str]:
    """Singular word tokens of identifier names (table, column, label)."""
    out: set[str] = set()
    for name in names:
        out.update(_singular(w) for w in words(name))
    return out


def informative_words(text: str, names: Iterable[str] = ()) -> list[str]:
    """Words of ``text`` that are not type, constraint, stop-word, number, or name echo."""
    echo = name_tokens(names)
    kept = []
    for w in words(_QUOTED.sub(" ", text or "")):
        if w.isdigit() or len(w) < 2:
            continue
        if w in _TYPE_WORDS or w in _CONSTRAINT_WORDS or w in _STOP_WORDS:
            continue
        if _singular(w) in echo:
            continue
        kept.append(w)
    return kept


def is_boilerplate(text: str | None, *, names: Iterable[str] = ()) -> bool:
    """True when ``text`` says nothing beyond the identifier, its type and its constraints.

    Args:
        text: The existing description (``rdfs:comment``).
        names: Identifier names the description may echo (column name, table name, label).
    """
    if not text or not text.strip():
        return True
    if _UNSPACED_SCRIPT.search(text):
        return False
    return not informative_words(text, names)
