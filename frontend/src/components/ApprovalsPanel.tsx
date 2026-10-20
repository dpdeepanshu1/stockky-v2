// group 468/469: approve-first mode. Shows entries waiting for your approval after a settings change.
// Renders nothing while the server has it off (APPROVE_FIRST_N unset or 0), so it costs no screen space by default.
// Used by Real Automatic Trade and by Position Stocks; each passes its OWN api client (the two services stay separate trust boundaries).
import { useCallback, useEffect, useState } from "react";

export interface ApprovalsStatus {
  enabled: boolean;
  n?: number | null;
  remaining?: number | null;
  ttl_min?: number;
  pending: { symbol: string; qty?: number; price?: number; stop?: number; target?: number; conviction?: number; pct_change?: number; window?: string }[];
  approved: { symbol: string; expires_in_s: number }[];
  rejected: { symbol: string; expires_in_s: number }[];
  error?: string;
}

export interface ApprovalsSource {
  status: () => Promise<ApprovalsStatus>;
  approve: (symbol: string) => Promise<ApprovalsStatus>;
  reject: (symbol: string) => Promise<ApprovalsStatus>;
  reset: () => Promise<ApprovalsStatus>;
}

export default function ApprovalsPanel({ loggedIn, source }: { loggedIn: boolean; source: ApprovalsSource }) {
  const [st, setSt] = useState<ApprovalsStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setSt(await source.status()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, [source]);

  useEffect(() => {
    if (!loggedIn) return;
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [loggedIn, load]);

  const act = async (key: string, fn: () => Promise<ApprovalsStatus>) => {
    setBusy(key);
    try { setSt(await fn()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(null); }
  };

  if (!loggedIn || !st || !st.enabled) return null;
  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="font-semibold text-amber-300">Entry approvals ({st.remaining ?? 0} of {st.n ?? 0} left)</div>
        <button disabled={busy !== null} onClick={() => act("reset", () => source.reset())}
          className="rounded border border-amber-500/40 px-2 py-0.5 text-xs text-amber-200 disabled:opacity-50">Start a new round</button>
      </div>
      <div className="mb-2 text-xs text-slate-400">
        Gate settings changed, so the next automatic entries wait here. An approval lasts {st.ttl_min ?? 15} min; not approved = not bought.
      </div>
      {st.pending.length === 0 && <div className="text-xs text-slate-500">Nothing waiting.</div>}
      {st.pending.map((p) => (
        <div key={p.symbol} className="mb-1 flex items-center justify-between gap-2 rounded bg-slate-900/60 px-2 py-1">
          <div className="text-xs text-slate-200">
            <span className="font-semibold">{p.symbol}</span>
            {p.qty != null && p.price != null ? ` ${p.qty} @ ₹${p.price.toFixed(2)}` : ""}
            {p.stop != null ? ` | stop ₹${p.stop.toFixed(2)}` : ""}
            {p.target != null ? ` | target ₹${p.target.toFixed(2)}` : ""}
            {p.conviction != null ? ` | conviction ${p.conviction}` : ""}
            {p.pct_change != null ? ` | ${p.pct_change > 0 ? "+" : ""}${p.pct_change}%${p.window ? " / " + p.window : ""}` : ""}
          </div>
          <div className="flex gap-1">
            <button disabled={busy !== null} onClick={() => act("a" + p.symbol, () => source.approve(p.symbol))}
              className="rounded bg-emerald-600 px-2 py-0.5 text-xs text-white disabled:opacity-50">Approve</button>
            <button disabled={busy !== null} onClick={() => act("r" + p.symbol, () => source.reject(p.symbol))}
              className="rounded bg-rose-700 px-2 py-0.5 text-xs text-white disabled:opacity-50">Reject</button>
          </div>
        </div>
      ))}
      {st.approved.length > 0 && (
        <div className="mt-1 text-xs text-emerald-300">Approved, waiting for the next cycle: {st.approved.map((a) => a.symbol).join(", ")}</div>
      )}
      {err && <div className="mt-1 text-xs text-rose-400">{err}</div>}
    </div>
  );
}
