// Round 175: small labels for one event row (type + polarity) on the Events tab. Pure, so it is tested without React.
export interface EventChip {
  text: string;
  tone: "buy" | "sell" | "plain";
}

const TYPE_LABEL: Record<string, string> = {
  results: "Results",
  guidance: "Guidance / orders",
  dividend: "Dividend",
  corporate_action: "Corporate action",
  board_meeting: "Board meeting",
  bulk_block: "Bulk / block deal",
  insider: "Insider / promoter",
  regulatory: "Regulator",
  legal: "Legal",
  management_change: "Management",
  rating_change: "Rating",
};

/** Chips for an event object from the news / event services; [] when it has no type worth showing ("general", missing). */
export function eventChips(o: Record<string, unknown> | null | undefined): EventChip[] {
  const out: EventChip[] = [];
  if (!o || typeof o !== "object") return out;
  const t = typeof o.event_type === "string" ? o.event_type : "";
  if (t && t !== "general") out.push({ text: TYPE_LABEL[t] || t.replace(/_/g, " "), tone: "plain" });
  const p = Number(o.event_polarity);
  const c = Number(o.event_confidence);
  if (p === 1 || p === -1) {
    const pct = Number.isFinite(c) && c > 0 && c <= 1 ? ` ${Math.round(c * 100)}%` : "";
    out.push({ text: (p === 1 ? "Positive" : "Negative") + pct, tone: p === 1 ? "buy" : "sell" });
  }
  return out;
}
