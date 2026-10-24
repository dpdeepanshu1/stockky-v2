// frontend/src/components/ui/ScoreRing.tsx  (design v5 - AI score ring; styles in design-v5.css)
// Presentation only. The ring draws itself once on mount; the number and a word (Strong / Good / Mixed / Weak) are always shown, never colour-only.
export function ScoreRing({ score, size = 88 }: { score: number; size?: number }) {
  const s = Math.max(0, Math.min(100, Math.round(Number.isFinite(score) ? score : 0)));
  const word = s >= 70 ? "Strong" : s >= 55 ? "Good" : s >= 40 ? "Mixed" : "Weak";
  const tone = s >= 70 ? "buy" : s >= 55 ? "acc" : s >= 40 ? "warn" : "sell";
  const r = 40, c = 2 * Math.PI * r;
  return (
    <div className={`v5-ring ${tone}`} style={{ width: size, height: size }} role="img" aria-label={`AI score ${s} out of 100, ${word}`}>
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle className="v5-ring-track" cx="50" cy="50" r={r} />
        <circle className="v5-ring-arc" cx="50" cy="50" r={r} strokeDasharray={c} style={{ ["--ring-full" as string]: c, ["--ring-to" as string]: c * (1 - s / 100) }} />
      </svg>
      <div className="v5-ring-label"><b className="num">{s}</b><small>{word}</small></div>
    </div>
  );
}
