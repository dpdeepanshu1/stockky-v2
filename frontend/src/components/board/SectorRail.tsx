// group306/324: sector list with a diverging bar for the average change; click a sector to filter the board to it. Rows share one height
// (`rowH`) with the flow curves' origins, so each curve visibly leaves from its own row.
import type { BoardSector } from "./boardTypes";

export default function SectorRail({ sectors, selected, onSelect, rowH = 26, height, horizontal = false }:
  { sectors: BoardSector[]; selected: string | null; onSelect: (s: string | null) => void; rowH?: number; height?: number; horizontal?: boolean }) {
  const maxAbs = Math.max(0.5, ...sectors.map(s => Math.abs(s.avg_change_pct)));
  const style = horizontal ? undefined : { height: rowH, fontSize: rowH < 20 ? 10 : 11 };
  return (
    <div className={`ob-rail${horizontal ? " h" : ""}`} role="listbox" aria-label="Sectors" style={height && !horizontal ? { height } : undefined}>
      <button onClick={() => onSelect(null)} aria-selected={selected == null} role="option" style={horizontal ? undefined : { ...style, gridTemplateColumns: "1fr" }}>
        <span className="nm" style={{ fontWeight: 700 }}>All sectors</span>
      </button>
      {sectors.map(s => {
        const up = s.avg_change_pct >= 0, wpc = Math.min(50, (Math.abs(s.avg_change_pct) / maxAbs) * 50);
        return (
          <button key={s.name} role="option" aria-selected={selected === s.name} onClick={() => onSelect(selected === s.name ? null : s.name)} style={style}>
            <span className="nm">{s.name}</span>
            {!horizontal && <span className="bar" aria-hidden="true"><i style={{ left: up ? "50%" : `${50 - wpc}%`, width: `${wpc}%`, background: up ? "var(--ob-g)" : "var(--ob-r)" }} /></span>}
            <span className={up ? "ob-up" : "ob-dn"}>{up ? "+" : ""}{s.avg_change_pct.toFixed(2)}%</span>
          </button>
        );
      })}
    </div>
  );
}
