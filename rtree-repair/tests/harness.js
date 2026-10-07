'use strict';
// Trusted side of the verifier. For one scenario it (1) generates a deterministic trace of
// operations, (2) has runner.js execute that trace against the candidate in a separate unprivileged
// process, and (3) judges the raw observations against a brute-force model. No candidate code ever
// runs in this process, so the verdict cannot be influenced by it.
// Usage: node harness.js <scenario> <rtree.js> <scratch-dir>

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const MAX = 9;
const MIN = 4;
const RECT_BUDGET = Number(process.env.RT_RECT_BUDGET || 400);
const HIT_BUDGET = Number(process.env.RT_HIT_BUDGET || 500);

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
const rowKey = (r) => `${r[0]}|${r[1]}|${r[2]}|${r[3]}|${r[4]}|${r[5]}`;
const better = (a, b) => !b || a.z > b.z || (a.z === b.z && cmpId(a.id, b.id) < 0);

class Ref {
  constructor() { this.m = new Map(); }
  insert(o) { this.m.set(o.id, { id: o.id, minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: o.z ?? 0 }); }
  remove(id) { return this.m.delete(id); }
  update(id, b) { const o = this.m.get(id); this.m.set(id, { id, minX: b.minX, minY: b.minY, maxX: b.maxX, maxY: b.maxY, z: b.z ?? o.z }); }
  query(r) { return [...this.m.values()].filter((o) => ov(o, r)).sort((a, b) => cmpId(a.id, b.id)); }
  hit(x, y) {
    let best = null;
    for (const o of this.m.values()) if (cp(o, x, y) && better(o, best)) best = o;
    return best;
  }
}

function box(x, y, w, h) { return { minX: x, minY: y, maxX: x + w, maxY: y + h }; }
const validBounds = (b) => b && ['minX', 'minY', 'maxX', 'maxY'].every((k) => Number.isFinite(b[k])) && b.minX <= b.maxX && b.minY <= b.maxY;

// ------------------------------------------------------------------ generator
class Gen {
  constructor() { this.ops = [['new']]; this.ref = new Ref(); }
  ins(o) { this.ops.push(['ins', o]); this.ref.insert(o); }
  insBad(o) { this.ops.push(['ins', o]); }
  rm(id) { this.ops.push(['rm', id]); this.ref.remove(id); }
  upd(id, b) { this.ops.push(['upd', id, b]); this.ref.update(id, b); }
  updBad(id, b) { this.ops.push(['upd', id, b]); }
  q(r) { this.ops.push(['q', r]); }
  h(x, y) { this.ops.push(['h', x, y]); }
  S() { this.ops.push(['S']); }
  fresh() { this.ops.push(['new']); this.ref = new Ref(); }
  probes(rand, W, H, nr, np, scale) {
    const list = [...this.ref.m.values()];
    for (let i = 0; i < nr; i++) {
      if (list.length && rand() < 0.5) {
        const o = list[Math.floor(rand() * list.length)];
        const pick = Math.floor(rand() * 4);
        if (pick === 0) this.q({ minX: o.maxX, minY: o.minY, maxX: o.maxX + scale, maxY: o.maxY });
        else if (pick === 1) this.q({ minX: o.minX - scale, minY: o.minY, maxX: o.minX, maxY: o.maxY });
        else if (pick === 2) this.q({ minX: o.minX, minY: o.maxY, maxX: o.maxX, maxY: o.maxY + scale });
        else this.q({ minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY });
      } else {
        const x = rand() * W, y = rand() * H;
        this.q({ minX: x, minY: y, maxX: x + rand() * scale * 4, maxY: y + rand() * scale * 4 });
      }
    }
    for (let i = 0; i < np; i++) {
      if (list.length && rand() < 0.6) {
        const o = list[Math.floor(rand() * list.length)];
        const pick = Math.floor(rand() * 3);
        if (pick === 0) this.h(o.minX, o.minY);
        else if (pick === 1) this.h(o.maxX, o.maxY);
        else this.h((o.minX + o.maxX) / 2, (o.minY + o.maxY) / 2);
      } else this.h(rand() * W, rand() * H);
    }
  }
  check(rand, W, H, nr, np, scale) { this.S(); this.probes(rand, W, H, nr, np, scale); }
}

