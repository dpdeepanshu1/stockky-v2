// frontend/src/components/WatchlistPulse.tsx  (design v5 - watchlist summary; styles in design-v5.css)
// Presentation only. Takes the "% since added" figures the Watchlist already computed (percentages, never money) and sums them up in four tiles.
// Direction is always shown with an arrow and a sign next to the colour.
import { formatPct } from "../watchlistGain";

export type PulseItem = { symbol: string; pct: number | null };

export default function WatchlistPulse({ items, onAnalyse }: { items: PulseItem[]; onAnalyse: (symbol: string) => void }) {
  const known = items.filter((i): i is { symbol: string; pct: number } => i.pct !== null);
  const up = known.filter((i) => i.pct > 0).length;
  const down = known.filter((i) => i.pct < 0).length;
  const best = known.length ? known.reduce((a, b) => (b.pct > a.pct ? b : a)) : null;
  const worst = known.length ? known.reduce((a, b) => (b.pct < a.pct ? b : a)) : null;
  const showWorst = worst && best && worst.symbol !== best.symbol;
  return (
    <div className="v5-pulse" role="group" aria-label="Watchlist summary">
      <div className="v5-pulse-tile">
        <small>Tracking</small>
        <b className="num">{items.length}</b>
        <span>{items.length === 1 ? "stock" : "stocks"}</span>
      </div>
      <div className="v5-pulse-tile">
        <small>Since you added</small>
        <b className="num"><span className="up">▲ {up}</span> <span className="down">▼ {down}</span></b>
        <span>{known.length ? `${known.length} with a price` : "waiting for prices"}</span>
      </div>
      <button type="button" className="v5-pulse-tile v5-pulse-btn" disabled={!best} onClick={() => best && onAnalyse(best.symbol)}>
        <small>Best mover</small>
        <b className={best && best.pct < 0 ? "down" : "up"}>{best ? best.symbol : "—"}</b>
        <span className="num">{best ? `${best.pct < 0 ? "▼" : "▲"} ${formatPct(best.pct)}` : "no data yet"}</span>
      </button>
      <button type="button" className="v5-pulse-tile v5-pulse-btn" disabled={!showWorst} onClick={() => showWorst && worst && onAnalyse(worst.symbol)}>
        <small>Weakest</small>
        <b className={showWorst && worst && worst.pct >= 0 ? "up" : "down"}>{showWorst && worst ? worst.symbol : "—"}</b>
        <span className="num">{showWorst && worst ? `${worst.pct < 0 ? "▼" : "▲"} ${formatPct(worst.pct)}` : "no data yet"}</span>
      </button>
    </div>
  );
}
