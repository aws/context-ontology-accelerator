# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every Smithy input member of a Sources API operation is read by the handler that serves it.

Guards two drift classes between ``unified-sources.smithy`` and the Sources API
Lambda:

* **Declared but inert** — a member the model advertises that the handler never
  reads is silently dropped; the caller gets a 2xx computed without it.
* **Undeclared input** — a query parameter the handler reads that the model
  does not declare is invisible to generated clients and to the published API.

Source of truth is the generated OpenAPI (``smithy-generated/openapi/openapi/
ControlPlaneService.openapi.json``). Each operation is mapped to its handler by
walking ``sources_handler._route``'s ``if resource == ... and http_method == ...``
chain; the handler — plus every module-level helper it calls, transitively, in
the three route modules — is AST-scanned for reads:

* ``body.get("x")`` / ``body["x"]`` / ``raw.get("x")`` — request body;
* ``qs.get("x")`` / ``qs["x"]`` — query string;
* a body parsed whole with ``<RequestModel>.model_validate(...)`` counts a
  member as read when its snake_case attribute is accessed;
* ``clientToken`` is read when the route goes through ``idempotent_create``;
* path labels are read by ``_route`` through ``path_params.get("x")``.

Reading a member is necessary, not sufficient: ``test_contract_bounds.py``
checks bounds, and the handler tests check behaviour.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from pathlib import Path

import pytest

# The route modules read their configuration at import; match the other API test modules.
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")

import coa_sources.api.sources_handler as sources_handler  # noqa: E402, I001  (imports the route modules)
from coa_sources.api import database_routes, document_routes  # noqa: E402

pytestmark = pytest.mark.unit

_OPENAPI = (
    Path(__file__).resolve().parents[5]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)
_BASE = "/namespaces/{namespaceId}/sources"

_BODY_NAMES = {"body", "raw"}
_QUERY_NAMES = {"qs"}

# (operationId, member) -> why it is acceptable that the handler does not read it.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}

# (operationId, query parameter) the handler reads without the model declaring
# it, with the reason it is kept.
_UNDECLARED_QUERY_ALLOWLIST: dict[tuple[str, str], str] = {}


# ── OpenAPI ──────────────────────────────────────────────────────────────────


def _spec() -> dict:
    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — it is generated, not committed. Run `make generate`.")
    return json.loads(_OPENAPI.read_text())


def _operations(spec: dict) -> dict[str, tuple[str, str, dict]]:
    """``operationId -> (METHOD, path, operation)`` for every Sources API operation."""
    ops: dict[str, tuple[str, str, dict]] = {}
    for path, item in spec["paths"].items():
        if not path.startswith(_BASE):
            continue
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op and method != "options":
                ops[op["operationId"]] = (method.upper(), path, op)
    return ops


def _declared(spec: dict, op: dict) -> tuple[set[str], set[str], set[str], str | None]:
    """``(body_members, query_members, path_labels, body_schema_name)``."""
    params = op.get("parameters", [])
    query = {p["name"] for p in params if p.get("in") == "query"}
    labels = {p["name"] for p in params if p.get("in") == "path"}
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    name = None
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return set(schema.get("properties", {})), query, labels, name


# ── Handler scanning ─────────────────────────────────────────────────────────


def _functions() -> dict[str, ast.FunctionDef]:
    funcs: dict[str, ast.FunctionDef] = {}
    for module in (database_routes, document_routes, sources_handler):
        for node in ast.parse(inspect.getsource(module)).body:
            if isinstance(node, ast.FunctionDef):
                funcs.setdefault(node.name, node)
    return funcs


def _string_collections() -> dict[str, set[str]]:
    """Module-level tuples/lists/sets of string constants, e.g. ``_CONFIG_MEMBERS``."""
    found: dict[str, set[str]] = {}
    for module in (database_routes, document_routes, sources_handler):
        for node in ast.parse(inspect.getsource(module)).body:
            target = node.target if isinstance(node, ast.AnnAssign) else None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            value = getattr(node, "value", None)
            if isinstance(target, ast.Name) and isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                items = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if items and len(items) == len(value.elts):
                    found[target.id] = set(items)
    return found


def _compare_constant(test: ast.expr, name: str) -> str | None:
    for node in ast.walk(test):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == name
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
        ):
            return node.comparators[0].value
    return None


def _routes() -> dict[tuple[str, str], tuple[str, bool]]:
    """``(METHOD, resource) -> (handler function name, wrapped in idempotent_create)`` from ``_route``."""
    route_fn = _functions()["_route"]
    routes: dict[tuple[str, str], tuple[str, bool]] = {}

    def visit(stmts: list[ast.stmt], resource: str | None, method: str | None) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                visit(
                    stmt.body,
                    _compare_constant(stmt.test, "resource") or resource,
                    _compare_constant(stmt.test, "http_method") or method,
                )
                visit(stmt.orelse, resource, method)
            elif isinstance(stmt, ast.Return) and resource and method and stmt.value is not None:
                calls = [n for n in ast.walk(stmt.value) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
                handlers = [c.func.id for c in calls if c.func.id.startswith("_handle_")]  # type: ignore[union-attr]
                if handlers:
                    wrapped = any(c.func.id == "idempotent_create" for c in calls)  # type: ignore[union-attr]
                    routes.setdefault((method, resource), (handlers[0], wrapped))

    visit(route_fn.body, None, None)
    return routes


def _route_labels() -> set[str]:
    """Path labels ``_route`` reads off ``path_params``."""
    keys: set[str] = set()
    for node in ast.walk(_functions()["_route"]):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "path_params"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "path_params"
            and isinstance(node.slice, ast.Constant)
        ):
            keys.add(node.slice.value)
    return keys


