// F&O Options > End of day (round 554, E579, Tredye plan P5): OI Flow (long build-up, short covering, short build-up, long unwinding across the F&O stocks) and futures
// basis (premium / discount, annualised carry, next-month rollover share), from the exchange's F&O file for the last close. READ ONLY and DISPLAY ONLY: it reads the
// gateway's /market/fo-eod; it feeds no trading decision, switch or engine setting. Words and rules live in foEodView.ts (foEodView.test.mjs): every screen says which
// close it is, a missing number is a dash, an old pack says it is old.
import { useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { setVisibleInterval } from "../../visibleInterval";
import {
  BUILDUP_LIST, basisList, basisText, buildupLabel, countLine, expiryText, flowList, intText, packHead, pct1Text, plainPct, priceText, rolloverList, signedIntText,
  type FoEodBody, type FoRow,
} from "../../foEodView";
import { pctText } from "../../globalCuesView";
import { Button, Card, CardHeader, DataTable, SkeletonLines, StatusPill, Tabs, type Column } from "../kit";

type View = "flow" | "basis" | "roll";
const VIEWS: { id: View; label: string }[] = [{ id: "flow", label: "OI flow" }, { id: "basis", label: "Basis" }, { id: "roll", label: "Rollover" }];

const sym = (r: FoRow) => <b>{r.symbol}</b>;
const FLOW_COLS: Column<FoRow>[] = [
  { key: "s", header: "Stock", render: sym },
  { key: "px", header: "Future %", align: "right", render: (r) => pctText(r.price_chg_pct) },
  { key: "oic", header: "OI change %", align: "right", render: (r) => pctText(r.oi_chg_pct) },
  { key: "oi", header: "Open interest", align: "right", render: (r) => intText(r.oi) },
  { key: "chg", header: "OI change", align: "right", render: (r) => signedIntText(r.oi_chg) },
];
const BASIS_COLS: Column<FoRow>[] = [
  { key: "s", header: "Stock", render: sym },
  { key: "fut", header: "Future", align: "right", render: (r) => priceText(r.fut_close) },
  { key: "spot", header: "Cash", align: "right", render: (r) => priceText(r.spot) },
  { key: "b", header: "Basis", align: "right", render: (r) => basisText(r.basis_pct) },
  { key: "a", header: "Annualised", align: "right", render: (r) => pct1Text(r.annualised_pct) },
  { key: "e", header: "Expiry", align: "right", render: (r) => expiryText(r.near_expiry, r.days_to_expiry) },
];
const ROLL_COLS: Column<FoRow>[] = [
  { key: "s", header: "Stock", render: sym },
  { key: "r", header: "Next-month share of OI", align: "right", render: (r) => plainPct(r.roll_pct) },
  { key: "e", header: "Near expiry", align: "right", render: (r) => expiryText(r.near_expiry, r.days_to_expiry) },
  { key: "f", header: "State", align: "right", render: (r) => (r.rolling == null ? "—" : r.rolling ? "Rolling now" : "Not yet") },
  { key: "oi", header: "Open interest", align: "right", render: (r) => intText(r.oi) },
];

export default function FnoEod() {
  const [data, setData] = useState<FoEodBody | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("flow");

  const load = async (manual = false) => {
    if (manual) setBusy(true);
    try {
      setData(await api.foEod());                 // only a good answer replaces what is on screen
      setFailed(false);
    } catch {
      setFailed(true);                              // the last good read stays, and the line under the title says the refresh failed
    } finally {
      setLoading(false);
      setBusy(false);
    }
  };
  useEffect(() => {
    void load();
    return setVisibleInterval(() => load(), 600000);   // 10 minutes, paused while the tab is hidden. The file changes once a day; this just picks up the new one
  }, []);

  const head = packHead(data, failed);
  const rows = data?.rows;
  const flows = useMemo(() => BUILDUP_LIST.map((b) => ({ def: b, ...flowList(rows, b.id) })), [rows]);
  const prem = useMemo(() => basisList(rows, "premium"), [rows]);
  const disc = useMemo(() => basisList(rows, "discount"), [rows]);
  const roll = useMemo(() => rolloverList(rows), [rows]);

  if (loading && !data) return <Card role="status" aria-label="Loading end-of-day F&O"><SkeletonLines rows={3} /></Card>;

  const none = <p className="text-mist">Nothing to show for this close.</p>;
  return (
    <div className="space-y-3" data-testid="fno-eod">
      <Card>
        <CardHeader title={head.title} sub={head.sub}
          action={<>
            {head.tone === "stale" && <StatusPill state="paused" title="The newest file is not out yet; this is the previous close">Previous close</StatusPill>}
            {(failed || head.tone === "none") && <StatusPill state="offline" title="No current data">No data</StatusPill>}
            <Button variant="ghost" size="sm" onClick={() => void load(true)} loading={busy} aria-label="Refresh end-of-day F&O">Refresh</Button>
          </>} />
        <p className="text-mist">{countLine(data?.counts)}</p>
        <Tabs items={VIEWS} value={view} onChange={setView} label="End-of-day view" />
      </Card>

      {view === "flow" && (
        <div className="grid gap-3 md:grid-cols-2">
          {flows.map(({ def, rows: list, total }) => (
            <Card key={def.id}>
              <CardHeader title={def.label} sub={`${def.read}. ${total ? `Top ${list.length} of ${total} by OI change %` : "None this close"}`} />
              <DataTable dense columns={FLOW_COLS} rows={list} rowKey={(r) => r.symbol} caption={`${def.label}, ${head.showDate || "no date"}`} empty={none} />
            </Card>
          ))}
        </div>
      )}

      {view === "basis" && (
        <div className="grid gap-3 md:grid-cols-2">
          <Card>
            <CardHeader title="Biggest premium" sub={`Future above the cash price. ${prem.total ? `Top ${prem.rows.length} of ${prem.total}` : "None this close"}`} />
            <DataTable dense columns={BASIS_COLS} rows={prem.rows} rowKey={(r) => r.symbol} caption={`Premium, ${head.showDate || "no date"}`} empty={none} />
          </Card>
          <Card>
            <CardHeader title="Biggest discount" sub={`Future below the cash price. ${disc.total ? `Top ${disc.rows.length} of ${disc.total}` : "None this close"}`} />
            <DataTable dense columns={BASIS_COLS} rows={disc.rows} rowKey={(r) => r.symbol} caption={`Discount, ${head.showDate || "no date"}`} empty={none} />
          </Card>
          <p className="text-mist md:col-span-2">
            Basis = nearest-month future close against the cash close in the same file. Annualised = basis x 365 / days to expiry (a dash on expiry day); inside the last
            few days it swings a lot, so read the plain basis there. A stock with no cash price in the file shows a dash.
          </p>
        </div>
      )}

      {view === "roll" && (
        <Card>
          <CardHeader title="Rollover" sub={`Share of open interest already in the next month, out of the first three. ${roll.total ? `Top ${roll.rows.length} of ${roll.total}, rolling stocks first` : "None this close"}`} />
          <DataTable dense columns={ROLL_COLS} rows={roll.rows} rowKey={(r) => r.symbol} caption={`Rollover, ${head.showDate || "no date"}`} empty={none} />
        </Card>
      )}

      <p className="text-mist">
        For reading only. Open interest is summed over all listed months and the price is the nearest month's own change, so a roll between months is not read as a build-up
        or an unwinding. {BUILDUP_LIST.map((b) => buildupLabel(b.id)).join(", ")} describe what happened on that day, not what will happen next.
      </p>
    </div>
  );
}
