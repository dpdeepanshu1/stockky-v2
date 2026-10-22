// Run: node --experimental-strip-types components/board/journalNotesView.test.mjs
import assert from "node:assert/strict";
import { noteRows, emotionLabel, feelingText } from "./journalNotesView.ts";

const T = (id, ws) => ({ id, symbol: "S" + id, window_source: ws, status: "TARGET_HIT", realized_pnl: 1, opened_at: "x" });
const rows = noteRows([T(5, "5m"), T(4, "MANUAL"), T(3, "5m"), T(2, "MANUAL")], [{ position_id: 2, reason: "bo", emotion: "calm" }, { position_id: 3, reason: null, emotion: "fomo" }]);
assert.deepEqual(rows.map(r => r.trade.id), [4, 5, 3, 2]);              // manual trade without a note first, then server order
assert.equal(rows[0].manualMissing, true);
assert.equal(rows[3].manualMissing, false);                              // manual trade that has a note
assert.equal(rows[2].note.emotion, "fomo");
assert.equal(noteRows([T(1, "5m"), T(2, "5m")], [], 1).length, 1);       // max
assert.equal(emotionLabel("fomo"), "FOMO"); assert.equal(emotionLabel("zzz"), "zzz"); assert.equal(emotionLabel(null), "");
assert.equal(feelingText({ trades: 3, wins: 2, win_rate: 0.667, pnl: -12.4, small_sample: true }), "3 trades · 67% wins (small sample) · ₹-12");
assert.equal(feelingText({ trades: 40, wins: 20, win_rate: 0.5, pnl: 100, small_sample: false }), "40 trades · 50% wins · ₹100");
console.log("journalNotesView ok");
