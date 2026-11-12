# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fail an image build when the code it runs imports a module the image cannot import.

Usage: python image_import_check.py ENTRYPOINT_MODULE... [--optional MODULE]...

Reads (never runs) the first-party coa_* modules reachable from the entrypoints, including imports
inside functions, and imports every third-party module they import. That catches a package the
image never installed and a dependency broken by its own undeclared import. Imports under
``if TYPE_CHECKING:`` or in a ``try`` that handles ImportError are optional by design and skipped.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import sys
from importlib.machinery import PathFinder
from pathlib import Path

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _spec(module: str):
    """Find a module without importing it (find_spec on a dotted name would run its parents)."""
    parts, spec = module.split("."), None
    for i in range(len(parts)):
        path = sys.path if i == 0 else spec and spec.submodule_search_locations
        try:
            spec = PathFinder.find_spec(".".join(parts[: i + 1]), list(path)) if path else None
        except KeyError:  # a namespace package nested in another looks its parent up in sys.modules
            return None
    return spec


def _imports(node: ast.AST):
    """Yield the import statements under ``node`` that are not optional by design."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node
        return
    children = list(ast.iter_child_nodes(node))
    if isinstance(node, ast.If) and ast.unparse(node.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING"):
        children = node.orelse
    elif isinstance(node, ast.Try) and any(
        isinstance(n, ast.Name) and n.id in _IMPORT_ERRORS for h in node.handlers if h.type for n in ast.walk(h.type)
    ):
        children = [*node.handlers, *node.orelse, *node.finalbody]
    for child in children:
        yield from _imports(child)


def third_party_imports(entrypoints: list[str]) -> dict[str, str]:
    """Map each third-party module imported from the entrypoints' closure to a file that imports it."""
    found: dict[str, str] = {}
    queue = [".".join(e.split(".")[: i + 1]) for e in entrypoints for i in range(e.count(".") + 1)]
    seen: set[str] = set()
    while queue:
        module = queue.pop()
        spec = _spec(module)
        if module in seen or spec is None or not (spec.origin or "").endswith(".py"):
            continue
        seen.add(module)
        package = module if spec.submodule_search_locations is not None else module.rpartition(".")[0]
        for stmt in _imports(ast.parse(Path(spec.origin).read_text(encoding="utf-8"))):
            if isinstance(stmt, ast.Import):
                targets = [alias.name for alias in stmt.names]
            else:
                base = package.split(".")[: len(package.split(".")) - stmt.level + 1] if stmt.level else []
                parent = ".".join([*base, stmt.module] if stmt.module else base)
                targets = [parent, *(f"{parent}.{a.name}" for a in stmt.names if _spec(f"{parent}.{a.name}"))]
            for target in targets:
                parts = target.split(".")
                if parts[0].startswith("coa_") and _spec(parts[0]):
                    queue += [".".join(parts[: i + 1]) for i in range(len(parts))]
                elif parts[0].startswith("coa_") or parts[0] not in sys.stdlib_module_names:
                    found.setdefault(target, spec.origin)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("entrypoints", nargs="+")
    parser.add_argument("--optional", action="append", default=[], help="module allowed to be missing")
    args = parser.parse_args()
    missing = [e for e in args.entrypoints if _spec(e) is None]
    if missing:
        parser.error(f"entrypoint not found: {', '.join(missing)}")
    found = third_party_imports(args.entrypoints)
    failures = []
    for module, where in sorted(found.items()):
        if module.split(".")[0] in args.optional:
            continue
        try:
            importlib.import_module(module)
        except BaseException as exc:  # noqa: BLE001 - any failure at import time is a finding
            failures.append(f"  {module} (imported by {where}): {type(exc).__name__}: {exc}")
    print(f"import check: {len(found)} third-party imports, {len(failures)} failed", *failures, sep="\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
