# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ontology-engine routes and the Smithy model agree on query parameters and list envelopes.

The ontology-api-proxy Lambda forwards each request's query string to the
ontology engine verbatim, renaming only the per-path ``_QUERY_ALIASES``. FastAPI
ignores a query parameter its route does not declare and falls back to the
default, so a contract parameter whose name the route does not bind is silently
dropped: the caller gets a 200 computed without it. ``ListProposals`` was the
live instance (``maxResults`` / ``nextToken`` / ``ontologyId`` unbound — the route
took ``limit`` / ``ontology_id`` and had no pagination), and ``ListInductionJobs``
ignored ``nextToken``.

How it works, using the proxy's own tables (imported, never copied, so a proxy
change is what gets tested):

1. Every operation in the generated ``ControlPlaneService.openapi.json`` whose
   path is in ``api_proxy_handler._PATH_MAP`` is routed to the ontology engine.
2. The backend path is resolved against ``coa_ontology.main.app`` the way
   Starlette does — first route whose method and path regex match.
3. Each declared query parameter is renamed through ``_QUERY_ALIASES`` and must
   be among the route's bound query parameters (by alias).

Known gaps go in ``_KNOWN_GAPS`` with a reason; the list is checked to stay
truthful (an entry that has been fixed fails until it is removed).

The reverse direction is checked too: a query parameter a route binds but the
model does not declare is an undocumented input — the old ListOntologies
``skip`` was one. Deliberately unmodelled legacy aliases are listed in
``_LEGACY_QUERY_PARAMS`` with a reason.

