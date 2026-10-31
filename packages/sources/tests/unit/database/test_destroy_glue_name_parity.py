# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""scripts/destroy.sh finds the federated Glue connections a deployment created by name.

It derives that name in bash (scripts/lib/glue-catalog-name.sh); these tests pin it to
the derivation the sources service names them with, so the two cannot drift apart. A
prefix derived the wrong way matches nothing, and the teardown would silently leave
the connections (and the network stack they hold) behind again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from coa_sources.database.connectors.glue_connection_provisioner import build_catalog_name
from coa_sources.database.glue_ownership import platform_catalog_prefix

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[5] / "scripts" / "lib" / "glue-catalog-name.sh"
_SOURCE_IDS = ("0d6c1c3e-6f7a-4c1b-9a63-2f3f0e1a9b10", "DS#x", "")


def _bash(function: str, prefix: str, env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; "$@"', "-", str(_LIB), function, prefix, env],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("prefix", "env"),
    [
        ("coa", "dev"),
        ("COA", "dev"),
        ("coa_", "dev"),
        ("coa", "dev-1"),
        ("coa", "dev1"),
        ("scl", "x"),
        ("averyveryverylongprefix", "development"),  # the digest is cut to fit 41 characters
    ],
)
def test_destroy_derives_the_names_the_provisioner_gives(
    monkeypatch: pytest.MonkeyPatch, prefix: str, env: str
) -> None:
    resource_prefix = f"{prefix}-{env}-"  # what the sources stack sets: prefixed("")
    monkeypatch.setenv("RESOURCE_PREFIX", resource_prefix)

    name_prefix = _bash("coa_glue_catalog_prefix", prefix, env)
    assert name_prefix.stdout.strip() == platform_catalog_prefix()

    pattern = _bash("coa_glue_catalog_pattern", prefix, env)
    assert pattern.returncode == 0, pattern.stderr
    for source_id in _SOURCE_IDS:
        name = build_catalog_name(resource_prefix, source_id)
        assert re.fullmatch(pattern.stdout.strip(), name), (pattern.stdout, name)


def test_aliased_prefixes_share_names_so_the_teardown_also_checks_the_role() -> None:
    names = {_bash("coa_glue_catalog_prefix", p, e).stdout.strip() for p, e in [("coa", "dev-1"), ("coa", "dev1")]}
    assert names == {"coadev1ds_"}
    assert build_catalog_name("coa-dev-1-", "s") == build_catalog_name("coa-dev1-", "s")


def test_a_sibling_envs_names_do_not_match() -> None:
    pattern = _bash("coa_glue_catalog_pattern", "coa", "dev").stdout.strip()
    assert not re.fullmatch(pattern, build_catalog_name("coa-dev-1-", "s"))
    assert not re.fullmatch(pattern, build_catalog_name("coa-dev-b-", "s"))


def test_a_digit_leading_prefix_gets_no_pattern() -> None:
    # build_catalog_name prepends a second "ds_" there, outside the {prefix}ds_* shape.
    assert build_catalog_name("1coa-dev-", "s").startswith("ds_1coadevds_")
    result = _bash("coa_glue_catalog_pattern", "1coa", "dev")
    assert result.returncode == 1
    assert result.stdout == ""
