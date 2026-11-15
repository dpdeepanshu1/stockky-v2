// Self Improve System > the left list of sections (round 542, E567). Same look as the Real Money Calibration list (cx-nav classes); groups come from selfImproveView.ts.
import { SI_GROUPS, type SiSection } from "../../selfImproveView";

export default function SiNav({ value, onChange }: { value: SiSection; onChange: (s: SiSection) => void }) {
  return (
    <nav className="cx-nav" aria-label="Self Improve sections">
      {SI_GROUPS.map((g) => (
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
