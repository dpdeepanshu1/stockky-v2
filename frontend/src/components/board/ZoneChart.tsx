// group307/324: SVG candle chart with shaded order-block zones (extend right until broken), dashed S/R levels with price tags, VWAP,
// opening range, held-position entry/stop/target lines, IST time axis and a pulsing last-price dot. Dark terminal look (colours from the
// .ob tokens; the chart is always drawn inside the popup's dark card). Candles fade in left to right unless reduced motion.
import type { SymbolView } from "./boardTypes";
import { istClock } from "./boardState";

export default function ZoneChart({ v, width = 640, height = 300, reduced }: { v: SymbolView; width?: number; height?: number; reduced: boolean }) {
  const c = v.candles;
  if (!c.length) return <div style={{ fontSize: 11, color: "var(--ob-dim)", padding: 12 }}>No bars yet.</div>;
  const padL = 6, padR = 58, padT = 8, padB = 18;
  const W = width - padL - padR, H = height - padT - padB;
  const lv = [...v.levels.supports, ...v.levels.resistances].map(l => l.price);
  const pos = v.position ? [v.position.entry, v.position.stop, v.position.target] : [];
  const all = [...c.map(k => k[2]), ...c.map(k => k[3]), ...v.zones.flatMap(z => [z.top, z.bottom]), ...pos];
  // levels only widen the axis when they are near the visible range (a far prev-day level must not flatten the candles)
  const lo0 = Math.min(...all), hi0 = Math.max(...all), rng0 = hi0 - lo0 || 1;
  const near = lv.filter(p => p > lo0 - rng0 * 0.5 && p < hi0 + rng0 * 0.5);
  const lo = Math.min(lo0, ...near), hi = Math.max(hi0, ...near), rng = hi - lo || 1;
  const y = (p: number) => padT + (1 - (p - lo) / rng) * H;
  const n = c.length, step = W / Math.max(n, 20), bw = Math.max(1.5, step * 0.62);
  const x = (i: number) => padL + (i + 0.5) * step;
  const xEnd = padL + W;
  const tIdx = (t: number) => Math.max(0, c.findIndex(k => k[0] >= t));
  const up = "var(--ob-g)", dn = "var(--ob-r)";
  const vw = v.vwap_series.map((p, i) => (p[1] == null ? null : [x(i), y(p[1] as number)] as [number, number])).filter(Boolean) as [number, number][];
  const grid = [0.25, 0.5, 0.75].map(f => padT + f * H);
  const nTicks = Math.min(5, Math.max(2, Math.floor(n / 12) + 1));
  const ticks = Array.from({ length: nTicks }, (_, k) => Math.min(n - 1, Math.round((k / Math.max(1, nTicks - 1)) * (n - 1))));
  const tag = (price: number, col: string, key: string) => (
    <g key={key}>
      <rect x={xEnd + 2} y={y(price) - 6} width={padR - 4} height={12} rx={3} fill={col} fillOpacity={0.16} stroke={col} strokeOpacity={0.5} strokeWidth={0.6} />
      <text x={xEnd + 2 + (padR - 4) / 2} y={y(price) + 3} fontSize={8} fontWeight={700} textAnchor="middle" fill={col}>{price.toFixed(2)}</text>
    </g>
  );
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label={`${v.symbol} ${v.tf_min}-minute candles with zones and levels`} style={{ display: "block" }}>
      {grid.map((gy, i) => <line key={i} x1={padL} x2={xEnd} y1={gy} y2={gy} stroke="var(--ob-line)" strokeWidth={1} />)}
      {v.zones.map((z, i) => (
        <g key={i}>
          <rect x={x(tIdx(z.t))} y={y(z.top)} width={Math.max(0, xEnd - x(tIdx(z.t)))} height={Math.max(1, y(z.bottom) - y(z.top))}
                fill={z.side === "bull" ? up : dn} fillOpacity={z.valid ? 0.2 : 0.08} stroke={z.side === "bull" ? up : dn} strokeOpacity={0.45} strokeDasharray={z.status === "tested" ? "3 2" : undefined} />
          <text x={xEnd - 3} y={y(z.top) + 9} fontSize={8} fontWeight={700} textAnchor="end" fill={z.side === "bull" ? up : dn}>{z.side === "bull" ? "OB↑" : "OB↓"}{z.fvg ? " FVG" : ""}{z.bos ? " BOS" : ""}</text>
        </g>
      ))}
      {[...v.levels.resistances, ...v.levels.supports].map(l => (
        <g key={l.label}>
          <line x1={padL} x2={xEnd} y1={y(l.price)} y2={y(l.price)} stroke="var(--ob-amber)" strokeOpacity={0.55} strokeDasharray="4 3" strokeWidth={0.8} />
          <text x={padL + 3} y={y(l.price) - 2} fontSize={8} fontWeight={700} fill="var(--ob-amber)">{l.label}</text>
          {tag(l.price, "var(--ob-amber)", `t-${l.label}`)}
        </g>
      ))}
      {v.or && [v.or[0], v.or[1]].map((p, i) => (
        <line key={i} x1={padL} x2={xEnd} y1={y(p)} y2={y(p)} stroke="var(--ob-cyan)" strokeOpacity={0.55} strokeWidth={0.8} strokeDasharray="1 3" />
      ))}
      {vw.length > 1 && <polyline points={vw.map(p => p.join(",")).join(" ")} fill="none" stroke="#8d86ff" strokeWidth={1.1} strokeDasharray="3 2" />}
      {c.map((k, i) => {
        const bull = k[4] >= k[1], col = bull ? up : dn;
        return (
          <g key={k[0]} className={reduced ? undefined : "ob-candle"} style={reduced ? undefined : { animationDelay: `${Math.min(700, i * 6)}ms` }}>
            <line x1={x(i)} x2={x(i)} y1={y(k[2])} y2={y(k[3])} stroke={col} strokeWidth={1} />
            <rect x={x(i) - bw / 2} y={Math.min(y(k[1]), y(k[4]))} width={bw} height={Math.max(1, Math.abs(y(k[1]) - y(k[4])))} fill={col} />
          </g>
        );
      })}
      {v.position && ([["ENTRY", v.position.entry, "var(--ob-fg)"], ["STOP", v.position.stop, dn], ["TARGET", v.position.target, up]] as [string, number, string][]).map(([t, p, col]) => (
        <g key={t}>
          <line x1={padL} x2={xEnd} y1={y(p)} y2={y(p)} stroke={col} strokeWidth={1.2} />
          <text x={padL + 2} y={y(p) - 2} fontSize={8} fontWeight={700} fill={col}>{t} {p.toFixed(2)}</text>
        </g>
      ))}
      {ticks.map(i => <text key={i} x={x(i)} y={height - 4} fontSize={8} textAnchor="middle" fill="var(--ob-dim)">{istClock(c[i][0])}</text>)}
      <circle cx={x(n - 1)} cy={y(v.ltp)} r={3} fill="var(--ob-fg)" />
      {!reduced && <circle cx={x(n - 1)} cy={y(v.ltp)} r={3} fill="none" stroke="var(--ob-fg)"><animate attributeName="r" values="3;9;3" dur="1.8s" repeatCount="indefinite" /><animate attributeName="opacity" values="0.8;0;0.8" dur="1.8s" repeatCount="indefinite" /></circle>}
      <g>
        <rect x={xEnd + 2} y={y(v.ltp) - 7} width={padR - 4} height={14} rx={3} fill="var(--ob-fg)" />
        <text x={xEnd + 2 + (padR - 4) / 2} y={y(v.ltp) + 3.5} fontSize={9} fontWeight={800} textAnchor="middle" fill="#07090d">{v.ltp.toFixed(2)}</text>
      </g>
    </svg>
  );
}
