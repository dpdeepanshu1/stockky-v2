// Round 175: event chips. Run: node --experimental-strip-types src/eventChips.test.mjs
import assert from "node:assert/strict";
import { eventChips } from "./eventChips.ts";

assert.deepEqual(eventChips(null), []);
assert.deepEqual(eventChips({ title: "x" }), []);
assert.deepEqual(eventChips({ event_type: "general" }), []);
assert.deepEqual(eventChips({ event_type: "results", event_polarity: 1, event_confidence: 0.82 }),
  [{ text: "Results", tone: "plain" }, { text: "Positive 82%", tone: "buy" }]);
assert.deepEqual(eventChips({ event_type: "legal", event_polarity: -1 }),
  [{ text: "Legal", tone: "plain" }, { text: "Negative", tone: "sell" }]);
assert.deepEqual(eventChips({ event_type: "some_new_type", event_polarity: 0 }), [{ text: "some new type", tone: "plain" }]);
assert.deepEqual(eventChips({ event_polarity: 1, event_confidence: 7 }), [{ text: "Positive", tone: "buy" }]);
assert.deepEqual(eventChips({ event_polarity: "x" }), []);
console.log("eventChips ok");
