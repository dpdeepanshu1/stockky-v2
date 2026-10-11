// group324: Card now renders the board's dark terminal island (.ob, see board.css) so the journal cards under the board match it.
// group311: ONE card style for everything on the Market Board, matching the dashboards' own cards (StatCard / SectionHdr in
// RealAutoTrade: graphite surface, slate border, rounded-2xl, small uppercase label). Pure presentation; no behaviour.
import type { ReactNode } from "react";

export const CARD_CLASS = "bg-graphite border border-slate rounded-2xl";
export const CARD_INNER_CLASS = "bg-ink border border-slate rounded-lg";       // a box nested inside a card (stat tiles in the popup)
export const LABEL_CLASS = "text-[10px] font-display tabular-nums uppercase tracking-widest text-mist";

export function Card({ children, title, right, className = "", pad = "p-3" }:
  { children: ReactNode; title?: ReactNode; right?: ReactNode; className?: string; pad?: string }) {
  return (
    <div className={`ob ob-card ${pad === "p-3" ? "" : pad} ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between gap-2 mb-2">
          {title ? <p className="ob-cap">{title}</p> : <span />}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, color }: { label: string; value: ReactNode; sub?: ReactNode; color?: string }) {
  return (
    <div className={`${CARD_INNER_CLASS} px-2 py-1.5`}>
      <p className="text-[9px] font-display tabular-nums uppercase tracking-widest text-mist">{label}</p>
      <p className={`font-bold font-display tabular-nums text-[12px] ${color ?? "text-paper"}`}>{value}</p>
      {sub != null && <p className="text-[9px] text-mist tabular-nums">{sub}</p>}
    </div>
  );
}
