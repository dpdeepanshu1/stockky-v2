// frontend/src/components/PositionStocksTab.tsx
//
// Session 9 — full dashboard rebuild: capital utilisation bar, circuit breaker
// status, WS health details, kill-switch badge, orders-placed counter, P&L
// history chart labels. All data comes from position-stocks-service only.
// E538 (redesign round 5b): the page shell only. State lives in positionStocks/usePositionStocks.ts, each sub-tab in positionStocks/Ps*Tab.tsx.

import { SessionMissingNote } from "./AdminGate";
import { logoutEverywhere } from "../adminAuth";
import { MoneyModeBanner } from "./ui/MoneyModeBanner";
import { PageHero } from "./ui/PageHero";
import { KpiStrip, type Kpi } from "./ui/KpiStrip";
import { AiPulse, type PulseChip } from "./ui/AiPulse";
import { signedInr } from "./realAuto/history";
import { getPositionStocksApiUrl } from "../positionStocksApi";
import { TradingHeader } from "./trading/TradingHeader";
import { GroupTabs } from "./trading/GroupTabs";
import { PS_GROUPS } from "./trading/tradingGroups";
import SessionRail from "./session/SessionRail";
import { usePositionStocks, type SubTab } from "./positionStocks/usePositionStocks";
import { PsBoardTab } from "./positionStocks/PsBoardTab";
import { PsTimeMachineTab } from "./positionStocks/PsTimeMachineTab";
import { PsOverviewTab } from "./positionStocks/PsOverviewTab";
import { PsPipelineTab } from "./positionStocks/PsPipelineTab";
import { PsScreenerTab } from "./positionStocks/PsScreenerTab";
import { PsPositionsTab } from "./positionStocks/PsPositionsTab";
import { PsHistoryTab } from "./positionStocks/PsHistoryTab";
import { PsDhanOrdersTab } from "./positionStocks/PsDhanOrdersTab";
import { PsChargesTab } from "./positionStocks/PsChargesTab";
import { PsSettingsTab } from "./positionStocks/PsSettingsTab";
import { Button } from "./kit";   // round 537

