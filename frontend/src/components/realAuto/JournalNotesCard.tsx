// E75: reason + feeling notes for Real Trade positions (twin of the Position Stocks journal notes, E30). Descriptive only: nothing here
// blocks, scores or sizes a trade. The row logic (manual trades first, labels) is the shared components/board/journalNotesView.ts.
import { useEffect, useMemo, useState } from "react";
import { realTradeApi, type JournalNotesResponse, type Position, type ClosedPosition } from "../../realTradeApi";
import { EMOTION_LABEL, FALLBACK_EMOTIONS, emotionLabel, feelingText, noteRows, type TradeLite } from "../board/journalNotesView";
import { SectionHdr } from "./parts";
import type { Mode } from "./types";

export function JournalNotesCard({ mode }: { mode: Mode }) {
  const [jn, setJn] = useState<JournalNotesResponse | null>(null);
  const [trades, setTrades] = useState<TradeLite[]>([]);
  const [editId, setEditId] = useState<number | null>(null);
  const [dReason, setDReason] = useState("");
  const [dEmotion, setDEmotion] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const [notes, open, closed] = await Promise.all([
        realTradeApi.journalNotes(mode),
        realTradeApi.positions(mode).catch(() => [] as Position[]),
        realTradeApi.closedPositions(mode, 100).catch(() => null),
      ]);
      setJn(notes);
      const manual = new Set(notes.manual_without_note);
      const noted = new Set(notes.notes.filter(n => n.manual).map(n => n.position_id));
      const lite = (id: number, symbol: string, status: string, pnl: number | null, opened: string): TradeLite =>
        ({ id, symbol, status, realized_pnl: pnl, opened_at: opened, window_source: manual.has(id) || noted.has(id) ? "MANUAL" : "AUTO" });
      const rows = [
        ...open.filter(p => !p.broker_imported).map(p => lite(p.id, p.symbol, p.status, null, p.opened_at)),
        ...((closed?.positions ?? []) as ClosedPosition[]).map(p => lite(p.id, p.symbol, "CLOSED", p.realized_pnl, p.opened_at)),
      ].sort((a, b) => (b.opened_at || "").localeCompare(a.opened_at || ""));
      setTrades(rows);
    } catch { /* notes are optional; the Activity tab still works */ }
  };
  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [mode]);

  const rows = useMemo(() => noteRows(trades, jn?.notes ?? [], 15), [trades, jn]);
  const emotions = jn?.emotions?.length ? jn.emotions : FALLBACK_EMOTIONS;
  const notesOn = jn?.enabled !== false;
  const startEdit = (id: number, reason: string | null, emotion: string | null) => { setEditId(id); setDReason(reason ?? ""); setDEmotion(emotion ?? ""); setErr(null); };
  const save = async () => {
    if (editId == null) return;
    setSaving(true); setErr(null);
    try { await realTradeApi.saveJournalNote(mode, editId, dReason.trim(), dEmotion); setEditId(null); await load(); }
    catch (e: any) { setErr(e?.message || "Could not save the note"); }
    finally { setSaving(false); }
  };

  return (
    <div className="space-y-2">
      <SectionHdr>Trade notes — why, and how it felt ({mode})</SectionHdr>
      <div className="bg-graphite border border-slate rounded-2xl p-3 font-display tabular-nums text-[11px] space-y-1">
        {jn?.by_emotion?.length ? jn.by_emotion.map(e => (
          <div key={e.emotion} className="flex justify-between text-mist">
            <span className={e.risky ? "text-signal-hold" : ""}>{emotionLabel(e.emotion)}</span><span>{feelingText(e)}</span>
          </div>
        )) : <div className="text-mist">Add a feeling to a few closed trades below and the win rate by feeling fills in.</div>}
        {!notesOn && <div className="text-mist">Journal notes are switched off (Real Money Calibration → Journal). Saved notes stay.</div>}
        {err && editId == null && <div className="text-signal-hold">{err}</div>}
        {rows.map(({ trade: t, note, manualMissing }) => (
          <div key={t.id} className="py-1 border-t border-slate/40">
            <div className="flex justify-between gap-2">
              <span className="text-paper">{t.symbol} <span className="text-mist text-[9px]">{t.window_source === "MANUAL" ? "manual" : "auto"} · {t.status}</span></span>
              <span className="text-mist">{t.realized_pnl == null ? "open" : `₹${t.realized_pnl.toFixed(0)}`}
                {notesOn && editId !== t.id && <button className="ml-2 underline" onClick={() => startEdit(t.id, note?.reason ?? null, note?.emotion ?? null)}>{note ? "edit" : "add note"}</button>}
              </span>
            </div>
            {editId === t.id ? (
              <div className="mt-1 flex flex-wrap items-center gap-1">
                <select value={dEmotion} onChange={e => setDEmotion(e.target.value)} className="px-1 py-1 rounded bg-ink border border-slate text-paper">
                  <option value="">feeling…</option>
                  {emotions.map(e => <option key={e} value={e}>{EMOTION_LABEL[e] ?? e}</option>)}
                </select>
                <input value={dReason} maxLength={jn?.max_reason ?? 300} onChange={e => setDReason(e.target.value)} placeholder="why did you take it?" className="flex-1 min-w-[10rem] px-2 py-1 rounded bg-ink border border-slate text-paper" />
                <button disabled={saving} onClick={() => void save()} className="px-2 py-1 rounded border border-slate text-paper disabled:opacity-40">{saving ? "Saving…" : "Save"}</button>
                <button disabled={saving} onClick={() => setEditId(null)} className="px-2 py-1 text-mist">Cancel</button>
                {err && <div className="w-full text-signal-hold">{err}</div>}
              </div>
            ) : note ? (
              <div className="text-mist text-[10px]">{note.emotion ? <b className="text-paper">{emotionLabel(note.emotion)}</b> : null}{note.emotion && note.reason ? " · " : ""}{note.reason}</div>
            ) : manualMissing ? <div className="text-[10px] text-signal-hold">Manual trade with no note yet.</div> : null}
          </div>
        ))}
        {!rows.length && <div className="text-mist">No trades yet.</div>}
        <div className="text-[9px] text-mist pt-1">Your own notes. Nothing here blocks or sizes a trade; under {jn?.min_sample ?? 30} trades a feeling is anecdote, not a pattern. Notes keep their own copy of the trade.</div>
      </div>
    </div>
  );
}
