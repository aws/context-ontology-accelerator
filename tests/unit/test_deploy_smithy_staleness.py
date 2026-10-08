# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deploy preflight's Smithy staleness check (``smithy-fingerprint.sh``).

``smithy-generated/`` is gitignored, so ``git pull`` leaves it at the previous
commit's output. The preflight used to accept it whenever any OpenAPI spec
existed, so a model change (a renamed enum member) shipped handlers against the
old generated code and the Lambda failed at import. These tests pin the state
the preflight acts on: ``missing`` / ``unstamped`` / ``stale`` regenerate,
``current`` does not.

Each test builds a throwaway repo with the script and fake codegen inputs; no
Gradle or codegen runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "smithy-fingerprint.sh"
_MODEL = "models/src/main/smithy/sources.smithy"


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    for rel, text in {
        _MODEL: "enum SourceSubType { CUSTOM_CONNECTOR }\n",
        "models/build.gradle.kts": "plugins {}\n",
        "models/smithy-build.json": "{}\n",
        "models/gradle/wrapper/gradle-wrapper.properties": "distributionUrl=gradle-8.zip\n",
        "scripts/smithy-generate.sh": "#!/usr/bin/env bash\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copy(_SCRIPT, root / "scripts" / "smithy-fingerprint.sh")
    return root


def _run(root: Path, mode: str) -> str:
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "smithy-fingerprint.sh"), mode, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _generate(root: Path) -> None:
    """What a successful smithy-generate.sh leaves behind: specs plus the stamp."""
    specs = root / "smithy-generated" / "openapi"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "ControlPlaneService.openapi.json").write_text("{" + '"x": 1, ' * 40 + '"y": 2}')
    (root / "smithy-generated" / ".inputs.sha256").write_text(_run(root, "hash") + "\n")


def test_no_generated_output_is_missing(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path), "state") == "missing"


def test_output_from_before_fingerprinting_is_unstamped(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _generate(root)
    (root / "smithy-generated" / ".inputs.sha256").unlink()
    assert _run(root, "state") == "unstamped"


def test_freshly_generated_output_is_current(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _generate(root)
    assert _run(root, "state") == "current"


def test_renamed_enum_member_after_pull_is_stale(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _generate(root)
    (root / _MODEL).write_text("enum SourceSubType { ATHENA_CONNECTOR }\n")
    assert _run(root, "state") == "stale"


@pytest.mark.parametrize(
    "rel",
    ["models/build.gradle.kts", "models/smithy-build.json", "scripts/smithy-generate.sh"],
)
def test_codegen_config_change_is_stale(tmp_path: Path, rel: str) -> None:
    root = _repo(tmp_path)
    _generate(root)
    (root / rel).write_text((root / rel).read_text() + "# changed\n")
    assert _run(root, "state") == "stale"


def test_added_model_file_is_stale(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _generate(root)
    (root / "models/src/main/smithy/new.smithy").write_text("structure New {}\n")
    assert _run(root, "state") == "stale"


def test_timestamp_only_change_stays_current(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _generate(root)
    os.utime(root / _MODEL, (1, 1))
    assert _run(root, "state") == "current"


def test_fingerprint_is_independent_of_the_clone_path(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path, "a"), "hash") == _run(_repo(tmp_path, "elsewhere/b"), "hash")


def test_generate_script_stamps_after_all_generation() -> None:
    """The stamp is the script's final write, so set -e never leaves a partial run stamped."""
    text = (_ROOT / "scripts" / "smithy-generate.sh").read_text()
    stamp = text.index('> "$GENERATED_DIR/.inputs.sha256"')
    assert stamp > text.rindex("java -jar")
    assert stamp > text.rindex("cat >")
    assert text.index('smithy-fingerprint.sh" hash') < text.index('rm -rf "$GENERATED_DIR"')


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts" / "smithy-fingerprint.sh"), "bogus", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
