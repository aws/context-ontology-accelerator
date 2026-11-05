# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded, TTL-expiring LRU cache.

A tiny self-contained cache that owns BOTH its expiry (TTL) and its size bound
(LRU eviction), so callers never handle timestamps or eviction themselves —
``get`` returns ``None`` for an absent or expired entry, ``set`` inserts and
evicts the least-recently-used entry past ``max_size``.

This exists to replace the hand-rolled ``OrderedDict`` + inline
``time.monotonic() - ts < TTL`` pattern duplicated across the serve clients, so
the expiry policy lives inside the cache instead of at every call site.

Not thread-safe on its own — the serve runtime is single-threaded asyncio and
each ``get``/``set`` is synchronous (no ``await`` between the OrderedDict ops),
so accesses are atomic with respect to other coroutines. Guard with a lock only
if shared across threads.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class TtlLruCache[K, V]:
    """A cache bounded by ``max_size`` (LRU) with per-entry ``ttl_s`` expiry.

    Args:
        ttl_s: Seconds an entry stays fresh. Reads after this return ``None``.
        max_size: Maximum entries retained; inserting past this evicts the
            least-recently-used entry.
        time_fn: Monotonic clock, injectable for tests. Defaults to
            ``time.monotonic``.
        on_evict: Called with each value the cache drops — expired on read,
            LRU-evicted, or replaced by a ``set`` of the same key — so a value that
            owns a resource (a pooled HTTP client) can release it.
    """

    def __init__(
        self,
        ttl_s: float,
        max_size: int,
        time_fn: Callable[[], float] = time.monotonic,
        on_evict: Callable[[V], None] | None = None,
    ) -> None:
        """Initialize the cache with a freshness window, size bound, and clock.

        Args:
            ttl_s: Seconds an entry stays fresh; must be positive.
            max_size: Maximum entries retained; must be positive.
            time_fn: Monotonic clock, injectable for tests.
            on_evict: Optional callback receiving every dropped value.

        Raises:
            ValueError: If ``ttl_s`` or ``max_size`` is not positive.
        """
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._ttl_s = ttl_s
        self._max_size = max_size
        self._time_fn = time_fn
        self._on_evict = on_evict
        self._store: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def _evicted(self, value: V) -> None:
        if self._on_evict is not None:
            self._on_evict(value)

    def get(self, key: K) -> V | None:
        """Return the cached value, or ``None`` if absent or expired.

        A fresh hit is LRU-touched (moved to most-recently-used). An expired
        entry is removed so it is not carried indefinitely.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        value, ts = entry
        if self._time_fn() - ts >= self._ttl_s:
            del self._store[key]  # drop the stale entry
            self._evicted(value)
            return None
        self._store.move_to_end(key)  # LRU touch
        return value

    def set(self, key: K, value: V) -> None:
        """Insert/refresh ``key`` and evict the least-recently-used entry if over size."""
        previous = self._store.get(key)
        self._store[key] = (value, self._time_fn())
        self._store.move_to_end(key)
        if previous is not None and previous[0] is not value:
            self._evicted(previous[0])
        while len(self._store) > self._max_size:
            _, (evicted, _) = self._store.popitem(last=False)  # evict least-recently-used
            self._evicted(evicted)

    def clear(self) -> list[V]:
        """Remove every entry WITHOUT calling ``on_evict``; returns the removed values."""
        values = [value for value, _ in self._store.values()]
        self._store.clear()
        return values

    def __len__(self) -> int:
        """Return the number of entries currently held (including any not yet expired)."""
        return len(self._store)
