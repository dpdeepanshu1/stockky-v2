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
  const label = `${v.side === "SHORT" ? "Short" : "Long"} position${v.locked ? " with the stop moved up" : ""}. Stop ${px(stop as number)}, entry ${px(entry)}, target ${px(target as number)}. ${v.words}`;
  return (
    <div className={`w-rail ${v.state} ${v.side === "SHORT" ? "short" : "long"}${v.locked ? " locked" : ""}${compact ? " compact" : ""}`} role="img" aria-label={label}
      style={{ ["--e" as string]: v.entryPct, ["--p" as string]: v.pricePct, ["--s" as string]: v.stopPct } as CSSProperties}>
      <div className="w-rail-track" aria-hidden="true">
        <span className="w-rail-zone loss" />
        <span className="w-rail-zone gain" />
        <span className="w-rail-tick" />
        {v.locked && <span className="w-rail-tick stop" />}
        {live && <span className="w-rail-mark"><i /></span>}
      </div>
      <div className="w-rail-legend" aria-hidden="true">
        {v.locked ? (<>
          <span className="e">Entry {px(entry)}</span>
          <span className="s">Stop {px(stop as number)}</span>
        </>) : (<>
          <span className="s">Stop {px(stop as number)}</span>
          <span className="e">Entry {px(entry)}</span>
        </>)}
        <span className="t">Target {px(target as number)}</span>
      </div>
      {!compact && <p className="w-rail-words" aria-hidden="true">{v.arrow ? `${v.arrow} ` : ""}{v.words}</p>}
    </div>
  );
}
