# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for AWS SDK User-Agent tagging (usage attribution)."""

from __future__ import annotations

import boto3
import pytest
from coa_common import __version__
from coa_common.aws_user_agent import USER_AGENT_EXTRA, install_user_agent

pytestmark = pytest.mark.unit


def test_token_is_the_aws_solutions_format():
    assert f"AWSSOLUTION/SO0376/v{__version__}" == USER_AGENT_EXTRA


def test_default_session_client_carries_tag():
    install_user_agent()  # idempotent; import already ran it
    client = boto3.client("s3", region_name="us-east-1")
    assert USER_AGENT_EXTRA in client.meta.config.user_agent


def test_explicit_session_client_carries_tag():
    install_user_agent()
    session = boto3.Session(region_name="us-east-1")
    client = session.client("sts")
    assert USER_AGENT_EXTRA in client.meta.config.user_agent


def test_install_is_idempotent():
    # Repeated installs must not stack the tag multiple times.
    install_user_agent()
    install_user_agent()
    ua = boto3.Session(region_name="us-east-1")._session.user_agent_extra
    assert ua.split().count(USER_AGENT_EXTRA) == 1
