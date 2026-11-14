// frontend/src/components/ui/KpiStrip.tsx  (design v6, moved onto the component kit in E540 - a row of summary tiles)
// Presentation only: label, value and sub-line are passed in already formatted from state the page holds. The value inside never animates (money rule).
// Direction is spelled in the sub-line / value sign, the tone colour is only an accent. Since E540 each tile is a kit `Stat` (same props, same callers):
// a tile with a direction tone (up / down / warn) shows its sub-line as the signed delta line; other tiles keep it as a quiet sub-line.
import type { ReactNode } from "react";
import { Stat, Stats } from "../kit";

export type KpiTone = "up" | "down" | "warn" | "info" | "flat";
export interface Kpi { label: string; value: ReactNode; sub?: string; tone?: KpiTone }
const ARROW: Record<KpiTone, string> = { up: "▲", down: "▼", warn: "⚠", info: "", flat: "" };

export function KpiStrip({ items, label = "Summary" }: { items: Kpi[]; label?: string }) {
  if (!items.length) return null;
  return (
    <div className="mk-kpis" role="group" aria-label={label}>
      <Stats>
        {items.map((k) => {
          const tone = k.tone ?? "flat";
          const directional = tone === "up" || tone === "down" || tone === "warn";
          if (directional && k.sub) return <Stat key={k.label} label={k.label} value={k.value} delta={k.sub} tone={tone} size="sm" />;
          // a direction tone with no sub-line: the arrow goes in front of the value, so the direction is never colour-only
          if (directional) return <Stat key={k.label} label={k.label} value={<><i aria-hidden="true">{ARROW[tone]} </i>{k.value}</>} size="sm" />;
          return <Stat key={k.label} label={k.label} value={k.value} sub={k.sub} size="sm" />;
        })}
      </Stats>
    </div>
  );
}
