// frontend/src/components/ui/CapitalRing.tsx  (design v7 - the one Dhan balance and how the two services share it)
// Presentation only, from the figures CapitalSplitCard already receives. Static drawing (dash lengths are computed once); the ring pops in on first paint.
// It does not claim Real Trade enforces its half: the legend repeats the card's own warning that the Real Trade share is a computed reference.
import { capitalView } from "../../v7View";

const R = 46, C = 2 * Math.PI * R, R2 = 33, C2 = 2 * Math.PI * R2;

export function CapitalRing({ total, psAllocated, psAvailable, splitPct }: { total: number | null; psAllocated: number | null; psAvailable: number | null; splitPct: number | null }) {
  const v = capitalView(total, psAllocated, psAvailable, splitPct);
  if (!v.ok) return null;
  const ps = (v.psPct / 100) * C, rt = (v.rtPct / 100) * C, free = (v.psFreePct / 100) * C2;
  return (
    <div className="v7-ring" role="img" aria-label={v.words}>
      <svg viewBox="0 0 120 120" className="v7-ring-svg" aria-hidden="true" focusable="false">
        {/* the outer <g> is the one CSS animates; the rotation lives on the inner <g> because a CSS transform would replace a transform attribute on the same element */}
        <g className="v7-ring-pop">
          <g transform="rotate(-90 60 60)">
            <circle cx="60" cy="60" r={R} className="v7-ring-base" />
            <circle cx="60" cy="60" r={R} className="v7-ring-ps" strokeDasharray={`${ps} ${C}`} />
            <circle cx="60" cy="60" r={R} className="v7-ring-rt" strokeDasharray={`${rt} ${C}`} strokeDashoffset={-ps} />
            <circle cx="60" cy="60" r={R2} className="v7-ring-base thin" />
            <circle cx="60" cy="60" r={R2} className="v7-ring-free" strokeDasharray={`${free} ${C2}`} />
          </g>
        </g>
        <text x="60" y="58" textAnchor="middle" className="v7-ring-big">{Math.round(v.psPct)}%</text>
        <text x="60" y="73" textAnchor="middle" className="v7-ring-small">Position Stocks</text>
      </svg>
      <ul className="v7-ring-legend">
        <li><i className="k ps" aria-hidden="true" /> Position Stocks pool <b>{Math.round(v.psPct)}%</b></li>
        <li><i className="k free" aria-hidden="true" /> free inside that pool <b>{Math.round(v.psFreePct)}%</b> of the balance</li>
        <li><i className="k rt" aria-hidden="true" /> Real Trade share (computed reference) <b>{Math.round(v.rtPct)}%</b></li>
      </ul>
    </div>
  );
}
