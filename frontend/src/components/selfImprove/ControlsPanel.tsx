// Self Improve System > Controls (round 543, E568, S3). Two things can be set here and nothing else: (1) the AI agent's OWN settings (mode, run-after time, caps;
// POST /agent/settings) and (2) auto-calibration's master switch, dry-run and pinned list (PUT /autocalib/settings). It never sets a trading switch itself: a
// pinned switch is only marked "never changed automatically", an unpinned one may be changed later by auto-calibration's own rules. The second-click rule
// (plan v6, section 7): every move that WIDENS what can happen needs two clicks (propose, dry-run OFF, unpin, switch ON while dry-run is off); a move that
// narrows it is one click. Pure helpers: selfImproveControls.ts. step % and cool-down are shown, not editable: the runner has no route that changes them.
import { useEffect, useMemo, useState } from "react";
import { runnerApi, type AutoStatus } from "../../calibrationApi";
import { acArmId, acBody, acButtonText, acConfirmText, acNeedsTwoClicks, agentNeedsTwoClicks, draftBody, draftChangeText, draftFrom, acAuditLines, PIN_GROUP_TEXT, pinRows, pinSummary, protectedRows, resetNeedsTwoClicks, validateDraft, type AcAction, type AgentDraft, type PinGroup } from "../../selfImproveControls";
import { auditText, settingLines, type LoopView } from "../../selfImproveView";
import { Btn, Card, Chip, Msg } from "../calibration/ui";
import { useArm } from "./useArm";

const FIELD = "w-full rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]";

