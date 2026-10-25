// group338: "Top picks" - the best 3 to 5 stocks as cards, one component for both services. Each card: rank, symbol, side badge, score ring, day move,
// price, a sparkline, where price sits between the stop and the target, entry / stop / target / R:R, zone score and the reasons it ranked.
// Cards from the engine's own last shortlist show what became of the pick (entered / skipped / first in line); when the engine has few fresh
// picks the strongest bullish board rows fill the list, labelled "Board preview" (no entry gate or risk check has looked at those).
// Display only: it places no order. Polls only while its tab is visible.
import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { Card } from "./boardCard";
import StockPopup from "./StockPopup";
import { usePrefersReducedMotion } from "./useBoardData";
import type { BoardRowLite } from "./boardTypes";
import { levelPosition, normalizePicks, pickBadge, sparkPath, type PickCard, type PicksView } from "./topPicksView";

const px = (v: number | null, d = 2) => (v == null ? "-" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));

function Ring({ value }: { value: number | null }) {
  const v = value == null ? 0 : Math.max(0, Math.min(100, value)), r = 15, c = 2 * Math.PI * r;
  return (
    <svg className="tp-ring" width="38" height="38" viewBox="0 0 38 38" role="img" aria-label={value == null ? "score unavailable" : `score ${v.toFixed(0)} of 100`}>
      <circle cx="19" cy="19" r={r} className="tp-ring-bg" />
      <circle cx="19" cy="19" r={r} className="tp-ring-fg" strokeDasharray={`${(v / 100) * c} ${c}`} transform="rotate(-90 19 19)" />
      <text x="19" y="22.5" textAnchor="middle" className="tp-ring-t">{value == null ? "-" : v.toFixed(0)}</text>
    </svg>
  );
}

/** Popup footer for a pick: what the auto-buy engine did / will do with it. Plain text, no order is placed from here. */
function PickStatus({ p }: { p: PickCard }) {
  const badge = pickBadge(p);
  const msg = p.source === "board"
    ? "Board preview: ranked strong by the Market Board, but the entry engine has not shortlisted it. Entry, stop and target are approximations."
    : p.outcome === "ENTERED" ? "The engine entered this pick."
    : p.status === "SELECTED" ? "First in line: the next entry cycle tries this stock first, if every entry gate and risk check still pass."
    : p.status === "RESERVE" ? "Reserve: it is tried only if a pick ahead of it is skipped or its order fails."
    : "Shortlisted by the entry engine.";
  return (
    <div className="rounded-lg border border-slate bg-ink p-2 text-[11px] text-mist space-y-1" data-testid="pick-status">
      <div><b className="text-paper">#{p.pick} · {badge.label}</b></div>
      <div>{msg}</div>
    </div>
  );
}

