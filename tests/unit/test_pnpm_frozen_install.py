# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The setup, deploy and destroy paths install exactly what pnpm-lock.yaml pins.

The public mirror leaves out workspace packages that ``pnpm-workspace.yaml`` and
``pnpm-lock.yaml`` still list (``tests/cdk``, ``benchmarks/dashboard``). A plain
``pnpm install`` there drops them from the lockfile, and the next ``git pull`` of a
release fails on that local change. ``--frozen-lockfile`` installs the lockfile as-is
and never writes it.

pnpm 11+ also runs a plain ``pnpm install`` on its own before ``pnpm nx`` or
``pnpm exec`` when node_modules is out of date, as it is right after a ``git pull``. The
deploy scripts therefore install the locked dependencies before their first such call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = re.compile(r"\bpnpm install\b")


def _command_lines(path: Path) -> list[tuple[int, str]]:
    """Non-comment lines, with Makefile `##` and shell `#` comments dropped."""
    lines = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((number, stripped))
    return lines


def _user_facing_files() -> list[Path]:
    return [_ROOT / "Makefile", *sorted((_ROOT / "scripts").rglob("*.sh"))]


def test_every_install_on_the_user_path_is_frozen() -> None:
    plain = [
        f"{path.relative_to(_ROOT)}:{number}: {line}"
        for path in _user_facing_files()
        for number, line in _command_lines(path)
        if _INSTALL.search(line) and "--frozen-lockfile" not in line
    ]
    assert not plain, "plain `pnpm install` rewrites pnpm-lock.yaml on the public repo:\n" + "\n".join(plain)


@pytest.mark.parametrize(
    ("script", "first_pnpm_use"),
    [
        ("scripts/deploy.sh", re.compile(r"^pnpm (nx|--filter)\b")),
        ("scripts/deploy-serve.sh", re.compile(r"^pnpm --filter\b")),
    ],
)
def test_deploy_installs_locked_dependencies_before_first_pnpm_command(script: str, first_pnpm_use) -> None:
    lines = [line for _, line in _command_lines(_ROOT / script)]
    install = next((i for i, line in enumerate(lines) if line.startswith("pnpm install --frozen-lockfile")), None)
    first_use = next((i for i, line in enumerate(lines) if first_pnpm_use.search(line)), None)
    assert install is not None, f"{script} must run `pnpm install --frozen-lockfile`"
    assert first_use is not None, f"{script} no longer runs pnpm; update this test"
    assert install < first_use, f"{script} must install locked dependencies before `{lines[first_use]}`"
