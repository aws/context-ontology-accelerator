# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bedrock client — re-exported from coa_common."""

from coa_common.bedrock import (
    DEFAULT_MODEL_ID,
    BedrockClient,
    BedrockInvocationResult,
    BedrockTruncationError,
    GuardrailBlockedError,
)

__all__ = [
    "BedrockClient",
    "BedrockInvocationResult",
    "BedrockTruncationError",
    "DEFAULT_MODEL_ID",
    "GuardrailBlockedError",
]
