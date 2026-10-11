// Run: node --experimental-strip-types components/board/treemapLayout.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { squarify } from "./treemapLayout.ts";

const items = Array.from({ length: 40 }, (_, i) => ({ id: "S" + i, value: 1 + ((i * 37) % 17) }));
const W = 800, H = 300;
const r = squarify(items, 0, 0, W, H);
assert.equal(r.length, 40);
const total = items.reduce((a, b) => a + b.value, 0);
let area = 0;
for (const t of r) {
  area += t.w * t.h;
  const v = items.find(i => i.id === t.id).value;
  assert.ok(Math.abs((t.w * t.h) / (W * H) - v / total) < 1e-9, "area proportional to value");
  assert.ok(t.x >= -1e-9 && t.y >= -1e-9 && t.x + t.w <= W + 1e-6 && t.y + t.h <= H + 1e-6, "inside box");
}
assert.ok(Math.abs(area - W * H) < 1e-6, "tiles fill the box exactly");
for (let a = 0; a < r.length; a++) for (let b = a + 1; b < r.length; b++) {
  const p = r[a], q = r[b];
  const ox = Math.min(p.x + p.w, q.x + q.w) - Math.max(p.x, q.x);
  const oy = Math.min(p.y + p.h, q.y + q.h) - Math.max(p.y, q.y);
  assert.ok(!(ox > 1e-6 && oy > 1e-6), `overlap ${p.id}/${q.id}`);
}
assert.deepEqual(squarify([], 0, 0, 10, 10), []);
assert.deepEqual(squarify([{ id: "a", value: 0 }, { id: "b", value: NaN }], 0, 0, 10, 10), []);
assert.equal(squarify([{ id: "a", value: 5 }], 0, 0, 10, 20)[0].h, 20);
console.log("treemapLayout ok");