export default function ControlsPanel({ loop, ac, acError, reload }: { loop: LoopView; ac: AutoStatus | null; acError: string | null; reload: () => Promise<void> }) {
  const st = loop.agent.settings;
  const [draft, setDraft] = useState<AgentDraft>(() => draftFrom(st.values));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const { armed, press, cancel } = useArm();

  // a change that arrives from the server (another browser, a reset) replaces the form; the 30 s refresh of the same values does not
  const key = JSON.stringify(st.values);
  useEffect(() => { setDraft(draftFrom(st.values)); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true); setOk(null);
    try { await fn(); setErr(null); setOk(done); await reload(); } catch (e: any) { setErr(e?.message || String(e)); setOk(null); } finally { setBusy(false); }
  };

  const errors = validateDraft(draft, st.bounds);
  const bad = Object.keys(errors).length > 0;
  const { body, changed } = draftBody(st.values, draft, st.bounds);
  const needs2 = agentNeedsTwoClicks(st.values, body);
  const save = () => press("agent:save", needs2, () => void act(() => runnerApi.agentSetSettings({ ...body, ...(needs2 ? { confirm: true } : {}) }), "Agent settings saved."));
  const reset = () => press("agent:reset", resetNeedsTwoClicks(st), () => void act(() => runnerApi.agentSetSettings({ reset: true, ...(resetNeedsTwoClicks(st) ? { confirm: true } : {}) }), "Agent settings reset to .env."));

  const acs = ac?.settings ?? loop.autocalib.settings;
  const rows = useMemo(() => (ac ? pinRows(ac.rules, acs.pinned) : []), [ac, acs.pinned]);
  const run = (a: AcAction) => press(acArmId(a), acNeedsTwoClicks(a, acs), () => void act(() => runnerApi.autoSettings(acBody(a, acs.pinned)), "Auto-calibration setting saved."));
  const btn = (a: AcAction, small = true) => {
    const id = acArmId(a);
    const isArmed = armed === id;
    return <Btn small={small} primary={isArmed} disabled={busy || (a.kind === "pin" && !ac)} title={acNeedsTwoClicks(a, acs) ? "Needs a second click" : undefined} onClick={() => run(a)}>{acButtonText(a, isArmed)}</Btn>;
  };
  const armedAc = [{ kind: "dry_run", to: false }, { kind: "enabled", to: true }].map((a) => a as AcAction).find((a) => armed === acArmId(a)) || null;
  const armedUnpin = armed && armed.startsWith("pin:") && armed.endsWith(":unpin") ? armed.split(":")[1] : null;
  const groups = (["A", "B", "C", "other"] as PinGroup[]).map((g) => ({ g, rows: rows.filter((r) => r.group === g) })).filter((x) => x.rows.length);

  return (
    <div className="space-y-3" data-testid="si-controls">
      {err && <Msg kind="err">{err}</Msg>}
      {ok && <Msg kind="ok">{ok}</Msg>}
      {armed && <div className="flex items-center gap-2"><Msg kind="warn">Armed. Click the same button again within 8 seconds to confirm.</Msg><Btn small onClick={cancel}>Cancel</Btn></div>}

      <Card title="AI agent settings" tone={st.values.mode === "propose" ? "warn" : undefined}>
        <p className="text-mist">The agent only suggests ideas and, in propose mode, queues the existing tests behind them. It cannot change a switch. Saved on the runner (survives a restart) and written to an audit line.</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <label className="block text-[11px] text-mist">Mode
            <select aria-label="Agent mode" value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })} className={FIELD}>
              {st.bounds.modes.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            {errors.mode && <span className="text-[11px] text-red-400">{errors.mode}</span>}
          </label>
          <label className="block text-[11px] text-mist">Run after (IST, weekdays)
            <input aria-label="Agent run-after time" value={draft.run_after} inputMode="numeric" placeholder="16:10" onChange={(e) => setDraft({ ...draft, run_after: e.target.value })} className={FIELD} />
            {errors.run_after && <span className="text-[11px] text-red-400">{errors.run_after}</span>}
          </label>
          <label className="block text-[11px] text-mist">Max ideas a night
            <input aria-label="Agent max ideas" type="number" min={st.bounds.max_ideas[0]} max={st.bounds.max_ideas[1]} value={draft.max_ideas} onChange={(e) => setDraft({ ...draft, max_ideas: e.target.value })} className={FIELD} />
            {errors.max_ideas && <span className="text-[11px] text-red-400">{errors.max_ideas}</span>}
          </label>
          <label className="block text-[11px] text-mist">Max queued tests a night
            <input aria-label="Agent max queued tests" type="number" min={st.bounds.max_queue[0]} max={st.bounds.max_queue[1]} value={draft.max_queue} onChange={(e) => setDraft({ ...draft, max_queue: e.target.value })} className={FIELD} />
            {errors.max_queue && <span className="text-[11px] text-red-400">{errors.max_queue}</span>}
          </label>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Btn small primary={armed === "agent:save"} disabled={busy || bad || changed.length === 0} title={bad ? "Fix the fields above first" : changed.length === 0 ? "Nothing changed" : needs2 ? "Needs a second click" : undefined} onClick={save}>
            {armed === "agent:save" ? "Click again: save and allow queueing tests" : "Save"}
          </Btn>
          <Btn small primary={armed === "agent:reset"} disabled={busy} title="Drop the saved values: back to the .env ones" onClick={reset}>{armed === "agent:reset" ? "Click again: reset to .env (mode becomes propose)" : "Reset to .env"}</Btn>
          {changed.length > 0 && !bad && <span className="text-[11px] text-mist">Will change: {draftChangeText(st.values, body)}</span>}
        </div>
        {armed === "agent:save" && <p className="mt-1 text-[11px] text-mist">Propose lets the agent queue up to {draft.max_queue} existing tests a night. It still sets no trading switch.</p>}
        <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">In force now</div>
        <div className="space-y-1">{settingLines(st).map((l) => <div key={l} className="rounded-lg border border-slate px-2 py-1">{l}</div>)}</div>
        <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">Last changes</div>
        {st.audit.length === 0 && <p className="text-mist">No setting was changed from the page or API yet.</p>}
        {st.audit.map((x, i) => <div key={i} className="text-[11px] text-mist">{auditText(x)}</div>)}
      </Card>

      <Card title="Auto-calibration" tone={acs.enabled ? (acs.dry_run ? "warn" : "good") : undefined}>
        <p className="text-mist">When a test finishes, auto-calibration sets each unpinned switch from its result. Dry-run reports what it would do and applies nothing. A rule is tightened at once; it is loosened only after two different test runs agree.</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={acs.enabled ? (acs.dry_run ? "warn" : "good") : "bad"}>{acs.enabled ? (acs.dry_run ? "ON, dry-run (applies nothing)" : "ON, applying") : "OFF"}</Chip>
          <Chip>step {Math.round(acs.step_pct * 100)} % a change</Chip>
          <Chip>cool-down {acs.cooldown_h} h</Chip>
          <Chip>{acs.pinned.length} pinned</Chip>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {btn({ kind: "enabled", to: !acs.enabled })}
          {btn({ kind: "dry_run", to: !acs.dry_run })}
        </div>
        {armedAc && <Msg kind="warn">{acConfirmText(armedAc)}</Msg>}
        <p className="mt-1 text-[11px] text-mist">Step % and cool-down are set in .env and are read only here. Applying a change on the spot (Evaluate and apply now) stays on Real Money Calibration &gt; Auto-calibration.</p>
        <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">Last changes</div>
        {acAuditLines(ac?.audit).length === 0 && <p className="text-mist">{ac ? "No setting was changed from the page or API since the audit started (runner round 543)." : "Not loaded."}</p>}
        {acAuditLines(ac?.audit).map((x, i) => <div key={i} className="text-[11px] text-mist">{auditText(x)}</div>)}
      </Card>

      <Card title="Pinned switches">
        {acError && <Msg kind="err">{acError}</Msg>}
        {!ac && !acError && <p className="text-mist">Loading the switch list…</p>}
        {ac && (
          <>
            <p className="text-mist">{pinSummary(rows)}</p>
            <p className="text-[11px] text-mist">Pin = one click. Unpin = two clicks, because the switch then becomes eligible for an automatic change. The suggested order below is from plan v6.</p>
            {armedUnpin && <Msg kind="warn">{acConfirmText({ kind: "pin", key: armedUnpin, to: false })}</Msg>}
            {groups.map(({ g, rows: rs }) => (
              <div key={g} className="mt-2">
                <div className="text-[11px] font-bold uppercase tracking-widest text-mist">{PIN_GROUP_TEXT[g]}</div>
                <div className="mt-1 space-y-1">
                  {rs.map((r) => (
                    <div key={r.key} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate px-2 py-1">
                      <Chip tone={r.pinned ? undefined : "warn"}>{r.pinned ? "◼ PINNED" : "◻ UNPINNED"}</Chip>
                      <b>{r.key}</b>
                      <span className="text-[11px] text-mist">{r.service} · test: {r.tests.join(", ") || "-"}</span>
                      <span className="ml-auto">{btn({ kind: "pin", key: r.key, to: !r.pinned })}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {rows.length === 0 && <p className="text-mist">No switch has an auto-calibration rule.</p>}
          </>
        )}
      </Card>

      <Card title="Protected switches (read only)">
        <p className="text-mist">These can never get an auto-calibration rule, so they can never be pinned, unpinned or changed by it.</p>
        {!ac && <p className="text-mist">{acError ? "Not loaded." : "Loading…"}</p>}
        {ac && protectedRows(ac.protected).length === 0 && <p className="text-mist">The runner sent no protected list (older runner).</p>}
        <div className="space-y-1">{ac && protectedRows(ac.protected).map((r) => <div key={r.key} className="rounded-lg border border-slate px-2 py-1"><b>{r.key}</b><div className="text-[11px] text-mist">{r.why}</div></div>)}</div>
      </Card>
    </div>
  );
}
