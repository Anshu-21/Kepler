'use strict';
// Verifier harness. Usage: node harness.js <path-to-rtree.js> <scenario>
// Prints one JSON line: {"ok":true} or {"ok":false,"error":"..."}.

const path = require('path');
const MAX = 9;
const MIN = 4;
const LOCAL_BUDGET = Number(process.env.RT_LOCAL_BUDGET || 400);

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

class Fail extends Error {}
function assert(c, msg) {
  if (!c) throw new Fail(msg);
}

const cmpId = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const ov = (a, b) => a.minX < b.maxX && b.minX < a.maxX && a.minY < b.maxY && b.minY < a.maxY;
const cp = (b, x, y) => b.minX <= x && x < b.maxX && b.minY <= y && y < b.maxY;
const key = (o) => `${o.id}|${o.minX}|${o.minY}|${o.maxX}|${o.maxY}|${o.z}`;

class Ref {
  constructor() { this.m = new Map(); }
  insert(o) { this.m.set(o.id, { id: o.id, minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: o.z ?? 0 }); }
  remove(id) { return this.m.delete(id); }
  update(id, b) { const o = this.m.get(id); this.m.set(id, { id, minX: b.minX, minY: b.minY, maxX: b.maxX, maxY: b.maxY, z: b.z ?? o.z }); }
  query(r) { return [...this.m.values()].filter((o) => ov(o, r)).sort((a, b) => cmpId(a.id, b.id)); }
  hit(x, y) {
    let best = null;
    for (const o of this.m.values()) {
      if (!cp(o, x, y)) continue;
      if (!best || o.z > best.z || (o.z === best.z && cmpId(o.id, best.id) < 0)) best = o;
    }
    return best;
  }
}

function mbrOf(items, isNode) {
  let b = null;
  for (const it of items) {
    const x = isNode ? it.bounds : it;
    if (!x) return undefined;
    b = b ? { minX: Math.min(b.minX, x.minX), minY: Math.min(b.minY, x.minY), maxX: Math.max(b.maxX, x.maxX), maxY: Math.max(b.maxY, x.maxY) }
      : { minX: x.minX, minY: x.minY, maxX: x.maxX, maxY: x.maxY };
  }
  return b;
}
const sameBox = (a, b) => (a === null && b === null) || (a && b && a.minX === b.minX && a.minY === b.minY && a.maxX === b.maxX && a.maxY === b.maxY);

function checkStructure(tree, ref) {
  const root = tree.root;
  assert(root && typeof root.leaf === 'boolean' && Array.isArray(root.children), 'root has no node shape');
  const seen = new Set();
  let leafDepth = -1;
  const visited = new Set();
  (function walk(n, depth, isRoot) {
    assert(!visited.has(n), 'node reachable twice');
    visited.add(n);
    const cnt = n.children.length;
    assert(cnt <= MAX, `node over capacity (${cnt})`);
    if (!isRoot) assert(cnt >= MIN, `non-root node underfull (${cnt})`);
    else if (!n.leaf) assert(cnt >= 2, 'internal root with fewer than 2 children');
    const exp = mbrOf(n.children, !n.leaf);
    if (cnt === 0) {
      assert(isRoot && n.leaf, 'empty non-root node');
      assert(n.bounds === null, 'empty root must have null bounds');
    } else {
      assert(sameBox(n.bounds, exp), `node bounds are not the exact MBR of its children (have ${JSON.stringify(n.bounds)}, want ${JSON.stringify(exp)})`);
    }
    if (n.leaf) {
      if (leafDepth < 0) leafDepth = depth;
      assert(leafDepth === depth, 'leaves at different depths');
      for (const e of n.children) {
        assert(typeof e.id === 'string', 'leaf child is not an entry');
        assert(!seen.has(e.id), `object ${e.id} appears more than once`);
        seen.add(e.id);
        const r = ref.m.get(e.id);
        assert(r, `object ${e.id} is in the tree but not in the scene`);
        assert(key(e) === key(r), `stored bounds/z of ${e.id} are stale`);
      }
    } else {
      for (const c of n.children) {
        assert(c && c.children && typeof c.leaf === 'boolean', 'internal child is not a node');
        walk(c, depth + 1, false);
      }
    }
  })(root, 0, true);
  assert(seen.size === ref.m.size, `tree holds ${seen.size} objects, scene has ${ref.m.size}`);
  assert(tree.size === ref.m.size, `size is ${tree.size}, expected ${ref.m.size}`);
}

