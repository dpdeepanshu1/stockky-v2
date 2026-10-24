// Run: node --experimental-strip-types src/realAutoTabs.test.mjs  (Node >= 22.6)
// E65 guard for the E31 split of RealAutoTrade.tsx: every tab lives in components/realAuto/tabs/ and is the only
// place its content is defined. Source-level checks (no React in the node test runner).
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const main = read("components/RealAutoTrade.tsx");

const TABS = { orders: "OrdersTab", positions: "PositionsTab", watchlist: "WatchlistTab", live: "LiveTab",
               pipeline: "PipelineTab", charges: "ChargesTab", log: "LogTab", board: "BoardTab", overview: "OverviewTab", history: "HistoryTab" };
for (const [id, name] of Object.entries(TABS)) {
  assert.ok(existsSync(join(here, `components/realAuto/tabs/${name}.tsx`)), `${name}.tsx exists`);
  assert.match(main, new RegExp(`import \\{ ${name}\\b[^}]*\\} from "\\./realAuto/tabs/${name}";`), `${name} imported`);
  assert.match(main, new RegExp(`activeTab === "${id}" && \\(\\s*<${name}\\b`), `tab "${id}" renders <${name}>`);
}

// Board tab: all three panels + the risk-cap hatching + the held/candidate chips survive the move.
const board = read("components/realAuto/tabs/BoardTab.tsx");
assert.match(board, /export function BoardTab\(/);
for (const piece of ["<TopPicks", "<ShortlistPanel", "<MarketBoard", "riskCap(", "hatchFor=", 'data-testid="real-board-marks"',
                     "realTradeApi.topPicks(mode)", "realTradeApi.shortlist(mode)"]) {
  assert.ok(board.includes(piece), `BoardTab keeps ${piece}`);
}
assert.equal((board.match(/\bactive\b(?!=)/g) || []).length >= 3, true, "the three panels are active");
assert.ok(!board.includes("activeTab"), "BoardTab does not read the parent's activeTab");

// RealAutoTrade no longer carries the board's code or its imports.
for (const gone of ['import MarketBoard', 'import ShortlistPanel', 'import TopPicks', 'riskCap', 'real-board-marks']) {
  assert.ok(!main.includes(gone), `RealAutoTrade no longer has ${gone}`);
}

// E66: the overview tab (arming, Dhan, risk config, snapshot) and its two helpers moved out too.
const ov = read("components/realAuto/tabs/OverviewTab.tsx");
for (const piece of ["export function OverviewTab(", "function GateStep(", "function PortfolioSummary(", "<CloseAllDialog", "<ManualTradeTicket",
                     "<OrderFlowDiagram", "Arming sequence", "doSaveRiskConfig", "doEdisAuthorize", "doRegenerateToken"]) {
  assert.ok(ov.includes(piece), `OverviewTab keeps ${piece}`);
}
assert.ok(!ov.includes("activeTab"), "OverviewTab does not read the parent's activeTab");
for (const gone of ["function GateStep(", "function PortfolioSummary(", "<CloseAllDialog", "<ManualTradeTicket", "Arming sequence"]) {
  assert.ok(!main.includes(gone), `RealAutoTrade no longer has ${gone}`);
}
// every prop the tab declares is handed over by RealAutoTrade
const propsBlock = ov.slice(ov.indexOf("export interface OverviewTabProps"), ov.indexOf("export function OverviewTab("));
const inner = propsBlock.slice(propsBlock.indexOf("{") + 1).replace(/\([^)]*\)/g, "").replace(/\{[^}]*\}/g, "").replace(/\/\/.*$/gm, "");
const names = [...inner.matchAll(/\b([a-zA-Z0-9]+)\??:/g)].map((m) => m[1]);
const call = main.slice(main.indexOf("<OverviewTab "), main.indexOf("/>", main.indexOf("<OverviewTab ")));
for (const n of ["mode", "status", "loggedIn", "armed", "positions", "doArm", "doSaveRiskConfig", "riskForm", "setRiskForm", "cumBrokerage"]) {
  assert.ok(names.includes(n), `prop ${n} declared`);
}
for (const n of new Set(names)) assert.ok(new RegExp(`\\b${n}=\\{`).test(call), `RealAutoTrade passes ${n}`);

// E75: Real Trade journal notes (reason + feeling) are mounted on the Activity tab and call the mode-scoped routes.
const logTab = read("components/realAuto/tabs/LogTab.tsx");
assert.match(logTab, /<JournalNotesCard mode=\{mode\} \/>/, "LogTab mounts the notes card");
const rtApi = read("realTradeApi.ts");
for (const piece of ["`/journal/notes/${mode}?limit=${limit}`", "`/journal/notes/${mode}/${positionId}`", "closedPositions:", "export interface JournalNotesResponse"]) {
  assert.ok(rtApi.includes(piece), `realTradeApi keeps ${piece}`);
}
const card = read("components/realAuto/JournalNotesCard.tsx");
assert.ok(card.includes("realTradeApi.saveJournalNote(mode, editId"), "the card saves through the mode-scoped route");

console.log("realAutoTabs.test.mjs: ok");
