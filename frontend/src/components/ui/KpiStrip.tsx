// frontend/src/components/ui/KpiStrip.tsx  (design v6 - a row of summary tiles)
// Presentation only: label, value and sub-line are passed in already formatted from state the page holds. Tiles rise in one after the other;
// the value inside never animates (money rule). Direction is spelled in the sub-line / value sign, the tone colour is only an accent.
import type { CSSProperties, ReactNode } from "react";

export type KpiTone = "up" | "down" | "warn" | "info" | "flat";
export interface Kpi { label: string; value: ReactNode; sub?: string; tone?: KpiTone }
const ARROW: Record<KpiTone, string> = { up: "▲", down: "▼", warn: "⚠", info: "", flat: "" };

export function KpiStrip({ items, label = "Summary" }: { items: Kpi[]; label?: string }) {
  if (!items.length) return null;
  return (
    <ul className="v6-kpis" aria-label={label}>
      {items.map((k, i) => {
        const tone = k.tone ?? "flat";
        return (
          <li key={k.label} className={`v6-kpi ${tone}`} style={{ ["--i" as string]: i } as CSSProperties}>
            <span className="v6-kpi-label">{k.label}</span>
            <b className="v6-kpi-value">{ARROW[tone] && <i className="v6-kpi-arrow" aria-hidden="true">{ARROW[tone]}</i>}{k.value}</b>
            {k.sub && <span className="v6-kpi-sub">{k.sub}</span>}
          </li>
        );
      })}
    </ul>
  );
}
