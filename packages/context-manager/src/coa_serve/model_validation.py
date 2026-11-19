# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-request model ID validation for LLM override."""

from __future__ import annotations

import os
import re

_MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._:/-]+$")

_ALLOWED_OVERRIDE_MODELS: set[str] | None = None
_raw_allowed = os.environ.get("ALLOWED_OVERRIDE_MODELS", "").strip()
if _raw_allowed:
    _ALLOWED_OVERRIDE_MODELS = {m.strip() for m in _raw_allowed.split(",") if m.strip()}


def validate_model_id(model_id: str) -> None:
    """Validate a per-request model ID override.

    Raises ValueError if the model ID fails format or allowlist checks.
    """
    if not model_id or not isinstance(model_id, str):
        raise ValueError("modelId must be a non-empty string.")
    if len(model_id) > 256:
        raise ValueError(f"modelId too long ({len(model_id)} chars). Max 256.")
    if not _MODEL_ID_PATTERN.match(model_id):
        raise ValueError("modelId contains invalid characters.")
    if _ALLOWED_OVERRIDE_MODELS is not None and model_id not in _ALLOWED_OVERRIDE_MODELS:
        raise ValueError(f"modelId {model_id!r} is not permitted for override.")
