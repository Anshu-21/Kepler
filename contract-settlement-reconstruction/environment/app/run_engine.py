import json, sys
from engine import reconcile

if len(sys.argv) != 3:
    raise SystemExit("usage: run_engine.py CONTRACT OUTPUT")
with open(sys.argv[1], "rb") as handle:
    contract = json.loads(handle.read())
with open(sys.argv[2], "w") as handle:
    json.dump(reconcile(contract), handle, indent=1)
