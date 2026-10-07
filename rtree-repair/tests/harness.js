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
const NEW_PER_OP = Number(process.env.RT_NEW_PER_OP || 40);
const TOP_BUDGET = Number(process.env.RT_TOP_BUDGET || 600);
const SORT_BUDGET = Number(process.env.RT_SORT_BUDGET || 400);
const NN_BUDGET = Number(process.env.RT_NN_BUDGET || 250);
const RR_FACTOR = Number(process.env.RT_RR_FACTOR || 3);
const RR_BASE = Number(process.env.RT_RR_BASE || 80);
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
  qt(r, k) { this.ops.push(['qt', r, k]); }
  rr(rect, budget) {
    this.ops.push(['rr', rect].concat(budget || []));
    for (const o of [...this.ref.m.values()]) if (rect.minX <= o.minX && o.maxX <= rect.maxX && rect.minY <= o.minY && o.maxY <= rect.maxY) this.ref.remove(o.id);
  }
  nn(x, y, k, budget) { this.ops.push(['nn', x, y, k, budget]); }
  diff(a, b, strict) { this.ops.push(['diff', a, b, strict]); }
  snap(name) { this.ops.push(['snap', name]); this.snaps = this.snaps || {}; const c = new Ref(); for (const [k, v] of this.ref.m) c.m.set(k, v); this.snaps[name] = c; }
  restore(name) { this.ops.push(['restore', name]); const c = new Ref(); for (const [k, v] of this.snaps[name].m) c.m.set(k, v); this.ref = c; }
  SV(name) { this.ops.push(['SV', name]); }
  probesV(rand, name, W, H, nr, np, scale) {
    const list = [...this.snaps[name].m.values()];
    for (let i = 0; i < nr; i++) {
      const o = list.length && rand() < 0.5 ? list[Math.floor(rand() * list.length)] : null;
      if (o) this.ops.push(['qv', name, { minX: o.minX - 1, minY: o.minY - 1, maxX: o.maxX + scale, maxY: o.maxY + scale }]);
      else { const x = rand() * W, y = rand() * H; this.ops.push(['qv', name, { minX: x, minY: y, maxX: x + scale * 3, maxY: y + scale * 3 }]); }
    }
    for (let i = 0; i < np; i++) {
      const o = list.length && rand() < 0.7 ? list[Math.floor(rand() * list.length)] : null;
      if (o) this.ops.push(['hv', name, (o.minX + o.maxX) / 2, (o.minY + o.maxY) / 2]);
      else this.ops.push(['hv', name, rand() * W, rand() * H]);
    }
  }
  NEW(a, b, limit) { this.ops.push(['NEW', a, b, limit]); }
  fresh() { this.ops.push(['new']); this.ref = new Ref(); this.snaps = {}; }
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

SC.persist = () => {
  // every op is bracketed by snapshots: untouched nodes must be shared, the old version must stay intact
  const rand = rng(404);
  const g = new Gen();
  const W = 4000, H = 4000;
  for (let i = 0; i < 3000; i++) g.ins({ id: `e${i}`, ...box(rand() * W, rand() * H, 2 + rand() * 30, 2 + rand() * 30), z: Math.floor(rand() * 9) });
  let prev = 'v0';
  g.snap(prev); g.SV(prev);
  const names = [prev];
  for (let i = 1; i <= 160; i++) {
    const ids = [...g.ref.m.keys()];
    const r = rand();
    if (r < 0.3) g.rm(ids[Math.floor(rand() * ids.length)]);
    else if (r < 0.7) g.upd(ids[Math.floor(rand() * ids.length)], { ...box(rand() * W, rand() * H, 2 + rand() * 30, 2 + rand() * 30), z: Math.floor(rand() * 9) });
    else g.ins({ id: `f${i}`, ...box(rand() * W, rand() * H, 2 + rand() * 30, 2 + rand() * 30), z: Math.floor(rand() * 9) });
    const cur = `v${i}`;
    g.snap(cur); g.SV(cur);
    g.NEW(prev, cur, NEW_PER_OP);
    names.push(cur);
    prev = cur;
  }
  for (const n of names.filter((_, i) => i % 7 === 0)) { g.SV(n); g.probesV(rand, n, W, H, 12, 12, 30); }
  g.S();
  return g.ops;
};

