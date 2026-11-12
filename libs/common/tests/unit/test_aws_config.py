# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared AWS retry/timeout config presets."""

from __future__ import annotations

import pytest
from coa_common.aws_config import (
    DEFAULT_ASYNC_READ_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SYNC_READ_TIMEOUT,
    async_boto_config,
    sync_boto_config,
)

pytestmark = pytest.mark.unit


def test_sync_config_is_fail_fast():
    cfg = sync_boto_config()
    assert cfg.connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert cfg.read_timeout == DEFAULT_SYNC_READ_TIMEOUT
    assert cfg.retries["mode"] == "standard"
    assert cfg.retries["max_attempts"] == 2  # 1 retry


def test_async_config_uses_backoff_and_more_attempts():
    cfg = async_boto_config()
    assert cfg.connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert cfg.read_timeout == DEFAULT_ASYNC_READ_TIMEOUT
    assert cfg.retries["mode"] == "standard"
    assert cfg.retries["max_attempts"] == 3  # 2 retries


def test_overrides_win_over_preset():
    cfg = sync_boto_config(read_timeout=45, max_attempts=1)
    assert cfg.read_timeout == 45
    assert cfg.retries["max_attempts"] == 1


def test_arbitrary_botoconfig_override_passthrough():
    cfg = async_boto_config(max_pool_connections=50)
    assert cfg.max_pool_connections == 50


def test_opensearch_sync_kwargs():
    from coa_common.aws_config import opensearch_client_kwargs

    kw = opensearch_client_kwargs(sync=True)
    assert kw["retry_on_timeout"] is True
    assert kw["max_retries"] == 1
    assert kw["timeout"] == 8


def test_opensearch_async_kwargs():
    from coa_common.aws_config import opensearch_client_kwargs

    kw = opensearch_client_kwargs(sync=False)
    assert kw["max_retries"] == 3
    assert kw["timeout"] == 30


def test_retry_policy_presets():
    from coa_common.aws_config import RetryPolicy

    assert RetryPolicy.for_async().attempts == 3
    assert RetryPolicy.for_async().max_delay >= RetryPolicy.for_async().base_delay


def test_apply_sets_default_client_config():
    import boto3
    from coa_common.aws_config import apply_default_client_config

    session = boto3.Session(region_name="us-east-1")
    apply_default_client_config(session._session)
    default = session._session.get_default_client_config()
    assert default is not None
    assert default.read_timeout == DEFAULT_ASYNC_READ_TIMEOUT
    assert default.connect_timeout == 2


def test_existing_default_client_config_is_preserved():
    import boto3
    from botocore.config import Config
    from coa_common.aws_config import apply_default_client_config

    session = boto3.Session(region_name="us-east-1")
    session._session.set_default_client_config(Config(read_timeout=999))
    apply_default_client_config(session._session)
    default = session._session.get_default_client_config()
    # Caller's explicit value wins; our connect_timeout fills the gap.
    assert default.read_timeout == 999
    assert default.connect_timeout == 2


def test_new_client_inherits_default_timeouts():
    import boto3

    # install_user_agent ran at package import; a fresh client should carry timeouts.
    client = boto3.client("s3", region_name="us-east-1")
    assert client.meta.config.connect_timeout == 2
    assert client.meta.config.read_timeout == DEFAULT_ASYNC_READ_TIMEOUT


def test_symbols_exported_from_package_root():
    import coa_common as scc

    assert hasattr(scc, "sync_boto_config")
    assert hasattr(scc, "async_boto_config")
    assert hasattr(scc, "opensearch_client_kwargs")
    assert hasattr(scc, "RetryPolicy")


def test_get_s3_client_defaults_to_timeout_config():
    from unittest.mock import patch

    from coa_common.s3 import get_s3_client

    with patch("coa_common.s3.boto3.client") as mock_client:
        get_s3_client()
        _, kwargs = mock_client.call_args
        assert kwargs["config"].connect_timeout == 2


def test_dynamodb_dao_defaults_to_timeout_config():
    from unittest.mock import patch

    from coa_common.dao.dynamodb import DynamoDBDAO

    with patch("coa_common.dao.dynamodb.boto3.resource") as mock_res:
        DynamoDBDAO("t", region="us-east-1")
        _, kwargs = mock_res.call_args
        assert kwargs["config"].connect_timeout == 2
