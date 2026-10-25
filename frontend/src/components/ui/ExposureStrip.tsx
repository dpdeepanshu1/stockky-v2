// frontend/src/components/ui/ExposureStrip.tsx  (design v7 - how the open money is spread, long vs short)
// Presentation only, from the open positions the page already holds. Bar width = money put in (entry x quantity), not a forecast. Each block says up / down with
// an arrow, and a short is hatched, so nothing relies on colour alone. Blocks rise in once; nothing loops.
import type { CSSProperties } from "react";
import { exposureView, type ExposureRow } from "../../v7View";

const inr = (n: number): string => `₹${Math.round(n).toLocaleString("en-IN")}`;
const signed = (n: number): string => `${n > 0 ? "+" : n < 0 ? "-" : ""}₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;

export function ExposureStrip({ rows, title = "Where the open money sits" }: { rows: ExposureRow[]; title?: string }) {
  const v = exposureView(rows);
  if (!v.count) return null;
  const pnlTone = v.pnlTotal == null || Math.abs(v.pnlTotal) < 0.5 ? "flat" : v.pnlTotal > 0 ? "up" : "down";
  return (
    <section className="v7-expo" aria-label={title}>
      <header className="v7-expo-head">
        <b>{title}</b>
        <ul className="v7-expo-chips">
          <li className="v7-chip">{v.count} open · {inr(v.totalValue)} in</li>
          {v.shortCount > 0 && <li className="v7-chip">↗ {v.longCount} long · ↘ {v.shortCount} short</li>}
          {v.pnlTotal != null && <li className={`v7-chip pnl ${pnlTone}`}>{pnlTone === "up" ? "▲ " : pnlTone === "down" ? "▼ " : ""}now {signed(v.pnlTotal)}</li>}
          {v.nearStop.length > 0 && <li className="v7-chip warn">⚠ {v.nearStop.length} near stop: {v.nearStop.slice(0, 3).join(", ")}{v.nearStop.length > 3 ? "…" : ""}</li>}
        </ul>
      </header>
      <ul className="v7-expo-bar" aria-label="Open positions by money put in">
        {v.segments.map((s, i) => (
          <li key={`${s.symbol}-${i}`} className={`seg ${s.tone}${s.side === "SHORT" ? " short" : ""}`} style={{ flexBasis: `${Math.max(s.widthPct, 3)}%`, ["--i" as string]: i } as CSSProperties}
            title={`${s.symbol} · ${s.side === "SHORT" ? "short" : "long"} · ${inr(s.value)} in · ${s.tone === "up" ? "up" : s.tone === "down" ? "down" : "flat"} now`}>
            <span className="seg-sym">{s.widthPct >= 11 ? s.symbol : ""}</span>
            <span className="seg-arrow" aria-hidden="true">{s.arrow}</span>
          </li>
        ))}
      </ul>
      <p className="v7-expo-note">Block width = money put in (entry × quantity). ▲ up, ▼ down right now. Hatched blocks are shorts.</p>
    </section>
  );
}
