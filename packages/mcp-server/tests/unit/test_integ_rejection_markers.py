# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every auth rejection the MCP server can raise is recognised by the integ suite.

The integ tool tests treat an ``isError`` result as a pass unless its text
matches a rejection marker, so a rejection message no marker covers would make
those tests pass while the server refused the call.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[2]
_AUTH_DIR = _PKG / "src" / "coa_mcp" / "auth"
_INTEG_MODULE_PATH = _PKG / "tests" / "integ" / "test_mcp_runtime.py"
_REJECTIONS = {"GrantResolutionError", "ClaimsExtractionError"}

if not _INTEG_MODULE_PATH.exists():
    pytest.skip("tests/integ/ is absent or incomplete in this checkout (public mirror)", allow_module_level=True)


@functools.cache
def _integ_module():
    spec = importlib.util.spec_from_file_location("mcp_integ_runtime", _INTEG_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constant_text(node: ast.expr) -> str:
    """The constant parts of a message, with each interpolation as a gap."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "\x00".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _rejection_messages() -> list[tuple[str, str]]:
    found = []
    for path in sorted(_AUTH_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _REJECTIONS and node.exc.args:
                found.append((f"{path.name}:{node.lineno}", _constant_text(node.exc.args[0])))
    return found


MESSAGES = _rejection_messages()


@pytest.mark.unit
def test_auth_rejections_are_found():
    # Guards the scan itself: an empty list would make the test below vacuous.
    assert len(MESSAGES) >= 6


@pytest.mark.unit
@pytest.mark.parametrize(("where", "message"), MESSAGES, ids=[w for w, _ in MESSAGES])
def test_every_auth_rejection_matches_a_marker(where: str, message: str):
    markers = _integ_module()._REJECTION_MARKERS
    assert message, f"{where}: rejection message has no constant text to match"
    assert any(m in message.lower() for m in markers), f"{where}: {message!r} matches no _REJECTION_MARKERS entry"


def _server_bearer_rejections() -> list[tuple[str, str]]:
    path = _PKG / "src" / "coa_mcp" / "server.py"
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            text = _constant_text(node.exc.args[0])
            if "bearer" in text.lower():
                found.append((f"server.py:{node.lineno}", text))
    return found


@pytest.mark.unit
def test_server_bearer_rejections_match_a_marker():
    rejections = _server_bearer_rejections()
    assert len(rejections) >= 2
    markers = _integ_module()._REJECTION_MARKERS
    for where, message in rejections:
        assert any(m in message.lower() for m in markers), f"{where}: {message!r} matches no marker"


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        # Status-carrying formats from server.py and clients/, with the status filled in.
        "CM 401: denied",
        "CM 403: denied",
        "CM 500: boom",
        "Context Manager returned 502: bad gateway",
        "Lambda returned 500: boom",
        # Backend failures that mean the call never reached a working tool.
        "Context Manager request timed out (30s)",
        "HTTP error invoking Context Manager: connection reset",
        "Lambda invoke failed: throttled",
        "Lambda function error: Unhandled",
    ],
)
def test_backend_failure_formats_match_a_marker(message: str):
    markers = _integ_module()._REJECTION_MARKERS
    assert any(m in message.lower() for m in markers), f"{message!r} matches no marker"
