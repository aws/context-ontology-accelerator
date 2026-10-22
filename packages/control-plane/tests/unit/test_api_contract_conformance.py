# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every control-plane input member is read by the handler that serves it, and nothing more.

Guards two drift classes between the Smithy model and the namespace / grants /
roles Lambdas:

* *declared-but-inert*: a member the contract advertises (path label, query
  parameter or body member) that the handler never reads, so the caller gets a
  2xx computed without it;
* *undeclared input*: a query parameter the handler reads that the contract
  does not declare, so generated clients cannot send it and it is untested.

Source of truth is the generated OpenAPI (``smithy-generated/openapi/openapi/
ControlPlaneService.openapi.json``), i.e. what API Gateway and clients are told.
Each operation maps to its handler function below; the function's source — plus
every same-package function it calls, transitively — is AST-scanned for
``<var>.get("x")`` / ``<var>["x"]`` reads, where ``<var>`` is the name the
handlers bind the path parameters, query string or body to. A body or query
string validated with ``<Model>.model_validate(<var>)`` counts as reading every
field alias of that Pydantic model. ``clientToken`` is read by the
``coa_common.idempotency`` wrapper and counts when the handler is wrapped.

The router tests (``test_grants_router.py`` and the namespace handler tests)
check that each route reaches the function listed here.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

pytestmark = pytest.mark.unit

_OPENAPI = (
    Path(__file__).resolve().parents[4]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)

# operationId -> "module:function" serving it.
_HANDLERS: dict[str, str] = {
    "CreateNamespace": "coa_control_plane.namespace.create_handler:handler",
    "GetNamespace": "coa_control_plane.namespace.get_handler:handler",
    "ListNamespaces": "coa_control_plane.namespace.list_handler:handler",
    "UpdateNamespace": "coa_control_plane.namespace.update_handler:handler",
    "DeleteNamespace": "coa_control_plane.namespace.delete_handler:handler",
    "UpdateNamespaceStatus": "coa_control_plane.namespace.update_status_handler:handler",
    "CreateGrant": "coa_control_plane.grants.create_handler:handler",
    "ListNamespaceGrants": "coa_control_plane.grants.list_handler:handler",
    "DeleteGrant": "coa_control_plane.grants.delete_handler:handler",
    "ListPrincipalGrants": "coa_control_plane.grants.list_principal_grants_handler:handler",
    "CreatePlatformGrant": "coa_control_plane.grants.create_platform_grant_handler:handler",
    "ListPlatformGrants": "coa_control_plane.grants.list_platform_grants_handler:handler",
    "DeletePlatformGrant": "coa_control_plane.grants.delete_platform_grant_handler:handler",
    "ListPlatformRoles": "coa_control_plane.roles.list_platform_roles_handler:handler",
    "ListNamespaceRoles": "coa_control_plane.roles.namespace_roles_handler:handler",
    "GetNamespaceRole": "coa_control_plane.roles.namespace_roles_handler:handler",
}

# Paths served by the namespace, grants and roles Lambdas (exact, or the
# prefix of a sub-resource): an operation on one of these that is missing from
# _HANDLERS fails the test.
_OWNED_EXACT = ("/namespaces", "/namespaces/{namespaceId}", "/namespaces/{namespaceId}/status", "/roles")
_OWNED_PREFIXES = ("/namespaces/{namespaceId}/grants", "/namespaces/{namespaceId}/roles", "/grants", "/principals/")

# Names the handlers bind each input location to.
_PATH_NAMES = {"path_params"}
_QUERY_NAMES = {"query_params", "raw_params"}
_BODY_NAMES = {"body"}

# Declared members a handler deliberately does not read: (operationId, member) -> why.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}
# Undeclared query parameters a handler still reads: (operationId, param) -> why.
_LEGACY_QUERY_ALIASES: dict[tuple[str, str], str] = {}


def _spec() -> dict[str, Any]:
    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — it is generated, not committed. Run `make generate` to produce it.")
    return json.loads(_OPENAPI.read_text())


