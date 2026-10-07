'use strict';

const DEFAULT_MAX_ENTRIES = 9;

function cmpId(a, b) {
  return a.localeCompare(b);
}

function overlaps(a, b) {
  return a.minX < b.maxX && b.minX < a.maxX && a.minY <= b.maxY && b.minY <= a.maxY;
}

function containsPoint(b, x, y) {
  return b.minX <= x && x < b.maxX && b.minY <= y && y < b.maxY;
}

function containsBox(outer, inner) {
  return (
    outer.minX <= inner.minX && outer.minY <= inner.minY &&
    outer.maxX >= inner.maxX && outer.maxY >= inner.maxY
  );
}

function area(b) {
  return (b.maxX - b.minX) * (b.maxY - b.minY);
}

function union(a, b) {
  return {
    minX: Math.min(a.minX, b.minX),
    minY: Math.min(a.minY, b.minY),
    maxX: Math.max(a.maxX, b.maxX),
    maxY: Math.max(a.maxY, b.maxY),
  };
}

function intersectionArea(a, b) {
  const w = Math.min(a.maxX, b.maxX) - Math.max(a.minX, b.minX);
  const h = Math.min(a.maxY, b.maxY) - Math.max(a.minY, b.minY);
  return w > 0 && h > 0 ? w * h : 0;
}

class Node {
  constructor(leaf) {
    this.leaf = leaf;
    this.children = [];
    this.bounds = null;
  }
}

function boxOf(c) {
  return c instanceof Node ? c.bounds : c;
}

function mbr(items) {
  let b = null;
  for (const it of items) {
    const x = boxOf(it);
    b = b ? union(b, x) : { minX: x.minX, minY: x.minY, maxX: x.maxX, maxY: x.maxY };
  }
  return b;
}

function recalc(node) {
  node.bounds = mbr(node.children);
}

function collectEntries(node, out) {
  for (const e of node.children) out.push(e);
}

function checkBounds(o, what) {
  const ok = o && ['minX', 'minY', 'maxX', 'maxY'].every((k) => Number.isFinite(o[k]));
  if (!ok || o.minX > o.maxX || o.minY > o.maxY) {
    throw new RangeError(`invalid ${what}`);
  }
}

class RTree {
  constructor(options = {}) {
    this.maxEntries = options.maxEntries || DEFAULT_MAX_ENTRIES;
    this.minEntries = Math.ceil(this.maxEntries * 0.4);
    this.root = new Node(true);
    this._ids = new Map();
    this.stats = { nodeVisits: 0, entryChecks: 0 };
  }

  get size() {
    return this._ids.size;
  }

  resetStats() {
    this.stats.nodeVisits = 0;
    this.stats.entryChecks = 0;
  }

  insert(obj) {
    checkBounds(obj, 'bounds');
    if (typeof obj.id !== 'string') throw new TypeError('id must be a string');
    if (this._ids.has(obj.id)) throw new Error(`duplicate id ${obj.id}`);
    const entry = {
      id: obj.id, minX: obj.minX, minY: obj.minY, maxX: obj.maxX, maxY: obj.maxY,
      z: obj.z === undefined ? 0 : obj.z,
    };
    this._ids.set(entry.id, entry);
    this._insertEntry(entry);
  }

  update(id, bounds) {
    const old = this._ids.get(id);
    if (!old) throw new Error(`unknown id ${id}`);
    checkBounds(bounds, 'bounds');
    const next = {
      id, minX: bounds.minX, minY: bounds.minY, maxX: bounds.maxX, maxY: bounds.maxY,
      z: bounds.z === undefined ? old.z : bounds.z,
    };
    this.remove(id);
    this._insertEntry(next);
  }

  remove(id) {
    const entry = this._ids.get(id);
    if (!entry) return false;
    const path = this._findPath(this.root, entry);
    if (!path) throw new Error('index corrupted: entry not reachable');
    const leaf = path[path.length - 1];
    leaf.children.splice(leaf.children.indexOf(entry), 1);
    this._ids.delete(id);

    const orphans = [];
    for (let i = path.length - 1; i >= 1; i--) {
      const node = path[i];
      if (node.children.length < this.minEntries) {
        const parent = path[i - 1];
        parent.children.splice(parent.children.indexOf(node), 1);
        collectEntries(node, orphans);
      } else if (i === path.length - 1) {
        recalc(node);
      }
    }
    if (!this.root.leaf && this.root.children.length === 0) this.root = new Node(true);
    for (const e of orphans) this._insertEntry(e);
    return true;
  }

