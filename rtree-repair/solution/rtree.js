'use strict';

const DEFAULT_MAX_ENTRIES = 9;

function cmpId(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

function overlaps(a, b) {
  return a.minX < b.maxX && b.minX < a.maxX && a.minY < b.maxY && b.minY < a.maxY;
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

// Coordinates may be anywhere in the finite double range, so differences and products are taken on
// coordinates scaled down by 2^-600; the heuristics only need relative sizes.
const SCALE = 2 ** -600;
function area(b) {
  return (b.maxX * SCALE - b.minX * SCALE) * (b.maxY * SCALE - b.minY * SCALE);
}

function margin(b) {
  return b.maxX * SCALE - b.minX * SCALE + (b.maxY * SCALE - b.minY * SCALE);
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
  const w = Math.min(a.maxX, b.maxX) * SCALE - Math.max(a.minX, b.minX) * SCALE;
  const h = Math.min(a.maxY, b.maxY) * SCALE - Math.max(a.minY, b.minY) * SCALE;
  return w > 0 && h > 0 ? w * h : 0;
}

// a is a strictly better hit than b: higher z, then smaller id
function better(a, b) {
  return !b || a.z > b.z || (a.z === b.z && cmpId(a.id, b.id) < 0);
}

class Node {
  constructor(leaf) {
    this.leaf = leaf;
    this.children = [];
    this.bounds = null;
    this.top = null; // best shape (highest z, then smallest id) anywhere below this node
    this._o = 0; // epoch that created this node; only nodes of the current epoch are mutated in place
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
  let top = null;
  for (const c of node.children) {
    const t = c instanceof Node ? c.top : c;
    if (better(t, top)) top = t;
  }
  node.top = top;
}

function collectEntries(node, out) {
  if (node.leaf) {
    for (const e of node.children) out.push(e);
  } else {
    for (const c of node.children) collectEntries(c, out);
  }
}

function checkBounds(o, what) {
  const ok = o && ['minX', 'minY', 'maxX', 'maxY'].every((k) => Number.isFinite(o[k]));
  if (!ok || o.minX > o.maxX || o.minY > o.maxY) {
    throw new RangeError(`invalid ${what}`);
  }
}

function queryImpl(root, stats, rect) {
  checkBounds(rect, 'rect');
  const out = [];
  if (root.bounds && overlaps(root.bounds, rect)) {
    (function go(node) {
      stats.nodeVisits++;
      if (node.leaf) {
        for (const e of node.children) {
          stats.entryChecks++;
          if (overlaps(e, rect)) out.push(e);
        }
      } else {
        for (const c of node.children) if (overlaps(c.bounds, rect)) go(c);
      }
    })(root);
  }
  out.sort((a, b) => cmpId(a.id, b.id));
  return out.map((e) => ({ ...e }));
}

function hitImpl(root, stats, x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) throw new RangeError('invalid point');
  let best = null;
  if (root.bounds && containsPoint(root.bounds, x, y)) {
    (function go(node) {
      stats.nodeVisits++;
      if (node.leaf) {
        for (const e of node.children) {
          stats.entryChecks++;
          if (containsPoint(e, x, y) && better(e, best)) best = e;
        }
      } else {
        const cand = node.children.filter((c) => containsPoint(c.bounds, x, y));
        cand.sort((a, b) => (better(a.top, b.top) ? -1 : 1));
        for (const c of cand) {
          if (!better(c.top, best)) break;
          go(c);
        }
      }
    })(root);
  }
  return best ? { ...best } : null;
}

// The k best shapes (highest z, then smallest id) intersecting rect, best first. Best-first search:
// a node is keyed by its top shape, which no shape below it can beat.
function queryTopImpl(root, stats, rect, k) {
  checkBounds(rect, 'rect');
  if (!Number.isInteger(k) || k < 1) throw new RangeError('invalid k');
  const out = [];
  if (!root.bounds || !overlaps(root.bounds, rect)) return out;
  const heap = [];
  const up = (i) => {
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (!better(heap[i].t, heap[p].t)) break;
      [heap[i], heap[p]] = [heap[p], heap[i]];
      i = p;
    }
  };
  const down = (i) => {
    for (;;) {
      let m = i;
      const l = 2 * i + 1, r = l + 1;
      if (l < heap.length && better(heap[l].t, heap[m].t)) m = l;
      if (r < heap.length && better(heap[r].t, heap[m].t)) m = r;
      if (m === i) break;
      [heap[i], heap[m]] = [heap[m], heap[i]];
      i = m;
    }
  };
  const push = (it) => { heap.push(it); up(heap.length - 1); };
  push({ t: root.top, node: root });
  while (heap.length && out.length < k) {
    const it = heap[0];
    const last = heap.pop();
    if (heap.length) { heap[0] = last; down(0); }
    if (!it.node) { out.push({ ...it.t }); continue; }
    stats.nodeVisits++;
    if (it.node.leaf) {
      for (const e of it.node.children) {
        stats.entryChecks++;
        if (overlaps(e, rect)) push({ t: e, node: null });
      }
    } else {
      for (const c of it.node.children) if (overlaps(c.bounds, rect)) push({ t: c.top, node: c });
    }
  }
  return out;
}

// Read-only view of one version of a tree. Nodes are never mutated once a snapshot refers to them.
class Snapshot {
  constructor(root, size) {
    this.root = root;
    this._size = size;
    this.stats = { nodeVisits: 0, entryChecks: 0 };
  }
  get size() { return this._size; }
  resetStats() { this.stats.nodeVisits = 0; this.stats.entryChecks = 0; }
  queryRect(rect) { return queryImpl(this.root, this.stats, rect); }
  hitTest(x, y) { return hitImpl(this.root, this.stats, x, y); }
  queryTop(rect, k) { return queryTopImpl(this.root, this.stats, rect, k); }
}

class RTree {
  constructor(options = {}) {
    this.maxEntries = options.maxEntries || DEFAULT_MAX_ENTRIES;
    this.minEntries = Math.ceil(this.maxEntries * 0.4);
    this._epoch = 1;
    this.root = new Node(true);
    this.root._o = 1;
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
    this._ids.set(id, next);
    this._insertEntry(next);
  }

  remove(id) {
    const entry = this._ids.get(id);
    if (!entry) return false;
    const path = this._findPath(this.root, entry);
    if (!path) throw new Error('index corrupted: entry not reachable');
    for (let i = 0; i < path.length; i++) path[i] = this._own(path[i], i ? path[i - 1] : null);
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
      } else {
        recalc(node);
      }
    }
    recalc(path[0]);
    while (!this.root.leaf && this.root.children.length <= 1) {
      if (this.root.children.length === 1) this.root = this.root.children[0];
      else { this.root = new Node(true); this.root._o = this._epoch; }
    }
    for (const e of orphans) this._insertEntry(e);
    return true;
  }

  queryRect(rect) {
    return queryImpl(this.root, this.stats, rect);
  }

  hitTest(x, y) {
    return hitImpl(this.root, this.stats, x, y);
  }

  queryTop(rect, k) {
    return queryTopImpl(this.root, this.stats, rect, k);
  }

  snapshot() {
    this._epoch++;
    return new Snapshot(this.root, this._ids.size);
  }

  restore(view) {
    if (!(view instanceof Snapshot)) throw new TypeError('not a snapshot');
    this._epoch++;
    this.root = view.root;
    this._ids = new Map();
    (function walk(n, ids) {
      if (n.leaf) for (const e of n.children) ids.set(e.id, e);
      else for (const c of n.children) walk(c, ids);
    })(this.root, this._ids);
  }

  // copy-on-write: return a node this epoch may mutate, relinking it under its (owned) parent
  _own(node, parent) {
    if (node._o === this._epoch) return node;
    const c = new Node(node.leaf);
    c.children = node.children.slice();
    c.bounds = node.bounds;
    c.top = node.top;
    c._o = this._epoch;
    if (parent) parent.children[parent.children.indexOf(node)] = c;
    else this.root = c;
    return c;
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
    let bestMEnl = Infinity;
    for (const c of node.children) {
      const a = area(c.bounds);
      const u = union(c.bounds, entry);
      const enl = area(u) - a;
      const menl = margin(u) - margin(c.bounds);
      if (best === null || enl < bestEnl || (enl === bestEnl && (menl < bestMEnl || (menl === bestMEnl && a < bestArea)))) {
        best = c; bestEnl = enl; bestArea = a; bestMEnl = menl;
      }
    }
    return best;
  }

  _insertEntry(entry) {
    let n = this._own(this.root, null);
    const path = [n];
    while (!n.leaf) {
      n = this._own(this._choose(n, entry), n);
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
          r._o = this._epoch;
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
            const mg = margin(bl) + margin(br);
            if (!best || ov < best.ov || (ov === best.ov && (ar < best.ar || (ar === best.ar && mg < best.mg)))) {
              best = { ov, ar, mg, sorted, k };
            }
          }
        }
      }
    }
    const sibling = new Node(node.leaf);
    sibling._o = this._epoch;
    node.children = best.sorted.slice(0, best.k);
    sibling.children = best.sorted.slice(best.k);
    recalc(node);
    recalc(sibling);
    return sibling;
  }
}

module.exports = { RTree, Node, Snapshot };
