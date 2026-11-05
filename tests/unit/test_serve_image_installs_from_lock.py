# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The serve image installs the versions uv.lock pins, not whatever its pyproject ranges allow today.

The query service's SQL and graph parsing depends on the locked package versions. An untested release can
change query behavior. Every project install is constrained by scripts/uv_lock_constraints.py, which turns uv.lock
into one ``name==version`` line per registry package.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from scripts import uv_lock_constraints

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "packages" / "context-manager" / "Dockerfile"
LOCK = ROOT / "uv.lock"
_CI_DIR = ROOT / "ci"


def _install_commands(dockerfile: str | None = None) -> list[str]:
    if dockerfile is None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", dockerfile)
    commands = [line[len("RUN ") :] for line in text.splitlines() if line.startswith("RUN ")]
    return [part.strip() for command in commands for part in command.split("&&") if " install " in part]


# Matched anywhere in a shell segment, so an env prefix (`UV_NO_CACHE=1 uv pip install`) is covered too. Flags before
# `install` may take a separate value (`pip --cache-dir /tmp/c install`).
_INSTALLER = re.compile(
    r"(?<![\w.-])(pip3?(\.\d+)?|python3?(\.\d+)?\s+-m\s+pip|uv\s+(pip|tool)|pipx)(\s+-\S+(\s+[^-\s]\S*)?)*\s+install\b"
)
# Flags whose next argument is a path or value, never a requirement.
_VALUE_FLAGS = frozenset(
    {"-c", "--constraint", "--python", "-p", "--target", "-t", "--prefix", "--root"}
    | {"-i", "--index-url", "--extra-index-url", "-f", "--find-links"}
)
_CONSTRAINTS = "/tmp/constraints.txt"
# Upgraded to its newest release on purpose, for security fixes, as the OS packages are (`dnf upgrade`).
_FLOATING = frozenset({"pip"})


_HEREDOC = re.compile(r"<<(-?)[\"']?(\w+)[\"']?")
# A heredoc after a bare RUN or a shell is the script it runs; after any other command it is only that command's stdin.
_SHELL = re.compile(r"(\S*/)?(ba|da|z)?sh(\s+-\S+)*")
_COMMENT = re.compile(r"\s*#")


def _joined(lines: list[str], i: int) -> tuple[str, int]:
    """The line at `i` with its continuations joined, and the index after them.

    Docker drops comment lines before it joins continuations, so a comment inside a continued RUN ends nothing.
    """
    line = lines[i]
    i += 1
    while line.endswith("\\"):
        line = line[:-1]
        while i < len(lines) and _COMMENT.match(lines[i]):
            i += 1
        if i == len(lines):
            break
        line += " " + lines[i].strip()
        i += 1
    return line, i


def _heredoc_body(lines: list[str], i: int, marker: str, strip_tabs: bool) -> tuple[list[str], int]:
    """The raw lines up to the exact end marker (`<<-` strips leading tabs only), and the index after it."""
    body = []
    while i < len(lines):
        line = lines[i].lstrip("\t") if strip_tabs else lines[i]
        i += 1
        if line == marker:
            break
        body.append(line)
    return body, i


def _script_commands(lines: list[str]) -> list[str]:
    commands = []
    i = 0
    while i < len(lines):
        line, i = _joined(lines, i)
        if not _COMMENT.match(line):
            commands.append(line)
    return commands


def _run_commands(dockerfile: str) -> list[str]:
    """Each RUN instruction's shell command: continuations joined, comments dropped, heredocs and exec form read."""
    lines = dockerfile.splitlines()
    commands = []
    i = 0
    while i < len(lines):
        instruction, i = _joined(lines, i)
        run = re.match(r"\s*RUN\s+((--\S+\s+)*)(.*)", instruction, re.I)
        if not run:
            continue
        command = run.group(3)
        heredoc = _HEREDOC.search(command)
        if heredoc:
            body, i = _heredoc_body(lines, i, heredoc.group(2), strip_tabs=heredoc.group(1) == "-")
            command = (command[: heredoc.start()] + command[heredoc.end() :]).strip()
            if not command or _SHELL.fullmatch(command):
                commands.extend(_script_commands(body))
                continue
        elif command.startswith("["):
            command = " ".join(json.loads(command))
        commands.append(command)
    return commands


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[\s]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _parse_install(args: list[str]) -> tuple[list[str], bool, bool]:
    """An install's requirements, whether the lock's constraints apply, and whether it reads a requirements file."""
    requirements: list[str] = []
    constrained = reads_file = False
    i = 0
    while i < len(args):
        arg = args[i]
        reads_file = reads_file or arg in ("-r", "--requirement") or arg.startswith("--requirement=")
        value = args[i + 1] if arg in _VALUE_FLAGS and i + 1 < len(args) else arg.partition("=")[2]
        constrained = constrained or (arg.split("=")[0] in ("-c", "--constraint") and value == _CONSTRAINTS)
        if not arg.startswith("-"):
            requirements.append(arg)
        i += 2 if arg in _VALUE_FLAGS else 1
    return requirements, constrained, reads_file


