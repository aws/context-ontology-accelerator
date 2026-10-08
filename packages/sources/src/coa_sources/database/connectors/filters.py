# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared schema/table filter compilation for database connectors.

Filters are pipe/comma-delimited lists of shell globs (``orders|customers``,
``staging_*``), each matched against the whole name. Both the
JDBC connector (client-side schema/table filtering) and the Glue connector
(client-side ``table_exclude_filter``) must interpret them identically —
treating the whole string as a single ``fnmatch`` glob silently matches nothing
for a delimited list. This module is the single source of truth so the two
connectors cannot drift apart.
"""

from __future__ import annotations

import fnmatch
import re


def split_glob_list(pattern: str) -> list[str]:
    """Split a filter into its individual globs on top-level ``|`` / ``,``.

    Delimiters inside a glob character class (``[...]``) are NOT split on, so
    a class such as ``log_[a,b]`` stays a single glob. (Table/schema
    identifiers can't contain ``|``/``,``, so the only place a delimiter is
    meaningful mid-glob is inside a bracket expression.) Surrounding
    whitespace on each entry is trimmed; empty entries are dropped.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0  # inside a [...] character class
    for ch in pattern:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in "|," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def compile_filter(pattern: str | None, field_name: str) -> re.Pattern[str] | None:
    """Compile an optional filter to a full-match regex; return None if no pattern.

    The filter is a list of one or more shell globs (``*``, ``?``, ``[seq]``)
    separated by ``|`` or ``,`` — e.g. ``circuits|drivers|races`` or
    ``constructor*,results``. A name matches the filter if it matches ANY of the
    globs. A single glob (no delimiter) behaves exactly as a plain glob.

    Comma/pipe support is deliberate: the source-creation UI labels this field
    "regex" with an ``orders|customers`` placeholder, so users reasonably type
    pipe-delimited lists. Treating the whole string as one glob silently matched
    nothing (``orders|customers`` is not a valid glob token). Delimiters inside a
    glob character class (``[a,b]``) are preserved — see ``split_glob_list``.
    """
    if not pattern:
        return None
    globs = split_glob_list(pattern)
    if not globs:
        return None
    try:
        # OR the per-glob translations together. fnmatch.translate wraps each in
        # ``(?s:...)\Z``; alternating those gives a full-match-any.
        return re.compile("|".join(fnmatch.translate(g) for g in globs))
    except re.error as exc:
        raise ValueError(f"Invalid pattern for {field_name}: {exc}") from exc


# The filter syntax, worded once for every user-facing message about it.
GLOB_SYNTAX_HINT = (
    "Filters are shell globs matched against the whole name (* any characters, ? one character, "
    "[abc] one of the set), separated by | or , — e.g. sales_*|finance"
)


def looks_like_regex(pattern: str | None) -> bool:
    """True if a filter carries regex-only syntax that a glob never needs.

    Only the unambiguous signs: an entry starting with ``^``, ending with ``$``, or
    containing ``.*``. Anything else (parentheses, ``+``, a ``$`` inside a name) can
    be part of a real identifier, so it is never treated as a mistake. Used only to
    word a hint; no filter is ever rejected for it.
    """
    if not pattern:
        return False
    return any(g.startswith("^") or g.endswith("$") or ".*" in g for g in split_glob_list(pattern))


def glob_suggestion(pattern: str) -> str | None:
    """The obvious glob equivalent of a regex-looking filter, or None if there isn't one.

    Strips ``^``/``$`` anchors, unwraps one ``(a|b)`` group, and turns ``.*`` into
    ``*`` — enough for ``^target_schema$`` → ``target_schema`` and
    ``^(public|analytics)$`` → ``public|analytics``. Returns None when the result
    would still contain regex syntax, rather than guessing.
    """
    # Anchors and one wrapping group are removed from the WHOLE pattern first, so
    # ``^(a|b)$`` unwraps before its ``|`` is read as a separator.
    whole = pattern.strip().removeprefix("^").removesuffix("$")
    if whole.startswith("(") and whole.endswith(")"):
        whole = whole[1:-1]
    out: list[str] = []
    for g in split_glob_list(whole):
        out.append(g.removeprefix("^").removesuffix("$").replace(".*", "*"))
    if not out or any(ch in "".join(out) for ch in "^$()+\\{}"):
        return None
    suggestion = "|".join(out)
    return suggestion if suggestion != pattern else None


def regex_hint(pattern: str | None, field_name: str) -> str:
    """A sentence pointing out a regex-looking filter, with its glob equivalent if obvious."""
    if not looks_like_regex(pattern):
        return ""
    hint = f" {field_name} {pattern!r} looks like a regular expression, but filters are globs."
    suggestion = glob_suggestion(pattern or "")
    if suggestion:
        hint += f" Did you mean {suggestion!r}?"
    return hint


def unmatched_regex_warning(pattern: str | None, field_name: str, names: list[str]) -> str | None:
    """A scan warning when a regex-looking filter matched none of ``names``, else None.

    A glob that legitimately matches nothing (an exclude for a table that does not
    exist yet) stays silent; only a filter that both looks like a regex and had no
    effect is reported, since that is almost certainly a syntax mistake. With no
    names at all (an empty database or schema) there was nothing to match, so
    nothing is reported either.
    """
    if not names or not looks_like_regex(pattern):
        return None
    compiled = compile_filter(pattern, field_name)
    if compiled is None or any(compiled.match(n) for n in names):
        return None
    return f"{field_name} matched nothing, so it had no effect.{regex_hint(pattern, field_name)} {GLOB_SYNTAX_HINT}."
