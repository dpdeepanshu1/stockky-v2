// Round 264, slice 2: F&O Overview > Pipeline check, drawn as one row of eight boxes (market data > chains > signal > gates > sizing > order > exits > reconcile).
// Each box: OK / PROBLEM / idle in words, how long ago it last did something, and a click opens the last decision that passed through it. Idle is neutral, never green.
import { useState } from "react";
import type { FnoStage } from "../../fnoApi";
import { istHm, stageAge, stageChip, stageHeadline } from "../../fnoView";
import { Chip, Msg } from "../calibration/ui";

const BORDER: Record<string, string> = { good: "border-signal-buy", bad: "border-signal-sell", neutral: "border-slate" };

export default function FnoStagesRow({ stages }: { stages: FnoStage[] | null | undefined }) {
  const [open, setOpen] = useState<string | null>(null);
  const head = stageHeadline(stages);
  const list = Array.isArray(stages) ? stages : [];
  const sel = list.find((s) => s.key === open) || null;
  return (
    <div className="mb-3 space-y-2">
      {head.tone === "bad" ? <Msg kind="err">{head.text}</Msg> : <p className="text-[12px] text-mist">{head.text}</p>}
      {list.length > 0 && (
        <ol className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
          {list.map((s, i) => {
            const chip = stageChip(s.status);
            return (
              <li key={s.key}>
                <button type="button" onClick={() => setOpen(open === s.key ? null : s.key)} aria-expanded={open === s.key}
                  className={`h-full w-full rounded-xl border p-2 text-left text-[11px] ${BORDER[chip.tone]} ${open === s.key ? "bg-white/10" : ""}`}>
                  <div className="flex items-center justify-between gap-1"><span className="text-mist">{i + 1}</span><Chip tone={chip.tone === "neutral" ? undefined : chip.tone}>{chip.text}</Chip></div>
                  <div className="mt-1 font-bold">{s.label}</div>
                  <div className="text-mist">{stageAge(s.age_s)}</div>
                  {s.count_today != null && s.count_today > 0 && <div className="text-mist">{s.count_today} today</div>}
                </button>
              </li>
            );
          })}
        </ol>
      )}
      {sel && (
        <div className="rounded-xl border border-slate p-2 text-[12px]">
          <p className="font-bold">{sel.label}: {sel.detail}</p>
          {sel.last_decision ? (
            <>
              <p className="mt-1">Last decision ({istHm(sel.last_decision.ts)} IST, {sel.last_decision.kind}): {sel.last_decision.text}</p>
              <details className="mt-1 text-mist"><summary className="cursor-pointer">Full journal entry</summary><pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-[11px]">{JSON.stringify(sel.last_decision.data, null, 2)}</pre></details>
            </>
          ) : <p className="mt-1 text-mist">Nothing has passed through this stage today.</p>}
        </div>
      )}
    </div>
  );
}
