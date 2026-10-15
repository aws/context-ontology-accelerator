# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Third-party license inventory for the installed Python environment.

Enumerates installed distributions via ``importlib.metadata`` and normalizes each
one's declared license into a canonical SPDX-ish token. The inventory backs the
root NOTICE (ORR PAK-A-3): every redistributed third-party component is listed
with its version and normalized license.

Pure functions (``normalize_license``) are unit-tested; the ``importlib.metadata``
enumeration is isolated in ``iter_installed`` so tests can inject synthetic
distributions.
"""

from __future__ import annotations

import importlib.metadata as im
from collections.abc import Iterable
from dataclasses import dataclass

# Canonical SPDX tokens we recognize. normalize_license() emits one of these (or
# UNKNOWN); the set is also used to canonical-case a raw token that already is a
# valid SPDX id. Copyleft ids are included so those licenses normalize accurately
# in the NOTICE rather than falling through to UNKNOWN.
_KNOWN_LICENSES: frozenset[str] = frozenset(
    {
        "Apache-2.0",
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "Python-2.0",
        "PSF-2.0",
        "MPL-2.0",
        "LGPL-2.1",
        "LGPL-3.0",
        "Unlicense",
        "0BSD",
        "BlueOak-1.0.0",
        "CC0-1.0",
        "GPL-2.0",
        "GPL-3.0",
        "AGPL-3.0",
        "W3C",
    }
)

# First-party workspace packages — not third-party, so excluded from the NOTICE.
# Matched case-insensitively by name prefix.
FIRST_PARTY_PREFIXES: tuple[str, ...] = ("coa-", "coa_")

# Curated overrides for packages whose installed metadata omits or malforms the
# license, verified against the project's bundled LICENSE file. Keep this list
# short and cite the source; every entry is a human review decision.
#   cedarpy 4.8.3  → bundled dist-info/licenses/LICENSE is Apache-2.0
#   tiktoken 0.13  → bundled dist-info/licenses/LICENSE is MIT (OpenAI)
#   owlrl 7.1.4    → W3C Software and Document Notice License (permissive)
REVIEWED_OVERRIDES: dict[str, str] = {
    "cedarpy": "Apache-2.0",
    "tiktoken": "MIT",
    "owlrl": "W3C",
}

# Raw license strings / classifier fragments → canonical token. Longest, most
# specific patterns are matched first (see normalize_license).
_ALIASES: tuple[tuple[str, str], ...] = (
    ("apache software license", "Apache-2.0"),
    ("apache 2", "Apache-2.0"),
    ("apache-2", "Apache-2.0"),
    ("apache license", "Apache-2.0"),
    ("bsd-3-clause", "BSD-3-Clause"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("bsd-2-clause", "BSD-2-Clause"),
    ("bsd 2-clause", "BSD-2-Clause"),
    ("bsd license", "BSD-3-Clause"),
    ("bsd", "BSD-3-Clause"),
    ("mit license", "MIT"),
    ("mit", "MIT"),
    ("isc license", "ISC"),
    ("isc", "ISC"),
    ("mozilla public license 2.0", "MPL-2.0"),
    ("mpl-2.0", "MPL-2.0"),
    ("mpl 2.0", "MPL-2.0"),
    ("gnu lesser general public license v3", "LGPL-3.0"),
    ("lgplv3", "LGPL-3.0"),
    ("lgpl-3", "LGPL-3.0"),
    ("gnu lesser general public license v2", "LGPL-2.1"),
    ("lgplv2", "LGPL-2.1"),
    ("lgpl-2", "LGPL-2.1"),
    ("gnu affero general public license", "AGPL-3.0"),
    ("agpl", "AGPL-3.0"),
    ("gnu general public license v3", "GPL-3.0"),
    ("gplv3", "GPL-3.0"),
    ("gpl-3", "GPL-3.0"),
    ("gnu general public license v2", "GPL-2.0"),
    ("gplv2", "GPL-2.0"),
    ("gpl-2", "GPL-2.0"),
    ("python software foundation", "PSF-2.0"),
    ("psf", "PSF-2.0"),
    ("python-2.0", "Python-2.0"),
    ("the unlicense", "Unlicense"),
    ("unlicense", "Unlicense"),
    ("0bsd", "0BSD"),
    ("cc0", "CC0-1.0"),
)

_KNOWN_LOWER: frozenset[str] = frozenset(t.lower() for t in _KNOWN_LICENSES)

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PackageLicense:
    name: str
    version: str
    raw_license: str
    normalized: str


def normalize_license(raw: str | None, classifiers: Iterable[str] = ()) -> str:
    """Normalize a package's declared license to a canonical token.

    Prefers the ``License ::`` trove classifiers (more reliable than the free-text
    ``License`` field), then falls back to alias-matching the raw string. Returns
    ``UNKNOWN`` when nothing matches.
    """
    # 1. Trove classifiers are the most reliable signal.
    for c in classifiers:
        if "License ::" not in c:
            continue
        tail = c.split("::")[-1].strip()
        token = _match_alias(tail)
        if token != UNKNOWN:
            return token

    # 2. Free-text License field.
    if raw:
        raw_stripped = raw.strip()
        # Exact SPDX token (e.g. "Apache-2.0", "MIT") — accept verbatim, canonical-cased.
        if raw_stripped.lower() in _KNOWN_LOWER:
            return _canonical_case(raw_stripped)
        token = _match_alias(raw_stripped)
        if token != UNKNOWN:
            return token

    return UNKNOWN


def _match_alias(text: str) -> str:
    low = text.strip().lower()
    if not low:
        return UNKNOWN
    if low in _KNOWN_LOWER:
        return _canonical_case(text.strip())
    for needle, token in _ALIASES:
        if needle in low:
            return token
    return UNKNOWN


def _canonical_case(token: str) -> str:
    for known in _KNOWN_LICENSES:
        if known.lower() == token.lower():
            return known
    return token


def is_first_party(name: str) -> bool:
    low = name.strip().lower()
    return any(low.startswith(p) for p in FIRST_PARTY_PREFIXES)


def _raw_license_of(md: im.PackageMetadata) -> str:
    """Extract the best raw license string from distribution metadata."""
    raw = (md.get("License") or "").strip()
    # PEP 639 License-Expression supersedes the legacy field when present.
    expr = (md.get("License-Expression") or "").strip()
    if expr:
        return expr
    # A multi-line license field usually means the full license text was pasted in;
    # that is noise, not a token — drop it and rely on classifiers.
    if "\n" in raw or len(raw) > 60:
        return ""
    return raw


def iter_installed(*, include_first_party: bool = False) -> list[PackageLicense]:
    """Enumerate third-party installed distributions in the current environment.

    First-party workspace packages are excluded by default (they carry no license
    metadata and are not redistributed as third-party). Curated ``REVIEWED_OVERRIDES``
    are applied when a package's own metadata omits/malforms its license.
    """
    out: list[PackageLicense] = []
    seen: set[str] = set()
    for dist in im.distributions():
        md = dist.metadata
        # Use .get() rather than subscripting: importlib.metadata's Message adapter
        # returns None for a missing header today but has deprecated that implicit
        # None and will raise KeyError in a future Python. Matches _raw_license_of().
        name = (md.get("Name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if not include_first_party and is_first_party(name):
            continue
        raw = _raw_license_of(md)
        classifiers = md.get_all("Classifier") or []
        # Keep compound declarations verbatim instead of selecting one license.
        compound = "," in raw
        normalized = UNKNOWN if compound else normalize_license(raw, classifiers)
        if not compound and normalized == UNKNOWN and name.lower() in REVIEWED_OVERRIDES:
            normalized = REVIEWED_OVERRIDES[name.lower()]
        out.append(
            PackageLicense(
                name=name,
                version=(md.get("Version") or "").strip(),
                raw_license=raw,
                normalized=normalized,
            )
        )
    return out
