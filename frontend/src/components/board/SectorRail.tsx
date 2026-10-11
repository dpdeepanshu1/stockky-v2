// group306: sector list with average change; click a sector to filter the board to it.
import type { BoardSector } from "./boardTypes";
import { CARD_CLASS } from "./boardCard";

export default function SectorRail({ sectors, selected, onSelect }:
  { sectors: BoardSector[]; selected: string | null; onSelect: (s: string | null) => void }) {
  return (
    <div className={`${CARD_CLASS} p-2 text-[11px] max-h-[640px] overflow-y-auto`} role="listbox" aria-label="Sectors">
      <button onClick={() => onSelect(null)} className={`w-full text-left px-2 py-1 rounded ${selected == null ? "bg-slate text-paper" : "text-mist hover:text-paper"}`}>All sectors</button>
      {sectors.map(s => (
        <button key={s.name} role="option" aria-selected={selected === s.name} onClick={() => onSelect(selected === s.name ? null : s.name)}
          className={`w-full flex items-center justify-between px-2 py-1 rounded tabular-nums ${selected === s.name ? "bg-slate text-paper" : "text-mist hover:text-paper"}`}>
          <span className="truncate">{s.name}</span>
          <span style={{ color: s.avg_change_pct >= 0 ? "var(--buy)" : "var(--sell)" }}>{s.avg_change_pct >= 0 ? "+" : ""}{s.avg_change_pct.toFixed(2)}%</span>
        </button>
      ))}
    </div>
  );
}
