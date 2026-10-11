// group306/324: one stock tile (SVG <g>). The outer <g> is positioned by a CSS transform (so a tile slides when its rank changes); the
// inner <g> plays the entrance animation. The volume-at-price profile is drawn as thin bars (they breathe while the market is live);
// the top ranks glow; a price tick flashes the tile green / red; big tiles also carry a sparkline and the rank.
// Ring = open position, dashed ring = marked (watchlist / candidate).
import { useEffect, useRef, useState } from "react";
import type { BoardRow } from "./boardTypes";
import { heat } from "./boardState";

export interface TileProps {
  row: BoardRow; x: number; y: number; w: number; h: number; reduced: boolean; live?: boolean; hatched?: boolean; marked?: boolean; showScore?: boolean;
  onOpen: (r: BoardRow) => void;
}

export default function BoardTile({ row, x, y, w, h, reduced, hatched, marked, showScore, onOpen }: TileProps) {
  const bull = row.side === "bull";
  const col = bull ? "var(--ob-g)" : "var(--ob-r)";
  const grad = bull ? "url(#ob-g-bull)" : "url(#ob-g-bear)";
  const pad = 1;
  const iw = Math.max(0, w - pad * 2), ih = Math.max(0, h - pad * 2);
  const showText = iw > 38 && ih > 26;
  const big = iw > 120 && ih > 70;
  const bars = row.profile || [];
  const bh = bars.length ? Math.min(4, (ih - 16) / bars.length) : 0;
  const ch = row.change_pct;
  const dlt = row.rank_delta;
  const top = row.rank <= 6;
  const hot = heat(row.percentile);

  // a price tick flashes the tile (skipped under reduced motion; one short timer per changed tile)
  const prev = useRef(row.ltp);
  const [flash, setFlash] = useState<0 | 1 | -1>(0);
  useEffect(() => {
    if (reduced || !(row.ltp > 0) || row.ltp === prev.current) { prev.current = row.ltp; return; }
    setFlash(row.ltp > prev.current ? 1 : -1); prev.current = row.ltp;
    const t = setTimeout(() => setFlash(0), 700);
    return () => clearTimeout(t);
  }, [row.ltp, reduced]);

  const sp = row.spark || [];
  let spark = "";
  if (big && sp.length > 2) {
    const lo = Math.min(...sp), hi = Math.max(...sp), span = hi - lo || 1, sw = Math.min(70, iw * 0.4);
    spark = sp.map((v, i) => `${(iw - 5 - sw + (i / (sp.length - 1)) * sw).toFixed(1)},${(8 + (1 - (v - lo) / span) * 22).toFixed(1)}`).join(" ");
  }
  const maxBar = bars.length ? Math.max(...bars, 1e-9) : 1;
  const nBars = showText && bh > 0 ? Math.max(0, Math.floor((ih - 16) / Math.max(1, bh))) : 0;

  return (
    <g className="ob-tile ob-tile-pos" tabIndex={0} role="button"
       style={{ transform: `translate(${x + pad}px, ${y + pad}px)` }}
       aria-label={`${row.symbol} ${bull ? "bullish" : "bearish"} rank ${row.rank}${hatched ? ", over your risk cap" : ""}`}
       onClick={() => onOpen(row)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(row); } }}>
      <g className={reduced ? undefined : "ob-in"} style={reduced ? undefined : { animationDelay: `${Math.min(420, (row.rank || 0) * 14)}ms` }}>
        <rect width={iw} height={ih} rx={5} fill="#0a0e14" />
        <rect width={iw} height={ih} rx={5} fill={grad} fillOpacity={0.35 + hot * 0.65} />
        {nBars > 0 && (
          <g className="ob-depth">
            {bars.slice(0, nBars).map((v, i) => (
              <rect key={i} x={0} y={ih - 2 - (i + 1) * bh} width={Math.max(0, (v / maxBar) * iw * 0.62)} height={Math.max(0.5, bh - 1)} fill={col} fillOpacity={0.22 + (v / maxBar) * 0.4}
                    style={{ transition: reduced ? "none" : "width 400ms ease" }} />
            ))}
          </g>
        )}
        <rect className="ob-tile-edge" width={iw} height={ih} rx={5} fill="none" stroke={col} strokeOpacity={top ? 0.95 : 0.4} strokeWidth={top ? 1.3 : 1}
              filter={top ? "url(#ob-glow)" : undefined} />
        {hatched && <rect width={iw} height={ih} rx={5} fill="url(#board-hatch)" />}
        {row.in_position && <rect width={iw} height={ih} rx={5} fill="none" stroke="var(--ob-amber)" strokeWidth={2} />}
        {marked && !row.in_position && <rect width={iw} height={ih} rx={5} fill="none" stroke="var(--ob-cyan)" strokeWidth={1.5} strokeDasharray="3 2" />}
        {flash !== 0 && <rect className={flash > 0 ? "ob-flash-up" : "ob-flash-dn"} width={iw} height={ih} rx={5} fill={flash > 0 ? "var(--ob-g)" : "var(--ob-r)"} pointerEvents="none" />}
        {showText && (
          <svg width={iw} height={ih} style={{ overflow: "hidden" }} aria-hidden="true">{/* nested <svg> clips long symbols to the tile */}
            <text x={5} y={12} fontSize={10} fontWeight={700} fill="var(--ob-fg)">{row.symbol}{row.is_fno ? " ·F" : ""}</text>
            <text x={5} y={24} fontSize={9} fontWeight={600} fill={col} className="tabular-nums">{ch == null ? "" : `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}%`}</text>
            {big && <text x={5} y={36} fontSize={8} fill="var(--ob-dim)">#{row.rank}{row.conviction != null ? ` · conv ${row.conviction}` : ""}</text>}
            {spark && <polyline points={spark} fill="none" stroke={col} strokeWidth={1.2} strokeOpacity={0.85} strokeLinejoin="round" />}
            {!spark && dlt != null && dlt !== 0 && iw > 70 && (
              <text x={iw - 4} y={12} fontSize={9} textAnchor="end" fill={dlt > 0 ? "var(--ob-g)" : "var(--ob-r)"}>{dlt > 0 ? "▲" : "▼"}{Math.abs(dlt)}</text>
            )}
            {row.extended && iw > 70 && <text x={iw - 4} y={spark ? 38 : 24} fontSize={8} textAnchor="end" fill="var(--ob-amber)">EXT</text>}
            {showScore && row.zone_score != null && iw > 70 && ih > 40 && <text x={iw - 4} y={ih - 5} fontSize={9} fontWeight={700} textAnchor="end" fill={row.zone_score >= 65 ? "var(--ob-g)" : row.zone_score >= 40 ? "var(--ob-amber)" : "var(--ob-dim)"}>Z{Math.round(row.zone_score)}</text>}
          </svg>
        )}
      </g>
    </g>
  );
}
