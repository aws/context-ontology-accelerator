# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every DataLayerService input member is read by the data-layer handler that serves it.

Guards the *declared-but-inert member* defect class: a member the Smithy model
advertises that the handler never reads is silently dropped — the caller gets a
200 computed without it. ``KBSearch`` was the live instance (``minScore``,
``sourceFilter`` and ``entityFilter`` were declared but only ``topK`` was read).

Source of truth is the generated OpenAPI (``smithy-generated/openapi/openapi/
DataLayerService.openapi.json``), i.e. exactly what API Gateway and clients are
told. For each operation the handler function is found through the handler's own
``_ROUTE_MAP`` (method + resource), and its source — plus every module-level
helper it calls, transitively — is AST-scanned for ``body.get("x")`` /
``body["x"]`` (request body) or ``query.get("x")`` / ``query["x"]`` (query
string). A read through a module-level member table also counts: when a
function calls ``body.get(name)`` with a non-literal key and references a
module-level dict/tuple whose keys are string literals (``_QUERY_INT_MEMBERS``),
those keys are treated as read. A member with no such read fails the test.

Reading a member is necessary, not sufficient: ``test_contract_bounds.py`` checks
the bounds, and the handler tests check what is forwarded.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

with patch.dict(
    "os.environ",
    {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
        "AWS_REGION": "us-east-1",
        "ALLOWED_ORIGIN": "https://app.example.com",
        "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
    },
):
    from coa_data_layer import handler as handler_module

pytestmark = pytest.mark.unit

_OPENAPI = (
    Path(__file__).resolve().parents[4] / "smithy-generated" / "openapi" / "openapi" / "DataLayerService.openapi.json"
)

# Operations checked. Every DataLayerService operation is; a new one must be added.
#
# "Read" means the handler validates and forwards the member. GraphTraverse's
# ``direction`` and ``relationshipFilter`` are read (and so pass here) but not yet
# applied by the Context Manager; the model's member docs say so, rather than this
# list hiding them.
_CHECKED_OPERATIONS = ("Query", "TranslateSPARQL", "KBSearch", "GraphTraverse", "DescribeSchema")
_SKIPPED_OPERATIONS: set[str] = set()

# Known gaps: (operationId, member) -> why it is acceptable for now. Keep empty
# unless a gap is deliberate and tracked; every entry is a member the contract
# promises the handler and the handler never reads.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}

# The variable names the handlers bind the parsed request body / query string to.
_BODY_NAMES = {"body"}
_QUERY_NAMES = {"query"}


def _spec() -> dict:
    if not _OPENAPI.is_file():
        pytest.skip(
            f"{_OPENAPI} not found — it is generated, not committed. Run `make generate` "
            "to produce it (CI generates it before the unit-test jobs)."
        )
    return json.loads(_OPENAPI.read_text())


def _operations(spec: dict) -> dict[str, tuple[str, str, dict]]:
    """``operationId -> (METHOD, path, operation)`` for every operation in the spec."""
    ops: dict[str, tuple[str, str, dict]] = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op:
                ops[op["operationId"]] = (method.upper(), path, op)
    return ops


def _declared_members(spec: dict, op: dict) -> tuple[set[str], set[str]]:
    """``(body_members, query_members)`` the operation declares."""
    query = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}
    body: set[str] = set()
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    body.update(schema.get("properties", {}))
    return body, query


