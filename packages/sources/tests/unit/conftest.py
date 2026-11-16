# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources unit test configuration.

``ALLOWED_ORIGIN`` is required by coa_common.response, so it is set before any
handler imports. ``AWS_REGION`` is pinned (not ``setdefault``-ed) because
``resolve_region()`` reads it before ``AWS_DEFAULT_REGION`` and the moto fixtures
live in us-east-1. Credentials are overwritten, and every network connection
outside loopback fails the test that made it (see ``block_real_network``).
Because these overrides happen at import, run integration tests in their own
pytest process rather than together with ``tests/unit``.
"""

import ipaddress
import os
import socket
import traceback
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from coa_common.dao import DynamoDBDAO

os.environ.setdefault("ALLOWED_ORIGIN", "https://test.example.com")

# Overwrite, don't setdefault: an inherited value is exactly the problem.
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
# moto rejects requests with no credentials; keep them obviously fake so a
# misconfigured test can never reach real AWS. Overwrite them and stop botocore
# from reading a profile, the shared config files or instance metadata.
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.pop("AWS_PROFILE", None)
os.environ.pop("AWS_DEFAULT_PROFILE", None)
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
# An exported endpoint override (e.g. localstack) would otherwise send calls past moto.
for _endpoint_var in [k for k in os.environ if k.startswith("AWS_ENDPOINT_URL")]:
    os.environ.pop(_endpoint_var)

# ``sources_handler`` reads ``SOURCES_TABLE`` at import time, so it is set here,
# before pytest imports any test module. The value matches the moto table in
# test_sources_handler.py.
os.environ.setdefault("SOURCES_TABLE", "test-sources")

# Imported after the env block above: the module reads AWS_REGION at import time.
from coa_sources.database import glue_ownership  # noqa: E402

_STUB_ACCOUNT = "0" * 12


class RealNetworkBlocked(RuntimeError):
    """A unit test tried to open a network connection outside the loopback interface.

    Deliberately not an ``OSError``: retry helpers treat connection errors as
    transient and would back off for minutes before the test could fail.
    """


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX path or an unusual family: not a network peer
    host = address[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def resource_prefix(monkeypatch):
    """Every test runs with the deployment prefix CDK sets on every Lambda that derives a name from it.

    Its accessor refuses to default (the Athena catalog name and the sts:ExternalId come from it), so a test module
    must not depend on another one having set it at import.
    """
    monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail any test that connects outside the loopback interface.

    The attempt raises ``RealNetworkBlocked`` at the call site and is also recorded,
    so the test fails at teardown even when product code catches the exception
    (several call sites are deliberately best-effort).
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address) -> None:
        if not _is_loopback(address):
            # Name the product frame that opened the socket: best-effort call sites
            # swallow the exception, so the teardown report is all that is left.
            callers = [
                f"{frame.filename.rsplit('/src/', 1)[-1]}:{frame.lineno}"
                for frame in traceback.extract_stack()
                if "/src/coa_" in frame.filename
            ]
            attempts.append(f"{address!r} from {callers[-1] if callers else 'unknown'}")
            raise RealNetworkBlocked(f"unit test attempted a real network connection to {address!r}")

    def _connect(self, address):
        _check(address)
        return real_connect(self, address)

    def _connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    yield
    if attempts:
        pytest.fail(f"unit test attempted real network connections (missing mock?): {attempts}")


def dao_double() -> Any:
    """A ``DynamoDBDAO`` double that rejects calls the real class would reject.

    Use this wherever a test patches ``_get_dao``. Autospec binds each method's real
    signature; ``MagicMock(spec=DynamoDBDAO)`` only checks that the attribute exists.
    """
    return create_autospec(DynamoDBDAO, instance=True)


@pytest.fixture(autouse=True)
def stub_glue_ownership(request):
    """Treat every Glue database as shared with all namespaces, unless opted out.

    The Glue ownership check calls ``glue:GetTags`` and STS, which unstubbed would
    reach for the network and fail closed. It is covered directly in
    ``tests/unit/database/test_glue_ownership.py`` and by tests carrying the
    ``real_glue_ownership`` marker, which this fixture steps aside for.
    """
    if request.node.get_closest_marker("real_glue_ownership"):
        yield
        return
    shared = glue_ownership._TagLookup(frozenset({glue_ownership.SHARED_WITH_ALL}), missing=False)
    with (
        patch.object(glue_ownership, "_tagged_namespaces", return_value=shared),
        patch.object(glue_ownership, "_deployment_account", return_value=_STUB_ACCOUNT),
    ):
        yield
