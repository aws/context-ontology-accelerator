# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the integ provisioning CLI (no AWS, no deployment).

The CLI provisions the cross-tier data once per CI pipeline. A key that provisioning
sets but the CLI does not export makes the consumer job silently re-provision it, and a
value containing a newline corrupts every variable after it in the dotenv artifact.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CLI_PATH = _REPO_ROOT / "packages" / "context-manager" / "tests" / "integ" / "provision_cli.py"
_CONFTEST_PATH = _REPO_ROOT / "packages" / "context-manager" / "tests" / "integ" / "conftest.py"

if not _CLI_PATH.exists() or not _CONFTEST_PATH.exists():
    pytest.skip("tests/integ/ is absent or incomplete in this checkout (public mirror)", allow_module_level=True)


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provision_cli_under_test", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_cli = _load_cli()


def test_exports_every_integ_var_the_conftest_sets() -> None:
    """Every INTEG_* var provisioning writes must be exported to the dotenv.

    Read from the conftest source rather than by importing it: importing pulls in
    boto3/requests and the shared fixtures module, which a unit test should not need.
    """
    source = _CONFTEST_PATH.read_text()
    assigned = set(re.findall(r'os\.environ(?:\.setdefault\(|\[)"(INTEG_[A-Z0-9_]+)"', source))
    # Set by the shared _provision_user fixture, not by the conftest's own provisioning.
    assigned.add("INTEG_SECRET_ARN")

    missing = assigned - set(_cli.EXPORTED_ENV_KEYS)
    assert not missing, (
        f"provision_cli.EXPORTED_ENV_KEYS is missing {sorted(missing)}. A consumer job "
        f"would re-provision that slice in-process instead of inheriting it — slow, and "
        f"silent. Add the key (or drop it from the conftest)."
    )


def test_env_file_is_written_in_gitlab_dotenv_format(tmp_path: Path) -> None:
    path = tmp_path / "integ.env"
    _cli._write_env_file(path, {"B_KEY": "two", "A_KEY": "one"})

    lines = path.read_text().splitlines()
    assert lines == ["A_KEY=one", "B_KEY=two"], "expected sorted KEY=value lines"
    assert path.read_text().endswith("\n")


def test_env_file_rejects_a_value_with_a_newline(tmp_path: Path) -> None:
    """A newline would corrupt every variable after it in the artifact."""
    with pytest.raises(ValueError, match="newline"):
        _cli._write_env_file(tmp_path / "integ.env", {"BAD": "line1\nline2"})


def test_teardown_is_a_noop_when_nothing_was_provisioned(tmp_path: Path, monkeypatch) -> None:
    """No recorded namespaces must not touch AWS — and must not fail the pipeline."""
    monkeypatch.delenv("INTEG_PROVISIONED_NAMESPACES", raising=False)

    def _explode() -> None:  # pragma: no cover - must never run
        raise AssertionError("teardown loaded the conftest despite having nothing to do")

    monkeypatch.setattr(_cli, "_load_conftest", _explode)
    assert _cli._teardown(tmp_path / "absent.env") == 0


def test_teardown_reads_namespaces_from_the_env_file(tmp_path: Path, monkeypatch) -> None:
    """The dotenv artifact is the fallback when the variable is not in the environment."""
    env_file = tmp_path / "integ.env"
    env_file.write_text("INTEG_METRIC_NAMESPACE=ns-a\nINTEG_PROVISIONED_NAMESPACES=ns-a,ns-b\n")
    monkeypatch.delenv("INTEG_PROVISIONED_NAMESPACES", raising=False)

    cleaned: list[str] = []

    class _FakeConftest:
        @staticmethod
        def _http_headers_for_provisioning():
            return "https://api.example.com", {"Authorization": "Bearer x"}

        @staticmethod
        def _cleanup_namespace(endpoint, headers, namespace_id):
            cleaned.append(namespace_id)

    monkeypatch.setattr(_cli, "_load_conftest", lambda: _FakeConftest)
    assert _cli._teardown(env_file) == 0
    assert cleaned == ["ns-a", "ns-b"]


def test_teardown_survives_unresolvable_credentials(tmp_path: Path, monkeypatch) -> None:
    """A credential blip must leave the namespaces to the reaper, not fail the job."""
    monkeypatch.setenv("INTEG_PROVISIONED_NAMESPACES", "ns-a")

    class _FakeConftest:
        @staticmethod
        def _http_headers_for_provisioning():
            return None

    monkeypatch.setattr(_cli, "_load_conftest", lambda: _FakeConftest)
    assert _cli._teardown(tmp_path / "integ.env") == 0


def test_failed_provisioning_records_partial_namespaces_for_teardown(tmp_path: Path, monkeypatch) -> None:
    """A half-provisioned namespace must still reach the teardown job.

    Otherwise a failed provision leaks a namespace + source + ontology + per-namespace
    VKG until the 2h orphan reaper catches it.
    """
    env_file = tmp_path / "integ.env"

    class _FakeConftest:
        _PROVISIONED_NAMESPACES = ["ns-partial"]

        @staticmethod
        def _http_headers_for_provisioning():
            return "https://api.example.com", {"Authorization": "Bearer x"}

        @staticmethod
        def _reap_orphaned_namespaces(endpoint, headers):
            return None

        @staticmethod
        def _provision_shared(endpoint, headers):
            raise RuntimeError("scan never reached a terminal state")

    monkeypatch.setattr(_cli, "_load_conftest", lambda: _FakeConftest)
    with pytest.raises(RuntimeError, match="scan never reached"):
        _cli._provision(env_file)

    assert env_file.read_text().strip() == "INTEG_PROVISIONED_NAMESPACES=ns-partial"


def test_skip_from_a_provisioning_helper_is_fatal(tmp_path: Path, monkeypatch) -> None:
    """pytest.skip() outside a test session must fail the job, not read as success."""

    class _FakeConftest:
        _PROVISIONED_NAMESPACES: list[str] = []

        @staticmethod
        def _http_headers_for_provisioning():
            return "https://api.example.com", {"Authorization": "Bearer x"}

        @staticmethod
        def _reap_orphaned_namespaces(endpoint, headers):
            return None

        @staticmethod
        def _provision_shared(endpoint, headers):
            pytest.skip("test databases not deployed")

    monkeypatch.setattr(_cli, "_load_conftest", lambda: _FakeConftest)
    with pytest.raises(SystemExit) as excinfo:
        _cli._provision(tmp_path / "integ.env")
    assert excinfo.value.code == 1