function genGrid(n, rand) {
  const side = Math.ceil(Math.sqrt(n));
  const out = [];
  for (let i = 0; i < n; i++) out.push({ id: `g${i}`, ...box((i % side) * 10, Math.floor(i / side) * 10, 10, 10), z: Math.floor(rand() * 4) });
  return out;
}
function genClusters(n, rand) {
  const centres = [[100, 100], [5000, 120], [2500, 4000], [4900, 4900]];
  const out = [];
  for (let i = 0; i < n; i++) {
    const c = centres[i % centres.length];
    out.push({ id: `C${i}`, ...box(c[0] + (rand() - 0.5) * 60, c[1] + (rand() - 0.5) * 60, 1 + rand() * 6, 1 + rand() * 6), z: Math.floor(rand() * 6) });
  }
  return out;
}
function genDiagonal(n, rand) {
  const out = [];
  for (let i = 0; i < n; i++) out.push({ id: `d${i}`, ...box(i * 4 + rand(), i * 4 + rand(), 2 + rand() * 20, 2 + rand() * 20), z: i % 3 });
  return out;
}
function genNested(n, rand) {
  const out = [{ id: 'BIG', ...box(0, 0, 3000, 3000), z: 0 }, { id: 'big2', ...box(100, 100, 1500, 1500), z: 1 }];
  for (let i = 0; i < n; i++) {
    const x = rand() * 2900, y = rand() * 2900, deg = rand();
    const b = deg < 0.1 ? { minX: x, maxX: x, minY: y, maxY: y + 5 } : deg < 0.2 ? { minX: x, maxX: x + 5, minY: y, maxY: y } : box(x, y, 1 + rand() * 30, 1 + rand() * 30);
    out.push({ id: `n${i}`, ...b, z: Math.floor(rand() * 5) });
  }
  return out;
}
const GENS = { grid: [genGrid, 1800, 10000, 10000, 10], clusters: [genClusters, 2000, 5000, 5000, 15], diagonal: [genDiagonal, 1800, 8000, 8000, 30], nested: [genNested, 1800, 3000, 3000, 40] };

function churn(g, rand, ops, W, H, scale, checkEvery, nextId) {
  for (let i = 0; i < ops; i++) {
    const ids = [...g.ref.m.keys()];
    const r = rand();
    if (r < 0.3 && ids.length) {
      g.rm(ids[Math.floor(rand() * ids.length)]);
    } else if (r < 0.7 && ids.length) {
      const id = ids[Math.floor(rand() * ids.length)];
      let nb;
      if (rand() < 0.5) nb = box(rand() * W, rand() * H, rand() * scale * 2, rand() * scale * 2);
      else { const o = g.ref.m.get(id); nb = box(o.minX + (rand() - 0.5) * scale * 3, o.minY + (rand() - 0.5) * scale * 3, o.maxX - o.minX, o.maxY - o.minY); }
      if (rand() < 0.3) nb.z = Math.floor(rand() * 6);
      g.upd(id, nb);
    } else {
      g.ins({ id: `new${nextId.n++}`, ...box(rand() * W, rand() * H, rand() * scale * 2, rand() * scale * 2), z: Math.floor(rand() * 6) });
    }
    if (i % checkEvery === 0) g.check(rand, W, H, 6, 6, scale);
  }
}

const SC = {};

