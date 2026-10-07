"""Produce one incident dataset from a real PostgreSQL 16 server.

Usage (as root, PostgreSQL 16 installed, server runs as user `claude`):
    python3 make_dataset.py SEED OUT_DIR SOCKET_DIR PGDATA

Builds a cluster with 1 MB WAL segments and WAL archiving, loads an order schema,
takes a base backup while the workload keeps running, then runs a seeded
multi-session workload (DDL, upserts, COPY, locks that become multixacts,
savepoints, two-phase commits, checkpoints, VACUUM, a changing wal_compression),
a bad bulk job and the VACUUM that erases the old row versions. While the
workload runs it records the live SELECT * of both tables at quiet instants.

Writes OUT_DIR/backup (the subset of the base backup the tool works from),
OUT_DIR/wal (archived segments from the backup start onward) and
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
    orders=rng.randint(150, 250), items_per=rng.randint(2, 3), steps=rng.randint(1000, 1300),
    sessions=rng.randint(3, 5), big_p=rng.uniform(0.03, 0.06),
    wrap=rng.random() < 0.6,
    compression=rng.sample(["pglz", "lz4", "off"], 3),
)
CTL = os.path.join(PGBIN, "pg_ctl")


def as_pg(*cmd, **kw):
    return subprocess.run(list(cmd), check=True, stdout=subprocess.DEVNULL, user="claude", **kw)


for d in (PGDATA, ARCH, BK):
    if os.path.exists(d):
        shutil.rmtree(d)
os.makedirs(ARCH)
shutil.chown(ARCH, "claude")
as_pg(os.path.join(PGBIN, "initdb"), "-D", PGDATA, "-U", "postgres", "-A", "trust", "--no-sync", "--wal-segsize=1")
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
CREATE TABLE customers (customer uuid PRIMARY KEY, name text);
CREATE TABLE archive.orders (order_id bigint PRIMARY KEY, status text, closed_at timestamptz, notes text);
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
  note text
);
""")
ddl.commit()

WORDS = ("alpha bravo crate delta eagle fjord gamma harbor iris jade kilo lumen mango nectar onyx pivot quartz "
         "raven sable tundra umber vivid willow xenon yarrow zephyr").split()
STATUSES = ["new", "paid", "packed", "shipped", "delivered", "returned", "on_hold"]


