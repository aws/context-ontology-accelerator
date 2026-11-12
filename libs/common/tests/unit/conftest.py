# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for libs/common unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_real_cloudwatch():
    """Keep guardrail decision metrics off the real CloudWatch API."""
    with patch("coa_common.guardrail_metrics._cloudwatch_client", return_value=MagicMock()) as factory:
        yield factory