// Candidate inspections a correct pruned descent makes on the candidate's own tree.
function simulate(tree, r) {
  let n = 0;
  (function go(node) {
    n++;
    if (node.leaf) { n += node.children.length; return; }
    for (const c of node.children) if (ov(c.bounds, r)) go(c);
  })(tree.root);
  return tree.root.bounds && ov(tree.root.bounds, r) ? n : 0;
}

function sameList(a, b, what) {
  assert(Array.isArray(a), `${what}: queryRect did not return an array`);
  assert(a.length === b.length, `${what}: got ${a.length} objects, expected ${b.length}`);
  for (let i = 0; i < a.length; i++) {
    assert(key(a[i]) === key(b[i]), `${what}: result ${i} is ${key(a[i])}, expected ${key(b[i])}`);
  }
}

function compare(tree, ref, r, q, rects, points) {
  for (const rc of rects) sameList(tree.queryRect(rc), ref.query(rc), `queryRect ${JSON.stringify(rc)}`);
  for (const [x, y] of points) {
    const a = tree.hitTest(x, y);
    const b = ref.hit(x, y);
    assert((a === null || a === undefined) === (b === null), `hitTest(${x},${y}): got ${a && a.id}, expected ${b && b.id}`);
    if (b) assert(key(a) === key(b), `hitTest(${x},${y}): got ${key(a)}, expected ${key(b)}`);
  }
}

function probes(rand, ref, W, H, nr, np, scale) {
  const list = [...ref.m.values()];
  const rects = [];
  const points = [];
  for (let i = 0; i < nr; i++) {
    if (list.length && rand() < 0.5) {
      const o = list[Math.floor(rand() * list.length)];
      // rects whose edges coincide with object edges
      const pick = Math.floor(rand() * 4);
      if (pick === 0) rects.push({ minX: o.maxX, minY: o.minY, maxX: o.maxX + scale, maxY: o.maxY });
      else if (pick === 1) rects.push({ minX: o.minX - scale, minY: o.minY, maxX: o.minX, maxY: o.maxY });
      else if (pick === 2) rects.push({ minX: o.minX, minY: o.maxY, maxX: o.maxX, maxY: o.maxY + scale });
      else rects.push({ minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY });
    } else {
      const x = rand() * W, y = rand() * H, w = rand() * scale * 4, h = rand() * scale * 4;
      rects.push({ minX: x, minY: y, maxX: x + w, maxY: y + h });
    }
  }
  for (let i = 0; i < np; i++) {
    if (list.length && rand() < 0.6) {
      const o = list[Math.floor(rand() * list.length)];
      const pick = Math.floor(rand() * 3);
      points.push(pick === 0 ? [o.minX, o.minY] : pick === 1 ? [o.maxX, o.maxY] : [(o.minX + o.maxX) / 2, (o.minY + o.maxY) / 2]);
    } else points.push([rand() * W, rand() * H]);
  }
  return { rects, points };
}

function box(x, y, w, h) { return { minX: x, minY: y, maxX: x + w, maxY: y + h }; }

