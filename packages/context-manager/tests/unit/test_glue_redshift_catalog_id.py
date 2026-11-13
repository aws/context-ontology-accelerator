# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Glue catalog-id resolution used by the Redshift-engine integ suite.

``catalogId`` must be a 12-digit account id, so an unset override resolves from STS.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.shared import glue_catalog_id as mod

pytestmark = pytest.mark.unit


@pytest.fixture
def _clear_cache():
    mod._glue_catalog_id.cache_clear()
    yield
    mod._glue_catalog_id.cache_clear()


@pytest.mark.usefixtures("_clear_cache")
def test_falls_back_to_the_callers_account_when_unset(monkeypatch) -> None:
    """An unset override must resolve from STS, never yield an empty catalogId."""
    monkeypatch.delenv("INTEG_GLUE_CATALOG_ID", raising=False)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    with patch.object(mod.boto3, "client", return_value=sts):
        assert mod._glue_catalog_id() == "123456789012"


@pytest.mark.usefixtures("_clear_cache")
def test_explicit_override_wins_and_is_not_overridden_by_sts(monkeypatch) -> None:
    monkeypatch.setenv("INTEG_GLUE_CATALOG_ID", "210987654321")
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    with patch.object(mod.boto3, "client", return_value=sts) as client:
        assert mod._glue_catalog_id() == "210987654321"
        client.assert_not_called()  # no STS call needed when told the answer


@pytest.mark.usefixtures("_clear_cache")
def test_blank_override_is_treated_as_unset(monkeypatch) -> None:
    """A CI variable defined-but-empty must not reintroduce the empty catalogId."""
    monkeypatch.setenv("INTEG_GLUE_CATALOG_ID", "   ")
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    with patch.object(mod.boto3, "client", return_value=sts):
        assert mod._glue_catalog_id() == "123456789012"


@pytest.mark.usefixtures("_clear_cache")
def test_resolution_is_cached_so_the_suite_makes_one_sts_call(monkeypatch) -> None:
    monkeypatch.delenv("INTEG_GLUE_CATALOG_ID", raising=False)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    with patch.object(mod.boto3, "client", return_value=sts) as client:
        mod._glue_catalog_id()
        mod._glue_catalog_id()
        assert client.call_count == 1
