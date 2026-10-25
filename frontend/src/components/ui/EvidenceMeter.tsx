// frontend/src/components/ui/EvidenceMeter.tsx  (design v7 - how far a shadow test has got)
// Presentation only. Two meters (closed trades, entry days); the verdict needs BOTH, so the slower one is the honest overall figure. The fills are revealed once
// on first paint (transform only); the numbers beside them are plain text.
import { evidenceView } from "../../v7View";

export function EvidenceMeter({ done, needed, days, daysNeeded }: { done: number; needed: number; days: number; daysNeeded: number }) {
  const v = evidenceView(done, needed, days, daysNeeded);
  const row = (label: string, now: number, goal: number, pct: number) => (
    <div className="v7-meter-row">
      <span className="v7-meter-label">{label}</span>
      <span className="v7-meter" role="meter" aria-valuemin={0} aria-valuemax={goal} aria-valuenow={Math.min(now, goal)} aria-label={`${label}: ${now} of ${goal}`}>
        <i className="v7-meter-fill" style={{ width: `${pct}%` }} />
        {[25, 50, 75].map((t) => <b key={t} className="v7-meter-tick" style={{ left: `${t}%` }} />)}
      </span>
      <span className="v7-meter-num">{now} / {goal}</span>
    </div>
  );
  return (
    <div className={`v7-evidence${v.ready ? " ready" : ""}`}>
      {row("Closed trades", done, needed, v.tradesPct)}
      {row("Entry days", days, daysNeeded, v.daysPct)}
      <p className="v7-evidence-words">{v.ready ? "✓ " : "… "}{v.words}</p>
    </div>
  );
}
