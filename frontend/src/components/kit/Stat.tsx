// components/kit/Stat.tsx (E534) - label, big tabular number, signed delta. The number never animates (money rule).
// Direction is always spelled with a sign or arrow in the delta text, the colour is only an accent (never colour-only).
import type { ReactNode } from "react";

export type StatTone = "up" | "down" | "warn" | "flat";
const ARROW: Record<StatTone, string> = { up: "▲ ", down: "▼ ", warn: "⚠ ", flat: "" };

export function Stat({ label, value, delta, tone = "flat", sub, size = "md" }: {
  label: string; value: ReactNode; delta?: ReactNode; tone?: StatTone; sub?: ReactNode; size?: "sm" | "md" | "lg";
}) {
  return (
    <div className={`k-stat ${size}`}>
      <span className="k-stat-label">{label}</span>
      <b className="k-stat-value">{value}</b>
      {delta != null && <span className={`k-stat-delta ${tone}`}><span aria-hidden="true">{ARROW[tone]}</span>{delta}</span>}
      {sub != null && <span className="k-stat-sub">{sub}</span>}
    </div>
  );
}

export function Stats({ children }: { children: ReactNode }) {
  return <div className="k-stats">{children}</div>;
}
