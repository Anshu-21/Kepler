import json, sys
from engine import reconcile

raw = open(sys.argv[1], "rb").read()
answer = reconcile(json.loads(raw))
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(answer, handle, separators=(",", ":"))
