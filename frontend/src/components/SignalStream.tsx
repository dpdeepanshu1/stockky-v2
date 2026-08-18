import { decisionStyle } from "../decisionStyle";

type Row = {
  symbol: string;
  decision?: string;
  combined_score?: number;
  close?: number;
  target?: number;
  stop_loss?: number;
  provisional?: boolean;
  data_quality?: { pillars?: Record<string, boolean>; provisional?: boolean; live_count?: number; total_pillars?: number };
  natural_language_summary?: string;
  sector?: string;
};

type Props = {
  rows: Row[];
  title?: string;
  onSelect?: (symbol: string) => void;
  filter?: "ALL" | "BUY" | "PREPARE" | "HOLD";
  onFilter?: (f: "ALL" | "BUY" | "PREPARE" | "HOLD") => void;
  partial?: boolean;
};

export default function SignalStream({
  rows,
  title = "Live Signal Stream",
  onSelect,
  filter = "ALL",
  onFilter,
  partial,
}: Props) {
  const filtered = rows.filter((r) => {
    const d = (r.decision || "").toUpperCase();
    if (filter === "ALL") return true;
    if (filter === "BUY") return d.includes("BUY NOW");
    if (filter === "PREPARE") return d.includes("PREPARE");
    if (filter === "HOLD") return d.includes("HOLD") || d.includes("WAIT") || d.includes("DO NOT");
    return true;
  });

  return (
    <section className="terminal-panel mb-4">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div>
          <h3 className="topo-title" style={{ margin: 0 }}>
            (( )) {title}
          </h3>
          <p className="topo-sub" style={{ margin: "0.25rem 0 0" }}>
            Ranked from last market scan{partial ? " (partial — stopped early)" : ""}. Real data only.
          </p>
        </div>
        <div className="chip-row" style={{ margin: 0 }}>
          {(["ALL", "BUY", "PREPARE", "HOLD"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className={`chip ${filter === f ? "active" : ""}`}
              onClick={() => onFilter?.(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <p className="mono text-xs text-mist py-6 text-center">No signals in this filter — run Market Scan.</p>
      ) : (
        filtered.slice(0, 12).map((r) => {
          const ds = decisionStyle(r.decision || "");
          const prov = r.provisional || r.data_quality?.provisional;
          const pillars = r.data_quality?.pillars;
          return (
            <article
              key={r.symbol}
              className="signal-stream-card cursor-pointer"
              onClick={() => onSelect?.(r.symbol)}
              onKeyDown={(e) => e.key === "Enter" && onSelect?.(r.symbol)}
              role="button"
              tabIndex={0}
            >
              <div className="signal-head">
                <div>
                  <div className="signal-sym">{r.symbol}</div>
                  <div className="signal-name">{r.sector || "NSE"}</div>
                </div>
                <span
                  className={
                    (r.decision || "").includes("BUY NOW")
                      ? "signal-pill-buy"
                      : (r.decision || "").includes("PREPARE")
                        ? "signal-pill-prepare"
                        : "signal-pill-prepare"
                  }
                >
                  {r.decision || "—"}
                </span>
              </div>
              {prov && (
                <div className="mono text-[10px] text-amber-400 mb-2 uppercase tracking-wide">
                  Provisional — not full conviction
                </div>
              )}
              <div className="signal-metrics">
                <div>
                  <label>Conviction</label>
                  <b>{r.combined_score != null ? Math.round(r.combined_score) : "—"}/100</b>
                </div>
                <div>
                  <label>Price</label>
                  <b>₹{r.close != null ? r.close.toLocaleString("en-IN") : "—"}</b>
                </div>
                <div>
                  <label>Target</label>
                  <b style={{ color: "var(--buy)" }}>
                    {r.target != null ? `₹${r.target.toLocaleString("en-IN")}` : "—"}
                  </b>
                </div>
                <div>
                  <label>Stop</label>
                  <b style={{ color: "var(--sell)" }}>
                    {r.stop_loss != null ? `₹${r.stop_loss.toLocaleString("en-IN")}` : "—"}
                  </b>
                </div>
              </div>
              {pillars && (
                <div className="pillar-row">
                  {Object.entries(pillars).map(([k, v]) => (
                    <span key={k} className={`pillar-chip ${v ? "pass" : "fail"}`}>
                      {k.slice(0, 4).toUpperCase()} {v ? "PASS" : "MISS"}
                    </span>
                  ))}
                </div>
              )}
              {r.natural_language_summary && (
                <p className="cc-blurb mt-2" style={{ marginBottom: 0 }}>
                  {String(r.natural_language_summary).slice(0, 140)}
                </p>
              )}
            </article>
          );
        })
      )}
    </section>
  );
}

/** Market breadth strip — only shows numbers when provided (no fabrication). */
export function BreadthStrip(props: {
  advancing?: number | null;
  declining?: number | null;
  mood?: string | null;
  niftyChangePct?: number | null;
}) {
  const adv = props.advancing;
  const dec = props.declining;
  const total = (adv ?? 0) + (dec ?? 0);
  const pct = total > 0 && adv != null ? Math.round((adv / total) * 100) : null;

  return (
    <div className="breadth-panel">
      <div className="topo-title" style={{ marginBottom: "0.5rem" }}>
        Market Breadth
      </div>
      {pct != null ? (
        <>
          <div className="flex justify-between mono text-xs">
            <span style={{ color: "var(--buy)" }}>Advancing {adv}</span>
            <span style={{ color: "var(--sell)" }}>Declining {dec}</span>
          </div>
          <div
            className="breadth-bar"
            style={{ ["--adv" as any]: `${pct}%`, background: `linear-gradient(90deg, var(--buy) 0%, var(--buy) ${pct}%, var(--sell) ${pct}%, var(--sell) 100%)` }}
          />
          <div className="mono text-xs text-mist">
            Breadth {pct}% bullish
            {props.mood ? ` · Mood ${props.mood}` : ""}
            {props.niftyChangePct != null ? ` · Nifty ${props.niftyChangePct > 0 ? "+" : ""}${props.niftyChangePct.toFixed(2)}%` : ""}
          </div>
        </>
      ) : (
        <p className="mono text-xs text-mist">
          {props.mood ? `Market mood: ${props.mood}` : "Run a market scan for breadth stats."}
          {props.niftyChangePct != null
            ? ` Nifty ${props.niftyChangePct > 0 ? "+" : ""}${props.niftyChangePct.toFixed(2)}%.`
            : ""}
        </p>
      )}
    </div>
  );
}