// ---------- scene generators (return arrays of objects) ----------
function genGrid(n, rand) {
  const side = Math.ceil(Math.sqrt(n));
  const out = [];
  for (let i = 0; i < n; i++) {
    const cx = (i % side) * 10, cy = Math.floor(i / side) * 10;
    out.push({ id: `g${i}`, ...box(cx, cy, 10, 10), z: Math.floor(rand() * 4) }); // edge-sharing tiles
  }
  return out;
}
function genClusters(n, rand) {
  const centres = [[100, 100], [5000, 120], [2500, 4000], [4900, 4900]];
  const out = [];
  for (let i = 0; i < n; i++) {
    const c = centres[i % centres.length];
    const x = c[0] + (rand() - 0.5) * 60, y = c[1] + (rand() - 0.5) * 60;
    out.push({ id: `C${i}`, ...box(x, y, 1 + rand() * 6, 1 + rand() * 6), z: Math.floor(rand() * 6) });
  }
  return out;
}
function genDiagonal(n, rand) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = i * 4;
    out.push({ id: `d${i}`, ...box(t + rand(), t + rand(), 2 + rand() * 20, 2 + rand() * 20), z: i % 3 });
  }
  return out;
}
function genNested(n, rand) {
  const out = [{ id: 'BIG', ...box(0, 0, 3000, 3000), z: 0 }, { id: 'big2', ...box(100, 100, 1500, 1500), z: 1 }];
  for (let i = 0; i < n; i++) {
    const x = rand() * 2900, y = rand() * 2900;
    const deg = rand();
    out.push({ id: `n${i}`, ...(deg < 0.1 ? { minX: x, maxX: x, minY: y, maxY: y + 5 } : deg < 0.2 ? { minX: x, maxX: x + 5, minY: y, maxY: y } : box(x, y, 1 + rand() * 30, 1 + rand() * 30)), z: Math.floor(rand() * 5) });
  }
  return out;
}
const GENS = { grid: [genGrid, 1800, 10000, 10000, 10], clusters: [genClusters, 2000, 5000, 5000, 15], diagonal: [genDiagonal, 1800, 8000, 8000, 30], nested: [genNested, 1800, 3000, 3000, 40] };

function churn(tree, ref, rand, ops, W, H, scale, checkEvery, nextId) {
  for (let i = 0; i < ops; i++) {
    const ids = [...ref.m.keys()];
    const r = rand();
    if (r < 0.3 && ids.length) {
      const id = ids[Math.floor(rand() * ids.length)];
      assert(tree.remove(id) === true, `remove(${id}) did not return true`);
      ref.remove(id);
    } else if (r < 0.7 && ids.length) {
      const id = ids[Math.floor(rand() * ids.length)];
      const nb = rand() < 0.5 ? box(rand() * W, rand() * H, rand() * scale * 2, rand() * scale * 2) : (() => { const o = ref.m.get(id); const dx = (rand() - 0.5) * scale * 3, dy = (rand() - 0.5) * scale * 3; return box(o.minX + dx, o.minY + dy, o.maxX - o.minX, o.maxY - o.minY); })();
      if (rand() < 0.3) nb.z = Math.floor(rand() * 6);
      tree.update(id, nb);
      ref.update(id, nb);
    } else {
      const o = { id: `new${nextId.n++}`, ...box(rand() * W, rand() * H, rand() * scale * 2, rand() * scale * 2), z: Math.floor(rand() * 6) };
      tree.insert(o);
      ref.insert(o);
    }
    if (i % checkEvery === 0) {
      checkStructure(tree, ref);
      const p = probes(rand, ref, W, H, 6, 6, scale);
      compare(tree, ref, rand, null, p.rects, p.points);
    }
  }
}

// ---------- scenarios ----------
const S = {};

