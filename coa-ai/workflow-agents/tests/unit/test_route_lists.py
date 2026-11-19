# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep the integ route-drift lists in step with the REST client.

``tests/routes.py`` lists every route ``CoaApiClient`` calls, and the
integ suite probes each one against the deployed API. Those lists are written
by hand, so this test parses ``coa_api_client.py`` and fails when a client
route is missing from them, or when a listed route no longer matches any
client call. It runs without a deployment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from coa_workflow_agents.clients import coa_api_client

from tests.routes import MUTATING_ROUTES, READ_ONLY_ROUTES

pytestmark = pytest.mark.unit

# Client methods that send a request, and the HTTP method each one uses.
_SENDERS = {
    "get": "GET",
    "post": "POST",
    "post_idempotent": "POST",
    "_post_maybe_idempotent": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}
# The generic senders themselves: their ``path`` argument is a parameter, not a route.
_GENERIC = {*_SENDERS, "_request"}
_PLACEHOLDER = "{}"


def _template(node: ast.expr) -> str:
    """The path template of an f-string or literal path, query part dropped.

    A ``{_seg(x)}`` or any other interpolated segment becomes ``{}``; an
    interpolation that only appends a query string (a trailing ``{q}`` or a
    ``{_query_string(...)}``) is dropped.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split("?")[0]
    if not isinstance(node, ast.JoinedStr):
        raise AssertionError(f"line {node.lineno}: client path is not a literal or f-string: {ast.unparse(node)}")
    parts: list[str] = []
    for i, value in enumerate(node.values):
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
            continue
        assert isinstance(value, ast.FormattedValue)
        inner = value.value
        is_query_builder = isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_query_string"
        is_seg = isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_seg"
        if is_query_builder or (not is_seg and i == len(node.values) - 1):
            continue
        parts.append(_PLACEHOLDER)
    return "".join(parts).split("?")[0]


def _client_routes() -> set[tuple[str, str]]:
    """Every ``(method, template)`` a ``CoaApiClient`` method sends."""
    tree = ast.parse(Path(coa_api_client.__file__).read_text())
    client = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CoaApiClient")
    routes: set[tuple[str, str]] = set()
    for method in client.body:
        if not isinstance(method, ast.FunctionDef) or method.name in _GENERIC:
            continue
        for call in ast.walk(method):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if not (isinstance(call.func.value, ast.Name) and call.func.value.id == "self"):
                continue
            name = call.func.attr
            if name == "_request":
                verb = call.args[0]
                assert isinstance(verb, ast.Constant), f"{method.name}: _request method is not a literal"
                routes.add((str(verb.value), _template(call.args[1])))
            elif name in _SENDERS:
                routes.add((_SENDERS[name], _template(call.args[0])))
    return routes


def _pattern(template: str) -> re.Pattern[str]:
    """A regex matching a listed route against a client template (``{}`` = one segment)."""
    return re.compile("".join("[^/]+" if p == _PLACEHOLDER else re.escape(p) for p in re.split(r"(\{\})", template)))


def _listed(routes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Listed routes with query strings dropped and placeholders filled with a sample value."""
    return [(m, re.sub(r"\{[^}]+\}", "x", t.split("?")[0])) for m, t in routes]


def test_client_routes_are_found() -> None:
    """The parser sees the client's routes (a guard against a parser that finds nothing)."""
    routes = _client_routes()
    assert ("GET", "/health") in routes
    assert ("POST", "/namespaces/{}/bulk-delete-metrics") in routes
    assert len(routes) > 60, sorted(routes)


def test_route_lists_have_no_duplicates() -> None:
    every = READ_ONLY_ROUTES + MUTATING_ROUTES
    assert len(every) == len(set(every)), [r for r in every if every.count(r) > 1]


def test_read_only_list_is_get_and_mutating_list_is_not() -> None:
    assert {m for m, _ in READ_ONLY_ROUTES} == {"GET"}
    assert "GET" not in {m for m, _ in MUTATING_ROUTES}


def test_every_client_route_is_listed() -> None:
    """A new client call must be added to the integ route lists."""
    listed = _listed(READ_ONLY_ROUTES + MUTATING_ROUTES)
    missing = sorted(
        (m, t) for m, t in _client_routes() if not any(lm == m and _pattern(t).fullmatch(lp) for lm, lp in listed)
    )
    assert not missing, f"client routes missing from tests/routes.py: {missing}"


def test_every_listed_route_is_a_client_route() -> None:
    """A route the client no longer calls must be dropped from the lists."""
    client = _client_routes()
    stale = sorted(
        (m, lp)
        for m, lp in _listed(READ_ONLY_ROUTES + MUTATING_ROUTES)
        if not any(cm == m and _pattern(t).fullmatch(lp) for cm, t in client)
    )
    assert not stale, f"listed routes the client does not call: {stale}"
