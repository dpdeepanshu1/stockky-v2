// Round 270: "Dhan money" card shared by Real Trade, Position Stocks and F&O Options.
// WHY: the three services share ONE Dhan account, and since Round 242 Real Trade writes a capital board (25 % each + a bonus 25 % to the better earner, F&O 0 while DEMO).
// The backends served it (GET /capital/board, GET /fno/capital) but no page showed it, and the old Capital Split card only drew a fixed 50/50 split and only
// after a REAL-mode login with Dhan funds. This card reads the board itself (own poll, own error line) and shows: live Dhan funds (when the host has them), the account total and free cash,
// each service's share in % and rupees, what each holds in open positions, what is still free to trade ("room"), and host-supplied charges / P&L tiles.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { fmtInr } from "./realAuto/format";
import { boardView, type BoardStatus } from "../moneyView";
import { setVisibleInterval } from "../visibleInterval";

const STATE_CHIP: Record<string, { text: string; cls: string; hint: string }> = {
  live: { text: "BOARD LIVE", cls: "border-signal-buy/40 bg-signal-buy/10 text-signal-buy", hint: "This service sizes its trades from its board share." },
  shadow: { text: "SHOWN ONLY", cls: "border-signal-hold/40 bg-signal-hold/10 text-signal-hold", hint: "The board is displayed, but sizing still uses the fixed share." },
  off: { text: "BOARD OFF", cls: "border-slate bg-ink text-mist", hint: "CAPITAL_ALLOC_MODE is off on this service." },
  missing: { text: "NO BOARD", cls: "border-signal-sell/40 bg-signal-sell/10 text-signal-sell", hint: "No fresh capital board file." },
};

export interface MoneyTile { label: string; value: string; sub?: string; tone?: "good" | "bad" }

export default function DhanMoneyCard({ fetcher, thisService, dhanAvailable, tiles, footer, enabled = true }: {
  fetcher: () => Promise<BoardStatus>;
  thisService: "real_trade" | "position_stocks" | "fno";
  /** the live Dhan "available balance" the host page already has (optional) */
  dhanAvailable?: number | null;
  /** extra tiles from the host page: charges today, P&L before / after charges ... */
  tiles?: MoneyTile[];
  footer?: ReactNode;
  /** false = do not poll (e.g. not logged in); a short sign-in line is shown instead */
  enabled?: boolean;
}) {
  const [st, setSt] = useState<BoardStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const fRef = useRef(fetcher);
  fRef.current = fetcher;
  const load = useCallback(async () => {
    try { setSt(await fRef.current()); setErr(null); } catch (e: any) { setErr(String(e?.message || e)); }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    void load();
    return setVisibleInterval(() => void load(), 30_000);
  }, [load, enabled]);

  const v = boardView(st ? { ...st, this_service: st.this_service || thisService } : null);
  const chip = STATE_CHIP[v.state];
  const me = v.thisRow;
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4" data-testid="dhan-money-card">
      <div className="flex items-center justify-between mb-3 gap-2">
        <p className="dash-section-title">Dhan money - allocation, free to trade, charges</p>
        <span title={chip.hint} className={`font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border ${chip.cls}`}>{chip.text}</span>
      </div>
      {!enabled && <p className="font-display tabular-nums text-[10px] text-signal-hold mb-2">Log in (admin session) to read the capital board.</p>}
      {err && <p className="font-display tabular-nums text-[10px] text-signal-sell mb-2">Could not read the capital board: {err}</p>}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2">
        <Tile label="Dhan available now" value={dhanAvailable != null ? fmtInr(dhanAvailable) : "-"} sub={dhanAvailable != null ? "live from Dhan funds" : "no live Dhan funds on this page"} />
        <Tile label="Account total (board)" value={fmtInr(v.total)} sub="free cash + money in open positions" />
        <Tile label="Free cash (board)" value={fmtInr(v.freeCash)} sub="from Real Trade's Dhan sync" />
        {v.sizing
          ? <Tile label="This service sizes from" value={fmtInr(v.sizing.amount)} sub={v.sizing.label} tone="good" />
          : <Tile label="This service's share" value={me?.pct != null ? `${me.pct}%` : "-"} sub={me?.amount != null ? fmtInr(me.amount) : "not applied"} />}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
        <Tile label="Free for this service" value={me?.room != null ? fmtInr(me.room) : "-"} tone="good" sub={me ? `${fmtInr(me.exposure)} already in open positions` : "needs the board"} />
        {(tiles || []).map((t) => <Tile key={t.label} label={t.label} value={t.value} sub={t.sub} tone={t.tone} />)}
      </div>

      {v.rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full font-display tabular-nums text-[11px]">
            <thead><tr className="text-mist text-[9px] uppercase tracking-widest text-left">
              <th className="py-1 pr-2">Service</th><th className="pr-2">Share</th><th className="pr-2">Rupees</th><th className="pr-2">In positions</th><th className="pr-2">Free</th><th>Score</th>
            </tr></thead>
            <tbody>
              {v.rows.map((r) => (
                <tr key={r.key} className={`border-t border-slate/50 ${r.isThis ? "text-paper font-bold" : "text-mist"}`}>
                  <td className="py-1 pr-2">{r.label}{r.isThis ? " (this)" : ""}{!r.active && <span className="ml-1 text-[9px] text-mist font-normal">idle</span>}</td>
                  <td className="pr-2">{r.pct != null ? `${r.pct}%` : "-"}</td>
                  <td className="pr-2">{fmtInr(r.amount)}</td>
                  <td className="pr-2">{fmtInr(r.exposure)}</td>
                  <td className="pr-2 text-signal-buy">{fmtInr(r.room)}</td>
                  <td>{r.score != null ? `${r.score}%` : "-"}<span className="text-[9px] text-mist font-normal"> {r.trades} trades</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {v.notes.map((n, i) => <p key={i} className="font-display tabular-nums text-[10px] text-mist mt-2 leading-relaxed">{n}</p>)}
      {footer}
      <p className="font-display tabular-nums text-[9px] text-mist mt-2 leading-relaxed">
        Rule: 25 % to each of Real Trade, Position Stocks and F&amp;O, and the last 25 % to the service with the better return after charges (an idle service gets 0 and its part is shared).
        {v.ageMin != null ? ` Board written ${v.ageMin} min ago.` : ""}
      </p>
    </div>
  );
}

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "good" | "bad" }) {
  return (
    <div className="rounded-xl bg-ink border border-slate p-2">
      <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">{label}</p>
      <p className={`font-display tabular-nums text-sm font-bold mt-0.5 ${tone === "good" ? "text-signal-buy" : tone === "bad" ? "text-signal-sell" : "text-paper"}`}>{value}</p>
      {sub ? <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">{sub}</p> : null}
    </div>
  );
}
