// Run: node --experimental-strip-types src/rtEntryReason.test.mjs  (Node >= 22.6). E285 round 104: Real Trade shows the Entry reason on the Positions cards and the History rows.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { toCsv } from "./components/realAuto/history.ts";
import { entryReason } from "./setupView.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let n = 0;
const test = (name, fn) => { fn(); n += 1; console.log("ok -", name); };

const stamp = { id: "LEGACY_SHORTLIST", trigger: "BUY NOW", confirmations: ["above_vwap"], penalties: { vix: 0.8 }, size_mult: 0.8, slot: "0930", side: "LONG" };

test("the API types carry the stamp on open positions, closed positions and trade records", () => {
  const api = rd("./realTradeApi.ts");
  assert.ok(/import type \{ SetupStamp \} from "\.\/setupView"/.test(api));
  assert.equal((api.match(/setup\?: SetupStamp \| null/g) || []).length, 3, "Position, ClosedPosition and TradeRecord");
});

test("the Positions card shows the Entry reason with the same helper as Position Stocks, and says why when it is missing", () => {
  const parts = rd("./components/realAuto/parts.tsx");
  assert.ok(/import \{ entryReason \} from "\.\.\/\.\.\/setupView"/.test(parts));
  assert.ok(/entryReason\(p\.setup\)/.test(parts) && /data-testid="entry-reason"/.test(parts) && /title=\{er\.title\}/.test(parts));
  const none = entryReason(undefined);
  assert.equal(none.known, false); assert.equal(none.label, "-"); assert.ok(/before entries were stamped/.test(none.title));
});

test("the History row shows the entry reason under the exit reason and in the detail list", () => {
  const tab = rd("./components/realAuto/tabs/HistoryTab.tsx");
  assert.ok(/import \{ entryReason \} from "\.\.\/\.\.\/\.\.\/setupView"/.test(tab));
  assert.ok(/entryReason\(r\.setup\)/.test(tab) && /data-testid="entry-reason"/.test(tab) && /data-testid="entry-reason-detail"/.test(tab));
  assert.ok(/<dt>Entry reason<\/dt>/.test(tab));
  const er = entryReason(stamp);
  assert.equal(er.known, true); assert.equal(er.label, "Shortlist (old rule)"); assert.equal(er.slot, "09:30");
});

test("the history CSV gains the entry setup and slot columns at the end; a trade without a stamp leaves them empty", () => {
  const base = { position_id: 1, mode: "REAL", symbol: "AAA", entry_price: 100, exit_price: 101, quantity: 2, held_minutes: 30, tier_name: "T1", source_tab: "watchlist" };
  const lines = toCsv([{ ...base, setup: stamp }, { ...base, position_id: 2 }]).split("\n");
  assert.ok(lines[0].endsWith(",source,entry_setup,entry_slot"));
  assert.ok(lines[1].endsWith(",watchlist,LEGACY_SHORTLIST,0930"));
  assert.ok(lines[2].endsWith(",watchlist,,"));
});

console.log(`rtEntryReason ok (${n})`);