SC.undo = () => {
  // undo/redo: restore() to old versions, branch off them, keep every version intact
  const rand = rng(515);
  const g = new Gen();
  const W = 3000, H = 3000;
  const rid = () => `u${Math.floor(rand() * 1e9)}`;
  const mutate = (n) => {
    for (let i = 0; i < n; i++) {
      const ids = [...g.ref.m.keys()];
      const r = rand();
      if (r < 0.3 && ids.length > 20) g.rm(ids[Math.floor(rand() * ids.length)]);
      else if (r < 0.65 && ids.length) g.upd(ids[Math.floor(rand() * ids.length)], { ...box(rand() * W, rand() * H, 2 + rand() * 40, 2 + rand() * 40), z: Math.floor(rand() * 9) });
      else g.ins({ id: rid(), ...box(rand() * W, rand() * H, 2 + rand() * 40, 2 + rand() * 40), z: Math.floor(rand() * 9) });
    }
  };
  mutate(1500);
  const names = [];
  for (let round = 0; round < 25; round++) {
    const n = `r${round}`;
    g.snap(n); names.push(n);
    mutate(40 + Math.floor(rand() * 200));
    g.check(rand, W, H, 8, 8, 30);
    if (round % 3 === 2) {
      const back = names[Math.floor(rand() * names.length)];
      g.restore(back);
      g.S();
      g.probes(rand, W, H, 10, 10, 30);
      mutate(60);
      g.check(rand, W, H, 8, 8, 30);
    }
  }
  for (const n of names) { g.SV(n); g.probesV(rand, n, W, H, 10, 10, 30); }
  return g.ops;
};

SC.topk = () => {
  const rand = rng(61);
  const g = new Gen();
  for (let i = 0; i < 1500; i++) g.ins({ id: `k${i}`, ...box(rand() * 600, rand() * 600, 5 + rand() * 150, 5 + rand() * 150), z: Math.floor(rand() * 15) });
  const qts = () => { for (let i = 0; i < 25; i++) { const x = rand() * 600, y = rand() * 600; g.qt({ minX: x, minY: y, maxX: x + rand() * 120, maxY: y + rand() * 120 }, 1 + Math.floor(rand() * 8)); } };
  g.S(); qts();
  g.qt({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 }, 5); g.qt({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 }, 5000); g.qt({ minX: 3, minY: 3, maxX: 3, maxY: 9 }, 2);
  for (let step = 0; step < 2500; step++) {
    const ids = [...g.ref.m.keys()];
    const id = ids[Math.floor(rand() * ids.length)];
    const r = rand();
    if (r < 0.3) { const o = g.ref.m.get(id); g.upd(id, { minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: Math.floor(rand() * 15) }); }
    else if (r < 0.5) g.rm(id);
    else if (r < 0.75) g.upd(id, box(rand() * 600, rand() * 600, 5 + rand() * 150, 5 + rand() * 150));
    else g.ins({ id: `kk${step}`, ...box(rand() * 600, rand() * 600, 5 + rand() * 150, 5 + rand() * 150), z: Math.floor(rand() * 15) });
    if (step % 100 === 0) { g.S(); qts(); }
  }
  g.snap('a'); g.upd([...g.ref.m.keys()][0], box(1, 1, 5, 5)); g.rm([...g.ref.m.keys()][3]);
  for (let i = 0; i < 20; i++) g.ops.push(['qtv', 'a', box(rand() * 500, rand() * 500, 100, 100), 4]);
  return g.ops;
};

