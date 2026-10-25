// Run: node --experimental-strip-types statusProbes.test.mjs  (Node >= 22.6). E144: the read-only status probes section (statusProbes.ts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { STATUS_PROBES, probesFor, probeText, probeBadge } from "./statusProbes.ts";

assert.equal(new Set(STATUS_PROBES.map((p) => `${p.svc}:${p.id}`)).size, STATUS_PROBES.length, "ids unique per service");
assert.equal(new Set(STATUS_PROBES.map((p) => `${p.svc}:${p.path}`)).size, STATUS_PROBES.length, "paths unique per service");
assert.ok(probesFor("rt").length >= 8 && probesFor("ps").length >= 3);
assert.deepEqual(probesFor("rt").concat(probesFor("ps")).length, STATUS_PROBES.length);

// CONTRACT: every probe path is a GET route that exists in its service (so no button points at a dead route) and none of them is a write route.
const root = new URL("../../", import.meta.url);
const src = { rt: readFileSync(new URL("services/real-trade-service/main.py", root), "utf8"), ps: readFileSync(new URL("services/position-stocks-service/main.py", root), "utf8") };
for (const p of STATUS_PROBES) {
  assert.ok(src[p.svc].includes(`@app.get("${p.path}")`), `${p.svc} has no GET ${p.path}`);
  assert.ok(!src[p.svc].includes(`@app.post("${p.path}")`) || p.path === "/short/probe", `${p.path} also has a POST: check what the button would reach`);
  assert.ok(p.title && p.what && p.path.startsWith("/"));
}

// probeText / probeBadge
assert.equal(probeText({ a: 1 }), '{\n  "a": 1\n}');
const big = probeText({ x: "y".repeat(9000) });
assert.match(big, /… \(\d+ more characters not shown\)$/);
assert.ok(big.length < 8100);
const circ = {}; circ.self = circ;
assert.doesNotThrow(() => probeText(circ));
assert.equal(probeText(undefined), "undefined");
assert.deepEqual(probeBadge({ error: "db down" }), { tone: "bad", text: "error: db down" });
assert.deepEqual(probeBadge({ a: 1, b: 2 }), { tone: "good", text: "2 fields" });
assert.deepEqual(probeBadge([1, 2, 3]), { tone: "good", text: "3 rows" });
assert.deepEqual(probeBadge(null), { tone: undefined, text: "empty answer" });

// WIRING: the panel fetches only on a button press, only catalog paths, no timer, no write.
const panel = readFileSync(new URL("frontend/src/components/calibration/StatusProbesPanel.tsx", root), "utf8");
assert.ok(!/setInterval|setVisibleInterval/.test(panel.replace(/\/\/.*$/gm, "")), "probes are fetched on demand, never polled");
assert.ok(!/method:\s*"(POST|PUT|DELETE)"/.test(panel));
const tab = readFileSync(new URL("frontend/src/components/CalibrationControlTab.tsx", root), "utf8");
assert.match(tab, /section === "probes" && <StatusProbesPanel svc=\{svc\} \/>/);
const rtApi = readFileSync(new URL("frontend/src/realTradeApi.ts", root), "utf8");
const psApi = readFileSync(new URL("frontend/src/positionStocksApi.ts", root), "utf8");
assert.match(rtApi, /probe: \(path: string\) => rtRequest<any>\(path, undefined, true\)/);
assert.match(psApi, /probe: \(path: string\) => psRequest<any>\(path, undefined, true\)/);
console.log("statusProbes.test.mjs: all assertions passed");
