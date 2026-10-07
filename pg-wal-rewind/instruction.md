Auditors need to know what our `orders`, `order_items` and `shipments` tables returned at many past instants. That history is gone from the live database. Bad jobs rewrote rows, VACUUM cleaned up after them, two point-in-time restores forked the cluster onto new timelines, and along the way columns and partitions came and went. What we still have is the WAL archive and one base backup, taken at the very end of all that. `/app/INCIDENT.md` describes what each dataset contains and exactly what an answer must look like.

Write `/app/rewind.py`, a tool that answers "what did `SELECT * FROM table` return at instant T" straight from a dataset's backup and archive. It reads its questions from stdin and prints one answer per line, as INCIDENT.md specifies. The current file is a skeleton.

`/app/datasets/staging` comes with real `SELECT *` results for `orders` and `shipments` at two early instants in `/app/datasets/staging-checks.json`. `/app/datasets/prod` has no answers. `python3 /app/run_rewind.py DATASET TABLE 'AT'` prints a single answer. Nothing on the box runs PostgreSQL. `/app/reference` has the PostgreSQL 16 sources you may want, plus a description of LZ4 blocks.

The grader runs your tool on five other datasets built the same way, with about eighty questions each, spread over the whole history. Every answer has to match what the live server returned at that instant, row for row, as text. Each dataset gets one fresh `python3 -I` process with 2 CPUs, 2 GB of memory and 180 seconds for all of its questions. Only `/app/rewind.py` is collected, and it may use only the Python standard library.

You have 10800 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
