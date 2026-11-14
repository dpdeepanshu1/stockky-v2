// components/kit/Card.tsx (E534) - the one card surface. Styles: styles/components.css (.k-card). Presentation only.
import type { HTMLAttributes, ReactNode } from "react";

export type CardTone = "default" | "warn" | "sell";

export function Card({ children, flush = false, quiet = false, tone = "default", className = "", ...rest }: {
  children: ReactNode; flush?: boolean; quiet?: boolean; tone?: CardTone; className?: string;
} & Omit<HTMLAttributes<HTMLElement>, "className" | "children">) {
  const cls = ["k-card", flush ? "flush" : "", quiet ? "quiet" : "", tone !== "default" ? `tint-${tone}` : "", className].filter(Boolean).join(" ");
  return <section className={cls} {...rest}>{children}</section>;
}

export function CardHeader({ title, sub, action }: { title: ReactNode; sub?: ReactNode; action?: ReactNode }) {
  return (
    <header className="k-card-head">
      <div>
        <h3 className="k-card-title">{title}</h3>
        {sub != null && <p className="k-card-sub">{sub}</p>}
      </div>
      {action != null && <div className="k-card-action">{action}</div>}
    </header>
  );
}
