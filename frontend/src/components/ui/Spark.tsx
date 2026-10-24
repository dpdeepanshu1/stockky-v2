// frontend/src/components/ui/Spark.tsx  (design v6 - small equity / trend line)
// Static drawing: points -> one polyline + a soft area. Only the entrance is animated (the line is revealed from the left, opacity/transform only);
// the numbers it stands for are never animated. Always paired with a text alternative that says where the line starts and ends.
export function Spark({ points, width = 260, height = 64, label }: { points: number[]; width?: number; height?: number; label: string }) {
  if (points.length < 2) return null;
  const lo = Math.min(0, ...points), hi = Math.max(0, ...points);
  const span = hi - lo || 1, pad = 4;
  const x = (i: number) => pad + (i * (width - 2 * pad)) / (points.length - 1);
  const y = (v: number) => height - pad - ((v - lo) / span) * (height - 2 * pad);
  const line = points.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)} ${height - pad} L${x(0).toFixed(1)} ${height - pad} Z`;
  const up = points[points.length - 1] >= 0;
  return (
    <svg className={`v6-spark ${up ? "up" : "down"}`} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} preserveAspectRatio="none">
      <line className="v6-spark-zero" x1={pad} x2={width - pad} y1={y(0)} y2={y(0)} />
      <g className="v6-spark-draw">
        <path className="v6-spark-area" d={area} />
        <path className="v6-spark-line" d={line} vectorEffect="non-scaling-stroke" />
      </g>
    </svg>
  );
}
