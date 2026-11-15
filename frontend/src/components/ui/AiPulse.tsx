// frontend/src/components/ui/AiPulse.tsx  (design v6 - "what the engine is doing right now" strip)
// Presentation only: it restates what the page already knows (running / idle, a one-line status, a few chips). While `working`, a soft light
// sweeps along the strip and the orb thinks; when idle everything is still. Chips always carry a symbol, never colour only.
import { AiOrb } from "./AiOrb";
import { Chip, ChipList } from "../kit/Chip";

export interface PulseChip { label: string; state: "ok" | "warn" | "bad" | "idle" }
const SYMBOL = { ok: "✓", warn: "⚠", bad: "✕", idle: "•" } as const;

export function AiPulse({ title, line, working = false, chips = [] }: { title: string; line: string; working?: boolean; chips?: PulseChip[] }) {
  return (
    <div className={`w-pulse ${working ? "working" : "idle"}`} role="status" aria-live="polite">
      <AiOrb state={working ? "thinking" : "idle"} size={40} />
      <div className="w-pulse-text">
        <b className="w-pulse-title">{title}<span className="w-pulse-state">{working ? "working" : "idle"}</span></b>
        <span className="w-pulse-line">{line}</span>
      </div>
      {chips.length > 0 && (
        <ChipList>
          {chips.map((c) => <Chip key={c.label} tone={c.state === "idle" ? "neutral" : c.state} symbol={SYMBOL[c.state]}>{c.label}</Chip>)}
        </ChipList>
      )}
    </div>
  );
}
