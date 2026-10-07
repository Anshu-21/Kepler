import json
import os
import shutil
import subprocess
import tempfile

import pytest

CANDIDATE = os.environ.get("RTREE_FILE", "/app/src/rtree.js")
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness.js")
SCENARIOS = [
    "contract", "sequence", "dist_grid", "dist_clusters", "dist_diagonal",
    "dist_nested", "moving", "drain", "zorder", "tamper", "perf", "perf_hit",
]


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario(name):
    scratch = tempfile.mkdtemp(prefix="rt-", dir=os.environ.get("RTREE_SCRATCH", "/candidate-tmp"))
    try:
        p = subprocess.run(
            ["node", "--max-old-space-size=3000", HARNESS, name, CANDIDATE, scratch],
            capture_output=True, text=True, timeout=700,
        )
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        assert lines, f"harness produced no result: {p.stderr[-500:]}"
        res = json.loads(lines[-1])
        assert res["ok"], res.get("error")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
