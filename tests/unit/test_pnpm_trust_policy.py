# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the pnpm frozen-lockfile trust policy."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_EXCLUSIONS = {
    "pnpm@10.34.5",
    "semver@6.3.1",
    "undici-types@6.21.0",
}


def test_trust_downgrade_exclusions_are_exact_and_locked() -> None:
    """Reviewed exceptions must stay version-scoped and present in the lockfile."""
    workspace = (_REPO_ROOT / "pnpm-workspace.yaml").read_text()

    assert re.search(r"^trustPolicy: no-downgrade$", workspace, re.MULTILINE)
    exclusions_block = re.search(
        r"^trustPolicyExclude:\n((?:  - [^\n]+\n)+)",
        workspace,
        re.MULTILINE,
    )
    assert exclusions_block, "pnpm-workspace.yaml must declare trustPolicyExclude"
    exclusions = {line.removeprefix("  - ").strip() for line in exclusions_block.group(1).splitlines()}
    assert exclusions == _EXPECTED_EXCLUSIONS

    lockfile = (_REPO_ROOT / "pnpm-lock.yaml").read_text()
    for selector in _EXPECTED_EXCLUSIONS:
        assert re.search(rf"^  {re.escape(selector)}:$", lockfile, re.MULTILINE), (
            f"{selector} is exempted from the trust policy but is not pinned in pnpm-lock.yaml"
        )
