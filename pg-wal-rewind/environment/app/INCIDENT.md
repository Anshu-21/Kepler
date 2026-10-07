# Answering "what did the orders tables say at time T?"

## Background

The shop database runs on PostgreSQL 16.15 (x86-64, 8 kB pages, no data checksums, `wal_level = replica`, `full_page_writes = on`, `track_commit_timestamp = off`). A base backup is taken with `pg_basebackup`, and every WAL segment since then is archived. The cluster was created with 1 MB WAL segments. `wal_compression` has been changed several times (`pglz`, `lz4`, `off`).

Since the backup, the history of `public.orders` and `public.order_items` has been messy:

* Columns were added and dropped, some with defaults.
* Rows were upserted, copied in with `COPY`, locked, updated, and deleted.
* Some transactions used savepoints or two-phase commit.
* A bad job rewrote and deleted rows, and `VACUUM` then removed the old row versions from the heap.
* Earlier, a different bad job had been undone with a point-in-time restore from this same backup. The restored server branched onto timeline 2 and kept going. The old timeline's WAL from after the branch point is still in the archive.

Auditors now ask what `SELECT *` returned on the primary at many past instants. Restoring a server for each instant is too slow, and the audit box has no PostgreSQL. We need a tool that answers straight from the backup and the archive.

## What a dataset directory holds

| path | content |
| --- | --- |
| `backup/backup_label` | the label `pg_basebackup` wrote |
| `backup/base/<db>/` | from the backup, the main fork of `pg_class`, `pg_attribute` and `pg_namespace`, of both tables and of their TOAST tables, plus `pg_filenode.map`; nothing else from the database was kept |
| `backup/pg_xact/`, `backup/pg_multixact/` | those directories as they were in the backup |
| `wal/` | the archive from the backup's start segment onward: segments of both timelines and `00000002.history` |

WAL records that touch relations not in `backup/base/<db>/` can be ignored.

## What the tool returns

`/app/rewind.py` is run as

```
python3 rewind.py DATA_DIR < queries.json
```

`queries.json` is a JSON list such as `[{"table": "orders", "at": "2026-10-07 11:57:02.52+00"}, ...]`. For each query, in order, the tool prints one line holding a JSON object:

```json
{"columns": ["order_id", "customer", "..."], "rows": [["1003", "5f0c...", null, "..."], ...]}
```

`table` is `orders` or `order_items` in schema `public`. `at` is `YYYY-MM-DD HH:MM:SS[.ffffff]+00`, in UTC. Each answer must be exactly what `SELECT * FROM public.<table>` returned on the primary at that instant:

* `columns` are the table's columns as defined at that instant, in `attnum` order.
* `rows` holds every row that query returned. Row order does not matter; duplicates do.
* The primary at an instant is whichever server was live then. Timeline 1 was live until the first commit on timeline 2. From that commit on, timeline 2 was live.
* Every instant asked about is after the base backup completed. No transaction commits exactly at an asked instant.

Each value is the column's text output in a session with `TimeZone = 'UTC'` and `DateStyle = 'ISO'`, i.e. `value::text`. SQL NULL is `null`.

| type | example |
| --- | --- |
| `int2`, `int4`, `int8` | `"-42"` |
| `bool` | `"true"`, `"false"` |
| `text`, `varchar` | the string itself |
| `numeric` | `"1250.50"`, `"-0.000120"`, `"NaN"` (always the stored display scale, never an exponent) |
| `timestamptz` | `"2026-03-04 05:06:07.25+00"`, `"2026-03-04 05:06:07+00"` |
| `date` | `"2026-03-04"` |
| `uuid` | `"0c9e3c6a-7d1f-4f3a-9b52-1e0d6c2a8f44"` |

Only the Python 3.13 standard library is available.

## Material in this directory

* `datasets/staging/` is a staging cluster that went through the same kind of history. `datasets/staging-checks.json` holds what `SELECT *` really returned there at two instants, captured live at the time.
* `datasets/prod/` is the production copy. Nobody knows its answers.
* `reference/postgresql-16/` has the relevant PostgreSQL 16 sources (PostgreSQL License, see `COPYRIGHT`). `reference/lz4-block-format.md` describes LZ4 blocks.
* `python3 /app/run_rewind.py DATASET TABLE 'AT'` prints one answer, formatted for reading.