def text(n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def blob(kind):
    if kind == "prose":
        return text(rng.randint(350, 1500))
    if kind == "noise":
        return "".join(rng.choice("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
                       for _ in range(rng.randint(2100, 5000)))
    if kind == "short-manifest":
        return "\n".join(f"{rng.choice(WORDS)}:{rng.randint(0, 9)}" for _ in range(rng.randint(250, 700)))
    return "\n".join(f"{rng.choice(WORDS)}-{rng.randint(0, 999)}:{rng.randint(0, 99)}" for _ in range(rng.randint(200, 600)))


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


customers = [str(uuid.UUID(int=rng.getrandbits(128))) for _ in range(60)]
cur.execute("INSERT INTO customers SELECT unnest(%s::uuid[]), 'c'", (customers,))
cur.execute("INSERT INTO archive.orders SELECT g, 'closed', now() - g * interval '1 day', 'archived ' || g "
            "FROM generate_series(1, 40) g")
ddl.commit()


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
    )


order_cols = ["order_id", "customer", "status", "total", "placed_at", "ship_by", "priority", "gift", "legacy_code",
              "notes", "manifest"]
item_cols = ["item_id", "order_id", "sku", "qty", "unit_price", "discount", "note"]
next_order = 1000
next_item = 1


def item_values(oid):
    global next_item
    next_item += 1
    return dict(item_id=next_item, order_id=oid,
                sku=f"SKU-{rng.randint(1, 99999):05d}{rng.choice(['', '-XL', '-blue-ltd'])}",
                qty=rng.randint(-5, 500), unit_price=money(0, 9999), discount=discount(),
                note=blob("prose") if rng.random() < 0.02 else (None if rng.random() < 0.6 else text(rng.randint(1, 12))),
                warehouse=None if rng.random() < 0.4 else rng.randint(1, 40))


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


def copy_items(c, oids):
    """COPY a batch of items: the server logs these as multi-insert records."""
    buf = io.StringIO()
    cols = [k for k in item_cols]
    for oid in oids:
        for _ in range(rng.randint(1, 3)):
            v = item_values(oid)
            buf.write("\t".join("\\N" if v[k] is None else str(v[k]).replace("\\", "\\\\") for k in cols) + "\n")
    buf.seek(0)
    c.cursor().copy_expert(f"COPY order_items ({','.join(cols)}) FROM STDIN", buf)


# initial load (part of it through COPY), then history before the backup
load = connect()
oids = [insert_order(load, with_items=rng.random() < 0.5) for _ in range(P["orders"])]
copy_items(load, [o for o in oids if rng.random() < 0.5])
load.commit()
k = ddl.cursor()
k.execute("ALTER TABLE orders DROP COLUMN legacy_code")
order_cols.remove("legacy_code")
ddl.commit()
ddl.autocommit = True
k.execute("VACUUM FULL orders")           # new relfilenode for orders and its TOAST table
k.execute("VACUUM FULL pg_class")         # mapped catalogs move to new filenodes
k.execute("VACUUM FULL pg_attribute")
k.execute("VACUUM (FREEZE) order_items")
ddl.autocommit = False

truth = []
snap = connect()
snap.autocommit = True


def snapshot(label):
    time.sleep(0.004)
    c = snap.cursor()
    names = {}
    for table in ("orders", "order_items"):
        c.execute(f"SELECT * FROM {table} LIMIT 0")
        names[table] = [d[0] for d in c.description]
    parts = [f"(SELECT json_agg(json_build_array({','.join(f'{n}::text' for n in names[t])})) FROM {t})"
             for t in ("orders", "order_items")]
    c.execute("SELECT statement_timestamp()::text, " + ", ".join(parts))
    at, orders, items = c.fetchone()
    truth.append({"label": label, "at": at,
                  "orders": {"columns": names["orders"], "rows": orders or []},
                  "order_items": {"columns": names["order_items"], "rows": items or []}})
    time.sleep(0.004)


sessions = [connect() for _ in range(P["sessions"])]
state = [dict(open=False, savepoint=False, ops=0) for _ in sessions]
held = {}       # order_id -> holder of an exclusive row lock or a modification
keyshared = {}  # order_id -> holders of FOR KEY SHARE
prepared = []   # gids waiting for COMMIT/ROLLBACK PREPARED
gid_seq = [0]


def live_orders():
    k = snap.cursor()
    k.execute("SELECT order_id FROM orders")
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


known = live_orders()
long_session = 0
long_until = -1


def step_once(step):
    global known
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
        if r < 0.15:
            known.append(insert_order(c, with_items=True, upsert=rng.random() < 0.4))
        elif r < 0.19 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=False):
                k.execute("INSERT INTO orders (order_id, customer, status, placed_at) VALUES (%s, %s, 'dup', now()) "
                          "ON CONFLICT (order_id) DO UPDATE SET status = 'upserted', total = %s",
                          (oid, rng.choice(customers), money(1, 999)))
                held[oid] = i
        elif r < 0.47 and known:
            shared = [o for o, h in keyshared.items() if h - {i}]
            oid = rng.choice(shared) if shared and rng.random() < 0.4 else rng.choice(known)
            if free(oid, i, exclusive=False):
                field = rng.random()
                if field < 0.45:
                    k.execute("UPDATE orders SET status = %s, total = %s WHERE order_id = %s",
                              (rng.choice(STATUSES), money(1, 99999), oid))
                elif field < 0.58:
                    k.execute("UPDATE orders SET notes = %s WHERE order_id = %s", (blob("prose"), oid))
                elif field < 0.7:
                    k.execute("UPDATE orders SET manifest = %s WHERE order_id = %s",
                              (blob(rng.choice(["manifest", "short-manifest", "noise"])), oid))
                elif field < 0.85:
                    k.execute("UPDATE orders SET priority = (coalesce(priority, 0) + 1) %% 30000 WHERE order_id = %s",
                              (oid,))
                else:
                    k.execute("UPDATE order_items SET qty = qty + %s, discount = %s WHERE order_id = %s",
                              (rng.randint(-3, 9), discount(), oid))
                held[oid] = i
        elif r < 0.53 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=True):
                k.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
                k.execute("DELETE FROM orders WHERE order_id = %s", (oid,))
                held[oid] = i
        elif r < 0.7 and known:
            pool = known[:30] if rng.random() < 0.6 else known
            for oid in rng.sample(pool, min(len(pool), rng.randint(1, 5))):
                if held.get(oid) not in (None, i):
                    continue
                k.execute("SELECT 1 FROM orders WHERE order_id = %s FOR KEY SHARE", (oid,))
                keyshared.setdefault(oid, set()).add(i)
        elif r < 0.75 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=True):
                k.execute("SELECT 1 FROM orders WHERE order_id = %s FOR UPDATE", (oid,))
                held[oid] = i
        elif r < 0.86 and known:
            oid = rng.choice(known)
            if free(oid, i, exclusive=False):
                insert_item(c, oid)
        elif r < 0.9 and known:
            batch = [o for o in rng.sample(known, min(len(known), rng.randint(2, 8))) if free(o, i, False)]
            if batch:
                copy_items(c, batch)
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