S.contract = (RTree) => {
  const t = new RTree();
  const ref = new Ref();
  const add = (o) => { t.insert(o); ref.insert(o); };
  add({ id: 'a', ...box(0, 0, 10, 10), z: 1 });
  add({ id: 'b', ...box(10, 0, 10, 10), z: 1 });
  add({ id: 'B', ...box(0, 10, 10, 10), z: 1 });
  add({ id: 'a10', ...box(5, 5, 10, 10), z: 1 });
  add({ id: 'a2', ...box(5, 5, 10, 10), z: 1 });
  add({ id: 'zero', minX: 3, maxX: 3, minY: 2, maxY: 8, z: 9 });
  add({ id: 'pt', minX: 4, maxX: 4, minY: 4, maxY: 4, z: 9 });
  add({ id: 'neg', ...box(-20, -20, 10, 10), z: -5 });
  add({ id: 'unz', ...box(30, 30, 5, 5) });
  checkStructure(t, ref);
  // touching boundaries
  sameList(t.queryRect({ minX: 10, minY: 0, maxX: 12, maxY: 5 }).map((o) => ({ ...o })), ref.query({ minX: 10, minY: 0, maxX: 12, maxY: 5 }), 'touching');
  const touch = t.queryRect(box(20, 0, 5, 5));
  assert(touch.length === 0, 'objects touching only at max edge must not intersect');
  const ids = t.queryRect({ minX: -100, minY: -100, maxX: 100, maxY: 100 }).map((o) => o.id);
  assert(JSON.stringify(ids) === JSON.stringify(['B', 'a', 'a10', 'a2', 'b', 'neg', 'pt', 'unz', 'zero']), `ids must be in code-unit order, got ${ids}`);
  const p = probes(rng(1), ref, 40, 40, 80, 80, 6);
  compare(t, ref, null, null, p.rects, p.points);
  // z order and tie-break
  assert(t.hitTest(12, 12).id === 'a10', 'z tie must go to the smaller id');
  add({ id: 'apple', ...box(100, 100, 10, 10), z: 3 });
  add({ id: 'Zed', ...box(100, 100, 10, 10), z: 3 });
  assert(t.hitTest(101, 101).id === 'Zed', 'z tie must use code-unit id order, not locale order');
  assert(t.hitTest(6, 6).id === 'a', 'hit tie');
  assert(t.hitTest(2, 2).id === 'a', 'hitTest basic');
  assert(t.hitTest(10, 2).id === 'b', 'hitTest on shared edge belongs to the box whose min edge it is');
  assert(t.hitTest(3, 5).id === 'a', 'zero-width object never contains a point');
  assert(t.hitTest(500, 500) === null, 'hitTest outside returns null');
  assert(t.hitTest(-15, -15).id === 'neg', 'negative z still hit');
  // unspecified z is 0 and update keeps z when omitted
  assert(t.hitTest(31, 31).z === 0, 'default z is 0');
  t.update('unz', box(40, 40, 5, 5)); ref.update('unz', box(40, 40, 5, 5));
  assert(t.hitTest(41, 41).z === 0 && t.hitTest(31, 31) === null, 'update moves object, keeps z');
  t.update('a', { ...box(0, 0, 10, 10), z: 7 }); ref.update('a', { ...box(0, 0, 10, 10), z: 7 });
  assert(t.hitTest(6, 6).id === 'a', 'update with z changes z');
  // errors
  let threw = false;
  try { t.insert({ id: 'a', ...box(0, 0, 1, 1) }); } catch (e) { threw = true; }
  assert(threw, 'duplicate insert must throw');
  threw = false;
  try { t.update('nope', box(0, 0, 1, 1)); } catch (e) { threw = true; }
  assert(threw, 'update of unknown id must throw');
  assert(t.remove('nope') === false, 'remove of unknown id returns false');
  for (const bad of [{ minX: 5, maxX: 1, minY: 0, maxY: 1 }, { minX: NaN, maxX: 1, minY: 0, maxY: 1 }, { minX: 0, maxX: Infinity, minY: 0, maxY: 1 }]) {
    threw = false;
    try { t.insert({ id: 'bad', ...bad }); } catch (e) { threw = true; }
    assert(threw, 'invalid bounds must throw');
    assert(t.size === ref.m.size, 'failed insert must not change the index');
  }
  threw = false;
  try { t.update('b', { minX: 5, maxX: 1, minY: 0, maxY: 1 }); } catch (e) { threw = true; }
  assert(threw, 'invalid update must throw');
  sameList(t.queryRect({ minX: -100, minY: -100, maxX: 100, maxY: 100 }), ref.query({ minX: -100, minY: -100, maxX: 100, maxY: 100 }), 'failed update must leave b untouched');
  // returned objects are copies
  const r0 = t.queryRect({ minX: 0, minY: 0, maxX: 1, maxY: 1 });
  r0[0].minX = 999;
  checkStructure(t, ref);
  // empty tree
  const e = new RTree();
  assert(e.queryRect({ minX: 0, minY: 0, maxX: 10, maxY: 10 }).length === 0 && e.hitTest(1, 1) === null, 'empty tree');
  assert(e.root.children.length === 0 && e.root.bounds === null, 'empty root shape');
};

