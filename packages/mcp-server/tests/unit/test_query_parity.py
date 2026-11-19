# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Parity between the Smithy ``Query`` input and the MCP ``query`` tool.

The MCP tool is hand-written, so a member added to ``serve.smithy`` does not
reach it automatically. This test fails when a new ``Query`` input member is
neither an MCP tool parameter nor listed below as deliberately omitted, and
when an MCP parameter has no matching Smithy member. It compares names only,
not types or bounds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from coa_mcp import server

_SERVE_SMITHY = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy" / "serve.smithy"

# Smithy ``Query`` input members the MCP ``query`` tool does not expose, each with
# the reason. Remove an entry when the tool gains the parameter.
_OMITTED_FROM_MCP: dict[str, str] = {
    "includeLlmUsage": "observability only, never changes how the query runs; not exposed over MCP yet",
}


# Parameters whose MCP default differs from the Smithy default, with the MCP default.
# Smithy documents the REST default in the member's doc comment and says the MCP default there too.
_MCP_DEFAULT_DIFFERS: dict[str, bool] = {"includeDebugInfo": True}


def _smithy_member_doc(name: str) -> str:
    """The ``///`` doc comment above one ``Query`` input member."""
    text = _SERVE_SMITHY.read_text()
    op = re.search(r"operation Query \{(.*?)\n\}", text, re.DOTALL)
    assert op, "operation Query not found in serve.smithy"
    m = re.search(rf"((?:^\s*///[^\n]*\n)+)(?:\s*@[^\n]*\n)*\s*{name}\s*:", op.group(1), re.MULTILINE)
    assert m, f"no doc comment found for Query.{name}"
    return m.group(1)


def _smithy_query_input_members() -> set[str]:
    op = re.search(r"operation Query \{(.*?)\n\}", _SERVE_SMITHY.read_text(), re.DOTALL)
    assert op, "operation Query not found in serve.smithy"
    block = re.search(r"input := \{(.*?)\n\s*\}\s*\n\s*output", op.group(1), re.DOTALL)
    assert block, "Query input block not found in serve.smithy"
    body = re.sub(r'""".*?"""', "", block.group(1), flags=re.DOTALL)  # text-block trait values
    body = re.sub(r"@\w+\((?:[^()]|\([^()]*\))*\)", "", body)  # trait arguments, even multi-line
    members = set(re.findall(r"^\s*(?:@\w+\s+)*([A-Za-z]\w*)\s*:", body, re.MULTILINE))
    assert members, "no members parsed from the Query input"
    return members


async def _mcp_query_params() -> set[str]:
    """Parameter names of the published MCP ``query`` tool (what clients see)."""
    tools = await server.mcp.list_tools()
    return set(next(t for t in tools if t.name == "query").inputSchema["properties"])


@pytest.mark.unit
class TestQueryInputParity:
    def test_parser_reads_known_members(self):
        """Guard the regex: a parser that matched nothing would make the parity check vacuous."""
        members = _smithy_query_input_members()
        assert {"namespaceId", "query", "execute", "includeDebugInfo", "timeoutMs"} <= members

    @pytest.mark.asyncio
    async def test_every_smithy_member_is_an_mcp_param_or_explicitly_omitted(self):
        missing = _smithy_query_input_members() - await _mcp_query_params() - set(_OMITTED_FROM_MCP)
        assert not missing, (
            f"Smithy Query input members missing from the MCP query tool: {sorted(missing)}. "
            "Add them to the tool, or to _OMITTED_FROM_MCP with the reason."
        )

    @pytest.mark.asyncio
    async def test_every_mcp_param_is_a_smithy_member(self):
        """The reverse direction: a renamed or misspelt MCP param would reach serve as a field it ignores."""
        extra = await _mcp_query_params() - _smithy_query_input_members()
        assert not extra, f"MCP query params with no Smithy Query input member: {sorted(extra)}"

    @pytest.mark.asyncio
    async def test_omitted_list_is_not_stale(self):
        """Every omitted entry still exists in Smithy and is still absent from MCP."""
        members = _smithy_query_input_members()
        params = await _mcp_query_params()
        for name in _OMITTED_FROM_MCP:
            assert name in members, f"{name} is no longer a Smithy Query input member"
            assert name not in params, f"{name} is now an MCP param; drop it from _OMITTED_FROM_MCP"

    @pytest.mark.asyncio
    async def test_mcp_defaults_that_differ_from_smithy_are_listed_and_documented(self):
        """An MCP default is never a silent difference: it is listed here and the Smithy doc says so."""
        tools = await server.mcp.list_tools()
        props = next(t for t in tools if t.name == "query").inputSchema["properties"]
        for name, mcp_default in _MCP_DEFAULT_DIFFERS.items():
            assert props[name].get("default") is mcp_default, f"{name}: MCP default is not {mcp_default}"
            doc = _smithy_member_doc(name)
            assert "Defaults to false on this API" in doc and "MCP" in doc, (
                f"Smithy doc for {name} omits the MCP default"
            )
