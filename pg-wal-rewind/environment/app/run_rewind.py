"""python3 /app/run_rewind.py DATASET TABLE 'YYYY-MM-DD HH:MM:SS.ffffff+00'

Runs /app/rewind.py the way the grader does, for one question, and pretty-prints the answer.
"""
import json
import subprocess
import sys
import time

query = json.dumps([{"table": sys.argv[2], "at": sys.argv[3]}])
start = time.perf_counter()
proc = subprocess.run([sys.executable, "-I", "/app/rewind.py", sys.argv[1]], input=query, capture_output=True,
                      text=True)
sys.stderr.write(proc.stderr)
if proc.returncode != 0:
    sys.exit(proc.returncode)
answer = json.loads(proc.stdout.splitlines()[0])
print(json.dumps(answer, indent=1, ensure_ascii=False))
print(f"{len(answer['rows'])} rows in {time.perf_counter() - start:.2f}s", file=sys.stderr)