S.sequence = (RTree) => {
  // the short sequences from the brief, repeated on many shapes
  const rand = rng(7);
  const t = new RTree();
  const ref = new Ref();
  const op = (f) => { f(t); f(ref); };
  for (let round = 0; round < 40; round++) {
    const base = round * 300;
    for (let i = 0; i < 30; i++) {
      const o = { id: `s${round}_${i}`, ...box(base + rand() * 200, rand() * 200, 1 + rand() * 40, 1 + rand() * 40), z: i % 3 };
      op((x) => x.insert(o));
    }
    for (const i of [3, 4, 5, 9, 12, 13, 14, 15, 20, 21, 22]) op((x) => x.remove(`s${round}_${i}`));
    for (const i of [0, 1, 2, 6]) { const nb = box(base + rand() * 200, 500 + rand() * 200, 5 + rand() * 20, 5 + rand() * 20); op((x) => x.update(`s${round}_${i}`, nb)); }
    op((x) => x.update(`s${round}_6`, box(base - 400, -400, 3, 3)));
    op((x) => x.remove(`s${round}_0`));
    op((x) => x.insert({ id: `s${round}_new`, ...box(base, 0, 30, 30), z: 2 }));
    op((x) => x.update(`s${round}_1`, box(base + 1, 1, 30, 30)));
    checkStructure(t, ref);
    const p = probes(rand, ref, 12000, 800, 15, 15, 30);
    compare(t, ref, rand, null, p.rects, p.points);
  }
  // equivalence with a rebuild
  const fresh = new RTree();
  for (const o of ref.m.values()) fresh.insert(o);
  checkStructure(fresh, ref);
  const p = probes(rand, ref, 12000, 800, 60, 60, 30);
  for (const rc of p.rects) sameList(t.queryRect(rc), fresh.queryRect(rc), 'rebuild equivalence');
};

for (const name of Object.keys(GENS)) {
  S[`dist_${name}`] = (RTree) => {
    const [gen, n, W, H, scale] = GENS[name];
    const rand = rng(11 + name.length);
    const objs = gen(n, rand);
    const t = new RTree();
    const ref = new Ref();
    let k = 0;
    for (const o of objs) {
      t.insert(o); ref.insert(o);
      if (++k % 400 === 0) {
        checkStructure(t, ref);
        const p = probes(rand, ref, W, H, 6, 6, scale);
        compare(t, ref, rand, null, p.rects, p.points);
      }
    }
    checkStructure(t, ref);
    let p = probes(rand, ref, W, H, 60, 60, scale);
    compare(t, ref, rand, null, p.rects, p.points);
    churn(t, ref, rand, 1500, W, H, scale, 25, { n: 0 });
    p = probes(rand, ref, W, H, 60, 60, scale);
    compare(t, ref, rand, null, p.rects, p.points);
    // delete in spatial order to force deep condensing
    const ordered = [...ref.m.values()].sort((a, b) => a.minX - b.minX || a.minY - b.minY || cmpId(a.id, b.id));
    let d = 0;
    for (const o of ordered) {
      assert(t.remove(o.id) === true, 'remove failed');
      ref.remove(o.id);
      if (++d % 150 === 0 && ref.m.size > 0) {
        checkStructure(t, ref);
        const pp = probes(rand, ref, W, H, 4, 4, scale);
        compare(t, ref, rand, null, pp.rects, pp.points);
      }
      if (ref.m.size === Math.floor(ordered.length / 20)) break;
    }
    checkStructure(t, ref);
  };
}

