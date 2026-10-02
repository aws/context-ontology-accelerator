// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
package dev.coa.databricks.config;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Supplier;

/**
 * A per-container map whose entries expire, with each deadline <b>jittered</b>.
 *
 * <p>The <b>TTL</b> is why a rotated credential or a corrected parameter takes effect without waiting for
 * containers to recycle. The <b>jitter</b> is because discovery is a per-table fan-out: a schema's worth
 * of containers filling their caches in the same second would expire them in the same second too, and
 * arrive together at Parameter Store's 40 TPS, at STS, or at the warehouse. A ±20% spread breaks the
 * alignment.
 *
 * <p>Nothing evicts on expiry, only on the next write for that key — a Lambda container's key space is its
 * own tenant set, not unbounded.
 *
 * <p>Thread-safe. A benign race loads twice and discards the loser.
 */
public final class ExpiringCache<K, V>
{
    private final long ttlMillis;
    private final ConcurrentMap<K, Entry<V>> entries = new ConcurrentHashMap<>();

    /**
     * @param ttlMillis entry lifetime before jitter. Zero disables the cache.
     */
    public ExpiringCache(long ttlMillis)
    {
        if (ttlMillis < 0) {
            throw new IllegalArgumentException("ttlMillis must not be negative");
        }
        this.ttlMillis = ttlMillis;
    }

    /** The value held for {@code key} while it is fresh, or null. */
    public V get(K key)
    {
        Entry<V> entry = entries.get(key);
        if (entry == null || System.currentTimeMillis() >= entry.expiresAtMillis) {
            return null;
        }
        return entry.value;
    }

    /** The value held for {@code key} while it is fresh, or {@code load}'s answer, cached. */
    public V get(K key, Supplier<V> load)
    {
        V fresh = get(key);
        if (fresh != null) {
            return fresh;
        }
        // Not computeIfAbsent: every caller loads over the network, and that would hold a bin lock across
        // the round trip.
        V loaded = load.get();
        put(key, loaded);
        return loaded;
    }

    public void put(K key, V value)
    {
        entries.put(key, new Entry<>(value, System.currentTimeMillis() + jittered(ttlMillis)));
    }

    /** Drops {@code key}, for a caller that has decided a cached entry must not be served again. */
    public void remove(K key)
    {
        entries.remove(key);
    }

    /** {@code ttl} scattered by up to ±20%. Zero stays zero, which disables the cache. */
    private static long jittered(long ttl)
    {
        if (ttl == 0) {
            return 0;
        }
        long spread = Math.max(1L, ttl / 5L);
        return ttl - spread + ThreadLocalRandom.current().nextLong(2L * spread);
    }

    private static final class Entry<V>
    {
        private final V value;
        private final long expiresAtMillis;

        private Entry(V value, long expiresAtMillis)
        {
            this.value = value;
            this.expiresAtMillis = expiresAtMillis;
        }
    }
}