  queryRect(rect) {
    checkBounds(rect, 'rect');
    const out = [];
    if (this.root.bounds && overlaps(this.root.bounds, rect)) this._query(this.root, rect, out);
    out.sort((a, b) => cmpId(a.id, b.id));
    return out.map((e) => ({ ...e }));
  }

  hitTest(x, y) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw new RangeError('invalid point');
    const best = { e: null };
    if (this.root.bounds && containsPoint(this.root.bounds, x, y)) this._hit(this.root, x, y, best);
    return best.e ? { ...best.e } : null;
  }

  _query(node, rect, out) {
    this.stats.nodeVisits++;
    if (node.leaf) {
      for (const e of node.children) {
        this.stats.entryChecks++;
        if (overlaps(e, rect)) out.push(e);
      }
    } else {
      for (const c of node.children) if (overlaps(c.bounds, rect)) this._query(c, rect, out);
    }
  }

  _hit(node, x, y, best) {
    this.stats.nodeVisits++;
    if (node.leaf) {
      for (const e of node.children) {
        this.stats.entryChecks++;
        if (!containsPoint(e, x, y)) continue;
        const b = best.e;
        if (!b || e.z > b.z) best.e = e;
      }
    } else {
      for (const c of node.children) if (containsPoint(c.bounds, x, y)) this._hit(c, x, y, best);
    }
  }

  _findPath(node, entry) {
    if (node.leaf) return node.children.includes(entry) ? [node] : null;
    for (const c of node.children) {
      if (c.bounds && containsBox(c.bounds, entry)) {
        const p = this._findPath(c, entry);
        if (p) return [node, ...p];
      }
    }
    return null;
  }

  _choose(node, entry) {
    let best = null;
    let bestEnl = Infinity;
    let bestArea = Infinity;
    for (const c of node.children) {
      const a = area(c.bounds);
      const enl = area(union(c.bounds, entry)) - a;
      if (enl < bestEnl || a < bestArea) {
        best = c; bestEnl = enl; bestArea = a;
      }
    }
    return best;
  }

  _insertEntry(entry) {
    const path = [this.root];
    let n = this.root;
    while (!n.leaf) {
      n = this._choose(n, entry);
      path.push(n);
    }
    n.children.push(entry);
    for (let i = path.length - 1; i >= 0; i--) {
      const node = path[i];
      recalc(node);
      if (node.children.length > this.maxEntries) {
        const sibling = this._split(node);
        if (i === 0) {
          const r = new Node(false);
          r.children = [node, sibling];
          recalc(r);
          this.root = r;
        } else {
          path[i - 1].children.push(sibling);
        }
      }
    }
  }

  _split(node) {
    const m = this.minEntries;
    const items = node.children;
    let best = null;
    for (const lo of ['minX', 'minY']) {
      for (const hi of ['maxX', 'maxY']) {
        if (lo[3] !== hi[3]) continue;
        for (const key of [lo, hi]) {
          const sorted = items.slice().sort((a, b) => boxOf(a)[key] - boxOf(b)[key]);
          for (let k = m; k <= sorted.length - m; k++) {
            const bl = mbr(sorted.slice(0, k));
            const br = mbr(sorted.slice(k));
            const ov = intersectionArea(bl, br);
            const ar = area(bl) + area(br);
            if (!best || ov < best.ov || (ov === best.ov && ar < best.ar)) {
              best = { ov, ar, sorted, k };
            }
          }
        }
      }
    }
    const sibling = new Node(node.leaf);
    node.children = best.sorted.slice(0, best.k);
    sibling.children = best.sorted.slice(best.k + (best.ov > 0 ? 1 : 0));
    recalc(node);
    recalc(sibling);
    return sibling;
  }
}

module.exports = { RTree, Node };
