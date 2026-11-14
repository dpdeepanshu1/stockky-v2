// components/kit/StatusPill.tsx (E534) - one pill for every state. Each state has its own dot SHAPE as well as its colour, so it is never colour-only.
import type { ReactNode } from "react";

export type PillState = "live" | "armed" | "paused" | "offline" | "closed" | "info";
export const PILL_LABEL: Record<PillState, string> = { live: "Live", armed: "Armed", paused: "Paused", offline: "Offline", closed: "Closed", info: "Info" };

export function StatusPill({ state, children }: { state: PillState; children?: ReactNode }) {
  return (
    <span className={`k-pill ${state}`} role="status">
      <i aria-hidden="true" />
      {children ?? PILL_LABEL[state]}
    </span>
  );
}
