# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for onboard-demo.sh's namespace-tag merge (``merge_ns_value``).

The demo onboards several industries into ONE account, and two namespaces
legitimately share a single Postgres credential secret. Every tag write is
therefore read-merge-write, and the failure this guards is silent: a blind
overwrite unbinds whichever industry onboarded first, and nothing notices until
that namespace's next scan fails or its serve-time secret read is denied.

Three properties matter and none are visible by reading a single tag:

- an existing namespace is preserved when a second one is added (the shared case)
- ``ALL`` is dropped rather than merged — it is a Glue-only wildcard valid only as
  the WHOLE value, so ``ALL <uuid>`` is a value ``parse_namespace_tag`` rejects
- output is canonical (single space, no padding), because the IAM conditions that
  enforce the same binding match entries on literal space boundaries

The function is pure, so it is extracted from the script and exercised directly;
no AWS calls are made.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "onboard-demo.sh"

if not _SCRIPT.exists():
    pytest.skip("scripts/onboard-demo.sh is absent from this checkout", allow_module_level=True)

_NS = "11111111-1111-4111-8111-111111111111"
_OTHER = "22222222-2222-4222-8222-222222222222"
_THIRD = "33333333-3333-4333-8333-333333333333"


def _merge_fn() -> str:
    """Extract the ``merge_ns_value`` definition from the script.

    Extracted rather than duplicated so the test cannot drift from the shipped
    implementation — a copy would keep passing after the real function changed.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^merge_ns_value\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "merge_ns_value() not found in onboard-demo.sh — did it get renamed?"
    return match.group(0)


def _merge(current: str, ns: str = _NS) -> str:
    """Run the extracted function with NS=*ns* against a *current* tag value."""
    script = f'NS="{ns}"\n{_merge_fn()}\nmerge_ns_value "$1"\n'
    proc = subprocess.run(
        ["bash", "-c", script, "bash", current],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_empty_value_binds_only_this_namespace() -> None:
    assert _merge("") == _NS


def test_existing_other_namespace_is_preserved() -> None:
    """The shared-secret case: adding a namespace must not unbind the first."""
    assert _merge(_OTHER) == f"{_OTHER} {_NS}"


def test_all_wildcard_is_replaced_not_merged() -> None:
    """``ALL`` is Glue-only and invalid as a list entry, so it must not survive."""
    assert _merge("ALL") == _NS
    assert "ALL" not in _merge("ALL")


def test_rerun_is_idempotent() -> None:
    assert _merge(_NS) == _NS
    assert _merge(f"{_OTHER} {_NS}") == f"{_OTHER} {_NS}"


def test_output_is_canonical_for_untidy_input() -> None:
    """Padded / repeated separators must normalize; parse_namespace_tag rejects them."""
    merged = _merge(f"  {_OTHER}   {_THIRD}  ")
    assert merged == f"{_OTHER} {_THIRD} {_NS}"
    assert merged == merged.strip()
    assert "  " not in merged


def test_caps_at_six_namespaces_to_fit_the_tag_value_limit() -> None:
    """An AWS tag value caps at 256 chars; a uuid plus separator is 37."""
    existing = " ".join(f"{i}1111111-1111-4111-8111-111111111111" for i in range(1, 7))
    merged = _merge(existing)
    ids = merged.split(" ")
    assert len(ids) == 6
    assert ids[-1] == _NS, "the namespace being onboarded must always be present"
    assert len(merged) <= 256
