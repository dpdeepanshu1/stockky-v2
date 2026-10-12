// group337: "Best picks" - the best-3-to-5 shortlist the last entry cycle ranked, and what became of each pick. Read-only: it places no order and
// the entry gates still decide. One panel for both services: position-stocks (scan score + board + quality + zone) and real-trade (composite score
// with the board / news / sector nudges). Polls only while its tab is visible.
import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { Card } from "./boardCard";
import StockPopup from "./StockPopup";
import { usePrefersReducedMotion } from "./useBoardData";
import type { BoardRowLite } from "./boardTypes";

type Kind = "scalp" | "real";

interface Row {
  pick: number;
  symbol: string;
  score: number | null;
  detail: string;
  status: string | null;
  outcome: string;
}

interface Summary {
  enabled: boolean;
  asOf: number | null;
  line: string;
  rows: Row[];
}

const fmt = (v: unknown, d = 0) => (typeof v === "number" && isFinite(v) ? v.toFixed(d) : "-");

function normalize(kind: Kind, body: any): Summary {
  const picks: any[] = Array.isArray(body?.picks) ? body.picks : [];
  if (kind === "scalp") {
    const w = body?.weights || {};
    return {
      enabled: !!body?.enabled,
      asOf: typeof body?.as_of === "number" ? body.as_of * 1000 : null,
      line: `${body?.passed_quality_gate ?? 0} of ${body?.evaluated ?? 0} scanned names passed the quality gate · ranked on scan ${fmt(w.scan * 100)}% / board ${fmt(w.board * 100)}% / quality ${fmt(w.quality * 100)}% / zone ${fmt(w.zone * 100)}%`,
      rows: picks.map(p => ({
        pick: p.pick, symbol: p.symbol, score: typeof p.score === "number" ? p.score : null,
        detail: [
          p.board_conviction != null ? `board conviction ${fmt(p.board_conviction)}` : "no board row",
          p.board_rank != null ? `rank #${p.board_rank}` : "",
          p.board_side === "bear" ? "board bearish" : "",
          p.extended ? "extended past zone" : "",
        ].filter(Boolean).join(" · "),
        status: null, outcome: p.outcome || "NOT_TRIED",
      })),
    };
  }
  return {
    enabled: !!body?.enabled,
    asOf: typeof body?.as_of === "number" ? body.as_of * 1000 : null,
    line: `${body?.approved_count ?? 0} risk-approved · shortlist up to ${body?.max ?? "-"} · up to ${body?.max_new_per_cycle ?? "-"} entered per cycle · composite floor ${fmt(body?.min_composite)}`,
    rows: picks.map(p => ({
      pick: p.pick, symbol: p.symbol, score: typeof p.composite_score === "number" ? p.composite_score : null,
      detail: [
        p.conviction != null ? `conviction ${fmt(p.conviction)}` : "",
        `R:R ${fmt(p.rr, 2)}`,
        p.board_bonus ? `board ${p.board_bonus > 0 ? "+" : ""}${fmt(p.board_bonus, 1)}` : "",
        p.news_bonus ? `news ${p.news_bonus > 0 ? "+" : ""}${fmt(p.news_bonus, 1)}` : "",
        p.is_upper_circuit ? "upper circuit" : "",
      ].filter(Boolean).join(" · "),
      status: p.status || null, outcome: p.outcome || "",
    })),
  };
}

function outcomeStyle(o: string): { label: string; cls: string } {
  if (o === "ENTERED") return { label: "Entered", cls: "text-signal-buy" };
  if (o.startsWith("SKIPPED:") || o.startsWith("PLACEMENT_FAILED")) {
    return { label: o.startsWith("SKIPPED:") ? `Skipped · ${o.slice(8)}` : "Order failed", cls: "text-signal-hold" };
  }
  if (o === "NOT_TRIED" || o === "NOT_NEEDED" || o === "NOT_PLACED" || !o) {
    return { label: o === "NOT_NEEDED" ? "Not needed" : o === "NOT_PLACED" ? "Not placed" : "Not tried", cls: "text-mist" };
  }
  return { label: o, cls: "text-mist" };
}

export default function ShortlistPanel({ active, kind, fetcher, title, note, resetKey }:
  { active: boolean; kind: Kind; fetcher: () => Promise<any>; title: string; note?: string; resetKey?: string }) {
  const [data, setData] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<BoardRowLite | null>(null);   // a row opens the stock popup: candle / line chart, levels and live momentum
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try {
        const body = await fetcher();
        if (alive) { setData(normalize(kind, body)); setErr(null); }
      } catch (e: any) {
        if (alive) setErr(e?.message || "shortlist unavailable");
      }
    };
    load();
    const stop = setVisibleInterval(load, 10000);
    return () => { alive = false; stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, kind, resetKey]);

  const age = data?.asOf ? Math.max(0, Math.round((Date.now() - data.asOf) / 1000)) : null;
  return (
    <>
    <Card title={title} right={age != null ? <span className="text-[9px] text-mist tabular-nums">last cycle {age < 90 ? `${age}s` : `${Math.round(age / 60)}m`} ago</span> : undefined}>
      <div className="space-y-1.5 text-[11px]" data-testid={`shortlist-${kind}`}>
        {err && <div className="text-signal-hold">{err}</div>}
        {data && !data.enabled && <div className="text-mist">Shortlist is switched off - entries use the old order.</div>}
        {data && data.enabled && <div className="text-mist text-[10px]">{data.line}</div>}
        {data && data.enabled && !data.rows.length && !err && (
          <div className="text-mist">No shortlist yet. It appears after an entry cycle ranks at least one candidate that cleared the gates.</div>
        )}
        {data?.rows.map((r: Row) => {
          const o = outcomeStyle(r.outcome);
          return (
            <div key={`${r.pick}-${r.symbol}`} className="sl-row flex items-center justify-between gap-2 rounded-lg border border-slate bg-ink px-2 py-1.5"
              role="button" tabIndex={0} aria-label={`${r.symbol}: open chart and live momentum`} data-testid={`shortlist-row-${r.symbol}`}
              onClick={() => setSel({ symbol: r.symbol })}
              onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSel({ symbol: r.symbol }); } }}>
              <div className="min-w-0">
                <span className="font-display font-bold text-paper tabular-nums">#{r.pick} {r.symbol}</span>
                {r.status && <span className="ml-1.5 text-[9px] uppercase tracking-widest text-mist">{r.status === "SELECTED" ? "first in line" : r.status === "RESERVE" ? "reserve" : r.status.toLowerCase()}</span>}
                {r.detail && <div className="text-[10px] text-mist truncate">{r.detail}</div>}
                <div className="text-[9px] text-signal-prepare">Tap for chart &amp; live momentum</div>
              </div>
              <div className="text-right shrink-0 tabular-nums">
                <div className="text-paper font-bold">{r.score == null ? "-" : r.score.toFixed(1)}</div>
                <div className={`text-[10px] ${o.cls}`}>{o.label}</div>
              </div>
            </div>
          );
        })}
        {note && <div className="text-[9px] text-mist pt-1">{note}</div>}
      </div>
    </Card>
    <StockPopup row={sel} onClose={() => setSel(null)} reduced={reduced} />
    </>
  );
}
