# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for the destroy.sh fallback security-group lookup.

`destroy.sh` Step 1 falls back to a name-based `describe-security-groups`
lookup when a Runtime's ARN is already gone from SSM (the normal case on a
second destroy run). The lookup must match only THIS deployment's
AgentCore/Mcp security groups: any other deployment's SG with attached ENIs
keeps Step 2's ENI-detach wait from reaching zero, so it burns the full wait
budget and exits 1, blaming a nonexistent AWS-side ENI delay.

The SGs have no explicit name, so CloudFormation names them
``<stack>-<LogicalId><hash>-<suffix>`` with stacks named
``<prefix>-<env>-serve`` / ``-mcp``. ENV and SCL_PREFIX may both contain
dashes, so a sibling deployment's stack name can start with this one's
``STACK_PREFIX`` (env ``dev-b`` vs ``dev``, prefix ``coa-x`` vs ``coa``).
EC2 filter ``*`` is a plain any-characters wildcard, modelled with
``fnmatchcase``.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "destroy.sh"


def _fallback_filter_line() -> str:
    text = _SCRIPT.read_text()
    match = re.search(r'--filters "Name=group-name,Values=([^"]+)"', text)
    assert match, "destroy.sh no longer has the expected describe-security-groups fallback filter"
    return match.group(1)


def _patterns(stack_prefix: str) -> list[str]:
    return [p.replace("${STACK_PREFIX}", stack_prefix) for p in _fallback_filter_line().split(",")]


def _matches(sg_name: str, stack_prefix: str) -> list[str]:
    return [p for p in _patterns(stack_prefix) if fnmatchcase(sg_name, p)]


def test_fallback_sg_filter_no_longer_matches_every_deployment_unscoped() -> None:
    filter_value = _fallback_filter_line()
    # A bare `*AgentCoreSG*`/`*McpSG*` glob with no prefix anchor matches every
    # deployment in the account/region, not just this one.
    assert "Values=*AgentCoreSG*" not in filter_value
    assert "Values=*McpSG*" not in filter_value


@pytest.mark.parametrize(
    "own_sg",
    [
        "coa-dev-serve-AgentCoreSG1A2B3C4D-ABCDEF012345",
        "coa-dev-mcp-McpSG9F8E7D6C-ABCDEF012345",
    ],
)
def test_fallback_sg_filter_own_deployment_matched(own_sg: str) -> None:
    assert _matches(own_sg, "coa-dev"), f"teardown of coa-dev would not find its own SG {own_sg!r}"


@pytest.mark.parametrize(
    "own_prefix, sibling_sg",
    [
        # Sibling env with a dash: SCL_PREFIX=coa, ENV=dev-b / dev-eu.
        ("coa-dev", "coa-dev-b-serve-AgentCoreSG1A2B3C4D-0123456789AB"),
        ("coa-dev", "coa-dev-b-mcp-McpSG9F8E7D6C-0123456789AB"),
        ("coa-dev", "coa-dev-eu-serve-AgentCoreSG9F8E7D6C-MNOPQRSTUVWX"),
        # Sibling prefix with a dash: own SCL_PREFIX=coa ENV=x, sibling SCL_PREFIX=coa-x ENV=dev.
        ("coa-x", "coa-x-dev-serve-AgentCoreSG1A2B3C4D-0123456789AB"),
        # Sibling env literally named after a stack suffix: ENV=dev-serve.
        ("coa-dev", "coa-dev-serve-serve-AgentCoreSG1A2B3C4D-0123456789AB"),
        ("coa-dev", "coa-dev-mcp-mcp-McpSG1A2B3C4D-0123456789AB"),
    ],
)
def test_fallback_sg_filter_sibling_deployment_not_matched(own_prefix: str, sibling_sg: str) -> None:
    hits = _matches(sibling_sg, own_prefix)
    assert not hits, f"teardown of {own_prefix!r} would wait on sibling SG {sibling_sg!r} via {hits}"


def test_fallback_sg_filter_other_sg_in_own_stack_not_matched() -> None:
    # Only the two runtime SGs are waited on, not another SG of the same stack.
    assert not _matches("coa-dev-serve-LambdaSG1A2B3C4D-ABCDEF012345", "coa-dev")
