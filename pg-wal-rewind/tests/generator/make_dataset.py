"""Produce one incident dataset from a real PostgreSQL 16 server.

Usage (as root, PostgreSQL 16 installed, server runs as user `claude`):
    python3 make_dataset.py SEED OUT_DIR SOCKET_DIR PGDATA

Builds a cluster with 1 MB WAL segments and WAL archiving, loads an order schema (two plain
tables and a partitioned one, an enum, a domain, intervals and floats), takes a base backup while the workload keeps
running (used only for the restores), then starts the archive the tool gets at a checkpoint taken while the server is
idle and runs a seeded multi-session workload (DDL, partition attach/detach/create, enum renames, upserts, COPY,
locks that become multixacts, savepoints, two-phase commits, checkpoints, VACUUM, a changing wal_compression), two
point-in-time restores (timeline 2 from timeline 1, then timeline 3 from timeline 1 again; each promoted server takes a
checkpoint before any work), bad bulk jobs and the VACUUM that erases the old row versions. At the end it takes a second
base backup, the one the tool gets. While the workload runs it records the live SELECT * of the three tables at quiet
instants.

Writes OUT_DIR/backup (the subset of the final base backup the tool works from),
OUT_DIR/wal (archived segments from the archive-start checkpoint onward) and
OUT_DIR.truth.json (the captured answers).
"""
import io
import json
import os
import random
import shutil
import subprocess
import sys
import time
import uuid

import psycopg2

SEED, OUT, SOCK, PGDATA = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
PGBIN = "/usr/lib/postgresql/16/bin"
ARCH = PGDATA + ".archive"
BK = PGDATA + ".backup"
rng = random.Random(SEED)
P = dict(
    orders=rng.randint(130, 200), items_per=rng.randint(1, 3), steps=rng.randint(850, 1100),
    sessions=rng.randint(3, 5), big_p=rng.uniform(0.03, 0.06),
    wrap=rng.random() < 0.6,
    compression=rng.sample(["pglz", "lz4", "off"], 3),
)
CTL = os.path.join(PGBIN, "pg_ctl")
TABLES = ("orders", "order_items", "shipments")


def as_pg(*cmd, **kw):
    return subprocess.run(list(cmd), check=True, stdout=subprocess.DEVNULL, user="claude", **kw)


for d in (PGDATA, ARCH, BK):
    if os.path.exists(d):
        shutil.rmtree(d)
os.makedirs(ARCH)
shutil.chown(ARCH, "claude")
as_pg(os.path.join(PGBIN, "initdb"), "-D", PGDATA, "-U", "postgres", "-A", "trust", "--no-sync", "--wal-segsize=1",
      "-E", "UTF8", "--locale=C")
with open(os.path.join(PGDATA, "postgresql.conf"), "a") as f:
    f.write(f"""
autovacuum = off
fsync = off
synchronous_commit = off
listen_addresses = ''
unix_socket_directories = '{SOCK}'
max_prepared_transactions = 10
wal_level = replica
full_page_writes = on
wal_compression = {P['compression'][0]}
archive_mode = on
archive_command = 'cp %p {ARCH}/%f'
checkpoint_timeout = 1h
max_wal_size = 4GB
min_wal_size = 32MB
""")

if P["wrap"]:
    XID0 = (1 << 32) - rng.randint(700, 1100)
else:
    XID0 = 0x100000 * rng.randint(1, 40) - rng.randint(100, 3000)
MULTI0 = 65536 * rng.randint(1, 3) - rng.randint(2, 6)
MOFF0 = 52352 * rng.randint(1, 3) - rng.randint(3, 30)


def zero_segment(directory, segno, pages=32):
    path = os.path.join(PGDATA, directory, "%04X" % segno)
    have = os.path.getsize(path) if os.path.exists(path) else 0
    with open(path, "ab") as f:
        f.write(bytes(max(0, 8192 * pages - have)))
    shutil.chown(path, "claude")


def freeze_all():
    admin = psycopg2.connect(host=SOCK, user="postgres", dbname="postgres")
    admin.autocommit = True
    admin.cursor().execute("UPDATE pg_database SET datallowconn = true WHERE datname = 'template0'")
    for db in ("template0", "template1", "postgres"):
        c = psycopg2.connect(host=SOCK, user="postgres", dbname=db)
        c.autocommit = True
        c.cursor().execute("VACUUM (FREEZE)")
        c.close()
    admin.cursor().execute("UPDATE pg_database SET datallowconn = false WHERE datname = 'template0'")
    admin.close()


