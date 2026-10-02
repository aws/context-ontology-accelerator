# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources unit test configuration.

``ALLOWED_ORIGIN`` is required by coa_common.response (lazy check at
api_response() call time). Set before any handler imports so api_response() can
return error responses; otherwise every handler test raises
RuntimeError("ALLOWED_ORIGIN environment variable must be set"). Matches the
pattern used by control-plane and metric-service unit conftests.

``AWS_REGION`` is pinned — not ``setdefault``-ed — because the handlers resolve
their region via ``coa_common.resolve_region()``, which reads ``AWS_REGION``
FIRST and only then falls back to ``AWS_DEFAULT_REGION``. The moto fixtures
create their tables in us-east-1 and several test modules set only
``AWS_DEFAULT_REGION``, so a developer (or CI runner) with ``AWS_REGION`` exported
to anything else had the handler build its DAO against that region while the
table lived in us-east-1 — every query failed with ResourceNotFoundException and
the handler returned 500. That made ~10 tests fail depending on nothing but the
shell they ran in.

Credentials are overwritten for the same reason, and every network connection
outside the loopback interface fails the test that made it (see
``block_real_network``). Unit tests use moto or mocks, which never open a socket;
a test that reaches for the network has a missing mock, and with a developer's
exported credentials that call would otherwise land in a real account.
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

# ``sources_handler`` captures ``SOURCES_TABLE`` at IMPORT time (module-level
# ``_SOURCES_TABLE``), so whichever test module imports it FIRST decides whether
# the DAO can be built at all — every later `_get_dao()` raises "SOURCES_TABLE env
# var not set" if that first import saw no value. test_sources_handler.py used to
# be that module and set the var itself, which held only because it happened to
# sort before the other importers. Any new module importing the handler earlier,
# directly or transitively, silently broke ~14 of its tests. Setting it here —
# before pytest imports any test module — removes the ordering dependence, the
# same reasoning as the AWS_REGION block above. The value matches the table
# test_sources_handler.py creates in moto.
os.environ.setdefault("SOURCES_TABLE", "test-sources")

# Imported after the env block above: the module reads AWS_REGION at import time.
from coa_sources.database import glue_ownership  # noqa: E402

_STUB_ACCOUNT = "000000000000"


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

    Use this wherever a test patches ``_get_dao``. A bare ``MagicMock()`` accepts any
    method name with any signature, which shipped a ``dao.put(..., condition_values=...)``
    that raised ``TypeError`` on every real request while the tests stayed green.
    ``MagicMock(spec=DynamoDBDAO)`` does not close it either — it checks only that the
    attribute exists — whereas autospec binds each method's real signature.
    """
    return create_autospec(DynamoDBDAO, instance=True)


@pytest.fixture(autouse=True)
def stub_glue_ownership(request):
    """Treat every Glue database as shared with all namespaces, unless opted out.

    Creating or scanning a Glue source now requires the target database to be
    registered to the caller's namespace — a live ``glue:GetTags`` call and an STS
    identity lookup (see ``coa_sources.database.glue_ownership``). Neither is what
    the tests around them are about, and unstubbed both reach for the network and
    then fail closed, turning every Glue fixture into a 403.

    The ownership check itself is covered directly in
    ``tests/unit/database/test_glue_ownership.py`` and the route/pipeline tests that
    carry the ``real_glue_ownership`` marker, which this fixture steps aside for.
    Reach for that marker in any new test where the ownership decision is the
    subject rather than a precondition.
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
