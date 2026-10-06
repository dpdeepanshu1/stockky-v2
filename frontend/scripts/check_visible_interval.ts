// group 184: self-check for setVisibleInterval (the frontend has no test runner).
// Run:  cd frontend && npx tsx scripts/check_visible_interval.ts   (exits 1 on any mismatch)
type L = () => void;
const listeners: L[] = [];
const doc: any = {
  visibilityState: "visible",
  addEventListener: (_e: string, l: L) => listeners.push(l),
  removeEventListener: (_e: string, l: L) => { const i = listeners.indexOf(l); if (i >= 0) listeners.splice(i, 1); },
};
(globalThis as any).document = doc;
import { setVisibleInterval } from "../src/visibleInterval";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let bad = 0;
const check = (name: string, ok: boolean, info = "") => { if (!ok) { bad++; console.error(`FAIL  ${name} ${info}`); } };

(async () => {
  // 1. visible: runs on cadence
  let n = 0;
  const stop1 = setVisibleInterval(() => { n++; }, 40);
  await sleep(150);
  check("runs while visible", n >= 2 && n <= 4, `n=${n}`);

  // 2. hidden: no runs
  doc.visibilityState = "hidden";
  const before = n;
  await sleep(150);
  check("silent while hidden", n === before, `n=${n} before=${before}`);

  // 3. visible again after >= one period: one immediate run
  doc.visibilityState = "visible";
  listeners.slice().forEach((l) => l());
  check("immediate run when visible again", n === before + 1, `n=${n} before=${before}`);

  // 4. becoming visible right after a run does not double-fire
  const after = n;
  listeners.slice().forEach((l) => l());
  check("no double fire", n === after, `n=${n} after=${after}`);

  // 5. cleanup stops timer and listener
  stop1();
  const frozen = n;
  await sleep(120);
  check("stops after cleanup", n === frozen, `n=${n} frozen=${frozen}`);
  check("listener removed", listeners.length === 0, `listeners=${listeners.length}`);

  // 6. a throwing / rejecting fn does not stop the timer
  let m = 0;
  const stop2 = setVisibleInterval(() => { m++; if (m === 1) throw new Error("boom"); return Promise.reject(new Error("x")).catch(() => {}); }, 30);
  await sleep(120);
  stop2();
  check("keeps running after a throw", m >= 2, `m=${m}`);

  // 7. no document (SSR / tests): behaves like a plain interval
  delete (globalThis as any).document;
  let k = 0;
  const stop3 = setVisibleInterval(() => { k++; }, 30);
  await sleep(100);
  stop3();
  check("works without document", k >= 2, `k=${k}`);

  if (bad) process.exit(1);
  console.log("ok: 7 checks");
})();
