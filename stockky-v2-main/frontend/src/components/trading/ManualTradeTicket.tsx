import { useState } from "react";
import { realTradeApi, type ManualOrderRequest, type ManualOrderResult } from "../../realTradeApi";

type Mode = "DEMO" | "REAL";

function fmtInr(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

/**
 * Manual Trade Ticket — the "MANUAL" leg of Stockky Trade's
 * DEMO/MANUAL/AUTO split. Talks only to /manual-order/{mode}/preview and
 * /manual-order/{mode}/confirm (manual_engine.py) — never places an order
 * directly. Two-step by design: Review computes the same risk-engine
 * verdict a real order would get, without writing anything; Confirm is a
 * separate, explicit click (a second "CONFIRM BUY"/"CONFIRM SELL" for
 * REAL mode) that re-checks everything server-side before it actually
 * reaches Dhan/the DEMO simulator — nothing here trusts the Review
 * numbers by the time Confirm is clicked.
 */
export default function ManualTradeTicket({
  mode, armed, onOrderComplete,
}: { mode: Mode; armed: boolean; onOrderComplete: () => void }) {
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [productType, setProductType] = useState<"CNC" | "MIS">("CNC");
  const [limitPrice, setLimitPrice] = useState<string>("");
  const [stopPrice, setStopPrice] = useState<string>("");
  const [targetPrice, setTargetPrice] = useState<string>("");

  const [preview, setPreview] = useState<ManualOrderResult | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [showConfirmStep, setShowConfirmStep] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const buildRequest = (): ManualOrderRequest => ({
    symbol: symbol.trim().toUpperCase(),
    side,
    qty,
    order_type: orderType,
    limit_price: orderType === "LIMIT" && limitPrice ? Number(limitPrice) : null,
    product_type: productType,
    stop_price: side === "BUY" && stopPrice ? Number(stopPrice) : null,
    target_price: side === "BUY" && targetPrice ? Number(targetPrice) : null,
  });

  const doPreview = async () => {
    setErr(null);
    setResult(null);
    if (!symbol.trim() || qty <= 0) {
      setErr("Enter a symbol and a positive quantity.");
      return;
    }
    setPreviewing(true);
    try {
      const p = await realTradeApi.previewManualOrder(mode, buildRequest());
      setPreview(p);
      setShowConfirmStep(true);
    } catch (e: any) {
      setErr(e?.message || "Preview failed");
      setShowConfirmStep(false);
    } finally {
      setPreviewing(false);
    }
  };

  const doConfirm = async () => {
    setErr(null);
    setConfirming(true);
    try {
      const r = await realTradeApi.confirmManualOrder(mode, buildRequest());
      if (!r.ok) {
        setResult({ ok: false, text: r.detail || r.reason || "Order was rejected." });
      } else if (r.side === "BUY") {
        const statusText =
          r.status === "FILLED" ? `Filled ${r.approved_qty} @ ₹${r.entry_price}` :
          r.status === "SENT_TO_BROKER" ? `Sent to Dhan (order ${r.dhan_order_id})` :
          `Placed, ${orderType === "LIMIT" ? "waiting to fill" : "pending"}`;
        setResult({ ok: true, text: `BUY ${symbol.toUpperCase()} — ${statusText}` });
      } else {
        const statusText =
          r.status === "SENT_TO_BROKER" ? `Sent to Dhan for confirmation` :
          `Closed ${r.approved_qty} @ ₹${r.exit_price_estimate} (pnl ${fmtInr(r.pnl ?? r.estimated_pnl)})`;
        setResult({ ok: true, text: `SELL ${symbol.toUpperCase()} — ${statusText}` });
      }
      setShowConfirmStep(false);
      setPreview(null);
      onOrderComplete();
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || "Confirm failed" });
    } finally {
      setConfirming(false);
    }
  };

  const canReview = symbol.trim().length > 0 && qty > 0 && (mode === "DEMO" || armed || side === "SELL");
  const blockedByArm = mode === "REAL" && !armed && side === "BUY";

  return (
    <div className="border border-white/10 rounded-xl p-4 mb-4">
      <p className="font-display tabular-nums text-xs text-paper/70 mb-3">Manual Trade Ticket ({mode})</p>

      <div className="grid grid-cols-2 gap-2 mb-2">
        <input
          value={symbol}
          onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setShowConfirmStep(false); setPreview(null); }}
          placeholder="SYMBOL e.g. TCS"
          className="bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs uppercase"
        />
        <div className="flex gap-1">
          {(["BUY", "SELL"] as const).map((s) => (
            <button
              key={s}
              onClick={() => { setSide(s); setShowConfirmStep(false); setPreview(null); }}
              className={`flex-1 px-2 py-2 rounded font-display tabular-nums text-xs border ${
                side === s
                  ? s === "BUY"
                    ? "bg-signal-buy/30 border-signal-buy/60 text-white"
                    : "bg-signal-sell/30 border-signal-sell/60 text-white"
                  : "bg-graphite border-white/10 text-paper/50"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 mb-2">
        <label className="font-display tabular-nums text-[10px] text-paper/40 col-span-3 -mb-1">Quantity</label>
        <input
          type="number" min={1} value={qty}
          onChange={(e) => { setQty(Math.max(1, Number(e.target.value) || 1)); setShowConfirmStep(false); }}
          className="bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs col-span-3"
        />
      </div>

      <div className="grid grid-cols-2 gap-2 mb-2">
        <div>
          <label className="font-display tabular-nums text-[10px] text-paper/40">Order Type</label>
          <select
            value={orderType}
            onChange={(e) => { setOrderType(e.target.value as "LIMIT" | "MARKET"); setShowConfirmStep(false); }}
            className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs"
          >
            <option value="LIMIT">LIMIT</option>
            <option value="MARKET">MARKET</option>
          </select>
        </div>
        <div>
          <label className="font-display tabular-nums text-[10px] text-paper/40">Product</label>
          <select
            value={productType}
            onChange={(e) => { setProductType(e.target.value as "CNC" | "MIS"); setShowConfirmStep(false); }}
            className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs"
          >
            <option value="CNC">CNC (delivery)</option>
            <option value="MIS">MIS (intraday)</option>
          </select>
        </div>
      </div>

      {orderType === "LIMIT" && (
        <div className="mb-2">
          <label className="font-display tabular-nums text-[10px] text-paper/40">Limit Price (₹, optional — defaults to LTP)</label>
          <input
            type="number" value={limitPrice}
            onChange={(e) => { setLimitPrice(e.target.value); setShowConfirmStep(false); }}
            className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs"
          />
        </div>
      )}

      {side === "BUY" && (
        <div className="grid grid-cols-2 gap-2 mb-2">
          <div>
            <label className="font-display tabular-nums text-[10px] text-paper/40">Stop Loss (₹, optional)</label>
            <input
              type="number" value={stopPrice}
              onChange={(e) => { setStopPrice(e.target.value); setShowConfirmStep(false); }}
              className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs"
            />
          </div>
          <div>
            <label className="font-display tabular-nums text-[10px] text-paper/40">Target (₹, optional)</label>
            <input
              type="number" value={targetPrice}
              onChange={(e) => { setTargetPrice(e.target.value); setShowConfirmStep(false); }}
              className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-display tabular-nums text-xs"
            />
          </div>
        </div>
      )}

      {blockedByArm && (
        <p className="font-display tabular-nums text-[11px] text-signal-hold/70 mb-2">
          {mode} is not armed — arm it before a manual BUY can be sent.
        </p>
      )}
      {err && <p className="font-display tabular-nums text-[11px] text-signal-sell/80 mb-2">{err}</p>}

      {!showConfirmStep && (
        <button
          onClick={() => void doPreview()}
          disabled={!canReview || previewing}
          className="w-full px-4 py-2 rounded bg-signal-prepare/30 border border-signal-prepare/60 font-display tabular-nums text-xs disabled:opacity-50"
        >
          {previewing ? "Checking…" : `Review ${side}`}
        </button>
      )}

      {showConfirmStep && preview && (
        <div className={`border rounded-xl p-3 mt-2 font-display tabular-nums text-[11px] space-y-1 ${
          preview.ok ? "border-signal-buy/40 bg-signal-buy/20" : "border-signal-sell/40 bg-signal-sell/20"
        }`}>
          {side === "BUY" ? (
            <>
              <div className="flex justify-between"><span className="text-paper/50">Entry</span><span>{fmtInr(preview.entry_price)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Stop</span><span>{fmtInr(preview.stop_price)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Target</span><span>{fmtInr(preview.target_price)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Qty (requested → approved)</span><span>{preview.qty_requested} → {preview.approved_qty}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Estimated Value</span><span>{fmtInr(preview.estimated_value)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Risk</span><span>{fmtInr(preview.risk_amount)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">R:R</span><span>{preview.risk_reward != null ? `1 : ${preview.risk_reward}` : "—"}</span></div>
            </>
          ) : (
            <>
              <div className="flex justify-between"><span className="text-paper/50">Qty available</span><span>{preview.qty_available}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Qty (requested → sending)</span><span>{preview.qty_requested} → {preview.approved_qty}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Est. exit price</span><span>{fmtInr(preview.exit_price_estimate)}</span></div>
              <div className="flex justify-between"><span className="text-paper/50">Est. P&amp;L</span><span>{fmtInr(preview.estimated_pnl)}</span></div>
            </>
          )}
          {!preview.ok && (
            <p className="text-signal-sell/90 pt-1">{preview.detail || preview.reason}</p>
          )}

          {preview.ok && mode === "REAL" ? (
            <div className="pt-2 space-y-2">
              <p className="text-signal-hold/90 uppercase tracking-wide">⚠ Real money order</p>
              <div className="flex gap-2">
                <button onClick={() => { setShowConfirmStep(false); setPreview(null); }} className="flex-1 px-3 py-2 rounded bg-graphite border border-white/10 text-paper/60">
                  Cancel
                </button>
                <button
                  onClick={() => void doConfirm()}
                  disabled={confirming}
                  className={`flex-1 px-3 py-2 rounded font-bold disabled:opacity-50 ${
                    side === "BUY" ? "bg-signal-buy/40 border border-signal-buy/70 text-white" : "bg-signal-sell/40 border border-signal-sell/70 text-white"
                  }`}
                >
                  {confirming ? "Sending…" : `CONFIRM ${side}`}
                </button>
              </div>
            </div>
          ) : preview.ok ? (
            <button
              onClick={() => void doConfirm()}
              disabled={confirming}
              className={`w-full mt-2 px-3 py-2 rounded font-bold disabled:opacity-50 ${
                side === "BUY" ? "bg-signal-buy/40 border border-signal-buy/70 text-white" : "bg-signal-sell/40 border border-signal-sell/70 text-white"
              }`}
            >
              {confirming ? "Sending…" : `Confirm ${side} (${mode})`}
            </button>
          ) : (
            <button onClick={() => { setShowConfirmStep(false); setPreview(null); }} className="w-full mt-2 px-3 py-2 rounded bg-graphite border border-white/10 text-paper/60">
              Close
            </button>
          )}
        </div>
      )}

      {result && (
        <p className={`font-display tabular-nums text-[11px] mt-2 ${result.ok ? "text-signal-buy/80" : "text-signal-sell/80"}`}>
          {result.ok ? "✓ " : "✕ "}{result.text}
        </p>
      )}
    </div>
  );
}
