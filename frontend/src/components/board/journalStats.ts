// group310: pure helpers for the journal panel (no DOM). Day = IST calendar date of closed_at (else opened_at).
export interface JTrade { realized_pnl: number | null; window_source: string; closed_at: string | null; opened_at: string; status: string }
export interface DayCell { day: string; pnl: number; trades: number; wins: number }
export interface TagStat { tag: string; trades: number; wins: number; win_rate: number | null; pnl: number }

export function istDay(iso: string): string {
  // market-data / this service write naive UTC ISO strings; treat a string without a zone as UTC, then shift to IST (+5:30)
  const hasZone = /([zZ]|[+-]\d\d:?\d\d)$/.test(iso);
  const t = Date.parse(hasZone ? iso : iso + "Z");
  if (!Number.isFinite(t)) return "";
  return new Date(t + 330 * 60000).toISOString().slice(0, 10);
}

export function byDay(trades: JTrade[]): DayCell[] {
  const m = new Map<string, DayCell>();
  for (const t of trades) {
    if (t.realized_pnl == null || !Number.isFinite(t.realized_pnl)) continue;       // unresolved / pending trades have no P&L yet
    const d = istDay(t.closed_at || t.opened_at);
    if (!d) continue;
    const c = m.get(d) ?? { day: d, pnl: 0, trades: 0, wins: 0 };
    c.pnl += t.realized_pnl; c.trades += 1; if (t.realized_pnl > 0) c.wins += 1;
    m.set(d, c);
  }
  return [...m.values()].sort((a, b) => a.day.localeCompare(b.day));
}

export function byTag(trades: JTrade[]): TagStat[] {
  const m = new Map<string, TagStat>();
  for (const t of trades) {
    if (t.realized_pnl == null || !Number.isFinite(t.realized_pnl)) continue;
    const tag = t.window_source || "?";
    const s = m.get(tag) ?? { tag, trades: 0, wins: 0, win_rate: null, pnl: 0 };
    s.trades += 1; s.pnl += t.realized_pnl; if (t.realized_pnl > 0) s.wins += 1;
    m.set(tag, s);
  }
  return [...m.values()].map(s => ({ ...s, win_rate: s.trades ? s.wins / s.trades : null })).sort((a, b) => b.trades - a.trades);
}