SC.extreme = () => {
  // coordinates anywhere in the finite double range: naive area/enlargement arithmetic overflows
  const rand = rng(8);
  const g = new Gen();
  const MAXV = Number.MAX_VALUE;
  const val = () => {
    const r = rand();
    if (r < 0.04) return 0;
    if (r < 0.06) return -0;
    if (r < 0.08) return Number.MIN_VALUE;
    if (r < 0.10) return MAXV;
    if (r < 0.12) return -MAXV;
    const sgn = rand() < 0.5 ? -1 : 1;
    return sgn * Math.pow(10, -300 + rand() * 608);
  };
  const shape = () => {
    let a = val(), b = val(), c = val(), d = val();
    if (rand() < 0.3) { b = a; }
    if (rand() < 0.2) { d = c; }
    return { minX: Math.min(a, b), maxX: Math.max(a, b), minY: Math.min(c, d), maxY: Math.max(c, d) };
  };
  const ids = [];
  for (let i = 0; i < 700; i++) { const o = { id: `x${i}`, ...shape(), z: Math.floor(rand() * 5) }; g.ins(o); ids.push(o.id); }
  const probe = () => {
    for (let i = 0; i < 12; i++) g.q(shape());
    for (let i = 0; i < 8; i++) g.qt(shape(), 3);
    const list = [...g.ref.m.values()];
    for (let i = 0; i < 12; i++) { const o = list[Math.floor(rand() * list.length)]; g.h(o.minX, o.minY); g.h(o.maxX, o.maxY); g.h(val(), val()); }
  };
  g.S(); probe();
  for (let step = 0; step < 1500; step++) {
    const live = [...g.ref.m.keys()];
    const id = live[Math.floor(rand() * live.length)];
    const r = rand();
    if (r < 0.3) g.rm(id);
    else if (r < 0.65) g.upd(id, { ...shape(), z: Math.floor(rand() * 5) });
    else g.ins({ id: `y${step}`, ...shape(), z: Math.floor(rand() * 5) });
    if (step % 50 === 0) { g.S(); probe(); }
  }
  g.S(); probe();
  return g.ops;
};

SC.removerect = () => {
  const rand = rng(1212);
  const g = new Gen();
  const W = 2000, H = 2000;
  const mk = (id) => ({ id, ...box(Math.floor(rand() * W), Math.floor(rand() * H), Math.floor(rand() * 40), Math.floor(rand() * 40)), z: Math.floor(rand() * 6) });
  for (let i = 0; i < 2500; i++) g.ins(mk(`r${i}`));
  // zero-area shapes exactly on the edges of a rectangle that will be removed
  for (let i = 0; i < 30; i++) g.ins({ id: `edge${i}`, minX: 1000, maxX: 1000, minY: 100 + i * 10, maxY: 100 + i * 10, z: 1 });
  g.check(rand, W, H, 10, 10, 30);
  g.snap('before');
  g.rr({ minX: 900, minY: 90, maxX: 1000, maxY: 400 });
  g.S(); g.probes(rand, W, H, 20, 20, 30);
  g.snap('after-edge');
  for (let step = 0; step < 90; step++) {
    const r = rand();
    const x = Math.floor(rand() * W), y = Math.floor(rand() * H);
    const sz = r < 0.5 ? 60 : r < 0.85 ? 300 : 900;
    g.rr({ minX: x, minY: y, maxX: x + sz, maxY: y + sz });
    for (let k = 0; k < 6; k++) g.ins(mk(`n${step}_${k}`));
    for (let k = 0; k < 4; k++) { const ids = [...g.ref.m.keys()]; if (ids.length) g.upd(ids[Math.floor(rand() * ids.length)], box(Math.floor(rand() * W), Math.floor(rand() * H), 30, 30)); }
    if (step % 3 === 0) g.check(rand, W, H, 6, 6, 30);
    if (step === 40) g.snap('mid');
  }
  g.rr({ minX: 0, minY: 0, maxX: 2500, maxY: 1200 }); g.S();
  g.rr({ minX: 5, minY: 5, maxX: 5, maxY: 5 }); g.rr({ minX: -9, minY: -9, maxX: -1, maxY: -1 });
  g.restore('before'); g.S(); g.probes(rand, W, H, 20, 20, 30);
  g.rr({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 });
  g.S(); g.q({ minX: -1e9, minY: -1e9, maxX: 1e9, maxY: 1e9 }); g.h(5, 5);
  for (const n of ['before', 'after-edge', 'mid']) { g.SV(n); g.probesV(rand, n, W, H, 8, 8, 30); }
  g.restore('mid'); g.S();
  for (let k = 0; k < 40; k++) g.ins(mk(`late${k}`));
  g.S();
  return g.ops;
};