SC.contract = () => {
  const g = new Gen();
  g.S(); g.q(box(0, 0, 10, 10)); g.h(1, 1);
  const add = (o) => g.ins(o);
  add({ id: 'a', ...box(0, 0, 10, 10), z: 1 });
  add({ id: 'b', ...box(10, 0, 10, 10), z: 1 });
  add({ id: 'B', ...box(0, 10, 10, 10), z: 1 });
  add({ id: 'a10', ...box(5, 5, 10, 10), z: 1 });
  add({ id: 'a2', ...box(5, 5, 10, 10), z: 1 });
  add({ id: 'zero', minX: 3, maxX: 3, minY: 2, maxY: 8, z: 9 });
  add({ id: 'pt', minX: 4, maxX: 4, minY: 4, maxY: 4, z: 9 });
  add({ id: 'neg', ...box(-20, -20, 10, 10), z: -5 });
  add({ id: 'unz', ...box(30, 30, 5, 5) });
  add({ id: 'apple', ...box(100, 100, 10, 10), z: 3 });
  add({ id: 'Zed', ...box(100, 100, 10, 10), z: 3 });
  g.S();
  const a = g.ref;
  assert(a.hit(101, 101).id === 'Zed' && a.hit(12, 12).id === 'a10' && a.hit(3, 5).id === 'a', 'internal: model');
  assert(a.query({ minX: -100, minY: -100, maxX: 200, maxY: 200 }).map((o) => o.id).join() === 'B,Zed,a,a10,a2,apple,b,neg,pt,unz,zero', 'internal: id order');
  g.q({ minX: 10, minY: 0, maxX: 12, maxY: 5 }); g.q(box(20, 0, 5, 5)); g.q({ minX: -100, minY: -100, maxX: 200, maxY: 200 });
  g.q({ minX: 3, minY: 2, maxX: 3, maxY: 8 }); g.q({ minX: 2, minY: 2, maxX: 4, maxY: 3 });
  for (const [x, y] of [[6, 6], [12, 12], [10, 2], [3, 5], [500, 500], [-15, -15], [31, 31], [101, 101], [110, 110], [0, 0], [4, 4]]) g.h(x, y);
  g.probes(rng(1), 120, 120, 80, 80, 6);
  g.upd('unz', box(40, 40, 5, 5)); g.h(41, 41); g.h(31, 31);
  g.upd('a', { ...box(0, 0, 10, 10), z: 7 }); g.h(6, 6); g.h(2, 2);
  g.upd('a', { ...box(0, 0, 10, 10), z: -3 }); g.h(6, 6);
  g.S();
  g.insBad({ id: 'a', ...box(0, 0, 1, 1) });
  g.updBad('nope', box(0, 0, 1, 1));
  g.rm('nope');
  for (const bad of [{ minX: 5, maxX: 1, minY: 0, maxY: 1 }, { minX: NaN, maxX: 1, minY: 0, maxY: 1 }, { minX: 0, maxX: Infinity, minY: 0, maxY: 1 }]) { g.insBad({ id: 'bad', ...bad }); g.S(); }
  g.updBad('b', { minX: 5, maxX: 1, minY: 0, maxY: 1 });
  g.q({ minX: -100, minY: -100, maxX: 200, maxY: 200 }); g.S();
  g.ops.push(['M', box(0, 0, 1, 1), 0.5, 0.5]); g.S(); g.q(box(0, 0, 1, 1)); g.h(0.5, 0.5);
  g.fresh(); g.S(); g.q(box(0, 0, 10, 10)); g.h(1, 1);
  g.ins({ id: 'only', ...box(0, 0, 1, 1) }); g.rm('only'); g.S(); g.h(0.5, 0.5);
  return g.ops;
};

SC.sequence = () => {
  const g = new Gen();
  const rand = rng(7);
  for (let round = 0; round < 40; round++) {
    const base = round * 300;
    for (let i = 0; i < 30; i++) g.ins({ id: `s${round}_${i}`, ...box(base + rand() * 200, rand() * 200, 1 + rand() * 40, 1 + rand() * 40), z: i % 3 });
    for (const i of [3, 4, 5, 9, 12, 13, 14, 15, 20, 21, 22]) g.rm(`s${round}_${i}`);
    for (const i of [0, 1, 2, 6]) g.upd(`s${round}_${i}`, box(base + rand() * 200, 500 + rand() * 200, 5 + rand() * 20, 5 + rand() * 20));
    g.upd(`s${round}_6`, box(base - 400, -400, 3, 3));
    g.rm(`s${round}_0`);
    g.ins({ id: `s${round}_new`, ...box(base, 0, 30, 30), z: 2 });
    g.upd(`s${round}_1`, box(base + 1, 1, 30, 30));
    g.upd(`s${round}_2`, { ...box(base + 3, 3, 30, 30), z: 9 });
    g.check(rand, 12000, 800, 15, 15, 30);
  }
  // the same final scene built from scratch must answer identically
  const rects = [], pts = [];
  const final = [...g.ref.m.values()];
  g.ops.push(['new']);
  for (const o of final) g.ops.push(['ins', o]);
  const ref2 = g.ref;
  g.S();
  g.probes(rand, 12000, 800, 60, 60, 30);
  void ref2; void rects; void pts;
  return g.ops;
};