S.moving = (RTree) => {
  const rand = rng(99);
  const t = new RTree();
  const ref = new Ref();
  const regions = [[0, 0], [90000, 0], [0, 90000], [90000, 90000], [45000, 45000]];
  for (let i = 0; i < 700; i++) {
    const o = { id: `m${i}`, ...box(rand() * 400, rand() * 400, 2 + rand() * 8, 2 + rand() * 8), z: i % 4 };
    t.insert(o); ref.insert(o);
  }
  for (let step = 0; step < 4500; step++) {
    const id = `m${Math.floor(rand() * 700)}`;
    const r = regions[Math.floor(rand() * regions.length)];
    const nb = box(r[0] + rand() * 400, r[1] + rand() * 400, 2 + rand() * 8, 2 + rand() * 8);
    t.update(id, nb); ref.update(id, nb);
    if (step % 150 === 0) {
      checkStructure(t, ref);
      const p = probes(rand, ref, 91000, 91000, 4, 4, 50);
      const extra = regions.map((r2) => box(r2[0], r2[1], 400, 400));
      compare(t, ref, rand, null, p.rects.concat(extra), p.points);
    }
  }
  checkStructure(t, ref);
};

S.drain = (RTree) => {
  const rand = rng(5);
  const t = new RTree();
  const ref = new Ref();
  for (let cycle = 0; cycle < 3; cycle++) {
    for (let i = 0; i < 1500; i++) {
      const o = { id: `r${cycle}_${i}`, ...box(rand() * 3000, rand() * 3000, rand() * 20, rand() * 20), z: i % 5 };
      t.insert(o); ref.insert(o);
    }
    checkStructure(t, ref);
    const ids = [...ref.m.keys()];
    for (let i = ids.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [ids[i], ids[j]] = [ids[j], ids[i]]; }
    let k = 0;
    for (const id of ids) {
      assert(t.remove(id) === true, 'remove failed');
      ref.remove(id);
      if (++k % 100 === 0 && ref.m.size) { checkStructure(t, ref); const p = probes(rand, ref, 3000, 3000, 3, 3, 40); compare(t, ref, rand, null, p.rects, p.points); }
    }
    checkStructure(t, ref);
    assert(t.size === 0 && t.root.leaf && t.root.children.length === 0 && t.root.bounds === null, 'drained tree must be an empty leaf root');
    assert(t.queryRect({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 }).length === 0, 'drained tree returns nothing');
  }
};

S.tamper = (RTree) => {
  // Results must come from the tree: take an entry out of its leaf and it must stop appearing.
  const rand = rng(3);
  const t = new RTree();
  const ref = new Ref();
  for (let i = 0; i < 800; i++) { const o = { id: `t${i}`, ...box(rand() * 2000, rand() * 2000, 5 + rand() * 10, 5 + rand() * 10), z: 0 }; t.insert(o); ref.insert(o); }
  let leaf = t.root;
  while (!leaf.leaf) leaf = leaf.children[0];
  const e = leaf.children[0];
  leaf.children.splice(0, 1);
  const got = t.queryRect({ minX: e.minX, minY: e.minY, maxX: e.maxX + 0.5, maxY: e.maxY + 0.5 });
  assert(!got.some((o) => o.id === e.id), 'queryRect is not answered from the tree structure');
  const h = t.hitTest((e.minX + e.maxX) / 2, (e.minY + e.maxY) / 2);
  assert(!h || h.id !== e.id, 'hitTest is not answered from the tree structure');
};

function perfScene(rand) {
  const objs = [];
  for (let i = 0; i < 40000; i++) objs.push({ id: `p${i}`, ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) });
  for (let c = 0; c < 20; c++) {
    const cx = rand() * 9500, cy = rand() * 9500;
    for (let i = 0; i < 500; i++) objs.push({ id: `k${c}_${i}`, ...box(cx + rand() * 300, cy + rand() * 300, 2 + rand() * 12, 2 + rand() * 12), z: Math.floor(rand() * 8) });
  }
  return objs;
}

