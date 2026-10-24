// frontend/src/components/app/AiInsightDeck.tsx  (design v5 - new dashboard sub-section)
// Presentation only: reads what the dashboard already has (scan rows / progress) and shows it as one calm card.
// No API call, no new state. idle -> "how it works" in 3 steps; loading -> live progress; results -> counts + top picks.
import type { Decision } from "../../api";
import type { ViewState } from "./appTypes";
import { AiOrb, AiTyping, type AiOrbState } from "../ui/AiOrb";

type Props = {
  view: ViewState;
  liveScanRows: Decision[];
  onSelect: (symbol: string) => unknown;
  onQuickScan: () => unknown;
};

const isBuy = (d: Decision) => d.decision === "BUY NOW";
const isPrep = (d: Decision) => d.decision === "PREPARE TO BUY";
const isAvoid = (d: Decision) => d.decision === "DO NOT BUY" || d.decision === "SELL";

function rowsFor(view: ViewState, live: Decision[]): Decision[] {
  if (view.mode === "scan") return view.data.all_results || view.data.recommendations || [];
  if (view.mode === "stock") return [view.data];
  if (view.mode === "loading") return live;
  return [];
}

const STEPS = [
  { n: "1", t: "Pick a stock", d: "Type a symbol or run a scan." },
  { n: "2", t: "AI reads 5 signals", d: "Chart, company, news, events, prediction." },
  { n: "3", t: "Get one clear call", d: "Buy, prepare, hold or avoid, with target and stop." },
];

export default function AiInsightDeck({ view, liveScanRows, onSelect, onQuickScan }: Props) {
  const rows = rowsFor(view, liveScanRows);
  const loading = view.mode === "loading";
  const orb: AiOrbState = loading ? "thinking" : rows.length ? "done" : "idle";

  const buy = rows.filter(isBuy).length;
  const prep = rows.filter(isPrep).length;
  const avoid = rows.filter(isAvoid).length;
  const hold = rows.length - buy - prep - avoid;

  const picks = rows
    .filter((d) => isBuy(d) || isPrep(d))
    .sort((a, b) => (b.combined_score ?? 0) - (a.combined_score ?? 0))
    .slice(0, 3);

  let pct = 0;
  if (view.mode === "loading" && view.progress) {
    pct = Math.min(100, Math.round((view.progress.processed / Math.max(1, view.progress.total)) * 100));
  }

  let title = "AI is ready";
  let sub = "Ask about any stock, or let the AI scan the market for you.";
  if (loading) {
    title = pct > 0 ? `AI is scanning, ${pct}% done` : "AI is thinking";
    sub = rows.length ? `${rows.length} stocks checked so far. Top picks appear below as they arrive.` : view.mode === "loading" ? view.label : "";
  } else if (rows.length > 1) {
    title = `${buy + prep} idea${buy + prep === 1 ? "" : "s"} worth a look`;
    sub = `From ${rows.length} stocks checked. Tap a pick to see the full reasoning.`;
  } else if (rows.length === 1) {
    title = `${rows[0].symbol}: ${rows[0].decision.toLowerCase()}`;
    sub = "Full reasoning is shown below.";
  }

  return (
    <section className="v5-deck" aria-label="AI insights" aria-live="polite">
      <div className="v5-deck-head">
        <AiOrb state={orb} size={64} />
        <div className="v5-deck-text">
          <div className="v5-eyebrow">AI INSIGHTS {loading && <AiTyping />}</div>
          <h2 className="v5-deck-title">{title}</h2>
          <p className="v5-deck-sub">{sub}</p>
        </div>
        {!loading && rows.length === 0 && (
          <button type="button" className="v5-cta" onClick={() => onQuickScan()}>Try a quick scan</button>
        )}
      </div>

      {loading && (
        <div className="v5-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
          <span className="v5-progress-bar" style={{ transform: `scaleX(${pct / 100})` }} />
          <span className="v5-progress-sweep" />
        </div>
      )}

      {rows.length > 1 && (
        <div className="v5-counts" role="list">
          <span className="v5-count buy" role="listitem"><b>{buy}</b> Buy now</span>
          <span className="v5-count prep" role="listitem"><b>{prep}</b> Prepare</span>
          <span className="v5-count hold" role="listitem"><b>{hold}</b> Hold</span>
          <span className="v5-count avoid" role="listitem"><b>{avoid}</b> Avoid</span>
        </div>
      )}

      {picks.length > 0 && rows.length > 1 && (
        <div className="v5-picks">
          {picks.map((d, i) => {
            const score = Math.max(0, Math.min(100, Math.round(d.combined_score ?? 0)));
            return (
              <button type="button" key={d.symbol} className="v5-pick" style={{ animationDelay: `${i * 80}ms` }} onClick={() => onSelect(d.symbol)}>
                <span className="v5-pick-top">
                  <span className="v5-pick-sym">{d.symbol}</span>
                  <span className={`v5-pick-tag ${isBuy(d) ? "buy" : "prep"}`}>{isBuy(d) ? "▲ Buy now" : "◆ Prepare"}</span>
                </span>
                <span className="v5-pick-meter" aria-hidden="true"><span style={{ transform: `scaleX(${score / 100})` }} /></span>
                <span className="v5-pick-meta">AI score <b className="num">{score}</b>/100 · {d.confidence} confidence</span>
              </button>
            );
          })}
        </div>
      )}

      {view.mode === "idle" && (
        <ol className="v5-steps">
          {STEPS.map((s) => (
            <li key={s.n} className="v5-step">
              <span className="v5-step-n">{s.n}</span>
              <span><b>{s.t}</b><small>{s.d}</small></span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
