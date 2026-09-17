# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A14 — contract-conformance test for the Smithy ``Query`` operation.

Guards the *declared-but-inert API member* defect class (B3/B6/B7 in
fashion-findings-final.md). B7 was the live instance: ``timeoutMs`` was declared
on the ``Query`` input for a long time but no consumer read it, so a caller's
requested budget was silently ignored. The generic guard here is: every member
declared on the ``Query`` input MUST reach a consumer — for the REST transport
that consumer is the data-layer handler, which forwards each option to the
Context Manager. A member that the handler neither forwards nor deliberately
drops is a silent no-op and fails this test.

This catches the whole class for the ``Query`` operation rather than the three
known instances: add a new option to the Smithy model and forget to wire it, and
this test goes red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Repo layout: packages/data-layer/tests/unit/<this> ->
# up 4 = packages/data-layer, up 5 = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SERVE_SMITHY = _REPO_ROOT / "models" / "src" / "main" / "smithy" / "serve.smithy"
_HANDLER = _REPO_ROOT / "packages" / "data-layer" / "src" / "coa_data_layer" / "handler.py"

# Members that are NOT request options carried in the forwarded ``options`` map:
# httpLabel path params and the required top-level query text are handled on
# their own dedicated code paths, so they are not expected in the options
# forwarding block.
_NON_OPTION_MEMBERS = {"namespaceId", "query"}


def _extract_query_input_members(smithy_text: str) -> list[str]:
    """Parse the member names of the ``Query`` operation's ``input := { ... }``.

    Deliberately a small hand parser (no smithy tooling dep in the data-layer
    unit env): find ``operation Query {``, then the ``input := {`` block, then
    collect ``name:`` member declarations until the block's closing brace,
    skipping trait lines (``@...``) and doc comments (``///``).
    """
    op = re.search(r"operation\s+Query\s*\{", smithy_text)
    assert op, "operation Query not found in serve.smithy"
    # Locate the input block start after the operation opening brace.
    inp = re.search(r"input\s*:=\s*\{", smithy_text[op.end() :])
    assert inp, "Query input block not found"
    start = op.end() + inp.end()

    members: list[str] = []
    depth = 1  # we are inside the input '{'
    for line in smithy_text[start:].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if not stripped or stripped.startswith("@") or stripped.startswith("///"):
            continue
        m = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:", stripped)
        if m:
            members.append(m.group(1))
    return members


def test_serve_smithy_present():
    assert _SERVE_SMITHY.is_file(), f"serve.smithy not found at {_SERVE_SMITHY}"
    assert _HANDLER.is_file(), f"handler.py not found at {_HANDLER}"


def test_every_query_input_option_reaches_a_consumer():
    """B7-class guard: each Query option member is forwarded by the handler.

    A member that the handler never references is a declared-but-inert field —
    the exact defect (timeoutMs) that made a caller's requested budget a silent
    no-op. If you add a Query option to the Smithy model, wire it here too.
    """
    smithy_text = _SERVE_SMITHY.read_text()
    handler_text = _HANDLER.read_text()

    members = _extract_query_input_members(smithy_text)
    # Sanity: the parser found the members we know are declared (guards against a
    # silent parse failure making the conformance check vacuously pass).
    assert {"timeoutMs", "mode", "maxResults", "dimensions"}.issubset(set(members)), (
        f"parser regression — Query members parsed as {members}"
    )

    option_members = [m for m in members if m not in _NON_OPTION_MEMBERS]
    missing = [
        m
        for m in option_members
        # The handler references each forwarded option by its wire name as a
        # quoted string key (body.get("X") / payload["options"]["X"]).
        if f'"{m}"' not in handler_text
    ]
    assert not missing, (
        "Query input members declared in serve.smithy but not consumed by the "
        f"data-layer handler (declared-but-inert, B7 class): {missing}"
    )


def test_conformance_guard_would_catch_an_inert_member():
    """Negative control: an invented, never-consumed member must be flagged.

    Proves the guard can actually FAIL — a conformance check that cannot fail
    proves nothing. Simulates adding ``phantomOption`` to the contract without
    wiring it, and asserts the same membership rule reports it missing.
    """
    handler_text = _HANDLER.read_text()
    fake_members = ["mode", "timeoutMs", "phantomOption"]
    missing = [m for m in fake_members if f'"{m}"' not in handler_text]
    assert missing == ["phantomOption"], "negative control failed — an unwired member should be reported missing"
