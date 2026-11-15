// E536 (redesign round 4): the small display pieces of the decision card, moved out of DecisionCard.tsx (code unchanged except `export`).
import { useState } from "react";
import { Button, Card, Chip } from "../kit";   // E538 (F3 part 2): the kit card, ghost button and chip

// ── Helper Components ──

export function PriceLevelBar({ close, support, resistance }: { close: number; support: number; resistance: number }) {
  const range = resistance - support;
  const closePct = range > 0 ? ((close - support) / range) * 100 : 50;

  return (
    <div>
      <div className="relative h-2 rounded-full bg-slate overflow-visible mb-3">
        <div
          className="absolute h-full rounded-full bg-gradient-to-r from-signal-sell/30 to-signal-buy/30"
          style={{ width: "100%" }}
        />
        <div
          className="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-paper border-2 border-signal-prepare shadow-lg"
          style={{ left: `${Math.min(95, Math.max(5, closePct))}%`, transform: "translate(-50%, -50%)" }}
        />
      </div>
      <div className="flex justify-between font-display tabular-nums text-[10px]">
        <span className="text-signal-sell">S ₹{support.toLocaleString("en-IN")}</span>
        <span className="text-signal-prepare">₹{close.toLocaleString("en-IN")}</span>
        <span className="text-signal-buy">R ₹{resistance.toLocaleString("en-IN")}</span>
      </div>
    </div>
  );
}

export function ScoreBar({ label, value }: { label: string; value: number }) {
  const color = value >= 70 ? "bg-signal-buy" : value >= 50 ? "bg-signal-prepare" : "bg-signal-sell/60";
  return (
    <div>
      <div className="flex justify-between font-display tabular-nums text-[10px] text-mist mb-1">
        <span className="uppercase tracking-wide">{label}</span>
        <span className={value >= 70 ? "text-signal-buy" : value >= 50 ? "text-signal-prepare" : "text-signal-sell"}>{value}</span>
      </div>
      <div className="h-1 rounded-full bg-slate overflow-hidden">
        <div className={`h-full rounded-full ${color} transition-all duration-700`} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

// ReasonList with maxItems and expand (v1 style) but also accepts no maxItems
export function ReasonList({ title, items, maxItems }: { title: string; items: string[]; maxItems?: number }) {
  const [expanded, setExpanded] = useState(false);
  const hasItems = items && items.length > 0;
  const shouldTruncate = maxItems != null && items.length > maxItems && !expanded;
  const visibleItems = shouldTruncate ? items.slice(0, maxItems) : items;

  return (
    <Card>
      <h3 className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-3">{title}</h3>
      {hasItems ? (
        <>
          <ul className="space-y-2">
            {visibleItems.map((item, i) => (
              <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>{item}</span>
              </li>
            ))}
          </ul>
          {maxItems != null && items.length > maxItems && (
            <Button variant="ghost" size="sm" className="mt-2" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
              {expanded ? "Show less" : `+${items.length - maxItems} more`}
            </Button>
          )}
        </>
      ) : (
        <p className="text-sm text-mist/40 italic">No specific {title.toLowerCase()} insights available</p>
      )}
    </Card>
  );
}

export function MetricItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-xl px-3 py-2">
      <div className="font-display tabular-nums text-[9px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-display tabular-nums text-sm text-paper mt-0.5">{value}</div>
    </div>
  );
}

// ── Event Score badge (nature-based 0-100 score + top contributors) ──
export function EventScoreBadge({ data }: { data: any }) {
  const score: number | undefined =
    data?.event_score ?? data?.event_data?.event_score ?? data?.events?.event_score;
  const breakdown: any[] =
    data?.event_score_breakdown ?? data?.event_data?.event_score_breakdown ?? data?.events?.event_score_breakdown ?? [];
  // "Natural"/uncompressed score — the signed raw delta before it gets
  // squashed onto the 0-100/50=neutral scale (see event_depth.py
  // compute_event_score). Was always computed backend-side but wasn't being
  // forwarded into the API response, so the UI only ever showed the
  // compressed score (which reads as a flat "50/100" whenever the raw delta
  // is small or events are thin) with no way to see the underlying signal.
  const rawDelta: number | undefined =
    data?.event_score_raw_delta ?? data?.event_data?.event_score_raw_delta ?? data?.events?.event_score_raw_delta;
  if (score == null) return null;

  // E538: the score and contributor badges are kit chips; the direction is a symbol as well as a colour.
  const scoreTone = score >= 65 ? "up" : score >= 50 ? "warn" : "down";
  const scoreSym = score >= 65 ? "▲" : score >= 50 ? "◆" : "▼";

  const top = [...breakdown]
    .sort((a, b) => Math.abs(b.decayed_impact ?? 0) - Math.abs(a.decayed_impact ?? 0))
    .slice(0, 3);

  return (
    <div className="mt-2 mb-3 flex flex-wrap items-center gap-2">
      <Chip as="span" tone={scoreTone} symbol={scoreSym}>
        Event Score {Math.round(score)}/100
      </Chip>
      {rawDelta != null && (
        <Chip
          as="span"
          title="Natural/raw score before the 0-100 neutral-50 compression — the signed sum of every event's recency-decayed impact"
        >
          natural {rawDelta >= 0 ? "+" : ""}
          {rawDelta}
        </Chip>
      )}
      {top.map((b, i) => {
        const pos = (b.decayed_impact ?? 0) >= 0;
        return (
          <Chip
            as="span"
            key={i}
            tone={pos ? "up" : "down"}
            symbol={pos ? "▲" : "▼"}
            title={`base ${b.base_impact} · age ${b.age_days ?? "?"}d`}
          >
            {String(b.type || "").replace(/_/g, " ")} {pos ? "+" : ""}
            {b.decayed_impact}
          </Chip>
        );
      })}
    </div>
  );
}

// ── HorizonStrip (v2) ──


export function HorizonStrip({ data }: { data: any }) {
  const hz = data?.horizons || {};
  const order = ["short", "mid", "long"] as const;
  const fv = data?.final_verdict;
  const hasCards = order.some((k) => hz[k]);
  if (!fv && !hasCards) return null;
  return (
    <div className="dh-strip">
      {fv && (
        <div className="dh-verdict">
          <div className="dh-verdict-title">Final verdict · short preferred</div>
          <div className="dh-verdict-body">{fv.summary || fv.headline}</div>
        </div>
      )}
      <div className="dh-cards">
        {order.map((k) => {
          const h = hz[k];
          if (!h) return null;
          const dec = String(h.decision || "");
          const tone =
            /BUY NOW/i.test(dec) ? "buy" :
            /PREPARE/i.test(dec) ? "prep" :
            /HOLD|WAIT/i.test(dec) ? "hold" : "sell";
          return (
            <div key={k} className={`dh-card dh-${tone}`}>
              <div className="dh-card-label">{h.label || k}</div>
              <div className="dh-card-decision">{h.decision}</div>
              <div className="dh-card-score">Score {h.score}</div>
              <div className="dh-card-period">{h.holding_period}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

