# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Application configuration using Pydantic Settings."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


def resolve_region(default: str = "us-east-1") -> str:
    """Resolve the active AWS region from the runtime environment.

    Checks ``AWS_REGION`` first, then falls back to ``AWS_DEFAULT_REGION``,
    then ``default``. In Lambda, ``AWS_REGION`` is always set by the runtime
    so the fallback never fires in production. Local dev and test environments
    benefit from the consistent fallback chain instead of each call site
    picking its own strategy.

    This is the single source of truth for region resolution across Python
    services. Do not inline ``os.environ.get("AWS_REGION", ...)`` at new call
    sites — import and use this helper instead.
    """
    return os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", default))


class SCLConfig(BaseSettings):
    """Base configuration for all services.

    Values are loaded from environment variables with the ``SCL_`` prefix.
    """

    model_config = {"env_prefix": "SCL_"}

    # General
    project_name: str = "coa"
    environment: str = "dev"
    log_level: str = "INFO"

    # AWS — SCL_AWS_REGION wins when set; otherwise resolve_region(). A hardcoded
    # default would pin every region to us-east-1.
    aws_region: str = Field(default_factory=resolve_region)

    # DynamoDB
    dynamodb_table_prefix: str = "coa"

    # S3
    s3_bucket_prefix: str = "coa"

    # OpenSearch
    opensearch_endpoint: str = ""

    # Neptune / SPARQL
    sparql_endpoint: str = ""
