// E25 / E27: candlestick chart (hand-drawn SVG, no new dependency) for 1m, 5m, 15m, 30m, 1h and daily candles, with volume bars, chart-pattern lines,
// candle-pattern markers, optional price lines (entry / stop / target) and a touch / hover readout. Intraday charts add a separator at every new
// session, a hollow bar for the candle that is still forming, and the clock time on the axis. Colours come from the theme variables (dark / light).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  type Bar, type ChartCtx, type PriceLevel, axisLabel, barChangePct, barLabel, clipLine, dayBreaks, defaultWindow, niceTicks, priceRange,
  sessionChangePct, toneColor, toneOf, toneText, windowStart, windowsFor,
} from "../chartContext";

interface Props {
  ctx: ChartCtx;
  windowKey?: string;        // a key of windowsFor(ctx.tf): 1m | 3m | 6m | 1y for daily, 1h | 1d | 5d ... for intraday
  height?: number;
  levels?: PriceLevel[];     // entry / stop / target lines
  showPatterns?: boolean;
}

const PAD = { l: 6, r: 46, t: 8, b: 18 };
const fmt = (v: number) => v.toLocaleString("en-IN", { maximumFractionDigits: v >= 1000 ? 0 : 2 });

export default function CandleChart({ ctx, windowKey, height = 240, levels = [], showPatterns = true }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(320);
  const [hover, setHover] = useState<number | null>(null);
  const tf = ctx.tf ?? "1d";
  const intraday = tf !== "1d";

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const apply = () => setWidth(Math.max(240, Math.floor(el.clientWidth)));
    apply();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const g = useMemo(() => {
    // closed bars first, then the forming one (drawn hollow, never part of a pattern)
    const all: Bar[] = ctx.live ? [...ctx.bars, ctx.live] : ctx.bars;
    const liveIdx = ctx.live ? all.length - 1 : -1;
    const wins = windowsFor(tf);
    const nWin = (wins.find((w) => w.key === windowKey) ?? wins.find((w) => w.key === defaultWindow(tf)) ?? wins[0]).bars;
    const lo = windowStart(all.length, nWin);
    const vis = all.slice(lo);
    const dates = all.map((b) => b[0]);
    const lines = showPatterns
      ? ctx.shapes.flatMap((s) => s.lines.map((l) => ({ s, l, c: clipLine(l, dates, lo) }))).filter((x) => x.c)
      : [];
    const extras = [...lines.flatMap((x) => [x.c!.y1, x.c!.y2]), ...levels.map((v) => v.price), ...ctx.levels.map((v) => v.price)];
    const range = priceRange(vis, extras);
    const breaks = dayBreaks(dates, lo, all.length - 1);
    return { all, lo, vis, dates, lines, range, liveIdx, breaks };
  }, [ctx, tf, windowKey, levels, showPatterns]);

  const { vis, lo, range } = g;
  if (!range || vis.length < 2) return null;

  const plotW = width - PAD.l - PAD.r;
  const volH = Math.round(height * 0.16);
  const priceH = height - PAD.t - PAD.b - volH - 4;
  const step = plotW / vis.length;
  const bw = Math.max(1, Math.min(14, step * 0.66));
  const thin = step < 2.2;                                   // 1-minute charts of 2 days: bodies are hairlines, so draw one stroke per candle
  const xC = (i: number) => PAD.l + (i - lo + 0.5) * step;
  const y = (p: number) => PAD.t + (1 - (p - range.min) / (range.max - range.min)) * priceH;
  const maxVol = Math.max(1, ...vis.map((b) => b[5]));
  const volBase = height - PAD.b;
  const ticks = niceTicks(range.min, range.max, 4);
  const inScale = (p: number) => p >= range.min && p <= range.max;

  const nTicks = width < 360 ? 3 : 4;
  const dateTicks = Array.from({ length: nTicks }, (_, j) => Math.min(vis.length - 1, Math.round((j / (nTicks - 1)) * (vis.length - 1))));
  const shown = hover != null ? hover : vis.length - 1;
  const bar = vis[shown];
  const gi = lo + shown;
  const isLive = gi === g.liveIdx;
  const chg = intraday ? sessionChangePct(g.all, gi) : barChangePct(g.all, gi);
  const up = bar[4] >= bar[1];
  const dayMarkers = ctx.markers.filter((m) => m.date === bar[0]);

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.floor(((e.clientX - r.left) / r.width * width - PAD.l) / step);
    setHover(Math.max(0, Math.min(vis.length - 1, i)));
  };

  const colOf = (b: Bar) => (b[4] >= b[1] ? "rgb(var(--buy-rgb))" : "rgb(var(--sell-rgb))");

  return (
    <div ref={boxRef} className="w-full">
      {/* readout: the newest bar until you touch the chart */}
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 mb-1 font-display tabular-nums text-[10px] text-mist min-h-[16px]">
        <span className="text-paper font-semibold">{barLabel(bar[0])}</span>
        {isLive && <span className="px-1 rounded border border-signal-hold/40 text-signal-hold">forming</span>}
        <span>O <span className="text-paper">{fmt(bar[1])}</span></span>
        <span>H <span className="text-paper">{fmt(bar[2])}</span></span>
        <span>L <span className="text-paper">{fmt(bar[3])}</span></span>
        <span>C <span className={up ? "text-signal-buy" : "text-signal-sell"}>{fmt(bar[4])}</span></span>
        {chg != null && <span className={chg >= 0 ? "text-signal-buy" : "text-signal-sell"} title={intraday ? "Close vs the previous session's close" : "Close vs the previous close"}>{chg >= 0 ? "+" : ""}{chg.toFixed(2)}%</span>}
        <span>Vol <span className="text-paper">{Intl.NumberFormat("en-IN", { notation: "compact" }).format(bar[5])}</span></span>
        {dayMarkers.map((m) => <span key={m.name} className={toneText(m.bias === "bullish" ? "good" : m.bias === "bearish" ? "bad" : "mute")}>{m.label}</span>)}
      </div>

      <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${intraday ? tf : "Daily"} candlestick chart`} style={{ touchAction: "pan-y", display: "block" }}
        onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} stroke="rgb(var(--border-rgb))" strokeWidth={0.5} strokeDasharray="3 3" />
            <text x={width - PAD.r + 4} y={y(t) + 3} fontSize={9} fill="rgb(var(--muted-rgb))">{fmt(t)}</text>
          </g>
        ))}

        {/* a new trading session starts here (intraday only) */}
        {g.breaks.map((i) => (
          <line key={`br${i}`} x1={xC(i) - step / 2} x2={xC(i) - step / 2} y1={PAD.t} y2={volBase} stroke="rgb(var(--muted-rgb))" strokeWidth={0.6} strokeDasharray="1 4" opacity={0.7} />
        ))}

        {/* volume */}
        {vis.map((b, k) => (
          <rect key={`v${k}`} x={xC(lo + k) - bw / 2} width={bw} y={volBase - (b[5] / maxVol) * volH} height={Math.max(0.5, (b[5] / maxVol) * volH)}
            fill={colOf(b)} opacity={lo + k === g.liveIdx ? 0.14 : 0.28} />
        ))}

        {/* old support / resistance flips */}
        {ctx.levels.filter((l) => inScale(l.price)).map((l) => (
          <line key={`${l.kind}${l.price}`} x1={PAD.l} x2={width - PAD.r} y1={y(l.price)} y2={y(l.price)} stroke={toneColor(l.kind === "support_flip" ? "good" : "bad")}
            strokeWidth={0.8} strokeDasharray="1 3" opacity={0.7} />
        ))}

        {/* candles */}
        {vis.map((b, k) => {
          const x = xC(lo + k), col = colOf(b), forming = lo + k === g.liveIdx;
          const top = y(Math.max(b[1], b[4])), bot = y(Math.min(b[1], b[4]));
          if (thin) return <line key={b[0]} x1={x} x2={x} y1={y(b[2])} y2={y(b[3])} stroke={col} strokeWidth={Math.max(0.8, step * 0.7)} opacity={forming ? 0.6 : 1} />;
          return (
            <g key={b[0]}>
              <line x1={x} x2={x} y1={y(b[2])} y2={y(b[3])} stroke={col} strokeWidth={1} />
              <rect x={x - bw / 2} y={top} width={bw} height={Math.max(1, bot - top)} fill={forming ? "none" : col} stroke={col} strokeWidth={forming ? 1 : 0} strokeDasharray={forming ? "2 1.5" : undefined} />
            </g>
          );
        })}

        {/* chart-pattern lines (neckline / trendlines / flag / target) */}
        {g.lines.map(({ s, l, c }, k) => (
          <line key={`${s.name}${l.role}${k}`} x1={xC(c!.i1)} y1={y(c!.y1)} x2={xC(c!.i2)} y2={y(c!.y2)} stroke={toneColor(toneOf(s.bias === "bullish" ? "good" : s.bias === "bearish" ? "bad" : "mute"))}
            strokeWidth={l.role === "target" ? 1 : 1.5} strokeDasharray={l.dashed ? "5 4" : undefined} opacity={0.9} />
        ))}

        {/* candle-pattern markers: arrow under a bullish candle, over a bearish one, dot for indecision */}
        {showPatterns && ctx.markers.map((m) => {
          const i = g.dates.indexOf(m.date);
          if (i < lo) return null;
          const b = g.all[i], x = xC(i), tone = m.bias === "bullish" ? "good" : m.bias === "bearish" ? "bad" : "mute";
          if (m.bias === "bullish") return <path key={m.name + m.date} d={`M${x} ${y(b[3]) + 4} l-4 7 h8 z`} fill={toneColor(tone)} />;
          if (m.bias === "bearish") return <path key={m.name + m.date} d={`M${x} ${y(b[2]) - 4} l-4 -7 h8 z`} fill={toneColor(tone)} />;
          return <circle key={m.name + m.date} cx={x} cy={y(b[2]) - 7} r={3} fill={toneColor(tone)} />;
        })}

        {/* last price line (intraday: the forming candle's close) */}
        {g.liveIdx >= 0 && inScale(g.all[g.liveIdx][4]) && (
          <line x1={xC(g.liveIdx)} x2={width - PAD.r} y1={y(g.all[g.liveIdx][4])} y2={y(g.all[g.liveIdx][4])} stroke={colOf(g.all[g.liveIdx])} strokeWidth={0.8} strokeDasharray="2 2" />
        )}

        {/* entry / stop / target */}
        {levels.filter((v) => inScale(v.price)).map((v) => (
          <g key={v.label}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(v.price)} y2={y(v.price)} stroke={toneColor(v.tone)} strokeWidth={1} strokeDasharray="6 3" />
            <rect x={width - PAD.r} y={y(v.price) - 7} width={PAD.r - 1} height={14} rx={3} fill={toneColor(v.tone)} />
            <text x={width - PAD.r + 3} y={y(v.price) + 3} fontSize={8.5} fontWeight={700} fill="rgb(var(--bg-rgb))">{v.label}</text>
          </g>
        ))}

        {dateTicks.map((k, j) => (
          <text key={`${k}-${j}`} x={Math.min(width - PAD.r - 2, Math.max(PAD.l + 2, xC(lo + k)))} y={height - 4} fontSize={9} fill="rgb(var(--muted-rgb))"
            textAnchor={j === 0 ? "start" : j === dateTicks.length - 1 ? "end" : "middle"}>{axisLabel(vis[k][0], j > 0 ? vis[dateTicks[j - 1]][0] : undefined)}</text>
        ))}

        {hover != null && <line x1={xC(lo + hover)} x2={xC(lo + hover)} y1={PAD.t} y2={volBase} stroke="rgb(var(--muted-rgb))" strokeWidth={0.7} strokeDasharray="3 3" />}
      </svg>
    </div>
  );
}
