// E26: plain-English chips for the chart patterns, candle patterns, signals and market mood. Tap a chip to read what it means.
import { useState } from "react";
import { type ChartBadge, type Tf, groupBadges, tfInfo, toneChip } from "../chartContext";

export default function PatternBadges({ badges, compact = false, tf = "1d" }: { badges: ChartBadge[]; compact?: boolean; tf?: Tf }) {
  const [open, setOpen] = useState<string | null>(null);
  const groups = groupBadges(badges);
  if (!groups.length) return <p className="font-display text-[10px] text-mist">No pattern or signal stands out on the {tfInfo(tf).word.toLowerCase()} chart right now.</p>;
  const all = groups.flatMap((g) => g.items);
  const sel = all.find((b) => b.key === open);
  return (
    <div className="space-y-1.5">
      {groups.map((g) => (
        <div key={g.kind} className="flex flex-wrap items-center gap-1.5">
          {!compact && <span className="font-display text-[9px] text-mist w-20 shrink-0">{g.title}</span>}
          {g.items.map((b) => (
            <button key={b.key} type="button" title={b.tip} aria-pressed={open === b.key} onClick={() => setOpen(open === b.key ? null : b.key)}
              className={`font-display text-[10px] px-2 py-0.5 rounded-full border ${toneChip(b.tone)} ${open === b.key ? "ring-1 ring-current" : ""}`}>
              {b.label}{b.status ? <span className="opacity-70"> · {b.status}</span> : null}
            </button>
          ))}
        </div>
      ))}
      {sel?.tip && <p className="font-display text-[10px] leading-snug text-mist bg-ink border border-slate rounded-lg px-2.5 py-1.5">{sel.tip}</p>}
    </div>
  );
}
