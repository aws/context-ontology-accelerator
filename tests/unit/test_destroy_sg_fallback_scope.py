# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression test for the destroy.sh fallback security-group lookup.

`destroy.sh` Step 1 falls back to a name-based `describe-security-groups`
lookup when a Runtime's ARN is already gone from SSM (the normal case on a
second destroy run). Without a `STACK_PREFIX` scope, that lookup matches
every deployment's AgentCore/Mcp security groups in the account/region, so
Step 2's ENI-detach wait never reaches zero if a sibling deployment (a
different `SCL_PREFIX`) is still running — it burns the full wait budget
and exits 1, blaming a nonexistent AWS-side ENI delay.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "destroy.sh"


def _fallback_filter_line() -> str:
    text = _SCRIPT.read_text()
    match = re.search(r'--filters "Name=group-name,Values=([^"]+)"', text)
    assert match, "destroy.sh no longer has the expected describe-security-groups fallback filter"
    return match.group(1)


def test_fallback_sg_filter_is_scoped_to_stack_prefix() -> None:
    filter_value = _fallback_filter_line()
    assert "${STACK_PREFIX}-*AgentCoreSG*" in filter_value
    assert "${STACK_PREFIX}-*McpSG*" in filter_value


def test_fallback_sg_filter_no_longer_matches_every_deployment_unscoped() -> None:
    filter_value = _fallback_filter_line()
    # The bug: a bare `*AgentCoreSG*`/`*McpSG*` glob with no prefix anchor
    # matches every deployment in the account/region, not just this one.
    assert "Values=*AgentCoreSG*" not in filter_value
    assert "Values=*McpSG*" not in filter_value
