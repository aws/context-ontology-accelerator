# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pluggable Tier-2 skip evaluators.

A ``Tier2SkipEvaluator`` lets the orchestrator prune the Tier-2 structured-query
path for a query it provably cannot answer — WITHOUT the orchestrator owning any
of the detection logic. The evaluator is injected optionally; when absent,
Tier 2 always runs.

See ``evaluator.py`` for the interface and the fail-open contract, and
``aoss_provenance.py`` for the AOSS mapped-class-provenance provider.
"""

from __future__ import annotations

from .evaluator import Tier2SkipDecision, Tier2SkipEvaluator

__all__ = ["Tier2SkipDecision", "Tier2SkipEvaluator"]
