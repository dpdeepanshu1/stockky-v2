// frontend/src/components/ui/ModeLadder.tsx  (design v7 - OFF -> SHADOW -> LIVE, which rung a lane stands on)
// Presentation only: it shows a mode the page already read. It is NOT a control (no click, no request): moving a lane up a rung stays on the Switches tab.
// LIVE gets a slow pulsing ring (a loop, switched off like every v7 loop); every rung has a symbol and a word, never colour only.
import { ladderView } from "../../v7View";

const SYMBOL = { past: "✓", current: "●", future: "○" } as const;

export function ModeLadder({ mode, lane }: { mode: unknown; lane: string }) {
  const v = ladderView(mode, lane);
  // mode classes are prefixed (v7m-) on purpose: a bare "shadow" class is a Tailwind box-shadow utility
  return (
    <div className={`v7-ladder v7m-${v.mode}`}>
      <ol className="v7-ladder-steps" aria-label={`${lane} mode`}>
        {v.steps.map((s) => (
          <li key={s.id} className={`step v7m-${s.id} ${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
            <span className="dot" aria-hidden="true">{SYMBOL[s.state]}</span>
            <b>{s.label}</b>
            <small>{s.note}</small>
          </li>
        ))}
      </ol>
      <p className="v7-ladder-words">{v.words}</p>
    </div>
  );
}
