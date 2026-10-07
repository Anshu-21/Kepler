"""Answer `SELECT * FROM table` as of a past instant from a base backup and its WAL archive.

    python3 rewind.py DATA_DIR < queries.json      (see /app/INCIDENT.md)

Skeleton from the incident call: the command-line plumbing works, the rest does not exist yet.
"""
import json
import sys


def rewind(data_dir, table, at):
    """Return {"columns": [...], "rows": [[...], ...]} for SELECT * FROM public.<table> at instant `at`."""
    raise NotImplementedError("rewind is not written yet")


def main():
    data_dir = sys.argv[1]
    for q in json.load(sys.stdin):
        print(json.dumps(rewind(data_dir, q["table"], q["at"]), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
