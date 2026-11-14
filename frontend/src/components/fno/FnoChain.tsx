// F&O Options > Chain (E539, redesign round 6): the live option chain as a table. CE on the left, PE on the right, the strike in the middle, the ATM row marked
// (and scrolled into view), open interest drawn as a bar behind the number. Same call and same 8 s poll as before; the bias card is unchanged. Nothing here places an order.
import { useEffect, useMemo, useRef, useState } from "react";
import { fnoApi } from "../../fnoApi";
import { pcrReading } from "../../fnoView";
import { atmIndex, chainN0, chainN2, chainRows, type ChainRow } from "../../fnoChainView";
import { Msg } from "../calibration/ui";
import { Card, CardHeader, DataTable, Stat, Stats, StatusPill, Tabs, type Column } from "../kit";
import { useFno } from "./useFno";

const UNDS = [{ id: "NIFTY" as const, label: "Nifty" }, { id: "SENSEX" as const, label: "Sensex" }];

/** a number with its open-interest bar behind it: --oi is the bar width, side says which edge it grows from (always from the strike side) */
function OiCell({ v, pct, side }: { v: number | null | undefined; pct: number; side: "ce" | "pe" }) {
  return <span className={`oc-oi ${side}`} style={{ ["--oi" as string]: `${pct}%` }}><span className="oc-oi-n">{chainN0(v)}</span></span>;
}

const COLS: Column<ChainRow>[] = [
  { key: "ceoi", header: "CE OI", align: "right", render: (r) => <OiCell v={r.ce?.oi} pct={r.ceOiPct} side="ce" />, cellClass: (r) => (r.ceItm ? "oc-itm" : "") },
  { key: "cevol", header: "CE Vol", align: "right", render: (r) => chainN0(r.ce?.volume), cellClass: (r) => (r.ceItm ? "oc-itm" : "") },
  { key: "ceiv", header: "CE IV", align: "right", render: (r) => chainN2(r.ce?.iv), cellClass: (r) => (r.ceItm ? "oc-itm" : "") },
  { key: "cedl", header: "CE Delta", align: "right", render: (r) => chainN2(r.ce?.delta), cellClass: (r) => (r.ceItm ? "oc-itm" : "") },
  { key: "celtp", header: "CE LTP", align: "right", render: (r) => <b>{chainN2(r.ce?.ltp)}</b>, cellClass: (r) => (r.ceItm ? "oc-itm" : "") },
  { key: "strike", header: "Strike", align: "right", render: (r) => <span className="oc-strike">{r.strike}{r.atm && <em className="oc-atm">ATM</em>}</span>, cellClass: () => "oc-mid" },
  { key: "peltp", header: "PE LTP", align: "right", render: (r) => <b>{chainN2(r.pe?.ltp)}</b>, cellClass: (r) => (r.peItm ? "oc-itm" : "") },
  { key: "pedl", header: "PE Delta", align: "right", render: (r) => chainN2(r.pe?.delta), cellClass: (r) => (r.peItm ? "oc-itm" : "") },
  { key: "peiv", header: "PE IV", align: "right", render: (r) => chainN2(r.pe?.iv), cellClass: (r) => (r.peItm ? "oc-itm" : "") },
  { key: "pevol", header: "PE Vol", align: "right", render: (r) => chainN0(r.pe?.volume), cellClass: (r) => (r.peItm ? "oc-itm" : "") },
  { key: "peoi", header: "PE OI", align: "right", render: (r) => <OiCell v={r.pe?.oi} pct={r.peOiPct} side="pe" />, cellClass: (r) => (r.peItm ? "oc-itm" : "") },
];

