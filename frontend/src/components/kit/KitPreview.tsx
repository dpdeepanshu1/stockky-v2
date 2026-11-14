// components/kit/KitPreview.tsx (E534) - every kit component on one page, light and dark. Open the app with ?kit=1 (main.tsx loads this page instead of the app).
// Sample numbers below are made up for the preview only; nothing here calls a service.
import { useState } from "react";
import { Button, Card, CardHeader, DataTable, Empty, ErrorState, Field, Sheet, SkeletonLines, Stat, Stats, StatusPill, Switch, Tabs, type Column, type PillState } from "./index";

type Row = { sym: string; ltp: number; chg: number; qty: number };
const ROWS: Row[] = [
  { sym: "SAMPLE1", ltp: 1284.5, chg: 1.82, qty: 12 },
  { sym: "SAMPLE2", ltp: 841.0, chg: -0.64, qty: 30 },
  { sym: "SAMPLE3", ltp: 219.35, chg: 0.0, qty: 100 },
];
const COLS: Column<Row>[] = [
  { key: "sym", header: "Symbol", render: (r) => <b>{r.sym}</b> },
  { key: "ltp", header: "LTP", align: "right", render: (r) => r.ltp.toFixed(2) },
  { key: "chg", header: "Change", align: "right", render: (r) => <span className={r.chg > 0 ? "k-up" : r.chg < 0 ? "k-down" : ""}>{r.chg > 0 ? "▲ +" : r.chg < 0 ? "▼ " : ""}{r.chg.toFixed(2)}%</span> },
  { key: "qty", header: "Qty", align: "right", render: (r) => r.qty },
];
const PILLS: PillState[] = ["live", "armed", "paused", "offline", "closed", "info"];

export default function KitPreview() {
  const [light, setLight] = useState(() => document.documentElement.classList.contains("light"));
  const [tab, setTab] = useState<"now" | "orders" | "history">("now");
  const [sw, setSw] = useState(true);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const toggle = () => { const n = !light; setLight(n); document.documentElement.classList.toggle("light", n); try { localStorage.setItem("stockky_theme", n ? "light" : "dark"); } catch { /* storage blocked */ } };
  return (
    <main style={{ maxWidth: 960, margin: "0 auto", padding: "24px 16px 64px", display: "grid", gap: 24 }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        <div><h1 style={{ margin: 0, fontSize: "1.5rem", fontWeight: 700 }}>Stockky component kit</h1><p className="k-card-sub">Redesign round E534. Sample data only.</p></div>
        <Button variant="ghost" size="sm" onClick={toggle}>{light ? "Dark theme" : "Light theme"}</Button>
      </header>

      <Card>
        <CardHeader title="Stat" sub="Label, big tabular number, signed delta" action={<StatusPill state="live" />} />
        <Stats>
          <Stat label="Today P&L" value="₹1,240.50" delta="+2.4%" tone="up" sub="vs capital in use" size="lg" />
          <Stat label="Open positions" value="3" sub="max 5" />
          <Stat label="Unrealised" value="-₹312.00" delta="-0.6%" tone="down" />
          <Stat label="Idle capital" value="₹14,000" delta="not deployed" tone="warn" />
        </Stats>
      </Card>

      <Card>
        <CardHeader title="StatusPill" sub="Colour and shape both carry the state" />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>{PILLS.map((p) => <StatusPill key={p} state={p} />)}</div>
      </Card>

      <Card flush>
        <CardHeader title="Tabs and DataTable" sub="Arrow keys, Home and End move between tabs" />
        <div style={{ padding: "0 16px" }}><Tabs label="Preview sections" value={tab} onChange={setTab} items={[{ id: "now", label: "Now", count: 3 }, { id: "orders", label: "Orders" }, { id: "history", label: "History" }]} /></div>
        <DataTable columns={COLS} rows={ROWS} rowKey={(r) => r.sym} onRowClick={() => setOpen(true)} caption={`Tab: ${tab}. Click a row to open the sheet.`} />
      </Card>

      <Card>
        <CardHeader title="Button, Field and Switch" />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 16 }}>
          <Button>Primary</Button><Button variant="soft">Soft</Button><Button variant="ghost">Ghost</Button>
          <Button variant="danger" confirmText="Tap again to confirm">Close all</Button><Button loading>Saving</Button><Button disabled>Disabled</Button>
        </div>
        <div style={{ display: "grid", gap: 12, maxWidth: 360 }}>
          <Field label="Symbol" placeholder="e.g. TCS" value={name} onChange={(e) => setName(e.target.value.toUpperCase())} hint="NSE symbol" />
          <Field label="Quantity" defaultValue="0" error="Enter a quantity of at least 1" />
        </div>
        <Switch label="Auto-arm at 09:20" description="Arms the engine after the opening checks pass" checked={sw} onChange={setSw} />
      </Card>

      <Card>
        <CardHeader title="Loading, empty and error" />
        <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
          <SkeletonLines rows={4} />
          <Empty title="No open positions" hint="Positions appear here once the engine enters a trade." />
          <ErrorState hint="The service did not answer in time." onRetry={() => undefined} />
        </div>
      </Card>

      <Sheet open={open} onClose={() => setOpen(false)} title="SAMPLE1" footer={<><Button variant="ghost" onClick={() => setOpen(false)}>Close</Button><Button>Analyse</Button></>}>
        <Stats><Stat label="LTP" value="1,284.50" delta="+1.82%" tone="up" /><Stat label="Qty" value="12" /></Stats>
      </Sheet>
    </main>
  );
}
