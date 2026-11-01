// frontend/src/liveStateView.ts  (E285 round 87)
// A readable card for GET /live/state of the two trading services (E283), shown above the raw JSON in the Calibration tab's probes panel.
// Pure: no runtime imports, no network, never throws. A part the service could not read ({ok:false,error}) becomes one red row; a missing or odd value becomes "-".
// liveStateView.test.mjs pins the shapes against the two services' live_state.py field names.

export type Tone = "good" | "bad" | "warn" | undefined;
export interface Row { label: string; value: string; tone?: Tone }
export interface Block { id: string; title: string; ok: boolean; rows: Row[] }
type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const numOrNull = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Whole rupees with Indian grouping, "-" when the value is not a finite number. */
export function rupees(v: unknown): string {
  const n = numOrNull(v);
  return n === null ? "-" : `₹${Math.round(n).toLocaleString("en-IN")}`;
}
/** A plain count / number, at most 2 decimals; "-" when not a finite number. */
export function plain(v: unknown): string {
  const n = numOrNull(v);
  return n === null ? "-" : String(Math.round(n * 100) / 100);
}
const yesNo = (v: unknown): string => (v === true ? "on" : v === false ? "off" : "-");

function failed(id: string, title: string, part: unknown): Block | null {
  if (!isObj(part)) return { id, title, ok: false, rows: [{ label: "part", value: "missing from the answer", tone: "bad" }] };
  if (part.ok === false) return { id, title, ok: false, rows: [{ label: "error", value: String(part.error ?? "unreadable").slice(0, 140), tone: "bad" }] };
  return null;
}

function orderWindow(id: string, title: string, p: Obj): Block {
  const rows: Row[] = [{ label: "shared window", value: yesNo(p.enabled), tone: p.enabled === false ? "warn" : undefined }];
  if (p.enabled !== false) {
    rows.push({ label: "file usable", value: yesNo(p.working), tone: p.working === true ? "good" : "bad" });
    rows.push({ label: "calls in the last second (both services)", value: plain(p.calls_last_s) });
    if (isObj(p.by_service_last_s)) for (const [k, n] of Object.entries(p.by_service_last_s)) rows.push({ label: `  ${k === "ps" ? "Position Stocks" : k === "rt" ? "Real Trade" : k}`, value: plain(n) });
    if (typeof p.last_error === "string" && p.last_error) rows.push({ label: "last error", value: p.last_error.slice(0, 100), tone: "warn" });
  }
  return { id, title, ok: true, rows };
}

function heatRows(p: Obj): Row[] {
  const rows: Row[] = [{ label: "shared heat limit", value: p.enabled === false ? "off" : `${plain(p.limit_pct)} % of the pool` }];
  if (numOrNull(p.own_open_risk) !== null) rows.push({ label: "this service: open risk + gap allowance", value: `${rupees(p.own_open_risk)} + ${rupees(p.own_gap_allowance)}` });
  rows.push({ label: "other service: open risk", value: rupees(p.peer_open_risk) });
  rows.push({ label: "other service: capital base", value: rupees(p.peer_capital_base) });
  if (numOrNull(p.peer_age_s) !== null) rows.push({ label: "other service: row age", value: `${plain(p.peer_age_s)} s`, tone: numOrNull(p.peer_max_age_s) !== null && (p.peer_age_s as number) > (p.peer_max_age_s as number) ? "warn" : undefined });
  return rows;
}

function stageText(n: number): string { return `stage ${n}`; }

