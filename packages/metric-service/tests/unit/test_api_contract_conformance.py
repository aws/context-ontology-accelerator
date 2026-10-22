# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every metric-service operation's Smithy input members are read by the handler that serves it.

Guards two drift classes between ``metric-service.smithy`` and the Lambda:

* **Declared but inert** — a member the model advertises (path label, query
  parameter or body member) that the handler never reads is silently dropped:
  the caller gets a 2xx computed without it.
* **Read but undeclared** — a parameter the handler honours that the model does
  not declare is invisible to generated clients and the published OpenAPI.

Source of truth is the generated ``ControlPlaneService.openapi.json`` (what API
Gateway and clients are told). Each operation declared in
``metric-service.smithy`` is dispatched through the real router
(``metric_api_handler.handler``) with every per-operation handler patched, so
the routing under test is the production routing. The chosen module's
``handler`` — plus every module-level helper it calls, transitively — is
AST-scanned for reads:

* ``body.get("x")`` / ``body["x"]`` — request body;
* ``query_params.get("x")`` / ``multi_value_params.get("x")`` — query string;
* ``.get("x")`` on ``pathParameters`` — path label;
* ``<GeneratedModel>.model_validate(body)`` — every field of the generated
  model counts as read from the body, and each field must then be used as
  ``request.<field>`` somewhere in the scanned code;
* ``@idempotent(...)`` on ``handler`` — ``clientToken`` is consumed by the
  idempotency layer before the handler runs.

Reading a member is necessary, not sufficient: ``test_contract_bounds.py`` checks
the bounds, and the handler tests check behaviour.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api import metric_api_handler

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[4]
_OPENAPI = _REPO / "smithy-generated" / "openapi" / "openapi" / "ControlPlaneService.openapi.json"
_SMITHY = _REPO / "models" / "src" / "main" / "smithy" / "metric-service.smithy"

_BODY_NAMES = {"body"}
_QUERY_NAMES = {"query_params", "multi_value_params"}
_PATH_NAMES = {"path_params"}
_REQUEST_NAMES = {"request"}

# (operationId, member) -> why a declared member is not read. Keep empty unless a
# gap is deliberate; every entry is a member the contract promises and the
# service does not honour.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}

# (operationId, field) -> why a field of the generated model is checked by the
# model's own schema validation only and never used afterwards.
_SCHEMA_ONLY_FIELDS: dict[tuple[str, str], str] = {
    # ValidateMetric persists nothing: these members carry no rule beyond their
    # type / bounds, which model_validate already enforces (out-of-contract
    # values get a 400). They are accepted so a create payload validates as-is.
    ("ValidateMetric", "aiContext"): "schema-only: no semantic check beyond the model's bounds",
    ("ValidateMetric", "defaultTimeGrain"): "schema-only: enum checked by the model",
    ("ValidateMetric", "returnType"): "schema-only: no semantic check beyond the model's bounds",
    ("ValidateMetric", "unit"): "schema-only: no semantic check beyond the model's bounds",
}

# (operationId, "body"|"query"|"path", key) -> why the handler reads a parameter
# the model does not declare (legacy aliases kept for existing callers).
_UNDECLARED_READS: dict[tuple[str, str, str], str] = {}


def _spec() -> dict[str, Any]:
    if not _OPENAPI.is_file():
        pytest.skip(
            f"{_OPENAPI} not found — it is generated, not committed. Run `make generate` "
            "to produce it (CI generates it before the unit-test jobs)."
        )
    return json.loads(_OPENAPI.read_text())


def _metric_operations() -> list[str]:
    return re.findall(r"^operation\s+(\w+)", _SMITHY.read_text(), re.MULTILINE)


def _operations(spec: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    ops: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict) and "operationId" in op:
                ops[op["operationId"]] = (method.upper(), path, op)
    return ops


def _declared(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, set[str]]:
    params = op.get("parameters", [])
    body: set[str] = set()
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    body.update(schema.get("properties", {}))
    return {
        "body": body,
        "query": {p["name"] for p in params if p.get("in") == "query"},
        "path": {p["name"] for p in params if p.get("in") == "path"},
    }


def _routed_module(method: str, path: str) -> str:
    """The module whose ``handler`` the production router dispatches ``method path`` to."""
    targets = {
        name: obj
        for name, obj in vars(metric_api_handler).items()
        if name.endswith("_handler") and callable(obj) and getattr(obj, "__module__", "").startswith("coa_metrics.api")
    }
    labels = {label: "sample" for label in re.findall(r"\{(\w+)\}", path)}
    event = {"httpMethod": method, "resource": path, "pathParameters": labels}
    mocks = {name: MagicMock(return_value={"statusCode": 200}) for name in targets}
    with patch.multiple(metric_api_handler, **mocks):
        metric_api_handler.handler(event, None)
    called = [name for name, mock in mocks.items() if mock.called]
    assert len(called) == 1, f"{method} {path} dispatched to {called or 'nothing'}"
    return targets[called[0]].__module__


class _Reads:
    def __init__(self) -> None:
        self.keys: dict[str, set[str]] = {"body": set(), "query": set(), "path": set()}
        self.request_attrs: set[str] = set()
        self.models: list[str] = []
        self.idempotent = False


def _scan(module_name: str) -> _Reads:
    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    reads = _Reads()
    handler_def = funcs["handler"]
    reads.idempotent = any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "idempotent"
        for d in handler_def.decorator_list
    )
    pending, visited = ["handler"], set()
    while pending:
        name = pending.pop()
        if name in visited or name not in funcs:
            continue
        visited.add(name)
        for node in ast.walk(funcs[name]):
            _record(node, reads)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pending.append(node.func.id)
    return reads


