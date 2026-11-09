// Round 266, plan slice 4: Real Money Calibration > All services. Real Trade, Position Stocks and F&O master switches and today's P&L side by side.
// READ-ONLY: it calls the status endpoints the service pages already use and changes nothing. Switches are changed where they live (the line on each card says where).
// F&O in DEMO mode is simulated money and is shown apart from the real-money total.
import { useCallback, useEffect, useRef, useState } from "react";
import { realTradeApi } from "../../realTradeApi";
import { positionStocksApi } from "../../positionStocksApi";
import { fnoApi } from "../../fnoApi";
import { basisText, failed, fromFno, fromPositionStocks, fromRealTrade, headline, liveState, rupees, STATE_TEXT, STATE_TONE, SVC_WHERE, totals, type SvcSnap } from "../../allServices";
import { Btn, Card, Chip, Msg } from "./ui";

const REFRESH_MS = 20000;
// The Real Trade charges route walks every filled order since the first one and stores today's gross with it, so it is NOT asked every 20 s: the answers
// of both charges routes are reused for CHARGES_MS (the Refresh button always asks again). A failed charges call is not kept, so the next refresh retries.
const CHARGES_MS = 60000;
interface ChargesCache { at: number; rt: unknown; ps: unknown }

async function loadAll(cache: { current: ChargesCache | null }, force: boolean): Promise<SvcSnap[]> {
  const fresh = !force && cache.current != null && Date.now() - cache.current.at < CHARGES_MS;
  // the status answers give today's GROSS; the charges answers give the NET (a failed charges call only means the figure is labelled gross)
  const [rt, ps, led, fno, rtCh, psCh] = await Promise.allSettled([
    realTradeApi.gateStatus("REAL"),
    positionStocksApi.status(),
    positionStocksApi.ledger(),
    fnoApi.overview(),
    fresh ? Promise.resolve(cache.current!.rt) : realTradeApi.cumulativeBrokerage("REAL", 1),
    fresh ? Promise.resolve(cache.current!.ps) : positionStocksApi.cumulativeCharges(1),
  ]);
  const ok = (r: PromiseSettledResult<unknown>) => (r.status === "fulfilled" ? r.value : null);
  if (!fresh) {
    // keep the two charges answers together only when both worked; one failure leaves that service's figure gross until the next try
    cache.current = rtCh.status === "fulfilled" && psCh.status === "fulfilled" ? { at: Date.now(), rt: rtCh.value, ps: psCh.value } : null;
  }
  return [
    rt.status === "fulfilled" ? fromRealTrade(rt.value, ok(rtCh)) : failed("rt", rt.reason),
    ps.status === "fulfilled" ? fromPositionStocks(ps.value, ok(led), ok(psCh)) : failed("ps", ps.reason),
    fno.status === "fulfilled" ? fromFno(fno.value) : failed("fno", fno.reason),
  ];
}

const yn = (v: boolean | null, yes = "ON", no = "OFF") => (v == null ? "?" : v ? yes : no);

function SvcCard({ s }: { s: SvcSnap }) {
  const st = liveState(s);
  return (
    <Card title={s.label} right={<Chip tone={STATE_TONE[st]}>{STATE_TEXT[st]}</Chip>} tone={st === "live" || st === "kill_switch" ? "bad" : undefined}>
      {!s.ok ? (
        <Msg kind="warn">{s.error}</Msg>
      ) : (
        <div className="space-y-2">
          <p className="text-[22px] font-bold" aria-label={`${s.label} P&L today`}>{rupees(s.pnlToday)}</p>
          <p className="text-[11px] text-mist">{basisText(s)}</p>
          <ul className="grid grid-cols-2 gap-1 text-[12px]">
            <li>Mode: <b>{s.mode ?? "?"}</b></li>
            <li>{s.id === "fno" ? "Engine" : "Auto-pilot"}: <b>{yn(s.engineOn)}</b></li>
            <li>Armed today: <b>{yn(s.armed, "YES", "NO")}</b></li>
            {s.killSwitch != null && <li>Loss kill switch: <b>{s.killSwitch ? "TRIPPED" : "clear"}</b></li>}
            {s.openCount != null && <li>Open positions: <b>{s.openCount}</b></li>}
          </ul>
          {s.note && <p className="text-[11px] text-mist">{s.note}</p>}
        </div>
      )}
      <p className="mt-2 text-[11px] text-mist">Change it on: {SVC_WHERE[s.id]}</p>
    </Card>
  );
}

export default function AllServicesPanel() {
  const [snaps, setSnaps] = useState<SvcSnap[] | null>(null);
  const [at, setAt] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const charges = useRef<ChargesCache | null>(null);
  const load = useCallback(async (force = false) => {
    setBusy(true);
    try {
      setSnaps(await loadAll(charges, force));
      setAt(new Date().toLocaleTimeString("en-IN", { hour12: false }));
    } finally { setBusy(false); }
  }, []);
  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const t = snaps ? totals(snaps) : null;
  return (
    <div className="space-y-3">
      <Card title="All services" right={<><span className="text-[11px] text-mist">{at ? `updated ${at}` : ""}</span><Btn small onClick={() => void load(true)} disabled={busy}>{busy ? "Refreshing…" : "Refresh"}</Btn></>}>
        {!snaps ? <p className="text-mist">Loading…</p> : (
          <div className="space-y-2">
            <Msg kind={t && t.live.length ? "warn" : "info"}>{headline(snaps)}</Msg>
            <div className="flex flex-wrap items-baseline gap-4">
              <div><p className="text-[11px] uppercase tracking-widest text-mist">Real money today</p><p className="text-[22px] font-bold">{rupees(t?.real)}</p></div>
              {t?.simulated != null && <div><p className="text-[11px] uppercase tracking-widest text-mist">Simulated (DEMO) today</p><p className="text-[22px] font-bold text-mist">{rupees(t.simulated)}</p></div>}
            </div>
            <p className="text-[11px] text-mist">Each figure says whether it is net (after charges) or gross. Real Trade and Position Stocks share one Dhan account, so this is the sum of their two books, not a broker statement. Status refreshes every 20 seconds, the charges behind the net figures every 60 seconds (the Refresh button asks for everything now). This page is read-only: it changes no switch and places no order.</p>
          </div>
        )}
      </Card>
      {snaps && <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">{snaps.map((s) => <SvcCard key={s.id} s={s} />)}</div>}
    </div>
  );
}
