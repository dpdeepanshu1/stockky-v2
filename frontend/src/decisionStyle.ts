export const decisionStyle: Record<
  string,
  { color: string; bg: string; border: string; glyph: string; verb: string }
> = {
  "BUY NOW": {
    color: "text-signal-buy",
    bg: "bg-signal-buy/10",
    border: "border-signal-buy/40",
    glyph: "●",
    verb: "Act now",
  },
  "PREPARE TO BUY": {
    color: "text-signal-prepare",
    bg: "bg-signal-prepare/10",
    border: "border-signal-prepare/40",
    glyph: "◐",
    verb: "Get ready",
  },
  HOLD: {
    color: "text-signal-hold",
    bg: "bg-signal-hold/10",
    border: "border-signal-hold/40",
    glyph: "◆",
    verb: "Stay in",
  },
  "WAIT": { // <--- NEW: Added for newly listed stocks
    color: "text-signal-hold",
    bg: "bg-signal-hold/10",
    border: "border-signal-hold/40",
    glyph: "◐",
    verb: "Monitor",
  },
  "DO NOT BUY": {
    color: "text-signal-avoid",
    bg: "bg-signal-avoid/10",
    border: "border-signal-avoid/40",
    glyph: "○",
    verb: "Wait",
  },
  SELL: {
    color: "text-signal-sell",
    bg: "bg-signal-sell/10",
    border: "border-signal-sell/40",
    glyph: "▼",
    verb: "Exit",
  },
};