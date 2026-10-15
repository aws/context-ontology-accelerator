# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pytest configuration for context-manager tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_aws_env():
    """Ensure tests don't hit real AWS services."""
    env_vars = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        # the firewall's DEFAULT Cedar authorizer fails CLOSED on a caller
        # with no resolved roles (prod-safe default). The general suite exercises
        # the firewall with role-less profiles and expects the permissive path, so
        # opt into the no-roles bypass for the test session (Cedar-specific tests
        # inject an explicit authorizer to assert both postures). Mirrors the dev
        # deployment env; prod leaves this UNSET so it fails closed.
        "SCL_CEDAR_FAIL_OPEN_NO_ROLES": "true",
        # No Roles table exists under unit tests, and the default Cedar authorizer
        # denies every query when none is configured. Opt into the shipped
        # seed policies for the session, as a local dev run does. Prod never sets
        # this (it always has ROLES_TABLE_NAME, where the opt-in has no effect).
        "ALLOW_WITHOUT_ROLES": "true",
    }
    with patch.dict(os.environ, env_vars):
        yield
