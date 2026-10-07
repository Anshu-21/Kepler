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
    "dist_nested", "moving", "drain", "tamper", "perf",
]


@pytest.fixture(scope="module")
def scratch():
    d = tempfile.mkdtemp(prefix="rt-", dir=os.environ.get("RTREE_SCRATCH", "/candidate-tmp"))
    os.chmod(d, 0o755)
    shutil.copy(CANDIDATE, os.path.join(d, "rtree.js"))
    shutil.copy(HARNESS, os.path.join(d, "harness.js"))
    for f in ("rtree.js", "harness.js"):
        os.chmod(os.path.join(d, f), 0o644)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run(scratch, name):
    cmd = ["node", "--max-old-space-size=1500", os.path.join(scratch, "harness.js"), os.path.join(scratch, "rtree.js"), name]
    if os.geteuid() == 0:
        cmd = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs"] + cmd
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=scratch, env={"PATH": os.environ["PATH"], "HOME": scratch})
    lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
    assert lines, f"no result from harness: {p.stderr[-500:]}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario(scratch, name):
    res = run(scratch, name)
    assert res["ok"], res.get("error")