for (const name of Object.keys(GENS)) {
  SC[`dist_${name}`] = () => {
    const [gen, n, W, H, scale] = GENS[name];
    const rand = rng(11 + name.length);
    const g = new Gen();
    let k = 0;
    for (const o of gen(n, rand)) {
      g.ins(o);
      if (++k % 400 === 0) g.check(rand, W, H, 6, 6, scale);
    }
    g.check(rand, W, H, 60, 60, scale);
    churn(g, rand, 1500, W, H, scale, 25, { n: 0 });
    g.probes(rand, W, H, 60, 60, scale);
    const ordered = [...g.ref.m.values()].sort((a, b) => a.minX - b.minX || a.minY - b.minY || cmpId(a.id, b.id));
    const stop = Math.floor(ordered.length / 20);
    let d = 0;
    for (const o of ordered) {
      g.rm(o.id);
      if (++d % 150 === 0 && g.ref.m.size > 0) g.check(rand, W, H, 4, 4, scale);
      if (g.ref.m.size === stop) break;
    }
    g.S();
    return g.ops;
  };
}

SC.moving = () => {
  const rand = rng(99);
  const g = new Gen();
  const regions = [[0, 0], [90000, 0], [0, 90000], [90000, 90000], [45000, 45000]];
  for (let i = 0; i < 700; i++) g.ins({ id: `m${i}`, ...box(rand() * 400, rand() * 400, 2 + rand() * 8, 2 + rand() * 8), z: i % 4 });
  for (let step = 0; step < 4500; step++) {
    const r = regions[Math.floor(rand() * regions.length)];
    g.upd(`m${Math.floor(rand() * 700)}`, box(r[0] + rand() * 400, r[1] + rand() * 400, 2 + rand() * 8, 2 + rand() * 8));
    if (step % 150 === 0) {
      g.S();
      g.probes(rand, 91000, 91000, 4, 4, 50);
      for (const r2 of regions) g.q(box(r2[0], r2[1], 400, 400));
    }
  }
  g.S();
  return g.ops;
};

SC.drain = () => {
  const rand = rng(5);
  const g = new Gen();
  for (let cycle = 0; cycle < 3; cycle++) {
    for (let i = 0; i < 1500; i++) g.ins({ id: `r${cycle}_${i}`, ...box(rand() * 3000, rand() * 3000, rand() * 20, rand() * 20), z: i % 5 });
    g.S();
    const ids = [...g.ref.m.keys()];
    for (let i = ids.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [ids[i], ids[j]] = [ids[j], ids[i]]; }
    let k = 0;
    for (const id of ids) {
      g.rm(id);
      if (++k % 100 === 0 && g.ref.m.size) g.check(rand, 3000, 3000, 3, 3, 40);
    }
    g.S();
    g.q({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 });
    g.h(5, 5);
  }
  return g.ops;
};

SC.zorder = () => {
  // z changes, ties and stacked layers: the per-node top shape must follow every kind of change
  const rand = rng(31);
  const g = new Gen();
  for (let i = 0; i < 1500; i++) g.ins({ id: `z${i}`, ...box(rand() * 500, rand() * 500, 20 + rand() * 200, 20 + rand() * 200), z: Math.floor(rand() * 12) });
  g.check(rand, 800, 800, 20, 60, 30);
  for (let step = 0; step < 3000; step++) {
    const ids = [...g.ref.m.keys()];
    const id = ids[Math.floor(rand() * ids.length)];
    const r = rand();
    if (r < 0.35) { const o = g.ref.m.get(id); g.upd(id, { minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: Math.floor(rand() * 12) }); }
    else if (r < 0.55) g.rm(id);
    else if (r < 0.75) g.upd(id, box(rand() * 500, rand() * 500, 20 + rand() * 200, 20 + rand() * 200));
    else g.ins({ id: `zz${step}`, ...box(rand() * 500, rand() * 500, 20 + rand() * 200, 20 + rand() * 200), z: Math.floor(rand() * 12) });
    if (step % 60 === 0) { g.S(); for (let k = 0; k < 40; k++) g.h(rand() * 800, rand() * 800); }
  }
  g.check(rand, 800, 800, 20, 80, 30);
  return g.ops;
};

SC.tamper = () => {
  const rand = rng(3);
  const g = new Gen();
  for (let i = 0; i < 800; i++) g.ins({ id: `t${i}`, ...box(rand() * 2000, rand() * 2000, 5 + rand() * 10, 5 + rand() * 10), z: 0 });
  g.ops.push(['T']);
  return g.ops;
};

