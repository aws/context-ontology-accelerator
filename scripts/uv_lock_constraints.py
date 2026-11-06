# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Print uv.lock's registry pins as a pip/uv constraints file: one ``name==version`` line per package, sorted.

An image build installs its packages with ``--constraint`` on this output, so it gets the versions the lock pins (and
the tests ran on) instead of the newest release inside each pyproject range. Workspace members (editable, virtual)
are the code being installed, and a direct-URL package is pinned by its own URL and hash, so neither is constrained.
A package the lock resolves differently per fork (one entry per set of ``resolution-markers``) gets one line per
fork, qualified by its markers, e.g. ``numpy==2.2.6 ; (python_full_version < '3.14')``.
Needs only the standard library, so it runs in a bare image before anything is installed.

Usage: python uv_lock_constraints.py path/to/uv.lock > constraints.txt
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def constraints(lock_text: str) -> str:
    """The constraints file for a uv.lock's text.

    Raises:
        ValueError: A package is locked at more than one version without resolution markers to tell them apart, or
            its forks mix the registry with another source (constraining only the registry fork would pin it on
            every Python, against the other fork's markers).
    """
    entries: dict[str, list[dict]] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        entries.setdefault(package["name"], []).append(package)
    registry: dict[str, list[dict]] = {}
    for name, forks in entries.items():
        from_registry = ["registry" in fork.get("source", {}) for fork in forks]
        if any(from_registry) and not all(from_registry):
            raise ValueError(f"{name} is forked between the registry and another source")
        if all(from_registry):
            registry[name] = forks
    return "".join(sorted(line + "\n" for name, forks in registry.items() for line in _lines(name, forks)))


def _lines(name: str, forks: list[dict]) -> list[str]:
    """One ``name==version`` line, or one marker-qualified line per fork of a forked resolution."""
    if len(forks) == 1:
        return [f"{name}=={forks[0]['version']}"]
    if not all(fork.get("resolution-markers") for fork in forks):
        versions = ", ".join(fork["version"] for fork in forks)
        raise ValueError(f"{name} is locked at more than one version ({versions}) without resolution markers")
    return [
        f"{name}=={fork['version']} ; " + " or ".join(f"({marker})" for marker in fork["resolution-markers"])
        for fork in forks
    ]


def main(argv: list[str]) -> int:
    """Write the constraints for ``argv[0]`` (a uv.lock) to stdout."""
    if len(argv) != 1:
        print("usage: uv_lock_constraints.py path/to/uv.lock", file=sys.stderr)
        return 2
    sys.stdout.write(constraints(Path(argv[0]).read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
