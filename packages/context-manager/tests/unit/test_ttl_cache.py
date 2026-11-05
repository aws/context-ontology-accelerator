# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared TtlLruCache."""

from __future__ import annotations

import pytest
from coa_serve.clients.ttl_cache import TtlLruCache


class _Clock:
    """Manual monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.unit
class TestTtlLruCache:
    def test_get_returns_set_value(self):
        c = TtlLruCache[str, int](ttl_s=100, max_size=10)
        c.set("a", 1)
        assert c.get("a") == 1

    def test_missing_key_returns_none(self):
        c = TtlLruCache[str, int](ttl_s=100, max_size=10)
        assert c.get("nope") is None

    def test_entry_expires_after_ttl(self):
        clk = _Clock()
        c = TtlLruCache[str, int](ttl_s=100, max_size=10, time_fn=clk)
        c.set("a", 1)
        clk.now = 99
        assert c.get("a") == 1  # still fresh just before TTL
        clk.now = 100
        assert c.get("a") is None  # expired at TTL boundary

    def test_expired_entry_is_evicted_on_read(self):
        clk = _Clock()
        c = TtlLruCache[str, int](ttl_s=10, max_size=10, time_fn=clk)
        c.set("a", 1)
        clk.now = 20
        assert c.get("a") is None
        assert len(c) == 0  # stale entry dropped, not carried

    def test_bounded_lru_eviction(self):
        c = TtlLruCache[int, int](ttl_s=1000, max_size=3)
        for i in range(5):
            c.set(i, i)
        assert len(c) == 3
        assert c.get(0) is None and c.get(1) is None  # evicted (LRU)
        assert c.get(4) == 4 and c.get(3) == 3 and c.get(2) == 2

    def test_get_refreshes_lru_position(self):
        c = TtlLruCache[str, int](ttl_s=1000, max_size=2)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")  # touch 'a' → now MRU, 'b' is LRU
        c.set("c", 3)  # evicts LRU ('b')
        assert c.get("a") == 1
        assert c.get("b") is None
        assert c.get("c") == 3

    def test_set_refreshes_ttl(self):
        clk = _Clock()
        c = TtlLruCache[str, int](ttl_s=10, max_size=10, time_fn=clk)
        c.set("a", 1)
        clk.now = 8
        c.set("a", 2)  # re-set resets the timestamp
        clk.now = 16  # 8s since re-set — still fresh
        assert c.get("a") == 2

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            TtlLruCache[str, int](ttl_s=0, max_size=10)
        with pytest.raises(ValueError):
            TtlLruCache[str, int](ttl_s=10, max_size=0)


@pytest.mark.unit
class TestOnEvict:
    def _cache(self, now, evicted, **kwargs):
        return TtlLruCache(ttl_s=10, max_size=2, time_fn=lambda: now[0], on_evict=evicted.append, **kwargs)

    def test_lru_eviction_reports_the_dropped_value(self):
        evicted: list[str] = []
        cache = self._cache([0.0], evicted)
        cache.set("a", "A")
        cache.set("b", "B")
        cache.set("c", "C")
        assert evicted == ["A"]

    def test_expiry_on_read_reports_the_dropped_value(self):
        now, evicted = [0.0], []
        cache = self._cache(now, evicted)
        cache.set("a", "A")
        now[0] = 11.0
        assert cache.get("a") is None
        assert evicted == ["A"]

    def test_replacing_a_key_reports_the_old_value_only_when_it_changes(self):
        evicted: list[str] = []
        cache = self._cache([0.0], evicted)
        cache.set("a", "A1")
        cache.set("a", "A1")
        cache.set("a", "A2")
        assert evicted == ["A1"]

    def test_clear_returns_values_without_reporting_them(self):
        evicted: list[str] = []
        cache = self._cache([0.0], evicted)
        cache.set("a", "A")
        assert cache.clear() == ["A"]
        assert evicted == []
        assert len(cache) == 0
