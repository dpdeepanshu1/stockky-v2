// Self Improve System: the two-click state shared by the Controls panel (round 543, S3). The first click on an action arms it, a second click on the SAME
// action fires it, a click on another action re-arms, and an armed action disarms itself after 8 seconds. The rule itself is nextArm() in selfImproveView.ts.
import { useCallback, useEffect, useState } from "react";
import { nextArm } from "../../selfImproveView";

export const ARM_MS = 8000;

export function useArm(): { armed: string | null; press: (id: string, twoClicks: boolean, fire: () => void) => void; cancel: () => void } {
  const [armed, setArmed] = useState<string | null>(null);
  useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(null), ARM_MS);
    return () => clearTimeout(id);
  }, [armed]);
  const press = useCallback((id: string, twoClicks: boolean, fire: () => void) => {
    if (!twoClicks) { setArmed(null); fire(); return; }
    const n = nextArm(armed, id);
    setArmed(n.armed);
    if (n.fire) fire();
  }, [armed]);
  return { armed, press, cancel: () => setArmed(null) };
}