def _operations(spec: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    ops: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op and method != "options":
                ops[op["operationId"]] = (method.upper(), path, op)
    return ops


def _declared(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, set[str]]:
    params = op.get("parameters", [])
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return {
        "path": {p["name"] for p in params if p.get("in") == "path"},
        "query": {p["name"] for p in params if p.get("in") == "query"},
        "body": set(schema.get("properties", {})),
    }


def _resolve(target: str) -> tuple[Any, Callable[..., Any]]:
    module_name, func_name = target.split(":")
    module = importlib.import_module(module_name)
    return module, getattr(module, func_name)


def _model_aliases(module: Any, name: str) -> set[str]:
    model = getattr(module, name, None)
    fields = getattr(model, "model_fields", None)
    if not fields:
        return set()
    return {f.alias or n for n, f in fields.items()}


def _reads(target: str) -> tuple[dict[str, set[str]], bool]:
    """Keys read per input location by ``target`` and the same-package functions it calls."""
    module, func = _resolve(target)
    wrapped = hasattr(func, "__wrapped__") or "idempotent_create" in inspect.getsource(module)
    reads: dict[str, set[str]] = {"path": set(), "query": set(), "body": set()}
    pending: list[tuple[Any, Callable[..., Any]]] = [(module, inspect.unwrap(func))]
    visited: set[Any] = set()
    while pending:
        mod, fn = pending.pop()
        if fn in visited:
            continue
        visited.add(fn)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            var, key = None, None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.args
            ):
                if node.func.attr == "get" and isinstance(node.args[0], ast.Constant):
                    var, key = node.func.value.id, node.args[0].value
                elif node.func.attr == "model_validate" and isinstance(node.args[0], ast.Name):
                    for location, names in (("query", _QUERY_NAMES), ("body", _BODY_NAMES)):
                        if node.args[0].id in names:
                            reads[location] |= _model_aliases(mod, node.func.value.id)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Constant)
            ):
                var, key = node.value.id, node.slice.value
            if isinstance(key, str):
                for location, names in (("path", _PATH_NAMES), ("query", _QUERY_NAMES), ("body", _BODY_NAMES)):
                    if var in names:
                        reads[location].add(key)
            if isinstance(node, ast.Call):
                callee = None
                if isinstance(node.func, ast.Name):
                    callee = getattr(mod, node.func.id, None)
                if inspect.isfunction(callee) and callee.__module__.startswith("coa_control_plane"):
                    pending.append((importlib.import_module(callee.__module__), callee))
    return reads, wrapped


def test_every_owned_operation_is_mapped() -> None:
    ops = _operations(_spec())
    owned = {op_id for op_id, (_, path, _) in ops.items() if path in _OWNED_EXACT or path.startswith(_OWNED_PREFIXES)}
    assert owned == set(_HANDLERS), f"unmapped: {owned - set(_HANDLERS)}, stale: {set(_HANDLERS) - owned}"


@pytest.mark.parametrize("operation", sorted(_HANDLERS))
def test_declared_members_are_read(operation: str) -> None:
    spec = _spec()
    _, _, op = _operations(spec)[operation]
    declared = _declared(spec, op)
    reads, wrapped = _reads(_HANDLERS[operation])
    if wrapped:
        reads["body"].add("clientToken")
    missing = {
        (location, member)
        for location, members in declared.items()
        for member in members - reads[location]
        if (operation, member) not in _KNOWN_GAPS
    }
    assert not missing, f"{operation} never reads declared members {sorted(missing)}"


@pytest.mark.parametrize("operation", sorted(_HANDLERS))
def test_no_undeclared_query_parameters(operation: str) -> None:
    spec = _spec()
    _, _, op = _operations(spec)[operation]
    declared = _declared(spec, op)["query"]
    reads, _ = _reads(_HANDLERS[operation])
    extra = {q for q in reads["query"] - declared if (operation, q) not in _LEGACY_QUERY_ALIASES}
    assert not extra, f"{operation} reads undeclared query parameters {sorted(extra)}"


def test_scanner_sees_a_known_read() -> None:
    reads, wrapped = _reads(_HANDLERS["CreateGrant"])
    assert {"principalType", "tableAllowlist", "columnDenylist"} <= reads["body"]
    assert "namespaceId" in reads["path"]
    assert wrapped