/** The blocks of a Real Trade (svc "rt") or Position Stocks (svc "ps") /live/state answer. Never throws. */
export function liveStateBlocks(svc: "rt" | "ps", value: unknown): Block[] {
  try {
    if (!isObj(value)) return [{ id: "all", title: "Answer", ok: false, rows: [{ label: "answer", value: "not a JSON object", tone: "bad" }] }];
    const out: Block[] = [];
    if (svc === "rt") {
      const pool = failed("pool", "Pool", value.pool);
      if (pool) out.push(pool);
      else {
        const accts = Array.isArray((value.pool as Obj).accounts) ? ((value.pool as Obj).accounts as unknown[]) : [];
        const rows: Row[] = [];
        for (const a of accts) if (isObj(a)) {
          const m = String(a.mode ?? "?");
          rows.push({ label: `${m}: starting capital`, value: rupees(a.starting_capital) }, { label: `${m}: equity`, value: rupees(a.current_equity) },
                    { label: `${m}: cash`, value: rupees(a.cash_available) }, { label: `${m}: broker cash`, value: rupees(a.broker_cash_available) });
        }
        out.push({ id: "pool", title: "Pool", ok: true, rows: rows.length ? rows : [{ label: "accounts", value: "none", tone: "warn" }] });
      }
      const heat = failed("heat", "Heat", value.heat);
      out.push(heat ?? { id: "heat", title: "Heat", ok: true, rows: heatRows(value.heat as Obj) });
      const st = failed("stamps", "Newest entries by setup", value.setup_stamp);
      if (st) out.push(st);
      else {
        const p = value.setup_stamp as Obj;
        const rows: Row[] = [{ label: "entries read", value: plain(p.n) }, { label: "stamped with a setup", value: plain(p.stamped) },
                             { label: "no stamp", value: plain(p.unstamped), tone: numOrNull(p.unstamped) ? "warn" : undefined }];
        if (isObj(p.by_setup)) for (const [k, n] of Object.entries(p.by_setup).sort((a, b) => Number(b[1]) - Number(a[1]))) rows.push({ label: `  ${k}`, value: plain(n) });
        if (isObj(p.by_slot)) rows.push({ label: "by time slot", value: Object.entries(p.by_slot).map(([k, n]) => `${k}: ${plain(n)}`).join(", ") || "-" });
        out.push({ id: "stamps", title: "Newest entries by setup", ok: true, rows });
      }
      if ("order_rate" in value) {
        const o = failed("order_rate", "Dhan order window", value.order_rate);
        out.push(o ?? orderWindow("order_rate", "Dhan order window", value.order_rate as Obj));
      }
      return out;
    }
    const orp = failed("order_rate", "Dhan order calls", value.order_rate);
    if (orp) out.push(orp);
    else {
      const p = value.order_rate as Obj;
      const rows: Row[] = [
        { label: "calls counted (since restart)", value: plain(p.calls) },
        { label: "budget a second (this service)", value: plain(p.budget_per_s) },
        { label: "modifies that waited", value: plain(p.waits), tone: numOrNull(p.waits) ? "warn" : undefined },
        { label: "longest wait", value: `${plain(p.max_wait_s)} s` },
        { label: "let through after the 2 s cap", value: plain(p.forced), tone: numOrNull(p.forced) ? "bad" : undefined },
        { label: "busiest second", value: plain(p.peak_in_window) },
        { label: "limit on", value: yesNo(p.enabled) },
      ];
      if ("shared_enabled" in p) {
        rows.push({ label: "account-wide count on", value: yesNo(p.shared_enabled), tone: p.shared_enabled === false ? "warn" : undefined },
                  { label: "account budget a second (both services)", value: plain(p.account_per_s) },
                  { label: "waits caused by the other service", value: plain(p.account_waits), tone: numOrNull(p.account_waits) ? "warn" : undefined },
                  { label: "calls written to the shared window", value: plain(p.shared_recorded) },
                  { label: "calls the shared window could not take", value: plain(p.shared_unusable), tone: numOrNull(p.shared_unusable) ? "warn" : undefined });
      }
      if (isObj(p.shared)) out.push(orderWindow("shared_window", "Dhan order window", { enabled: true, ...p.shared }));
      out.unshift({ id: "order_rate", title: "Dhan order calls", ok: true, rows });
    }
    const mom = failed("momentum", "Momentum exit", value.momentum);
    if (mom) out.push(mom);
    else {
      const p = value.momentum as Obj;
      const rows: Row[] = [{ label: "mode", value: String(p.mode ?? "-"), tone: p.mode === "live" ? "good" : p.mode === "off" ? "warn" : undefined }];
      for (const [k, n] of Object.entries(p)) if (k !== "ok" && k !== "mode" && numOrNull(n) !== null && rows.length < 9) rows.push({ label: k.replace(/_/g, " "), value: plain(n) });
      out.push({ id: "momentum", title: "Momentum exit", ok: true, rows });
    }
    const heat = failed("shared_heat", "Heat (the other service)", value.shared_heat);
    out.push(heat ?? { id: "shared_heat", title: "Heat (the other service)", ok: true, rows: heatRows(value.shared_heat as Obj) });
    const lad = failed("ladder", "Risk ladder", value.ladder);
    if (lad) out.push(lad);
    else {
      const p = value.ladder as Obj;
      const rows: Row[] = [{ label: "ladder", value: yesNo(p.enabled), tone: p.enabled === false ? "warn" : undefined }, { label: "short cap", value: plain(p.short_cap) }];
      if (isObj(p.stages)) for (const [k, n] of Object.entries(p.stages).sort((a, b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]))) {
        const s = numOrNull(n);
        rows.push({ label: `  ${k.replace("|", " ")}`, value: s === null ? "-" : stageText(s), tone: s !== null && s > 0 ? "good" : undefined });
      }
      out.push({ id: "ladder", title: "Risk ladder", ok: true, rows });
    }
    return out;
  } catch {
    return [{ id: "all", title: "Answer", ok: false, rows: [{ label: "answer", value: "could not be read", tone: "bad" }] }];
  }
}
