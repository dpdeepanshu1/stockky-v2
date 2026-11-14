// components/kit/Button.tsx (E534) - primary / soft / ghost / danger.
// `confirmText` (for danger actions): the first click turns the label into the confirm text for 3 seconds, the second click inside that time runs onClick.
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type MouseEvent, type ReactNode } from "react";

export type ButtonVariant = "primary" | "soft" | "ghost" | "danger";

export function Button({ variant = "primary", size = "md", block = false, loading = false, confirmText, onClick, disabled, children, className = "", type = "button", ...rest }: {
  variant?: ButtonVariant; size?: "sm" | "md"; block?: boolean; loading?: boolean; confirmText?: string; children: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const [asking, setAsking] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current != null) window.clearTimeout(timer.current); }, []);

  const click = (e: MouseEvent<HTMLButtonElement>) => {
    if (loading) return;
    if (confirmText && !asking) {
      setAsking(true);
      timer.current = window.setTimeout(() => setAsking(false), 3000);
      return;
    }
    if (timer.current != null) window.clearTimeout(timer.current);
    setAsking(false);
    onClick?.(e);
  };

  const cls = ["k-btn", variant, size === "sm" ? "sm" : "", block ? "block" : "", asking ? "asking" : "", className].filter(Boolean).join(" ");
  return (
    <button type={type} className={cls} disabled={disabled || loading} aria-busy={loading || undefined} onClick={click} {...rest}>
      {loading && <span className="k-spin" aria-hidden="true" />}
      {asking && confirmText ? confirmText : children}
    </button>
  );
}
