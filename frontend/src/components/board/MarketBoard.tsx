// group306/324: the Market Board container - header, sector rail, flow lanes, bullish block over bearish block (treemaps), popup.
// ONE SVG holds both blocks, so the flow curves' tile coordinates are exact. Nothing ranked yet -> animated placeholder tiles
// (clearly labelled, never data). Display only: it places no order. `marks` lets a host tab highlight symbols (watchlist,
// candidates); open positions are ringed.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./board.css";
import { squarify } from "./treemapLayout";
import { useBoardData, usePrefersReducedMotion } from "./useBoardData";
import MarketStrip from "./MarketStrip";
import SectorRail from "./SectorRail";
import FlowLines, { type FlowItem } from "./FlowLines";
import BoardTile from "./BoardTile";
import StockPopup from "./StockPopup";
import { isNarrow, laneWidth, railRowHeight, sectorY } from "./flowGeometry";
import { ghostItems } from "./ghostLayout";
import { stripState } from "./boardState";
import type { BoardRow, BoardSector } from "./boardTypes";

const UNIVERSES: { id: string; label: string }[] = [{ id: "all", label: "All" }, { id: "fno", label: "F&O only" }, { id: "liquid", label: "Liquid top-N" }];
const weight = (r: BoardRow) => Math.max(0.1, r.strength) * Math.log10(Math.max(10, r.turnover || 0) + 10);
const LABEL_H = 20, BLOCK_GAP = 12;
const NO_SECTORS: BoardSector[] = [];

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
  const [boardRef, boardW] = useWidth();
  const narrow = isNarrow(boardW);
  const blockH = narrow ? 260 : 300;
  const lane = laneWidth(width, narrow);
  const tw = Math.max(0, width - lane);
  const yBull = LABEL_H, yBear = LABEL_H + blockH + BLOCK_GAP + LABEL_H;
  const H = yBear + blockH;

  const bull = useMemo(() => (sector ? (data?.bullish ?? []).filter(r => r.sector === sector) : data?.bullish ?? []), [data, sector]);
  const bear = useMemo(() => (sector ? (data?.bearish ?? []).filter(r => r.sector === sector) : data?.bearish ?? []), [data, sector]);
  const rb = useMemo(() => squarify(bull.map(r => ({ id: r.symbol, value: weight(r) })), lane, 0, tw, blockH), [bull, lane, tw, blockH]);
  const rr = useMemo(() => squarify(bear.map(r => ({ id: r.symbol, value: weight(r) })), lane, 0, tw, blockH), [bear, lane, tw, blockH]);
  const ghostB = useMemo(() => squarify(ghostItems(34, 11), lane, 0, tw, blockH), [lane, tw, blockH]);
  const ghostR = useMemo(() => squarify(ghostItems(28, 29), lane, 0, tw, blockH), [lane, tw, blockH]);

  const sectors = data?.sectors ?? NO_SECTORS;
  const rowH = railRowHeight(sectors.length, H);
  const flow: FlowItem[] = useMemo(() => {
    const idx = new Map(sectors.map((s, i) => [s.name, i]));
    const m = new Map([...bull, ...bear].map(r => [r.symbol, r]));
    const mk = (t: typeof rb, dy: number): FlowItem[] => t.flatMap(x => {
      const row = m.get(x.id);
      return row ? [{ row, rect: { ...x, y: x.y + dy }, srcY: sectorY(idx.get(row.sector) ?? -1, sectors.length, H) }] : [];
    });
    return [...mk(rb, yBull), ...mk(rr, yBear)];
  }, [rb, rr, bull, bear, sectors, yBull, yBear, H]);

  const st = data ? stripState(data) : null;
  const live = !!data && st?.kind === "live";
  const empty = !!data && bull.length === 0 && bear.length === 0;

  const block = (rows: BoardRow[], rects: typeof rb, y0: number, label: string, color: string) => {
    const m = new Map(rows.map(r => [r.symbol, r]));
    return (
      <g>
        <text x={lane + 2} y={y0 - 6} className="ob-lbl" fill={color}>{label}<tspan fill="var(--ob-dim)" fontWeight={400} dx={6}>{rows.length}</tspan></text>
        {rects.map(t => { const r = m.get(t.id); return r ? <BoardTile key={r.symbol} row={r} x={t.x} y={t.y + y0} w={t.w} h={t.h} reduced={reduced} live={live} marked={marks?.has(r.symbol)} hatched={hatchFor ? hatchFor(r) : false} showScore={showZoneScore} onOpen={setSel} /> : null; })}
      </g>
    );
  };
  const ghost = (rects: typeof rb, y0: number, label: string, color: string) => (
    <g>
      <text x={lane + 2} y={y0 - 6} className="ob-lbl" fill={color}>{label}<tspan fill="var(--ob-dim)" fontWeight={400} dx={6}>0</tspan></text>
      {rects.map((t, i) => <rect key={t.id} className="ob-ghost" x={t.x + 1} y={t.y + y0 + 1} width={Math.max(0, t.w - 2)} height={Math.max(0, t.h - 2)} rx={5} style={{ animationDelay: `${(i % 11) * 160}ms` }} />)}
    </g>
  );

  return (
    <>
    <div className="ob" data-testid="market-board" ref={boardRef}>
      <div className="ob-bar-top">
        <span className="ob-dots" aria-hidden="true"><i /><i /><i /></span>
        <span className="ob-title">{title}</span>
        {st && <span className={`ob-pill ${st.kind}`}><i />{st.label}</span>}
        {UNIVERSES.map(u => <button key={u.id} className="ob-chip" aria-pressed={universe === u.id} onClick={() => setUniverse(u.id)}>{u.label}</button>)}
        <button className="ob-chip" onClick={() => void reload()}>{loading ? "…" : "Refresh"}</button>
      </div>
      {legend && <div className="ob-meta">{legend}</div>}
      {error && <div className="ob-meta ob-warn">{error}{data ? " Showing the last good data." : ""}</div>}
      {!data && !error && <div style={{ padding: 28, textAlign: "center", fontSize: 12, color: "var(--ob-dim)" }}>Loading the board…</div>}
      {data && (
        <>
          <MarketStrip data={data} reduced={reduced} />
          {narrow && <SectorRail sectors={sectors} selected={sector} onSelect={setSector} horizontal />}
          <div className={`ob-body${narrow ? "" : " wide"}`}>
            {!narrow && <SectorRail sectors={sectors} selected={sector} onSelect={setSector} rowH={rowH} height={H} />}
            <div ref={wrapRef} className={`ob-stage${live && !reduced ? " ob-live" : ""}`} style={{ height: H }}>
              {width > 0 && <FlowLines items={flow} width={width} height={H} lane={lane} live={live} reduced={reduced} />}
              {width > 0 && (
                <svg width={width} height={H} role="group" aria-label="Market board">
                  <defs>
                    <pattern id="board-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="#5c6b7a" strokeWidth="2" strokeOpacity="0.55" /></pattern>
                    <linearGradient id="ob-g-bull" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#1fe08f" stopOpacity="0.30" /><stop offset="1" stopColor="#0a8f5a" stopOpacity="0.10" /></linearGradient>
                    <linearGradient id="ob-g-bear" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#ff3d5a" stopOpacity="0.30" /><stop offset="1" stopColor="#a31a33" stopOpacity="0.10" /></linearGradient>
                    <filter id="ob-glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.2" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
                  </defs>
                  {empty ? ghost(ghostB, yBull, "BULLISH", "var(--ob-g)") : block(bull, rb, yBull, "BULLISH", "var(--ob-g)")}
                  {empty ? ghost(ghostR, yBear, "BEARISH", "var(--ob-r)") : block(bear, rr, yBear, "BEARISH", "var(--ob-r)")}
                </svg>
              )}
              {empty && (
                <div className="ob-ghost-msg" role="status">
                  <div>
                    <b>{sector ? `Nothing ranked in ${sector}` : data.market_open ? "Ranking the market…" : "Market closed"}</b>
                    {sector
                      ? "Pick another sector or choose All sectors."
                      : data.market_open
                        ? `${data.coverage.warming} symbols still warming up. Tiles appear as each one has enough price action.`
                        : "Nothing is ranked right now. The board fills when the market opens (09:15 IST Mon-Fri); after a session it keeps that session's closing board for weekends and holidays."}
                    <br /><span style={{ fontSize: 10, opacity: 0.7 }}>Placeholder tiles - not market data.</span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
    {/* outside .ob on purpose: the sheet follows the app theme; only the card inside it is the dark island */}
    <StockPopup row={sel} onClose={() => setSel(null)} reduced={reduced} extra={sel && popupExtra ? popupExtra(sel) : undefined} />
    </>
  );
}