export default function FnoChain() {
  const [und, setUnd] = useState<"NIFTY" | "SENSEX">("NIFTY");
  const [expiry, setExpiry] = useState("");
  const c = useFno(() => fnoApi.chain(und, expiry), 8000, [und, expiry]);
  const d = c.data;
  const spot = d?.chain.spot ?? 0;
  const rows = useMemo(() => (d ? chainRows(d.chain.strikes, spot, und) : []), [d, spot, und]);
  const s = d?.summary || {};
  const wrap = useRef<HTMLDivElement>(null);
  const placed = useRef("");
  // pin the ATM row: scroll the table's own box (not the page) so the ATM row is in the middle, once per index / expiry (not on every 8 s refresh, which would undo your scrolling)
  useEffect(() => {
    const key = `${und}|${d?.chain.expiry || ""}`;
    const box = wrap.current;
    const i = atmIndex(rows);
    if (!box || i < 0 || placed.current === key) return;
    const tr = box.querySelector<HTMLElement>("tr.oc-row-atm");
    if (!tr) return;
    box.scrollTop = Math.max(0, tr.offsetTop - box.clientHeight / 2 + tr.offsetHeight / 2);
    placed.current = key;
  }, [rows, und, d?.chain.expiry]);
  const dir = d?.bias.direction;
  return (
    <div className="space-y-3">
      <Tabs items={UNDS} value={und} onChange={(v) => { setUnd(v); setExpiry(""); }} label="Index" />
      {c.error && <Msg kind="err">{c.error}</Msg>}
      <Card>
        <CardHeader title={`${und} chain`} sub={d ? `Expiry ${d.chain.expiry}` : undefined}
          action={<span className="oc-tools">
            <select value={expiry || d?.chain.expiry || ""} onChange={(e) => setExpiry(e.target.value)} aria-label="Expiry" className="oc-select">
              {(d?.expiries || []).map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            {d && <StatusPill state={d.age_s > 30 ? "paused" : "live"}>{Math.round(d.age_s)}s old</StatusPill>}
          </span>} />
        {d ? (
          <>
            <Stats>
              <Stat size="sm" label="Spot" value={chainN2(spot)} />
              <Stat size="sm" label="PCR (OI)" value={chainN2(s.pcr_oi)} sub={pcrReading(s.pcr_oi)} />
              <Stat size="sm" label="Max pain" value={chainN0(s.max_pain)} />
              <Stat size="sm" label="Call wall" value={chainN0(s.call_wall)} sub="most call OI" />
              <Stat size="sm" label="Put wall" value={chainN0(s.put_wall)} sub="most put OI" />
            </Stats>
            <div className="oc-table" style={{ ["--k-table-max" as string]: "30rem" }}>
              <DataTable dense columns={COLS} rows={rows} rowKey={(r) => String(r.strike)} rowClass={(r) => (r.atm ? "oc-row-atm" : "")} wrapRef={wrap}
                caption="Option chain around the spot price" empty={<p className="text-mist">No strikes near the money.</p>} />
            </div>
            <p className="oc-legend">
              <span><i className="oc-sw atm" aria-hidden="true" /> ATM = the strike nearest the spot price</span>
              <span><i className="oc-sw itm" aria-hidden="true" /> shaded cells = in the money (calls below the spot, puts above it)</span>
              <span><i className="oc-sw bar" aria-hidden="true" /> bar behind OI = open interest relative to the biggest strike shown</span>
            </p>
          </>
        ) : <p className="text-mist">{c.loading ? "Loading…" : "No chain."}</p>}
      </Card>
      {d && (
        <Card>
          <CardHeader title="What the signal sees" action={<StatusPill state={dir === "UP" ? "live" : dir === "DOWN" ? "offline" : "closed"}>{dir === "UP" ? "▲ UP (CE)" : dir === "DOWN" ? "▼ DOWN (PE)" : "FLAT (no trade)"} · score {Math.round(d.bias.score)}</StatusPill>} />
          <ul className="list-disc space-y-0.5 pl-4">{(d.bias.reasons || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
          {!d.bias.reasons?.length && <p className="text-mist">No reasons reported.</p>}
        </Card>
      )}
    </div>
  );
}