function PickCardView({ p, onOpen }: { p: PickCard; onOpen: (p: PickCard) => void }) {
  const up = (p.changePct ?? 0) >= 0, badge = pickBadge(p);
  const pts = sparkPath(p.spark, 96, 28);
  const pos = levelPosition(p.stop, p.ltp ?? p.entry, p.target);
  return (
    <article className={`tp-card ${p.side === "bear" ? "bear" : "bull"}${p.inPosition ? " held" : ""}`} data-testid={`top-pick-${p.symbol}`}
      role="button" tabIndex={0} aria-label={`${p.symbol}: open chart and live momentum`} onClick={() => onOpen(p)}
      onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(p); } }}>
      <header className="tp-head">
        <span className="tp-rank">#{p.pick}</span>
        <div className="tp-id">
          <b className="tp-sym">{p.symbol}</b>
          <span className="tp-sub">{p.sector ? `${p.sector} · ` : ""}{p.name !== p.symbol ? p.name : ""}</span>
        </div>
        <Ring value={p.score} />
      </header>
      <div className="tp-badges">
        <span className={`tp-side ${p.side === "bear" ? "r" : "g"}`}>{p.side === "bear" ? "BEARISH" : "BULLISH"}</span>
        <span className={`tp-badge ${badge.tone}`}>{badge.label}</span>
        {p.inPosition && <span className="tp-badge wait">Held</span>}
        {p.extended && <span className="tp-badge bad">Extended</span>}
      </div>
      <div className="tp-mid">
        <div>
          <div className={`tp-chg ${up ? "g" : "r"}`}>{p.changePct == null ? "-" : `${up ? "+" : ""}${p.changePct.toFixed(2)}%`}</div>
          <div className="tp-px">₹{px(p.ltp ?? p.entry)}</div>
        </div>
        {pts && (
          <svg className="tp-spark" width="96" height="28" viewBox="0 0 96 28" aria-hidden="true">
            <polyline points={pts} fill="none" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" className={up ? "g" : "r"} />
          </svg>
        )}
      </div>
      {pos != null && (
        <div className="tp-range" title="Price between stop and target">
          <i style={{ left: `${(pos * 100).toFixed(1)}%` }} />
        </div>
      )}
      <dl className="tp-lv">
        <div><dt>Entry</dt><dd>{px(p.entry)}</dd></div>
        <div><dt>Stop loss</dt><dd className="r">{px(p.stop)}</dd></div>
        <div><dt>Target</dt><dd className="g">{px(p.target)}</dd></div>
        <div><dt>R:R</dt><dd>{p.rr == null ? "-" : p.rr.toFixed(1)}</dd></div>
        <div><dt>Zone</dt><dd>{p.zoneScore == null ? "-" : p.zoneScore.toFixed(0)}</dd></div>
        <div><dt>Conv.</dt><dd>{p.conviction == null ? "-" : p.conviction.toFixed(0)}</dd></div>
      </dl>
      {p.reasons.length > 0 && <div className="tp-why">{p.reasons.slice(0, 4).join(" · ")}</div>}
      <div className="tp-open">Tap for chart &amp; live momentum</div>
    </article>
  );
}

export default function TopPicks({ active, fetcher, title, note, resetKey }:
  { active: boolean; fetcher: () => Promise<any>; title: string; note?: string; resetKey?: string }) {
  const [view, setView] = useState<PicksView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<BoardRowLite | null>(null);
  const [selPick, setSelPick] = useState<PickCard | null>(null);
  const reduced = usePrefersReducedMotion();
  // a card opens the same stock popup the board tiles use (candle / line chart, zones, levels, live momentum); the card's own fields fill the header
  const open = (p: PickCard) => (setSelPick(p), setSel({ symbol: p.symbol, name: p.name, sector: p.sector ?? undefined, side: p.side ?? undefined, ltp: p.ltp ?? undefined,
    change_pct: p.changePct ?? undefined, conviction: p.conviction ?? undefined, rank: p.rank ?? undefined, zone_score: p.zoneScore ?? undefined,
    rr: p.rr ?? undefined, extended: p.extended }));

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try {
        const body = await fetcher();
        if (alive) { setView(normalizePicks(body)); setErr(null); }
      } catch (e: any) {
        if (alive) setErr(e?.message || "top picks unavailable");
      }
    };
    load();
    const stop = setVisibleInterval(load, 8000);
    return () => { alive = false; stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, resetKey]);

  const age = view?.asOf ? Math.max(0, Math.round((Date.now() - view.asOf) / 1000)) : null;
  const right = view
    ? <span className="text-[9px] text-mist tabular-nums">
        {view.enginePicks} from the last entry cycle · {view.previewPicks} board preview{age != null && view.engineFresh ? ` · cycle ${age < 90 ? `${age}s` : `${Math.round(age / 60)}m`} ago` : ""}
      </span>
    : undefined;
  return (
    <>
    <Card title={title} right={right}>
      <div data-testid="top-picks">
        {err && <div className="tp-msg warn">{err}</div>}
        {view && !view.picks.length && !err && <div className="tp-msg">{view.note || "No picks yet."}</div>}
        {view && view.picks.length > 0 && <div className="tp-grid">{view.picks.map(p => <PickCardView key={`${p.pick}-${p.symbol}`} p={p} onOpen={open} />)}</div>}
        {!view && !err && <div className="tp-msg">Loading the best picks…</div>}
        {note && <div className="tp-note">{note}</div>}
      </div>
    </Card>
    {/* outside the Card on purpose: the sheet is a fixed overlay and follows the app theme */}
    <StockPopup row={sel} onClose={() => setSel(null)} reduced={reduced} extra={selPick ? <PickStatus p={selPick} /> : undefined} />
    </>
  );
}
