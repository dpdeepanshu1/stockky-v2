// E30: pure helpers for the trade-notes card in the journal panel (no DOM). The notes themselves live on the server
// (services/position-stocks-service/orders/journal_notes.py); this only decides what to show and how to label it.
export interface NoteLite { position_id: number; reason: string | null; emotion: string | null }
export interface TradeLite { id: number; symbol: string; window_source: string; status: string; realized_pnl: number | null; opened_at: string }
export interface NoteRow { trade: TradeLite; note: NoteLite | null; manualMissing: boolean }

export const EMOTION_LABEL: Record<string, string> = {
  calm: "Calm", confident: "Confident", hopeful: "Hopeful", unsure: "Unsure", impatient: "Impatient",
  fomo: "FOMO", fear: "Fear", greed: "Greed", revenge: "Revenge",
};
export const FALLBACK_EMOTIONS = Object.keys(EMOTION_LABEL);

export function emotionLabel(e: string | null | undefined): string {
  if (!e) return "";
  return EMOTION_LABEL[e] ?? e;
}

/** Trades newest first (as the server sends them), each with its note. Manual trades with no note come first so they get filled in. */
export function noteRows(trades: TradeLite[], notes: NoteLite[], max = 15): NoteRow[] {
  const by = new Map(notes.map(n => [n.position_id, n]));
  const rows = trades.map((t, i) => {
    const note = by.get(t.id) ?? null;
    return { trade: t, note, manualMissing: t.window_source === "MANUAL" && !note, i };
  });
  rows.sort((a, b) => (Number(b.manualMissing) - Number(a.manualMissing)) || (a.i - b.i));
  return rows.slice(0, max).map(({ i: _i, ...r }) => r);
}

/** Win-rate text for one feeling: "-" is never shown; a small sample is labelled so 2 wins out of 3 is not read as an edge. */
export function feelingText(s: { trades: number; wins: number; win_rate: number; pnl: number; small_sample: boolean }): string {
  const pct = Math.round(s.win_rate * 100);
  return `${s.trades} trades · ${pct}% wins${s.small_sample ? " (small sample)" : ""} · ₹${s.pnl.toFixed(0)}`;
}
