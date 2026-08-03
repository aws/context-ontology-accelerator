# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the infra Lambda handler tests.

Patch the boto3 factory to mock the cloudwatch client so it survives the in-test
`importlib.reload(index)` and never makes real PutMetricData calls.

Adding a test for another Lambda handler here
-----------------------------------------------
Every handler in this repo is named ``index.py``. Importing one by its bare
module name registers it as ``sys.modules["index"]``, so two such test modules
in this directory fight over that one slot: whichever imports last wins, and
``@patch("index.<attr>")`` in the other module then resolves against the wrong
handler and raises ``AttributeError`` — only when the files run together, which
makes it look like a flaky, order-dependent failure.

``test_vkg_reload_handler.py`` owns the bare ``index`` name (it patches
``index.ecs`` / ``index.sd`` / ``index.ssm`` in ~19 places). Any NEW handler test
must load its module under a unique name instead of touching ``sys.path`` — see
``test_lakeformation_admin_handler.py`` for the ``spec_from_file_location``
pattern.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

_VKG_RELOAD_DIR = Path(__file__).resolve().parents[2] / "lambda" / "vkg-reload"


@pytest.fixture(autouse=True)
def _guard_index_module_identity():
    """Fail loudly if ``sys.modules["index"]`` is not the vkg-reload handler.

    Turns the order-dependent ``AttributeError`` described in the module
    docstring into an explicit, self-explaining failure at the point of
    contamination, instead of a confusing patch error in an unrelated test.
    """
    yield
    module = sys.modules.get("index")
    origin = getattr(module, "__file__", None)
    if origin is not None and Path(origin).parent != _VKG_RELOAD_DIR:
        pytest.fail(
            f'sys.modules["index"] is {origin}, not the vkg-reload handler. '
            "A handler test imported its index.py by bare module name — load it under a "
            "unique name instead (see this directory's conftest docstring)."
        )


@pytest.fixture(autouse=True)
def _mock_cloudwatch_client(monkeypatch):
    real_client = boto3.client

    def fake_client(name, *args, **kwargs):
        if name == "cloudwatch":
            return MagicMock()
        return real_client(name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", fake_client)
