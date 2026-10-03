"""Settlement ledger batch worker.

Reads a settlement batch, joins it against the in-memory account-code cache,
and writes the resulting ledger entries. The cache is rebuilt at boot.
"""

import os
import time

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25000"))

# Measured cold-start time for build_account_cache() against the production
# account table. /healthz does not answer until this completes.
CACHE_WARM_SECONDS = 45


def build_account_cache() -> dict[str, dict]:
    """Warm the account-code cache. Blocks until every code is loaded."""
    cache: dict[str, dict] = {}
    for code in _all_account_codes():
        cache[code] = _describe(code)
    time.sleep(CACHE_WARM_SECONDS)
    return cache


def process_batch(rows: list[dict], cache: dict[str, dict]) -> list[dict]:
    entries = []
    for row in rows:
        account = cache.get(row["account_code"])
        entries.append({**row, "account": account, "ledger_ref": _ref(row)})
    return entries


def main() -> None:
    cache = build_account_cache()
    _serve_healthz()
    while True:
        rows = _fetch(limit=BATCH_SIZE)
        if not rows:
            time.sleep(5)
            continue
        _write(process_batch(rows, cache))


def _all_account_codes() -> list[str]: ...
def _describe(code: str) -> dict: ...
def _fetch(limit: int) -> list[dict]: ...
def _ref(row: dict) -> str: ...
def _write(entries: list[dict]) -> None: ...
def _serve_healthz() -> None: ...


if __name__ == "__main__":
    main()
