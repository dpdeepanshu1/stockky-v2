// frontend/src/components/ui/MoneyModeBanner.tsx  (design v5 - one shared "where is my money" banner for Trades, Real Auto Trade, Position Stocks)
// Presentation only: the mode and the safety chips are passed in from state the page already has. It never changes mode and never shows a money amount.
// The mode is always spelled out in words and every chip carries a symbol (✓ / ⚠ / ✕), so nothing relies on colour alone.
export type MoneyMode = "PAPER" | "DEMO" | "REAL";
export type SafetyChip = { label: string; state: "ok" | "warn" | "bad" };

const COPY: Record<MoneyMode, { title: string; line: string }> = {
  PAPER: { title: "PAPER TRADING", line: "Virtual cash only. Nothing here touches your broker." },
  DEMO: { title: "DEMO MODE", line: "Simulated orders. No real money is used." },
  REAL: { title: "REAL MONEY", line: "Orders go to your live Dhan account. Check before you act." },
};
const SYMBOL = { ok: "✓", warn: "⚠", bad: "✕" } as const;

export function MoneyModeBanner({ mode, detail, chips = [] }: { mode: MoneyMode; detail?: string; chips?: SafetyChip[] }) {
  const c = COPY[mode];
  return (
    <div className="v5-money" data-money={mode} role="status" aria-label={`${c.title}. ${detail || c.line}`}>
      <span className="v5-money-icon" aria-hidden="true">
        {mode === "REAL" ? (
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6l8-3z" /><path d="M12 8v5" /><path d="M12 16h.01" /></svg>
        ) : (
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M9 10c0-1 1-2 3-2s3 1 3 2-1 1.6-3 2-3 1-3 2 1 2 3 2 3-1 3-2" /></svg>
        )}
        {mode === "REAL" && <i className="v5-money-ping" />}
      </span>
      <div className="v5-money-text">
        <b className="v5-money-title">{c.title}</b>
        <span className="v5-money-line">{detail || c.line}</span>
      </div>
      {chips.length > 0 && (
        <ul className="v5-money-chips">
          {chips.map((x) => (
            <li key={x.label} className={`v5-chip ${x.state}`}><span aria-hidden="true">{SYMBOL[x.state]}</span> {x.label}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
