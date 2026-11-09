# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Python dependencies resolve to the patched release of each advisory cleared here.

Two things decide which version of a package ships, and each is checked:

- uv.lock, which the serve image and the workspace packages install from, and the
  hash-pinned requirements.lock.txt that each Lambda bundle installs from. Every
  pin must be a patched release.
- Each requirement that sets a lower bound in a tracked requirements*.txt or
  pyproject.toml must reject the last release before the patch. Dependency
  scanning reports these on their own, so a stale floor is reported even when the
  lock beside it is patched. For example, ``PyJWT>=2.9.0`` in the api-proxy
  Lambda's requirements.txt was reported as GHSA-ffc3-869f-jxw9 (critical) while
  its requirements.lock.txt pinned 2.15.1. A floor also decides what an image
  installs when it has no lock, such as pypdf in the document preprocessing image.
  A bare name with no lower bound is left to the lock it installs from, and so are an
  upper bound alone (``pypdf<7``) and a direct URL reference.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# package -> (first patched release, the advisory with the highest fixed version)
PATCHED = {
    "pyjwt": (Version("2.15.0"), "GHSA-ffc3-869f-jxw9 / GHSA-42vr-xj54-vc7v"),
    "cryptography": (Version("50.0.0"), "GHSA-g6cj-pr64-35w5"),
    "pypdf": (Version("6.21.4"), "GHSA-mfr5-77xh-9j7h"),
    "pyasn1": (Version("0.6.5"), "GHSA-mm57-4v3p-rchx"),
    "aiohttp": (Version("3.14.4"), "GHSA-9xw5-w5jj-pvcg"),
    "requests": (Version("2.33.0"), "GHSA-gc5v-m9x4-r6x2"),
    "pydantic": (Version("2.4.0"), "GHSA-mr82-8j83-vxmv"),
    "strands-agents": (Version("1.48.0"), "GHSA-3w55-933p-8pv5 / GHSA-763v-5pjm-fx36"),
}

_LOWER_BOUND_OPERATORS = {"==", "===", ">=", ">", "~="}
_LEADING_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _release_before(version: Version) -> Version:
    """The highest release that sorts below ``version`` in practice, e.g. 2.15.0 -> 2.14.9999."""
    major, minor, micro = (*version.release, 0, 0)[:3]
    if micro:
        return Version(f"{major}.{minor}.{micro - 1}")
    if minor:
        return Version(f"{major}.{minor - 1}.9999")
    return Version(f"{major - 1}.9999.9999")


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.splitlines()]


def _requirement_lines(path: Path) -> Iterator[tuple[int, str]]:
    """(line, requirement string) for each requirement a requirements*.txt or pyproject.toml declares."""
    if path.name.endswith(".toml"):
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        project = data.get("project", {})
        groups = [
            project.get("dependencies", []),
            *project.get("optional-dependencies", {}).values(),
            *data.get("dependency-groups", {}).values(),
            data.get("tool", {}).get("uv", {}).get("constraint-dependencies", []),
            data.get("tool", {}).get("uv", {}).get("override-dependencies", []),
        ]
        lines = text.splitlines()
        for spec in (s for group in groups for s in group if isinstance(s, str)):
            yield next((n for n, line in enumerate(lines, start=1) if spec in line), 0), spec
        return
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # Drop a trailing comment, per-requirement options such as --hash, and a continuation.
        spec = re.split(r"\s#|\s--", line, maxsplit=1)[0].strip().removesuffix("\\").strip()
        if spec and not spec.startswith(("#", "-")):
            yield lineno, spec


def _parse(path: Path) -> Iterator[tuple[Requirement | None, str, int]]:
    """(requirement or None if it does not parse, the raw spec, line) for each tracked package's requirement."""
    for lineno, spec in _requirement_lines(path):
        try:
            requirement: Requirement | None = Requirement(spec)
            name = requirement.name
        except InvalidRequirement:
            requirement, leading = None, _LEADING_NAME.match(spec)
            name = leading.group(0) if leading else ""
        if canonicalize_name(name) in PATCHED:
            yield requirement, spec, lineno


def _declared(path: Path) -> Iterator[tuple[Requirement, int]]:
    for requirement, _, lineno in _parse(path):
        if requirement is not None:
            yield requirement, lineno


def _vulnerable_match(requirement: Requirement) -> Version | None:
    """A release below the patch that a requirement with a lower bound accepts, or None."""
    specifiers = list(requirement.specifier)
    if not any(s.operator in _LOWER_BOUND_OPERATORS for s in specifiers):
        return None
    patched, _ = PATCHED[canonicalize_name(requirement.name)]
    probes = {_release_before(patched)}
    for spec in specifiers:
        try:
            probes.add(Version(spec.version))
        except InvalidVersion:
            continue
    return next(
        (v for v in sorted(probes) if v < patched and requirement.specifier.contains(v, prereleases=True)), None
    )