function budgetOp(g, rand, nq, np) {
  const list = [...g.ref.m.values()];
  const rects = [], pts = [];
  for (let i = 0; i < nq; i++) { const o = list[Math.floor(rand() * list.length)]; rects.push(box(o.minX - 10, o.minY - 10, 40, 40)); }
  for (let i = 0; i < np; i++) {
    if (rand() < 0.5) { const o = list[Math.floor(rand() * list.length)]; pts.push([o.minX + (o.maxX - o.minX) / 2, o.minY + (o.maxY - o.minY) / 2]); }
    else pts.push([rand() * 10000, rand() * 10000]);
  }
  g.ops.push(['B', rects, pts]);
}

SC.perf = () => {
  const rand = rng(2024);
  const g = new Gen();
  for (let i = 0; i < 40000; i++) g.ins({ id: `p${i}`, ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) });
  for (let c = 0; c < 20; c++) {
    const cx = rand() * 9500, cy = rand() * 9500;
    for (let i = 0; i < 500; i++) g.ins({ id: `k${c}_${i}`, ...box(cx + rand() * 300, cy + rand() * 300, 2 + rand() * 12, 2 + rand() * 12), z: Math.floor(rand() * 8) });
  }
  g.S();
  budgetOp(g, rand, 400, 200);
  for (let i = 0; i < 20000; i++) {
    const id = rand() < 0.5 ? `p${Math.floor(rand() * 40000)}` : `k${Math.floor(rand() * 20)}_${Math.floor(rand() * 500)}`;
    g.upd(id, box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20));
  }
  const ids = [...g.ref.m.keys()];
  for (let i = 0; i < 10000; i++) {
    const j = i + Math.floor(rand() * (ids.length - i));
    [ids[i], ids[j]] = [ids[j], ids[i]];
    g.rm(ids[i]);
  }
  for (let i = 0; i < 10000; i++) g.ins({ id: `q${i}`, ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) });
  g.S();
  budgetOp(g, rand, 400, 200);
  return g.ops;
};

SC.perf_hit = () => {
  // stacked layers: hundreds of large shapes cover any point, so only z-aware pruning is cheap
  const rand = rng(77);
  const g = new Gen();
  const big = () => box(rand() * 10000, rand() * 10000, 1500 + rand() * 3000, 1500 + rand() * 3000);
  for (let i = 0; i < 8000; i++) g.ins({ id: `L${i}`, ...big(), z: Math.floor(rand() * 400) });
  for (let i = 0; i < 30000; i++) g.ins({ id: `s${i}`, ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 400) });
  g.S();
  const pts = () => { const p = []; for (let i = 0; i < 300; i++) p.push([rand() * 10000, rand() * 10000]); return p; };
  g.ops.push(['B', [], pts()]);
  for (let i = 0; i < 12000; i++) {
    const r = rand();
    if (r < 0.4) g.upd(`L${Math.floor(rand() * 8000)}`, big());
    else if (r < 0.7) { const id = `L${Math.floor(rand() * 8000)}`; const o = g.ref.m.get(id); if (o) g.upd(id, { minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: Math.floor(rand() * 400) }); }
    else g.upd(`s${Math.floor(rand() * 30000)}`, { ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 400) });
  }
  for (let i = 0; i < 3000; i++) { const id = `L${Math.floor(rand() * 8000)}`; if (g.ref.m.has(id)) g.rm(id); }
  for (let i = 0; i < 3000; i++) g.ins({ id: `N${i}`, ...big(), z: Math.floor(rand() * 400) });
  g.S();
  g.ops.push(['B', [], pts()]);
  return g.ops;
};

// ------------------------------------------------------------------ judge
function sameBox(a, b) { return (a === null && b === null) || (a && b && a.minX === b.minX && a.minY === b.minY && a.maxX === b.maxX && a.maxY === b.maxY); }

function mbrOf(items, isNode) {
  let b = null;
  for (const it of items) {
    const x = isNode ? it.bounds : it;
    b = b ? { minX: Math.min(b.minX, x.minX), minY: Math.min(b.minY, x.minY), maxX: Math.max(b.maxX, x.maxX), maxY: Math.max(b.maxY, x.maxY) } : { minX: x.minX, minY: x.minY, maxX: x.maxX, maxY: x.maxY };
  }
  return b;
}

