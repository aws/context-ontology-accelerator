# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Datasource-id normalization shared across the ontology engine.

Different pipelines spell the same datasource id differently: the sources
pipeline keys datasources as ``DS#{uuid}`` (and the sources table as
``SRC#{uuid}``), while an induction request — and the ``datasource_ids``
recorded on accepted proposals — carries the bare uuid. Catalog lookups and
cross-source target resolution must not depend on which spelling reached them,
so both normalize through :func:`bare_datasource_id`.
"""

from __future__ import annotations

import re

# The key prefixes the sources pipeline uses. A datasource id carries AT MOST ONE
# of these, so exactly one is stripped — a chained strip would wrongly collapse a
# (malformed) ``DS#SRC#uuid`` down to ``uuid`` and mask the bad input.
_DATASOURCE_KEY_PREFIXES = ("DS#", "SRC#")


def bare_datasource_id(value: str) -> str:
    """Return ``value`` with a single leading ``DS#`` / ``SRC#`` key prefix removed.

    Strips at most one prefix so ids spelled by different pipelines compare equal
    without silently double-stripping a malformed input. A value with no prefix
    (or an empty string) is returned unchanged.
    """
    for prefix in _DATASOURCE_KEY_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


# A datasource id becomes a catalog URL path segment, so it must not carry path
# separators, whitespace, or URL/query metacharacters (path-traversal / injection
# defence). It is NOT required to be a UUID: the system legitimately uses ids like
# ``ds-1`` / ``ds-def789``. So validate by REJECTING unsafe characters rather than
# enforcing a UUID shape — allow ``[A-Za-z0-9._-]`` and require non-empty.
_UNSAFE_DATASOURCE_ID_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def is_valid_datasource_id(bare_id: str) -> bool:
    """Whether ``bare_id`` (already prefix-stripped) is safe to interpolate into a URL path.

    Rejects empty ids and anything with path separators, whitespace, ``..`` traversal,
    or URL/query metacharacters. Does NOT require a UUID — ordinary ids like ``ds-1``
    are valid.
    """
    return bool(bare_id) and ".." not in bare_id and bool(_UNSAFE_DATASOURCE_ID_RE.match(bare_id))
