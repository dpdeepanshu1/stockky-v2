// E564: a tab from before a redeploy asks for page files that no longer exist. These tests pin the error detection, the reload-once guard and the wiring.
// (run: node --experimental-strip-types src/chunkReload.test.mjs)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { test } from "node:test";
import { isChunkLoadError, reloadOnceForNewVersion } from "./chunkReload.ts";

test("recognises the browser messages for a missing lazy chunk", () => {
  for (const m of [
    "Failed to fetch dynamically imported module: https://stockky.duckdns.org/assets/FnoTab-hOVy85cB.js",
    "error loading dynamically imported module: https://x/assets/a.js",
    "Importing a module script failed.",
    "Unable to preload CSS for /assets/x.css",
    "Loading chunk 12 failed.",
  ]) assert.ok(isChunkLoadError(new Error(m)), m);
  assert.ok(isChunkLoadError("Failed to fetch dynamically imported module: x"));
});
test("does not treat ordinary crashes as a chunk error", () => {
  for (const e of [new Error("Cannot read properties of undefined"), null, undefined, "boom", new Error("Failed to fetch")]) assert.equal(isChunkLoadError(e), false);
});
test("reloads once, then not again within a minute, then again after it", () => {
  const mem = {}; const store = { getItem: (k) => mem[k] ?? null, setItem: (k, v) => { mem[k] = v; } };
  let n = 0; const reload = () => { n++; };
  assert.equal(reloadOnceForNewVersion(store, 1_000_000, reload), true);
  assert.equal(reloadOnceForNewVersion(store, 1_030_000, reload), false);
  assert.equal(n, 1);
  assert.equal(reloadOnceForNewVersion(store, 1_070_000, reload), true);
  assert.equal(n, 2);
});
test("a blocked storage still reloads (once per call, the page then starts fresh)", () => {
  let n = 0;
  const bad = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.equal(reloadOnceForNewVersion(bad, 5, () => { n++; }), true);
  assert.equal(n, 1);
});
test("ErrorBoundary and main.tsx are wired to it", () => {
  const eb = readFileSync(new URL("./components/ui/ErrorBoundary.tsx", import.meta.url), "utf8");
  assert.match(eb, /isChunkLoadError\(error\)\) reloadOnceForNewVersion\(\)/);
  assert.match(eb, /window\.location\.reload\(\)/);
  const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
  assert.match(main, /vite:preloadError/);
  assert.match(main, /reloadOnceForNewVersion/);
});
