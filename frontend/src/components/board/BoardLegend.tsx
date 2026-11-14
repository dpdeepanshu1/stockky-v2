// E538 (redesign round 5b): the legend line under the Market Board title. Presentation only; the words come from boardLegend.ts.
import { legendItems } from "./boardLegend";

export default function BoardLegend({ marked = false }: { marked?: boolean }) {
  return (
    <ul className="tr-legend" aria-label="How to read the board" data-testid="board-legend">
      {legendItems({ marked }).map((i) => (
        <li key={i.key}>
          {i.swatches.map((s, n) => <span key={n} className={`tr-lg-sw ${s}`} aria-hidden="true">{s === "text" ? i.mark : ""}</span>)}
          <span>{i.label}</span>
        </li>
      ))}
    </ul>
  );
}
