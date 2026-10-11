// group306: one stock tile (SVG <g>). Position by transform with a CSS transition so a tile slides when its rank changes; the
// volume-at-price profile is drawn as thin horizontal bars whose lengths ease to new values. Ring = open position.
import type { BoardRow } from "./boardTypes";

export interface TileProps {
  row: BoardRow; x: number; y: number; w: number; h: number; reduced: boolean; hatched?: boolean; marked?: boolean; showScore?: boolean;
  onOpen: (r: BoardRow) => void;
}

export default function BoardTile({ row, x, y, w, h, reduced, hatched, marked, showScore, onOpen }: TileProps) {
  const bull = row.side === "bull";
  const col = bull ? "var(--buy)" : "var(--sell)";
  const pad = 1;
  const iw = Math.max(0, w - pad * 2), ih = Math.max(0, h - pad * 2);
  const showText = iw > 38 && ih > 26;     // group311: was 46 x 28 - on a phone most of the 130 tiles were left unlabelled
  const bars = row.profile || [];
  const bh = bars.length ? Math.min(4, (ih - 16) / bars.length) : 0;
  const ch = row.change_pct;
  const dlt = row.rank_delta;
  return (
    <g transform={`translate(${x + pad},${y + pad})`} style={{ transition: reduced ? "none" : "transform 260ms ease" }}
       tabIndex={0} role="button" aria-label={`${row.symbol} ${bull ? "bullish" : "bearish"} rank ${row.rank}${hatched ? ", over your risk cap" : ""}`}
       onClick={() => onOpen(row)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(row); } }}
       className="cursor-pointer outline-none focus-visible:opacity-80">
      <rect width={iw} height={ih} rx={4} fill={col} fillOpacity={0.10 + Math.min(0.28, row.percentile / 400)} stroke={col} strokeOpacity={0.45} />
      {hatched && <rect width={iw} height={ih} rx={4} fill="url(#board-hatch)" />}
      {row.in_position && <rect width={iw} height={ih} rx={4} fill="none" stroke="var(--amber)" strokeWidth={2} />}
      {marked && !row.in_position && <rect width={iw} height={ih} rx={4} fill="none" stroke="var(--cyan)" strokeWidth={1.5} strokeDasharray="3 2" />}
      {showText && bars.slice(0, Math.max(0, Math.floor((ih - 16) / Math.max(1, bh)))).map((v, i) => (
        <rect key={i} x={0} y={ih - 2 - (i + 1) * bh} width={Math.max(0, v * iw * 0.6)} height={Math.max(0.5, bh - 1)} fill={col} fillOpacity={0.35}
              style={{ transition: reduced ? "none" : "width 400ms ease" }} />
      ))}
      {showText && (
        <>
          <text x={5} y={12} fontSize={10} fontWeight={700} fill="var(--fg)">{row.symbol}{row.is_fno ? " ·F" : ""}</text>
          <text x={5} y={24} fontSize={9} fill={col} className="tabular-nums">{ch == null ? "" : `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}%`}</text>
          {dlt != null && dlt !== 0 && iw > 70 && (
            <text x={iw - 4} y={12} fontSize={9} textAnchor="end" fill={dlt > 0 ? "var(--buy)" : "var(--sell)"}>{dlt > 0 ? "▲" : "▼"}{Math.abs(dlt)}</text>
          )}
          {row.extended && iw > 70 && <text x={iw - 4} y={24} fontSize={8} textAnchor="end" fill="var(--amber)">EXT</text>}
          {showScore && row.zone_score != null && iw > 70 && ih > 40 && <text x={iw - 4} y={ih - 5} fontSize={9} fontWeight={700} textAnchor="end" fill={row.zone_score >= 65 ? "var(--buy)" : row.zone_score >= 40 ? "var(--amber)" : "var(--muted)"}>Z{Math.round(row.zone_score)}</text>}
        </>
      )}
    </g>
  );
}
