// Run: node --experimental-strip-types fnoPoll.test.mjs  (Node >= 22.6). Round 339 (E341): the stale-response gate of the F&O polling hook.
import assert from "node:assert/strict";
import { createPollGate, sameInputs } from "./fnoPoll.ts";

// a normal request is shown
let g = createPollGate();
let t = g.start();
assert.equal(g.accept(t), true);

// inputs change while a request is in flight: its answer is dropped, the new one is shown
g = createPollGate();
const oldReq = g.start();          // Nifty
g.reset();                         // user picks Sensex
const newReq = g.start();          // Sensex
assert.equal(g.accept(newReq), true);
assert.equal(g.accept(oldReq), false, "late Nifty answer must not show under Sensex");

// ... also when the old answer lands first
g = createPollGate();
const a = g.start(); g.reset(); const b = g.start();
assert.equal(g.accept(a), false);
assert.equal(g.accept(b), true);

// two polls of the same inputs finish out of order: the older one is dropped once the newer was shown
g = createPollGate();
const p1 = g.start(); const p2 = g.start();
assert.equal(g.accept(p2), true);
assert.equal(g.accept(p1), false);

// in order is fine, and a slow poll that is the newest still lands (no starvation when polls overlap)
g = createPollGate();
const q1 = g.start(); const q2 = g.start();
assert.equal(g.accept(q1), true);
assert.equal(g.accept(q2), true);
g = createPollGate();
const slow = g.start(); g.start();          // a newer poll was sent but has not finished
assert.equal(g.accept(slow), true);          // the older one is still better than nothing

// unmount (reset) drops everything in flight
g = createPollGate();
const u = g.start(); g.reset();
assert.equal(g.accept(u), false);

// sameInputs
assert.equal(sameInputs([], []), true);
assert.equal(sameInputs(["NIFTY", ""], ["NIFTY", ""]), true);
assert.equal(sameInputs(["NIFTY", ""], ["SENSEX", ""]), false);
assert.equal(sameInputs(["A"], ["A", "B"]), false);
assert.equal(sameInputs([NaN], [NaN]), true);
console.log("fnoPoll.test.mjs ok");
