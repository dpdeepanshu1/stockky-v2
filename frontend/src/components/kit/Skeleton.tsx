// components/kit/Skeleton.tsx (E534) - loading placeholder (instead of spinners), the empty state and the error state.
import type { ReactNode } from "react";
import { Button } from "./Button";

export function Skeleton({ w = "100%", h = 14 }: { w?: number | string; h?: number | string }) {
  return <span className="k-skel" style={{ width: w, height: h }} aria-hidden="true" />;
}

export function SkeletonLines({ rows = 3 }: { rows?: number }) {
  return (
    <div className="k-skel-lines" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} w={i === rows - 1 ? "60%" : "100%"} />)}
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="k-empty" role="status">
      <p className="k-empty-title">{title}</p>
      {hint && <p className="k-empty-hint">{hint}</p>}
      {action != null && <div className="k-empty-action">{action}</div>}
    </div>
  );
}

export function ErrorState({ title = "Could not load this", hint, onRetry }: { title?: string; hint?: string; onRetry?: () => void }) {
  return (
    <div className="k-error" role="alert">
      <p className="k-error-title">{title}</p>
      {hint && <p className="k-error-hint">{hint}</p>}
      {onRetry && <div className="k-error-action"><Button variant="ghost" size="sm" onClick={onRetry}>Try again</Button></div>}
    </div>
  );
}