SC.nearest = () => {
  const rand = rng(2323);
  const g = new Gen();
  const W = 3000, H = 3000;
  const mk = (id) => ({ id, ...box(Math.floor(rand() * W), Math.floor(rand() * H), rand() < 0.2 ? 0 : Math.floor(rand() * 30), rand() < 0.2 ? 0 : Math.floor(rand() * 30)), z: Math.floor(rand() * 5) });
  for (let i = 0; i < 2500; i++) g.ins(mk(i % 7 === 0 ? `Q${i}` : `q${i}`));
  const probe = () => { for (let i = 0; i < 25; i++) g.nn(Math.floor(rand() * W), Math.floor(rand() * H), 1 + Math.floor(rand() * 10)); const list = [...g.ref.m.values()]; for (let i = 0; i < 10; i++) { const o = list[Math.floor(rand() * list.length)]; g.nn(o.minX, o.minY, 4); g.nn(o.maxX, o.maxY, 3); } };
  g.S(); probe();
  g.nn(-500, -500, 5); g.nn(1500, 1500, 100000); g.nn(0, 0, 1);
  for (let step = 0; step < 1500; step++) {
    const ids = [...g.ref.m.keys()];
    const id = ids[Math.floor(rand() * ids.length)];
    const r = rand();
    if (r < 0.3) g.rm(id); else if (r < 0.65) g.upd(id, box(Math.floor(rand() * W), Math.floor(rand() * H), Math.floor(rand() * 30), Math.floor(rand() * 30))); else g.ins(mk(`m${step}`));
    if (step % 60 === 0) { g.S(); probe(); }
  }
  g.snap('s');
  for (let i = 0; i < 100; i++) g.rm([...g.ref.m.keys()][0]);
  for (let i = 0; i < 20; i++) g.ops.push(['nnv', 's', Math.floor(rand() * W), Math.floor(rand() * H), 1 + Math.floor(rand() * 6)]);
  return g.ops;
};

SC.versions = () => {
  // branching history: edits, snapshots, restores, more edits; diff between any two versions
  const rand = rng(3434);
  const g = new Gen();
  const W = 3000, H = 3000;
  const mk = (id) => ({ id, ...box(rand() * W, rand() * H, 3 + rand() * 40, 3 + rand() * 40), z: Math.floor(rand() * 6) });
  const edits = (n, tag) => {
    for (let i = 0; i < n; i++) {
      const ids = [...g.ref.m.keys()];
      const r = rand();
      if (r < 0.25 && ids.length > 50) g.rm(ids[Math.floor(rand() * ids.length)]);
      else if (r < 0.6) g.upd(ids[Math.floor(rand() * ids.length)], { ...box(rand() * W, rand() * H, 3 + rand() * 40, 3 + rand() * 40), z: Math.floor(rand() * 6) });
      else if (r < 0.7) { const id = ids[Math.floor(rand() * ids.length)]; const o = g.ref.m.get(id); g.upd(id, { minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: o.z + 1 }); }
      else if (r < 0.75) { const x = rand() * W, y = rand() * H; g.rr({ minX: x, minY: y, maxX: x + 120, maxY: y + 120 }); }
      else g.ins(mk(`${tag}${i}`));
    }
  };
  for (let i = 0; i < 1500; i++) g.ins(mk(`b${i}`));
  g.snap('base');
  edits(60, 'a'); g.snap('A1'); edits(40, 'aa'); g.snap('A2');
  g.restore('base'); edits(50, 'c'); g.snap('B1');
  g.restore('A1'); edits(30, 'd'); g.snap('C1');
  g.restore('base'); g.snap('base2');
  const names = ['base', 'A1', 'A2', 'B1', 'C1', 'base2'];
  for (const a of names) for (const b of names) g.diff(a, b);
  g.diff('A2', '@'); g.diff('@', 'B1'); g.diff('@', '@');
  edits(25, 'e');
  g.diff('base', '@'); g.diff('C1', '@'); g.diff('@', 'A2');
  g.S();
  return g.ops;
};

