# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test helpers shared by the packages' unit tests.

Imported only from tests; ``moto`` is a development dependency and is never
available (or needed) in a deployed Lambda.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
from moto import mock_aws

from coa_common.idempotency import reset_store_cache

DEFAULT_IDEMPOTENCY_TABLE = "test-idempotency"


@contextmanager
def mock_idempotency_table(table_name: str = DEFAULT_IDEMPOTENCY_TABLE, region: str = "us-east-1") -> Iterator[Any]:
    """Yield a moto-backed idempotency table (boto3 ``Table`` resource).

    The caller sets ``IDEMPOTENCY_TABLE`` (to ``table_name``) and fake AWS
    credentials, e.g. with ``monkeypatch.setenv``. The cached stores are reset
    on entry and exit so no store bound to a previous mock leaks between tests.
    """
    reset_store_cache()
    try:
        with mock_aws():
            table = boto3.resource("dynamodb", region_name=region).create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            yield table
    finally:
        reset_store_cache()
