// F&O Options > Positions / History / Charges / Log. Read-only views of what the engine decided and did.
import { useState } from "react";
import { fnoApi, type FnoPosition } from "../../fnoApi";
import { dayTotals, inr, istDay, istHm, splitEvent, tradesCsv } from "../../fnoView";
import { Btn, Card, Chip, Msg, pnlClass } from "../calibration/ui";
import { SubTabs } from "../ui/SubTabs";
import { useFno } from "./useFno";

export function TradeTable({ rows }: { rows: FnoPosition[] }) {
  if (!rows.length) return <p className="text-mist">No trades yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-mist"><tr><th>Day</th><th>Contract</th><th>Strategy</th><th>Lots</th><th>In</th><th>Entry</th><th>Out</th><th>Exit</th><th>Why</th><th className="text-right">Net</th></tr></thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.id ?? `${p.label}-${p.entry_ts}`}>
              <td>{istDay(p.entry_ts)}</td><td>{p.label}</td><td>{p.strategy || "CORE"}</td><td>{p.lots}{p.parent_id != null ? " (part)" : ""}</td><td>{istHm(p.entry_ts)}</td><td>{p.entry_premium.toFixed(2)}</td>
              <td>{istHm(p.exit_ts)}</td><td>{p.exit_premium?.toFixed(2) ?? "-"}</td><td>{p.exit_reason || (p.status === "OPEN" ? "open" : "-")}</td>
              <td className={`text-right ${pnlClass(p.net_pnl)}`}>{inr(p.net_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function download(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}

export function FnoPositions() {
  const [mode, setMode] = useState<"DEMO" | "REAL">("DEMO");
  const r = useFno(() => fnoApi.positions("all", mode), 10000, [mode]);
  return (
    <Card title="Positions" right={<><Chip active={mode === "DEMO"} onClick={() => setMode("DEMO")}>DEMO</Chip><Chip active={mode === "REAL"} onClick={() => setMode("REAL")} tone="bad">REAL</Chip></>}>
      {r.error && <Msg kind="err">{r.error}</Msg>}
      <TradeTable rows={r.data?.positions || []} />
    </Card>
  );
}

export function FnoHistory() {
  const [days, setDays] = useState(30);
  const [mode, setMode] = useState("DEMO");
  const h = useFno(() => fnoApi.history(days, mode), 0, [days, mode]);
  const ch = useFno(() => fnoApi.charges(days, mode), 0, [days, mode]);
  const m = h.data?.metrics?.overall || {};
  const totals = dayTotals(h.data?.trades || []);
  return (
    <div className="space-y-3">
      <Card title="History" right={<>
        {[7, 30, 90].map((d) => <Chip key={d} active={days === d} onClick={() => setDays(d)}>{d}d</Chip>)}
        <Chip active={mode === "DEMO"} onClick={() => setMode("DEMO")}>DEMO</Chip><Chip active={mode === "REAL"} onClick={() => setMode("REAL")} tone="bad">REAL</Chip>
        <Btn small disabled={!h.data?.trades.length} onClick={() => download(`fno_trades_${mode}.csv`, tradesCsv(h.data!.trades))}>CSV</Btn>
      </>}>
        {h.error && <Msg kind="err">{h.error}</Msg>}
        {h.data && (
          <>
            <ul className="mb-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
              <li>Trades <b>{m.trades ?? 0}</b></li><li>Win rate <b>{m.win_rate_pct ?? "-"}%</b></li><li>Net <b className={pnlClass(m.net)}>{inr(m.net)}</b></li>
              <li>Max drawdown <b>{inr(m.max_drawdown)}</b></li><li>Profit factor <b>{m.profit_factor ?? "-"}</b></li>
            </ul>
            <Msg kind="info"><b>{h.data.verdict.status}</b>{(h.data.verdict.reasons || []).map((r, i) => <div key={i}>{r}</div>)}</Msg>
          </>
        )}
      </Card>
      <Card title="By day"><ul className="space-y-0.5">{totals.map((d) => <li key={d.day} className="flex justify-between"><span>{d.day} · {d.trades} trades · {d.wins} wins</span><b className={pnlClass(d.net)}>{inr(d.net)}</b></li>)}</ul>{!totals.length && <p className="text-mist">Nothing closed in this period.</p>}</Card>
      <Card title="Trades"><TradeTable rows={h.data?.trades || []} /></Card>
      <Card title="Charges (configured rates, check against a real contract note)">
        {ch.data && (
          <>
            <ul className="grid grid-cols-2 gap-2 sm:grid-cols-4">{Object.entries(ch.data.totals).map(([k, v]) => <li key={k} className="capitalize">{k} <b>{inr(v, 2)}</b></li>)}</ul>
            <p className="mt-1 text-[11px] text-mist">{ch.data.note}</p>
          </>
        )}
      </Card>
    </div>
  );
}

export function FnoLog() {
  const [kind, setKind] = useState<"signals" | "all">("signals");
  const l = useFno(() => (kind === "signals" ? fnoApi.signals(150) : fnoApi.log(250)), 10000, [kind]);
  return (
    <Card title="Decision log" right={<><Chip active={kind === "signals"} onClick={() => setKind("signals")}>Signals &amp; trades</Chip><Chip active={kind === "all"} onClick={() => setKind("all")}>Everything</Chip></>}>
      {l.error && <Msg kind="err">{l.error}</Msg>}
      <ul className="space-y-1">
        {(l.data?.events || []).map((e) => {
          const { id, ts, kind: k, underlying } = e;
          const { strategy, rest } = splitEvent(e);
          return <li key={id} className="break-words"><span className="text-mist">{istDay(ts)} {istHm(ts)}</span> <Chip tone={k === "entry" ? "good" : k === "exit" || k === "veto" ? "warn" : k === "real_blocked" || k === "order_failed" ? "bad" : undefined}>{k}</Chip> {underlying} {strategy && <Chip>{strategy}</Chip>} <span className="text-mist">{JSON.stringify(rest).slice(0, 240)}</span></li>;
        })}
      </ul>
      {l.data && !l.data.events.length && <p className="text-mist">Nothing logged yet. The engine ships OFF.</p>}
    </Card>
  );
}

export { SubTabs };
