// frontend/src/components/ui/MoodGauge.tsx  (design v5 - market mood at a glance; styles in design-v5.css)
// A fear -> greed bar with a needle. Presentation only: takes the 0-100 market score the page already has.
// The text label always spells the mood out (never colour-only).
export function MoodGauge({ score, mood }: { score: number; mood?: string }) {
  const s = Math.max(0, Math.min(100, Math.round(Number.isFinite(score) ? score : 50)));
  const word = s >= 66 ? "Greedy" : s >= 55 ? "Optimistic" : s >= 45 ? "Neutral" : s >= 34 ? "Cautious" : "Fearful";
  return (
    <div className="v5-gauge" role="img" aria-label={`Market mood ${word}, ${s} out of 100${mood ? ` (${mood})` : ""}`}>
      <div className="v5-gauge-head">
        <span className="v5-gauge-word">{word}</span>
        <span className="v5-gauge-score num">{s}<small>/100</small></span>
      </div>
      <div className="v5-gauge-rail">
        <span className="v5-gauge-needle" style={{ left: `${s}%` }} />
      </div>
      <div className="v5-gauge-scale" aria-hidden="true"><span>Fear</span><span>Neutral</span><span>Greed</span></div>
    </div>
  );
}
