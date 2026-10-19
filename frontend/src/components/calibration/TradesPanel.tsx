// group 442: daily trades of one service with entry time, exit time, hold and profit / loss, straight from the service (read-only GETs).
// The same numbers, with more cuts, are written to a txt file by the runner's "Daily trades" job (button at the top).
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { bucketBy, dayTotals, istDay, istHm, saveBlob, tradesApi, tradesCsv, type Svc, type TradeRow } from "../../calibrationApi";
import { Btn, Card, Msg, money, pnlClass } from "./ui";

function holdBucket(m: number): string {
  return m < 5 ? "a: < 5 min" : m < 15 ? "b: 5-15 min" : m < 60 ? "c: 15-60 min" : m < 240 ? "d: 1-4 h" : m < 1440 ? "e: 4 h-1 d" : "f: > 1 day";
}
function held(m: number): string {
  return m < 90 ? `${Math.round(m)}m` : m < 1440 ? `${(m / 60).toFixed(1)}h` : `${(m / 1440).toFixed(1)}d`;
}

export default function TradesPanel({ svc, onRunReport }: { svc: Svc; onRunReport: () => void }) {
  const [rows, setRows] = useState<TradeRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [days, setDays] = useState(10);
  const [openDay, setOpenDay] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setRows(svc === "rt" ? await tradesApi.realTrade(days) : await tradesApi.positionStocks());
      setErr(null);
    } catch (e: any) {
      setRows(null);
      setErr(e?.message || "Could not load trades");
    } finally {
      setBusy(false);
    }
  }, [svc, days]);

  useEffect(() => { setRows(null); void load(); }, [load]);

  const totals = useMemo(() => dayTotals(rows || []), [rows]);
  const today = istDay(new Date());
  const todayRow = totals.find((t) => t.day === today);
  const all = useMemo(() => {
    const rs = rows || [];
    const net = rs.reduce((a, r) => a + r.pnl, 0);
    const wins = rs.filter((r) => r.pnl > 0).length;
    return { n: rs.length, net, winPct: rs.length ? (100 * wins) / rs.length : null, avg: rs.length ? net / rs.length : null };
  }, [rows]);
  const byEntryHour = useMemo(() => bucketBy(rows || [], (r) => istHm(r.entry).slice(0, 2) + ":00"), [rows]);
  const byExitHour = useMemo(() => bucketBy(rows || [], (r) => istHm(r.exit).slice(0, 2) + ":00"), [rows]);
  const byHold = useMemo(() => bucketBy(rows || [], (r) => holdBucket(r.heldMin)), [rows]);
  const byReason = useMemo(() => bucketBy(rows || [], (r) => r.reason), [rows]);

  const csv = () => saveBlob(new Blob([tradesCsv(rows || [])], { type: "text/csv" }), `trades_${svc === "rt" ? "real-trade" : "position-stocks"}_${today}.csv`);

  return (
    <div className="space-y-3" data-testid={`trades-${svc}`}>
      <Card title={svc === "rt" ? "Real trade: closed trades" : "Position stocks: settled trades"}
        right={<>
          {svc === "rt" && (
            <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Days" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[11px]">
              {[5, 10, 30, 60].map((d) => <option key={d} value={d}>last {d} days</option>)}
            </select>
          )}
          <Btn small onClick={() => void load()} disabled={busy}>{busy ? "…" : "Refresh"}</Btn>
          <Btn small onClick={csv} disabled={!rows?.length}>CSV</Btn>
          <Btn small primary onClick={onRunReport} title="Runs the Daily trades job and keeps its txt output for download">Full report to txt</Btn>
        </>}>
        {svc === "ps" && <p className="mb-2 text-mist">Position stocks keeps only a few days of ledger on the server. Run the daily close pack every day so the saved copies build up for the opening replay.</p>}
        {err && <Msg kind="err">{err}</Msg>}
        {!err && !rows && <div className="text-mist">Loading…</div>}
        {rows && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Today net" value={money(todayRow?.net ?? 0)} cls={pnlClass(todayRow?.net ?? 0)} sub={`${todayRow?.n ?? 0} trades`} />
            <Stat label="Period net" value={money(all.net)} cls={pnlClass(all.net)} sub={`${all.n} trades`} />
            <Stat label="Win rate" value={all.winPct == null ? "-" : `${all.winPct.toFixed(0)}%`} sub={`avg ${money(all.avg)}`} />
            <Stat label="Days with trades" value={String(totals.length)} sub={svc === "rt" ? `of last ${days}` : "in the ledger"} />
          </div>
        )}
      </Card>

      {rows && rows.length === 0 && !err && <Msg kind="info">No closed trades in this window.</Msg>}

      {rows && rows.length > 0 && (
        <Card title="Per day (IST). Tap a day for every trade with entry and exit time">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-[11px] tabular-nums">
              <thead className="text-left text-mist"><tr><th className="py-1">Day</th><th>Trades</th><th>W / L</th><th>First entry</th><th>Last exit</th><th>Best</th><th>Worst</th><th className="text-right">Net P&L</th></tr></thead>
              <tbody>
                {totals.map((t) => (
                  <Fragment key={t.day}>
                    <tr className="cursor-pointer border-t border-slate hover:bg-black/10 focus-visible:outline focus-visible:outline-1" tabIndex={0} role="button" onClick={() => setOpenDay(openDay === t.day ? null : t.day)}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpenDay(openDay === t.day ? null : t.day); } }} aria-expanded={openDay === t.day}>
                      <td className="py-1">{openDay === t.day ? "▼" : "▶"} {t.day}</td><td>{t.n}</td><td>{t.wins} / {t.losses}</td><td>{t.firstEntry}</td><td>{t.lastExit}</td>
                      <td className={pnlClass(t.best)}>{money(t.best)}</td><td className={pnlClass(t.worst)}>{money(t.worst)}</td><td className={`text-right font-bold ${pnlClass(t.net)}`}>{money(t.net)}</td>
                    </tr>
                    {openDay === t.day && (
                      <tr><td colSpan={8} className="bg-black/10 p-2">
                        <table className="w-full text-[11px]"><thead className="text-left text-mist"><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Held</th><th>Return</th><th>Reason</th><th className="text-right">P&L</th></tr></thead>
                          <tbody>
                            {(rows || []).filter((r) => r.day === t.day).sort((a, b) => a.entry.getTime() - b.entry.getTime()).map((r, i) => (
                              <tr key={`${r.id}-${i}`}><td>{r.symbol}</td><td>{istDay(r.entry) === r.day ? "" : `${istDay(r.entry).slice(5)} `}{istHm(r.entry)}</td><td>{istHm(r.exit)}</td><td>{held(r.heldMin)}</td>
                                <td className={pnlClass(r.retPct)}>{r.retPct == null ? "-" : `${r.retPct.toFixed(2)}%`}</td><td className="text-mist">{r.reason}</td><td className={`text-right ${pnlClass(r.pnl)}`}>{money(r.pnl)}</td></tr>
                            ))}
                          </tbody></table>
                      </td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {rows && rows.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2">
          <BucketCard title="By entry hour (IST)" rows={byEntryHour} />
          <BucketCard title="By exit hour (IST)" rows={byExitHour} />
          <BucketCard title="By hold length" rows={byHold.map((b) => ({ ...b, key: b.key.slice(3) }))} />
          <BucketCard title="By exit reason" rows={byReason.sort((a, b) => a.net - b.net)} />
        </div>
      )}
      {rows && rows.length > 0 && <p className="text-[11px] text-mist">A bucket with fewer than 8 trades is a hint, not a finding. A bad hour or reason that repeats over several days is what to act on, and only through the evidence jobs, not by reacting to one day.</p>}
    </div>
  );
}

function Stat({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return <div className="rounded-xl border border-slate p-2"><div className="text-[10px] uppercase tracking-widest text-mist">{label}</div><div className={`text-[15px] font-bold tabular-nums ${cls || ""}`}>{value}</div>{sub && <div className="text-[10px] text-mist">{sub}</div>}</div>;
}

function BucketCard({ title, rows }: { title: string; rows: { key: string; n: number; net: number; avg: number; winPct: number }[] }) {
  return (
    <Card title={title}>
      <table className="w-full text-[11px] tabular-nums"><thead className="text-left text-mist"><tr><th>Bucket</th><th>n</th><th>Win</th><th>Avg</th><th className="text-right">Net</th></tr></thead>
        <tbody>{rows.map((b) => (
          <tr key={b.key} className="border-t border-slate"><td className="py-0.5">{b.key}{b.n < 8 ? <span className="text-mist"> (few)</span> : ""}</td><td>{b.n}</td><td>{b.winPct.toFixed(0)}%</td><td className={pnlClass(b.avg)}>{money(b.avg)}</td><td className={`text-right ${pnlClass(b.net)}`}>{money(b.net)}</td></tr>
        ))}</tbody></table>
    </Card>
  );
}
