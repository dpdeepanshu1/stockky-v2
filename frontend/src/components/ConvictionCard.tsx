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
    level?: string;
    pillars?: Record<string, boolean | string>;
    missing?: string[];
    note?: string;
    flags?: string[];
  } | string | null;
  data_insufficient?: boolean;
  news_data?: { summary?: string; headline_count?: number } | null;
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

function shortText(s: string, max = 140) {
  const t = (s || "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  return t.slice(0, max - 1) + "…";
}

function QualityGate({ data }: { data: ConvictionData }) {
  const dq = data.data_quality;
  let quality = "";
  let note = "";
  if (typeof dq === "string") quality = dq;
  else if (dq && typeof dq === "object") {
    quality = String(dq.level || dq.quality || "");
    note = dq.note ? String(dq.note) : "";
    if (!note && Array.isArray(dq.flags) && dq.flags.length) note = dq.flags[0];
  }
  if (data.data_insufficient && !quality) quality = "low";
  if (!quality && !note) return null;
  const q = quality.toLowerCase();
  const cls = q === "high" ? "qg-high" : q === "medium" || q === "med" ? "qg-med" : "qg-low";
  return (
    <div className={`cc-quality ${cls}`}>
      <span className="cc-quality-badge">DATA {quality ? quality.toUpperCase() : "CHECK"}</span>
      {note && <span className="cc-quality-note">{shortText(note, 80)}</span>}
    </div>
  );
}

export default function ConvictionCard({ data, rank, compact, onSelect, footer }: Props) {
  const decision = data.decision || "DO NOT BUY";
  const style = decisionStyle[decision] ?? decisionStyle["DO NOT BUY"];
  const score = data.combined_score ?? data.score ?? null;
  const entryLow = data.entry_range?.low ?? data.entry_range_low ?? null;
  const entryHigh = data.entry_range?.high ?? data.entry_range_high ?? null;
  const up = upsidePct(data.close ?? data.live_price, data.target);
  const horizon = data.holding_period_estimate?.label || data.holding_period || "—";
  const reasons = collectReasons(data);
  const blurb =
    shortText(
      data.natural_language_summary ||
        data.summary ||
        reasons.slice(0, 2).join(" · ") ||
        "",
      compact ? 120 : 160
    ) || null;

  return (
    <article
      className={`cc-card ${compact ? "cc-compact" : ""}`}
      onClick={() => onSelect?.(data.symbol)}
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onKeyDown={(e) => {
        if (onSelect && (e.key === "Enter" || e.key === " ")) onSelect(data.symbol);
      }}
    >
      <div className="cc-top">
        <div className="cc-sym-block">
          {rank != null && <span className="cc-rank">#{rank}</span>}
          <span className="cc-symbol">{data.symbol}</span>
          {data.sector && <span className="cc-sector">{data.sector}</span>}
        </div>
        <span className={`cc-decision ${style.color}`}>{decision}</span>
      </div>

      <QualityGate data={data} />

      <div className="cc-metrics">
        <div className="cc-metric">
          <span className="cc-label">Score</span>
          <span className="cc-value">
            {score != null ? Math.round(Number(score)) : "—"}
            <span className="cc-muted">/100</span>
          </span>
        </div>
        <div className="cc-metric">
          <span className="cc-label">Price</span>
          <span className="cc-value">₹{fmt(data.live_price ?? data.close)}</span>
        </div>
        {data.confidence && (
          <div className="cc-metric">
            <span className="cc-label">Conf</span>
            <span className="cc-value cc-muted">{data.confidence}</span>
          </div>
        )}
      </div>

      <div className="cc-levels">
        <div>
          <span className="cc-label">Entry</span>
          <span className="cc-value">
            {entryLow != null || entryHigh != null ? `₹${fmt(entryLow)}–${fmt(entryHigh)}` : "—"}
          </span>
        </div>
        <div>
          <span className="cc-label">Target</span>
          <span className="cc-value text-signal-buy">
            ₹{fmt(data.target)}
            {up != null && <span className="cc-up"> +{up.toFixed(1)}%</span>}
          </span>
        </div>
        <div>
          <span className="cc-label">Stop</span>
          <span className="cc-value text-signal-sell">₹{fmt(data.stop_loss)}</span>
        </div>
        <div>
          <span className="cc-label">Horizon</span>
          <span className="cc-value">{horizon}</span>
        </div>
      </div>

      {!compact && (
        <>
          <div className="cc-breakdown">
            {[
              ["Tech", data.technical_score],
              ["Fund", data.fundamental_score],
              ["News", data.news_score],
              ["Pred", data.prediction_score],
              ["Mkt", data.market_score],
              ["Train", data.training_score],
            ].map(([label, val]) => (
              <span key={String(label)} className="cc-chip">
                <span className="cc-label">{label}</span>{" "}
                <strong>{val != null ? Math.round(Number(val)) : "—"}</strong>
              </span>
            ))}
          </div>
          {blurb && <p className="cc-blurb">{blurb}</p>}
          {reasons.length > 0 && (
            <ul className="cc-reasons">
              {reasons.slice(0, 3).map((r, i) => (
                <li key={i}>{shortText(r, 100)}</li>
              ))}
            </ul>
          )}
        </>
      )}

      {footer && <div className="cc-footer" onClick={(e) => e.stopPropagation()}>{footer}</div>}
    </article>
  );
}
