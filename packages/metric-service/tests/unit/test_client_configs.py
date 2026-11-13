# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Neptune SPARQL client carries a split connect/read timeout."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.unit


def test_metric_neptune_split_timeout():
    from coa_metrics import neptune_client as nc

    assert isinstance(nc.NEPTUNE_HTTP_TIMEOUT, httpx.Timeout)
    assert nc.NEPTUNE_HTTP_TIMEOUT.connect == 2.0
    assert nc.NEPTUNE_HTTP_TIMEOUT.read == nc.NEPTUNE_TIMEOUT
