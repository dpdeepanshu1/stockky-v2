// E544: one progress bar for the system pages. Presentation only: the page passes the percentage it already computed.
// It states time / count done in words next to it (the page's own text line), and the bar is a real `progressbar` for assistive tech. The width is static.
export function ProgressBar({ pct, label }: { pct: number; label: string }) {
  const v = Number.isFinite(pct) ? Math.max(0, Math.min(100, Math.round(pct))) : 0;
  return (
    <div className="mk-progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={v}>
      <i style={{ width: `${v}%` }} />
    </div>
  );
}
