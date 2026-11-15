// Run: node --experimental-strip-types src/f3pages2.test.mjs  (Node >= 22.6). Round 538 (F3 part 2): decision Bits, SignalStream, the Setup scorecard, ScanPanel cards, the Surprise tables and the IPO rows
// keep their handlers and use the kit (Button, Card, Chip, DataTable). Presentation only.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };
const F = { bits: "./components/decision/Bits.tsx", stream: "./components/SignalStream.tsx", setup: "./components/calibration/SetupScorecardPanel.tsx", scan: "./components/ScanPanel.tsx", surprise: "./components/SurpriseStocks.tsx", ipo: "./components/IpoTracker.tsx" };
const code = (p) => rd(p).replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const count = (s, re) => (s.match(re) || []).length;

test("the five card / button files import the kit; the scorecard imports the kit DataTable", () => {
  for (const k of ["bits", "stream", "scan", "surprise", "ipo"]) assert.match(rd(F[k]), /from "\.{1,2}\/kit"|from "\.\/kit"/, k);
  assert.match(rd(F.setup), /import \{ DataTable, type Column \} from "\.\.\/kit"/);
});
test("no hand-rolled coloured action button is left; the only <button> tags are the toggle groups that were kept on purpose", () => {
  for (const k of Object.keys(F)) assert.ok(!/<button[^>]*(bg-signal-|text-signal-|border-signal-)/.test(code(F[k])), `${k} still has a hand-rolled coloured button`);
  assert.equal(count(code(F.bits), /<button/g), 0);
  assert.equal(count(code(F.surprise), /<button/g), 0, "Surprise has no raw button");
  assert.equal(count(code(F.scan), /<button/g), 2, "ScanPanel: only the decision filter and preset chips (toggle groups)");
  assert.equal(count(code(F.ipo), /<button/g), 4, "IPO: only the four window toggles (a segmented control)");
  assert.ok(/aria-selected=\{filterChip === id\}/.test(code(F.scan)) && /aria-pressed=\{presetId === p\.id\}/.test(code(F.scan)), "chip toggles keep their state attributes");
  assert.ok(/className=\{`chip \$\{filter === f \? "active" : ""\}`\}/.test(code(F.stream)), "SignalStream filter chips stay a toggle group");
});
test("ScanPanel: every handler is wired, REAL stays danger, DEMO is soft, the guard on the sniper button is kept", () => {
  const s = code(F.scan);
  for (const [h, v] of [["handleRefreshPrepareToBuy", "soft"], ["handleSearchBuys", "primary"], ["onBack", "ghost"]]) assert.ok(new RegExp(`<Button variant="${v}"[^>]*onClick=\\{${h}\\}`).test(s), `${h} as ${v}`);
  assert.ok(/<Button variant="danger" size="sm" onClick=\{\(\) => void sendToRealTrade\(r, "REAL"\)\} loading=\{sendingToTrade === `\$\{r\.symbol\}:REAL`\}/.test(s), "REAL");
  assert.ok(/<Button variant="soft" size="sm" onClick=\{\(\) => void sendToRealTrade\(r, "DEMO"\)\} loading=\{sendingToTrade === `\$\{r\.symbol\}:DEMO`\}/.test(s), "DEMO");
  assert.ok(/disabled=\{!result \|\| sniperLoading\} loading=\{sniperLoading\}/.test(s));
  assert.ok(/onClick=\{\(\) => onSelect\(r\.symbol\)\}>View →/.test(s));
});
test("ScanPanel cards: a kit Card that is one control with Enter / Space, and the Watchlist button inside is a real kit Button that does not open the card", () => {
  const s = code(F.scan);
  assert.equal(count(s, /<Card\b/g), 2, "TopPick and CandidateCard");
  assert.equal(count(s, /role="button"\s+tabIndex=\{0\}/g), 2);
  assert.equal(count(s, /e\.target === e\.currentTarget && \(e\.key === "Enter" \|\| e\.key === " "\)/g), 2, "an Enter on the inner button does not also open the card");
  assert.equal(count(s, /onClick=\{\(e\) => \{ e\.stopPropagation\(\); onAddToWatchlist\(data\.symbol\); \}\}/g), 2);
  assert.ok(!/<span\s+role="button"/.test(s), "no span pretending to be a button");
  assert.ok(/<Chip key=\{s\.key\} as="span"/.test(s), "horizon stamps are chips");
});
test("SignalStream: kit Card, decision chip with symbol, data pillars as a ChipList with pass / miss symbol; old classes gone", () => {
  const s = code(F.stream);
  assert.ok(/<Card className="mb-4">/.test(s) && !/terminal-panel/.test(s));
  assert.ok(/<Chip as="span" tone=\{pillTone\} symbol=\{pillSym\}>/.test(s));
  assert.ok(/<ChipList label="Data pillars"/.test(s) && /tone=\{v \? "ok" : "bad"\} symbol=\{v \? "✓" : "✗"\}/.test(s));
  assert.ok(!/signal-pill|pillar-chip|pillar-row/.test(s));
  assert.ok(/onClick=\{\(\) => onSelect\?\.\(r\.symbol\)\}/.test(s) && /role="button"/.test(s), "the row still opens the stock");
});
test("Bits: ReasonList is a kit Card with a kit ghost button that keeps its state; the event badges are kit chips with direction symbols", () => {
  const b = code(F.bits);
  assert.ok(/<Card>\s*<h3/.test(b) && !/bg-graphite/.test(b));
  assert.ok(/<Button variant="ghost" size="sm" className="mt-2" aria-expanded=\{expanded\} onClick=\{\(\) => setExpanded\(!expanded\)\}>/.test(b));
  assert.ok(/<Chip as="span" tone=\{scoreTone\} symbol=\{scoreSym\}>/.test(b) && /tone=\{pos \? "up" : "down"\}\s+symbol=\{pos \? "▲" : "▼"\}/.test(b));
});
test("Setup scorecard: kit DataTable, same seven columns in order, four right-aligned, rows and test id kept", () => {
  const s = code(F.setup);
  let at = -1;
  for (const k of ["setup", "slot", "n", "win", "avgR", "lowerR", "verdict"]) { const i = s.indexOf(`key: "${k}"`); assert.ok(i > at, `column ${k}`); at = i; }
  assert.equal(count(s, /align: "right"/g), 4);
  assert.ok(/data-testid="setup-table"/.test(s) && /<DataTable dense columns=\{scoreColumns\(v\.minTrades\)\}/.test(s) && /rowKey=\{\(r\) => r\.key\}/.test(s) && /isAll \? "font-bold"/.test(s));
  assert.ok(!/<table|<thead|<tr\b/.test(s), "no hand-written table left");
});
test("Surprise: header and audit buttons keep their handlers on the kit; the repair table is a DataTable of Chips and kit buttons", () => {
  const flat = code(F.surprise).replace(/\s+/g, " ");
  for (const [h, v] of [["startPremarket", "soft"], ["() => void runMarketAwareQuoteFeed(false)", "soft"], ["() => fetchSurpriseStocks(true)", "primary"], ["() => void stopSurprise()", "danger"], ["handleSearchBuysFromSurprise", "soft"], ["() => void notifyTopPicks()", "soft"], ["() => void fetchSurpriseHealth()", "ghost"], ["() => void handleRepairBatchMissing()", "soft"]]) {
    assert.ok(flat.includes(`<Button variant="${v}" size="sm" onClick={${h}}`), `${h} as ${v}`);
  }
  assert.ok(/disabled=\{!busy\} loading=\{stopBusy\}/.test(flat), "Stop stays disabled when nothing runs");
  assert.ok(/\(healthData\?\.incomplete_stocks\?\.length \?\? healthData\?\.missing_data \?\? 0\) === 0/.test(flat), "Repair All keeps its disable rule");
  assert.ok(/<Chip key=\{m\} as="span" tone="bad" symbol="✗">/.test(flat) && /patching === r\.symbol/.test(flat) && /disabled=\{batchBusy\}/.test(flat));
  assert.ok(/NoMatch[\s\S]*Scanning universe…[\s\S]*No surprise breakouts or early-building setups right now\./.test(flat), "the three empty states are kept");
});
test("IPO: toolbar and row buttons keep their handlers on the kit; rows are quiet kit Cards; the decision badge is a chip; the buy button keeps its labels", () => {
  const s = code(F.ipo);
  for (const [h, v] of [["() => setIpoAddOpen(!ipoAddOpen)", "ghost"], ["() => void startIpoScan(true, true)", "soft"], ["() => void loadFromDb()", "soft"], ["() => void startIpoScan(true, false)", "ghost"], ["() => void stopIpoScan()", "danger"], ["() => void handleSearchBuysFromIpo()", "primary"], ["() => void notifyTop5(5)", "soft"], ["() => void submitIpoAdd()", "primary"], ["() => void openIpoSuggestion(ipo)", "soft"]]) {
    assert.ok(s.includes(`onClick={${h}}`) && new RegExp(`<Button\\s+(variant="${v}"[^>]*|[^>]*variant="${v}"[^>]*)onClick=\\{${h.replace(/[()]/g, "\\$&")}\\}`).test(s), `${h} as ${v}`);
  }
  assert.ok(/<Card quiet key=\{ipo\.symbol\} className=\{`mk-ipo-row \$\{v\.stage\.tone\}`\}>/.test(s));
  assert.ok(/decisionChip\(ipo\.decision\)\.tone/.test(s) && !/decisionBadgeClass|BusySpinner/.test(s));
  for (const w of ["Buy Now →", "Prepare to Buy →", "🎯 Scan for Buy →", "Hide detail ▴", "Detail ▾"]) assert.ok(s.includes(w), w);
  assert.ok(/aria-expanded=\{expandedSymbol === ipo\.symbol\}/.test(s), "the detail toggle says whether it is open");
});
test("stylesheet: dead signal-pill / pillar rules gone, .topo-online kept, IPO row stripe written for the quiet kit Card", () => {
  const b = rd("./styles/base.css"), m = rd("./styles/markets.css");
  assert.ok(!/signal-pill|pillar-/.test(b));
  assert.ok(/\.topo-online \{ font-weight: 700; \}/.test(b));
  for (const t of ["", ".up", ".warn", ".down"]) assert.ok(m.includes(`.k-card.quiet.mk-ipo-row${t} {`), `stripe ${t || "default"}`);
  assert.ok(!/^\.mk-ipo-row[ .{]/m.test(m), "the old one-class stripe rules are gone");
});
console.log(`f3pages2.test.mjs: ${n} ok`);