def refresh_columns():
    k = snap.cursor()
    for table, cols in (("orders", order_cols), ("order_items", item_cols)):
        k.execute(f"SELECT * FROM {table} LIMIT 0")
        cols[:] = [d[0] for d in k.description]


DDL_OPS = [
    ("channel", "orders", ["ALTER TABLE orders ADD COLUMN channel text NOT NULL DEFAULT 'web'",
                           "ALTER TABLE order_items ADD COLUMN warehouse smallint"]),
    ("-gift", "orders", ["ALTER TABLE orders DROP COLUMN gift"]),
    ("rush", "orders", ["ALTER TABLE orders ADD COLUMN rush boolean NOT NULL DEFAULT false"]),
]


def do_ddl():
    finish_all(0.8)
    while prepared:
        resolve_prepared()
    for key, table, stmts in DDL_OPS:
        present = key.lstrip("-") in order_cols
        if present == key.startswith("-"):
            k = ddl.cursor()
            for st in stmts:
                k.execute(st)
            ddl.commit()
            refresh_columns()
            return


def run_phase(n, ddl_count, label, bad_job=False):
    """Run n workload steps with DDL, checkpoints, compression changes, VACUUM and snapshots spread over them."""
    global long_until
    long_until = rng.randint(n // 3, n * 2 // 3)
    ddl_at = set(rng.sample(range(20, n - 60), ddl_count))
    ckpt_at = set(rng.sample(range(10, n - 10), 1))
    comp_at = set(rng.sample(range(10, n - 10), 1))
    vac_at = set(rng.sample(range(30, n - 30), 1))
    bad_at = rng.randint(n - 120, n - 60) if bad_job else -1
    snap_at = set(rng.sample(range(10, n - 5), 4)) | ({bad_at - 1, bad_at + 1} if bad_job else set())
    for step in range(n):
        if step in ddl_at:
            do_ddl()
            continue
        if step in ckpt_at:
            snap.cursor().execute("CHECKPOINT")
        if step in comp_at:
            snap.cursor().execute(f"ALTER SYSTEM SET wal_compression = '{rng.choice(['pglz', 'lz4', 'off'])}'")
            snap.cursor().execute("SELECT pg_reload_conf()")
            time.sleep(0.05)
        if step in vac_at:
            snap.cursor().execute(rng.choice(["VACUUM order_items", "VACUUM orders", "VACUUM (FREEZE) order_items",
                                              "VACUUM (FREEZE) orders"]))
        if step == bad_at:
            finish_all(1.0)
            while prepared:
                resolve_prepared()
            k = ddl.cursor()
            k.execute("UPDATE orders SET status = 'cancelled', total = 0 WHERE order_id %% 3 = %s", (rng.randint(0, 2),))
            k.execute("DELETE FROM order_items WHERE qty < %s", (rng.randint(50, 200),))
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
finish_all(0.9)
time.sleep(0.05)
snapshot("after-backup")

steps = P["steps"]
# phase A on timeline 1
run_phase(steps * 2 // 5, 1, "a")
snapshot("restore-point")
restore_at = truth[-1]["at"]
# phase B on timeline 1: later undone by a point-in-time restore
run_phase(steps // 5, 1, "b", bad_job=True)
archive_everything()
subprocess.run([CTL, "-D", PGDATA, "-m", "fast", "stop"], check=True, stdout=subprocess.DEVNULL, user="claude")
shutil.rmtree(PGDATA)
shutil.copytree(BK, PGDATA)
subprocess.run(["chown", "-R", "claude", PGDATA], check=True)
os.chmod(PGDATA, 0o700)
with open(os.path.join(PGDATA, "postgresql.auto.conf"), "a") as f:
    f.write(f"restore_command = 'cp {ARCH}/%f %p'\nrecovery_target_time = '{restore_at}'\n"
            "recovery_target_action = 'promote'\n")
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
probe.close()
reconnect()
refresh_columns()
k = snap.cursor()
k.execute("SELECT gid FROM pg_prepared_xacts")
for (gid,) in k.fetchall():
    k.execute(f"{'COMMIT' if rng.random() < 0.6 else 'ROLLBACK'} PREPARED '{gid}'")
k.execute("UPDATE customers SET name = 'restored' WHERE customer = %s", (customers[0],))
known = live_orders()
time.sleep(0.01)
# phase C on timeline 2
run_phase(steps - steps * 3 // 5, 2, "c", bad_job=True)

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
snapshot("after-vacuum")
# work still open or prepared when the archive was copied
k = sessions[2].cursor()
insert_order(sessions[2])
k.execute("UPDATE orders SET status = 'in_flight' WHERE order_id = %s", (rng.choice(live_orders()),))
two_pc = connect()
insert_order(two_pc)
two_pc.cursor().execute("PREPARE TRANSACTION 'payout-final'")
two_pc.commit()
time.sleep(0.05)
k = snap.cursor()
k.execute("SELECT oid FROM pg_database WHERE datname = 'shop'")
shop_oid = str(k.fetchone()[0])
k.execute("""SELECT pg_relation_filenode(c.oid) FROM pg_class c
             WHERE c.relname IN ('pg_class', 'pg_attribute', 'pg_namespace')
                OR c.oid IN (SELECT x.oid FROM pg_class x WHERE x.relname IN ('orders', 'order_items'))
                OR c.oid IN (SELECT x.reltoastrelid FROM pg_class x WHERE x.relname IN ('orders', 'order_items'))""")
ship = {str(r[0]) for r in k.fetchall()}
last_seg = archive_everything()
subprocess.run([CTL, "-D", PGDATA, "-m", "fast", "stop"], check=True, stdout=subprocess.DEVNULL, user="claude")

# copy out what the recovery tool gets
if os.path.exists(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
with open(os.path.join(BK, "backup_label")) as f:
    label = f.read()
start_file = label.split("(file ")[1].split(")")[0]
dst_base = os.path.join(OUT, "backup", "base", shop_oid)
os.makedirs(dst_base)
for name in os.listdir(os.path.join(BK, "base", shop_oid)):
    if name in ship or name == "pg_filenode.map":  # main forks of the relations involved
        shutil.copyfile(os.path.join(BK, "base", shop_oid, name), os.path.join(dst_base, name))
for d in ("pg_xact", "pg_multixact/offsets", "pg_multixact/members"):
    shutil.copytree(os.path.join(BK, d), os.path.join(OUT, "backup", d))
shutil.copyfile(os.path.join(BK, "backup_label"), os.path.join(OUT, "backup", "backup_label"))
os.makedirs(os.path.join(OUT, "wal"))
for name in sorted(os.listdir(ARCH)):
    if (len(name) == 24 and name[8:] >= start_file[8:]) or name.endswith(".history") or \
            (name.endswith(".partial") and name[8:24] >= start_file[8:]):
        shutil.copyfile(os.path.join(ARCH, name), os.path.join(OUT, "wal", name))
with open(OUT.rstrip("/") + ".truth.json", "w") as f:
    json.dump({"params": P, "restore_at": restore_at, "xid0": XID0, "multi0": MULTI0, "moff0": MOFF0, "db": shop_oid,
               "snapshots": truth}, f)
print("done", P, len(truth), "segments", len(os.listdir(os.path.join(OUT, "wal"))))