SC.perf_ops = () => {
  // large scene: nearest, bulk removal and version diff on 50,000 shapes
  const rand = rng(4545);
  const g = new Gen();
  const mk = (id) => ({ id, ...box(Math.floor(rand() * 20000), Math.floor(rand() * 20000), 1 + Math.floor(rand() * 40), 1 + Math.floor(rand() * 40)), z: Math.floor(rand() * 8) });
  for (let i = 0; i < 50000; i++) g.ins(mk(`p${i}`));
  g.S();
  const nns = []; for (let i = 0; i < 150; i++) nns.push([Math.floor(rand() * 20000), Math.floor(rand() * 20000)]);
  g.ops.push(['B', [], [], [], 3, undefined, undefined, nns]);
  g.snap('v0');
  for (let i = 0; i < 40; i++) { const ids = [...g.ref.m.keys()]; g.upd(ids[Math.floor(rand() * ids.length)], box(rand() * 20000, rand() * 20000, 30, 30)); }
  g.snap('v1');
  g.diff('v0', 'v1'); g.diff('v1', 'v0');
  g.restore('v0');
  for (let i = 0; i < 25; i++) g.ins(mk(`new${i}`));
  g.snap('v2');
  g.diff('v1', 'v2'); g.diff('v2', '@');
  for (let i = 0; i < 5; i++) {
    g.S();
    const x = Math.floor(rand() * 19000), y = Math.floor(rand() * 19000);
    g.rr({ minX: x, minY: y, maxX: x + 600, maxY: y + 600 }, [RR_FACTOR, RR_BASE]);
    g.S();
  }
  g.diff('v2', '@');
  return g.ops;
};

SC.tamper = () => {
  const rand = rng(3);
  const g = new Gen();
  for (let i = 0; i < 800; i++) g.ins({ id: `t${i}`, ...box(rand() * 2000, rand() * 2000, 5 + rand() * 10, 5 + rand() * 10), z: 0 });
  g.ops.push(['T']);
  return g.ops;
};