def _covered(requirement: str, constrained: bool, locked: set[str]) -> bool:
    if re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9.]+", requirement):
        return True
    name = _requirement_name(requirement)
    if constrained and (requirement.startswith(("/", ".")) or name in locked):
        return True
    # Only the bare, unversioned upgrade floats: `pip<20` or `pip==9.0.0` is a version choice like any other.
    return requirement.lower() in _FLOATING


def _unpinned_installs(dockerfile: str) -> list[str]:
    """The Python installs in a Dockerfile that neither the lock's constraints nor an exact pin covers.

    A project path (`.`, `/tmp/common/`) passes only under the lock's constraints, a named package passes when it is
    `==`-pinned or the constraints name it, and a requirements file never passes: its contents are not checked here.
    """
    lock = uv_lock_constraints.constraints(LOCK.read_text(encoding="utf-8"))
    locked = {_requirement_name(line) for line in lock.splitlines()}
    unpinned = []
    for command in _run_commands(dockerfile):
        for segment in re.split(r"&&|\|\||;", command):
            found = _INSTALLER.search(segment)
            if not found:
                continue
            requirements, constrained, reads_file = _parse_install(shlex.split(segment[found.end() :]))
            if reads_file or not requirements or not all(_covered(r, constrained, locked) for r in requirements):
                unpinned.append(segment.strip())
    return unpinned


def test_every_project_install_in_the_serve_image_is_constrained_by_the_lock() -> None:
    installs = [c for c in _install_commands() if re.search(r"\s(/tmp/[\w-]+/|\.)(\s|$)", c)]
    assert installs, "the Dockerfile installs no project packages"
    for command in installs:
        assert command.startswith("uv pip install"), command
        assert "--constraint /tmp/constraints.txt" in command, command


def test_every_python_install_in_the_serve_image_is_constrained_or_pinned() -> None:
    """An install outside the lock (a tool, a pip/setuptools upgrade, a later added package) still pins its versions."""
    installs = [c for c in _install_commands() if re.match(r"(uv )?pip3? install", c)]
    assert installs, "the Dockerfile installs no Python packages"
    assert _unpinned_installs(DOCKERFILE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "line",
    [
        "RUN pip install httpx",
        "RUN pip install \\\n    httpx",
        "RUN pip install 'requests>=2'",
        "RUN python -m pip install httpx",
        "RUN python3.12 -m pip install httpx",
        "RUN UV_NO_CACHE=1 uv pip install --system httpx",
        "RUN pipx install httpx",
        "RUN uv tool install ruff",
        "RUN pip3.12 install httpx",
        "RUN --mount=type=cache,target=/root/.cache pip install httpx",
        "RUN true; pip install httpx",
        "run pip install httpx",
        'RUN ["pip", "install", "httpx"]',
        "RUN cd /tmp ; pip install httpx",
        "RUN pip install -c /tmp/constraints.txt some-unlocked-pkg",
        "RUN pip install --requirement=/tmp/req.txt uv==0.13.4",
        "RUN pip install -r /tmp/req.txt",
        "RUN pip install --no-cache-dir uv==0.13.4 && python -m pip install httpx",
        "RUN pip install uv==0.13.4 || pip install httpx",
        "RUN pip --no-cache-dir install httpx",
        "RUN pip  install httpx",
        "RUN <<EOF\npip install httpx\nEOF",
        "RUN pip install uv==0.13.4 && \\\n    # a comment line inside the continuation\n    pip install httpx",
        "RUN pip install 'pip<20'",
        "RUN pip --cache-dir /tmp/c install httpx",
        "RUN python -m pip --log /tmp/l install httpx",
        "RUN uv pip --directory /x install --system httpx",
        "RUN <<EOF xargs pip install\nhttpx\nEOF",
        "RUN <<EOF pip install -r /dev/stdin\nhttpx\nEOF",
        "RUN bash -e <<EOF\npip install httpx\nEOF",
        "RUN <<EOF\n  EOF\npip install httpx\nEOF",
    ],
)
def test_an_unpinned_install_in_any_form_is_caught(line: str) -> None:
    assert _unpinned_installs(f"FROM scratch\n{line}\n"), line


@pytest.mark.parametrize(
    "line",
    [
        "RUN pip install --no-cache-dir uv==0.13.4",
        "RUN uv pip install --system --python /usr/bin/python3.12 --constraint /tmp/constraints.txt /tmp/common/",
        "RUN pip install --no-cache-dir -c /tmp/constraints.txt 'setuptools>=78.1.1'",
        "RUN pip uninstall -y msgpack",
        "RUN <<EOF cat > /etc/motd\npip install httpx\nEOF",
        "RUN <<EOF\npip install uv==0.13.4 \\\nEOF\nRUN true",
    ],
)
def test_a_pinned_or_lock_constrained_install_passes(line: str) -> None:
    assert _unpinned_installs(f"FROM scratch\n{line}\n") == [], line


