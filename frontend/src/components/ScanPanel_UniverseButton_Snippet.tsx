/**
 * SNIPPET – integrate into ScanPanel.tsx
 *
 * 1. Rename the old "Send all Actionable to Training" / "Add to Training" button to:
 *    "Send the Stock Universe For Training"
 *
 * 2. On click, send the FULL scan universe (all symbols), not only BUY NOW / PREPARE TO BUY.
 *
 * Example usage inside ScanPanel (replace the old actionable-only handler):
 */

/*
import { sendStockUniverseForTraining, buildUniversePayloadFromScan } from "../api_universe";

// Inside component:
const handleSendUniverseForTraining = async () => {
  if (!result) {
    setStatusMessage("Run a market scan first");
    return;
  }
  try {
    setStatusMessage("Sending full stock universe for training…");
    const payload = buildUniversePayloadFromScan({
      // Prefer all_results if API returns the full scanned set
      all_results: result.all_results || result.recommendations,
      recommendations: result.recommendations,
      universe: result.universe,
    });
    if (!payload.symbols.length) {
      setStatusMessage("No symbols in scan universe");
      return;
    }
    const res = await sendStockUniverseForTraining(payload);
    if (res.ok) {
      setStatusMessage(
        `✅ ${res.ingested} symbols sent to training (kept ${res.retention_hours || 48}h)` +
          (res.training_triggered ? " · training triggered" : "")
      );
    } else {
      setStatusMessage(`❌ ${res.message || "Failed to send universe"}`);
    }
  } catch (e: any) {
    setStatusMessage(`❌ ${e?.message || "Failed to send universe for training"}`);
  }
};

// Button JSX:
<button
  className="text-xs px-3 py-1.5 rounded bg-signal-prepare hover:bg-signal-prepare text-white font-medium"
  onClick={handleSendUniverseForTraining}
  title="Send the entire daily scan universe into training (more stocks = better model)"
>
  Send the Stock Universe For Training
</button>
*/

export {};
