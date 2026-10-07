"""Answer `SELECT * FROM table` as of a past instant from a base backup and archived WAL.

    rewind(data_dir, table, at) -> {"columns": [...], "rows": [[...], ...]}

Command line (what the grader runs):  python3 rewind.py DATA_DIR  < queries.json
where queries.json is a JSON list of {"table": ..., "at": ...}; one JSON answer per
line is written to stdout, in query order.

The tool replays the archived WAL on top of the backed-up pages (point-in-time
recovery semantics: stop before the first commit later than the instant), then reads
the catalogs and the table as they are at that point.
"""
import json
import os
import struct
import sys
from datetime import datetime, timedelta, timezone

BLCKSZ = 8192
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# rmgr ids
RM_XLOG, RM_XACT, RM_MULTIXACT, RM_HEAP2, RM_HEAP = 0, 1, 6, 9, 10

# infomask bits
HEAP_HASNULL = 0x0001
HEAP_XMAX_KEYSHR_LOCK = 0x0010
HEAP_XMAX_EXCL_LOCK = 0x0040
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMIN_FROZEN = 0x0300
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_XMAX_BITS = 0x1CD0
HEAP_MOVED = 0xC000
HEAP_KEYS_UPDATED = 0x2000
HEAP_HOT_UPDATED = 0x4000
NATTS_MASK = 0x07FF

def u16(b, o):
    return b[o] | (b[o + 1] << 8)


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def maxalign(n):
    return (n + 7) & ~7


def xid_precedes(a, b):
    """Modulo-2^32 comparison of normal transaction ids."""
    return ((a - b) & 0xFFFFFFFF) >= 0x80000000


# ---------------------------------------------------------------- compression

def pglz_decompress(src, rawsize):
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < rawsize:
        ctrl = src[i]
        i += 1
        for bit in range(8):
            if i >= n or len(out) >= rawsize:
                break
            if ctrl & (1 << bit):
                length = (src[i] & 0x0F) + 3
                offset = ((src[i] & 0xF0) << 4) | src[i + 1]
                i += 2
                if length == 18:
                    length += src[i]
                    i += 1
                start = len(out) - offset
                if offset >= length:
                    out += out[start:start + length]
                else:
                    for k in range(length):
                        out.append(out[start + k])
            else:
                out.append(src[i])
                i += 1
    return bytes(out)


def lz4_decompress(src, rawsize):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:
            for k in range(mlen):
                out.append(out[start + k])
    return bytes(out[:rawsize])


# ---------------------------------------------------------------- WAL reading

def parse_lsn(text):
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def timelines(wal_dir):
    """{tli: [(tli, first_lsn), ...]} -- the WAL path of every timeline in the archive."""
    names = os.listdir(wal_dir)
    tlis = {int(n[:8], 16) for n in names if len(n) == 24}
    paths = {}
    for tli in tlis:
        path = [(1, 0)]
        hist = os.path.join(wal_dir, "%08X.history" % tli)
        if os.path.exists(hist):
            path = []
            begin = 0
            with open(hist) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].isdigit():
                        path.append((int(parts[0]), begin))
                        begin = parse_lsn(parts[1])
            path.append((tli, begin))
        paths[tli] = path
    return paths