def test_the_image_writes_its_constraints_from_uv_lock() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY uv.lock scripts/uv_lock_constraints.py /tmp/lock/" in text
    assert "python /tmp/lock/uv_lock_constraints.py /tmp/lock/uv.lock > /tmp/constraints.txt" in text


def test_the_docker_context_admits_the_lock_and_the_script() -> None:
    allowed = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!uv.lock" in allowed
    assert "!scripts/uv_lock_constraints.py" in allowed


def test_the_constraints_pin_every_registry_package_at_its_locked_version() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    registry = {p["name"]: p["version"] for p in lock["package"] if "registry" in p["source"]}
    lines = uv_lock_constraints.constraints(LOCK.read_text(encoding="utf-8")).splitlines()
    assert lines == sorted(f"{name}=={version}" for name, version in registry.items())
    for pinned in ("rdflib==", "sqlglot==", "pyparsing=="):
        assert any(line.startswith(pinned) for line in lines), pinned


def test_workspace_editable_virtual_and_url_packages_are_not_constrained() -> None:
    lock = """
version = 1
[[package]]
name = "coa-common"
version = "0.3.7"
source = { editable = "libs/common" }
[[package]]
name = "coa"
version = "0.3.7"
source = { virtual = "." }
[[package]]
name = "en-core-web-sm"
version = "3.8.0"
source = { url = "https://example.test/en_core_web_sm-3.8.0-py3-none-any.whl" }
[[package]]
name = "rdflib"
version = "7.6.0"
source = { registry = "https://pypi.org/simple" }
"""
    assert uv_lock_constraints.constraints(lock) == "rdflib==7.6.0\n"


def test_a_forked_package_is_constrained_per_resolution_marker() -> None:
    """uv writes one entry per fork, each with its own resolution-markers; each becomes a marker-qualified line."""
    lock = """
version = 1
[[package]]
name = "numpy"
version = "2.3.5"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["python_full_version >= '3.14' and sys_platform == 'win32'", "python_full_version >= '3.14'"]
[[package]]
name = "numpy"
version = "2.2.6"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["python_full_version < '3.14'"]
[[package]]
name = "rdflib"
version = "7.6.0"
source = { registry = "https://pypi.org/simple" }
"""
    assert uv_lock_constraints.constraints(lock).splitlines() == [
        "numpy==2.2.6 ; (python_full_version < '3.14')",
        "numpy==2.3.5 ; (python_full_version >= '3.14' and sys_platform == 'win32') or (python_full_version >= '3.14')",
        "rdflib==7.6.0",
    ]


def test_a_package_locked_at_two_versions_without_markers_is_refused() -> None:
    entry = '[[package]]\nname = "sqlglot"\nversion = "{}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    lock = "version = 1\n" + entry.format("30.8.0") + entry.format("30.24.0")
    with pytest.raises(ValueError, match="sqlglot"):
        uv_lock_constraints.constraints(lock)


def test_the_image_and_ci_install_the_same_uv_that_the_project_requires() -> None:
    """The image, the relock script and CI each pin uv; a bump to one alone would resolve with different uvs.

    ci/ is not in the public mirror, so there only the image and the relock script are compared.
    """
    sources = {
        "packages/context-manager/Dockerfile": r"\buv==([0-9.]+)",
        "scripts/relock-python-requirements.sh": r"^UV_VERSION=([0-9.]+)$",
    }
    if _CI_DIR.is_dir():
        sources["ci/merge-request.yml"] = r"\buv==([0-9.]+)"
    pins = {
        path: set(re.findall(pattern, (ROOT / path).read_text(encoding="utf-8"), re.M))
        for path, pattern in sources.items()
    }
    assert all(pins.values()), pins
    versions = set().union(*pins.values())
    assert len(versions) == 1, pins
    required = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["required-version"]
    floor = re.fullmatch(r">=\s*([0-9.]+)", required)
    assert floor, required
    pinned = versions.pop()
    assert tuple(map(int, pinned.split("."))) >= tuple(map(int, floor.group(1).split("."))), (pinned, required)


def test_a_package_forked_between_the_registry_and_another_source_is_refused() -> None:
    """Constraining only the registry fork would pin it on every Python, against the other fork's markers."""
    lock = """
version = 1
[[package]]
name = "numpy"
version = "2.2.6"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["python_full_version < '3.14'"]
[[package]]
name = "numpy"
version = "2.3.5"
source = { git = "https://example.test/numpy.git?rev=v2.3.5#0123456789abcdef" }
resolution-markers = ["python_full_version >= '3.14'"]
"""
    with pytest.raises(ValueError, match="numpy"):
        uv_lock_constraints.constraints(lock)
