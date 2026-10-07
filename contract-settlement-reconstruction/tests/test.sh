#!/bin/sh
set +e
mkdir -p /logs/verifier /sandbox
chown root:root /logs/verifier /sandbox
chmod 700 /logs/verifier
chmod 711 /sandbox
chmod -R 700 /tests
chmod -R a-w /app
pytest -q -x -p no:cacheprovider /tests/test_task.py --ctrf /logs/verifier/ctrf.json
code=$?
if [ "$code" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
[ -s /logs/verifier/ctrf.json ] || echo '{"results":{"tool":{"name":"pytest"},"summary":{"tests":0,"passed":0,"failed":1,"pending":0,"skipped":0,"other":0,"start":0,"stop":0},"tests":[]}}' > /logs/verifier/ctrf.json
chmod 600 /logs/verifier/reward.txt /logs/verifier/ctrf.json
exit 0
