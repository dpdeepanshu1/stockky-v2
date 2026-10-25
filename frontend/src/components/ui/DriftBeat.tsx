// frontend/src/components/ui/DriftBeat.tsx  (design v7 - the heartbeat line of a reconciliation check)
// Decoration that restates a verdict the page already computed: steady = a calm pulse that scrolls, warn = a rougher pulse (still scrolls), alarm = flat line with a
// cross, idle = a dashed flat line. The state is always also written as a word beside it. Only the scrolling is a loop (transform), and it is switched off like every v7 loop.
import { beatKind } from "../../v7View";

const WORDS = { steady: "✓ In step", warn: "⚠ Drift found", alarm: "✕ Check did not finish", idle: "• No report yet" } as const;
// One period is 80 wide; the line is drawn 4 periods long and slid by exactly one period, so the loop has no jump.
const PULSE: [number, number][] = [[0, 20], [16, 20], [22, 20], [26, 8], [32, 32], [38, 14], [42, 20], [80, 20]];
const ROUGH: [number, number][] = [[0, 20], [10, 20], [14, 4], [20, 36], [25, 10], [31, 30], [36, 16], [42, 24], [48, 20], [80, 20]];

function line(pts: [number, number][]): string {
  const out: string[] = [];
  for (let i = 0; i < 4; i++) pts.forEach(([x, y], j) => out.push(`${i === 0 && j === 0 ? "M" : "L"}${x + i * 80} ${y}`));
  return out.join(" ");
}

export function DriftBeat({ kind, warnCount }: { kind: unknown; warnCount?: number }) {
  const b = beatKind(kind, warnCount);
  return (
    <div className={`v7-beat ${b}`} role="img" aria-label={WORDS[b]}>
      <svg viewBox="0 0 240 40" className="v7-beat-svg" aria-hidden="true" focusable="false" preserveAspectRatio="none">
        {b === "alarm" || b === "idle" ? <path d="M0 20 L240 20" className="v7-beat-flat" /> : <g className="v7-beat-run"><path d={line(b === "warn" ? ROUGH : PULSE)} className="v7-beat-line" /></g>}
      </svg>
      <span className="v7-beat-word">{WORDS[b]}</span>
    </div>
  );
}
