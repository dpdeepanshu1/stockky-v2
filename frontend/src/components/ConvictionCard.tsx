import { decisionStyle } from "../decisionStyle";
import { resolveDisplayPrice, formatInrPrice } from "../priceDisplay";
import { ScoreRing } from "./ui/ScoreRing";          // E160: v5/v7 pieces, presentation only
import { TradePlanBar } from "./ui/TradePlanBar";
import { Card } from "./kit/Card";   // E550: the card is a kit Card; its inner rules are mk-cc-* in styles/markets.css

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
    live_count?: number;
    total_pillars?: number;
    provisional?: boolean;
    live_win_rate_n?: number;
  } | string | null;
  provisional?: boolean;
  technical_thin?: boolean;
  block_buy_now?: boolean;
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
  const cls = q === "high" ? "high" : q === "medium" || q === "med" ? "med" : "low";
  return (
    <div className={`mk-cc-quality ${cls}`}>
      <span className="mk-cc-badge">DATA {quality ? quality.toUpperCase() : "CHECK"}</span>
      {note && <span className="mk-cc-note">{shortText(note, 80)}</span>}
    </div>
  );
}

export default function ConvictionCard({ data, rank, compact, onSelect, footer }: Props) {
  const decision = data.decision || "DO NOT BUY";
  const style = decisionStyle[decision] ?? decisionStyle["DO NOT BUY"];
  const score = data.combined_score ?? data.score ?? null;
  const entryLow = data.entry_range?.low ?? data.entry_range_low ?? null;
  const entryHigh = data.entry_range?.high ?? data.entry_range_high ?? null;
  const up = upsidePct(resolveDisplayPrice(data, data.live_price), data.target);
  const horizon = data.holding_period_estimate?.label || data.holding_period || "—";
  const reasons = collectReasons(data);
  // E160: a real 0 is a score (F-11), so test for a finite number, not for truthiness.
  const scoreNum = score != null && Number.isFinite(Number(score)) ? Number(score) : null;
  const showRing = !compact && scoreNum != null;
  const shownPx = resolveDisplayPrice(data, data.live_price);
  // Same rule as DecisionCard: the buy zone is only drawn for a buy-side call, never for HOLD / DO NOT BUY.
  const showPlan = !compact && (decision === "BUY NOW" || decision === "PREPARE TO BUY");
  const blurb =
    shortText(
      data.natural_language_summary ||
        data.summary ||
        reasons.slice(0, 2).join(" · ") ||
        "",
      compact ? 120 : 160
    ) || null;

  return (
    <Card
      className={`mk-cc ${compact ? "compact" : ""}`}
      onClick={() => onSelect?.(data.symbol)}
      role={onSelect ? "button" : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onKeyDown={(e) => {
        if (onSelect && (e.key === "Enter" || e.key === " ")) onSelect(data.symbol);
      }}
    >
      <div className="mk-cc-top">
        <div className="mk-cc-sym">
          {rank != null && <span className="mk-cc-rank">#{rank}</span>}
          <span className="mk-cc-symbol">{data.symbol}</span>
          {data.sector && <span className="mk-cc-sector">{data.sector}</span>}
        </div>
        <span className={`mk-cc-decision ${style.color}`}>{decision}</span>
      </div>

      <QualityGate data={data} />
      {data.technical_thin === true && (
        <div className="mk-cc-quality med" title="Technical read rests on a minimal / fallback price history (label only, score unchanged)">
          <span className="mk-cc-badge">THIN TECH</span>
          <span className="mk-cc-note">Technicals on minimal price history</span>
        </div>
      )}

      {(showRing || showPlan) && (
        <div className="mk-cc-hero">
          {showRing && <ScoreRing score={scoreNum as number} size={76} />}
          {showPlan && (
            <TradePlanBar stop={data.stop_loss} entryLow={entryLow} entryHigh={entryHigh} target={data.target} price={shownPx > 0 ? shownPx : null} />
          )}
        </div>
      )}

      <div className="mk-cc-grid">
        {!showRing && (
          <div className="mk-cc-cell">
            <span className="mk-cc-label">Score</span>
            <span className="mk-cc-value">
              {score != null ? Math.round(Number(score)) : "—"}
              <span className="mk-cc-muted">/100</span>
            </span>
          </div>
        )}
        <div className="mk-cc-cell">
          <span className="mk-cc-label">Price</span>
          <span className="mk-cc-value">
            {(() => {
              const px = resolveDisplayPrice(data, data.live_price);
              // px === 0 means "no positive price found under any known
              // key" (see priceDisplay.ts), not "the stock is worth ₹0" —
              // rendering "₹0" here was the literal bug (Hot Picks
              // Results/Earnings cards for symbols outside the feed
              // store, e.g. purged over-₹5000-cap names, showed "₹0"
              // instead of an honest "no price").
              return px > 0 ? `₹${fmt(px)}` : "₹—";
            })()}
          </span>
        </div>
        {data.confidence && (
          <div className="mk-cc-cell">
            <span className="mk-cc-label">Conf</span>
            <span className="mk-cc-value mk-cc-muted">{data.confidence}</span>
          </div>
        )}
      </div>

      <div className="mk-cc-grid">
        <div className="mk-cc-cell">
          <span className="mk-cc-label">Entry</span>
          <span className="mk-cc-value">
            {entryLow != null || entryHigh != null ? `₹${fmt(entryLow)}–${fmt(entryHigh)}` : "—"}
          </span>
        </div>
        <div className="mk-cc-cell">
          <span className="mk-cc-label">Target</span>
          <span className="mk-cc-value text-signal-buy">
            ₹{fmt(data.target)}
            {up != null && <span className="mk-cc-up"> +{up.toFixed(1)}%</span>}
          </span>
        </div>
        <div className="mk-cc-cell">
          <span className="mk-cc-label">Stop loss</span>
          <span className="mk-cc-value text-signal-sell">₹{fmt(data.stop_loss)}</span>
        </div>
        <div className="mk-cc-cell">
          <span className="mk-cc-label">Horizon</span>
          <span className="mk-cc-value">{horizon}</span>
        </div>
      </div>

      {!compact && (
        <>
          <div className="mk-cc-chips">
            {[
              ["Tech", data.technical_score],
              ["Fund", data.fundamental_score],
              ["News", data.news_score],
              ["Pred", data.prediction_score],
              ["Mkt", data.market_score],
              ["Train", data.training_score],
            ].map(([label, val]) => (
              <span key={String(label)} className="mk-cc-chip">
                <span className="mk-cc-label">{label}</span>{" "}
                <strong>{val != null ? Math.round(Number(val)) : "—"}</strong>
              </span>
            ))}
          </div>
          {typeof data.data_quality === "object" && data.data_quality?.pillars && (
            <div className="mk-cc-pillars">
              {Object.entries(data.data_quality.pillars).map(([k, v]) => (
                <span key={k} className={`mk-cc-pillar ${v ? "pass" : "fail"}`}>
                  {k.toUpperCase().slice(0, 4)} {v ? "PASS" : "MISS"}
                </span>
              ))}
            </div>
          )}
          {blurb && <p className="mk-cc-blurb">{blurb}</p>}
          {reasons.length > 0 && (
            <ul className="mk-cc-reasons">
              {reasons.slice(0, 3).map((r, i) => (
                <li key={i}>{shortText(r, 100)}</li>
              ))}
            </ul>
          )}
        </>
      )}

      {footer && <div className="mk-cc-footer" onClick={(e) => e.stopPropagation()}>{footer}</div>}
    </Card>
  );
}
