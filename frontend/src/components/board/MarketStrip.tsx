// group306: top strip - live/stale/closed, feed health, counts, leading and lagging sector.
import type { BoardPayload } from "./boardTypes";

const pct = (v: number | null | undefined) => (v == null ? "-" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);

export default function MarketStrip({ data }: { data: BoardPayload }) {
  const m = data.market, f = data.feed;
  const stale = data.stale || (data.market_open && (f.tick_age_s ?? 0) > 30) || (data.market_open && !f.connected);
  const label = data.stale ? `LAST SESSION ${data.stale_day ?? ""}` : !data.market_open ? "MARKET CLOSED" : stale ? "FEED STALE" : "LIVE";
  const dot = data.stale || !data.market_open ? "bg-mist" : stale ? "bg-signal-hold" : "bg-signal-buy";
  const tot = Math.max(1, m.up + m.down + m.flat);
  return (
    <div className="rounded-xl border border-slate bg-ink p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-display">
        <span className="flex items-center gap-1.5 font-bold text-paper"><span className={`w-2 h-2 rounded-full ${dot}`} />{label}</span>
        <span className="text-mist">feed {f.source ?? "?"}{f.tick_age_s != null ? ` · ${f.tick_age_s.toFixed(0)}s old` : ""}</span>
        <span className="text-signal-buy tabular-nums">{m.bull_count} bullish</span>
        <span className="text-signal-sell tabular-nums">{m.bear_count} bearish</span>
        <span className="text-mist tabular-nums">{m.tracked} tracked · {data.coverage.warming} warming</span>
      </div>
      <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate" aria-label="advancing vs declining">
        <div style={{ width: `${(m.up / tot) * 100}%`, background: "var(--buy)" }} />
        <div style={{ width: `${(m.flat / tot) * 100}%`, background: "var(--muted-2)" }} />
        <div style={{ width: `${(m.down / tot) * 100}%`, background: "var(--sell)" }} />
      </div>
      <div className="flex flex-wrap gap-x-4 text-[11px] text-mist">
        <span>Leading: <b className="text-signal-buy">{m.leading_sector ?? "-"}</b> {pct(m.leading_change_pct)}</span>
        <span>Lagging: <b className="text-signal-sell">{m.lagging_sector ?? "-"}</b> {pct(m.lagging_change_pct)}</span>
        <span className="ml-auto">params {data.params_version} · research view, not advice</span>
      </div>
    </div>
  );
}
