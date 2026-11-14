// E538 (redesign round 5b): guards for the Position Stocks split (page shell + usePositionStocks + ten Ps*Tab files + parts) and the board legend.
// Run: node --experimental-strip-types src/psSplit.test.mjs  (Node >= 22.6). Source-level checks, no React in the node test runner.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { psSource, PS_DIR } from "./psSource.mjs";
import { legendItems } from "./components/board/boardLegend.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const code = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const TABS = { overview: "PsOverviewTab", board: "PsBoardTab", timemachine: "PsTimeMachineTab", pipeline: "PsPipelineTab", screener: "PsScreenerTab",
               positions: "PsPositionsTab", history: "PsHistoryTab", dhanorders: "PsDhanOrdersTab", charges: "PsChargesTab", settings: "PsSettingsTab" };
const main = rd("./components/PositionStocksTab.tsx");
const hook = rd("./components/positionStocks/usePositionStocks.ts");

test("the page file is only the shell: it renders each of the ten sub-tabs from its own file, with the same id guard as before", () => {
  assert.ok(main.split("\n").length < 250, "the page shell is small");
  for (const [id, name] of Object.entries(TABS)) {
    assert.ok(existsSync(new URL(`./components/positionStocks/${name}.tsx`, import.meta.url)), `${name}.tsx exists`);
    assert.ok(main.includes(`import { ${name} } from "./positionStocks/${name}";`), `${name} imported`);
    assert.ok(main.includes(`{subTab === "${id}" && <${name} s={s} />}`), `sub-tab "${id}" renders <${name}>`);
  }
  assert.ok(main.includes("const s = usePositionStocks();"));
  assert.ok(/getPositionStocksApiUrl\(\)\)/.test(main) && main.includes("Position Stocks Service URL"), "the service-URL screen stays in the page");
  for (const gone of ["useState(", "useEffect(", "useMemo(", "useCallback(", "fetch(", "positionStocksApi."]) assert.ok(!code(main).includes(gone), `the page shell no longer has ${gone}`);
});
test("all state, effects and handlers live in the one hook, and it returns every name the page and the tabs use", () => {
  assert.ok((hook.match(/\buseState\b/g) || []).length >= 30, "the hook holds the page's state");
  assert.ok(hook.includes("export function usePositionStocks()") && hook.includes("export type PsState = ReturnType<typeof usePositionStocks>;"));
  const ret = hook.slice(hook.lastIndexOf("  return {"));
  const returned = new Set([...ret.matchAll(/^\s{4}([A-Za-z_$][\w$]*),$/gm)].map((m) => m[1]));
  assert.ok(returned.size >= 80, `returns ${returned.size} names`);
  const body = hook.slice(0, hook.lastIndexOf("  return {"));
  for (const m of body.matchAll(/^  const \[([^\]]+)\] =/gm)) for (const nm of m[1].split(",").map((x) => x.trim()).filter(Boolean)) assert.ok(returned.has(nm), `hook returns ${nm}`);
  for (const m of body.matchAll(/^  const ([A-Za-z_$][\w$]*)\s*[=:]/gm)) assert.ok(returned.has(m[1]), `hook returns ${m[1]}`);
  for (const m of body.matchAll(/^  (?:async )?function ([A-Za-z_$][\w$]*)/gm)) assert.ok(returned.has(m[1]), `hook returns ${m[1]}`);
  for (const k of ["subTab", "setSubTab", "status", "ledger", "openPositions", "doAction", "killSwitchTripped", "loggedIn", "loadAll"]) assert.ok(returned.has(k), k);
  // every name a tab or the page takes from `s` is one the hook returns
  for (const f of [...Object.values(TABS).map((x) => `positionStocks/${x}.tsx`), "PositionStocksTab.tsx"]) {
    const src = rd(`./components/${f}`);
    const m = src.match(/const \{([^}]+)\} = s;/);
    if (!m) continue;
    for (const nm of m[1].split(",").map((x) => x.trim()).filter(Boolean)) assert.ok(returned.has(nm), `${f} reads ${nm} from s but the hook does not return it`);
  }
});
test("the sub-tab files hold no state, effect or call of their own: they only draw what the hook gives them", () => {
  for (const name of Object.values(TABS)) {
    const src = code(rd(`./components/positionStocks/${name}.tsx`));
    assert.ok(new RegExp(`export function ${name}\\(\\{ s \\}: \\{ s: PsState \\}\\)`).test(src), `${name} takes { s }`);
    for (const bad of ["useState(", "useEffect(", "useMemo(", "useCallback(", "useRef(", "fetch("]) assert.ok(!src.includes(bad), `${name} has ${bad}`);
  }
});
test("the shared helpers and the two row components moved to parts.tsx unchanged and are exported", () => {
  const parts = rd("./components/positionStocks/parts.tsx");
  for (const nm of ["fmtInr", "fmtHms", "fmtDateTimeIst", "statusColor", "statusLabel", "CapitalBar", "CBadge", "RunCycleResultPanel", "LivePipelineStatus", "CandidateList", "PositionRow", "psOpenCharges"])
    assert.ok(new RegExp(`export (function|const) ${nm}\\b`).test(parts), `parts exports ${nm}`);
  assert.ok(parts.includes("<RiskRail side={p.side}") && parts.includes('data-testid="entry-reason"'), "the position row keeps its stop/entry/target rail and the entry reason");
  assert.ok(!/^function (fmtInr|PositionRow|CandidateList)\b/m.test(main), "none of them is left in the page file");
});
test("every ten-tab body is still somewhere in the joined source, with its old guard strings", () => {
  const all = psSource();
  for (const s of ['active={subTab === "positions"}', "<SellOrdersCard", "<TimeMachinePanel", "<MarketBoard", "<StockCandleBlock", "doManualBuy", "<CloseAllDialog", "<ApprovalsPanel", "<DhanMoneyCard", "<StructurePanel"])
    assert.ok(all.includes(s), `${s} still present`);
  assert.equal(readdirSync(PS_DIR).filter((f) => /^Ps\w+Tab\.tsx$/.test(f)).length, 10);
});
test("board legend: words for every cue the tiles draw, a marked-ring line only when marks are passed, none of it colour-only", () => {
  const base = legendItems();
  const keys = base.map((i) => i.key);
  assert.deepEqual(keys, ["bull", "bear", "heat", "size", "held", "fno", "ext", "rank"]);
  assert.ok(legendItems({ marked: true }).some((i) => i.key === "marked"));
  assert.ok(base.every((i) => i.label.length > 8 && i.swatches.length >= 1), "every swatch has words");
  assert.ok(base.find((i) => i.key === "ext").mark === "EXT" && base.find((i) => i.key === "fno").mark === "·F");
  const tile = rd("./components/board/BoardTile.tsx");
  for (const cue of ['" ·F"', "EXT", "row.in_position", "marked && !row.in_position", "ob-g-bull", "heat(row.percentile)"]) assert.ok(tile.includes(cue), `the tile really draws ${cue}`);
});
test("the Position Stocks board shows the legend; the Auto Trade board keeps its own text legend", () => {
  const b = rd("./components/positionStocks/PsBoardTab.tsx");
  assert.ok(b.includes("legend={<BoardLegend />}") && b.includes('import BoardLegend from "../board/BoardLegend"'));
  assert.ok(rd("./components/realAuto/tabs/BoardTab.tsx").includes("legend=\"Hatched tile"), "Auto Trade board legend untouched");
  const lg = rd("./components/board/BoardLegend.tsx");
  assert.ok(!/fetch\(|Api\.|useState|useEffect/.test(code(lg)), "the legend is presentation only");
  const css = rd("./styles/trading.css").replace(/\/\*[\s\S]*?\*\//g, "");
  for (const c of ["tr-legend", "tr-lg-sw", "bull", "bear", "heat-lo", "heat-hi", "size-s", "size-l", "held", "marked", "text"]) assert.ok(css.includes(`.${c}`), `.${c} defined`);
});
console.log(`${n} Position Stocks split tests passed`);
