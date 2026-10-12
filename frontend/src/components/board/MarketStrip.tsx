// group306/324: board header - the two big counts with an advancing-vs-declining gauge, then feed health, coverage, leading and lagging
// sector. (The live / closed / last-session pill sits in the board's top bar, see MarketBoard.) Same facts as before, drawn as a terminal header.
import { useEffect, useRef, useState } from "react";
import type { BoardPayload } from "./boardTypes";
import { sectorSpark, tweenInt } from "./boardState";

const pct = (v: number | null | undefined) => (v == null ? "-" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);

/** Big number that eases to its new value (instant under reduced motion). */
function CountUp({ value, reduced }: { value: number; reduced: boolean }) {
  const [v, setV] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduced || v === value) { setV(value); from.current = value; return; }
    const a = from.current, t0 = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const x = tweenInt(a, value, now - t0);
      setV(x); from.current = x;
      if (x !== value) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, reduced]);
  return <>{v}</>;
}

function Spark({ pts, color }: { pts: number[]; color: string }) {
  if (pts.length < 2) return null;
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const d = pts.map((p, i) => `${((i / (pts.length - 1)) * 54).toFixed(1)},${(2 + (1 - (p - lo) / span) * 12).toFixed(1)}`).join(" ");
  return <svg width={56} height={16} viewBox="0 0 56 16" aria-hidden="true" style={{ verticalAlign: "middle" }}><polyline points={d} fill="none" stroke={color} strokeWidth={1.3} strokeLinejoin="round" /></svg>;
}

export default function MarketStrip({ data, reduced = false }: { data: BoardPayload; reduced?: boolean }) {
  const m = data.market, f = data.feed;
  const rows = [...(data.bullish ?? []), ...(data.bearish ?? [])];
  const leadSp = sectorSpark(rows, m.leading_sector), lagSp = sectorSpark(rows, m.lagging_sector);
  const tot = Math.max(1, m.up + m.down + m.flat);
  return (
    <>
      <div className="ob-head">
        <div className="ob-count g"><b><CountUp value={m.bull_count} reduced={reduced} /></b><span>bullish</span></div>
        <div style={{ minWidth: 0 }}>
          <div className="ob-gauge" aria-label="advancing vs declining">
            <div style={{ width: `${(m.up / tot) * 100}%`, background: "var(--ob-g)" }} />
            <div style={{ width: `${(m.flat / tot) * 100}%`, background: "#3a4757" }} />
            <div style={{ width: `${(m.down / tot) * 100}%`, background: "var(--ob-r)" }} />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 5, fontSize: 10, color: "var(--ob-dim)" }} className="tabular-nums">
            <span className="ob-up">{m.up} up</span><span>{m.flat} flat</span><span className="ob-dn">{m.down} down</span>
          </div>
        </div>
        <div className="ob-count r"><b><CountUp value={m.bear_count} reduced={reduced} /></b><span>bearish</span></div>
      </div>
      <div className="ob-meta">
        <span>feed {f.source ?? "?"}{f.tick_age_s != null ? ` · ${f.tick_age_s.toFixed(0)}s old` : ""}</span>
        <span>{m.tracked} tracked · {data.coverage.warming} warming</span>
        {!!data.coverage.backfill?.pending && <span className="ob-warn" title="After a restart the morning's candles are fetched so these symbols can be ranked">filling morning bars: {data.coverage.backfill.pending} queued</span>}
        {!data.coverage.backfill?.pending && !!data.coverage.no_day_open && <span title="No exchange open and the first bar started late; backfill retries">{data.coverage.no_day_open} without a day open</span>}
        <span>Leading <b className="ob-up">{m.leading_sector ?? "-"}</b> {pct(m.leading_change_pct)} <Spark pts={leadSp} color="var(--ob-g)" /></span>
        <span>Lagging <b className="ob-dn">{m.lagging_sector ?? "-"}</b> {pct(m.lagging_change_pct)} <Spark pts={lagSp} color="var(--ob-r)" /></span>
        <span style={{ marginLeft: "auto" }}>params {data.params_version} · research view, not advice</span>
      </div>
    </>
  );
}
