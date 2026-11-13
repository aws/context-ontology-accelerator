# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for parallel Athena enum-sampling: concurrency, its cap, best-effort and result placement."""

from __future__ import annotations

import importlib.util
import threading
import time
from unittest.mock import patch

import pytest
from coa_common.domain_models import Column, Table
from coa_sources.database.connectors import athena_sampler as athena_mod
from coa_sources.database.connectors.athena_sampler import AthenaSampler
from coa_sources.database.connectors.glue_catalog import GlueCatalogConnector

pytestmark = pytest.mark.unit

_HOLD_S = 0.05  # per-"query" dwell — long enough for threads to overlap observably.


def _sampler() -> AthenaSampler:
    # Explicit output_location makes .enabled True without env.
    return AthenaSampler(region="us-east-1", workgroup="wg-test", output_location="s3://spill/enum/")


class _ConcurrencyTracker:
    """Fake ``_sample_one`` recording the max number of concurrent in-flight calls."""

    def __init__(self, hold_s: float = _HOLD_S) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self._hold_s = hold_s

    def __call__(self, database: str, table: str, col: str, max_distinct: int) -> list[str]:
        with self._lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
        try:
            time.sleep(self._hold_s)
            return [f"{table}:{col}"]
        finally:
            with self._lock:
                self.current -= 1


def test_columns_sampled_concurrently_max_in_flight_gt_one():
    """Core assertion: parallel fan-out overlaps per-column sampling.

    On the ORIGINAL serial ``for col in candidates`` loop this fails (max == 1).
    """
    s = _sampler()
    tracker = _ConcurrencyTracker()
    cols = [(f"c{i}", "varchar") for i in range(8)]
    with patch.object(s, "_sample_one", side_effect=tracker):
        out = s.sample_columns("db", "t", cols)
    assert tracker.max_seen > 1, f"expected concurrent sampling, saw max_in_flight={tracker.max_seen}"
    assert len(out) == 8  # every column sampled


def test_concurrency_capped_at_env_knob(monkeypatch):
    """With ATHENA_SAMPLING_CONCURRENCY=4, max in-flight never exceeds 4."""
    monkeypatch.setattr(athena_mod, "ATHENA_SAMPLING_CONCURRENCY", 4)
    s = _sampler()
    tracker = _ConcurrencyTracker()
    # 20 tables x 1 col — plenty to exceed the cap if unbounded.
    tables = [(f"t{i}", [("c", "varchar")]) for i in range(20)]
    with patch.object(s, "_sample_one", side_effect=tracker):
        s.sample_all("db", tables)
    assert tracker.max_seen > 1, "sampling did not run in parallel"
    assert tracker.max_seen <= 4, f"cap exceeded: max_in_flight={tracker.max_seen}"


def test_one_column_failure_does_not_abort_others():
    """Best-effort: a raising column is skipped; siblings are still sampled and
    no exception escapes ``_sample_enum_values``."""

    def flaky(database: str, table: str, col: str, max_distinct: int) -> list[str]:
        if col == "bad":
            raise RuntimeError("athena denied for this column")
        return [f"{col}_val"]

    tables = [
        Table(
            name="orders",
            database="db",
            columns=[
                Column(name="good1", data_type="varchar"),
                Column(name="bad", data_type="varchar"),
                Column(name="good2", data_type="string"),
            ],
        )
    ]
    with (
        patch.dict("os.environ", {"ATHENA_WORKGROUP": "wg-test"}),
        patch.object(AthenaSampler, "_sample_one", autospec=False, side_effect=flaky),
    ):
        # Must not raise.
        GlueCatalogConnector._sample_enum_values(tables, "db", "cat", "us-east-1")

    by_name = {c.name: c.distinct_values for c in tables[0].columns}
    assert by_name["good1"] == ["good1_val"]
    assert by_name["good2"] == ["good2_val"]
    assert by_name["bad"] == []  # failed column left unsampled


def test_values_assigned_to_correct_columns_regardless_of_completion_order():
    """Out-of-order completion still maps each result back to its own column."""
    s = _sampler()
    # First-submitted columns dwell longest → they complete last.
    delays = {("t", f"c{i}"): _HOLD_S * (6 - i) for i in range(6)}

    def fake(database: str, table: str, col: str, max_distinct: int) -> list[str]:
        time.sleep(delays[(table, col)])
        return [f"{table}|{col}"]

    cols = [(f"c{i}", "varchar") for i in range(6)]
    with patch.object(s, "_sample_one", side_effect=fake):
        out = s.sample_columns("t", "t", cols)  # database==table irrelevant to assignment
    assert out == {f"c{i}": [f"t|c{i}"] for i in range(6)}


def _reimport_athena_module():
    """Execute athena_sampler.py fresh under a throwaway name.

    Proves IMPORT-time behaviour under the current env without ``reload``-ing the
    live module (that would rebind ``AthenaSampler`` to a new class object and
    make the other tests in this file order-dependent).
    """
    spec = importlib.util.spec_from_file_location(athena_mod.__name__ + "_probe", athena_mod.__file__)
    assert spec and spec.loader
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)  # must not raise, whatever the env holds
    return fresh


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("bogus", 16),  # non-numeric → default (a raw int() raises at import)
        ("", 16),  # empty → default
        ("0", 1),  # floored: ThreadPoolExecutor(max_workers=0) raises
        ("-4", 1),  # floored
        ("4", 4),  # valid value honoured
    ],
)
def test_concurrency_env_parse_never_raises(monkeypatch, env_value, expected):
    """A misconfigured ATHENA_SAMPLING_CONCURRENCY must not break import or the pool."""
    monkeypatch.setenv("ATHENA_SAMPLING_CONCURRENCY", env_value)
    parsed = _reimport_athena_module().ATHENA_SAMPLING_CONCURRENCY
    assert parsed == expected


def test_parallel_result_equals_serial_result():
    """For a fixed fake ``_run``, the parallel dict == the serial dict."""
    # Per-column gate inputs; all chosen to PASS (bounded distinct, repeated).
    specs = {
        "return_state": (100, 2, ["RETURNED", "SHIPPED"]),
        "priority": (90, 3, ["LOW", "MED", "HIGH"]),
        "region": (120, 4, ["NA", "EU", "APAC", "SA"]),
    }

    def run(sql: str) -> list[list]:
        low = sql.lower()
        for col, (total, distinct, values) in specs.items():
            if f'"{col}"' in low:
                if "count(" in low:
                    return [[str(total), str(distinct)]]
                if "select distinct" in low:
                    return [[v] for v in values]
        return []

    cols = [(c, "varchar") for c in specs]

    s_par = _sampler()
    with patch.object(s_par, "_run", side_effect=run):
        parallel = s_par.sample_columns("db", "t", cols)

    assert parallel == {col: vals for col, (_, _, vals) in specs.items()}
