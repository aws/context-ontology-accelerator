# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the pnpm install-script allowlist (``allowBuilds``)."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Locked packages that ship an install script. pnpm 10 skips an undecided one with a
# warning, but pnpm 11 and later fail `pnpm install` (ERR_PNPM_IGNORED_BUILDS), so each
# must have an explicit true/false in allowBuilds. The lockfile does not record which
# packages have install scripts, so a new one has to be added here by hand.
_INSTALL_SCRIPT_PACKAGES = {"esbuild", "nx", "@parcel/watcher", "unrs-resolver"}


def _allow_builds() -> dict[str, str]:
    workspace = (_REPO_ROOT / "pnpm-workspace.yaml").read_text()
    block = re.search(r"^allowBuilds:\n((?:  [^\n]*\n)+)", workspace, re.MULTILINE)
    assert block, "pnpm-workspace.yaml must declare allowBuilds"
    entries = {}
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition(":")
        entries[name.strip().strip("'\"")] = value.strip()
    return entries


def _locked_package_names() -> set[str]:
    lockfile = (_REPO_ROOT / "pnpm-lock.yaml").read_text()
    names = set()
    for key in re.findall(r"^  '?(@?[^@'\s]+)@[^:\s]+'?:$", lockfile, re.MULTILINE):
        names.add(key)
    return names


def test_allow_builds_values_are_explicit_booleans() -> None:
    """pnpm 11 writes a 'set this to true or false' placeholder for an undecided script."""
    for name, value in _allow_builds().items():
        assert value in {"true", "false"}, f"allowBuilds[{name!r}] must be true or false, got {value!r}"


def test_every_locked_install_script_package_is_decided() -> None:
    """A locked package with an install script must be listed, or pnpm 11+ fails the install."""
    locked = _locked_package_names()
    present = _INSTALL_SCRIPT_PACKAGES & locked
    assert present, "none of the known install-script packages is locked; is the lockfile parsed?"
    missing = sorted(present - set(_allow_builds()))
    assert not missing, f"locked packages with install scripts missing from allowBuilds: {missing}"
