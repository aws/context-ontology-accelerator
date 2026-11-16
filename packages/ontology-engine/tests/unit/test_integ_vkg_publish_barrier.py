# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit cover for the integ suite's VKG-publish barrier.

``_wait_for_vkg_publish`` is the fix for a race that only reproduces against a
deployed environment (accept flips the proposal to ``accepted`` three pipeline
steps before it writes ``latest/mappings.r2rml``), so its own logic — tolerate a
missing object, wait for the ETag to CHANGE, give up on a budget — would
otherwise be covered by nothing that runs on a PR. Loaded by path because the
integ directory is not an importable package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[1] / "integ" / "test_accept_ontology_components.py"

if not _MODULE_PATH.exists():
    pytest.skip("tests/integ/ is absent or incomplete in this checkout", allow_module_level=True)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("_accept_components_integ", _MODULE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeS3:
    """head_object over a scripted sequence: None → 404, str → that ETag."""

    def __init__(self, sequence: list[str | None]):
        self.sequence = list(sequence)
        self.calls = 0

    def head_object(self, **_kwargs: Any) -> dict[str, str]:
        self.calls += 1
        etag = self.sequence.pop(0) if self.sequence else None
        if etag is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ETag": etag}


@pytest.fixture
def patched(mod, monkeypatch):
    """Wire the module to a scripted S3 and remove real waiting/STS."""

    def _install(sequence: list[str | None], timeout: int = 30):
        fake = _FakeS3(sequence)
        monkeypatch.setattr(mod, "boto3", SimpleNamespace(client=lambda *a, **k: fake))
        monkeypatch.setattr(mod, "_LATEST_BUCKET", "bucket")
        monkeypatch.setattr(mod, "VKG_PUBLISH_TIMEOUT", timeout)
        monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
        return fake

    return _install


def test_wait_for_vkg_publish_returns_etag_once_object_appears(mod, patched):
    """A first accept publishes late: 404s are tolerated until the object lands."""
    fake = patched([None, None, '"abc"'])
    assert mod._wait_for_vkg_publish("ns-1") == '"abc"'
    assert fake.calls == 3


def test_wait_for_vkg_publish_waits_for_etag_to_change(mod, patched):
    """A merge accept must not be satisfied by the PREVIOUS accept's payload."""
    patched(['"old"', '"old"', '"new"'])
    assert mod._wait_for_vkg_publish("ns-1", since_etag='"old"') == '"new"'


def test_wait_for_vkg_publish_returns_none_when_publish_never_lands(mod, patched):
    """Budget exhausted returns None (caller's own assertion reports the failure)."""
    patched(['"old"'] * 50, timeout=0)
    assert mod._wait_for_vkg_publish("ns-1", since_etag='"old"') is None


def test_wait_for_vkg_publish_reraises_non_404_client_errors(mod, patched):
    """AccessDenied is a real defect, not a not-yet-published — must surface."""
    fake = patched([])

    def _denied(**_kwargs: Any):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "HeadObject")

    fake.head_object = _denied
    with pytest.raises(ClientError):
        mod._wait_for_vkg_publish("ns-1")
