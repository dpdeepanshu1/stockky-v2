// Run: node --experimental-strip-types src/settingsView.test.mjs
import assert from "node:assert/strict";
import { settingsKpis, apiHost } from "./components/settingsView.ts";

assert.deepEqual(settingsKpis(null), []);
assert.deepEqual(settingsKpis(undefined), []);

// host only, never path / query; empty is same-origin; junk is invalid
assert.equal(apiHost("https://api.example.com/x?y=1#z"), "api.example.com");
assert.equal(apiHost("http://localhost:8000"), "localhost:8000");
assert.equal(apiHost("  "), ""); assert.equal(apiHost(null), "");
assert.equal(apiHost("api.example.com"), null);
assert.equal(apiHost("ftp://api.example.com"), null);

const base = { backendUp: "up", url: "https://gw.example.com", savedUrl: "https://gw.example.com", theme: "dark", dbWake: "idle" };
const ok = settingsKpis(base);
assert.equal(ok.length, 4);
assert.equal(ok[0].value, "Connected"); assert.equal(ok[0].sub, "gw.example.com"); assert.equal(ok[0].tone, "info");
assert.equal(ok[1].value, "gw.example.com"); assert.equal(ok[1].sub, "saved on this device"); assert.equal(ok[1].tone, "info");
assert.equal(ok[2].value, "Dark");
assert.equal(ok[3].value, "Not checked"); assert.equal(ok[3].tone, "flat");

// backend states, spelled in words
const down = settingsKpis({ ...base, backendUp: "down" })[0];
assert.equal(down.value, "Unreachable"); assert.equal(down.tone, "warn"); assert.match(down.sub, /gw\.example\.com did not answer/);
assert.equal(settingsKpis({ ...base, backendUp: "checking" })[0].value, "Checking");
assert.equal(settingsKpis({ ...base, backendUp: "down", url: "", savedUrl: "" })[0].sub, "no answer from this origin");

// an unsaved edit warns; the saved host is still what the backend check used
const edit = settingsKpis({ ...base, url: "https://new.example.com" });
assert.equal(edit[1].value, "new.example.com"); assert.equal(edit[1].tone, "warn"); assert.match(edit[1].sub, /not saved/);
assert.equal(edit[0].sub, "gw.example.com");
// whitespace-only difference is not an edit
assert.equal(settingsKpis({ ...base, url: " https://gw.example.com " })[1].tone, "info");
// bad address
const bad = settingsKpis({ ...base, url: "gw.example.com" })[1];
assert.equal(bad.value, "Not valid"); assert.equal(bad.tone, "warn");
// empty = same origin, not an error
const empty = settingsKpis({ ...base, url: "", savedUrl: "" });
assert.equal(empty[1].value, "Same origin"); assert.equal(empty[1].sub, "no API URL set"); assert.equal(empty[0].sub, "same origin");

assert.equal(settingsKpis({ ...base, theme: "light" })[2].value, "Light");

// database wake states
assert.equal(settingsKpis({ ...base, dbWake: "waking" })[3].value, "Waking");
const dbOk = settingsKpis({ ...base, dbWake: "ok", dbDetail: "neon: up · cache: up" })[3];
assert.equal(dbOk.value, "Awake"); assert.equal(dbOk.sub, "neon: up · cache: up");
assert.equal(settingsKpis({ ...base, dbWake: "ok" })[3].sub, "all targets answered");
const dbBad = settingsKpis({ ...base, dbWake: "error", dbDetail: "x".repeat(200) })[3];
assert.equal(dbBad.value, "Wake failed"); assert.equal(dbBad.tone, "warn"); assert.equal(dbBad.sub.length, 80);
assert.equal(settingsKpis({ ...base, dbWake: "error" })[3].sub, "no detail from the backend");

// never prints a path, query or token from the address
const leak = JSON.stringify(settingsKpis({ ...base, url: "https://gw.example.com/secret?token=abc", savedUrl: "https://gw.example.com/secret?token=abc" }));
assert.ok(!leak.includes("secret") && !leak.includes("token"));
for (const k of ok) assert.equal(typeof k.value, "string");
console.log("settingsView ok");
