// components/kit/Chip.tsx (F1) - the one small status chip. It replaces the three look-alikes the widgets had (money banner, AI strip, exposure strip).
// A chip with a tone always carries a symbol (the caller passes one), so a state is never colour-only. Presentation only.
import type { ReactNode } from "react";

export type ChipTone = "neutral" | "ok" | "warn" | "bad" | "up" | "down";

export function ChipList({ children, label, className = "" }: { children: ReactNode; label?: string; className?: string }) {
  return <ul className={["k-chips", className].filter(Boolean).join(" ")} aria-label={label}>{children}</ul>;
}

export function Chip({ tone = "neutral", symbol, as = "li", children, title }: { tone?: ChipTone; symbol?: string; as?: "li" | "span"; children: ReactNode; title?: string }) {
  const Tag = as;
  return (
    <Tag className={["k-chip", tone !== "neutral" ? tone : ""].filter(Boolean).join(" ")} title={title}>
      {symbol ? <span className="k-chip-sym" aria-hidden="true">{symbol}</span> : null}
      {children}
    </Tag>
  );
}