def _record(node: ast.AST, reads: _Reads) -> None:
    target: ast.AST | None = None
    key: object = None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        target, key = node.func.value, node.args[0].value
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        target, key = node.value, node.slice.value
    if target is not None and isinstance(key, str):
        if isinstance(target, ast.Name) and target.id in _BODY_NAMES:
            reads.keys["body"].add(key)
        elif isinstance(target, ast.Name) and target.id in _QUERY_NAMES:
            reads.keys["query"].add(key)
        elif key != "pathParameters" and (
            (isinstance(target, ast.Name) and target.id in _PATH_NAMES) or "'pathParameters'" in ast.dump(target)
        ):
            reads.keys["path"].add(key)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_validate"
        and isinstance(node.func.value, ast.Name)
    ):
        reads.models.append(node.func.value.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in _REQUEST_NAMES:
        reads.request_attrs.add(node.attr)


def _model_fields(module_name: str, class_name: str) -> dict[str, str]:
    """``alias -> python attribute`` for a generated model the module validates the body with."""
    cls = getattr(importlib.import_module(module_name), class_name)
    return {(field.alias or attr): attr for attr, field in cls.model_fields.items()}


def _effective_reads(module_name: str, reads: _Reads) -> dict[str, set[str]]:
    keys = {kind: set(values) for kind, values in reads.keys.items()}
    for class_name in reads.models:
        keys["body"].update(_model_fields(module_name, class_name))
    if reads.idempotent:
        keys["body"].add("clientToken")
    return keys


def _case(operation: str) -> tuple[str, dict[str, set[str]], dict[str, set[str]], _Reads]:
    spec = _spec()
    ops = _operations(spec)
    assert operation in ops, f"{operation} not found in ControlPlaneService.openapi.json"
    method, path, op = ops[operation]
    module_name = _routed_module(method, path)
    reads = _scan(module_name)
    return module_name, _declared(spec, op), _effective_reads(module_name, reads), reads


@pytest.mark.parametrize("operation", _metric_operations())
def test_every_declared_input_member_is_read(operation: str) -> None:
    module_name, declared, read, _ = _case(operation)
    missing = sorted(
        f"{kind}.{member}"
        for kind in ("body", "query", "path")
        for member in declared[kind] - read[kind]
        if (operation, member) not in _KNOWN_GAPS
    )
    assert not missing, (
        f"{operation}: members declared in metric-service.smithy but never read by "
        f"{module_name}.handler (silently dropped): {missing}"
    )


@pytest.mark.parametrize("operation", _metric_operations())
def test_handler_reads_no_undeclared_parameter(operation: str) -> None:
    module_name, declared, read, _ = _case(operation)
    extra = sorted(
        f"{kind}.{key}"
        for kind in ("body", "query", "path")
        for key in read[kind] - declared[kind]
        if (operation, kind, key) not in _UNDECLARED_READS
    )
    assert not extra, (
        f"{operation}: {module_name}.handler reads parameters metric-service.smithy does not "
        f"declare (declare them, or allowlist a legacy alias with a reason): {extra}"
    )


@pytest.mark.parametrize("operation", _metric_operations())
def test_every_validated_model_field_is_used(operation: str) -> None:
    """A field the generated model parses but the handler never touches is dropped too."""
    module_name, _, _, reads = _case(operation)
    for class_name in reads.models:
        unused = sorted(
            alias
            for alias, attr in _model_fields(module_name, class_name).items()
            if attr not in reads.request_attrs
            and (operation, alias) not in _SCHEMA_ONLY_FIELDS
            # Stripped from the body by @idempotent before validation.
            and not (reads.idempotent and alias == "clientToken")
        )
        assert not unused, f"{operation}: {class_name} fields parsed but never read via request.<field>: {unused}"


def test_known_gaps_and_allowlist_are_still_needed() -> None:
    for operation, alias in _SCHEMA_ONLY_FIELDS:
        module_name, _, _, reads = _case(operation)
        attrs = {a: f for c in reads.models for a, f in _model_fields(module_name, c).items()}
        assert alias in attrs and attrs[alias] not in reads.request_attrs, f"drop stale entry {operation}.{alias}"
    for operation, member in _KNOWN_GAPS:
        _, _, read, _ = _case(operation)
        assert all(member not in keys for keys in read.values()), f"{operation}.{member} is now read — drop the gap"
    for operation, kind, key in _UNDECLARED_READS:
        _, declared, read, _ = _case(operation)
        assert key in read[kind] and key not in declared[kind], f"drop stale allowlist entry {operation}.{kind}.{key}"


def test_scanner_detects_reads_and_follows_helpers() -> None:
    """Negative control: reads in called helpers are seen; absent keys stay absent."""
    bulk = _scan("coa_metrics.api.bulk_delete_metrics")
    assert {"names", "filter", "deleteAll"} <= bulk.keys["body"]
    assert "phantomMember" not in bulk.keys["body"]
    create = _scan("coa_metrics.api.create_metric")
    assert create.idempotent and "CreateMetricRequestContent" in create.models
    # ontology_concepts is read inside the _validate_soft / _build_metric_definition helpers.
    assert "ontology_concepts" in create.request_attrs
    assert "namespaceId" in _scan("coa_metrics.api.get_metric").keys["path"]
