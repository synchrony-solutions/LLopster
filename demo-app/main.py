"""Demo app for llopster.

Runs several independent 'production-like' checks in parallel. Each check
exercises a real subsystem — config validation, a DB connection pool, a TTL
cache, a downstream HTTP call, and a background heartbeat scheduler — and emits
log lines to Loki plus Prometheus counters/gauges scraped from :8001. Alert
rules in config/prometheus/rules/demo-alerts.yml watch these signals and fire
alerts that the agent webhook then attempts to fix.
"""

import os
import time
from threading import Thread

import httpx
from prometheus_client import start_http_server

import check_cache
import check_db_pool
import check_helm_values
import check_scheduler
import check_upstream

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))


def push_log(line: str, *, level: str = "error", component: str = "app") -> None:
    payload = {
        "streams": [
            {
                "stream": {
                    "service": "demo-app",
                    "level": level,
                    "component": component,
                },
                "values": [[str(time.time_ns()), line]],
            }
        ]
    }
    try:
        httpx.post(f"{LOKI_URL}/loki/api/v1/push", json=payload, timeout=2.0)
    except Exception as e:
        print(f"[warn] failed to push log to loki: {e}", flush=True)


def main() -> None:
    Thread(target=start_http_server, args=(METRICS_PORT,), daemon=True).start()
    print(f"[info] demo-app started; metrics on :{METRICS_PORT}", flush=True)

    workers = [
        Thread(target=check_helm_values.run, args=(push_log,), daemon=True, name="helm_values"),
        Thread(target=check_db_pool.run, args=(push_log,), daemon=True, name="db_pool"),
        Thread(target=check_cache.run, args=(push_log,), daemon=True, name="cache"),
        Thread(target=check_upstream.run, args=(push_log,), daemon=True, name="upstream"),
        Thread(target=check_scheduler.run, args=(push_log,), daemon=True, name="scheduler"),
    ]
    for w in workers:
        w.start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
