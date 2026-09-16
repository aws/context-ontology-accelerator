# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Functional health-probe tests: health requires a real query to
reformulate to SQL, not just Ontop being up (--lazy flips UP too early)."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# Unique module name (+ patch.object) so this never collides with
# test_translate_contract.py, which also loads translate-server.py.
_server_path = Path(__file__).parent.parent / "translate-server.py"
_spec = importlib.util.spec_from_file_location("translate_server_probe", _server_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Failed to load translate_server from {_server_path}")
ts = importlib.util.module_from_spec(_spec)
sys.modules["translate_server_probe"] = ts
_spec.loader.exec_module(ts)


def _resp(body: str):
    """A urlopen-style context manager returning `body` bytes."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body.encode("utf-8")
    return cm


class TestReformulateProbe:
    def test_true_when_reformulate_returns_sql(self):
        raw = "ans1(s)\nCONSTRUCT [...]\nSELECT t0.id FROM claims t0"
        with patch.object(ts.urllib.request, "urlopen", return_value=_resp(raw)):
            ok, reason = ts._reformulate_probe()
            assert ok is True
            assert reason is None

    def test_true_when_reformulate_returns_with_cte(self):
        with patch.object(ts.urllib.request, "urlopen", return_value=_resp("WITH x AS (SELECT 1) SELECT * FROM x")):
            ok, reason = ts._reformulate_probe()
            assert ok is True
            assert reason is None

    def test_false_with_reason_when_reformulate_returns_non_sql(self):
        # Ontop can return an error/empty body even while the process is up.
        with patch.object(ts.urllib.request, "urlopen", return_value=_resp("ERROR: no mapping for predicate")):
            ok, reason = ts._reformulate_probe()
            assert ok is False
            assert reason and "reformulate" in reason.lower()

    def test_false_with_reason_when_reformulate_raises(self):
        with patch.object(ts.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            ok, reason = ts._reformulate_probe()
            assert ok is False
            assert reason and "probe failed" in reason.lower()


class TestProbeHealth:
    def test_healthy_when_up_and_reformulate_succeeds(self):
        with (
            patch.object(ts.urllib.request, "urlopen", return_value=_resp('{"status": "UP"}')),
            patch.object(ts, "_reformulate_probe", return_value=(True, None)),
        ):
            ok, reason = ts._probe_health()
            assert ok is True
            assert reason is None

    def test_unhealthy_with_reason_when_actuator_up_but_reformulate_fails(self):
        # The core case: process UP under --lazy but mappings not loaded,
        # so translation fails. Must report unhealthy despite actuator UP.
        with (
            patch.object(ts.urllib.request, "urlopen", return_value=_resp('{"status": "UP"}')),
            patch.object(ts, "_reformulate_probe", return_value=(False, "mappings did not reformulate")),
        ):
            ok, reason = ts._probe_health()
            assert ok is False
            assert reason == "mappings did not reformulate"

    def test_unhealthy_with_reason_when_actuator_down(self):
        with (
            patch.object(ts.urllib.request, "urlopen", return_value=_resp('{"status": "DOWN"}')),
            patch.object(ts, "_reformulate_probe", return_value=(True, None)) as probe,
        ):
            ok, reason = ts._probe_health()
            assert ok is False
            assert reason and "DOWN" in reason
            probe.assert_not_called()  # actuator down short-circuits before the probe

    def test_unhealthy_with_reason_when_actuator_unreachable(self):
        with patch.object(ts.urllib.request, "urlopen", side_effect=OSError("refused")):
            ok, reason = ts._probe_health()
            assert ok is False
            assert reason and "unreachable" in reason.lower()


class TestHealthReader:
    """_check_ontop_health returns the cached probe result the /health endpoint serves."""

    def test_reader_reflects_cache(self):
        with ts._health_lock:
            ts._health_status["healthy"] = True
        assert ts._check_ontop_health() is True
        with ts._health_lock:
            ts._health_status["healthy"] = False
        assert ts._check_ontop_health() is False
