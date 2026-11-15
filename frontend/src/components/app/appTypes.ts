import type { Decision, ScanResult } from "../../api";
// Shared by App.tsx and components/app/AppNav.tsx (E68).
export type Tab = "dashboard" | "notifications" | "training" | "trades" | "hot" | "surprise" | "ipo" | "datafeed" | "settings" | "watchlist" | "realtrade" | "positionstocks" | "fno" | "calibration" | "selfimprove";

export type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string; progress?: { processed: number; total: number; elapsed: number; estimatedRemaining?: number } }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };
