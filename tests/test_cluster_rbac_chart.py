"""Chart-level guard for the cluster-context RBAC.

The real gate is `scripts/check_cluster_rbac.py` in helm CI, where helm is
guaranteed present. This runs the same checker locally so a chart edit fails
in `pytest` too, for anyone who has helm installed.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm not installed (covered by helm CI)"
)


def test_generated_cluster_rbac_is_read_only_and_secret_free():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_cluster_rbac.py"),
         str(REPO_ROOT / "helm-chart")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
