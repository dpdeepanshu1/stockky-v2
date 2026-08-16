import { decisionStyle } from "../decisionStyle";

/** Minimal decision-like shape so card works from scan, hot stocks, or full Decision */
export type ConvictionData = {
  symbol: string;
  decision?: string;
  combined_score?: number | null;
  score?: number | null;
  confidence?: string | null;
  close?: number | null;
  entry_range?: { low: number | null; high: number | null } | null;
  entry_range_low?: number | null;
  entry_range_high?: number | null;
  target?: number | null;
  stop_loss?: number | null;
  holding_period?: string | null;
  holding_period_estimate?: {
    label?: string;
    min_days?: number;
    max_days?: number;
  } | null;
  technical_score?: number | null;
  fundamental_score?: number | null;
  news_score?: number | null;
  prediction_score?: number | null;
  market_score?: number | null;
  training_score?: number | null;
  natural_language_summary?: string | null;
  reasons?: {
    technical?: string[];
    fundamental?: string[];
    news?: string[];
    prediction?: string[];
    event?: string[];
    market?: string[];
    training?: string[];
  } | string[] | null;
  summary?: string | null;
  sector?: string | null;
  valuation?: string | null;
  data_quality?: {
    quality?: string;
    pillars?: Record<string, boolean | string>;
    missing?: string[];
    note?: string;
  } | string | null;
  data_insufficient?: boolean;
  news_data?: { summary?: string } | null;
  event_data?: { summary?: string; next_earnings_date?: string } | null;
  circuit_open?: string | null;
  live_price?: number | null;
  quote_as_of?: string | null;
};

type Props = {
  data: ConvictionData;
  rank?: number;
  compact?: boolean;
  onSelect?: (symbol: string) => void;
  footer?: React.ReactNode;
};

function fmt(n: number | null | undefined, digits = 2) {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function upsidePct(close?: number | null, target?: number | null) {
  if (close == null || target == null || close === 0) return null;
  return ((target - close) / close) * 100;
}

function collectReasons(data: ConvictionData): string[] {
  if (Array.isArray(data.reasons)) return data.reasons.slice(0, 6);
  const r = data.reasons || {};
  const out: string[] = [];
  for (const key of ["technical", "fundamental", "news", "prediction", "event", "market", "training"] as const) {
    const arr = (r as any)[key];
    if (Array.isArray(arr)) out.push(...arr.slice(0, 2));
  }
  return out.slice(0, 6);
}


function QualityGate({ data }: { data: ConvictionData }) {
  const dq = data.data_quality;
  let quality = "";
  let missing: string[] = [];
  let note = "";
  if (typeof dq === "string") quality = dq;
  else if (dq && typeof dq === "object") {
    quality = String(dq.quality || "");
    missing = Array.isArray(dq.missing) ? dq.missing.map(String) : [];
    note = dq.note ? String(dq.note) : "";
  }
  if (data.data_insufficient && !quality) quality = "low";
  if (data.circuit_open) {
    quality = quality || "low";
    note = note || `Circuit open: ${data.circuit_open}`;
  }
  if (!quality && !note && !missing.length) return null;
  const q = quality.toLowerCase();
  const cls =
    q === "high" ? "qg-high" : q === "medium" || q === "med" ? "qg-med" : "qg-low";
  return (
    <div className={`quality-gate ${cls}`}>
      <span className="mono">DATA {quality ? quality.toUpperCase() : "CHECK"}</span>
      {missing.length > 0 && (
        <span className="qg-miss">Missing: {missing.slice(0, 4).join(", ")}</span>
      )}
      {note && <span className="qg-note">{note}</span>}
      {(data.news_data?.summary || data.event_data?.summary) && (
        <span className="qg-sum">
          {[data.news_data?.summary, data.event_data?.summary].filter(Boolean).slice(0, 1).join(" · ")}
        </span>
      )}
    </div>
  );
}

export default function ConvictionCard({ data, rank, compact, onSelect, footer }: Props) {
  const decision = data.decision || "DO NOT BUY";
  const style = decisionStyle[decision] ?? decisionStyle["DO NOT BUY"];
  const score = data.combined_score ?? data.score ?? null;
  const entryLow = data.entry_range?.low ?? data.entry_range_low ?? null;
  const entryHigh = data.entry_range?.high ?? data.entry_range_high ?? null;
  const up = upsidePct(data.close, data.target);
  const horizon =
    data.holding_period_estimate?.label ||
    data.holding_period ||
    "—";
  const reasoning =
    data.natural_language_summary ||
    data.summary ||
    collectReasons(data).slice(0, 2).join(" · ") ||
    "No AI narrative available for this pick.";
  const reasons = collectReasons(data);

  return (
    <article
      className={`conviction-card ${compact ? "conviction-card-compact" : ""}`}
      onClick={() => onSelect?.(data.symbol)}
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onKeyDown={(e) => {
        if (onSelect && (e.key === "Enter" || e.key === " ")) onSelect(data.symbol);
      }}
    >
      <header className="conviction-head">
        <div className="conviction-sym-row">
          {rank != null && <span className="conviction-rank mono">#{rank}</span>}
          <span className="conviction-symbol mono">{data.symbol}</span>
          {data.sector && <span className="conviction-sector">{data.sector}</span>}
        </div>
        <span className={`decision-badge ${style.color} ${style.bg} ${style.border}`}>
          {style.glyph} {decision}
        </span>
      </header>

      <QualityGate data={data} />

      <div className="conviction-score-row mono">
        <span>
          Score <strong>{score != null ? Math.round(Number(score)) : "—"}</strong>
          <span className="conviction-muted">/100</span>
        </span>
        {data.confidence && (
          <span className="conviction-muted">{data.confidence} confidence</span>
        )}
        {(data.live_price ?? data.close) != null && (
          <span className="conviction-muted">₹{fmt(data.live_price ?? data.close)}</span>
        )}
      </div>

      <div className="conviction-levels mono">
        <div>
          <span className="conviction-muted">Entry</span>
          <strong>
            {entryLow != null || entryHigh != null
              ? `₹${fmt(entryLow)}–${fmt(entryHigh)}`
              : "—"}
          </strong>
        </div>
        <div>
          <span className="conviction-muted">Target</span>
          <strong className="text-signal-buy">₹{fmt(data.target)}</strong>
          {up != null && <span className="conviction-up">+{up.toFixed(1)}%</span>}
        </div>
        <div>
          <span className="conviction-muted">Stop</span>
          <strong className="text-signal-sell">₹{fmt(data.stop_loss)}</strong>
        </div>
        <div>
          <span className="conviction-muted">Horizon</span>
          <strong>{horizon}</strong>
        </div>
      </div>

      {!compact && (
        <>
          <div className="conviction-breakdown mono">
            {[
              ["Tech", data.technical_score],
              ["Fund", data.fundamental_score],
              ["News", data.news_score],
              ["Pred", data.prediction_score],
              ["Mkt", data.market_score],
              ["Train", data.training_score],
            ].map(([label, val]) => (
              <span key={String(label)}>
                {label} <strong>{val != null ? Math.round(Number(val)) : "—"}</strong>
              </span>
            ))}
          </div>

          <p className="conviction-reasoning">{reasoning}</p>

          {reasons.length > 0 && (
            <ul className="conviction-reasons">
              {reasons.slice(0, 4).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </>
      )}

      {footer}
    </article>
  );
}