function budgetPhase(t, ref, rand, label) {
  const list = [...ref.m.values()];
  const trapped = [];
  const on = () => {
    for (const k of ['values', 'entries', 'keys', 'forEach', Symbol.iterator]) {
      const orig = Map.prototype[k];
      Map.prototype[k] = function (...a) { if (this.size > 1000) throw new Fail('queries must not iterate over the whole scene'); return orig.apply(this, a); };
      trapped.push([k, orig]);
    }
  };
  const off = () => { for (const [k, orig] of trapped.splice(0)) Map.prototype[k] = orig; };
  const guard = (f) => { on(); try { return f(); } finally { off(); } };
  try {
    let worst = 0;
    for (let i = 0; i < 400; i++) {
      const o = list[Math.floor(rand() * list.length)];
      const rc = box(o.minX - 10, o.minY - 10, 40, 40);
      t.resetStats();
      const res = guard(() => t.queryRect(rc));
      const reported = t.stats.nodeVisits + t.stats.entryChecks;
      const sim = simulate(t, rc);
      const exp = ref.query(rc);
      sameList(res, exp, `${label} localized queryRect`);
      worst = Math.max(worst, reported, sim);
      assert(reported <= LOCAL_BUDGET, `${label}: queryRect reported ${reported} inspections (budget ${LOCAL_BUDGET})`);
      assert(sim <= LOCAL_BUDGET, `${label}: a localized query needs ${sim} inspections on your tree (budget ${LOCAL_BUDGET}); the tree is badly shaped or its bounds are stale`);
      const px = o.minX + (o.maxX - o.minX) / 2, py = o.minY + (o.maxY - o.minY) / 2;
      t.resetStats();
      const h = guard(() => t.hitTest(px, py));
      const hb = ref.hit(px, py);
      assert(h && hb && key(h) === key(hb), `${label}: hitTest mismatch`);
      assert(t.stats.nodeVisits + t.stats.entryChecks <= LOCAL_BUDGET, `${label}: hitTest inspected ${t.stats.nodeVisits + t.stats.entryChecks} (budget ${LOCAL_BUDGET})`);
    }
    return worst;
  } finally {
    off();
  }
}

S.perf = (RTree) => {
  const rand = rng(2024);
  const t = new RTree();
  const ref = new Ref();
  for (const o of perfScene(rand)) { t.insert(o); ref.insert(o); }
  checkStructure(t, ref);
  const w1 = budgetPhase(t, ref, rand, 'fresh');
  // 20k moves, 10k deletes, 10k inserts
  for (let i = 0; i < 20000; i++) {
    const id = rand() < 0.5 ? `p${Math.floor(rand() * 40000)}` : `k${Math.floor(rand() * 20)}_${Math.floor(rand() * 500)}`;
    const nb = box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20);
    t.update(id, nb); ref.update(id, nb);
  }
  const ids = [...ref.m.keys()];
  for (let i = 0; i < 10000; i++) {
    const j = i + Math.floor(rand() * (ids.length - i));
    [ids[i], ids[j]] = [ids[j], ids[i]];
    assert(t.remove(ids[i]) === true, 'remove failed');
    ref.remove(ids[i]);
  }
  for (let i = 0; i < 10000; i++) {
    const o = { id: `q${i}`, ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) };
    t.insert(o); ref.insert(o);
  }
  checkStructure(t, ref);
  const w2 = budgetPhase(t, ref, rand, 'after churn');
  if (process.env.RT_PRINT) console.error('worst', w1, w2);
};

// ---------- run ----------
(function main() {
  const [, , file, name] = process.argv;
  try {
    const { RTree } = require(path.resolve(file));
    assert(typeof RTree === 'function', 'rtree.js must export RTree');
    assert(S[name], `unknown scenario ${name}`);
    S[name](RTree);
    console.log(JSON.stringify({ ok: true }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e && e.message || e).slice(0, 600), kind: e instanceof Fail ? 'fail' : 'crash' }));
  }
})();
