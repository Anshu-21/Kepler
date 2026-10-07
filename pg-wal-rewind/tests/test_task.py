"""Runs the collected /app/rewind.py on five sealed datasets and compares its answers with
what the live PostgreSQL server returned at the same instants.

The candidate runs as its own `python3 -I` process (copied into a scratch directory, as an
unprivileged user with bounded resources); this process only reads the JSON it prints.
"""
import ctypes
import glob
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import tempfile

import pytest

DATASETS = sorted(os.path.basename(p) for p in glob.glob("/tests/datasets/*"))
ARTIFACT = "/app/rewind.py"
LIMIT = 180
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/candidate-tmp", "TMPDIR": "/candidate-tmp",
       "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


def sandbox():
    # No privilege recovery, bounded resources, then become nobody.
    libc = ctypes.CDLL(None)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("prctl failed")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (LIMIT, LIMIT))
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)


def row_hash(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def run_dataset(name, queries):
    folder = tempfile.mkdtemp(prefix="rewind-", dir="/candidate-tmp")
    try:
        data = os.path.join(folder, name)
        shutil.copytree(os.path.join("/tests/datasets", name), data)
        tool = os.path.join(folder, "rewind.py")
        shutil.copyfile(ARTIFACT, tool)
        for root, dirs, files in os.walk(folder):
            os.chmod(root, 0o555)
            for f in files:
                os.chmod(os.path.join(root, f), 0o444)
        proc = subprocess.Popen(["python3", "-I", tool, data], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=ENV, cwd="/candidate-tmp",
                                start_new_session=True, preexec_fn=sandbox)
        try:
            out, err = proc.communicate(json.dumps([{"table": q["table"], "at": q["at"]} for q in queries]),
                                        timeout=LIMIT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise AssertionError(f"{name}: exceeded {LIMIT} seconds")
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert proc.returncode == 0, err[-3000:]
        lines = [x for x in out.splitlines() if x.strip()]
        assert len(lines) == len(queries), f"{name}: expected {len(queries)} answers, got {len(lines)}\n{err[-2000:]}"
        return [json.loads(x) for x in lines]
    finally:
        for root, dirs, files in os.walk(folder):
            os.chmod(root, 0o755)
        shutil.rmtree(folder, ignore_errors=True)


def test_artifact_present():
    assert os.path.isfile(ARTIFACT) and not os.path.islink(ARTIFACT)


def test_five_datasets():
    assert DATASETS == [f"incident-{i}" for i in range(1, 6)]


@pytest.mark.parametrize("name", DATASETS)
def test_dataset(name):
    with open(f"/tests/expected/{name}.json") as f:
        queries = json.load(f)["queries"]
    answers = run_dataset(name, queries)
    for q, a in zip(queries, answers):
        where = f"{name} {q['table']} at {q['at']}"
        assert isinstance(a, dict) and isinstance(a.get("rows"), list), f"{where}: malformed answer"
        assert a.get("columns") == q["columns"], f"{where}: columns {a.get('columns')}"
        assert all(isinstance(r, list) and len(r) == len(q["columns"]) and
                   all(v is None or isinstance(v, str) for v in r) for r in a["rows"]), f"{where}: malformed rows"
        got = sorted(row_hash(r) for r in a["rows"])
        if got != q["rows"]:
            want, have = set(q["rows"]), set(got)
            raise AssertionError(f"{where}: {len(got)} rows, expected {q['count']} "
                                 f"({len(have - want)} unexpected, {len(want - have)} missing)")


def test_verifier_boundaries():
    for code in ("open('/tests/expected/incident-1.json').read()", "open('/logs/verifier/reward.txt','w').write('1')"):
        proc = subprocess.run(["python3", "-I", "-c", code], env=ENV, cwd="/candidate-tmp", preexec_fn=sandbox,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        assert proc.returncode != 0
