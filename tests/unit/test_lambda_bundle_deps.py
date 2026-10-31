# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every third-party module a Lambda handler imports must be in that Lambda's bundle.

`bundlePython` installs exactly what its caller declares in its `requirementsFile`
(a lock generated from the requirements.txt next to it) and nothing else. The uv workspace resolves imports
transitively, so a handler can import something that is absent from the bundle and
still pass every test, lint and type check. The failure appears only at invoke
time, as::

    Runtime.ImportModuleError: Unable to import module 'coa_data_layer.handler':
    No module named 'pydantic'

which API Gateway reports as a 502 on every route the function serves.

This has now happened twice. Both times the import arrived in an unrelated
refactor, and both times the missing distribution was reachable only through a
package ``__init__``:

- The namespace deletion Lambdas: ``namespace/cleanup.py`` gained
  ``from coa_common.opensearch import AossVectorClient``, and every
  deletion_pipeline handler imports cleanup, so four Lambdas broke at once —
  including ``finalize``, the pipeline's must-succeed step. The state machine
  burned its retries and left the namespace in DELETE_FAILED.
- The Data Layer API Lambda: ``handler.py`` gained ``coa_common.constants`` and
  ``coa_common.smithy_shapes``. Neither needs pydantic, but importing ANY
  ``coa_common`` submodule executes ``coa_common/__init__.py``, which eagerly
  imports ``authnz_types`` (pydantic), ``config`` (pydantic-settings) and
  ``namespace_resolver`` (structlog).

Both are now declared, and each stack's own CDK test asserts the declaration is
passed to `bundlePython`. Those assertions are fixed lists, so they pin the current
answer and cannot notice when a new import makes it wrong again — which is exactly
how both outages happened.

This test derives the answer instead. For each bundle it walks module-scope imports
from the handler entry points, follows first-party modules, and asserts every
third-party top-level module it reaches is covered by what that bundle declares.

Two properties are what make it see this defect class at all, and both were absent
from the per-package predecessors of this file:

- **Package ``__init__`` execution is modelled.** Importing ``a.b`` runs
  ``a/__init__.py`` before ``a/b``. Walking only the named module misses whatever
  the package initialiser drags in — the mechanism behind both outages.
- **Relative imports are resolved.** ``coa_common.metadata_store`` uses them, so
  skipping ``level > 0`` left part of the graph invisible.

Only module-scope imports count, because only those run when Lambda imports the
handler and can therefore raise ``ImportModuleError``. An import nested in a
function, class, ``if TYPE_CHECKING`` block or guarded ``try/except ImportError``
degrades one code path instead of killing the function — concretely,
``coa_common.embeddings`` imports ``llama_index`` inside a function for an optional
adapter, and bundling llama-index into every Lambda would be dead weight.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMON = _REPO_ROOT / "libs" / "common" / "src"
_STACKS = _REPO_ROOT / "infra" / "lib" / "stacks" / "services"

# Provided by the AWS Lambda Python runtime, so deliberately never declared.
_RUNTIME_PROVIDED = frozenset({"boto3", "botocore"})


@dataclass(frozen=True)
class Bundle:
    """One `bundlePython` call: its handlers, its sources, and what it declares.

    ``declared_in`` is the stack file, and ``declares`` is the path, relative to the
    repo root, of the requirements.txt the bundle's lock is generated from. Reading
    the real declaration rather than mirroring it here is the point: a copy would
    drift.
    """

    name: str
    entry_points: tuple[str, ...]
    src_roots: dict[str, Path]
    declared_in: Path
    declares: str

    def declared(self) -> set[str]:
        requirements = _REPO_ROOT / self.declares
        assert requirements.is_file(), f"missing {self.declares}"
        names = []
        for raw in requirements.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            for separator in (">=", "==", "<=", "~=", ">", "<", "["):
                if separator in line:
                    line = line.split(separator, 1)[0]
                    break
            names.append(line.strip())
        assert names, f"{self.name}: declaration parsed as empty"
        return {_normalize(name) for name in names}


BUNDLES = (
    Bundle(
        name="data-layer-api",
        entry_points=("coa_data_layer.handler",),
        src_roots={
            "coa_data_layer": _REPO_ROOT / "packages" / "data-layer" / "src",
            "coa_common": _COMMON,
        },
        declared_in=_STACKS / "data-layer-stack.ts",
        declares="packages/data-layer/requirements.txt",
    ),
    Bundle(
        name="namespace-deletion-pipeline",
        entry_points=(
            "coa_control_plane.namespace.deletion_pipeline.delete_sources",
            "coa_control_plane.namespace.deletion_pipeline.finalize",
        ),
        src_roots={
            "coa_control_plane": _REPO_ROOT / "packages" / "control-plane" / "src",
            "coa_common": _COMMON,
        },
        declared_in=_STACKS / "namespace-stack.ts",
        declares="packages/control-plane/requirements.txt",
    ),
)


