# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the infra Lambda handler tests.

Patch the boto3 factory to mock the cloudwatch and logs clients so they survive the
in-test `importlib.reload(index)` and never make real CloudWatch calls.

Adding a test for another Lambda handler here
-----------------------------------------------
Every handler is named ``index.py``, so importing one by its bare name registers
``sys.modules["index"]``; two test modules then fight over the slot and
``@patch("index.<attr>")`` raises ``AttributeError`` depending on run order.
``test_vkg_reload_handler.py`` owns the bare ``index`` name. Load any new handler under a
unique name with ``spec_from_file_location``, as ``test_lakeformation_admin_handler.py`` does.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest

# Handlers build boto3 clients at MODULE scope, before any fixture can replace them, and
# botocore resolves the region at construction: with no region configured the import
# raises ``NoRegionError``. Set at import time, with ``setdefault`` since only the
# presence of a region matters.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
# No test here talks to AWS; skip the EC2 instance-metadata probe, which waits ~5s in CI.
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

_VKG_RELOAD_DIR = Path(__file__).resolve().parents[2] / "lambda" / "vkg-reload"


@pytest.fixture(autouse=True)
def _guard_index_module_identity():
    """Fail loudly if ``sys.modules["index"]`` is not the vkg-reload handler."""
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
def _mock_cloudwatch_clients(monkeypatch):
    real_client = boto3.client

    def fake_client(name, *args, **kwargs):
        if name in ("cloudwatch", "logs"):
            return MagicMock()
        return real_client(name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", fake_client)
