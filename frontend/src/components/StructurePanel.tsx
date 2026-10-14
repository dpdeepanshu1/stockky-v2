// group351: daily market-structure snapshot for one decision. Default COLLAPSED with a one-line summary (mobile-safe); expands to
// Trend / Momentum / Structure / Location / Risk lines with ✓ / ✗, the invalidation level and the stop options. Display only.
import { useState } from "react";
import { badEntryText, emptyReason, gateStatus, groups, invalidation, oneLine, stopLine, type StructureSnapshotDoc, type Tone } from "../structureView";

const TONE: Record<Tone, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" };

export default function StructurePanel({ snapshot, atrStop, defaultOpen = false }:
  { snapshot: StructureSnapshotDoc | null | undefined; atrStop?: number | null; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!snapshot) return null;                      // gate off / not reached: show nothing rather than a blank card
  const st = gateStatus(snapshot);
  const empty = emptyReason(snapshot);
  const line = oneLine(snapshot);
  const sl = stopLine(snapshot, atrStop);
  const inval = invalidation(snapshot);
  const bad = badEntryText(snapshot);
  return (
    <div className="rounded-lg border border-slate bg-ink p-2 font-display tabular-nums text-[10px]" data-testid="structure-panel">
      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open} className="flex w-full items-start justify-between gap-2 text-left">
        <span className="min-w-0">
          <span className="uppercase tracking-widest text-mist mr-2">Daily structure</span>
          <span className={`font-bold ${TONE[st.tone]}`}>{st.label}</span>
          {!open && line && <span className="block text-mist truncate">{line}</span>}
          {!open && empty && <span className="block text-mist">{empty}</span>}
        </span>
        <span className="text-mist shrink-0">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="mt-2 space-y-2 border-t border-slate pt-2">
          {bad && <p className="text-signal-hold">{bad}</p>}
          {empty && <p className="text-mist">{empty}</p>}
          {groups(snapshot).map(g => (
            <div key={g.title}>
              <p className="uppercase tracking-widest text-[9px] text-mist">{g.title}</p>
              {g.lines.map((l, i) => (
                <p key={i} className={l.ok === true ? "text-signal-buy" : l.ok === false ? "text-signal-sell" : "text-mist"}>
                  <span className="inline-block w-3">{l.ok === true ? "✓" : l.ok === false ? "✗" : "·"}</span>{l.text}
                </p>
              ))}
            </div>
          ))}
          {inval && <p className="text-mist">{inval}</p>}
          {sl && <p className="text-mist">{sl}</p>}
          <p className="text-[9px] text-mist">
            Mode {snapshot.gate_mode ?? "?"}{snapshot.enforced ? " (applied)" : snapshot.would_reject ? " (recorded only)" : ""}
            {snapshot.summary?.ms_ver ? ` · engine ${snapshot.summary.ms_ver}` : ""}{snapshot.source ? ` · from ${snapshot.source}` : ""}
          </p>
        </div>
      )}
    </div>
  );
}