def _table_keys(node: ast.expr | None) -> set[str]:
    """String-literal keys of a dict literal, or string items of a tuple/list literal."""
    if isinstance(node, ast.Dict):
        items: list[ast.expr | None] = list(node.keys)
    elif isinstance(node, (ast.Tuple, ast.List)):
        items = list(node.elts)
    else:
        return set()
    return {k.value for k in items if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _module_tables(tree: ast.Module) -> dict[str, set[str]]:
    """Module-level ``NAME = {...}`` / ``NAME: T = (...)`` member tables and their string keys."""
    tables: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        else:
            continue
        if keys := _table_keys(value):
            tables[name] = keys
    return tables


def _reads(func_name: str) -> tuple[set[str], set[str]]:
    """String keys read off ``body`` / ``query`` by ``func_name`` and the helpers it calls."""
    tree = ast.parse(inspect.getsource(handler_module))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    tables = _module_tables(tree)
    body_keys: set[str] = set()
    query_keys: set[str] = set()
    pending, visited = [func_name], set()
    while pending:
        name = pending.pop()
        if name in visited or name not in funcs:
            continue
        visited.add(name)
        dynamic = {"body": False, "query": False}
        referenced_tables: set[str] = set()
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Name) and node.id in tables:
                referenced_tables.add(node.id)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.args
                and not isinstance(node.args[0], ast.Constant)
            ):
                if node.func.value.id in _BODY_NAMES:
                    dynamic["body"] = True
                elif node.func.value.id in _QUERY_NAMES:
                    dynamic["query"] = True
            target, key = None, None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                target, key = node.func.value.id, node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Constant)
            ):
                target, key = node.value.id, node.slice.value
            if isinstance(key, str):
                if target in _BODY_NAMES:
                    body_keys.add(key)
                elif target in _QUERY_NAMES:
                    query_keys.add(key)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pending.append(node.func.id)
        for table in referenced_tables:
            if dynamic["body"]:
                body_keys |= tables[table]
            if dynamic["query"]:
                query_keys |= tables[table]
    return body_keys, query_keys


def _handler_for(method: str, path: str) -> str:
    route = handler_module._ROUTE_MAP.get((method, path))
    assert route is not None, f"{method} {path} is declared in the model but has no data-layer route"
    return route.__name__


@pytest.mark.parametrize("operation", _CHECKED_OPERATIONS)
def test_every_declared_input_member_is_read(operation):
    spec = _spec()
    ops = _operations(spec)
    assert operation in ops, f"{operation} not found in DataLayerService.openapi.json"
    method, path, op = ops[operation]
    declared_body, declared_query = _declared_members(spec, op)
    read_body, read_query = _reads(_handler_for(method, path))

    missing = sorted(
        [f"body.{m}" for m in declared_body - read_body if (operation, m) not in _KNOWN_GAPS]
        + [f"query.{m}" for m in declared_query - read_query if (operation, m) not in _KNOWN_GAPS]
    )
    assert not missing, (
        f"{operation}: members declared in the Smithy model but never read by "
        f"{_handler_for(method, path)} (silently dropped): {missing}"
    )


def test_every_operation_is_checked_or_explicitly_skipped():
    """A new DataLayerService operation must be added to the check, not missed by it."""
    unaccounted = set(_operations(_spec())) - set(_CHECKED_OPERATIONS) - _SKIPPED_OPERATIONS
    assert not unaccounted, f"add these operations to _CHECKED_OPERATIONS: {sorted(unaccounted)}"


def test_known_gaps_are_still_gaps():
    """An allowlist entry that has been fixed must be removed, so the list stays honest."""
    spec = _spec()
    ops = _operations(spec)
    for operation, member in _KNOWN_GAPS:
        method, path, _ = ops[operation]
        read_body, read_query = _reads(_handler_for(method, path))
        assert member not in read_body | read_query, f"{operation}.{member} is now read — drop it from _KNOWN_GAPS"


def test_scanner_detects_reads_and_follows_helpers():
    """Negative control: the scanner sees reads made inside a called helper.

    ``_handle_kb_search`` reads ``topK`` only via ``_parse_kb_search_options``; if
    helper-following broke, the main test would report false gaps — and an
    over-eager scanner would pass vacuously, so assert an absent key stays absent.
    """
    read_body, _ = _reads("_handle_kb_search")
    assert {"query", "topK", "minScore"} <= read_body
    assert "phantomMember" not in read_body


def test_scanner_resolves_member_tables():
    """Negative control for table-driven reads: ``_handle_query`` reads ``tierOverride``
    only through ``_QUERY_INT_MEMBERS``, and that must be seen; a table a function
    never references must not count (``_handle_kb_search`` reads no Query member, and
    the error-type table in the main handler is not a request member)."""
    read_body, _ = _reads("_handle_query")
    assert {"tierOverride", "timeoutMs", "maxResults", "mode", "strategy", "includeDebugInfo"} <= read_body
    assert "ValidationError" not in read_body
    kb_body, _ = _reads("_handle_kb_search")
    assert "tierOverride" not in kb_body
