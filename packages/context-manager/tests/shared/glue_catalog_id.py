# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Glue catalog-id resolution shared between the integ and unit suites.

Extracted here (outside ``tests/integ/``) so the unit suite can import it in
environments where integ tests are stripped (e.g. the public GitHub mirror).
"""

from __future__ import annotations

import os
from functools import lru_cache

import boto3

_AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))


@lru_cache(maxsize=1)
def _glue_catalog_id() -> str:
    """Glue catalog id: explicit override, else the caller's own AWS account.

    Resolved lazily from STS rather than defaulting to ``""``. An empty catalogId
    is not a soft failure: ``catalogId`` is length-constrained to an AWS account
    id, so every onboarding request built from it is rejected 400
    ``"String should have at least 12 characters"``.
    """
    override = os.environ.get("INTEG_GLUE_CATALOG_ID", "").strip()
    if override:
        return override
    return boto3.client("sts", region_name=_AWS_REGION).get_caller_identity()["Account"]
