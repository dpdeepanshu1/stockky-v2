import type { AuditLogRow } from "../../../realTradeApi";
import { fmtTime } from "../format";
import { SectionHdr } from "../parts";
import type { Mode } from "../types";

// E64 - E31 step 8: the "log" (Activity) tab, moved out of RealAutoTrade.tsx (JSX unchanged).
export interface LogTabProps {
  mode: Mode;
  auditRows: AuditLogRow[];
  loadAudit: () => Promise<void>;
}

export function LogTab({ mode, auditRows, loadAudit }: LogTabProps) {
  return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Recent activity — {mode}</SectionHdr>
                <button onClick={() => void loadAudit()}
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                  ↻ Refresh
                </button>
              </div>
              <div className="bg-graphite border border-slate rounded-2xl overflow-hidden">
                {auditRows.length === 0 ? (
                  <p className="font-display tabular-nums text-[11px] text-mist p-4">No activity yet — click Refresh.</p>
                ) : (
                  <div className="divide-y divide-slate">
                    {auditRows.map((r, i) => {
                      const isPositive = ["ARMED", "ADMIN_LOGIN", "DHAN_CONNECTED", "MANUAL_CLOSE", "RISK_CONFIG_CONFIRMED"].includes(r.action);
                      const isNegative = ["DISARMED", "ADMIN_LOGOUT", "MANUAL_CANCEL"].includes(r.action);
                      return (
                        <div key={i} className="flex items-start justify-between gap-3 px-4 py-2 hover:bg-ink">
                          <div className="flex items-start gap-2 min-w-0">
                            <span className={`flex-shrink-0 mt-0.5 ${isPositive ? "text-signal-buy" : isNegative ? "text-signal-sell" : "text-mist"}`}>●</span>
                            <div className="min-w-0">
                              <span className="font-display tabular-nums text-[11px] text-paper font-bold">{r.action}</span>
                              {r.detail && <span className="font-display tabular-nums text-[10px] text-mist ml-2 truncate">{r.detail}</span>}
                            </div>
                          </div>
                          <span className="font-display tabular-nums text-[10px] text-mist whitespace-nowrap flex-shrink-0">{fmtTime(r.occurred_at)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
  );
}
