#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Propagate the repo-root ``VERSION`` into every package manifest in the monorepo.

``VERSION`` is the single source of truth for the monorepo version — bump it there
and nowhere else. Package manifests cannot read an external file at
metadata-resolution time (pip/uv/hatchling/pnpm all require a literal ``version``
for a package to resolve, and the backends here are mixed: hatchling, setuptools,
and virtual/no-backend), so this script writes the value into each manifest and
``--check`` fails when any has drifted. ``make lint`` runs the check, so a manual
edit to a single manifest cannot land.

Three manifest kinds are updated:

- ``pyproject.toml`` — the ``version`` key of the ``[project]`` table only.
- ``package.json`` — the top-level ``"version"`` key only (never a nested one).
- ``__init__.py`` — an existing ``__version__`` assignment (the runtime accessor).

Files that declare no version are skipped, and generated / vendored / build trees
are never touched.

Usage:
    python3 scripts/sync_version.py            # write VERSION into every manifest
    python3 scripts/sync_version.py --check    # exit 1 if any manifest has drifted
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"

# Trees that must never be rewritten: generated code is overwritten on each
# codegen run, and the rest are vendored or build output.
EXCLUDED_PARTS = frozenset(
    {"smithy-generated", "node_modules", ".venv", "venv", "dist", "build", "cdk.out", "__pycache__", ".git"}
)

# Globs covering every first-party manifest. Explicit rather than a full-tree walk
# so a stray manifest in a fixture or scratch directory is never rewritten.
MANIFEST_GLOBS = (
    "pyproject.toml",
    "package.json",
    "infra/package.json",
    "libs/*/pyproject.toml",
    "libs/*/package.json",
    "packages/*/pyproject.toml",
    "packages/*/package.json",
    "coa-ai/*/pyproject.toml",
    "external-docs/package.json",
    "tests/cdk/package.json",
    "libs/*/src/*/__init__.py",
    "packages/*/src/*/__init__.py",
    "packages/*/src/*/*/__init__.py",
    "coa-ai/*/src/*/__init__.py",
)

# One capturing-group pattern per manifest kind. Group 1 and 3 are the literal
# text around the version, group 2 is the version itself — so a substitution
# rewrites ONLY the value and preserves the file's formatting byte for byte.
_PYPROJECT_VERSION = re.compile(
    r"(^\[project\](?:[^\[]*?)^version\s*=\s*[\"'])([^\"']+)([\"'])",
    re.MULTILINE | re.DOTALL,
)
# Anchored to a two-space indent: top-level keys in these 2-space-indented
# manifests. Prevents matching a nested "version" inside another object.
_PACKAGE_JSON_VERSION = re.compile(r"(^  \"version\"\s*:\s*\")([^\"]+)(\")", re.MULTILINE)
_DUNDER_VERSION = re.compile(r"(^__version__(?:\s*:\s*str)?\s*=\s*[\"'])([^\"']+)([\"'])", re.MULTILINE)


def pattern_for(path: Path) -> re.Pattern[str] | None:
    """Return the version pattern for a manifest kind, or None if unrecognized."""
    if path.name == "pyproject.toml":
        return _PYPROJECT_VERSION
    if path.name == "package.json":
        return _PACKAGE_JSON_VERSION
    if path.name == "__init__.py":
        return _DUNDER_VERSION
    return None


def read_version() -> str:
    """Read and validate the monorepo version from ``VERSION``.

    Raises:
        SystemExit: If ``VERSION`` is missing or is not a bare semver string.
    """
    if not VERSION_FILE.is_file():
        sys.exit(f"error: {VERSION_FILE} not found")
    raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    # Reject a leading "v" or stray content: the value is interpolated into
    # PEP 621 / npm manifests, both of which require a bare version string.
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", raw):
        sys.exit(f"error: {VERSION_FILE} must contain a bare semver version (e.g. 0.1.0), got {raw!r}")
    return raw


def current_version(path: Path) -> str | None:
    """Return the version ``path`` currently declares, or None if it declares none."""
    pattern = pattern_for(path)
    if pattern is None:
        return None
    match = pattern.search(path.read_text(encoding="utf-8"))
    return match.group(2) if match else None


def discover_manifests() -> list[Path]:
    """Return every first-party manifest that declares a version, sorted by path.

    Files with no version declaration are excluded — an ``__init__.py`` without a
    ``__version__`` (most of them) is not a manifest and gets no new attribute.
    """
    found: set[Path] = set()
    for glob in MANIFEST_GLOBS:
        for path in REPO_ROOT.glob(glob):
            if EXCLUDED_PARTS.isdisjoint(path.parts) and path.is_file():
                found.add(path)
    return sorted(p for p in found if current_version(p) is not None)


def write_version(path: Path, version: str) -> None:
    """Rewrite ``path``'s declared version to ``version``, preserving all other bytes."""
    pattern = pattern_for(path)
    if pattern is None:
        return
    text = path.read_text(encoding="utf-8")
    path.write_text(pattern.sub(rf"\g<1>{version}\g<3>", text, count=1), encoding="utf-8")


def missing_kinds(manifests: list[Path]) -> list[str]:
    """Return manifest kinds that matched nothing.

    A zero-match kind means a broken pattern or layout change: the sync would silently cover fewer files.
    """
    names = {p.name for p in manifests}
    return [kind for kind in ("pyproject.toml", "package.json", "__init__.py") if kind not in names]


def main() -> int:
    """Sync (or check) every manifest against ``VERSION``."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift and exit 1 instead of rewriting")
    args = parser.parse_args()

    version = read_version()
    manifests = discover_manifests()
    if missing := missing_kinds(manifests):
        sys.exit(f"error: no {', '.join(missing)} manifests discovered — check MANIFEST_GLOBS against the repo layout")

    drifted = [(p, found) for p in manifests if (found := current_version(p)) != version]

    if args.check:
        if drifted:
            print(f"version drift: VERSION is {version} but {len(drifted)} manifest(s) disagree:", file=sys.stderr)
            for path, found in drifted:
                print(f"  {path.relative_to(REPO_ROOT)}: {found}", file=sys.stderr)
            print("run `make version` to sync", file=sys.stderr)
            return 1
        print(f"✓ {len(manifests)} manifest(s) at version {version}")
        return 0

    for path, found in drifted:
        write_version(path, version)
        print(f"  {path.relative_to(REPO_ROOT)}: {found} -> {version}")
    print(f"✓ {len(manifests)} manifest(s) at version {version} ({len(drifted)} updated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
