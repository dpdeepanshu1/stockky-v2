// E537 (redesign round 5): the arming flow as a 3-step stepper with a clear "armed" end state. Presentation only: the ARM / DISARM / PAUSE buttons stay where they were.
import { armSteps } from "./tradingGroups";

export function ArmStepper({ gate1, gate2, gate3, armed, dhanRequired = true }: { gate1: boolean; gate2: boolean; gate3: boolean; armed: boolean; dhanRequired?: boolean }) {
  const steps = armSteps({ gate1, gate2, gate3, armed, dhanRequired });
  return (
    <ol className={`tr-stepper${armed ? " armed" : ""}`} aria-label="Arming steps">
      {steps.map((s) => (
        <li key={s.n} className={`tr-step ${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
          <span className="tr-step-n" aria-hidden="true">{s.state === "done" ? "✓" : s.n}</span>
          <span className="tr-step-text"><b>{s.label}</b><small>{s.hint}</small></span>
        </li>
      ))}
    </ol>
  );
}
