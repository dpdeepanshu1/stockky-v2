// frontend/src/components/ui/PageHero.tsx  (design v6 - the header of Real Trade, Position Stocks and Real Money Calibration)
// Presentation only: it renders what the page passes in and changes nothing. The artwork is decorative (aria-hidden):
// a drifting price line, breathing candles and a small neural net. All motion is transform / opacity and is switched off by
// reduced-motion, TV mode, a hidden tab, or the "Animations" button here (remembered in localStorage when it is available).
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { NavIcon, type IconName } from "./NavIcon";

const CALM_KEY = "stockky:v6_calm";
const readCalm = (): boolean => { try { return window.localStorage.getItem(CALM_KEY) === "1"; } catch { return false; } };

/** One shared "animations on / off" preference for every v6 page: html.v6-calm turns the decorative loops off. */
function useCalm(): [boolean, () => void] {
  const [calm, setCalm] = useState<boolean>(() => (typeof window === "undefined" ? false : readCalm()));
  useEffect(() => {
    try { document.documentElement.classList.toggle("v6-calm", calm); } catch { /* no document */ }
  }, [calm]);
  const toggle = () => setCalm((c) => {
    const next = !c;
    try { window.localStorage.setItem(CALM_KEY, next ? "1" : "0"); } catch { /* storage blocked: the choice lasts for this visit only */ }
    return next;
  });
  return [calm, toggle];
}

// Candle bodies: x, wick top, body top, body bottom, wick bottom, direction. Pure decoration, not data.
const CANDLES: [number, number, number, number, number, "u" | "d"][] = [
  [470, 62, 74, 104, 118, "u"], [506, 52, 60, 86, 100, "u"], [542, 58, 66, 92, 112, "d"], [578, 40, 50, 80, 96, "u"],
  [614, 36, 44, 66, 84, "u"], [650, 46, 54, 82, 100, "d"], [686, 30, 38, 62, 78, "u"], [722, 24, 32, 58, 72, "u"],
];
// Neural net: nodes (x, y) and the links between them (indexes). Decoration.
const NODES: [number, number][] = [[300, 40], [340, 84], [300, 126], [380, 34], [392, 108], [436, 64]];
const LINKS: [number, number][] = [[0, 1], [1, 2], [0, 3], [1, 3], [1, 4], [2, 4], [3, 5], [4, 5]];
// A seamless wave: period 180 so the CSS slide of exactly one period (-180) loops with no jump. Decoration, not data.
const WAVE = Array.from({ length: 125 }, (_, i) => {
  const x = i * 10;
  const y = 86 + 22 * Math.sin((x * 2 * Math.PI) / 180) + 9 * Math.sin((x * 2 * Math.PI) / 90 + 1);
  return `${i ? "L" : "M"}${x} ${y.toFixed(1)}`;
}).join(" ");

export function PageHero({ icon, title, kicker, subtitle, right }: { icon: IconName; title: ReactNode; kicker: string; subtitle?: string; right?: ReactNode }) {
  const [calm, toggleCalm] = useCalm();
  return (
    <header className="v6-hero">
      <svg className="v6-art" viewBox="0 0 880 150" preserveAspectRatio="xMaxYMid slice" aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id="v6g-wave" gradientUnits="userSpaceOnUse" x1="0" x2="880" y1="0" y2="0"><stop offset="0" stopColor="currentColor" stopOpacity="0" /><stop offset="0.6" stopColor="currentColor" stopOpacity="0.55" /><stop offset="1" stopColor="currentColor" stopOpacity="0.9" /></linearGradient>
        </defs>
        <g className="v6-art-grid">{[30, 60, 90, 120].map((y) => <line key={y} x1="0" x2="880" y1={y} y2={y} />)}</g>
        <path className="v6-art-wave" d={WAVE} stroke="url(#v6g-wave)" />
        <g className="v6-art-candles">
          {CANDLES.map(([x, wt, bt, bb, wb, d], i) => (
            <g key={x} className={`v6-candle ${d}`} style={{ ["--i" as string]: i } as CSSProperties}>
              <line x1={x} x2={x} y1={wt} y2={wb} /><rect x={x - 7} y={bt} width="14" height={bb - bt} rx="2" />
            </g>
          ))}
        </g>
        <g className="v6-art-net">
          {LINKS.map(([a, b]) => <line key={`${a}-${b}`} x1={NODES[a][0]} y1={NODES[a][1]} x2={NODES[b][0]} y2={NODES[b][1]} />)}
          {NODES.map(([x, y], i) => <circle key={i} className="v6-node" cx={x} cy={y} r="4.5" style={{ ["--i" as string]: i } as CSSProperties} />)}
        </g>
      </svg>
      <div className="v6-hero-row">
        <div className="v6-hero-title">
          <span className="v6-hero-icon" aria-hidden="true"><NavIcon name={icon} /></span>
          <div className="min-w-0">
            <span className="v6-kicker"><i aria-hidden="true" />{kicker}</span>
            <h2 className="v6-h">{title}</h2>
            {subtitle && <p className="v6-sub">{subtitle}</p>}
          </div>
        </div>
        <div className="v6-hero-right">
          {right}
          <button type="button" role="switch" aria-checked={!calm} onClick={toggleCalm} className="v6-anim-toggle" title="Turn the decorative animations on or off">
            <span className="v6-anim-track" aria-hidden="true"><i /></span>
            <span>Animations: {calm ? "Off" : "On"}</span>
          </button>
        </div>
      </div>
    </header>
  );
}
