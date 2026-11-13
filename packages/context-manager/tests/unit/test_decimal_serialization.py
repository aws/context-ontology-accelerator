# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: numeric/Decimal query results must serialize.

The SSE ``done`` event and the session-history envelope serialize rows via stdlib json, which rejects Decimal.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest
from coa_serve.serialization import json_safe as _json_safe
from coa_serve.session import _build_message_envelope

pytestmark = pytest.mark.unit


class TestEnvelopeDecimalSerialization:
    def test_envelope_serializes_decimal_result_rows(self) -> None:
        env = _build_message_envelope(
            "Total claim amount is 46550.50",
            {"resultRows": [{"amount_type_code": "LOSS", "total": Decimal("46550.50")}]},
        )
        parsed = json.loads(env)
        assert parsed["resultRows"][0]["total"] == 46550.50

    def test_envelope_serializes_date_and_datetime(self) -> None:
        env = _build_message_envelope(
            "ok",
            {"resultRows": [{"d": datetime.date(2026, 1, 1)}]},
        )
        parsed = json.loads(env)
        assert parsed["resultRows"][0]["d"] == "2026-01-01"


class TestEmitterJsonSafe:
    def test_decimal_becomes_float(self) -> None:
        assert _json_safe(Decimal("99.9")) == 99.9

    def test_nested_containers_are_sanitized(self) -> None:
        out = _json_safe({"result": {"resultRows": [{"amt": Decimal("1.5")}, {"amt": Decimal("2")}]}})
        # Must be plain json.dumps-able (no custom encoder).
        json.dumps(out)
        assert out["result"]["resultRows"][0]["amt"] == 1.5
        assert out["result"]["resultRows"][1]["amt"] == 2.0

    def test_native_types_pass_through(self) -> None:
        val = {"a": 1, "b": "x", "c": [True, None, 3.14]}
        assert _json_safe(val) == val
