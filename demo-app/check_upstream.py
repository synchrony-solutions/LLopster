"""Simulated downstream HTTP call.

Wraps a call to a downstream API with a per-request timeout and a retry budget.
Upstream latency sits around 500ms; calls that exceed TIMEOUT_SECONDS time out,
increment a metric, and consume the retry budget.
"""

import time

from prometheus_client import Counter

# Per-request HTTP timeout for the downstream API.
TIMEOUT_SECONDS = 0.001
UPSTREAM_LATENCY_SECONDS = 0.5
MAX_RETRIES = 5
TICK_SECONDS = 30

upstream_timeouts = Counter(
    "demo_app_upstream_timeout_total",
    "Upstream HTTP calls that timed out before responding.",
)


def _call_upstream() -> None:
    """Block until the upstream 'responds' or the timeout elapses."""
    start = time.monotonic()
    # Simulate the upstream by sleeping for either its real latency or
    # just past the configured timeout, whichever is shorter.
    time.sleep(min(UPSTREAM_LATENCY_SECONDS, TIMEOUT_SECONDS + 0.001))
    elapsed = time.monotonic() - start
    if elapsed > TIMEOUT_SECONDS:
        raise TimeoutError(
            f"upstream took {elapsed * 1000:.1f}ms "
            f"(timeout={TIMEOUT_SECONDS * 1000:.1f}ms)"
        )


def run(push_log) -> None:
    while True:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _call_upstream()
                break
            except TimeoutError as e:
                upstream_timeouts.inc()
                push_log(
                    f"ERROR upstream: {e}; retry {attempt}/{MAX_RETRIES}",
                    level="error",
                    component="upstream",
                )
        time.sleep(TICK_SECONDS)
