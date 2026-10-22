// group310 (+309 display): P&L calendar heatmap, win rate by scan window, and the shadow-report summary. Read-only; all of it is
// derived from GET /trades/history and GET /board/shadow-report. Small samples are labelled as such, never dressed up.
import { useEffect, useMemo, useState } from "react";
import { positionStocksApi, type JournalNotesResponse } from "../../positionStocksApi";
import { byDay, byTag, type JTrade } from "./journalStats";
import { EMOTION_LABEL, FALLBACK_EMOTIONS, emotionLabel, feelingText, noteRows, type TradeLite } from "./journalNotesView";
import { Card } from "./boardCard";

export default function JournalPanel({ active }: { active: boolean }) {
  const [trades, setTrades] = useState<JTrade[]>([]);
  const [rep, setRep] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  // E30: reason + feeling notes
  const [jn, setJn] = useState<JournalNotesResponse | null>(null);
  const [editId, setEditId] = useState<number | null>(null);
  const [dReason, setDReason] = useState("");
  const [dEmotion, setDEmotion] = useState("");
  const [saving, setSaving] = useState(false);
  const [noteErr, setNoteErr] = useState<string | null>(null);
  const loadNotes = async () => { try { setJn(await positionStocksApi.journalNotes(200)); } catch { /* notes are optional; the rest of the journal still works */ } };
  const startEdit = (id: number, reason: string | null, emotion: string | null) => { setEditId(id); setDReason(reason ?? ""); setDEmotion(emotion ?? ""); setNoteErr(null); };
  const saveNote = async () => {
    if (editId == null) return;
    setSaving(true); setNoteErr(null);
    try { await positionStocksApi.saveJournalNote(editId, dReason.trim(), dEmotion); setEditId(null); await loadNotes(); }
    catch (e: any) { setNoteErr(e?.message || "Could not save the note"); }
    finally { setSaving(false); }
  };
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void loadNotes();
    (async () => {
      try { const h = await positionStocksApi.tradeHistory(500); if (alive) setTrades(h.trades as unknown as JTrade[]); } catch (e: any) { if (alive) setErr(e?.message || "history failed"); }
      try { const r = await positionStocksApi.boardShadowReport(30); if (alive) setRep(r); } catch { /* shadow report is optional */ }
    })();
    return () => { alive = false; };
  }, [active]);
  const days = useMemo(() => byDay(trades), [trades]);
  const tags = useMemo(() => byTag(trades), [trades]);
  const maxAbs = Math.max(1, ...days.map(d => Math.abs(d.pnl)));
  const rows = useMemo(() => noteRows(trades as unknown as TradeLite[], jn?.notes ?? [], 15), [trades, jn]);
  const emotions = jn?.emotions?.length ? jn.emotions : FALLBACK_EMOTIONS;
  const notesOn = jn?.enabled !== false;
  return (
    <div className="space-y-3 text-[11px]">
      <Card title="P&L by day (IST)">
        {err && <div className="text-signal-hold">{err}</div>}
        {!days.length ? <div className="text-mist">No closed trades with a final P&L yet.</div> : (
          <div className="flex flex-wrap gap-1">
            {days.map(d => (
              <div key={d.day} title={`${d.day}: ₹${d.pnl.toFixed(2)} · ${d.trades} trades · ${d.wins} wins`} className="w-8 h-8 rounded flex items-center justify-center text-[8px] tabular-nums text-paper"
                   style={{ background: d.pnl >= 0 ? "var(--buy)" : "var(--sell)", opacity: 0.25 + 0.75 * (Math.abs(d.pnl) / maxAbs) }}>{d.day.slice(8)}</div>
            ))}
          </div>
        )}
      </Card>
      <Card title="Win rate by scan window">
        {tags.map(t => (
          <div key={t.tag} className="flex justify-between tabular-nums text-mist py-0.5">
            <span>{t.tag}</span><span>{t.trades} trades · {t.win_rate == null ? "-" : `${Math.round(t.win_rate * 100)}%`} wins{t.trades < 30 ? " (small sample)" : ""} · ₹{t.pnl.toFixed(0)}</span>
          </div>
        ))}
        {!tags.length && <div className="text-mist">Nothing to show yet.</div>}
      </Card>
      <Card title="Win rate by feeling">
        {jn?.by_emotion?.length ? jn.by_emotion.map(e => (
          <div key={e.emotion} className="flex justify-between tabular-nums text-mist py-0.5">
            <span className={e.risky ? "text-signal-hold" : ""}>{emotionLabel(e.emotion)}</span><span>{feelingText(e)}</span>
          </div>
        )) : <div className="text-mist">Add a feeling to a few closed trades below and this fills in.</div>}
        <div className="text-[9px] text-mist mt-1">Your own notes, over settled trades only. Nothing here blocks or sizes a trade; under {jn?.min_sample ?? 30} trades a feeling is anecdote, not a pattern.</div>
      </Card>
      <Card title="Trade notes · why, and how it felt">
        {!notesOn && <div className="text-mist">Journal notes are switched off (Real Money Calibration → Journal). Saved notes stay.</div>}
        {noteErr && editId == null && <div className="text-signal-hold">{noteErr}</div>}
        {!rows.length && <div className="text-mist">No trades yet.</div>}
        {rows.map(({ trade: t, note, manualMissing }) => (
          <div key={t.id} className="py-1 border-b border-slate/40 last:border-0">
            <div className="flex justify-between gap-2 tabular-nums">
              <span className="text-paper">{t.symbol} <span className="text-mist text-[9px]">{t.window_source} · {t.status}</span></span>
              <span className="text-mist">{t.realized_pnl == null ? "open / pending" : `₹${t.realized_pnl.toFixed(0)}`}
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
                <button disabled={saving} onClick={() => void saveNote()} className="px-2 py-1 rounded border border-slate text-paper disabled:opacity-40">{saving ? "Saving…" : "Save"}</button>
                <button disabled={saving} onClick={() => setEditId(null)} className="px-2 py-1 text-mist">Cancel</button>
                {noteErr && <div className="w-full text-signal-hold">{noteErr}</div>}
              </div>
            ) : note ? (
              <div className="text-mist text-[10px]">{note.emotion ? <b className="text-paper">{emotionLabel(note.emotion)}</b> : null}{note.emotion && note.reason ? " · " : ""}{note.reason}</div>
            ) : manualMissing ? <div className="text-[10px] text-signal-hold">Manual trade with no note yet.</div> : null}
          </div>
        ))}
        <div className="text-[9px] text-mist mt-1">Saving a note needs the admin login. Notes keep their own copy of the trade, so they outlive the 3-day trade-history cleanup.</div>
      </Card>
      {rep && (
        <Card title={`Board shadow study (${rep.days}d, gate mode: ${rep.gate_mode})`}>
          {Object.entries(rep.rules as Record<string, any>).map(([k, r]) => (
            <div key={k} className="py-0.5 text-mist"><b className="text-paper">{k}</b>: {r.verdict} <span className="text-[9px]">({r.about})</span></div>
          ))}
          <div className="text-[9px] text-mist mt-1">Needs {rep.min_obs} observations over {rep.min_sessions} sessions before any rule can be promoted. Research output, not advice.</div>
        </Card>
      )}
    </div>
  );
}
