// Real Money Calibration > the left list of sections (E539, redesign round 6). Groups come from calibrationNav.ts; on a phone it becomes one scrolling row.
import { CAL_GROUPS, type CalSection } from "../../calibrationNav";

export default function CalNav({ value, onChange }: { value: CalSection; onChange: (s: CalSection) => void }) {
  return (
    <nav className="cx-nav" aria-label="Calibration sections">
      {CAL_GROUPS.map((g) => (
        <div key={g.id} className="cx-nav-group" role="group" aria-label={g.label}>
          <p className="cx-nav-h">{g.label}</p>
          {g.items.map((i) => (
            <button key={i.id} type="button" className="cx-nav-i" aria-current={value === i.id ? "page" : undefined} onClick={() => onChange(i.id)}>{i.label}</button>
          ))}
        </div>
      ))}
    </nav>
  );
}
