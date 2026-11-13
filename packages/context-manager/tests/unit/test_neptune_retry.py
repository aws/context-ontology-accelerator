# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune serve client uses a split connect/read timeout."""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.unit


def test_neptune_http_client_has_split_timeout():
    from coa_serve.clients import neptune as nmod

    client = nmod.NeptuneGraphClient(endpoint="db.example.com", region="us-east-1")

    http = asyncio.run(client._get_http())
    assert isinstance(http.timeout, httpx.Timeout)
    assert http.timeout.connect == 2.0