Finally, every engine-routed ``List*`` operation is called through the app (on an
in-memory table) and its response's top-level keys must be the Smithy output
member names: a bare array, or an envelope key the generated client does not
know, is the drift that made ``out.ontologies`` silently ``undefined`` before.
"""

from __future__ import annotations

import functools
import importlib
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from .test_dynamo_store import _FakeDynamoTable

pytestmark = pytest.mark.unit

_OPENAPI = (
    Path(__file__).resolve().parents[4]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)

# (operationId, query parameter) -> reason the gap is tolerated. Keep this empty
# unless a gap is deliberate; every entry is a parameter the contract promises and
# the service silently ignores. ListProposals and ListInductionJobs must never
# appear here: their pagination is the defect this test was written for.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}

# (operationId, route query parameter) -> why the route binds a parameter the
# model does not declare. Only pre-contract spellings kept so existing callers
# keep working; anything else belongs in the model.
_LEGACY_QUERY_PARAMS: dict[tuple[str, str], str] = {
    ("ListOntologies", "skip"): "pre-nextToken offset; documented on the operation, superseded by nextToken",
    ("ListProposals", "limit"): "pre-contract spelling of maxResults",
    ("ListProposals", "ontology_id"): "pre-contract spelling of ontologyId",
}

# Added to every forwarded request by the proxy itself, never sent by a caller.
_PROXY_QUERY_PARAMS = {"namespace"}

_ENGINE_METHODS = {"get", "put", "post", "patch", "delete"}

# Placeholder substituted for ``{param}`` path segments when matching routes.
_SAMPLE_SEGMENT = "sample-id"


def _spec() -> dict:
    if not _OPENAPI.is_file():
        pytest.skip(
            f"{_OPENAPI} not found — it is generated, not committed. Run `make generate` "
            "to produce it (CI generates it before the unit-test jobs)."
        )
    return json.loads(_OPENAPI.read_text())


@functools.cache
def _proxy():
    """The proxy module, for its ``_PATH_MAP`` / ``_QUERY_ALIASES`` tables.

    It reads ``ONTOLOGY_ENGINE_ENDPOINT`` at import time. When this test is the
    first importer, the module is imported under a placeholder endpoint and then
    unregistered, so the proxy's own tests later import it fresh under theirs
    instead of inheriting this placeholder.
    """
    import coa_ontology

    name = "coa_ontology.api_proxy_handler"
    if name in sys.modules:
        return sys.modules[name]
    with patch.dict("os.environ", {"ONTOLOGY_ENGINE_ENDPOINT": "http://ontology-engine.test:8001"}):
        module = importlib.import_module(name)
    sys.modules.pop(name, None)
    if getattr(coa_ontology, "api_proxy_handler", None) is module:
        delattr(coa_ontology, "api_proxy_handler")
    return module


def _routes():
    from coa_ontology.main import app
    from fastapi.routing import APIRoute

    return [r for r in app.routes if isinstance(r, APIRoute)]


def _resolve(routes, method: str, concrete_path: str):
    """First route matching ``method`` + ``concrete_path``, as Starlette routes it."""
    for route in routes:
        if method in route.methods and route.path_regex.match(concrete_path):
            return route
    return None


def _bound_query_params(route) -> set[str]:
    from fastapi.dependencies.utils import get_flat_dependant

    return {param.alias for param in get_flat_dependant(route.dependant).query_params}


def _engine_operations() -> list[tuple[str, str, str, list[str]]]:
    """``(operationId, METHOD, smithy_path, query_params)`` for every engine-routed operation."""
    path_map = _proxy()._PATH_MAP
    ops = []
    for path, item in _spec()["paths"].items():
        if path not in path_map:
            continue
        for method, op in item.items():
            # OPTIONS operations are the API Gateway CORS preflight mocks the
            # OpenAPI conversion adds; they never reach the ontology engine.
            if method in _ENGINE_METHODS and isinstance(op, dict) and "operationId" in op:
                params = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
                ops.append((op["operationId"], method.upper(), path, params))
    return ops


def _unbound(method: str, smithy_path: str, params: list[str]) -> tuple[object, list[str]]:
    """``(route, [declared params the route does not bind])`` for one operation."""
    proxy = _proxy()
    # The proxy substitutes path parameters and THEN looks the result up in
    # ``_QUERY_ALIASES`` — mirror that exactly.
    backend = re.sub(r"\{[^}]+\}", _SAMPLE_SEGMENT, proxy._PATH_MAP[smithy_path])
    route = _resolve(_routes(), method, backend)
    if route is None:
        return None, params
    aliases = proxy._QUERY_ALIASES.get(backend, {})
    bound = _bound_query_params(route)
    return route, [p for p in params if aliases.get(p, p) not in bound]


def _ids(ops):
    return [op[0] for op in ops]


def test_engine_routed_operations_are_found():
    """Sanity: the path map selects the operations we know are engine-backed."""
    ids = set(_ids(_engine_operations()))
    expected = {"ListProposals", "ListInductionJobs", "StartInduction"}
    assert expected <= ids, f"path-map selection regressed: {sorted(ids)}"


@pytest.mark.parametrize("operation", _engine_operations() if _OPENAPI.is_file() else [], ids=lambda op: op[0])
def test_every_smithy_query_parameter_is_bound_by_the_route(operation):
    operation_id, method, smithy_path, params = operation
    route, unbound = _unbound(method, smithy_path, params)
    assert route is not None, (
        f"{operation_id}: {method} {smithy_path} maps to {_proxy()._PATH_MAP[smithy_path]!r}, "
        "which no ontology-engine route serves"
    )
    gaps = [p for p in unbound if (operation_id, p) not in _KNOWN_GAPS]
    assert not gaps, (
        f"{operation_id}: query parameters declared in the Smithy model but not bound by "
        f"{route.path} (after _QUERY_ALIASES renaming) — FastAPI drops them silently: {gaps}. "
        "Bind them on the route (Query(alias=...)) or add a _QUERY_ALIASES entry."
    )


def test_pagination_operations_are_not_allowlisted():
    """The two operations this guard was written for must conform outright."""
    assert not [k for k in _KNOWN_GAPS if k[0] in {"ListProposals", "ListInductionJobs"}]


def test_known_gaps_are_still_gaps():
    """A fixed gap must be removed from the allowlist, so it stays truthful."""
    by_id = {op[0]: op for op in _engine_operations()}
    for operation_id, param in _KNOWN_GAPS:
        _, method, smithy_path, params = by_id[operation_id]
        _, unbound = _unbound(method, smithy_path, params)
        assert param in unbound, f"{operation_id}.{param} is now bound — drop it from _KNOWN_GAPS"


def test_guard_detects_an_unbound_parameter():
    """Negative control: an invented contract parameter must be reported as unbound."""
    _spec()
    _, unbound = _unbound("GET", "/namespaces/{namespaceId}/proposals", ["maxResults", "phantomParam"])
    assert unbound == ["phantomParam"]


# ── Route-bound parameters the model does not declare ────────────────────────


def _undeclared(method: str, smithy_path: str, params: list[str]) -> list[str]:
    """Query parameters the route binds that the operation does not declare (after renaming)."""
    proxy = _proxy()
    backend = re.sub(r"\{[^}]+\}", _SAMPLE_SEGMENT, proxy._PATH_MAP[smithy_path])
    route = _resolve(_routes(), method, backend)
    if route is None:
        return []
    aliases = proxy._QUERY_ALIASES.get(backend, {})
    declared = {aliases.get(p, p) for p in params}
    return sorted(_bound_query_params(route) - declared - _PROXY_QUERY_PARAMS)


@pytest.mark.parametrize("operation", _engine_operations() if _OPENAPI.is_file() else [], ids=lambda op: op[0])
def test_every_route_query_parameter_is_declared_in_the_model(operation):
    operation_id, method, smithy_path, params = operation
    extra = [p for p in _undeclared(method, smithy_path, params) if (operation_id, p) not in _LEGACY_QUERY_PARAMS]
    assert not extra, (
        f"{operation_id}: the route binds query parameters the Smithy model does not declare: {extra}. "
        "Model them, or — for a legacy alias kept for existing callers — add them to _LEGACY_QUERY_PARAMS."
    )


def test_legacy_query_params_are_still_bound_and_undeclared():
    """An allowlisted alias that was modelled (or dropped) must leave the allowlist."""
    by_id = {op[0]: op for op in _engine_operations()}
    for operation_id, param in _LEGACY_QUERY_PARAMS:
        _, method, smithy_path, params = by_id[operation_id]
        assert param in _undeclared(method, smithy_path, params), (
            f"{operation_id}.{param} is no longer an undeclared bound parameter — drop it from _LEGACY_QUERY_PARAMS"
        )


def test_guard_detects_an_undeclared_route_parameter():
    """Negative control: dropping a declared parameter from the contract must surface it."""
    (op,) = [op for op in _engine_operations() if op[0] == "ListOntologies"]
    _, method, smithy_path, params = op
    assert "uri" in _undeclared(method, smithy_path, [p for p in params if p != "uri"])


# ── List-operation response envelopes ────────────────────────────────────────


def _list_operations() -> list[tuple[str, str, str]]:
    """``(operationId, smithy_path, response schema name)`` for every engine-routed GET List* operation."""
    ops = []
    for path, item in _spec()["paths"].items():
        if path not in _proxy()._PATH_MAP:
            continue
        op = item.get("get")
        if not isinstance(op, dict) or not op.get("operationId", "").startswith("List"):
            continue
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        ops.append((op["operationId"], path, ref.rsplit("/", 1)[-1]))
    return ops


@pytest.fixture()
def empty_table(monkeypatch):
    from coa_ontology import dynamo_store

    table = _FakeDynamoTable()
    monkeypatch.setattr(dynamo_store, "_get_table", lambda: table)
    return table


def test_list_operations_are_found():
    ids = {op[0] for op in _list_operations()}
    assert {"ListProposals", "ListInductionJobs", "ListOntologies", "ListInducedDatasources"} <= ids, sorted(ids)


@pytest.mark.parametrize("operation", _list_operations() if _OPENAPI.is_file() else [], ids=lambda op: op[0])
def test_list_response_envelope_matches_the_model(operation, empty_table):
    from coa_ontology.main import app

    operation_id, smithy_path, schema_name = operation
    schema = _spec()["components"]["schemas"][schema_name]
    backend = re.sub(r"\{[^}]+\}", _SAMPLE_SEGMENT, _proxy()._PATH_MAP[smithy_path])
    resp = TestClient(app).get(backend, params={"namespace": "ns"})

    assert resp.status_code == 200, f"{operation_id}: {resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert isinstance(body, dict), f"{operation_id} returns a bare {type(body).__name__}, not the modelled envelope"
    assert set(body) <= set(schema["properties"]), (
        f"{operation_id}: envelope keys {sorted(body)} are not output members {sorted(schema['properties'])}"
    )
    assert set(schema.get("required", [])) <= set(body), f"{operation_id}: missing required members"
