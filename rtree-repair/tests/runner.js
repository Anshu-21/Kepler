'use strict';
// Untrusted-side executor. Loads the candidate, replays a trace of operations and writes the raw
// observations to a file. It never decides anything: the judge (harness.js) lives in another process.
// Usage: node runner.js <rtree.js> <trace.json> <out.json>

const fs = require('fs');
const path = require('path');

const [, , file, tracePath, outPath] = process.argv;
const ops = JSON.parse(fs.readFileSync(tracePath, 'utf8'));
const results = [];
let tree = null;
const views = {};
const nids = new Map();
const nid = (n) => { if (!nids.has(n)) nids.set(n, nids.size + 1); return nids.get(n); };

function flush() {
  fs.writeFileSync(outPath, JSON.stringify(results));
}

const row = (e) => [e.id, e.minX, e.minY, e.maxX, e.maxY, e.z];
const rows = (l) => (Array.isArray(l) ? l.map(row) : { bad: true });
const one = (e) => (e === null || e === undefined ? null : row(e));
const bx = (b) => (b ? { minX: b.minX, minY: b.minY, maxX: b.maxX, maxY: b.maxY } : b);

function dump(n, depth) {
  if (depth > 40) throw new Error('tree too deep');
  const o = { nid: nid(n), leaf: n.leaf, bounds: bx(n.bounds), top: n.top === null ? null : n.top ? { id: n.top.id, z: n.top.z } : 'missing', children: [] };
  if (n.leaf) for (const e of n.children) o.children.push({ id: e.id, minX: e.minX, minY: e.minY, maxX: e.maxX, maxY: e.maxY, z: e.z });
  else for (const c of n.children) o.children.push(dump(c, depth + 1));
  return o;
}

function guarded(f) {
  const names = ['values', 'entries', 'keys', 'forEach', Symbol.iterator];
  const saved = names.map((k) => [k, Map.prototype[k]]);
  let violated = false;
  for (const [k, orig] of saved) {
    Map.prototype[k] = function (...a) { if (this.size > 1000) violated = true; return orig.apply(this, a); };
  }
  try {
    return [f(), violated];
  } finally {
    for (const [k, orig] of saved) Map.prototype[k] = orig;
  }
}

function step(op) {
  const kind = op[0];
  if (kind === 'new') {
    const { RTree } = require(path.resolve(file));
    tree = new RTree();
    return {};
  }
  if (kind === 'ins') { try { tree.insert(op[1]); return { threw: false }; } catch (e) { return { threw: true }; } }
  if (kind === 'upd') { try { tree.update(op[1], op[2]); return { threw: false }; } catch (e) { return { threw: true }; } }
  if (kind === 'rm') return { v: tree.remove(op[1]) };
  if (kind === 'q') return { list: rows(tree.queryRect(op[1])) };
  if (kind === 'qt') return { list: rows(tree.queryTop(op[1], op[2])) };
  if (kind === 'qtv') return { list: rows(views[op[1]].queryTop(op[2], op[3])) };
  if (kind === 'h') return { v: one(tree.hitTest(op[1], op[2])) };
  if (kind === 'S') return { size: tree.size, dump: dump(tree.root, 0) };
  if (kind === 'snap') { views[op[1]] = tree.snapshot(); return {}; }
  if (kind === 'restore') { tree.restore(views[op[1]]); return {}; }
  if (kind === 'SV') { const v = views[op[1]]; return { size: v.size, dump: dump(v.root, 0) }; }
  if (kind === 'qv') return { list: rows(views[op[1]].queryRect(op[2])) };
  if (kind === 'hv') return { v: one(views[op[1]].hitTest(op[2], op[3])) };
  if (kind === 'M') {
    const r = tree.queryRect(op[1]);
    if (r.length) { r[0].minX = 12345; r[0].z = -777; }
    const h = tree.hitTest(op[2], op[3]);
    if (h) { h.minX = 12345; h.z = -777; }
    return {};
  }
  if (kind === 'B') {
    const items = [];
    let trap = false;
    for (const rc of op[1]) {
      tree.resetStats();
      const [list, v] = guarded(() => tree.queryRect(rc));
      trap = trap || v;
      items.push({ list: rows(list), rep: tree.stats.nodeVisits + tree.stats.entryChecks });
    }
    const hits = [];
    for (const [x, y] of op[2]) {
      tree.resetStats();
      const [h, v] = guarded(() => tree.hitTest(x, y));
      trap = trap || v;
      hits.push({ v: one(h), rep: tree.stats.nodeVisits + tree.stats.entryChecks });
    }
    const tops = [];
    for (const rc of op[3] || []) {
      tree.resetStats();
      const [list, v] = guarded(() => tree.queryTop(rc, op[4]));
      trap = trap || v;
      tops.push({ list: rows(list), rep: tree.stats.nodeVisits + tree.stats.entryChecks });
    }
    return { items, hits, tops, trap, dump: dump(tree.root, 0), size: tree.size };
  }
  if (kind === 'T') {
    let leaf = tree.root;
    while (!leaf.leaf) leaf = leaf.children[0];
    const e = leaf.children[0];
    const info = { id: e.id };
    leaf.children.splice(0, 1);
    info.list = rows(tree.queryRect({ minX: e.minX, minY: e.minY, maxX: e.maxX + 0.5, maxY: e.maxY + 0.5 }));
    return info;
  }
  return {};
}

for (const op of ops) {
  try {
    results.push(step(op));
  } catch (e) {
    results.push({ error: String((e && e.message) || e).slice(0, 200) });
  }
}
flush();
