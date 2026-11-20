"""Runs the Grafana relay Worker's node:test suite (deploy/grafana-relay, AKKIA-28)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

RELAY_TESTS = Path(__file__).resolve().parents[1] / "deploy" / "grafana-relay" / "worker.test.mjs"


def test_grafana_relay_worker() -> None:
    node = shutil.which("node")
    assert node, "node (>= 20) is required to test deploy/grafana-relay"
    proc = subprocess.run(
        [node, "--test", str(RELAY_TESTS)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