class Wal:
    def __init__(self, wal_dir, path):
        self.files = {}
        self.path = path
        names = sorted(n for n in os.listdir(wal_dir) if len(n) == 24)
        with open(os.path.join(wal_dir, names[0]), "rb") as f:
            head = f.read(40)
        self.segsize = u32(head, 32)
        per_xlogid = 0x100000000 // self.segsize
        for name in names:
            segno = int(name[8:16], 16) * per_xlogid + int(name[16:24], 16)
            self.files[(int(name[:8], 16), segno)] = os.path.join(wal_dir, name)
        self.key = None
        self.data = None

    def segment(self, pos):
        tli = [t for t, begin in self.path if begin <= pos][-1]
        key = (tli, pos // self.segsize)
        if key != self.key:
            path = self.files.get(key)
            self.data = None
            if path:
                with open(path, "rb") as f:
                    self.data = f.read()
            self.key = key
        return self.data

    def read(self, pos, n):
        """Read n record bytes starting at pos, skipping page headers. Returns (bytes, end)."""
        out = bytearray()
        while n > 0:
            seg = self.segment(pos)
            if seg is None:
                return None, pos
            off = pos % self.segsize
            if off % BLCKSZ == 0:
                hdr = 40 if off == 0 else 24
                if u16(seg, off) != 0xD113:
                    return None, pos
                pos += hdr
                off += hdr
            take = min(n, BLCKSZ - off % BLCKSZ)
            out += seg[off:off + take]
            pos += take
            n -= take
        return bytes(out), pos

    def records(self, start):
        pos = start
        while True:
            head, _ = self.read(pos, 4)
            if head is None:
                return
            tot = u32(head, 0)
            if tot < 24:
                return
            rec, end = self.read(pos, tot)
            if rec is None:
                return
            yield pos, end, rec
            if rec[17] == RM_XLOG and (rec[16] & 0xF0) == 0x40:  # XLOG_SWITCH: rest of the segment is unused
                pos = -(-end // self.segsize) * self.segsize
            else:
                pos = maxalign(end)


class Block:
    __slots__ = ("fork", "rel", "blkno", "image", "data", "apply")


def decode_record(rec):
    tot, xid = u32(rec, 0), u32(rec, 4)
    info, rmid = rec[16], rec[17]
    p = 24
    blocks = {}
    order = []
    main_len = 0
    rel = None
    datatotal = 0
    while tot - p > datatotal:  # block headers end where the declared data begins
        bid = rec[p]
        if bid == 255:
            main_len = rec[p + 1]
            p += 2
            break
        if bid == 254:
            main_len = u32(rec, p + 1)
            p += 5
            break
        if bid == 253:
            p += 3
            continue
        if bid == 252:
            p += 5
            continue
        b = Block()
        fork_flags = rec[p + 1]
        dlen = u16(rec, p + 2)
        p += 4
        b.fork = fork_flags & 0x0F
        img = None
        if fork_flags & 0x10:
            ilen, hole_off, binfo = u16(rec, p), u16(rec, p + 2), rec[p + 4]
            p += 5
            if binfo & 0x01 and binfo & 0x1C:
                hole_len = u16(rec, p)
                p += 2
            elif binfo & 0x01:
                hole_len = BLCKSZ - ilen
            else:
                hole_len = 0
            img = (ilen, hole_off, hole_len, binfo)
            datatotal += ilen
        if not fork_flags & 0x80:
            rel = struct.unpack_from("<III", rec, p)
            p += 12
        b.rel = rel
        b.blkno = u32(rec, p)
        p += 4
        b.image = img
        b.data = dlen if fork_flags & 0x20 else 0
        datatotal += b.data
        b.apply = bool(img and img[3] & 0x02)
        blocks[bid] = b
        order.append(bid)
    for bid in order:
        b = blocks[bid]
        if b.image:
            ilen, hole_off, hole_len, binfo = b.image
            raw = rec[p:p + ilen]
            p += ilen
            if binfo & 0x04:
                raw = pglz_decompress(raw, BLCKSZ - hole_len)
            elif binfo & 0x08:
                raw = lz4_decompress(raw, BLCKSZ - hole_len)
            elif binfo & 0x10:
                raise ValueError("zstd page images are not supported")
            b.image = raw[:hole_off] + bytes(hole_len) + raw[hole_off:]
        n = b.data
        b.data = rec[p:p + n]
        p += n
    main = rec[p:p + main_len]
    return xid, info, rmid, blocks, main


# ---------------------------------------------------------------- pages

class Page:
    """A heap page as an LSN and its line pointers: offnum -> None | ('n', tuple) | ('r', to) | ('d',)."""
    __slots__ = ("lsn", "items")

    def __init__(self, lsn=0, items=None):
        self.lsn = lsn
        self.items = items if items is not None else {}

    @classmethod
    def parse(cls, raw):
        lsn = (u32(raw, 0) << 32) | u32(raw, 4)
        lower, upper = u16(raw, 12), u16(raw, 14)
        items = {}
        if upper and 24 <= lower <= BLCKSZ:
            for i in range((lower - 24) // 4):
                lp = u32(raw, 24 + 4 * i)
                off, flags, length = lp & 0x7FFF, (lp >> 15) & 3, lp >> 17
                if flags == 1:
                    items[i + 1] = ("n", bytearray(raw[off:off + length]))
                elif flags == 2:
                    items[i + 1] = ("r", off)
                elif flags == 3:
                    items[i + 1] = ("d",)
        return cls(lsn, items)


def set_xmax(t, x):
    struct.pack_into("<I", t, 4, x)


def get_masks(t):
    return u16(t, 18), u16(t, 20)


def set_masks(t, m2, m):
    struct.pack_into("<HH", t, 18, m2 & 0xFFFF, m & 0xFFFF)


def apply_infobits(t, infobits):
    m2, m = get_masks(t)
    m &= ~(HEAP_XMAX_BITS | HEAP_MOVED)
    m2 &= ~HEAP_KEYS_UPDATED
    if infobits & 0x01:
        m |= HEAP_XMAX_IS_MULTI
    if infobits & 0x02:
        m |= HEAP_XMAX_LOCK_ONLY
    if infobits & 0x04:
        m |= HEAP_XMAX_EXCL_LOCK
    if infobits & 0x08:
        m |= HEAP_XMAX_KEYSHR_LOCK
    if infobits & 0x10:
        m2 |= HEAP_KEYS_UPDATED
    set_masks(t, m2, m)


def new_tuple(xid, m2, m, hoff, body, blkno, offnum):
    t = bytearray(23) + body
    struct.pack_into("<IIIHHHHHB", t, 0, xid, 0, 0, blkno >> 16, blkno & 0xFFFF, offnum, m2, m, hoff)
    return t


def reset_xmax(pg, offnum):
    item = pg.items.get(offnum)
    if item and item[0] == "n":
        t = bytearray(item[1])
        set_xmax(t, 0)
        pg.items[offnum] = ("n", t)


def undo(rmid, info, bid, blocks, main, pg):
    """The page before the record that wrote image `pg`, as far as SELECT can tell: tuples the record added
    are gone and tuples it deleted, updated or locked are still live. Pruning, freezing and the rest only
    touched tuples no snapshot could see any more, or nothing a row's value depends on."""
    op = info & 0x70
    if rmid == RM_HEAP:
        if op == 0x00:
            pg.items.pop(u16(main, 0), None)
        elif op in (0x10, 0x60):
            reset_xmax(pg, u16(main, 4))
        elif op in (0x20, 0x40):
            old_off, new_off = u16(main, 4), u16(main, 12)
            if bid == 0:
                pg.items.pop(new_off, None)
            if bid == 1 or 1 not in blocks:  # the old version is on this page
                reset_xmax(pg, old_off)
    elif rmid == RM_HEAP2:
        if op == 0x50:
            for i in range(u16(main, 2)):
                pg.items.pop(u16(main, 4 + 2 * i), None)
        elif op == 0x60:
            reset_xmax(pg, u16(main, 4))
    return pg


class Base:
    """What every timeline shares: the pages as they were at the checkpoint the archive starts with, rebuilt
    from the base backup taken at the end, plus the backup's pg_xact and pg_multixact for the transactions
    that were over by then.

    The server was idle at that checkpoint, so all earlier transactions had ended, and full_page_writes means
    the first change to any page after it (on the final timeline's path too, since each promoted server took
    a checkpoint before any work) carries an image of the page. A page that path never changes is the backup's
    copy; any other page is its first image with that one change undone."""

    def __init__(self, data_dir, paths):
        bk = os.path.join(data_dir, "backup")
        with open(os.path.join(bk, "backup_label")) as f:
            label = dict(line.split(": ", 1) for line in f.read().splitlines() if ": " in line)
        final_tli = int(label["START TIMELINE"])
        dbs = os.listdir(os.path.join(bk, "base"))
        assert len(dbs) == 1
        self.db = int(dbs[0])
        base = os.path.join(bk, "base", dbs[0])
        self.pages = {}       # (relnode, blkno) -> Page
        self.rels = set()
        for name in os.listdir(base):
            if name.isdigit():
                rel = int(name)
                self.rels.add(rel)
                with open(os.path.join(base, name), "rb") as f:
                    data = f.read()
                for blk in range(len(data) // BLCKSZ):
                    raw = data[blk * BLCKSZ:(blk + 1) * BLCKSZ]
                    if raw[14:16] != b"\0\0":
                        self.pages[(rel, blk)] = Page.parse(raw)
        with open(os.path.join(base, "pg_filenode.map"), "rb") as f:
            m = f.read()
        assert u32(m, 0) == 0x592717
        self.relmap = {u32(m, 8 + 8 * i): u32(m, 12 + 8 * i) for i in range(u32(m, 4))}
        self.clog = SlruReader(os.path.join(bk, "pg_xact"))
        self.moffsets = SlruReader(os.path.join(bk, "pg_multixact", "offsets"))
        self.mmembers = SlruReader(os.path.join(bk, "pg_multixact", "members"))

        # the checkpoint at the start of the archive
        wal_dir = os.path.join(data_dir, "wal")
        wal = Wal(wal_dir, paths[1])
        first = min(seg for tli, seg in wal.files if tli == 1)
        seg_start = first * wal.segsize
        rem = u32(wal.segment(seg_start), 16)
        pos = seg_start + 40 + maxalign(rem) if rem < BLCKSZ - 40 else None
        assert pos is not None
        for _, _, rec in wal.records(pos):
            if rec[17] == RM_XLOG and (rec[16] & 0xF0) in (0x00, 0x10):
                _, _, _, _, main = decode_record(rec)
                self.start = struct.unpack_from("<Q", main, 0)[0]
                self.next_xid = struct.unpack_from("<Q", main, 24)[0] & 0xFFFFFFFF
                self.next_multi, self.next_moffset = struct.unpack_from("<II", main, 36)
                break

        # first change to each page on the final timeline's path
        seen = set()
        for _, end, rec in Wal(wal_dir, paths[final_tli]).records(self.start):
            rmid, info = rec[17], rec[16]
            if rmid not in (RM_HEAP, RM_HEAP2) and not (rmid == RM_XLOG and (info & 0xF0) == 0xB0):
                continue
            if rmid == RM_HEAP2 and (info & 0x70) == 0x40:  # VISIBLE leaves the heap page's tuples alone
                continue
            xid, info, rmid, blocks, main = decode_record(rec)
            for bid, b in blocks.items():
                if b.fork != 0 or b.rel[1] != self.db:
                    continue
                key = (b.rel[2], b.blkno)
                if key in seen:
                    continue
                seen.add(key)
                init = rmid != RM_XLOG and info & 0x80 and bid == 0
                if init:
                    self.pages.pop(key, None)
                elif b.apply:
                    pg = Page.parse(b.image)
                    self.pages[key] = undo(rmid, info, bid, blocks, main, pg) if rmid != RM_XLOG else pg
                    self.rels.add(key[0])
        for pg in self.pages.values():
            pg.lsn = 0


class Replayer:
    def __init__(self, base, data_dir, path):
        self.db = base.db
        self.pages = {k: Page(0, {o: (it[0], bytearray(it[1])) if it[0] == "n" else it for o, it in p.items.items()})
                      for k, p in base.pages.items()}
        self.rels = set(base.rels)
        self.relmap = base.relmap
        self.clog, self.moffsets, self.mmembers = base.clog, base.moffsets, base.mmembers
        self.commits = {}     # xid -> commit time (us since 2000)
        self.multis = {}      # multixact -> [(xid, status)]
        # transactions and multixacts from before the archive's checkpoint are looked up in the backup
        self.next_xid, self.next_multi, self.next_moffset = base.next_xid, base.next_multi, base.next_moffset
        self.wal = Wal(os.path.join(data_dir, "wal"), path)
        self.stream = self.wal.records(base.start)
        self.pending = None

    # --- transaction status
    def committed(self, xid):
        if xid in self.commits:
            return True
        if xid < 3:
            return xid != 0
        if xid_precedes(xid, self.next_xid):
            return self.clog.xid_status(xid) == 1
        return False

    def members(self, multi):
        if multi in self.multis:
            return self.multis[multi]
        # created before the archive's checkpoint: read pg_multixact
        start = self.moffsets.u32(multi // 2048, (multi % 2048) * 4)
        nxt = (multi + 1) & 0xFFFFFFFF or 1
        end = self.next_moffset if nxt == self.next_multi else self.moffsets.u32(nxt // 2048, (nxt % 2048) * 4)
        out = []
        o = start
        while o != end:
            page = o // 1636
            group, idx = divmod(o % 1636, 4)
            flag = self.mmembers.byte(page, group * 20 + idx)
            x = self.mmembers.u32(page, group * 20 + 4 + 4 * idx)
            if x:
                out.append((x, flag))
            o = (o + 1) & 0xFFFFFFFF
        return out

    # --- replay
    def run_until(self, when):
        """Replay up to (not including) the first commit whose time is after `when`."""
        while True:
            if self.pending is None:
                try:
                    self.pending = next(self.stream)
                except StopIteration:
                    return
            pos, end, rec = self.pending
            xid, info, rmid, blocks, main = decode_record(rec)
            if rmid == RM_XACT and (info & 0x70) in (0x00, 0x30):
                if struct.unpack_from("<q", main, 0)[0] > when:
                    return
            self.pending = None
            self.apply(end, xid, info, rmid, blocks, main)

    def page_for(self, b, lsn, adopt=False):
        """Fetch a block for redo. Returns the Page if the record still has to be applied.

        Relations created after the archive's checkpoint and gone by the end (a partition made on an abandoned
        timeline) are not in the backup; they are followed from the first heap record or page image that
        writes them (`adopt`)."""
        if b.fork != 0 or b.rel[1] != self.db:
            return None
        if b.rel[2] not in self.rels:
            if not (adopt and b.apply):
                return None
            self.rels.add(b.rel[2])
        key = (b.rel[2], b.blkno)
        if b.apply:
            pg = Page.parse(b.image)
            pg.lsn = lsn
            self.pages[key] = pg
            return None
        pg = self.pages.get(key)
        if pg is None:
            return None
        if lsn <= pg.lsn:
            return None
        return pg

    def init_page(self, b, lsn):
        if b.fork != 0 or b.rel[1] != self.db:
            return None
        self.rels.add(b.rel[2])
        if b.apply:  # a page image of the initialised page
            return self.page_for(b, lsn)
        pg = Page(lsn)
        self.pages[(b.rel[2], b.blkno)] = pg
        return pg

    def apply(self, lsn, xid, info, rmid, blocks, main):
        if rmid == RM_XACT:
            op = info & 0x70
            if op in (0x00, 0x30):
                self.commit(xid, info, main)
            return
        if rmid == RM_MULTIXACT:
            if (info & 0xF0) == 0x20:
                mid, _, n = struct.unpack_from("<IIi", main, 0)
                self.multis[mid] = [struct.unpack_from("<II", main, 12 + 8 * i) for i in range(n)]
            return
        if rmid == RM_XLOG:
            op = info & 0xF0
            for b in blocks.values():  # XLOG_FPI from log_newpage() writes rewritten heaps too
                self.page_for(b, lsn, adopt=(info & 0xF0) == 0xB0)
            return
        if rmid in (RM_HEAP, RM_HEAP2):
            for b in blocks.values():
                if b.apply and b.fork == 0 and b.rel[1] == self.db:
                    self.rels.add(b.rel[2])
        if rmid == RM_HEAP:
            self.heap(lsn, xid, info, blocks, main)
        elif rmid == RM_HEAP2:
            self.heap2(lsn, xid, info, blocks, main)
        else:
            for b in blocks.values():
                self.page_for(b, lsn)

    def commit(self, xid, info, main):
        when = struct.unpack_from("<q", main, 0)[0]
        p = 8
        xinfo = 0
        if info & 0x80:
            xinfo = u32(main, p)
            p += 4
        if xinfo & 0x01:
            p += 8
        subs = []
        if xinfo & 0x02:
            n = u32(main, p)
            subs = list(struct.unpack_from(f"<{n}I", main, p + 4))
            p += 4 + 4 * n
        if xinfo & 0x04:
            p += 4 + 12 * u32(main, p)
        if xinfo & 0x100:
            p += 4 + 12 * u32(main, p)
        if xinfo & 0x08:
            p += 4 + 16 * u32(main, p)
        if xinfo & 0x10:
            xid = u32(main, p)
        for x in [xid] + subs:
            self.commits[x] = when

    def heap(self, lsn, xid, info, blocks, main):
        op = info & 0x70
        init = info & 0x80
        if op == 0x00:  # INSERT
            b = blocks[0]
            pg = self.init_page(b, lsn) if init else self.page_for(b, lsn)
            if pg is None:
                return
            offnum = u16(main, 0)
            d = b.data
            m2, m, hoff = u16(d, 0), u16(d, 2), d[4]
            pg.items[offnum] = ("n", new_tuple(xid, m2, m, hoff, d[5:], b.blkno, offnum))
            pg.lsn = lsn
        elif op == 0x10:  # DELETE
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            xmax, offnum, infobits, flags = struct.unpack_from("<IHBB", main, 0)
            t = pg.items[offnum][1]
            apply_infobits(t, infobits)
            m2, m = get_masks(t)
            set_masks(t, m2 & ~HEAP_HOT_UPDATED, m)
            if flags & 0x08:  # super-deletion of a speculative insertion
                struct.pack_into("<I", t, 0, 0)
            else:
                set_xmax(t, xmax)
            pg.lsn = lsn
        elif op in (0x20, 0x40):  # UPDATE, HOT_UPDATE
            self.update(lsn, xid, info, blocks, main, op == 0x40)
        elif op == 0x50:  # CONFIRM
            pg = self.page_for(blocks[0], lsn)
            if pg is not None:
                pg.lsn = lsn
        elif op == 0x60:  # LOCK
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            xmax, offnum, infobits, _ = struct.unpack_from("<IHBB", main, 0)
            t = pg.items[offnum][1]
            apply_infobits(t, infobits)
            m2, m = get_masks(t)
            if m & HEAP_XMAX_LOCK_ONLY:
                set_masks(t, m2 & ~HEAP_HOT_UPDATED, m)
            set_xmax(t, xmax)
            pg.lsn = lsn
        elif op == 0x70:  # INPLACE
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            t = pg.items[u16(main, 0)][1]
            hoff = t[22]
            t[hoff:hoff + len(blocks[0].data)] = blocks[0].data
            pg.lsn = lsn
        else:
            for b in blocks.values():
                self.page_for(b, lsn)

    def update(self, lsn, xid, info, blocks, main, hot):
        old_xmax, old_off, old_infobits, flags, new_xmax, new_off = struct.unpack_from("<IHBBIH", main, 0)
        nb = blocks[0]
        ob = blocks.get(1, nb)
        oldtup = None
        opg = self.page_for(ob, lsn)
        if opg is not None:
            t = opg.items[old_off][1]
            oldtup = bytes(t)
            apply_infobits(t, old_infobits)
            m2, m = get_masks(t)
            set_masks(t, (m2 | HEAP_HOT_UPDATED) if hot else (m2 & ~HEAP_HOT_UPDATED), m)
            set_xmax(t, old_xmax)
            struct.pack_into("<HHH", t, 12, nb.blkno >> 16, nb.blkno & 0xFFFF, new_off)
            opg.lsn = lsn
        if ob is nb:
            npg = opg
        elif info & 0x80:
            npg = self.init_page(nb, lsn)
        else:
            npg = self.page_for(nb, lsn)
        if npg is None:
            return
        d = nb.data
        p = 0
        prefix = suffix = 0
        if flags & 0x20:
            prefix = u16(d, p)
            p += 2
        if flags & 0x40:
            suffix = u16(d, p)
            p += 2
        m2, m, hoff = u16(d, p), u16(d, p + 2), d[p + 4]
        p += 5
        rest = d[p:]
        if prefix:
            bitmap_len = hoff - 23
            body = rest[:bitmap_len] + oldtup[oldtup[22]:oldtup[22] + prefix] + rest[bitmap_len:]
        else:
            body = rest
        if suffix:
            body = body + oldtup[len(oldtup) - suffix:]
        t = new_tuple(xid, m2, m, hoff, body, nb.blkno, new_off)
        set_xmax(t, new_xmax)
        npg.items[new_off] = ("n", t)
        npg.lsn = lsn

    def heap2(self, lsn, xid, info, blocks, main):
        op = info & 0x70
        if op == 0x10:  # PRUNE
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            nred, ndead = u16(main, 4), u16(main, 6)
            d = blocks[0].data
            offs = struct.unpack_from(f"<{len(d) // 2}H", d, 0)
            for i in range(nred):
                pg.items[offs[2 * i]] = ("r", offs[2 * i + 1])
            for o in offs[2 * nred:2 * nred + ndead]:
                pg.items[o] = ("d",)
            for o in offs[2 * nred + ndead:]:
                pg.items.pop(o, None)
            pg.lsn = lsn
        elif op == 0x20:  # VACUUM
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            d = blocks[0].data
            for o in struct.unpack_from(f"<{len(d) // 2}H", d, 0):
                pg.items.pop(o, None)
            pg.lsn = lsn
        elif op == 0x30:  # FREEZE_PAGE
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            nplans = u16(main, 4)
            d = blocks[0].data
            offs_at = 12 * nplans
            cur = 0
            for i in range(nplans):
                xmax, fm2, fm, frz, ntup = struct.unpack_from("<IHHBxH", d, 12 * i)
                for _ in range(ntup):
                    o = u16(d, offs_at + 2 * cur)
                    cur += 1
                    t = pg.items[o][1]
                    set_xmax(t, xmax)
                    if frz & 0x02:
                        struct.pack_into("<I", t, 8, 2)
                    if frz & 0x04:
                        struct.pack_into("<I", t, 8, 0)
                    set_masks(t, fm2, fm)
            pg.lsn = lsn
        elif op == 0x50:  # MULTI_INSERT
            b = blocks[0]
            init = info & 0x80
            pg = self.init_page(b, lsn) if init else self.page_for(b, lsn)
            if pg is None:
                return
            ntup = u16(main, 2)
            d = b.data
            p = 0
            for i in range(ntup):
                offnum = i + 1 if init else u16(main, 4 + 2 * i)
                p = (p + 1) & ~1
                dlen, m2, m, hoff = u16(d, p), u16(d, p + 2), u16(d, p + 4), d[p + 6]
                p += 7
                pg.items[offnum] = ("n", new_tuple(xid, m2, m, hoff, d[p:p + dlen], b.blkno, offnum))
                p += dlen
            pg.lsn = lsn
        elif op == 0x60:  # LOCK_UPDATED
            pg = self.page_for(blocks[0], lsn)
            if pg is None:
                return
            xmax, offnum, infobits, _ = struct.unpack_from("<IHBB", main, 0)
            t = pg.items[offnum][1]
            apply_infobits(t, infobits)
            set_xmax(t, xmax)
            pg.lsn = lsn
        else:  # VISIBLE and the rest only matter through their page images
            for b in blocks.values():
                self.page_for(b, lsn)

    # --- reading tables at the current replay point
    def tuples(self, relnode):
        keys = sorted(k for k in self.pages if k[0] == relnode)
        for key in keys:
            for off, item in self.pages[key].items.items():
                if item[0] == "n":
                    yield item[1]

    def visible(self, t):
        xmin, xmax = u32(t, 0), u32(t, 4)
        m = u16(t, 20)
        if (m & HEAP_XMIN_FROZEN) != HEAP_XMIN_FROZEN and not self.committed(xmin):
            return False
        if xmax == 0:
            return True
        if (m & HEAP_XMAX_LOCK_ONLY) or (m & (HEAP_XMAX_IS_MULTI | HEAP_XMAX_EXCL_LOCK | HEAP_XMAX_KEYSHR_LOCK)) \
                == HEAP_XMAX_EXCL_LOCK:
            return True
        if m & HEAP_XMAX_IS_MULTI:
            upd = [x for x, st in self.members(xmax) if st >= 4]
            return not (upd and self.committed(upd[0]))
        return not self.committed(xmax)


class SlruReader:
    def __init__(self, directory):
        self.dir = directory
        self.cache = {}

    def page(self, pageno):
        if pageno not in self.cache:
            seg, rel = divmod(pageno, 32)
            data = None
            path = os.path.join(self.dir, "%04X" % seg)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    f.seek(rel * BLCKSZ)
                    data = f.read(BLCKSZ)
                if len(data) < BLCKSZ:
                    data = None
            self.cache[pageno] = data
        return self.cache[pageno]

    def xid_status(self, xid):
        page = self.page(xid // 32768)
        if page is None:
            return 0
        return (page[(xid % 32768) // 4] >> ((xid % 4) * 2)) & 3

    def u32(self, pageno, off):
        page = self.page(pageno)
        return 0 if page is None else u32(page, off)

    def byte(self, pageno, off):
        page = self.page(pageno)
        return 0 if page is None else page[off]


# ---------------------------------------------------------------- tuple decoding

def align_to(off, a):
    n = {"c": 1, "s": 2, "i": 4, "d": 8}[a]
    return (off + n - 1) & ~(n - 1)


def varlena_size(d, off):
    b = d[off]
    if b == 0x01:
        return 2 + {18: 16}.get(d[off + 1], 16)
    if b & 1:
        return (b >> 1) & 0x7F
    return (u32(d, off) >> 2) & 0x3FFFFFFF


def deform(t, atts):
    """atts: list of (attlen, attalign). Returns raw values (bytes) or None per attribute present."""
    m2, m = get_masks(t)
    natts = m2 & NATTS_MASK
    hoff = t[22]
    nulls = t[23:hoff] if m & HEAP_HASNULL else None
    off = hoff
    out = []
    for i, (attlen, attalign) in enumerate(atts):
        if i >= natts:
            break
        if nulls is not None and not (nulls[i >> 3] >> (i & 7)) & 1:
            out.append(None)
            continue
        if attlen > 0:
            off = align_to(off, attalign)
            out.append(bytes(t[off:off + attlen]))
            off += attlen
        elif attlen == -1:
            if t[off] == 0:
                off = align_to(off, attalign)
            n = varlena_size(t, off)
            out.append(bytes(t[off:off + n]))
            off += n
        else:  # cstring
            end = t.index(0, off)
            out.append(bytes(t[off:end]))
            off = end + 1
    return out, natts


def varlena_payload(v, toast):
    """Datum bytes (with header) -> value bytes, detoasting and decompressing."""
    b = v[0]
    if b == 0x01:
        rawsize, extinfo, valueid, toastrel = struct.unpack_from("<iIII", v, 2)
        data = toast(toastrel, valueid)
        if (extinfo & 0x3FFFFFFF) < rawsize - 4:
            tcinfo = u32(data, 0)
            return decompress(extinfo >> 30, data[4:], tcinfo & 0x3FFFFFFF)
        return data
    if b & 1:
        return v[1:]
    if (b & 0x03) == 0x02:
        tcinfo = u32(v, 4)
        return decompress(tcinfo >> 30, v[8:], tcinfo & 0x3FFFFFFF)
    return v[4:]


def decompress(method, payload, rawsize):
    return pglz_decompress(payload, rawsize) if method == 0 else lz4_decompress(payload, rawsize)


def numeric_text(v):
    h = u16(v, 0)
    kind = h & 0xC000
    if kind == 0xC000:
        return {0xC000: "NaN", 0xD000: "Infinity", 0xF000: "-Infinity"}[h & 0xF000]
    if kind == 0x8000:
        neg = bool(h & 0x2000)
        dscale = (h & 0x1F80) >> 7
        weight = (h & 0x003F) | (~0x003F if h & 0x0040 else 0)
        digits = struct.unpack_from(f"<{(len(v) - 2) // 2}h", v, 2)
    else:
        neg = kind == 0x4000
        dscale = h & 0x3FFF
        weight = struct.unpack_from("<h", v, 2)[0]
        digits = struct.unpack_from(f"<{(len(v) - 4) // 2}h", v, 4)
    if weight < 0:
        out = "0"
    else:
        out = "".join(str(digits[i] if i < len(digits) else 0) if i == 0 else
                      "%04d" % (digits[i] if i < len(digits) else 0) for i in range(weight + 1))
    if dscale > 0:
        frac = []
        i = weight + 1
        while 4 * len(frac) < dscale:
            frac.append("%04d" % (digits[i] if 0 <= i < len(digits) else 0))
            i += 1
        out += "." + "".join(frac)[:dscale]
    return "-" + out if neg else out


def timestamp_text(us):
    t = EPOCH + timedelta(microseconds=us)
    s = t.strftime("%Y-%m-%d %H:%M:%S")
    if t.microsecond:
        s += ("." + "%06d" % t.microsecond).rstrip("0")
    return s + "+00"


def tdiv(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def interval_text(raw):
    """interval_out with IntervalStyle = postgres."""
    time, day, month = struct.unpack_from("<qii", raw, 0)
    year, mon = tdiv(month, 12), month - 12 * tdiv(month, 12)
    hour = tdiv(time, 3600000000)
    time -= hour * 3600000000
    minute = tdiv(time, 60000000)
    time -= minute * 60000000
    sec = tdiv(time, 1000000)
    fsec = time - sec * 1000000
    out = ""
    is_zero, is_before = True, False
    for value, unit in ((year, "year"), (mon, "mon"), (day, "day")):
        if value == 0:
            continue
        out += ("" if is_zero else " ") + ("+" if is_before and value > 0 else "") + f"{value} {unit}" + \
            ("s" if value != 1 else "")
        is_before, is_zero = value < 0, False
    if is_zero or hour or minute or sec or fsec:
        minus = hour < 0 or minute < 0 or sec < 0 or fsec < 0
        out += ("" if is_zero else " ") + ("-" if minus else "+" if is_before else "") + \
            "%02d:%02d:%02d" % (abs(hour), abs(minute), abs(sec))
        if fsec:
            out += ("." + "%06d" % abs(fsec)).rstrip("0")
    return out


def _pow5_factor(v):
    n = 0
    while v and v % 5 == 0:
        v //= 5
        n += 1
    return n


def _d2d(mant, exp2):
    """PostgreSQL's d2d() (Ryu, built without STRICTLY_SHORTEST) with exact integer arithmetic."""
    if exp2 == 0:
        e2, m2 = 1 - 1023 - 52 - 2, mant
    else:
        e2, m2 = exp2 - 1023 - 52 - 2, (1 << 52) | mant
    mv = 4 * m2
    mm_shift = 1 if mant != 0 or exp2 <= 1 else 0
    mp, mm = mv + 2, mv - 1 - mm_shift
    vr_tz = False
    if e2 >= 0:
        q = ((e2 * 78913) >> 18) - (e2 > 3)
        e10 = q
        den = 10 ** q
        vr, vp, vm = (mv << e2) // den, (mp << e2) // den, (mm << e2) // den
        if q <= 21:
            if mv % 5 == 0:
                vr_tz = _pow5_factor(mv) >= q
            else:
                vp -= _pow5_factor(mv + 2) >= q
    else:
        q = ((-e2 * 732923) >> 20) - (-e2 > 1)
        i = -e2 - q
        e10 = q + e2
        f = 5 ** i
        vr, vp, vm = (mv * f) >> q, (mp * f) >> q, (mm * f) >> q
        if q <= 1:
            vr_tz = True
            vp -= 1
        elif q < 63:
            vr_tz = mv & ((1 << (q - 1)) - 1) == 0
    removed = 0
    if vr_tz:
        last = 0
        while vp // 10 > vm // 10:
            vr_tz &= last == 0
            last = vr % 10
            vr, vp, vm = vr // 10, vp // 10, vm // 10
            removed += 1
        if vr_tz and last == 5 and vr % 2 == 0:
            last = 4
        output = vr + (vr == vm or last >= 5)
    else:
        round_up = False
        while vp // 10 > vm // 10:
            round_up = vr % 10 >= 5
            vr, vp, vm = vr // 10, vp // 10, vm // 10
            removed += 1
        output = vr + (vr == vm or round_up)
    return output, e10 + removed


def float8_text(raw):
    """float8out with extra_float_digits > 0: double_to_shortest_decimal() of src/common/d2s.c."""
    bits = struct.unpack("<Q", raw[:8])[0]
    sign = "-" if bits >> 63 else ""
    exp2, mant = (bits >> 52) & 0x7FF, bits & ((1 << 52) - 1)
    if exp2 == 0x7FF:
        return "NaN" if mant else sign + "Infinity"
    if exp2 == 0 and mant == 0:
        return sign + "0"
    e2 = exp2 - 1023 - 52
    if -52 <= e2 <= 0 and mant & ((1 << -e2) - 1) == 0:  # d2d_small_int
        output, e10 = ((1 << 52) | mant) >> -e2, 0
    else:
        output, e10 = _d2d(mant, exp2)
    digits = str(output)
    exp = e10 + len(digits) - 1
    if -4 <= exp < 15:
        if e10 >= 0:
            return sign + digits + "0" * e10
        point = len(digits) + e10
        if point > 0:
            return sign + digits[:point] + "." + digits[point:]
        return sign + "0." + "0" * -point + digits
    if e10 == 0:
        digits = digits.rstrip("0")
    body = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return sign + body + "e" + ("-" if exp < 0 else "+") + "%02d" % abs(exp)


class Types:
    """pg_type and pg_enum as of the snapshot."""

    def __init__(self, types, labels):
        self.types = types    # oid -> dict(len, align, kind, base, elem)
        self.labels = labels  # enum value oid -> label

    def text(self, oid, raw):
        t = self.types.get(oid)
        if t is not None and t["kind"] == "d":
            return self.text(t["base"], raw)
        if t is not None and t["kind"] == "e":
            return self.labels[u32(raw, 0)]
        return render(oid, raw)

    def array_values(self, raw):
        """(dims, lbounds, [value text or None]) of an array (raw = bytes after the varlena header)."""
        ndim, dataoffset, elemtype = struct.unpack_from("<iiI", raw, 0)
        if ndim == 0:
            return [], [], []
        dims = struct.unpack_from(f"<{ndim}i", raw, 12)
        lbs = struct.unpack_from(f"<{ndim}i", raw, 12 + 4 * ndim)
        nitems = 1
        for d in dims:
            nitems *= d
        p = 12 + 8 * ndim
        bitmap = None
        if dataoffset:
            bitmap = raw[p:p + (nitems + 7) // 8]
            p = dataoffset - 4
        else:
            p = maxalign(p + 4) - 4
        et = self.types[elemtype]
        out = []
        for i in range(nitems):
            if bitmap is not None and not (bitmap[i >> 3] >> (i & 7)) & 1:
                out.append(None)
                continue
            if et["len"] > 0:
                p = align_to(p + 4, et["align"]) - 4
                out.append(self.text(elemtype, raw[p:p + et["len"]]))
                p += et["len"]
            else:
                if raw[p] == 0:
                    p = align_to(p + 4, et["align"]) - 4
                n = varlena_size(raw, p)
                out.append(self.text(elemtype, varlena_payload(raw[p:p + n], None)))
                p += n
        return list(dims), list(lbs), out

def render(oid, raw):
    if oid == 21:
        return str(struct.unpack("<h", raw[:2])[0])
    if oid == 23:
        return str(struct.unpack("<i", raw[:4])[0])
    if oid == 26:
        return str(struct.unpack("<I", raw[:4])[0])
    if oid == 20:
        return str(struct.unpack("<q", raw[:8])[0])
    if oid == 16:
        return "true" if raw[0] else "false"
    if oid in (25, 1043, 1042):
        return raw.decode("utf-8")
    if oid == 19:
        return raw.rstrip(b"\0").decode("utf-8")
    if oid == 1700:
        return numeric_text(raw)
    if oid == 1184:
        return timestamp_text(struct.unpack("<q", raw[:8])[0])
    if oid == 1082:
        return (EPOCH + timedelta(days=struct.unpack("<i", raw[:4])[0])).strftime("%Y-%m-%d")
    if oid == 1186:
        return interval_text(raw)
    if oid == 701:
        return float8_text(raw)
    if oid == 2950:
        h = raw[:16].hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    raise ValueError(f"type {oid}")


# catalog layouts (PostgreSQL 16), as (attlen, attalign) up to the columns we read
PG_CLASS = [(4, "i"), (64, "c"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"),
            (4, "i"), (4, "i"), (4, "i"), (1, "c"), (1, "c"), (1, "c"), (1, "c")]
PG_NAMESPACE = [(4, "i"), (64, "c")]
PG_ATTRIBUTE = [(4, "i"), (64, "c"), (4, "i"), (2, "s"), (2, "s"), (4, "i"), (4, "i"), (2, "s"), (1, "c"), (1, "c"),
                (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (2, "s"),
                (2, "s"), (4, "i"), (-1, "d"), (-1, "i"), (-1, "i"), (-1, "d")]
PG_TYPE = [(4, "i"), (64, "c"), (4, "i"), (4, "i"), (2, "s"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"),
           (1, "c"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"),
           (4, "i"), (4, "i"), (1, "c"), (1, "c"), (1, "c"), (4, "i")]
PG_ENUM = [(4, "i"), (4, "i"), (4, "i"), (64, "c")]
PG_INHERITS = [(4, "i"), (4, "i"), (4, "i"), (1, "c")]


class Snapshot:
    """The database as of the current replay point."""

    def __init__(self, rp):
        self.rp = rp
        self.toast_chunks = {}
        rp_cls = rp.relmap[1259]
        self.classes = {}
        for v in self.rows(rp_cls, PG_CLASS):
            oid = u32(v[0], 0)
            node = u32(v[7], 0) or rp.relmap.get(oid)
            self.classes[oid] = dict(name=v[1].rstrip(b"\0").decode(), nsp=u32(v[2], 0), filenode=node,
                                     toast=u32(v[12], 0), kind=chr(v[16][0]))
        types = {}
        for v in self.rows(self.node(1247), PG_TYPE):
            types[u32(v[0], 0)] = dict(len=struct.unpack("<h", v[4])[0], align=chr(v[22][0]), kind=chr(v[6][0]),
                                       elem=u32(v[13], 0), base=u32(v[25], 0))
        labels = {u32(v[0], 0): v[3].rstrip(b"\0").decode() for v in self.rows(self.node(3501), PG_ENUM)}
        self.types = Types(types, labels)
        self.children = {}
        for v in self.rows(self.node(2611), PG_INHERITS):
            self.children.setdefault(u32(v[1], 0), []).append(u32(v[0], 0))
        self.atts = {}
        for v in self.rows(rp.relmap[1249], PG_ATTRIBUTE):
            num = struct.unpack("<h", v[4])[0]
            if num <= 0:
                continue
            missing = None
            if v[14][0] and len(v) > 25 and v[25] is not None:
                missing = self.types.array_values(varlena_payload(v[25], None))[2][0]
            self.atts.setdefault(u32(v[0], 0), []).append(
                dict(num=num, name=v[1].rstrip(b"\0").decode(), typ=u32(v[2], 0), len=struct.unpack("<h", v[3])[0],
                     align=chr(v[9][0]), dropped=bool(v[17][0]), missing=missing))
        for a in self.atts.values():
            a.sort(key=lambda x: x["num"])

    def node(self, oid):
        return self.classes[oid]["filenode"]

    def rows(self, relnode, layout):
        for t in self.rp.tuples(relnode):
            if self.rp.visible(t):
                vals, _ = deform(t, layout)
                yield vals

    def leaves(self, oid):
        if self.classes[oid]["kind"] == "r":
            return [oid]
        return [leaf for child in self.children.get(oid, []) for leaf in self.leaves(child)]

    def toast(self, toast_node):
        if toast_node not in self.toast_chunks:
            chunks = {}
            for t in self.rp.tuples(toast_node):
                vals, _ = deform(t, [(4, "i"), (4, "i"), (-1, "i")])
                chunks.setdefault(u32(vals[0], 0), {})[struct.unpack("<i", vals[1])[0]] = \
                    varlena_payload(vals[2], None)
            self.toast_chunks[toast_node] = chunks
        return self.toast_chunks[toast_node]

    def select(self, name):
        public = [u32(v[0], 0) for v in self.rows(self.node(2615), PG_NAMESPACE) if v[1].rstrip(b"\0") == b"public"][0]
        oid = [o for o, c in self.classes.items() if c["name"] == name and c["nsp"] == public and c["kind"] in "rp"][0]
        columns = [a for a in self.atts[oid] if not a["dropped"]]
        rows = []
        for leaf in self.leaves(oid):
            rel = self.classes[leaf]
            atts = self.atts[leaf]
            by_name = {a["name"]: i for i, a in enumerate(atts) if not a["dropped"]}
            pick = [by_name[c["name"]] for c in columns]
            chunks = self.toast(self.node(rel["toast"])) if rel["toast"] else {}

            def fetch(_toastrel, valueid, chunks=chunks):
                parts = chunks[valueid]
                return b"".join(parts[i] for i in range(len(parts)))

            layout = [(a["len"], a["align"]) for a in atts]
            for t in self.rp.tuples(rel["filenode"]):
                if not self.rp.visible(t):
                    continue
                vals, natts = deform(t, layout)
                row = []
                for i in pick:
                    a = atts[i]
                    if i >= natts:
                        row.append(a["missing"])
                    elif vals[i] is None:
                        row.append(None)
                    elif a["len"] == -1:
                        row.append(self.types.text(a["typ"], varlena_payload(vals[i], fetch)))
                    else:
                        row.append(self.types.text(a["typ"], vals[i]))
                rows.append(row)
        return {"columns": [c["name"] for c in columns], "rows": rows}


def parse_at(at):
    s = at.strip()
    if s.endswith("+00"):
        s += ":00"
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    d = t - EPOCH
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def first_commit(data_dir, path):
    """Time of the first commit a timeline wrote after branching off its parent."""
    wal = Wal(os.path.join(data_dir, "wal"), path)
    for _, _, rec in wal.records(path[-1][1]):
        if rec[17] == RM_XACT and (rec[16] & 0x70) in (0x00, 0x30):
            _, _, _, _, main = decode_record(rec)
            return struct.unpack_from("<q", main, 0)[0]
    return None


def answer(data_dir, queries):
    """Answer [(table, at), ...]; each timeline is replayed once. Returns answers in query order."""
    paths = timelines(os.path.join(data_dir, "wal"))
    live_from = {}
    for tli, path in paths.items():
        live_from[tli] = -(1 << 63) if len(path) == 1 else first_commit(data_dir, path)
    base = Base(data_dir, paths)
    groups = {}
    for i, (_, at) in enumerate(queries):
        when = parse_at(at)
        tli = max((t for t, since in live_from.items() if since is not None and since <= when), default=1)
        groups.setdefault(tli, []).append(i)
    out = [None] * len(queries)
    for tli, idx in groups.items():
        rp = Replayer(base, data_dir, paths[tli])
        for i in sorted(idx, key=lambda i: parse_at(queries[i][1])):
            rp.run_until(parse_at(queries[i][1]))
            out[i] = Snapshot(rp).select(queries[i][0])
    return out


def rewind(data_dir, table, at):
    return answer(data_dir, [(table, at)])[0]


if __name__ == "__main__":
    qs = json.load(sys.stdin)
    for result in answer(sys.argv[1], [(q["table"], q["at"]) for q in qs]):
        print(json.dumps(result, ensure_ascii=False), flush=True)