def _normalize(distribution: str) -> str:
    return distribution.lower().replace("_", "-")


def _module_path(bundle: Bundle, module: str) -> Path | None:
    root = bundle.src_roots.get(module.split(".", 1)[0])
    if root is None:
        return None
    relative = Path(*module.split("."))
    for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _executed_paths(bundle: Bundle, module: str) -> list[Path]:
    """Every first-party file Python runs to satisfy one import of ``module``."""
    parts = module.split(".")
    return [
        path
        for depth in range(1, len(parts) + 1)
        if (path := _module_path(bundle, ".".join(parts[:depth]))) is not None
    ]


def _package_of(bundle: Bundle, path: Path) -> list[str]:
    """Dotted package parts containing ``path``, for resolving relative imports."""
    for root in bundle.src_roots.values():
        try:
            return list(path.relative_to(root).parts[:-1])
        except ValueError:
            continue
    return []


def _resolve_relative(package: list[str], level: int, module: str | None) -> str | None:
    """Turn ``from ..x import y`` into an absolute dotted name, or ``None``."""
    if level - 1 >= len(package) and level > 1:
        return None
    base = package[: len(package) - (level - 1)] if level > 1 else package
    parts = [*base, *(module.split(".") if module else [])]
    return ".".join(parts) if parts else None


def _imports_in(bundle: Bundle, path: Path) -> set[str]:
    """Top-level dotted module names imported at MODULE SCOPE by one file."""
    found: set[str] = set()
    package = _package_of(bundle, path)
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.add(node.module)
            elif (resolved := _resolve_relative(package, node.level, node.module)) is not None:
                found.add(resolved)
    return found


def _reachable_third_party(bundle: Bundle) -> dict[str, str]:
    """Third-party top-level module -> the first-party file that pulled it in."""
    seen: set[Path] = set()
    queue = [path for entry in bundle.entry_points for path in _executed_paths(bundle, entry)]
    third_party: dict[str, str] = {}

    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for module in _imports_in(bundle, path):
            top = module.split(".", 1)[0]
            if top in bundle.src_roots:
                queue.extend(_executed_paths(bundle, module))
            elif top not in sys.stdlib_module_names:
                third_party.setdefault(top, str(path.relative_to(_REPO_ROOT)))
    return third_party


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda b: b.name)
class TestLambdaBundleDependencies:
    def test_entry_points_exist(self, bundle: Bundle):
        """Guards the walk against silently covering nothing if a handler moves."""
        for entry in bundle.entry_points:
            assert _module_path(bundle, entry) is not None, f"{bundle.name}: {entry} not found"

    def test_walk_reaches_the_shared_lib(self, bundle: Bundle):
        """A walk that stops at the package boundary would pass by covering nothing.

        Every bundle here ships ``coa_common``, and it is the shared lib's eager
        ``__init__`` that produced both outages, so reaching it is the minimum
        evidence that the walk is doing its job.
        """
        reached = {
            Path(importer).name for importer in _reachable_third_party(bundle).values() if "libs/common" in importer
        }
        assert reached, f"{bundle.name}: the walk never entered libs/common"

    def test_every_third_party_import_is_declared(self, bundle: Bundle):
        declared = bundle.declared()
        module_to_distributions = packages_distributions()
        missing: list[str] = []

        for module, importer in sorted(_reachable_third_party(bundle).items()):
            if module in _RUNTIME_PROVIDED:
                continue
            candidates = {_normalize(d) for d in module_to_distributions.get(module, [])} or {_normalize(module)}
            if not candidates & declared:
                missing.append(f"{module} (imported by {importer}; provide one of {sorted(candidates)})")

        assert not missing, (
            f"{bundle.name}: these modules are imported at module scope but no distribution "
            f"providing them is declared in {bundle.declared_in.relative_to(_REPO_ROOT)}"
            f" ({bundle.declares}):\n  " + "\n  ".join(missing) + "\n\n"
            "The uv workspace resolves them transitively, so tests and lint pass, but the "
            "deployed bundle installs ONLY what is declared — every route then returns 502 "
            "with Runtime.ImportModuleError."
        )