function budgetOp(g, rand, nq, np, nt, rectBudget) {
  const list = [...g.ref.m.values()];
  const rects = [], pts = [], tops = [];
  for (let i = 0; i < (nt || 0); i++) { const o = list[Math.floor(rand() * list.length)]; tops.push(box(o.minX - 60, o.minY - 60, 120, 120)); }
  for (let i = 0; i < nq; i++) { const o = list[Math.floor(rand() * list.length)]; rects.push(box(o.minX - 10, o.minY - 10, 40, 40)); }
  for (let i = 0; i < np; i++) {
    if (rand() < 0.5) { const o = list[Math.floor(rand() * list.length)]; pts.push([o.minX + (o.maxX - o.minX) / 2, o.minY + (o.maxY - o.minY) / 2]); }
    else pts.push([rand() * 10000, rand() * 10000]);
  }
  g.ops.push(['B', rects, pts, tops, 3, undefined, rectBudget]);
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

SC.perf_sorted = () => {
  // shapes arrive in sweep order, the worst case for naive insertion heuristics and split rules
  const rand = rng(909);
  const g = new Gen();
  for (let i = 0; i < 30000; i++) g.ins({ id: `w${i}`, ...box(i / 3 + rand(), rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) });
  for (let i = 0; i < 20000; i++) g.ins({ id: `v${i}`, ...box(rand() * 10000, i / 2 + rand(), 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 8) });
  g.S();
  budgetOp(g, rand, 400, 100, 0, SORT_BUDGET);
  for (let i = 0; i < 15000; i++) g.upd(`w${i * 2}`, box(10000 - i / 2, 10000 - i / 3, 2 + rand() * 20, 2 + rand() * 20));
  g.S();
  budgetOp(g, rand, 400, 100, 0, SORT_BUDGET);
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
  const tpts = () => { const p = []; for (let i = 0; i < 150; i++) p.push(box(rand() * 10000, rand() * 10000, 100, 100)); return p; };
  g.ops.push(['B', [], pts(), tpts(), 3]);
  for (let i = 0; i < 12000; i++) {
    const r = rand();
    if (r < 0.4) g.upd(`L${Math.floor(rand() * 8000)}`, big());
    else if (r < 0.7) { const id = `L${Math.floor(rand() * 8000)}`; const o = g.ref.m.get(id); if (o) g.upd(id, { minX: o.minX, minY: o.minY, maxX: o.maxX, maxY: o.maxY, z: Math.floor(rand() * 400) }); }
    else g.upd(`s${Math.floor(rand() * 30000)}`, { ...box(rand() * 10000, rand() * 10000, 2 + rand() * 20, 2 + rand() * 20), z: Math.floor(rand() * 400) });
  }
  for (let i = 0; i < 3000; i++) { const id = `L${Math.floor(rand() * 8000)}`; if (g.ref.m.has(id)) g.rm(id); }
  for (let i = 0; i < 3000; i++) g.ins({ id: `N${i}`, ...big(), z: Math.floor(rand() * 400) });
  g.S();
  g.ops.push(['B', [], pts(), tpts(), 3]);
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

function expTop(ref, rect, k) {
  return ref.query(rect).sort((a, b) => (better(a, b) ? -1 : 1)).slice(0, k);
}

function simulateTop(root, rect, k) {
  let n = 0;
  if (!root.bounds || !ov(root.bounds, rect)) return 0;
  let heap = [{ t: root.top, node: root }];
  let out = 0;
  while (heap.length && out < k) {
    let bi = 0;
    for (let i = 1; i < heap.length; i++) if (better(heap[i].t, heap[bi].t)) bi = i;
    const it = heap.splice(bi, 1)[0];
    if (!it.node) { out++; continue; }
    n++;
    if (it.node.leaf) { for (const e of it.node.children) { n++; if (ov(e, rect)) heap.push({ t: e, node: null }); } }
    else for (const c of it.node.children) if (ov(c.bounds, rect)) heap.push({ t: c.top, node: c });
  }
  return n;
}

function simulateNearest(root, x, y, k) {
  const d2 = (b) => { const dx = Math.max(b.minX - x, 0, x - b.maxX), dy = Math.max(b.minY - y, 0, y - b.maxY); return dx * dx + dy * dy; };
  if (!root.bounds) return 0;
  const less = (a, b) => (a.d !== b.d ? a.d < b.d : a.node ? !b.node : b.node ? false : cmpId(a.e.id, b.e.id) < 0);
  const heap = [{ d: d2(root.bounds), node: root }];
  let n = 0, out = 0;
  while (heap.length && out < k) {
    let bi = 0;
    for (let i = 1; i < heap.length; i++) if (less(heap[i], heap[bi])) bi = i;
    const it = heap.splice(bi, 1)[0];
    if (!it.node) { out++; continue; }
    n++;
    if (it.node.leaf) for (const e of it.node.children) { n++; heap.push({ d: d2(e), e }); }
    else for (const c of it.node.children) heap.push({ d: d2(c.bounds), node: c });
  }
  return n;
}

function judge(ops, res) {
  let ref = new Ref();
  let snaps = {};
  const ids = {};
  let pendingRR = null;
  let lastNids = null;
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i];
    const r = res[i];
    assert(r, `no observation for step ${i} (${op[0]}); the candidate crashed or hung`);
    assert(!r.error, `step ${i} (${op[0]}) crashed: ${r.error}`);
    const at = `step ${i}`;
    switch (op[0]) {
      case 'new': ref = new Ref(); snaps = {}; break;
      case 'snap': { const c = new Ref(); for (const [k, v] of ref.m) c.m.set(k, v); snaps[op[1]] = c; break; }
      case 'restore': { const c = new Ref(); for (const [k, v] of snaps[op[1]].m) c.m.set(k, v); ref = c; break; }
      case 'SV': {
        checkStructure(r.dump, r.size, snaps[op[1]]);
        const set = new Set();
        (function w(n) { set.add(n.nid); if (!n.leaf) n.children.forEach(w); })(r.dump);
        ids[op[1]] = set;
        break;
      }
      case 'qv': listMatches(r.list, snaps[op[1]].query(op[2]), `${at}: queryRect on snapshot ${op[1]}`); if (r.list.length) assert(r.rep >= 1, `${at}: snapshot.stats does not count the work of queryRect`); break;
      case 'hv': {
        const exp = snaps[op[1]].hit(op[2], op[3]);
        assert((r.v === null) === (exp === null) && (!exp || rowKey(r.v) === key(exp)), `${at}: hitTest on snapshot ${op[1]} mismatch`);
        break;
      }
      case 'NEW': {
        let fresh = 0;
        for (const id of ids[op[2]]) if (!ids[op[1]].has(id)) fresh++;
        assert(fresh <= op[3], `${at}: one operation created ${fresh} new nodes (limit ${op[3]}); unchanged nodes must be shared between versions`);
        break;
      }
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
      case 'rr': {
        const hit = [...ref.m.values()].filter((o) => op[1].minX <= o.minX && o.maxX <= op[1].maxX && op[1].minY <= o.minY && o.maxY <= op[1].maxY);
        assert(r.n === hit.length, `${at}: removeRect returned ${r.n}, expected ${hit.length}`);
        for (const o of hit) ref.remove(o.id);
        if (op[2]) { assert(r.rep <= op[2] * hit.length + op[3], `${at}: removeRect of ${hit.length} shapes inspected ${r.rep} nodes and shapes (limit ${op[2] * hit.length + op[3]})`); pendingRR = { n: hit.length, f: op[2], b: op[3], at }; }
        break;
      }
      case 'nn': case 'nnv': {
        const src = op[0] === 'nn' ? ref : snaps[op[1]];
        const [x, y, k] = op[0] === 'nn' ? [op[1], op[2], op[3]] : [op[2], op[3], op[4]];
        const d2 = (o) => { const dx = Math.max(o.minX - x, 0, x - o.maxX), dy = Math.max(o.minY - y, 0, y - o.maxY); return dx * dx + dy * dy; };
        const exp = [...src.m.values()].sort((a, b) => d2(a) - d2(b) || cmpId(a.id, b.id)).slice(0, k);
        listMatches(r.list, exp, `${at}: nearest(${x},${y},${k})`);
        break;
      }
      case 'diff': {
        const A = op[1] === '@' ? ref : snaps[op[1]];
        const Bv = op[2] === '@' ? ref : snaps[op[2]];
        const exp = [];
        for (const id of new Set([...A.m.keys(), ...Bv.m.keys()])) {
          const a = A.m.get(id) || null, b = Bv.m.get(id) || null;
          if (!(a && b && key(a) === key(b))) exp.push([id, a, b]);
        }
        exp.sort((p, q) => cmpId(p[0], q[0]));
        assert(Array.isArray(r.list), `${at}: diff did not return an array`);
        assert(r.list.length === exp.length, `${at}: diff(${op[1]},${op[2]}) returned ${r.list.length} changes, expected ${exp.length}`);
        for (let k = 0; k < exp.length; k++) {
          const g = r.list[k];
          assert(g[0] === exp[k][0], `${at}: diff entry ${k} is ${g[0]}, expected ${exp[k][0]}`);
          assert((g[1] === null) === (exp[k][1] === null) && (!g[1] || rowKey(g[1]) === key(exp[k][1])), `${at}: diff before of ${exp[k][0]} is wrong`);
          assert((g[2] === null) === (exp[k][2] === null) && (!g[2] || rowKey(g[2]) === key(exp[k][2])), `${at}: diff after of ${exp[k][0]} is wrong`);
        }
        break;
      }
      case 'qt': listMatches(r.list, expTop(ref, op[1], op[2]), `${at}: queryTop ${JSON.stringify(op[1])} k=${op[2]}`); break;
      case 'qtv': listMatches(r.list, expTop(snaps[op[1]], op[2], op[3]), `${at}: queryTop on snapshot ${op[1]}`); break;
      case 'S': {
        checkStructure(r.dump, r.size, ref);
        const set = new Set();
        (function w(n) { set.add(n.nid); if (!n.leaf) n.children.forEach(w); })(r.dump);
        if (pendingRR) {
          assert(lastNids, `${at}: missing previous version to compare against`);
          let fresh = 0;
          for (const id of set) if (!lastNids.has(id)) fresh++;
          const lim = pendingRR.f * pendingRR.n + pendingRR.b;
          if (process.env.RT_REPORT) console.error(`rr n=${pendingRR.n} fresh=${fresh}`);
          assert(fresh <= lim, `${pendingRR.at}: removeRect of ${pendingRR.n} shapes created ${fresh} new nodes (limit ${lim}); untouched subtrees must be shared with the previous version`);
          pendingRR = null;
        }
        lastNids = set;
        break;
      }
      case 'M': break;
      case 'T': {
        assert(ref.m.has(r.id), 'tamper: unknown entry');
        assert(Array.isArray(r.list) && !r.list.some((x) => x[0] === r.id), 'queryRect is not answered from the tree structure');
        assert(Array.isArray(r.nn) && !r.nn.some((x) => x[0] === r.id), 'nearest is not answered from the tree structure');
        break;
      }
      case 'B': {
        assert(!r.trap, `${at}: a query iterated over the whole scene`);
        checkStructure(r.dump, r.size, ref);
        const rep = { rect: 0, hit: 0, top: 0 };
        for (let k = 0; k < op[1].length; k++) {
          const it = r.items[k];
          rep.rect += simulateRect(r.dump, op[1][k]) / op[1].length;
          listMatches(it.list, ref.query(op[1][k]), `${at}: localized queryRect`);
          const rb = op[6] || RECT_BUDGET;
          assert(it.rep <= rb, `${at}: queryRect reported ${it.rep} inspections (budget ${rb})`);
          const sim = simulateRect(r.dump, op[1][k]);
          assert(sim <= rb, `${at}: a localized query needs ${sim} inspections on your tree (budget ${RECT_BUDGET}); the tree is badly shaped or its bounds are stale`);
        }
        for (let k = 0; k < op[2].length; k++) {
          const [x, y] = op[2][k];
          const exp = ref.hit(x, y);
          const hv = r.hits[k].v;
          assert((hv === null) === (exp === null) && (!exp || rowKey(hv) === key(exp)), `${at}: hitTest(${x},${y}) mismatch`);
          assert(r.hits[k].rep <= HIT_BUDGET, `${at}: hitTest reported ${r.hits[k].rep} inspections (budget ${HIT_BUDGET})`);
          rep.hit += simulateHit(r.dump, x, y) / op[2].length;
          const sim = simulateHit(r.dump, x, y);
          assert(sim <= HIT_BUDGET, `${at}: a hit test needs ${sim} inspections on your tree (budget ${HIT_BUDGET}); is node.top used to prune?`);
        }
        for (let k = 0; k < (op[7] || []).length; k++) {
          const [x, y] = op[7][k];
          const d2 = (o) => { const dx = Math.max(o.minX - x, 0, x - o.maxX), dy = Math.max(o.minY - y, 0, y - o.maxY); return dx * dx + dy * dy; };
          const exp = [...ref.m.values()].sort((a, b) => d2(a) - d2(b) || cmpId(a.id, b.id)).slice(0, 5);
          listMatches(r.nns[k].list, exp, `${at}: nearest(${x},${y},5)`);
          assert(r.nns[k].rep <= NN_BUDGET, `${at}: nearest reported ${r.nns[k].rep} inspections (budget ${NN_BUDGET})`);
          const sim = simulateNearest(r.dump, x, y, 5);
          assert(sim <= NN_BUDGET, `${at}: nearest needs ${sim} inspections on your tree (budget ${NN_BUDGET})`);
        }
        const tb = op[5] || TOP_BUDGET;
        for (let k = 0; k < (op[3] || []).length; k++) {
          const t = r.tops[k];
          listMatches(t.list, expTop(ref, op[3][k], op[4]), `${at}: queryTop`);
          assert(t.rep <= tb, `${at}: queryTop reported ${t.rep} inspections (budget ${tb})`);
          rep.top += simulateTop(r.dump, op[3][k], op[4]) / op[3].length;
          const sim = simulateTop(r.dump, op[3][k], op[4]);
          assert(sim <= tb, `${at}: queryTop needs ${sim} inspections on your tree (budget ${tb})`);
        }
        if (process.env.RT_REPORT) console.error(`B@${i}: mean rect ${rep.rect.toFixed(1)} hit ${rep.hit.toFixed(1)} top ${rep.top.toFixed(1)}`);
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
    if (process.geteuid && process.geteuid() === 0) { fs.chownSync(scratch, 65534, 65534); fs.chmodSync(scratch, 0o700); } else fs.chmodSync(scratch, 0o700);
    const args = ['--max-old-space-size=1500', 'runner.js', 'rtree.js', 'trace.json', 'out.json'];
    const cmd = process.geteuid && process.geteuid() === 0
      ? ['setpriv', ['--reuid=65534', '--regid=65534', '--clear-groups', '--no-new-privs', 'node', ...args]]
      : ['node', args];
    const p = spawnSync(cmd[0], cmd[1], { cwd: scratch, timeout: 540000, env: { PATH: process.env.PATH, HOME: scratch }, stdio: 'ignore' });
    if (process.geteuid && process.geteuid() === 0) spawnSync('pkill', ['-9', '-u', '65534'], { stdio: 'ignore' });
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
