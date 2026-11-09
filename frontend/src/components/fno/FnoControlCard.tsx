// Round 262, slice 1: F&O Overview > Control. The four locks, the underlyings choice and the nine strategy switches as real switches in one place.
// Every click goes through controlApi.set (the same call as Real Money Calibration > F&O > Switches), so the LOOSENS confirmation, the audit log and the
// live / restart behaviour come from the service. Turning REAL on (REAL orders ON, or mode REAL) asks for a typed word. Risk numbers, entry window and exits stay on the Switches page.
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, controlApi, type FlagRow } from "../../calibrationApi";
import { currentValue, EXPIRY_STRATS, isOn, needsTypedConfirm, offRecommended, pickControl, REAL_PHRASE, resultText, toggledValue, typedConfirmOk, valueText } from "../../fnoControl";
import { Card, Chip, Msg } from "../calibration/ui";

function Switch({ row, busy, onSet }: { row: FlagRow; busy: boolean; onSet: (row: FlagRow, value: string) => void }) {
  const on = isOn(row);
  const demoOnly = EXPIRY_STRATS.includes(row.key);
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-lg border border-slate p-2 text-[12px]">
      <button type="button" role="switch" aria-checked={on} aria-label={row.title} disabled={busy} onClick={() => onSet(row, toggledValue(row))}
        className={`h-5 w-9 shrink-0 rounded-full border transition-colors ${on ? "border-signal-buy bg-signal-buy/30" : "border-slate bg-white/5"} disabled:opacity-50`}>
        <span className={`block h-4 w-4 rounded-full bg-white transition-transform ${on ? "translate-x-4" : "translate-x-0.5"}`} />
      </button>
      <b>{row.title}</b>
      <Chip tone={on ? "good" : undefined}>{on ? "ON" : "OFF"}</Chip>
      {demoOnly && <Chip tone="warn">DEMO only</Chip>}
      {row.pending_restart && <Chip tone="warn">restart pending</Chip>}
      {row.recommended != null && currentValue(row) !== String(row.recommended).toLowerCase() && <Chip title={`recommended: ${valueText(row, String(row.recommended))}`}>not recommended value</Chip>}
    </li>
  );
}

export default function FnoControlCard({ onChanged }: { onChanged?: () => void }) {
  const [rows, setRows] = useState<FlagRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "warn" | "err"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await controlApi.flags("fno");
      if (!d || !Array.isArray(d.flags)) throw new Error("fno-options-service answered /control/flags with an unexpected shape.");
      setRows(d.flags);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message || "Could not load the switches");
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const view = useMemo(() => pickControl(rows), [rows]);

  const onSet = useCallback(async (row: FlagRow, value: string) => {
    setMsg(null);
    let confirm = false;
    if (needsTypedConfirm(row.key, value)) {
      const typed = window.prompt(`${row.title} -> ${valueText(row, value)} lets this service send REAL orders to Dhan (they also need the daily Arm). Only do this after DEMO evidence.\n\nType ${REAL_PHRASE} to continue:`);
      if (!typedConfirmOk(typed)) { setMsg({ kind: "warn", text: "Not changed: the confirmation word did not match." }); return; }
      confirm = true;
    }
    setBusy(row.key);
    try {
      let res;
      try {
        res = await controlApi.set("fno", row.key, value, confirm);
      } catch (e: any) {
        if (e instanceof ApiError && e.needsConfirm) {
          if (!window.confirm(`${e.detail?.message || `${row.title}: this LOOSENS a live rule.`}\n\nApply anyway?`)) return;
          res = await controlApi.set("fno", row.key, value, true);
        } else throw e;
      }
      setMsg({ kind: res.restart_required ? "warn" : "ok", text: resultText(row, res) });
      await load();
      onChanged?.();
    } catch (e: any) {
      setMsg({ kind: "err", text: `${row.title}: ${e?.message || e}` });
    } finally {
      setBusy(null);
    }
  }, [load, onChanged]);

  const bools = view.locks.filter((r) => r.kind === "bool");
  const enums = view.locks.filter((r) => r.kind === "enum");
  const off = offRecommended(view);
  return (
    <Card title="Control" right={<>{off > 0 && <Chip tone="warn">{off} not on recommended</Chip>}<Chip tone="good">LIVE: no restart</Chip></>}>
      {err && <Msg kind="err">{err}</Msg>}
      {msg && <div className="mb-2"><Msg kind={msg.kind}>{msg.text}</Msg></div>}
      {!rows && !err && <p className="text-mist">Loading…</p>}
      {rows && (
        <div className="space-y-3">
          <div>
            <p className="mb-1 text-[11px] font-bold uppercase tracking-widest text-mist">Locks</p>
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {bools.map((r) => <Switch key={r.key} row={r} busy={busy === r.key} onSet={onSet} />)}
              {enums.map((r) => (
                <li key={r.key} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate p-2 text-[12px]">
                  <b>{r.title}</b>
                  <select aria-label={r.title} disabled={busy === r.key} value={currentValue(r)} onChange={(e) => void onSet(r, e.target.value)} className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]">
                    {r.choices.map((c) => <option key={c} value={c} className="bg-graphite">{c.toUpperCase()}</option>)}
                  </select>
                </li>
              ))}
            </ul>
            <p className="mt-1 text-[11px] text-mist">REAL orders need all four locks plus the daily Arm below, and Dhan must have Derivatives enabled in Trader Control. Turning REAL on asks you to type {REAL_PHRASE}.</p>
          </div>
          <div>
            <p className="mb-1 text-[11px] font-bold uppercase tracking-widest text-mist">Strategies</p>
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">{view.strategies.map((r) => <Switch key={r.key} row={r} busy={busy === r.key} onSet={onSet} />)}</ul>
            <p className="mt-1 text-[11px] text-mist">A strategy stays OFF until its backtest passes. Risk %, entry window, exits and charges are on Real Money Calibration &gt; F&amp;O &gt; Switches.</p>
          </div>
        </div>
      )}
    </Card>
  );
}
