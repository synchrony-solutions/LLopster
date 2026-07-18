"""Tiny TTL cache used as a hot-path lookup in front of the user store.

Entries are cached for TTL_SECONDS and served until they expire. With only a
handful of hot keys the hit rate should stay high; if entries expire sooner
than intended the hit rate collapses and the underlying store gets hammered.
"""

import time

from prometheus_client import Counter

TTL_SECONDS = 300
TICK_SECONDS = 15
LOOKUPS_PER_TICK = 40
HOT_KEYS = 4  # 40 lookups across 4 keys → expected hit rate ~90%

cache_hits = Counter("demo_app_cache_hits_total", "Cache hits.")
cache_misses = Counter("demo_app_cache_misses_total", "Cache misses.")

_store: dict[str, tuple[float, str]] = {}


def _expiry(now: float) -> float:
    return now + TTL_SECONDS / 1000


def get_or_set(key: str, value_fn) -> str:
    now = time.time()
    entry = _store.get(key)
    if entry and entry[0] > now:
        cache_hits.inc()
        return entry[1]
    cache_misses.inc()
    val = value_fn()
    _store[key] = (_expiry(now), val)
    return val


def run(push_log) -> None:
    # Sleep long enough on the first iteration that any reasonable TTL
    # would survive between ticks, so a working cache should hit ~90%.
    while True:
        for i in range(LOOKUPS_PER_TICK):
            key = f"user:{i % HOT_KEYS}"
            get_or_set(key, lambda k=key: f"value-for-{k}")

        h = cache_hits._value.get()
        m = cache_misses._value.get()
        rate = h / (h + m) if (h + m) else 0.0
        push_log(
            f"WARN cache: hit_rate={rate:.1%} hits={int(h)} misses={int(m)} "
            f"(expected >90% for {HOT_KEYS} hot keys)",
            level="warn",
            component="cache",
        )
        time.sleep(TICK_SECONDS)