function checkStructure(dump, size, ref) {
  const root = dump;
  assert(root && typeof root.leaf === 'boolean' && Array.isArray(root.children), 'tree.root has no node shape');
  const seen = new Set();
  let leafDepth = -1;
  (function walk(n, depth, isRoot) {
    const cnt = n.children.length;
    assert(cnt <= MAX, `node over capacity (${cnt})`);
    if (!isRoot) assert(cnt >= MIN, `non-root node underfull (${cnt})`);
    else if (!n.leaf) assert(cnt >= 2, 'internal root with fewer than 2 children');
    let top = null;
    if (cnt === 0) {
      assert(isRoot && n.leaf, 'empty non-root node');
      assert(n.bounds === null, 'empty root must have null bounds');
      assert(n.top === null, 'empty root must have top === null');
      return;
    }
    const exp = mbrOf(n.children, !n.leaf);
    assert(sameBox(n.bounds, exp), `node bounds are not the exact MBR of its children (have ${JSON.stringify(n.bounds)}, want ${JSON.stringify(exp)})`);
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
        if (better(e, top)) top = e;
      }
    } else {
      for (const c of n.children) {
        assert(c && Array.isArray(c.children) && typeof c.leaf === 'boolean', 'internal child is not a node');
        walk(c, depth + 1, false);
        if (better(c.top, top)) top = c.top;
      }
    }
    assert(n.top && typeof n.top === 'object' && n.top.id === top.id && n.top.z === top.z, `node.top is ${JSON.stringify(n.top)}, expected the highest-z (then smallest id) shape below it, ${top.id}`);
  })(root, 0, true);
  assert(seen.size === ref.m.size, `tree holds ${seen.size} objects, scene has ${ref.m.size}`);
  assert(size === ref.m.size, `size is ${size}, expected ${ref.m.size}`);
}

function simulateRect(root, r) {
  let n = 0;
  (function go(node) {
    n++;
    if (node.leaf) { n += node.children.length; return; }
    for (const c of node.children) if (ov(c.bounds, r)) go(c);
  })(root);
  return root.bounds && ov(root.bounds, r) ? n : 0;
}

function simulateHit(root, x, y) {
  let n = 0;
  const best = { e: null };
  (function go(node) {
    n++;
    if (node.leaf) {
      for (const e of node.children) { n++; if (cp(e, x, y) && better(e, best.e)) best.e = e; }
      return;
    }
    const cand = node.children.filter((c) => cp(c.bounds, x, y));
    cand.sort((a, b) => (better(a.top, b.top) ? -1 : 1));
    for (const c of cand) { if (!better(c.top, best.e)) break; go(c); }
  })(root);
  return n;
}

function listMatches(got, exp, what) {
  assert(Array.isArray(got), `${what}: queryRect did not return an array`);
  assert(got.length === exp.length, `${what}: got ${got.length} objects, expected ${exp.length}`);
  for (let i = 0; i < exp.length; i++) assert(rowKey(got[i]) === key(exp[i]).split('|').join('|'), `${what}: result ${i} is ${got[i]}, expected ${key(exp[i])}`);
}

