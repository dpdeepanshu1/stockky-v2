// group 442: every ON / OFF switch of one service (real-trade or position-stocks), grouped, with LIVE / RESTART badges, a confirm step for anything that
// LOOSENS a rule, the recommended value next to the current one, a one-click "move to recommended" (with a preview) and the change log.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, controlApi, type FlagRow, type FlagsDoc, type PresetPlanRow, type Svc } from "../../calibrationApi";
import { Btn, Card, Msg } from "./ui";

const SVC_NAME: Record<Svc, string> = { rt: "real-trade-service", ps: "position-stocks-service" };

interface Pending { row: FlagRow; value: string; info: any }

function valueLabel(f: FlagRow, v: string | null): string {
  if (v == null || v === "") return "unset";
  if (f.kind === "bool") return v === "true" ? "ON" : "OFF";
  return v;
}

function changeTone(c: string): string {
  return c === "LOOSENS" ? "text-signal-sell" : c === "TIGHTENS" ? "text-signal-buy" : "text-mist";
}

export default function SwitchesPanel({ svc, onChanged }: { svc: Svc; onChanged?: () => void }) {
  const [doc, setDoc] = useState<FlagsDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ kind: "ok" | "warn" | "err"; text: string } | null>(null);
  const [q, setQ] = useState("");
  const [only, setOnly] = useState<"all" | "diff" | "changed">("all");
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Pending | null>(null);
  const [preset, setPreset] = useState<{ changes: PresetPlanRow[]; loosening: number } | null>(null);
  const [audit, setAudit] = useState<any[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDoc(await controlApi.flags(svc));
      setErr(null);
    } catch (e: any) {
      setDoc(null);
      setErr(e instanceof ApiError && e.status === 404
        ? `This service has no /control routes yet: deploy the group 442 build of ${SVC_NAME[svc]} (docker compose up -d --build ${SVC_NAME[svc]}).`
        : e?.message || "Could not load the switches");
    }
  }, [svc]);

  useEffect(() => { setDoc(null); setNote(null); setAudit(null); setPreset(null); setDrafts({}); void load(); }, [load]);

  const apply = useCallback(async (row: FlagRow, value: string, confirm = false) => {
    setBusy(row.key);
    setNote(null);
    try {
      const r = await controlApi.set(svc, row.key, value, confirm);
      setPending(null);
      setDrafts((d) => { const n = { ...d }; delete n[row.key]; return n; });
      if (r.change === "SAME") setNote({ kind: "ok", text: `${row.title}: already ${valueLabel(row, r.value)}.` });
      else if (r.restart_required) setNote({ kind: "warn", text: `${row.title}: saved as ${valueLabel(row, r.value)}, but it only takes effect after a restart: ${doc?.restart_hint || "restart the service"} (outside market hours).` });
      else setNote({ kind: "ok", text: `${row.title}: now ${valueLabel(row, r.value)}, effective immediately (${r.change.toLowerCase()}).` });
      await load();
      onChanged?.();
    } catch (e: any) {
      if (e instanceof ApiError && e.needsConfirm) setPending({ row, value, info: e.detail });
      else setNote({ kind: "err", text: `${row.title}: ${e?.message || e}` });
    } finally {
      setBusy(null);
    }
  }, [svc, doc, load, onChanged]);

  const clear = useCallback(async (row: FlagRow) => {
    setBusy(row.key);
    try {
      const r = await controlApi.clear(svc, row.key);
      setNote({ kind: r.restart_required ? "warn" : "ok", text: `${row.title}: back to the deployed default${r.restart_required ? ` (restart needed: ${doc?.restart_hint})` : ""}.` });
      await load();
      onChanged?.();
    } catch (e: any) {
      setNote({ kind: "err", text: `${row.title}: ${e?.message || e}` });
    } finally {
      setBusy(null);
    }
  }, [svc, doc, load, onChanged]);

  const previewPreset = useCallback(async () => {
    setNote(null);
    try {
      const r = await controlApi.preset(svc, true);
      if (!r.changes.length) setNote({ kind: "ok", text: "Every switch that has a recommended value is already on it." });
      else setPreset({ changes: r.changes, loosening: r.loosening || 0 });
    } catch (e: any) {
      setNote({ kind: "err", text: e?.message || String(e) });
    }
  }, [svc]);

  const applyPreset = useCallback(async () => {
    if (!preset) return;
    setBusy("__preset");
    try {
      const r = await controlApi.preset(svc, false, preset.loosening > 0);
      setPreset(null);
      setNote({ kind: r.restart_required?.length ? "warn" : "ok", text: `Moved ${r.changes.length} switch(es) to the recommended value. Live: ${r.applied?.length || 0}. ${r.restart_required?.length ? `Need a restart: ${r.restart_required.join(", ")}.` : ""}` });
      await load();
      onChanged?.();
    } catch (e: any) {
      setNote({ kind: "err", text: e?.message || String(e) });
    } finally {
      setBusy(null);
    }
  }, [svc, preset, load, onChanged]);

  const showAudit = useCallback(async () => {
    try { setAudit((await controlApi.audit(svc, 80)).rows); } catch (e: any) { setNote({ kind: "err", text: e?.message || String(e) }); }
  }, [svc]);

  const groups = useMemo(() => {
    const m = new Map<string, FlagRow[]>();
    const ql = q.trim().toLowerCase();
    for (const f of doc?.flags || []) {
      if (ql && !(`${f.title} ${f.key} ${f.help}`.toLowerCase().includes(ql))) continue;
      if (only === "diff" && f.matches_recommended !== false) continue;
      if (only === "changed" && !(f.override || f.pending_restart)) continue;
      m.set(f.group, [...(m.get(f.group) || []), f]);
    }
    return [...m.entries()];
  }, [doc, q, only]);

  if (err) return <Msg kind="err">{err}</Msg>;
  if (!doc) return <div className="text-mist text-[12px]">Loading switches…</div>;

  const s = doc.summary;
  return (
    <div className="space-y-3" data-testid={`switches-${svc}`}>
      <Card title={`${SVC_NAME[svc]}: ${s.flags} switches`}
        right={<><Btn small onClick={() => void load()}>Refresh</Btn><Btn small onClick={() => void previewPreset()} title="Shows what would change before anything is applied">Move to recommended…</Btn><Btn small onClick={() => void showAudit()}>Change log</Btn></>}>
        <div className="flex flex-wrap gap-2 text-[11px]">
          <span className="rounded-lg border border-slate px-2 py-0.5">changed from this page: <b>{s.overrides}</b></span>
          <span className={`rounded-lg border px-2 py-0.5 ${s.pending_restart ? "border-signal-hold text-signal-hold" : "border-slate"}`}>waiting for a restart: <b>{s.pending_restart}</b></span>
          <span className={`rounded-lg border px-2 py-0.5 ${s.off_recommended ? "border-signal-hold text-signal-hold" : "border-slate"}`}>different from recommended: <b>{s.off_recommended}</b></span>
        </div>
        {!doc.persistent && <div className="mt-2"><Msg kind="warn">Changes made here are NOT saved across a restart: the state folder {doc.state_dir} is not writable ({doc.store_error || "unknown"}). Fix the volume before relying on them.</Msg></div>}
        <p className="mt-2 text-mist">LIVE = takes effect at once, proved by a code check. RESTART = saved now, applied when the service restarts ({doc.restart_hint}). Anything that loosens a rule asks you to confirm. The recommended value is what the repo's own evidence notes say, not advice.</p>
      </Card>

      {note && <Msg kind={note.kind === "err" ? "err" : note.kind === "warn" ? "warn" : "ok"}>{note.text}</Msg>}

      <div className="flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search switches…" aria-label="Search switches" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]" />
        {(["all", "diff", "changed"] as const).map((k) => (
          <button key={k} type="button" aria-pressed={only === k} onClick={() => setOnly(k)} className={`rounded-lg border px-2 py-0.5 text-[11px] ${only === k ? "border-signal-buy text-signal-buy" : "border-slate text-mist"}`}>
            {k === "all" ? "All" : k === "diff" ? "Not recommended" : "Changed / pending"}
          </button>
        ))}
      </div>

      {groups.length === 0 && <div className="text-mist">Nothing matches.</div>}
      {groups.map(([g, flags]) => (
        <Card key={g} title={<button type="button" onClick={() => setClosed((c) => ({ ...c, [g]: !c[g] }))} aria-expanded={!closed[g]}>{closed[g] ? "▶" : "▼"} {g} ({flags.length})</button>}>
          {!closed[g] && (
            <ul className="divide-y divide-slate">
              {flags.map((f) => {
                const draft = drafts[f.key];
                const cur = f.effective;
                const dirty = draft !== undefined && draft !== cur;
                return (
                  <li key={f.key} className="py-2" data-testid={`flag-${f.key}`}>
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <button type="button" className="text-left font-bold" onClick={() => setOpen(open === f.key ? null : f.key)} aria-expanded={open === f.key}>{f.title}</button>
                          <span className={`rounded border px-1 text-[9px] font-bold ${f.live ? "border-signal-buy text-signal-buy" : "border-signal-hold text-signal-hold"}`}>{f.live ? "LIVE" : "RESTART"}</span>
                          {f.override && <span className="rounded border border-slate px-1 text-[9px] text-mist" title={`${f.override.by} at ${f.override.at}`}>changed here</span>}
                          {f.pending_restart && <span className="rounded border border-signal-hold px-1 text-[9px] font-bold text-signal-hold">PENDING RESTART (running {valueLabel(f, f.running)})</span>}
                          {f.matches_recommended === false && <span className="rounded border border-signal-hold px-1 text-[9px] text-signal-hold">recommended: {valueLabel(f, f.recommended)}</span>}
                          {f.matches_recommended === true && <span className="rounded border border-slate px-1 text-[9px] text-mist">= recommended</span>}
                        </div>
                        <div className="text-[10px] text-mist">{f.key} · from {f.source}{f.shipped != null ? ` · deployed default ${valueLabel(f, f.shipped)}` : ""}</div>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {f.kind === "bool" ? (
                          <button type="button" role="switch" aria-checked={cur === "true"} aria-label={`${f.title}: ${valueLabel(f, cur)}`} disabled={busy === f.key}
                            onClick={() => void apply(f, cur === "true" ? "false" : "true")}
                            className={`min-w-[64px] rounded-xl border px-3 py-1 text-[11px] font-bold ${cur === "true" ? "border-signal-buy text-signal-buy" : "border-slate text-mist"}`}>
                            {busy === f.key ? "…" : cur === "true" ? "ON" : "OFF"}
                          </button>
                        ) : f.kind === "enum" ? (
                          <select value={draft ?? cur ?? ""} aria-label={f.title} disabled={busy === f.key} onChange={(e) => void apply(f, e.target.value)} className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]">
                            {f.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                          </select>
                        ) : (
                          <>
                            <input type="number" inputMode="decimal" step={f.kind === "int" ? 1 : "any"} min={f.lo ?? undefined} max={f.hi ?? undefined} value={draft ?? cur ?? ""} aria-label={f.title}
                              onChange={(e) => setDrafts((d) => ({ ...d, [f.key]: e.target.value }))} className="w-24 rounded-lg border border-slate bg-transparent px-2 py-1 text-right text-[12px]" />
                            <Btn small disabled={!dirty || busy === f.key} onClick={() => void apply(f, draft as string)}>Apply</Btn>
                          </>
                        )}
                        {f.override && <Btn small disabled={busy === f.key} onClick={() => void clear(f)} title="Remove the change made here and go back to the deployed default">Reset</Btn>}
                      </div>
                    </div>
                    {open === f.key && (
                      <div className="mt-1 space-y-1 text-[11px] text-mist">
                        <p>{f.help}</p>
                        {f.evidence && <p><b>Decided by:</b> {f.evidence}</p>}
                        {(f.lo != null || f.hi != null) && <p>Allowed range: {f.lo ?? "-"} to {f.hi ?? "-"}</p>}
                        {f.recommended != null && f.matches_recommended === false && <Btn small disabled={busy === f.key} onClick={() => void apply(f, f.recommended as string)}>Set to recommended ({valueLabel(f, f.recommended)})</Btn>}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      ))}

      {pending && (
        <Modal onClose={() => setPending(null)} title="This loosens a rule">
          <p>{pending.row.title}: <b>{valueLabel(pending.row, pending.info?.from)}</b> → <b>{valueLabel(pending.row, pending.info?.to ?? pending.value)}</b></p>
          <p className="mt-1 text-mist">{pending.info?.message || "Real trades that this rule used to stop may now go through."}</p>
          {pending.row.evidence && <p className="mt-1 text-mist">Decided by: {pending.row.evidence}</p>}
          <div className="mt-3 flex gap-2"><Btn primary onClick={() => void apply(pending.row, pending.value, true)}>Yes, loosen it</Btn><Btn onClick={() => setPending(null)}>Cancel</Btn></div>
        </Modal>
      )}

      {preset && (
        <Modal onClose={() => setPreset(null)} title={`Move ${preset.changes.length} switch(es) to the recommended value`}>
          <ul className="max-h-64 space-y-1 overflow-auto">
            {preset.changes.map((c) => (
              <li key={c.key} className="flex justify-between gap-2"><span>{c.title}</span><span className={changeTone(c.change)}>{c.from ?? "unset"} → {c.to} ({c.change.toLowerCase()}{c.live ? "" : ", restart"})</span></li>
            ))}
          </ul>
          {preset.loosening > 0 && <p className="mt-2 text-signal-sell">{preset.loosening} of these LOOSEN a live rule.</p>}
          <div className="mt-3 flex gap-2"><Btn primary disabled={busy === "__preset"} onClick={() => void applyPreset()}>{preset.loosening ? "Apply, including the loosening ones" : "Apply"}</Btn><Btn onClick={() => setPreset(null)}>Cancel</Btn></div>
        </Modal>
      )}

      {audit && (
        <Card title="Change log (newest first)" right={<Btn small onClick={() => setAudit(null)}>Close</Btn>}>
          {audit.length === 0 ? <div className="text-mist">No changes yet.</div> : (
            <table className="w-full text-[11px]"><tbody>
              {audit.map((r, i) => (
                <tr key={i} className="border-t border-slate"><td className="py-1 pr-2 text-mist">{String(r.at || "").slice(0, 16).replace("T", " ")}</td><td className="pr-2">{r.by}</td><td className="pr-2">{r.key}</td><td>{r.action === "clear" ? `reset (was ${String(r.was ?? "?")})` : `${String(r.from ?? "unset")} → ${String(r.to ?? "")}`} <span className="text-mist">{r.change || ""}{r.note ? ` · ${r.note}` : ""}</span></td></tr>
              ))}
            </tbody></table>
          )}
        </Card>
      )}
    </div>
  );
}

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3" role="dialog" aria-modal="true" aria-label={title} onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl border border-slate bg-graphite p-4 text-[12px]" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-2 text-[13px] font-bold">{title}</h3>
        {children}
      </div>
    </div>
  );
}