def jump(xid, multi=None):
    """Move the xid counter; no unfrozen xid may end up more than 2^31 behind it."""
    zero_segment("pg_xact", xid // 1048576)
    args = ["-f", "-x", str(xid)]
    if multi:
        zero_segment("pg_multixact/offsets", MULTI0 // 65536)
        zero_segment("pg_multixact/members", MOFF0 // 52352)
        args += ["-m", f"{MULTI0},{MULTI0}", "-O", str(MOFF0)]
    as_pg(os.path.join(PGBIN, "pg_resetwal"), *args, PGDATA)
    as_pg(CTL, "-D", PGDATA, "-l", PGDATA + ".log", "-w", "start")
    freeze_all()


if P["wrap"]:
    for stage in (1_400_000_000, 2_800_000_000):
        jump(stage)
        as_pg(CTL, "-D", PGDATA, "-w", "stop")
jump(XID0, multi=True)
admin = psycopg2.connect(host=SOCK, user="postgres", dbname="postgres")
admin.autocommit = True
admin.cursor().execute("CREATE DATABASE shop")


def connect():
    c = psycopg2.connect(host=SOCK, user="postgres", dbname="shop")
    c.cursor().execute("SET TimeZone='UTC'; SET DateStyle='ISO, YMD'; SET lock_timeout='2s'")
    c.commit()
    return c


ddl = connect()
cur = ddl.cursor()
cur.execute("""
CREATE SCHEMA archive;
CREATE TYPE order_stage AS ENUM ('queued', 'picking', 'packed', 'dispatched');
CREATE DOMAIN weight_kg AS numeric(9,3) CHECK (VALUE >= 0);
CREATE TABLE customers (customer uuid PRIMARY KEY, name text);
CREATE TABLE archive.orders (order_id bigint PRIMARY KEY, status text, closed_at timestamptz, notes text);
CREATE TABLE archive.shipments (ship_id bigint, carrier text, note text);
CREATE TABLE orders (
  order_id bigint PRIMARY KEY,
  customer uuid NOT NULL,
  status text NOT NULL,
  total numeric(12,2),
  placed_at timestamptz NOT NULL,
  ship_by date,
  priority smallint,
  gift boolean,
  legacy_code varchar(16),
  notes text,
  manifest text
) WITH (fillfactor = 80);
ALTER TABLE orders ALTER COLUMN manifest SET COMPRESSION lz4;
CREATE TABLE order_items (
  item_id integer PRIMARY KEY,
  order_id bigint NOT NULL,
  sku varchar(24) NOT NULL,
  qty integer NOT NULL,
  unit_price numeric(10,2) NOT NULL,
  discount numeric,
  note text,
  lead_time interval,
  weight weight_kg,
  ratio float8
);
CREATE TABLE shipments (
  ship_id bigint NOT NULL,
  order_id bigint,
  carrier text NOT NULL,
  shipped_on date NOT NULL,
  cost numeric(10,2),
  label text
) PARTITION BY RANGE (shipped_on);
CREATE TABLE shipments_2025 PARTITION OF shipments FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE shipments_2026 PARTITION OF shipments FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')
  PARTITION BY LIST (carrier);
CREATE TABLE shipments_2026_ups PARTITION OF shipments_2026 FOR VALUES IN ('ups');
CREATE TABLE shipments_2026_misc PARTITION OF shipments_2026 DEFAULT;
CREATE TABLE shipments_misc PARTITION OF shipments DEFAULT;
CREATE TABLE shipments_legacy (
  label text,
  junk integer,
  carrier text NOT NULL,
  cost numeric(10,2),
  ship_id bigint NOT NULL,
  shipped_on date NOT NULL,
  order_id bigint
);
ALTER TABLE shipments_legacy DROP COLUMN junk;
CREATE TABLE archive.shipments_2019 (LIKE shipments);
""")
ddl.commit()

WORDS = ("alpha bravo crate delta eagle fjord gamma harbor iris jade kilo lumen mango nectar onyx pivot quartz "
         "raven sable tundra umber vivid willow xenon yarrow zephyr").split()
STATUSES = ["new", "paid", "packed", "shipped", "delivered", "returned", "on_hold"]
CARRIERS = ["ups", "dhl", "fedex", "post"]


def text(n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def blob(kind):
    if kind == "prose":
        return text(rng.randint(300, 700))
    if kind == "noise":
        return "".join(rng.choice("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
                       for _ in range(rng.randint(2050, 2600)))
    if kind == "short-manifest":
        return "\n".join(f"{rng.choice(WORDS)}:{rng.randint(0, 9)}" for _ in range(rng.randint(250, 450)))
    return "\n".join(f"{rng.choice(WORDS)}-{rng.randint(0, 999)}:{rng.randint(0, 99)}" for _ in range(rng.randint(200, 400)))


def money(lo, hi):
    return f"{rng.randint(lo * 100, hi * 100) / 100:.2f}"


def discount():
    r = rng.random()
    if r < 0.15:
        return None
    if r < 0.3:
        return str(rng.randint(-10 ** 12, 10 ** 12))
    if r < 0.45:
        return f"{rng.uniform(-1, 1):.{rng.randint(0, 12)}f}"
    if r < 0.55:
        return "0." + "0" * rng.randint(0, 9) + str(rng.randint(1, 99999))
    if r < 0.65:
        return str(rng.randint(1, 9)) + "0" * rng.randint(4, 30) + "." + "0" * rng.randint(0, 3)
    if r < 0.68:
        return "NaN"
    if r < 0.72:
        return ("-" if rng.random() < 0.5 else "") + "0." + "".join(rng.choice("0123456789") for _ in range(rng.randint(64, 90)))
    if r < 0.74:
        return str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(rng.randint(260, 300)))
    return f"{rng.uniform(0, 500):.{rng.randint(1, 6)}f}"


# ---------------------------------------------------------------- interval, float8 and domain values

def interval_value():
    r = rng.random()
    if r < 0.15:
        return None
    if r < 0.45:
        return rng.choice(["1 day", "00:00:00", "-00:00:00.5", "36 hours", "1.5 days", "2 weeks", "P1Y2M3DT4H5M6S",
                           "-1 year 2 mons -3 days +04:05:06.789", "1 mon -1 day", "-2 mons", "1 year", "3 years 1 mon",
                           "-1 days -02:00:00", "5 days 23:59:59.999999", "100 years", "0.25 seconds", "-7 days 01:00"])
    parts = []
    for unit, lo, hi in (("years", -3, 3), ("mons", -14, 14), ("days", -40, 40)):
        if rng.random() < 0.4:
            parts.append(f"{rng.randint(lo, hi)} {unit}")
    if rng.random() < 0.6:
        sign = "-" if rng.random() < 0.3 else ""
        frac = "" if rng.random() < 0.5 else "." + str(rng.randint(0, 999999)).zfill(6)
        parts.append(f"{sign}{rng.randint(0, 50)}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}{frac}")
    return " ".join(parts) or "0"


def float_value():
    r = rng.random()
    if r < 0.12:
        return None
    if r < 0.25:
        return rng.choice([0.0, -0.0, 1.0, 0.1, 1e15, 1e16, 123456789012345.6, 1e-4, 1e-5, 1.5e-7, -2.5e300,
                           5e-324, 1.7976931348623157e308, 100.0, 1e14, 0.30000000000000004, float("nan"),
                           float("inf"), float("-inf"), 2.0 ** 60, 1 / 3])
    return rng.uniform(-1, 1) * 10 ** rng.randint(-9, 20)


def weight_value():
    if rng.random() < 0.15:
        return None
    return f"{rng.uniform(0, 99999):.{rng.randint(0, 4)}f}"


customers = [str(uuid.UUID(int=rng.getrandbits(128))) for _ in range(60)]
cur.execute("INSERT INTO customers SELECT unnest(%s::uuid[]), 'c'", (customers,))
cur.execute("INSERT INTO archive.orders SELECT g, 'closed', now() - g * interval '1 day', 'archived ' || g "
            "FROM generate_series(1, 40) g")
cur.execute("INSERT INTO archive.shipments SELECT g, 'ups', 'archived ' || g FROM generate_series(1, 25) g")
ddl.commit()

stage_labels = ["queued", "picking", "packed", "dispatched"]


def order_values(oid):
    big = rng.random() < P["big_p"]
    return dict(
        order_id=oid, customer=rng.choice(customers), status=rng.choice(STATUSES[:3]),
        total=None if rng.random() < 0.05 else money(1, 99999),
        placed_at=f"2026-0{rng.randint(1, 9)}-{rng.randint(10, 28)} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:"
                  f"{rng.randint(0, 59):02d}{'' if rng.random() < 0.3 else '.' + str(rng.randint(0, 999999)).zfill(6)}+00",
        ship_by=None if rng.random() < 0.2 else f"{rng.choice([1999, 2026, 2027, 2100])}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        priority=None if rng.random() < 0.1 else rng.randint(-32768, 32767) if rng.random() < 0.1 else rng.randint(0, 9),
        gift=None if rng.random() < 0.2 else rng.random() < 0.5,
        legacy_code=None if rng.random() < 0.3 else text(1)[:rng.randint(1, 16)],
        notes=blob("prose") if big and rng.random() < 0.5 else (None if rng.random() < 0.3 else text(rng.randint(1, 30))),
        manifest=blob(rng.choice(["manifest", "noise"])) if big else (None if rng.random() < 0.5 else text(rng.randint(1, 8))),
        rush=rng.random() < 0.3,
        stage=rng.choice(stage_labels),
    )


order_cols = ["order_id", "customer", "status", "total", "placed_at", "ship_by", "priority", "gift", "legacy_code",
              "notes", "manifest"]
item_cols = ["item_id", "order_id", "sku", "qty", "unit_price", "discount", "note", "lead_time", "weight", "ratio"]
ship_cols = ["ship_id", "order_id", "carrier", "shipped_on", "cost", "label"]
next_order = 1000
next_item = 1
next_ship = 5000


def item_values(oid):
    global next_item
    next_item += 1
    return dict(item_id=next_item, order_id=oid,
                sku=f"SKU-{rng.randint(1, 99999):05d}{rng.choice(['', '-XL', '-blue-ltd'])}",
                qty=rng.randint(-5, 500), unit_price=money(0, 9999), discount=discount(),
                note=blob("prose") if rng.random() < 0.02 else (None if rng.random() < 0.6 else text(rng.randint(1, 12))),
                lead_time=interval_value(), weight=weight_value(), ratio=float_value(), warehouse=None if rng.random() < 0.4 else rng.randint(1, 40))


# partition layout as the workload sees it; refreshed from the catalog after DDL and restores
parts = dict(legacy=False, y2025=True, y2027=False, y2019=False)


def ship_date():
    years = [2026, 2026, 2018, 2031] + ([2025] if parts["y2025"] or rng.random() < 0.5 else []) + \
            ([2019] if parts["y2019"] else []) + \
            ([2027, 2027] if parts["y2027"] else []) + ([2021, 2023, 2024] if parts["legacy"] else [])
    return f"{rng.choice(years)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def ship_values(oid):
    global next_ship
    next_ship += rng.randint(1, 2)
    big = rng.random() < 0.04
    return dict(ship_id=next_ship, order_id=oid, carrier=rng.choice(CARRIERS), shipped_on=ship_date(),
                cost=None if rng.random() < 0.1 else money(0, 900),
                label=None if rng.random() < 0.3 else (blob("prose") if big and rng.random() < 0.3 else text(rng.randint(1, 4))),
                insured=rng.random() < 0.8)


def insert_order(c, with_items=True, upsert=False):
    global next_order
    next_order += rng.randint(1, 3)
    v = order_values(next_order)
    cols = [k for k in order_cols if k in v]
    sql = f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
    if upsert:
        sql += " ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status"
    c.cursor().execute(sql, [v[k] for k in cols])
    if with_items:
        for _ in range(rng.randint(1, P["items_per"] * 2)):
            insert_item(c, next_order)
    return next_order


def insert_item(c, oid):
    v = item_values(oid)
    cols = [k for k in item_cols if k in v]
    c.cursor().execute(f"INSERT INTO order_items ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
                       [v[k] for k in cols])
    return v["item_id"]


def copy_escape(v):
    if v is None:
        return "\\N"
    s = str(v)
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def copy_rows(c, table, cols, rows):
    buf = io.StringIO()
    for v in rows:
        buf.write("\t".join(copy_escape(v[k]) for k in cols) + "\n")
    buf.seek(0)
    c.cursor().copy_expert(f"COPY {table} ({','.join(cols)}) FROM STDIN", buf)


def copy_items(c, oids):
    """COPY a batch of items: the server logs these as multi-insert records."""
    rows = [item_values(oid) for oid in oids for _ in range(rng.randint(1, 3))]
    copy_rows(c, "order_items", [k for k in item_cols], rows)


def insert_shipments(c, oids):
    rows = [ship_values(o) for o in oids]
    cols = [k for k in ship_cols]
    if len(rows) > 2 and rng.random() < 0.6:
        copy_rows(c, "shipments", cols, rows)
    else:
        for v in rows:
            c.cursor().execute(f"INSERT INTO shipments ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
                               [v[k] for k in cols])
    return [v["ship_id"] for v in rows]


# initial load (part of it through COPY), then history before the backup
load = connect()
oids = [insert_order(load, with_items=rng.random() < 0.5) for _ in range(P["orders"])]
copy_items(load, [o for o in oids if rng.random() < 0.5])
# items of long-closed orders: nothing in the workload or the bad jobs touches them again
cold = [dict(item_values(rng.randint(1, 900)), qty=rng.randint(200, 500)) for _ in range(rng.randint(700, 1100))]
copy_rows(load, "order_items", item_cols, cold)
insert_shipments(load, rng.sample(oids, len(oids) // 2))
legacy = [dict(ship_values(o), shipped_on=f"{rng.randint(2020, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
          for o in rng.sample(oids, 25)]
copy_rows(load, "shipments_legacy", ship_cols, legacy)
old = [dict(ship_values(o), shipped_on=f"2019-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}") for o in rng.sample(oids, 15)]
copy_rows(load, "archive.shipments_2019", ship_cols, old)
load.commit()
k = ddl.cursor()
k.execute("ALTER TABLE orders DROP COLUMN legacy_code")
order_cols.remove("legacy_code")
ddl.commit()
ddl.autocommit = True
k.execute("VACUUM FULL orders")           # new relfilenode for orders and its TOAST table
k.execute("VACUUM FULL pg_class")         # mapped catalogs move to new filenodes
k.execute("VACUUM FULL pg_attribute")
k.execute("VACUUM FULL pg_type")
k.execute("VACUUM FULL pg_enum")
k.execute("VACUUM (FREEZE) order_items")
k.execute("SELECT pg_relation_filenode('orders')")
ORDERS_NODE0 = k.fetchone()[0]
ddl.autocommit = False

truth = []
snap = connect()
snap.autocommit = True


def snapshot(label):
    time.sleep(0.004)
    c = snap.cursor()
    names = {}
    for table in TABLES:
        c.execute(f"SELECT * FROM {table} LIMIT 0")
        names[table] = [d[0] for d in c.description]
    sel = [f"(SELECT json_agg(json_build_array({','.join(f'{n}::text' for n in names[t])})) FROM {t})" for t in TABLES]
    c.execute("SELECT statement_timestamp()::text, " + ", ".join(sel))
    at, *res = c.fetchone()
    entry = {"label": label, "at": at}
    for t, rows in zip(TABLES, res):
        entry[t] = {"columns": names[t], "rows": rows or []}
    truth.append(entry)
    time.sleep(0.004)


sessions = [connect() for _ in range(P["sessions"])]
state = [dict(open=False, savepoint=False, ops=0) for _ in sessions]
held = {}       # order_id or ("s", ship_id) -> holder of an exclusive row lock or a modification
keyshared = {}  # order_id -> holders of FOR KEY SHARE
prepared = []   # gids waiting for COMMIT/ROLLBACK PREPARED
gid_seq = [0]


def live_orders():
    k = snap.cursor()
    k.execute("SELECT order_id FROM orders")
    return [r[0] for r in k.fetchall()]


def live_shipments():
    k = snap.cursor()
    k.execute("SELECT ship_id FROM shipments")
    return [r[0] for r in k.fetchall()]


def free(oid, i, exclusive):
    h = held.get(oid)
    if h is not None and h != i:
        return False
    if exclusive and any(s != i for s in keyshared.get(oid, ())):
        return False
    return True


def release(holder):
    for oid in [o for o, h in held.items() if h == holder]:
        del held[oid]
    for holders in keyshared.values():
        holders.discard(holder)


def finish(i, commit):
    c = sessions[i]
    if commit and rng.random() < 0.06 and len(prepared) < 6:
        gid_seq[0] += 1
        gid = f"batch-{SEED}-{gid_seq[0]}"
        c.cursor().execute(f"PREPARE TRANSACTION '{gid}'")
        c.commit()
        for oid in [o for o, h in held.items() if h == i]:
            held[oid] = gid
        for holders in keyshared.values():
            if i in holders:
                holders.discard(i)
                holders.add(gid)
        prepared.append(gid)
    else:
        c.commit() if commit else c.rollback()
        release(i)
    state[i].update(open=False, savepoint=False, ops=0)


def resolve_prepared():
    gid = prepared.pop(rng.randrange(len(prepared)))
    snap.cursor().execute(f"{'COMMIT' if rng.random() < 0.7 else 'ROLLBACK'} PREPARED '{gid}'")
    release(gid)


def finish_all(commit_p):
    for i in range(len(sessions)):
        if state[i]["open"]:
            finish(i, rng.random() < commit_p)


def go_idle():
    """Close every open and prepared transaction."""
    finish_all(0.9)
    while prepared:
        resolve_prepared()


known = live_orders()
known_ship = live_shipments()
long_session = 0
long_until = -1


def step_once(step):
    global known, known_ship
    i = rng.randrange(len(sessions))
    if i == long_session and state[i]["open"] and step < long_until and rng.random() < 0.9:
        return
    if prepared and rng.random() < 0.01:
        resolve_prepared()
        return
    c, st = sessions[i], state[i]
    if st["open"] and st["ops"] >= rng.randint(1, 6) and not (i == long_session and step < long_until):
        finish(i, rng.random() < 0.85)
        return
    st["open"] = True
    k = c.cursor()
    st["ops"] += 1
    if not st["savepoint"] and rng.random() < 0.12:
        k.execute("SAVEPOINT sp")
        st["savepoint"] = True
    r = rng.random()
    try:
        if r < 0.12:
            known.append(insert_order(c, with_items=True, upsert=rng.random() < 0.4))
        elif r < 0.15 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=False):
                k.execute("INSERT INTO orders (order_id, customer, status, placed_at) VALUES (%s, %s, 'dup', now()) "
                          "ON CONFLICT (order_id) DO UPDATE SET status = 'upserted', total = %s",
                          (oid, rng.choice(customers), money(1, 999)))
                held[oid] = i
        elif r < 0.38 and known:
            shared = [o for o, h in keyshared.items() if h - {i}]
            oid = rng.choice(shared) if shared and rng.random() < 0.4 else rng.choice(known)
            if free(oid, i, exclusive=False):
                field = rng.random()
                if field < 0.35:
                    k.execute("UPDATE orders SET status = %s, total = %s WHERE order_id = %s",
                              (rng.choice(STATUSES), money(1, 99999), oid))
                elif field < 0.45:
                    k.execute("UPDATE orders SET notes = %s WHERE order_id = %s", (blob("prose"), oid))
                elif field < 0.55:
                    k.execute("UPDATE orders SET manifest = %s WHERE order_id = %s",
                              (blob(rng.choice(["manifest", "short-manifest", "noise"])), oid))
                elif field < 0.68 and "stage" in order_cols:
                    k.execute("UPDATE orders SET stage = %s WHERE order_id = %s", (rng.choice(stage_labels), oid))
                elif field < 0.84:
                    k.execute("UPDATE orders SET priority = (coalesce(priority, 0) + 1) %% 30000 WHERE order_id = %s",
                              (oid,))
                elif field < 0.93:
                    k.execute("UPDATE order_items SET qty = qty + %s, discount = %s WHERE order_id = %s",
                              (rng.randint(-3, 9), discount(), oid))
                else:
                    k.execute("UPDATE order_items SET weight = %s, lead_time = %s, ratio = %s WHERE order_id = %s",
                              (weight_value(), interval_value(), float_value(), oid))
                held[oid] = i
        elif r < 0.43 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=True):
                k.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
                k.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
                held[oid] = i
        elif r < 0.55 and known:
            pool = known[:30] if rng.random() < 0.6 else known
            for oid in rng.sample(pool, min(len(pool), rng.randint(1, 5))):
                if held.get(oid) not in (None, i):
                    continue
                k.execute("SELECT 1 FROM orders WHERE order_id = %s FOR KEY SHARE", (oid,))
                keyshared.setdefault(oid, set()).add(i)
        elif r < 0.59 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=True):
                k.execute("SELECT 1 FROM orders WHERE order_id = %s FOR UPDATE", (oid,))
                held[oid] = i
        elif r < 0.68 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=False):
                insert_item(c, oid)
        elif r < 0.71 and known:
            batch = [o for o in rng.sample(known, min(len(known), rng.randint(2, 8))) if free(o, i, False)]
            if batch:
                copy_items(c, batch)
        elif r < 0.79 and known:
            known_ship.extend(insert_shipments(c, rng.sample(known, min(len(known), rng.randint(1, 6)))))
        elif r < 0.89 and known_ship:
            sid = rng.choice(known_ship)
            if free(("s", sid), i, exclusive=True):
                field = rng.random()
                if field < 0.2:
                    k.execute("UPDATE shipments SET cost = %s WHERE ship_id = %s", (money(0, 900), sid))
                elif field < 0.35 and "label" in ship_cols:
                    k.execute("UPDATE shipments SET label = %s WHERE ship_id = %s",
                              (blob("prose") if rng.random() < 0.1 else text(rng.randint(1, 3)), sid))
                elif field < 0.65:
                    k.execute("UPDATE shipments SET shipped_on = %s WHERE ship_id = %s", (ship_date(), sid))
                elif field < 0.8:
                    k.execute("UPDATE shipments SET carrier = %s WHERE ship_id = %s", (rng.choice(CARRIERS), sid))
                else:
                    k.execute("UPDATE shipments SET order_id = order_id + 1 WHERE ship_id = %s", (sid,))
                held[("s", sid)] = i
        elif r < 0.92 and known_ship:
            sid = rng.choice(known_ship)
            if free(("s", sid), i, exclusive=True):
                k.execute("DELETE FROM shipments WHERE ship_id = %s", (sid,))
                held[("s", sid)] = i
        else:
            k.execute("SELECT count(*) FROM orders WHERE status = 'paid'")
        if st["savepoint"] and rng.random() < 0.35:
            k.execute("ROLLBACK TO SAVEPOINT sp" if rng.random() < 0.5 else "RELEASE SAVEPOINT sp")
            st["savepoint"] = False
    except psycopg2.Error:
        c.rollback()
        release(i)
        state[i].update(open=False, savepoint=False, ops=0)
    if step % 150 == 0:
        known = live_orders()
        known_ship = live_shipments()


def refresh_columns():
    k = snap.cursor()
    for table, cols in (("orders", order_cols), ("order_items", item_cols), ("shipments", ship_cols)):
        k.execute(f"SELECT * FROM {table} LIMIT 0")
        cols[:] = [d[0] for d in k.description]
    k.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = 'order_stage'::regtype ORDER BY enumsortorder")
    stage_labels[:] = [r[0] for r in k.fetchall()]
    k.execute("SELECT to_regclass('shipments_2027') IS NOT NULL, "
              "EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'shipments_legacy'::regclass), "
              "EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'shipments_2025'::regclass), "
              "EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'archive.shipments_2019'::regclass)")
    parts["y2027"], parts["legacy"], parts["y2025"], parts["y2019"] = k.fetchone()


def has_column(table, col):
    return (f"SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = '{table}'::regclass "
            f"AND attname = '{col}' AND NOT attisdropped)")


# Each change: (query that is true once the change is in place, statements). do_ddl applies the first
# change not yet in place, so after a restore the lost changes are made again, in the same order.
DDL_OPS = [
    (f"SELECT pg_relation_filenode('orders') <> {ORDERS_NODE0}", ["VACUUM FULL orders"]),
    (has_column("orders", "channel"), ["ALTER TABLE orders ADD COLUMN channel text NOT NULL DEFAULT 'web'",
                                       "ALTER TABLE order_items ADD COLUMN warehouse smallint"]),
    (has_column("orders", "stage"), ["ALTER TABLE orders ADD COLUMN stage order_stage NOT NULL DEFAULT 'queued'"]),
    ("SELECT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'shipments_legacy'::regclass)",
     ["ALTER TABLE shipments ATTACH PARTITION shipments_legacy FOR VALUES FROM ('2020-01-01') TO ('2025-01-01')"]),
    ("SELECT NOT " + has_column("orders", "gift")[7:], ["ALTER TABLE orders DROP COLUMN gift"]),
    ("SELECT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'picked')",
     ["ALTER TYPE order_stage RENAME VALUE 'picking' TO 'picked'"]),
    ("SELECT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'archive.shipments_2019'::regclass)",
     ["ALTER TABLE shipments ATTACH PARTITION archive.shipments_2019 FOR VALUES FROM ('2019-01-01') TO ('2020-01-01')"]),
    ("SELECT to_regclass('shipments_2027') IS NOT NULL",
     ["CREATE TABLE shipments_2027 PARTITION OF shipments FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')"]),
    ("SELECT NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'shipments'::regclass AND attnum = 6 "
     "AND NOT attisdropped)", ["ALTER TABLE shipments DROP COLUMN label"]),
    ("SELECT NOT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = 'shipments_2025'::regclass)",
     ["ALTER TABLE shipments DETACH PARTITION shipments_2025"]),
    ("SELECT format_type(atttypid, atttypmod) = 'numeric(12,3)' FROM pg_attribute "
     "WHERE attrelid = 'order_items'::regclass AND attname = 'unit_price'",
     ["ALTER TABLE order_items ALTER COLUMN unit_price TYPE numeric(12,3)"]),
    ("SELECT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'held')",
     ["ALTER TYPE order_stage ADD VALUE 'held' BEFORE 'packed'"]),
    (has_column("shipments", "label"), ["ALTER TABLE shipments ADD COLUMN label text DEFAULT 'relabelled'"]),
    (has_column("shipments", "insured"), ["ALTER TABLE shipments ADD COLUMN insured boolean NOT NULL DEFAULT true"]),
    (has_column("orders", "rush"), ["ALTER TABLE orders ADD COLUMN rush boolean NOT NULL DEFAULT false"]),
]


def do_ddl():
    finish_all(0.8)
    while prepared:
        resolve_prepared()
    k = snap.cursor()
    for check, stmts in DDL_OPS:
        k.execute(check)
        if k.fetchone()[0]:
            continue
        if stmts[0].startswith("VACUUM"):
            for st in stmts:
                k.execute(st)
        else:
            d = ddl.cursor()
            for st in stmts:
                d.execute(st)
            ddl.commit()
        refresh_columns()
        return


compression_cycle = list(P["compression"][1:]) + [P["compression"][0]]


def run_phase(n, ddl_count, label, bad_job=False, snaps=4):
    """Run n workload steps with DDL, checkpoints, compression changes, VACUUM and snapshots spread over them."""
    global long_until
    long_until = rng.randint(n // 3, n * 2 // 3)
    ddl_at = set(rng.sample(range(15, max(n - 45, 16 + ddl_count)), ddl_count))
    ckpt_at = set(rng.sample(range(10, n - 10), 1))
    comp_at = {min(ckpt_at) - 1}
    vac_at = set(rng.sample(range(30, n - 30), 1))
    bad_at = rng.randint(max(15, n - 120), n - 40) if bad_job else -1
    snap_at = set(rng.sample(range(10, n - 5), snaps)) | ({bad_at - 1, bad_at + 1} if bad_job else set())
    for step in range(n):
        if step in ddl_at:
            do_ddl()
            continue
        if step in comp_at:
            mode = compression_cycle.pop(0)
            compression_cycle.append(mode)
            snap.cursor().execute(f"ALTER SYSTEM SET wal_compression = '{mode}'")
            snap.cursor().execute("SELECT pg_reload_conf()")
            time.sleep(0.05)
        if step in ckpt_at:
            snap.cursor().execute("CHECKPOINT")
        if step in vac_at:
            snap.cursor().execute(rng.choice(["VACUUM order_items", "VACUUM orders", "VACUUM (FREEZE) order_items",
                                              "VACUUM (FREEZE) orders", "VACUUM shipments"]))
        if step == bad_at:
            finish_all(1.0)
            while prepared:
                resolve_prepared()
            k = ddl.cursor()
            k.execute("UPDATE orders SET status = 'cancelled', total = 0 WHERE order_id %% 3 = %s", (rng.randint(0, 2),))
            k.execute("DELETE FROM order_items WHERE qty < %s", (rng.randint(50, 200),))
            k.execute("UPDATE shipments SET cost = 0 WHERE ship_id %% 4 = %s", (rng.randint(0, 3),))
            ddl.commit()
            continue
        if step in snap_at:
            snapshot(f"{label}-{step}")
        step_once(step)
    finish_all(0.9)


def archive_everything():
    k = snap.cursor()
    k.execute("SELECT pg_walfile_name(pg_switch_wal())")
    last = k.fetchone()[0]
    for _ in range(300):
        if os.path.exists(os.path.join(PGDATA, "pg_wal", "archive_status", last + ".done")):
            return last
        time.sleep(0.1)
    raise SystemExit("archiver did not finish " + last)


def reconnect():
    global snap, ddl
    snap = connect()
    snap.autocommit = True
    ddl = connect()
    sessions[:] = [connect() for _ in sessions]
    for st in state:
        st.update(open=False, savepoint=False, ops=0)
    held.clear()
    keyshared.clear()
    prepared[:] = []


def restore(target, timeline):
    """Stop the server and start a point-in-time restore of the base backup to `target` on `timeline`."""
    global known, known_ship
    archive_everything()
    subprocess.run([CTL, "-D", PGDATA, "-m", "fast", "stop"], check=True, stdout=subprocess.DEVNULL, user="claude")
    shutil.rmtree(PGDATA)
    shutil.copytree(BK, PGDATA)
    subprocess.run(["chown", "-R", "claude", PGDATA], check=True)
    os.chmod(PGDATA, 0o700)
    with open(os.path.join(PGDATA, "postgresql.auto.conf"), "a") as f:
        f.write(f"restore_command = 'cp {ARCH}/%f %p'\nrecovery_target_time = '{target}'\n"
                f"recovery_target_timeline = '{timeline}'\nrecovery_target_action = 'promote'\n")
    open(os.path.join(PGDATA, "recovery.signal"), "w").close()
    shutil.chown(os.path.join(PGDATA, "recovery.signal"), "claude")
    as_pg(CTL, "-D", PGDATA, "-l", PGDATA + ".log", "-w", "start")
    probe = psycopg2.connect(host=SOCK, user="postgres", dbname="postgres")
    probe.autocommit = True
    pk = probe.cursor()
    for _ in range(300):
        pk.execute("SELECT pg_is_in_recovery()")
        if not pk.fetchone()[0]:
            break
        time.sleep(0.1)
    pk.execute("CHECKPOINT")  # before any work on the new timeline
    probe.close()
    reconnect()
    refresh_columns()
    k = snap.cursor()
    k.execute("SELECT gid FROM pg_prepared_xacts")
    for (gid,) in k.fetchall():
        k.execute(f"{'COMMIT' if rng.random() < 0.6 else 'ROLLBACK'} PREPARED '{gid}'")
    k.execute("UPDATE customers SET name = 'restored' WHERE customer = %s", (customers[0],))
    known = live_orders()
    known_ship = live_shipments()
    time.sleep(0.01)


# history before the backup, some of it still open when the backup starts
for step in range(150):
    step_once(step)

# base backup taken while the workload keeps going
os.makedirs(BK)
shutil.chown(BK, "claude")
bb = subprocess.Popen([os.path.join(PGBIN, "pg_basebackup"), "-h", SOCK, "-U", "postgres", "-D", BK, "-X", "none",
                       "-c", "fast", "--no-sync", "--no-manifest"], user="claude", stdout=subprocess.DEVNULL)
step = 150
while bb.poll() is None:
    step_once(step)
    step += 1
assert bb.returncode == 0
for step in range(step, step + 60):
    step_once(step)
# the archive the tool gets starts here: the server is idle, a new segment begins and a checkpoint is taken
go_idle()
k = snap.cursor()
k.execute("SELECT pg_walfile_name(pg_switch_wal())")
k.execute("SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
start_file = k.fetchone()[0]
k.execute("CHECKPOINT")
time.sleep(0.05)
snapshot("archive-start")

steps = P["steps"]
# phase A on timeline 1
run_phase(steps * 3 // 10, 4, "a")
snapshot("restore-point-1")
restore_1 = truth[-1]["at"]
# phase B on timeline 1, later abandoned twice over
run_phase(steps // 10, 1, "b1", snaps=2)
snapshot("restore-point-2")
restore_2 = truth[-1]["at"]
run_phase(steps // 10, 1, "b2", bad_job=True, snaps=2)
# timeline 2: restored to the first restore point
restore(restore_1, "1")
run_phase(steps // 4, 6, "c", bad_job=True)
# timeline 3: restored again, this time to a point on timeline 1 after timeline 2 branched off
restore(restore_2, "1")
run_phase(steps - steps * 3 // 10 - steps // 5 - steps // 4, 10, "d", bad_job=True)

# shared locks at the end: lockers-only multixact, and a locker plus an updater
la, lb = sessions[0], sessions[1]
o1, o2 = rng.sample(live_orders()[:60], 2)
for c in (la, lb):
    c.cursor().execute("SELECT 1 FROM orders WHERE order_id = %s FOR KEY SHARE", (o1,))
la.commit()
lb.commit()
la.cursor().execute("SELECT 1 FROM orders WHERE order_id = %s FOR KEY SHARE", (o2,))
lb.cursor().execute("UPDATE orders SET status = 'audited' WHERE order_id = %s", (o2,))
lb.commit()
la.commit()
snapshot("before-vacuum")
# the cleanup that erased the old versions from the heap
snap.cursor().execute("VACUUM orders")
snap.cursor().execute("VACUUM order_items")
snap.cursor().execute("VACUUM shipments")
snapshot("after-vacuum")
# work still open or prepared when the archive was copied
k = sessions[2].cursor()
insert_order(sessions[2])
k.execute("UPDATE orders SET status = 'in_flight' WHERE order_id = %s", (rng.choice(live_orders()),))
two_pc = connect()
insert_order(two_pc)
insert_shipments(two_pc, [rng.choice(live_orders())])
two_pc.cursor().execute("PREPARE TRANSACTION 'payout-final'")
two_pc.commit()
time.sleep(0.05)
# relations whose backed-up files the tool gets: the catalogs it needs, and every table and TOAST
# table that is, or once was, part of the three tables on the final timeline
k = snap.cursor()
k.execute("""SELECT DISTINCT pg_relation_filenode(c.oid) FROM pg_class c
             WHERE (c.relname IN ('pg_class', 'pg_attribute', 'pg_namespace', 'pg_type', 'pg_enum', 'pg_inherits')
                    AND c.relnamespace = 'pg_catalog'::regnamespace)
                OR c.oid IN (SELECT x.oid FROM pg_class x WHERE x.relnamespace = 'public'::regnamespace
                             AND (x.relname IN ('orders', 'order_items') OR x.relname LIKE 'shipments%')
                             AND x.relkind = 'r')
                OR c.oid = 'archive.shipments_2019'::regclass
                OR c.oid = (SELECT reltoastrelid FROM pg_class WHERE oid = 'archive.shipments_2019'::regclass)
                OR c.oid IN (SELECT x.reltoastrelid FROM pg_class x WHERE x.relnamespace = 'public'::regnamespace
                             AND (x.relname IN ('orders', 'order_items') OR x.relname LIKE 'shipments%'))""")
ship = {str(r[0]) for r in k.fetchall() if r[0]}

k.execute("SELECT oid FROM pg_database WHERE datname = 'shop'")
shop_oid = str(k.fetchone()[0])
# the base backup the tool gets, taken at the end with the last transactions still open or prepared
FB = PGDATA + ".final"
if os.path.exists(FB):
    shutil.rmtree(FB)
os.makedirs(FB)
shutil.chown(FB, "claude")
as_pg(os.path.join(PGBIN, "pg_basebackup"), "-h", SOCK, "-U", "postgres", "-D", FB, "-X", "none",
      "-c", "fast", "--no-sync", "--no-manifest")
last_seg = archive_everything()
subprocess.run([CTL, "-D", PGDATA, "-m", "fast", "stop"], check=True, stdout=subprocess.DEVNULL, user="claude")

# copy out what the recovery tool gets
if os.path.exists(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
dst_base = os.path.join(OUT, "backup", "base", shop_oid)
os.makedirs(dst_base)
for name in os.listdir(os.path.join(FB, "base", shop_oid)):
    if name in ship or name == "pg_filenode.map":  # main forks of the relations involved
        shutil.copyfile(os.path.join(FB, "base", shop_oid, name), os.path.join(dst_base, name))
for d in ("pg_xact", "pg_multixact/offsets", "pg_multixact/members"):
    shutil.copytree(os.path.join(FB, d), os.path.join(OUT, "backup", d))
shutil.copyfile(os.path.join(FB, "backup_label"), os.path.join(OUT, "backup", "backup_label"))
os.makedirs(os.path.join(OUT, "wal"))
for name in sorted(os.listdir(ARCH)):
    if (len(name) == 24 and name[8:] >= start_file[8:]) or name.endswith(".history") or \
            (name.endswith(".partial") and name[8:24] >= start_file[8:]):
        shutil.copyfile(os.path.join(ARCH, name), os.path.join(OUT, "wal", name))
with open(OUT.rstrip("/") + ".truth.json", "w") as f:
    json.dump({"params": P, "restore_at": [restore_1, restore_2], "xid0": XID0, "multi0": MULTI0, "moff0": MOFF0,
               "db": shop_oid, "snapshots": truth}, f)
print("done", P, len(truth), "segments", len(os.listdir(os.path.join(OUT, "wal"))))
