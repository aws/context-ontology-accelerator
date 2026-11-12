# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SCLConfig."""

import pytest
from coa_common.config import SCLConfig, resolve_region


@pytest.mark.unit
class TestSCLConfig:
    """Tests for the base configuration."""

    def test_default_values(self, monkeypatch):
        """Config should have sensible defaults."""
        # aws_region falls back to the runtime environment (resolve_region),
        # so clear the region vars or this test inherits the host's region.
        for var in ("SCL_AWS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        config = SCLConfig()
        assert config.project_name == "coa"
        assert config.environment == "dev"
        assert config.log_level == "INFO"
        assert config.aws_region == "us-east-1"

    def test_env_override(self, monkeypatch):
        """Config should read from SCL_ prefixed env vars."""
        monkeypatch.setenv("SCL_ENVIRONMENT", "staging")
        monkeypatch.setenv("SCL_LOG_LEVEL", "DEBUG")
        config = SCLConfig()
        assert config.environment == "staging"
        assert config.log_level == "DEBUG"

    def test_aws_region_override(self, monkeypatch):
        """AWS region should be configurable."""
        monkeypatch.setenv("SCL_AWS_REGION", "eu-west-1")
        config = SCLConfig()
        assert config.aws_region == "eu-west-1"

    def test_aws_region_follows_runtime_environment(self, monkeypatch):
        """Infra sets AWS_REGION, not SCL_AWS_REGION, so the default must follow the runtime region."""
        monkeypatch.delenv("SCL_AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert SCLConfig().aws_region == "us-west-2"

    def test_scl_aws_region_precedence_over_runtime(self, monkeypatch):
        """The SCL_ prefixed var remains an explicit operator override."""
        monkeypatch.setenv("SCL_AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert SCLConfig().aws_region == "eu-west-1"


@pytest.mark.unit
class TestResolveRegion:
    """Tests for the shared resolve_region() helper."""

    def test_prefers_aws_region(self, monkeypatch):
        """AWS_REGION takes precedence over AWS_DEFAULT_REGION."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        assert resolve_region() == "us-west-2"

    def test_falls_back_to_aws_default_region(self, monkeypatch):
        """AWS_DEFAULT_REGION is used when AWS_REGION is unset."""
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        assert resolve_region() == "eu-west-1"

    def test_falls_back_to_default_when_neither_set(self, monkeypatch):
        """Falls back to the provided default when no region env var is set."""
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert resolve_region() == "us-east-1"

    def test_custom_default(self, monkeypatch):
        """Caller can override the ultimate fallback region."""
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert resolve_region(default="ap-southeast-2") == "ap-southeast-2"

    def test_empty_aws_region_is_returned_as_set(self, monkeypatch):
        """An empty-string AWS_REGION counts as set; only missing vars fall through."""
        monkeypatch.setenv("AWS_REGION", "")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        assert resolve_region() == ""
