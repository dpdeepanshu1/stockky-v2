// frontend/src/components/ui/RiskRail.tsx  (design v7 - stop / entry / target drawn to scale, for a long or a short)
// Presentation only: it draws levels and a live price the page already holds. The marker glides with a transform transition; the prices themselves are plain
// text that never animates (money rule). Profit always lies to the RIGHT of the entry tick, for a short too (the maths is in v7View.railView, tested).
// The state is spelled in words with an arrow, never colour only.
import type { CSSProperties } from "react";
import { railView } from "../../v7View";

const px = (n: number): string => `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function RiskRail({ side, entry, stop, target, price, compact = false }: { side?: string | null; entry: number; stop: number | null | undefined; target: number | null | undefined; price?: number | null; compact?: boolean }) {
  const v = railView({ side, entry, stop, target, price });
  if (!v.ok) return null;
  const live = v.state !== "unknown";
  const label = `${v.side === "SHORT" ? "Short" : "Long"} position. Stop ${px(stop as number)}, entry ${px(entry)}, target ${px(target as number)}. ${v.words}`;
  return (
    <div className={`v7-rail ${v.state} ${v.side === "SHORT" ? "short" : "long"}${compact ? " compact" : ""}`} role="img" aria-label={label}
      style={{ ["--e" as string]: v.entryPct, ["--p" as string]: v.pricePct } as CSSProperties}>
      <div className="v7-rail-track" aria-hidden="true">
        <span className="v7-rail-zone loss" />
        <span className="v7-rail-zone gain" />
        <span className="v7-rail-tick" />
        {live && <span className="v7-rail-mark"><i /></span>}
      </div>
      <div className="v7-rail-legend" aria-hidden="true">
        <span className="s">Stop {px(stop as number)}</span>
        <span className="e">Entry {px(entry)}</span>
        <span className="t">Target {px(target as number)}</span>
      </div>
      {!compact && <p className="v7-rail-words" aria-hidden="true">{v.arrow ? `${v.arrow} ` : ""}{v.words}</p>}
    </div>
  );
}
