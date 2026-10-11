// group306: the Market Board container - strip, sector rail, bullish block over bearish block (treemaps), flow lines, popup.
// Display only: it places no order. `marks` lets a host tab highlight symbols (watchlist, candidates); open positions are ringed.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { squarify } from "./treemapLayout";
import { useBoardData, usePrefersReducedMotion } from "./useBoardData";
import MarketStrip from "./MarketStrip";
import SectorRail from "./SectorRail";
import FlowLines, { type FlowItem } from "./FlowLines";
import BoardTile from "./BoardTile";
import StockPopup from "./StockPopup";
import type { BoardRow } from "./boardTypes";

const UNIVERSES: { id: string; label: string }[] = [{ id: "all", label: "All" }, { id: "fno", label: "F&O only" }, { id: "liquid", label: "Liquid top-N" }];
const weight = (r: BoardRow) => Math.max(0.1, r.strength) * Math.log10(Math.max(10, r.turnover || 0) + 10);

// group311 fix: the measured <div> only exists once data has loaded, so a mount-time effect never saw it and the width stayed 0 (empty
// board). A callback ref attaches the ResizeObserver whenever the element appears.
function useWidth(): [(el: HTMLDivElement | null) => void, number] {
  const [w, setW] = useState(0);
  const roRef = useRef<ResizeObserver | null>(null);
  const setRef = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect(); roRef.current = null;
    if (!el) return;
    setW(Math.floor(el.clientWidth));
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(es => setW(Math.floor(es[0].contentRect.width)));
    ro.observe(el); roRef.current = ro;
  }, []);
  useEffect(() => () => roRef.current?.disconnect(), []);
  return [setRef, w];
}

export default function MarketBoard({ active = true, marks, popupExtra, title = "Market Board", hatchFor, showZoneScore, legend }:
  { active?: boolean; marks?: Set<string>; popupExtra?: (row: BoardRow) => React.ReactNode; title?: string;
    hatchFor?: (row: BoardRow) => boolean; showZoneScore?: boolean; legend?: React.ReactNode }) {
  const [universe, setUniverse] = useState("all");
  const [sector, setSector] = useState<string | null>(null);
  const [sel, setSel] = useState<BoardRow | null>(null);
  const { data, error, loading, reload } = useBoardData(universe, active);
  const reduced = usePrefersReducedMotion();
  const [wrapRef, width] = useWidth();
  const narrow = width < 520;
  const blockH = narrow ? 260 : 300;

  const filt = (rows: BoardRow[]) => (sector ? rows.filter(r => r.sector === sector) : rows);
  const bull = useMemo(() => filt(data?.bullish ?? []), [data, sector]);
  const bear = useMemo(() => filt(data?.bearish ?? []), [data, sector]);
  const rb = useMemo(() => squarify(bull.map(r => ({ id: r.symbol, value: weight(r) })), 0, 0, width, blockH), [bull, width, blockH]);
  const rr = useMemo(() => squarify(bear.map(r => ({ id: r.symbol, value: weight(r) })), 0, 0, width, blockH), [bear, width, blockH]);
  const flow: FlowItem[] = useMemo(() => {
    const m = new Map([...bull, ...bear].map(r => [r.symbol, r]));
    return [...rb.map(t => ({ row: m.get(t.id)!, rect: t })), ...rr.map(t => ({ row: m.get(t.id)!, rect: { ...t, y: t.y + blockH + 8 } }))].filter(i => i.row);
  }, [rb, rr, bull, bear, blockH]);

  const block = (rows: BoardRow[], rects: typeof rb, label: string, color: string) => {
    const m = new Map(rows.map(r => [r.symbol, r]));
    return (
      <div>
        <div className="flex items-center gap-2 mb-1 text-[11px] font-display font-bold" style={{ color }}>{label}<span className="text-mist font-normal">{rows.length}</span></div>
        {rows.length === 0 ? <div className="text-[11px] text-mist rounded-lg border border-slate p-4">Nothing ranked on this side{data?.coverage.warming ? ` (${data.coverage.warming} symbols still warming up)` : ""}.</div> : (
          <svg width={width} height={blockH} role="group" aria-label={label}>
            <defs><pattern id="board-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="var(--muted-2)" strokeWidth="2" strokeOpacity="0.5" /></pattern></defs>
            {rects.map(t => { const r = m.get(t.id); return r ? <BoardTile key={r.symbol} row={r} x={t.x} y={t.y} w={t.w} h={t.h} reduced={reduced} marked={marks?.has(r.symbol)} hatched={hatchFor ? hatchFor(r) : false} showScore={showZoneScore} onOpen={setSel} /> : null; })}
          </svg>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-display font-bold text-paper text-[14px] mr-auto">{title}</h3>
        {UNIVERSES.map(u => (
          <button key={u.id} onClick={() => setUniverse(u.id)} className={`px-2.5 py-1 rounded-full text-[11px] border ${universe === u.id ? "border-signal-prepare/40 bg-signal-prepare/20 text-signal-prepare" : "border-slate text-mist hover:text-paper"}`}>{u.label}</button>
        ))}
        <button onClick={() => void reload()} className="px-2.5 py-1 rounded-full text-[11px] border border-slate text-mist hover:text-paper">{loading ? "…" : "Refresh"}</button>
      </div>
      {legend && <div className="text-[10px] text-mist">{legend}</div>}
      {error && <div className="rounded-lg border border-signal-hold/40 bg-signal-hold/10 text-signal-hold text-[11px] p-2">{error}{data ? " Showing the last good data." : ""}</div>}
      {!data && !error && <div className="text-[12px] text-mist p-6 text-center">Loading the board…</div>}
      {data && (
        <>
          <MarketStrip data={data} />
          <div className={narrow ? "space-y-3" : "grid gap-3"} style={narrow ? undefined : { gridTemplateColumns: "170px 1fr" }}>
            <SectorRail sectors={data.sectors} selected={sector} onSelect={setSector} />
            <div ref={wrapRef} className="relative space-y-2 min-w-0">
              {width > 0 && !narrow && <FlowLines items={flow} width={width} height={blockH * 2 + 48} reduced={reduced} />}
              {width > 0 && block(bull, rb, "BULLISH", "var(--buy)")}
              {width > 0 && block(bear, rr, "BEARISH", "var(--sell)")}
            </div>
          </div>
        </>
      )}
      <StockPopup row={sel} onClose={() => setSel(null)} reduced={reduced} extra={sel && popupExtra ? popupExtra(sel) : undefined} />
    </div>
  );
}