def _reads(func_name: str) -> tuple[set[str], set[str], set[str], set[str]]:
    """``(body_keys, query_keys, attributes, validated_models)`` read by ``func_name`` and its helpers."""
    funcs = _functions()
    collections = _string_collections()
    body_keys: set[str] = set()
    query_keys: set[str] = set()
    attrs: set[str] = set()
    models: set[str] = set()
    pending, visited = [func_name], set()
    while pending:
        name = pending.pop()
        if name in visited or name not in funcs:
            continue
        visited.add(name)
        # ``for k in _CONFIG_MEMBERS: body.get(k)`` reads every name in the collection.
        dynamic_body_read = False
        referenced: set[str] = set()
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Name) and node.id in collections:
                referenced.add(node.id)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in _BODY_NAMES
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                dynamic_body_read = True
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
            if isinstance(node, ast.Attribute):
                attrs.add(node.attr)
                if node.attr == "model_validate" and isinstance(node.value, ast.Name):
                    models.add(node.value.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pending.append(node.func.id)
        if dynamic_body_read:
            for ref in referenced:
                body_keys |= collections[ref]
    return body_keys, query_keys, attrs, models


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _check(spec: dict, operation: str) -> tuple[list[str], list[str]]:
    """``(declared-but-unread members, undeclared query reads)`` for ``operation``."""
    method, path, op = _operations(spec)[operation]
    routes = _routes()
    assert (method, path) in routes, f"{method} {path} ({operation}) is declared in the model but not routed"
    handler, wrapped = routes[(method, path)]
    body, query, labels, schema_name = _declared(spec, op)
    read_body, read_query, attrs, models = _reads(handler)
    if wrapped:
        read_body.add("clientToken")
    if schema_name in models:
        read_body |= {m for m in body if _snake(m) in attrs}
    missing = sorted(
        [f"body.{m}" for m in body - read_body if (operation, m) not in _KNOWN_GAPS]
        + [f"query.{m}" for m in query - read_query if (operation, m) not in _KNOWN_GAPS]
        + [f"label.{m}" for m in labels - _route_labels() if (operation, m) not in _KNOWN_GAPS]
    )
    undeclared = sorted(q for q in read_query - query if (operation, q) not in _UNDECLARED_QUERY_ALLOWLIST)
    return missing, undeclared


def _operation_ids() -> list[str]:
    return sorted(_operations(_spec())) if _OPENAPI.is_file() else ["<openapi-not-generated>"]


@pytest.mark.parametrize("operation", _operation_ids())
def test_every_declared_input_member_is_read(operation: str) -> None:
    missing, _ = _check(_spec(), operation)
    assert not missing, f"{operation}: declared in the Smithy model but never read by its handler: {missing}"


@pytest.mark.parametrize("operation", _operation_ids())
def test_handler_reads_no_undeclared_query_parameter(operation: str) -> None:
    _, undeclared = _check(_spec(), operation)
    assert not undeclared, f"{operation}: handler reads query parameters the model does not declare: {undeclared}"


def test_every_route_is_a_declared_operation() -> None:
    """The reverse direction: a route the model does not declare is an undocumented API."""
    declared = {(m, p) for m, p, _ in _operations(_spec()).values()}
    undeclared = sorted(set(_routes()) - declared)
    assert not undeclared, f"routes with no Smithy operation: {undeclared}"


def test_known_gaps_are_still_gaps() -> None:
    spec = _spec()
    for operation, member in {**_KNOWN_GAPS, **_UNDECLARED_QUERY_ALLOWLIST}:
        method, path, _ = _operations(spec)[operation]
        handler, _ = _routes()[(method, path)]
        read_body, read_query, _, _ = _reads(handler)
        assert member not in read_body | read_query or (operation, member) in _UNDECLARED_QUERY_ALLOWLIST, (
            f"{operation}.{member} is now read — drop it from _KNOWN_GAPS"
        )


def test_scanner_detects_reads_and_follows_helpers() -> None:
    """Negative control: reads inside called helpers are seen, and absent keys stay absent.

    ``_handle_review_table`` reads ``decision`` only via ``_parse_decision``.
    """
    read_body, _, _, _ = _reads("_handle_review_table")
    assert "decision" in read_body
    assert "phantomMember" not in read_body
    assert _routes()[("POST", _BASE)] == ("_handle_create", True)
