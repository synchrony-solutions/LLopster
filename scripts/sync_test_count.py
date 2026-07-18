#!/usr/bin/env python
"""Single source of truth for the test-count numbers quoted in the docs.

The count is derived from what pytest actually collects — never hand-typed — so
README / CLAUDE / ROADMAP can't drift from reality (the exact rot a reviewer
flagged: 391 / 419 / 151 quoted three different ways). Each doc marks the number
with an HTML comment sentinel:

    <!--TEST_COUNT-->471<!--/TEST_COUNT--> tests, all passing.

Usage:
    python scripts/sync_test_count.py           # rewrite the docs in place
    python scripts/sync_test_count.py --check    # exit 1 if any doc is stale (CI)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_DOCS = ["README.md"]

_MARKER = re.compile(r"(<!--TEST_COUNT-->)(\d+)(<!--/TEST_COUNT-->)")


def collect_test_count() -> int:
    """Number of tests pytest collects — the authoritative count."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("pytest --collect-only failed; cannot derive test count")
    # Prefer the summary line ("N tests collected"); fall back to counting node ids.
    m = re.search(r"(\d+) tests? collected", result.stdout)
    if m:
        return int(m.group(1))
    return sum(1 for line in result.stdout.splitlines() if "::" in line)


def main() -> int:
    check = "--check" in sys.argv[1:]
    count = collect_test_count()
    stale: list[str] = []
    for rel in TARGET_DOCS:
        path = REPO_ROOT / rel
        text = path.read_text()
        if not _MARKER.search(text):
            continue  # doc doesn't reference the count — nothing to sync
        new_text = _MARKER.sub(rf"\g<1>{count}\g<3>", text)
        if new_text != text:
            stale.append(rel)
            if not check:
                path.write_text(new_text)

    if check and stale:
        sys.stderr.write(
            f"Test count is {count} but these docs are stale: {', '.join(stale)}.\n"
            "Run `python scripts/sync_test_count.py` and commit the result.\n"
        )
        return 1
    action = "would update" if check else "updated"
    print(f"test count = {count}; {action if stale else 'all docs in sync'}"
          + (f": {', '.join(stale)}" if stale else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
