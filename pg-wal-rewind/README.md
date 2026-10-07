# pg-wal-rewind

The difficulty, solution and verification are explained in `task.toml`. This file only maps the bundle.

| path | role |
| --- | --- |
| `environment/app/rewind.py` | skeleton of the tool the agent writes (the only collected artifact) |
| `environment/app/INCIDENT.md` | the background, what a dataset holds and the exact output contract |
| `environment/app/run_rewind.py` | runs the tool for one question, the way the grader does |
| `environment/app/datasets/staging`, `staging-checks.json` | a practice dataset, with live `SELECT *` results at two instants |
| `environment/app/datasets/prod` | a practice dataset without answers |
| `environment/app/reference/` | PostgreSQL 16.15 sources (PostgreSQL License) and notes on the LZ4 block and Zstandard frame formats |
| `tests/datasets/incident-1..5` | the sealed datasets: a base-backup subset and the WAL archive of all three timelines |
| `tests/expected/incident-*.json` | per query: instant, table, columns, row count and sorted row hashes of the live result |
| `tests/generator/make_dataset.py` | the workload that produced every dataset from a PostgreSQL 16.15 server (not run by the verifier) |
| `tests/test_task.py`, `tests/test.sh` | run the tool unprivileged as `python3 -I rewind.py DATA_DIR` and compare its JSON output |
| `solution/rewind.py` | reference solution |

To regenerate a dataset, run `python3 tests/generator/make_dataset.py SEED OUT_DIR SOCKET_DIR PGDATA` as root, with PostgreSQL 16 (built with lz4 and zstd) and psycopg2 installed. The server runs as user `claude`; change `user=` in the script to use another account. Then rebuild `tests/expected` from `OUT_DIR.truth.json`: one query per snapshot and table, with the sorted 16-hex-digit SHA-256 prefixes of the rows as `tests/test_task.py` computes them. `staging-checks.json` holds the full `orders` and `shipments` rows of the staging run's `after-backup` and first phase-A snapshots. Seeds used: staging 801, prod 802, incidents 901 to 905.
