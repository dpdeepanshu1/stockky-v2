// frontend/src/components/ui/motion.tsx  (group 460, phase 1 - the motion toolkit; styles live in design-v4.css)
// Transform/opacity only, no animation library. Everything is switched off by CSS under reduced-motion and in TV mode, and paused while the tab is hidden.
// Money and order values must NOT use these (they never animate, so they cannot be misread mid-motion) - pass animate={false} or just don't use them there.
import { useEffect, useRef, useState, type CSSProperties, type HTMLAttributes, type ReactNode } from "react";

/** 1. Price roll: when `value` changes, the text slides up (rose) or down (fell). `text` is what is shown (already formatted). */
export function PriceRoll({ value, text, animate = true, className = "" }: { value: number | null | undefined; text: string; animate?: boolean; className?: string }) {
  const prev = useRef(value);
  const [dir, setDir] = useState<"" | "up" | "down">("");
  const [k, setK] = useState(0);
  useEffect(() => {
    const p = prev.current;
    prev.current = value;
    if (!animate || p == null || value == null || value === p) return;
    if (typeof document !== "undefined" && document.hidden) return;
    setDir(value > p ? "up" : "down");
    setK((n) => n + 1);
  }, [value, animate]);
  return <span key={k} className={`v4-roll ${dir} ${className}`.trim()}>{text}</span>;
}

/** 2. Tick glint: returns "v4-glint-up" / "v4-glint-down" for ~700 ms after `value` changes. Use on VISIBLE rows only. */
export function useTickGlint(value: number | null | undefined, enabled = true): string {
  const prev = useRef(value);
  const [cls, setCls] = useState("");
  useEffect(() => {
    const p = prev.current;
    prev.current = value;
    if (!enabled || p == null || value == null || value === p) return;
    if (typeof document !== "undefined" && document.hidden) return;
    setCls(value > p ? "v4-glint-up" : "v4-glint-down");
    const id = window.setTimeout(() => setCls(""), 720);
    return () => window.clearTimeout(id);
  }, [value, enabled]);
  return cls;
}

/** 4. Market breathing dot: green + pulsing when the feed is live, grey and still when stale or the market is closed. */
export function LiveDot({ state = "live", label }: { state?: "live" | "stale" | "closed"; label?: string }) {
  return <span className={`v4-live-dot ${state === "live" ? "" : state}`} role="img" aria-label={label || (state === "live" ? "Live data" : state === "stale" ? "Data is stale" : "Market closed")} />;
}

/** 5. Skeleton shimmer instead of a spinner. */
export function Skeleton({ w = "100%", h = 14, className = "" }: { w?: number | string; h?: number | string; className?: string }) {
  const style: CSSProperties = { width: w, height: h };
  return <span className={`v4-skel ${className}`.trim()} style={style} aria-hidden="true" />;
}

/** Row wrapper for a live price: the row edge flashes green / red for ~700 ms when `value` (a PRICE, never a P&L or order value) changes. */
export function GlintBox({ value, className = "", children, ...rest }: { value: number | null | undefined; className?: string; children: ReactNode } & HTMLAttributes<HTMLDivElement>) {
  const glint = useTickGlint(value);
  return <div {...rest} className={`${className} ${glint}`.trim()}>{children}</div>;
}
