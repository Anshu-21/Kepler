# Answering "what did the order tables say at time T?"

## Background

The shop database runs on PostgreSQL 16.15. Its setup:

* x86-64, 8 kB pages, no data checksums, database encoding UTF8.
* `wal_level = replica`, `full_page_writes = on`, `track_commit_timestamp = off`.
* The cluster was created with 1 MB WAL segments.
* `wal_compression` has been changed several times, between `pglz`, `lz4` and `off`.
* The WAL archive we kept starts with a segment in which a checkpoint was taken while the primary was idle: no transaction was open or prepared at that moment. Every segment written since then, on any timeline, is in the archive.
* The only base backup we have was taken with `pg_basebackup` at the very end of the history below, on the last timeline, while one transaction was still open and another was prepared. The older backup that the restores used is lost.

Three tables matter: `public.orders`, `public.order_items` and `public.shipments`. `shipments` is partitioned by range on `shipped_on`, and one of its partitions is itself partitioned by list on `carrier`. Since the archive starts, their history has been messy:

* Columns were added and dropped, some with defaults. One column has an enum type whose values were later renamed and extended. Another has a domain type, and there are `interval` and `float8` columns.
* Rows were upserted, copied in with `COPY`, locked, updated, moved between partitions, and deleted.
* Partitions were created, attached and detached. Tables that had been built separately were attached as partitions; one of them has its own column order.
* `orders` was rewritten once with `VACUUM FULL`, early on timeline 1, and `order_items` once with `ALTER TABLE ... ALTER COLUMN ... TYPE`, on timeline 3 only.
* `order_items` also holds the items of long-closed orders, which nothing touched again after they were loaded.
* Some transactions used savepoints or two-phase commit.
* Bad jobs rewrote and deleted rows, and `VACUUM` then removed the old row versions from the heap.
* Bad jobs were twice undone with a point-in-time restore from the older backup.
  * The first restore went back to a point on timeline 1 and became timeline 2.
  * Later, timeline 2 was abandoned too. The second restore went back to a later point on timeline 1 and became timeline 3.
  * Each restored server took a checkpoint as soon as it was promoted, before doing any work.
  * The WAL of every abandoned branch is still in the archive.

Auditors now ask what `SELECT *` returned on the primary at many past instants. Restoring a server for each instant is too slow, and the audit box has no PostgreSQL. We need a tool that answers straight from the backup and the archive.

## What a dataset directory holds

| path | content |
| --- | --- |
| `backup/backup_label` | the label `pg_basebackup` wrote for the final backup |
| `backup/base/<db>/` | from that backup, `pg_filenode.map` and the main fork of these relations: `pg_class`, `pg_attribute`, `pg_namespace`, `pg_type`, `pg_enum` and `pg_inherits`, and every table and TOAST table that is, or at some point on the last timeline was, part of the three tables. Nothing else from the database was kept. |
| `backup/pg_xact/`, `backup/pg_multixact/` | those directories as they were in that backup |
| `wal/` | the archive, from the segment of the idle checkpoint onward: the segments of every timeline and the timeline history files |

Files that were dropped before the final backup, such as the ones a rewrite replaced, are gone. A relation that only ever existed on an abandoned timeline exists only in the WAL.

## What the tool returns

`/app/rewind.py` is run as

```
python3 rewind.py DATA_DIR < queries.json
```

`queries.json` is a JSON list such as `[{"table": "orders", "at": "2026-10-07 11:57:02.52+00"}, ...]`. For each query, in order, the tool prints one line holding a JSON object:

```json
{"columns": ["order_id", "customer", "..."], "rows": [["1003", "5f0c...", null, "..."], ...]}
```

`table` is `orders`, `order_items` or `shipments`, in schema `public`. `at` is `YYYY-MM-DD HH:MM:SS[.ffffff]+00`, in UTC. Each answer must be exactly what `SELECT * FROM public.<table>` returned on the primary at that instant:

* `columns` are the table's columns as defined at that instant, in `attnum` order.
* `rows` holds every row that query returned. For `shipments`, that means the rows of every partition attached at that instant. Row order does not matter; duplicates do.
* The primary at an instant is whichever server was live then. Timeline 1 was live until the first commit on timeline 2. Timeline 2 was then live until the first commit on timeline 3, and timeline 3 from then on.
* Every instant asked about is after the archive's first checkpoint and before the final backup started. No transaction commits exactly at an asked instant.

Each value is the column's text output in a session with `TimeZone = 'UTC'`, `DateStyle = 'ISO'`, `IntervalStyle = 'postgres'` and `extra_float_digits = 1`, i.e. `value::text`. SQL NULL is `null`.

| type | example |
| --- | --- |
| `int2`, `int4`, `int8` | `"-42"` |
| `bool` | `"true"`, `"false"` |
| `text`, `varchar` | the string itself |
| `numeric` | `"1250.50"`, `"-0.000120"`, `"NaN"` (always the stored display scale, never an exponent) |
| `float8` | `"0.1"`, `"1e+15"`, `"-2.5e-07"`, `"-5.5756665098698096e+16"`, `"NaN"`, `"-Infinity"` (the output of `src/common/d2s.c` as PostgreSQL builds it, which is not always the same as Python's `repr`: that value's `repr` is `-5.57566650986981e+16`) |
| `timestamptz` | `"2026-03-04 05:06:07.25+00"`, `"2026-03-04 05:06:07+00"` |
| `date` | `"2026-03-04"` |
| `interval` | `"-1 years -2 mons +3 days -04:05:06.789"`, `"36:00:00"` |
| `uuid` | `"0c9e3c6a-7d1f-4f3a-9b52-1e0d6c2a8f44"` |
| enums | the label |
| domains | the text of the underlying type |

Only the Python 3.13 standard library is available.

## Material in this directory

* `datasets/staging/` is a staging cluster that went through the same kind of history. `datasets/staging-checks.json` holds what `SELECT * FROM orders` and `SELECT * FROM shipments` really returned there at two instants, captured live at the time.
* `datasets/prod/` is the production copy. Nobody knows its answers.
* `reference/postgresql-16/` has the relevant PostgreSQL 16 sources (PostgreSQL License, see `COPYRIGHT`).
* `reference/lz4-block-format.md` describes LZ4 blocks.
* `python3 /app/run_rewind.py DATASET TABLE 'AT'` prints one answer, formatted for reading.