function judge(ops, res) {
  let ref = new Ref();
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    const r = res[i];
    assert(r, `no observation for step ${i} (${op[0]}); the candidate crashed or hung`);
    assert(!r.error, `step ${i} (${op[0]}) crashed: ${r.error}`);
    const at = `step ${i}`;
    switch (op[0]) {
      case 'new': ref = new Ref(); break;
      case 'ins': {
        const exp = !(validBounds(op[1]) && typeof op[1].id === 'string' && !ref.m.has(op[1].id));
        assert(r.threw === exp, `${at}: insert(${op[1].id}) ${exp ? 'must throw' : 'must not throw'}`);
        if (!exp) ref.insert(op[1]);
        break;
      }
      case 'upd': {
        const exp = !(ref.m.has(op[1]) && validBounds(op[2]));
        assert(r.threw === exp, `${at}: update(${op[1]}) ${exp ? 'must throw' : 'must not throw'}`);
        if (!exp) ref.update(op[1], op[2]);
        break;
      }
      case 'rm': {
        const exp = ref.remove(op[1]);
        assert(r.v === exp, `${at}: remove(${op[1]}) returned ${r.v}, expected ${exp}`);
        break;
      }
      case 'q': listMatches(r.list, ref.query(op[1]), `${at}: queryRect ${JSON.stringify(op[1])}`); break;
      case 'h': {
        const exp = ref.hit(op[1], op[2]);
        assert((r.v === null) === (exp === null), `${at}: hitTest(${op[1]},${op[2]}) returned ${r.v && r.v[0]}, expected ${exp && exp.id}`);
        if (exp) assert(rowKey(r.v) === key(exp), `${at}: hitTest(${op[1]},${op[2]}) returned ${r.v}, expected ${key(exp)}`);
        break;
      }
      case 'S': checkStructure(r.dump, r.size, ref); break;
      case 'M': break;
      case 'T': {
        assert(ref.m.has(r.id), 'tamper: unknown entry');
        assert(Array.isArray(r.list) && !r.list.some((x) => x[0] === r.id), 'queryRect is not answered from the tree structure');
        break;
      }
      case 'B': {
        assert(!r.trap, `${at}: a query iterated over the whole scene`);
        checkStructure(r.dump, r.size, ref);
        for (let k = 0; k < op[1].length; k++) {
          const it = r.items[k];
          listMatches(it.list, ref.query(op[1][k]), `${at}: localized queryRect`);
          assert(it.rep <= RECT_BUDGET, `${at}: queryRect reported ${it.rep} inspections (budget ${RECT_BUDGET})`);
          const sim = simulateRect(r.dump, op[1][k]);
          assert(sim <= RECT_BUDGET, `${at}: a localized query needs ${sim} inspections on your tree (budget ${RECT_BUDGET}); the tree is badly shaped or its bounds are stale`);
        }
        for (let k = 0; k < op[2].length; k++) {
          const [x, y] = op[2][k];
          const exp = ref.hit(x, y);
          const hv = r.hits[k].v;
          assert((hv === null) === (exp === null) && (!exp || rowKey(hv) === key(exp)), `${at}: hitTest(${x},${y}) mismatch`);
          assert(r.hits[k].rep <= HIT_BUDGET, `${at}: hitTest reported ${r.hits[k].rep} inspections (budget ${HIT_BUDGET})`);
          const sim = simulateHit(r.dump, x, y);
          assert(sim <= HIT_BUDGET, `${at}: a hit test needs ${sim} inspections on your tree (budget ${HIT_BUDGET}); is node.top used to prune?`);
        }
        break;
      }
      default: throw new Error('bad op');
    }
  }
}

function main() {
  const [, , name, file, scratch] = process.argv;
  try {
    assert(SC[name], `unknown scenario ${name}`);
    const ops = SC[name]();
    fs.copyFileSync(path.join(__dirname, 'runner.js'), path.join(scratch, 'runner.js'));
    fs.writeFileSync(path.join(scratch, 'trace.json'), JSON.stringify(ops));
    fs.copyFileSync(file, path.join(scratch, 'rtree.js'));
    for (const f of ['runner.js', 'trace.json', 'rtree.js']) fs.chmodSync(path.join(scratch, f), 0o644);
    const out = path.join(scratch, 'out.json');
    fs.chmodSync(scratch, 0o777);
    const args = ['--max-old-space-size=1500', 'runner.js', 'rtree.js', 'trace.json', 'out.json'];
    const cmd = process.geteuid && process.geteuid() === 0
      ? ['setpriv', ['--reuid=65534', '--regid=65534', '--clear-groups', '--no-new-privs', 'node', ...args]]
      : ['node', args];
    const p = spawnSync(cmd[0], cmd[1], { cwd: scratch, timeout: 540000, env: { PATH: process.env.PATH, HOME: scratch }, stdio: 'ignore' });
    assert(!p.error, `candidate process failed: ${p.error && p.error.code}`);
    assert(fs.existsSync(out) && fs.statSync(out).size < 400e6, 'candidate produced no usable observations (crash, hang or exit during the run)');
    let res;
    try { res = JSON.parse(fs.readFileSync(out, 'utf8')); } catch (e) { throw new Fail('observations unreadable'); }
    assert(Array.isArray(res), 'observations malformed');
    judge(ops, res);
    console.log(JSON.stringify({ ok: true }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String((e && e.message) || e).slice(0, 600), kind: e instanceof Fail ? 'fail' : 'crash' }));
  }
}

main();
