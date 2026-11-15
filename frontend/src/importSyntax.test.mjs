// E562: a multi-line `import {` must be closed before the next import starts. Round 537 pasted `import { Button } ...` between `import {` and its names in
// HistoryTab.tsx; every node test passed (none parses the TSX) and only `tsc` / `vite build` on the VM failed. This guard catches that shape without a compiler.
// (run: node --experimental-strip-types src/importSyntax.test.mjs)
import { readdirSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { test } from "node:test";

const walk = (u, out = []) => { for (const e of readdirSync(u, { withFileTypes: true })) { const n = new URL(e.name + (e.isDirectory() ? "/" : ""), u); if (e.isDirectory()) { if (e.name !== "node_modules") walk(n, out); } else if (/\.(ts|tsx|mjs)$/.test(e.name)) out.push(n); } return out; };

test("no source file starts a new import inside an unfinished `import {` list", () => {
  const bad = [];
  for (const f of walk(new URL("./", import.meta.url))) {
    let open = 0;
    readFileSync(f, "utf8").split("\n").forEach((l, i) => {
      if (open) {
        if (/^\s*import\s/.test(l) && /\sfrom\s+["']/.test(l)) { bad.push(`${f.pathname}:${i + 1} (import list opened at line ${open})`); open = 0; }
        else if (/^\s*\}/.test(l) || /\sfrom\s+["']/.test(l)) open = 0;
      } else if (/^import\s+(type\s+)?\{\s*$/.test(l)) open = i + 1;
    });
  }
  assert.deepEqual(bad, []);
});
test("the guard really catches the round 537 shape", () => {
  const sample = ["import {", 'import { Button } from "../../kit";', "  A, B,", '} from "../history";'];
  let open = 0, hit = false;
  sample.forEach((l, i) => { if (open) { if (/^\s*import\s/.test(l) && /\sfrom\s+["']/.test(l)) hit = true; } else if (/^import\s+(type\s+)?\{\s*$/.test(l)) open = i + 1; });
  assert.ok(hit);
});