def _admits_vulnerable(requirement: Requirement) -> bool:
    return _vulnerable_match(requirement) is not None


def _stale(name: str, version: str) -> bool:
    patched, _ = PATCHED[canonicalize_name(name)]
    return Version(version) < patched


def _describe(name: str) -> str:
    patched, advisory = PATCHED[canonicalize_name(name)]
    return f"< {patched} ({advisory})"


def test_uv_lock_pins_patched_releases() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    stale = [
        f"uv.lock: {pkg['name']} {pkg['version']} {_describe(pkg['name'])}"
        for pkg in lock["package"]
        if canonicalize_name(pkg["name"]) in PATCHED and "version" in pkg and _stale(pkg["name"], pkg["version"])
    ]
    assert not stale, "\n".join(stale)


def test_lambda_requirement_locks_pin_patched_releases() -> None:
    locks = _tracked("*requirements.lock.txt")
    assert locks, "git ls-files found no requirements.lock.txt"
    stale = [
        f"{path.relative_to(ROOT)}:{line}: {req.name} {spec.version} {_describe(req.name)}"
        for path in locks
        for req, line in _declared(path)
        for spec in req.specifier
        if spec.operator == "==" and _stale(req.name, spec.version)
    ]
    assert not stale, "\n".join(stale)


def test_declared_lower_bounds_reject_vulnerable_releases() -> None:
    manifests = _tracked("*requirements*.txt", "*pyproject.toml")
    assert manifests, "git ls-files found no manifests"
    stale = [
        f"{path.relative_to(ROOT)}:{line}: {req} admits {_vulnerable_match(req)} {_describe(req.name)}"
        for path in manifests
        for req, line in _declared(path)
        if _admits_vulnerable(req)
    ]
    assert not stale, "\n".join(stale)


def test_tracked_package_requirements_all_parse() -> None:
    manifests = _tracked("*requirements*.txt", "*pyproject.toml")
    unparsed = [
        f"{path.relative_to(ROOT)}:{line}: {spec!r}"
        for path in manifests
        for requirement, spec, line in _parse(path)
        if requirement is None
    ]
    assert not unparsed, "requirements this test cannot check:\n" + "\n".join(unparsed)


@pytest.mark.parametrize(
    ("patched", "before"),
    [("3.14.4", "3.14.3"), ("2.15.0", "2.14.9999"), ("50.0.0", "49.9999.9999"), ("50", "49.9999.9999")],
)
def test_release_before_sorts_just_below_the_patch(patched: str, before: str) -> None:
    assert _release_before(Version(patched)) == Version(before)


@pytest.mark.parametrize(
    ("line", "admits"),
    [
        ("PyJWT>=2.9.0", [True]),
        ("pyjwt>2.14", [True]),
        ("pyjwt~=2.13", [True]),
        ("pyjwt!=2.13.0,>=2.9", [True]),
        ("PyJWT>=2.15.1", [False]),
        ("pyjwt==2.15.1 \\", [False]),
        ('    "cryptography>=50.0.0",', []),
        ("cryptography>=50.0.0", [False]),
        ('aiohttp>=3.14.4; python_version >= "3.10"', [False]),
        ("pypdf>=6.21.4  # PDF parsing", [False]),
        ("unstructured[pdf]>=0.27", []),
        ("cryptography<51", [False]),
        ("PyJWT", [False]),
        ("# PyJWT>=2.9.0", []),
        ("--hash=sha256:00", []),
        ("pyjwt-extras>=1.0", []),
        ("PyJWT==2.9.0", [True]),
        ("pypdf===6.21.2", [True]),
        ("cryptography>=43.0,<44", [True]),
        ("aiohttp~=3.13.0", [True]),
        ("pyjwt==2.14.0 --hash=sha256:00", [True]),
        ("pyjwt>=2.9\t# tab comment", [True]),
    ],
)
def test_requirements_txt_lines_are_parsed_as_requirements(tmp_path: Path, line: str, admits: list[bool]) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text(line + "\n", encoding="utf-8")
    assert [_admits_vulnerable(req) for req, _ in _declared(manifest)] == admits


def test_pyproject_dependencies_and_uv_constraints_are_checked(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\ndependencies = ["PyJWT>=2.9.0"]\n'
        '[dependency-groups]\ndev = ["requests>=2.34.2", {include-group = "x"}]\n'
        '[tool.uv]\nconstraint-dependencies = ["aiohttp>=3.14.3"]\n',
        encoding="utf-8",
    )
    found = {(req.name, _admits_vulnerable(req), line) for req, line in _declared(manifest)}
    assert found == {("PyJWT", True, 2), ("requests", False, 4), ("aiohttp", True, 6)}


def test_unparseable_requirement_of_a_tracked_package_is_reported(tmp_path: Path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("PyJWT>=2.15.1; python_version >>> '3'\nnot-tracked>>>1\n", encoding="utf-8")
    assert [(requirement, line) for requirement, _, line in _parse(manifest)] == [(None, 1)]
