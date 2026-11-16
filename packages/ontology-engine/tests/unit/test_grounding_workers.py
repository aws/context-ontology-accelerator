# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``_grounding_workers`` defaults to 24 and ``INDUCER_GROUNDING_WORKERS`` overrides it.

The default is coupled to the shared bedrock client's connection cap.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.services.pipeline import _grounding_workers


def test_grounding_workers_defaults_to_24(monkeypatch) -> None:
    monkeypatch.delenv("INDUCER_GROUNDING_WORKERS", raising=False)
    assert _grounding_workers() == 24


def test_grounding_workers_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("INDUCER_GROUNDING_WORKERS", "32")
    assert _grounding_workers() == 32
