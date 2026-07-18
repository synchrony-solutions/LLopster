"""Mock database connection pool.

Serves the app's query workload: roughly 5 concurrent queries at a time, each
holding a connection for a few seconds before releasing it. A query that can't
acquire a connection is rejected and counted so an alert can watch the reject
rate.
"""

import threading
import time

from prometheus_client import Counter

# Pool sizing and workload shape.
MAX_CONNECTIONS = 2
CONCURRENT_QUERIES = 5
QUERY_DURATION_SECONDS = 5
TICK_SECONDS = 15

pool_exhausted = Counter(
    "demo_app_db_pool_exhausted_total",
    "Number of queries rejected because no connection was available.",
)

_sem = threading.Semaphore(MAX_CONNECTIONS)


def _query(push_log) -> None:
    if not _sem.acquire(blocking=False):
        pool_exhausted.inc()
        push_log(
            f"ERROR db_pool: connection pool exhausted (max={MAX_CONNECTIONS}); "
            f"rejecting query — concurrent demand exceeds pool capacity",
            level="error",
            component="db_pool",
        )
        return
    try:
        time.sleep(QUERY_DURATION_SECONDS)
    finally:
        _sem.release()


def run(push_log) -> None:
    while True:
        for _ in range(CONCURRENT_QUERIES):
            threading.Thread(target=_query, args=(push_log,), daemon=True).start()
        time.sleep(TICK_SECONDS)