export default function PositionStocksTab() {
  const s = usePositionStocks();
  const { apiUrlInput, setApiUrlInput, status, positions, ledger, busy, error, lastRefreshed, subTab, setSubTab, loggedIn, saveApiUrl, doAction, openPositions, closedToday, killSwitchTripped } = s;
  if (!getPositionStocksApiUrl()) {
    return (
      <div className="p-4 max-w-lg mx-auto">
        <p className="font-display tabular-nums text-xs text-mist mb-1 uppercase tracking-widest">Position Stocks Service URL</p>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">Paste your position-stocks-service URL (port 8006).</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-graphite border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
            placeholder="https://stockky-position-stocks.onrender.com"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <Button variant="primary" onClick={saveApiUrl}>Save</Button>
        </div>
      </div>
    );
  }


  return (
    <div className="page-terminal hx-page hx-flow space-y-4 mode-frame" data-mode="REAL" data-frame-label="REAL MONEY · live Dhan scalp orders">
      <PageHero glow={killSwitchTripped ? "halt" : status?.armed ? "armed" : undefined} icon="trend" title="Position Stocks" kicker="AI-assisted · live Dhan scalps" subtitle="1m / 5m / 15m / 60m scalp pool"
        right={(
          <span className="ra-updated font-display tabular-nums">
            {lastRefreshed ? `Updated ${lastRefreshed.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : "Refreshing…"}
          </span>
        )} />

      {/* design v5: money mode + safety chips from state this page already holds */}
      <MoneyModeBanner mode="REAL" detail="Live Dhan scalp orders. Orders here use real money." chips={[
        ...(status != null ? [{ label: status?.risk_confirmed ? "Risk setting confirmed" : "Risk setting not confirmed", state: (status?.risk_confirmed ? "ok" : "warn") as "ok" | "warn" }] : []),
        ...(status != null || ledger != null ? [{ label: killSwitchTripped ? "Loss kill switch TRIPPED" : "Loss kill switch ready", state: (killSwitchTripped ? "bad" : "ok") as "ok" | "bad" }] : []),
      ]} />

      {/* E537: status first - REAL mode, armed, today's realised P&L, capital in use / idle, and a pause that only disarms (the existing disarm action) */}
      {(status != null || ledger != null) && (
        <TradingHeader
          input={{ mode: "REAL", armed: status ? status.armed : null, killed: !!killSwitchTripped, pnl: ledger?.realized_pnl_today, total: ledger?.total_allocated_capital, idle: ledger?.available_capital }}
          onPause={() => doAction("disarm")} pauseLabel="Pause (disarm)" pauseConfirm="Click again to disarm" busy={busy === "disarm"} />
      )}

      {/* design v6: engine strip + summary tiles, only from state this page already holds (shown once the service has answered) */}
      {status != null && (
        <>
          <AiPulse title="Scalp engine" working={!!status.armed && !killSwitchTripped}
            line={killSwitchTripped ? "Stopped by the daily loss kill switch" : status.armed ? "Armed · watching the 1m / 5m / 15m / 60m pool for entries" : "Not armed · no new orders will be placed"}
            chips={[
              { label: status.armed ? "Armed" : "Not armed", state: status.armed ? "warn" : "idle" },
              { label: status.auto_pilot_enabled ? "Auto-pilot on" : "Auto-pilot off", state: status.auto_pilot_enabled ? "ok" : "idle" },
              { label: status.risk_confirmed ? "Risk confirmed" : "Risk not confirmed", state: status.risk_confirmed ? "ok" : "warn" },
            ] as PulseChip[]} />
          <KpiStrip label="Position Stocks summary" items={[
            { label: "Open positions", value: openPositions.length, sub: openPositions.length ? "held right now" : "nothing held", tone: "info" },
            { label: "Closed today", value: closedToday.length, sub: "finished trades", tone: "flat" },
            ...(ledger != null ? [{ label: "Realised today", value: signedInr(ledger.realized_pnl_today), sub: "from the capital ledger", tone: (Math.abs(ledger.realized_pnl_today) < 0.5 ? "flat" : ledger.realized_pnl_today > 0 ? "up" : "down") as Kpi["tone"] }] : []),
            { label: "Orders today", value: status.orders_placed_today_budget ? `${status.orders_placed_today} / ${status.orders_placed_today_budget}` : status.orders_placed_today, sub: status.orders_placed_today_budget ? "used of the daily budget" : "placed so far", tone: status.orders_placed_today_budget && status.orders_placed_today >= status.orders_placed_today_budget ? "warn" : "flat" },
          ]} />
        </>
      )}

      {error && (
        <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 font-display tabular-nums text-xs text-signal-sell">
          {error}
        </div>
      )}

      {!status?.risk_confirmed && status != null && (
        <div className="rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
          ⚠ RISK_PER_TRADE_PCT not yet confirmed — running on a placeholder value ({status?.risk_per_trade_pct ?? "—"}%). Set RISK_PER_TRADE_PCT_CONFIRMED=true in the service env.
        </div>
      )}

      {killSwitchTripped && (
        <div className="rounded-xl border border-signal-sell/60 bg-signal-sell/15 px-3 py-2 font-display tabular-nums text-xs text-signal-sell font-bold">
          🔴 DAILY LOSS KILL SWITCH TRIPPED — No new entries for the rest of the day.
        </div>
      )}

      {/* ── Admin auth ── */}
      {!loggedIn ? (
        <SessionMissingNote service="Position Stocks" />
      ) : (
        <div className="ra-session ra-session-ok">
          <span className="font-display tabular-nums">● Admin session active</span>
          <Button variant="ghost" size="sm" onClick={logoutEverywhere}>Log out</Button>
        </div>
      )}

      {/* ── Sub-tab nav ── */}
      {/* E277: where IST time stands against the plan's morning timeline (display only) */}
      <SessionRail service="ps" />

      <GroupTabs<SubTab>
        groups={PS_GROUPS}
        label="Position Stocks sections"
        value={subTab}
        onChange={setSubTab}
        items={[
          { id: "overview", label: "Overview" },
          { id: "board", label: "Board" },
          { id: "timemachine", label: "Time Machine" },
          { id: "pipeline", label: "Pipeline" },
          { id: "screener", label: "Screener" },
          { id: "positions", label: "Positions", count: openPositions.length },
          { id: "history", label: "Trade History" },
          { id: "dhanorders", label: "Dhan Live Orders" },
          { id: "charges", label: "Charges" },
          { id: "settings", label: "Settings" },
        ]}
      />

      {/* group306: Market Board (display only; places no order) */}
      {subTab === "board" && <PsBoardTab s={s} />}

      {subTab === "timemachine" && <PsTimeMachineTab s={s} />}

      {subTab === "overview" && <PsOverviewTab s={s} />}

      {subTab === "pipeline" && <PsPipelineTab s={s} />}

      {subTab === "screener" && <PsScreenerTab s={s} />}

      {subTab === "positions" && <PsPositionsTab s={s} />}

      {subTab === "history" && <PsHistoryTab s={s} />}

      {subTab === "dhanorders" && <PsDhanOrdersTab s={s} />}

      {subTab === "charges" && <PsChargesTab s={s} />}

      {subTab === "settings" && <PsSettingsTab s={s} />}
    </div>
  );
}
