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

TYPES = {16: "bool", 20: "int8", 21: "int2", 23: "int4", 25: "text", 26: "oid", 1043: "varchar", 1042: "bpchar",
         1700: "numeric", 1184: "timestamptz", 1114: "timestamp", 1082: "date", 2950: "uuid", 19: "name"}


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
    while p < tot:
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
        if not fork_flags & 0x80:
            rel = struct.unpack_from("<III", rec, p)
            p += 12
        b.rel = rel
        b.blkno = u32(rec, p)
        p += 4
        b.image = img
        b.data = dlen if fork_flags & 0x20 else 0
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


class Replayer:
    def __init__(self, data_dir, path):
        bk = os.path.join(data_dir, "backup")
        with open(os.path.join(bk, "backup_label")) as f:
            label = dict(line.split(": ", 1) for line in f.read().splitlines() if ": " in line)
        hi, lo = label["START WAL LOCATION"].split()[0].split("/")
        self.start = (int(hi, 16) << 32) | int(lo, 16)
        hi, lo = label["CHECKPOINT LOCATION"].split("/")
        self.checkpoint_lsn = (int(hi, 16) << 32) | int(lo, 16)
        dbs = os.listdir(os.path.join(bk, "base"))
        assert len(dbs) == 1
        self.db = int(dbs[0])
        self.base = os.path.join(bk, "base", dbs[0])
        self.pages = {}       # (relnode, blkno) -> Page
        self.rels = set()
        for name in os.listdir(self.base):
            if name.isdigit():
                rel = int(name)
                self.rels.add(rel)
                with open(os.path.join(self.base, name), "rb") as f:
                    data = f.read()
                for blk in range(len(data) // BLCKSZ):
                    raw = data[blk * BLCKSZ:(blk + 1) * BLCKSZ]
                    if raw[14:16] != b"\0\0":
                        self.pages[(rel, blk)] = Page.parse(raw)
        with open(os.path.join(self.base, "pg_filenode.map"), "rb") as f:
            m = f.read()
        assert u32(m, 0) == 0x592717
        self.relmap = {u32(m, 8 + 8 * i): u32(m, 12 + 8 * i) for i in range(u32(m, 4))}
        self.clog = SlruReader(os.path.join(bk, "pg_xact"))
        self.moffsets = SlruReader(os.path.join(bk, "pg_multixact", "offsets"))
        self.mmembers = SlruReader(os.path.join(bk, "pg_multixact", "members"))
        self.commits = {}     # xid -> commit time (us since 2000)
        self.multis = {}      # multixact -> [(xid, status)]
        self.next_xid = None  # from the backup's checkpoint
        self.next_multi = None
        self.next_moffset = None
        self.wal = Wal(os.path.join(data_dir, "wal"), path)
        self.stream = self.wal.records(self.start)
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
        # created before the backup: read pg_multixact
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

    def page_for(self, b, lsn):
        """Fetch a block for redo. Returns the Page if the record still has to be applied."""
        if b.fork != 0 or b.rel[1] != self.db or b.rel[2] not in self.rels:
            return None
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
        if b.fork != 0 or b.rel[1] != self.db or b.rel[2] not in self.rels:
            return None
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
            if op in (0x00, 0x10) and self.next_xid is None:
                full = struct.unpack_from("<Q", main, 24)[0]
                self.next_xid = full & 0xFFFFFFFF
                self.next_multi, self.next_moffset = struct.unpack_from("<II", main, 36)
            for b in blocks.values():
                self.page_for(b, lsn)
            return
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


def render(typ, raw):
    if typ == "int2":
        return str(struct.unpack("<h", raw[:2])[0])
    if typ in ("int4",):
        return str(struct.unpack("<i", raw[:4])[0])
    if typ == "oid":
        return str(struct.unpack("<I", raw[:4])[0])
    if typ == "int8":
        return str(struct.unpack("<q", raw[:8])[0])
    if typ == "bool":
        return "true" if raw[0] else "false"
    if typ in ("text", "varchar", "bpchar", "name"):
        return raw.decode("utf-8").rstrip("\0") if typ == "name" else raw.decode("utf-8")
    if typ == "numeric":
        return numeric_text(raw)
    if typ == "timestamptz":
        return timestamp_text(struct.unpack("<q", raw[:8])[0])
    if typ == "date":
        return (EPOCH + timedelta(days=struct.unpack("<i", raw[:4])[0])).strftime("%Y-%m-%d")
    if typ == "uuid":
        h = raw[:16].hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    raise ValueError(typ)


TYPINFO = {"bool": (1, "c"), "int2": (2, "s"), "int4": (4, "i"), "int8": (8, "d"), "oid": (4, "i"),
           "timestamptz": (8, "d"), "date": (4, "i"), "uuid": (16, "c")}


def array_first(raw):
    """First element of a one-dimensional array value (attmissingval)."""
    ndim, dataoffset, elemtype = struct.unpack_from("<iiI", raw, 0)
    typ = TYPES[elemtype]
    p = 12 + 8 * ndim
    if dataoffset:
        nitems = struct.unpack_from("<i", raw, 12)[0]
        bitmap = raw[p:p + (nitems + 7) // 8]
        if not bitmap[0] & 1:
            return None
        p = dataoffset - 4
    else:
        p = maxalign(p + 4) - 4  # data starts MAXALIGNed relative to the varlena header
    if typ in TYPINFO:
        return render(typ, raw[p:p + TYPINFO[typ][0]])
    n = varlena_size(raw, p)
    return render(typ, varlena_payload(raw[p:p + n], None))


# catalog layouts (PostgreSQL 16), as (attlen, attalign) up to the columns we read
PG_CLASS = [(4, "i"), (64, "c"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"), (4, "i"),
            (4, "i"), (4, "i"), (4, "i"), (1, "c"), (1, "c"), (1, "c"), (1, "c")]
PG_NAMESPACE = [(4, "i"), (64, "c")]
PG_ATTRIBUTE = [(4, "i"), (64, "c"), (4, "i"), (2, "s"), (2, "s"), (4, "i"), (4, "i"), (2, "s"), (1, "c"), (1, "c"),
                (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (1, "c"), (2, "s"),
                (2, "s"), (4, "i"), (-1, "d"), (-1, "i"), (-1, "i"), (-1, "d")]


class Snapshot:
    """The database as of the current replay point."""

    def __init__(self, rp):
        self.rp = rp
        self.toast_chunks = {}

    def rows(self, relnode, layout):
        for t in self.rp.tuples(relnode):
            if self.rp.visible(t):
                vals, _ = deform(t, layout)
                yield vals

    def table(self, name):
        rp = self.rp
        cls_node = rp.relmap[1259]
        att_node = rp.relmap[1249]
        classes = {}
        for v in self.rows(cls_node, PG_CLASS):
            oid = u32(v[0], 0)
            classes[oid] = dict(name=v[1].rstrip(b"\0").decode(), nsp=u32(v[2], 0), filenode=u32(v[7], 0),
                                toast=u32(v[12], 0), kind=chr(v[16][0]))
        ns_node = classes[2615]["filenode"] or rp.relmap.get(2615)
        public = [u32(v[0], 0) for v in self.rows(ns_node, PG_NAMESPACE) if v[1].rstrip(b"\0") == b"public"][0]
        oid = [o for o, c in classes.items() if c["name"] == name and c["nsp"] == public and c["kind"] == "r"][0]
        rel = classes[oid]
        atts = []
        for v in self.rows(att_node, PG_ATTRIBUTE):
            if u32(v[0], 0) != oid or struct.unpack("<h", v[4])[0] <= 0:
                continue
            missing = None
            if v[14][0] and len(v) > 25 and v[25] is not None:
                missing = array_first(varlena_payload(v[25], None))
            atts.append(dict(num=struct.unpack("<h", v[4])[0], name=v[1].rstrip(b"\0").decode(),
                             typ=TYPES.get(u32(v[2], 0)), len=struct.unpack("<h", v[3])[0], align=chr(v[9][0]),
                             dropped=bool(v[17][0]), missing=missing))
        atts.sort(key=lambda a: a["num"])
        toast_node = classes[rel["toast"]]["filenode"] if rel["toast"] else None
        return rel["filenode"], toast_node, atts

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
        heap_node, toast_node, atts = self.table(name)
        chunks = self.toast(toast_node) if toast_node else {}

        def fetch(_toastrel, valueid):
            parts = chunks[valueid]
            return b"".join(parts[i] for i in range(len(parts)))

        layout = [(a["len"], a["align"]) for a in atts]
        rows = []
        for t in self.rp.tuples(heap_node):
            if not self.rp.visible(t):
                continue
            vals, natts = deform(t, layout)
            row = []
            for i, a in enumerate(atts):
                if a["dropped"]:
                    continue
                if i >= natts:
                    row.append(a["missing"])
                elif vals[i] is None:
                    row.append(None)
                elif a["len"] == -1:
                    row.append(render(a["typ"], varlena_payload(vals[i], fetch)))
                else:
                    row.append(render(a["typ"], vals[i]))
            rows.append(row)
        return {"columns": [a["name"] for a in atts if not a["dropped"]], "rows": rows}


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
    groups = {}
    for i, (_, at) in enumerate(queries):
        when = parse_at(at)
        tli = max((t for t, since in live_from.items() if since is not None and since <= when), default=1)
        groups.setdefault(tli, []).append(i)
    out = [None] * len(queries)
    for tli, idx in groups.items():
        rp = Replayer(data_dir, paths[tli])
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
