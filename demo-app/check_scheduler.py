"""Background heartbeat scheduler.

Fires an internal '/heartbeat' record on a fixed interval (meant to be about
once a minute) so external monitors can confirm the worker is alive. If the
heartbeat stops firing, the age gauge climbs and an alert trips.
"""

import time

from prometheus_client import Counter, Gauge

# How often the heartbeat should fire.
HEARTBEAT_INTERVAL_SECONDS = 86400
TICK_SECONDS = 30
STALE_THRESHOLD_SECONDS = 120

heartbeat_age = Gauge(
    "demo_app_heartbeat_age_seconds",
    "Seconds since the scheduler last fired a heartbeat.",
)
heartbeat_overdue = Counter(
    "demo_app_heartbeat_overdue_total",
    "Number of times the heartbeat was found to be overdue.",
)

_last_fire: float | None = None


def _fire_heartbeat() -> None:
    global _last_fire
    _last_fire = time.monotonic()


def run(push_log) -> None:
    next_fire = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
    process_start = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= next_fire:
            _fire_heartbeat()
            push_log(
                "INFO scheduler: heartbeat fired",
                level="info",
                component="scheduler",
            )
            next_fire = now + HEARTBEAT_INTERVAL_SECONDS

        # If the heartbeat has never fired, age = uptime.
        age = (now - _last_fire) if _last_fire is not None else (now - process_start)
        heartbeat_age.set(age)

        if age > STALE_THRESHOLD_SECONDS:
            heartbeat_overdue.inc()
            push_log(
                f"WARN scheduler: heartbeat overdue (age={age:.0f}s, "
                f"interval={HEARTBEAT_INTERVAL_SECONDS}s); background job not running",
                level="warn",
                component="scheduler",
            )
        time.sleep(TICK_SECONDS)
