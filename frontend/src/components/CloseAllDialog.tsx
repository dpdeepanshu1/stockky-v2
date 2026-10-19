// group 466: typed-confirmation dialog for "Close all and disarm". Shared by Real Automatic Trade and Position Stocks.
// The button stays disabled until the operator types CLOSE ALL; the backend re-checks the same phrase (400 without it).
import { useEffect, useRef, useState } from "react";

export const CLOSE_ALL_PHRASE = "CLOSE ALL";

export interface CloseAllResult {
  ok: boolean;
  summary: string;
}

export default function CloseAllDialog({
  open, title, detail, onCancel, onConfirm,
}: {
  open: boolean;
  title: string;
  detail: string;
  onCancel: () => void;
  onConfirm: (phrase: string) => Promise<CloseAllResult>;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CloseAllResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) { setTyped(""); setResult(null); setErr(null); setBusy(false); setTimeout(() => inputRef.current?.focus(), 0); }
  }, [open]);

  if (!open) return null;
  const matches = typed.trim().toUpperCase() === CLOSE_ALL_PHRASE;

  const go = async () => {
    if (!matches || busy) return;
    setBusy(true); setErr(null);
    try {
      setResult(await onConfirm(typed.trim()));
    } catch (e: any) {
      setErr(e?.message || "Close all failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4" role="dialog" aria-modal="true">
      <div className="w-full max-w-sm rounded-2xl border border-signal-sell/40 bg-graphite p-4">
        <p className="font-display text-sm text-signal-sell">🚨 {title}</p>
        <p className="mt-2 font-display text-[11px] text-mist">{detail}</p>
        {!result && (
          <>
            <p className="mt-3 font-display text-[11px] text-paper">
              Type <span className="text-signal-sell">{CLOSE_ALL_PHRASE}</span> to confirm:
            </p>
            <input
              ref={inputRef}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoCapitalize="characters"
              autoComplete="off"
              className="mt-1 w-full rounded-xl border border-slate bg-ink px-3 py-2 font-display tabular-nums text-xs text-paper"
              placeholder={CLOSE_ALL_PHRASE}
            />
          </>
        )}
        {err && <p className="mt-2 font-display text-[11px] text-signal-sell">{err}</p>}
        {result && (
          <p className={`mt-3 font-display text-[11px] ${result.ok ? "text-signal-buy" : "text-signal-sell"}`}>{result.summary}</p>
        )}
        <div className="mt-4 flex gap-2">
          <button onClick={onCancel} disabled={busy}
            className="flex-1 rounded-xl border border-slate bg-ink py-2 font-display text-xs text-paper disabled:opacity-40">
            {result ? "Done" : "Cancel"}
          </button>
          {!result && (
            <button onClick={() => void go()} disabled={!matches || busy}
              className="flex-1 rounded-xl border border-signal-sell/40 bg-signal-sell/30 py-2 font-display text-xs text-signal-sell disabled:opacity-30">
              {busy ? "Closing…" : "Close all"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
