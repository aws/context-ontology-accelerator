# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ONTOP_JAVA_ARGS heap fallback in entrypoint.sh.

Line 113 of entrypoint.sh sets the Ontop heap via ONTOP_JAVA_ARGS (the variable
the launcher actually reads; JAVA_OPTS was a silent no-op that caused #149 cause
C). These tests execute the REAL expansion line pulled from the script — not a
re-implementation — so a future edit that breaks the fallback or reverts to
JAVA_OPTS is caught here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _heap_export_line() -> str:
    """Extract the exact `export ONTOP_JAVA_ARGS=...` line from entrypoint.sh.

    Reading it from source keeps the test bound to the real logic; if the line
    is removed or changed the extraction (and the assertions) will surface it.
    """
    text = _ENTRYPOINT.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export ONTOP_JAVA_ARGS="):
            return stripped
    raise AssertionError("no `export ONTOP_JAVA_ARGS=` line found in entrypoint.sh")


def _resolve(env: dict[str, str]) -> str:
    """Run the real export line under bash with the given env and echo the result."""
    line = _heap_export_line()
    script = f'{line}\nprintf "%s" "$ONTOP_JAVA_ARGS"'
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class TestOntopJavaArgsFallback:
    def test_uses_ontop_java_args_when_set(self):
        assert _resolve({"ONTOP_JAVA_ARGS": "-Xmx1536m -Xms512m"}) == "-Xmx1536m -Xms512m"

    def test_ontop_java_args_wins_over_java_opts(self):
        # Both set -> ONTOP_JAVA_ARGS takes precedence (it is the authoritative var).
        out = _resolve({"ONTOP_JAVA_ARGS": "-Xmx2048m", "JAVA_OPTS": "-Xmx768m"})
        assert out == "-Xmx2048m"

    def test_falls_back_to_java_opts_when_only_java_opts_set(self):
        # Back-compat: a task def that still only sets JAVA_OPTS is honoured
        # rather than silently ignored (the #149 cause C failure mode).
        assert _resolve({"JAVA_OPTS": "-Xmx768m -Xms256m"}) == "-Xmx768m -Xms256m"

    def test_empty_when_neither_set(self):
        # Neither var: expand to empty so the Ontop launcher uses its built-in
        # default rather than erroring on an unbound variable.
        assert _resolve({}) == ""

    def test_line_targets_ontop_java_args_not_java_opts(self):
        # Guard against a regression that exports JAVA_OPTS (the dead var).
        line = _heap_export_line()
        assert re.match(r"^export ONTOP_JAVA_ARGS=", line)
