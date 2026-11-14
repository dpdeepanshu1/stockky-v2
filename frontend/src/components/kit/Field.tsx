// components/kit/Field.tsx (E534) - a labelled input / select / textarea, and a Switch row. 16px text (no iOS zoom), 44px tall.
import { useId, type InputHTMLAttributes, type ReactNode } from "react";

export function Field({ label, hint, error, as = "input", children, ...rest }: {
  label: string; hint?: ReactNode; error?: string | null; as?: "input" | "select" | "textarea"; children?: ReactNode;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "children">) {
  const id = useId();
  const hintId = `${id}-hint`, errId = `${id}-err`;
  const aria = { id, "aria-invalid": error ? true : undefined, "aria-describedby": [error ? errId : "", hint != null ? hintId : ""].filter(Boolean).join(" ") || undefined };
  return (
    <div className={`k-field ${error ? "invalid" : ""}`}>
      <label htmlFor={id}>{label}</label>
      {as === "select" ? <select {...(rest as object)} {...aria}>{children}</select>
        : as === "textarea" ? <textarea {...(rest as object)} {...aria} />
        : <input {...rest} {...aria} />}
      {hint != null && !error && <span className="k-field-hint" id={hintId}>{hint}</span>}
      {error && <span className="k-field-error" id={errId} role="alert">{error}</span>}
    </div>
  );
}

export function Switch({ label, description, checked, onChange, disabled = false }: {
  label: string; description?: ReactNode; checked: boolean; onChange: (next: boolean) => void; disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="k-switch-row">
      <div className="k-switch-text">
        <span className="k-switch-label" id={`${id}-l`}>{label}</span>
        {description != null && <span className="k-switch-desc" id={`${id}-d`}>{description}</span>}
      </div>
      <button type="button" role="switch" className="k-switch" aria-checked={checked} aria-labelledby={`${id}-l`} aria-describedby={description != null ? `${id}-d` : undefined}
        disabled={disabled} onClick={() => onChange(!checked)} />
    </div>
  );
}
