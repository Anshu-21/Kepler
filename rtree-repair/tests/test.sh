#!/bin/sh
set -u
mkdir -p /logs/verifier
chmod 700 /logs/verifier
reward=0
# The EXIT trap keeps the reward binary if pytest itself dies.
trap 'printf "%s\n" "$reward" > /logs/verifier/reward.txt' EXIT
cd /tests
if /opt/venv/bin/python -m pytest -q -p no:cacheprovider /tests/test_task.py --ctrf /logs/verifier/ctrf.json; then
    reward=1
fi
[ -s /logs/verifier/ctrf.json ] || printf '%s' '{"results":{"tool":{"name":"pytest"},"summary":{"tests":0,"passed":0,"failed":1,"pending":0,"skipped":0,"other":0,"start":0,"stop":0},"tests":[]}}' > /logs/verifier/ctrf.json
printf "%s\n" "$reward" > /logs/verifier/reward.txt
chmod 600 /logs/verifier/reward.txt /logs/verifier/ctrf.json
trap - EXIT
