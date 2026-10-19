// frontend/src/components/AdminGate.tsx  (group 460)
// One login for Real Trade, Position Stocks and Real Money Calibration.
//   * <AdminGate> wraps each of the three pages. Before login the page is NOT mounted, so it makes no backend calls while locked.
//   * If the session ends while you are using a page, the page stays mounted (its state is kept) under a re-login overlay.
//   * <SessionChip> is the small countdown ring in the top bar (click = log out).
// Storage keys and endpoints are unchanged (see adminAuth.ts).
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  AUTH_CHANGED_EVENT, AUTH_LOGOUT_EVENT, AUTH_LOST_EVENT, changePasswordAll, loginAll, logoutEverywhere, readSession, type LoginOutcome, type SessionInfo,
} from "../adminAuth";
import { formatCountdown, initialGatePhase, MIN_NEW_PASSWORD, nextGatePhase, passwordStrength, validateNewPassword, type GatePhase } from "../adminSession";

const reducedMotion = () => {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.documentElement.classList.contains("tv"); } catch { return false; }
};

/* ---------- session hook ---------- */
const bucket = (x: number | null) => (x == null ? null : x > 600 ? Math.floor(x / 30) * 30 : x);

export function useAdminSession(): SessionInfo {
  const [s, setS] = useState<SessionInfo>(readSession);
  useEffect(() => {
    const upd = () => setS((prev) => {
      const n = readSession();
      return prev.has === n.has && prev.rt === n.rt && prev.ps === n.ps && bucket(prev.secondsLeft) === bucket(n.secondsLeft) ? prev : n;
    });
    const id = window.setInterval(() => { if (!document.hidden) upd(); }, 1000); // paused while the tab is hidden
    const evs = [AUTH_CHANGED_EVENT, AUTH_LOST_EVENT, AUTH_LOGOUT_EVENT, "storage", "visibilitychange"];
    evs.forEach((e) => window.addEventListener(e, upd));
    document.addEventListener("visibilitychange", upd);
    return () => { window.clearInterval(id); evs.forEach((e) => window.removeEventListener(e, upd)); document.removeEventListener("visibilitychange", upd); };
  }, []);
  return s;
}

/* ---------- icons ---------- */
function LockIcon({ open }: { open: boolean }) {
  return (
    <svg className={`auth-lock ${open ? "open" : ""}`} viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path className="auth-lock-shackle" d="M8 11V8a4 4 0 0 1 8 0v3" />
      <rect x="5" y="11" width="14" height="10" rx="2.5" />
    </svg>
  );
}
function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" />
      {off && <path d="M4 4l16 16" />}
    </svg>
  );
}

/* ---------- floating-label field ---------- */
function Field({ id, label, type = "text", value, onChange, autoComplete, onKey, trailing, autoFocus }: {
  id: string; label: string; type?: string; value: string; onChange: (v: string) => void; autoComplete?: string;
  onKey?: (e: React.KeyboardEvent<HTMLInputElement>) => void; trailing?: ReactNode; autoFocus?: boolean;
}) {
  return (
    <div className="auth-field">
      <input id={id} type={type} value={value} placeholder=" " autoComplete={autoComplete} autoFocus={autoFocus} spellCheck={false}
        autoCapitalize="none" onChange={(e) => onChange(e.target.value)} onKeyUp={onKey} onKeyDown={onKey} />
      <label htmlFor={id}>{label}</label>
      {trailing}
    </div>
  );
}

/* ---------- the form (login + change password), shared by the page and the overlay ---------- */
export function LoginForm({ onHold, onSuccess, compact }: { onHold: () => void; onSuccess: (o?: LoginOutcome) => void; compact?: boolean }) {
  const [mode, setMode] = useState<"login" | "change">("login");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [caps, setCaps] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [shake, setShake] = useState(0);
  const [done, setDone] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const caps_ = (e: React.KeyboardEvent<HTMLInputElement>) => { try { setCaps(e.getModifierState("CapsLock")); } catch { /* ignore */ } };
  const fail = (m: string) => { setErr(m); setShake((n) => n + 1); };
  const succeed = (o?: LoginOutcome) => {
    setDone(true); onHold();
    window.setTimeout(() => { if (alive.current) onSuccess(o); }, reducedMotion() ? 0 : 750); // let the lock finish opening
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || done) return;
    setErr(null); setInfo(null);
    if (mode === "login") {
      setBusy(true);
      const o = await loginAll(username, password);
      if (!alive.current) return;
      setBusy(false);
      if (!o.ok) return fail(o.message || "Login failed.");
      setPassword("");
      return succeed(o);
    }
    const bad = validateNewPassword(password, next, confirm);
    if (bad) return fail(bad);
    setBusy(true);
    const r = await changePasswordAll(username, password, next);
    if (!alive.current) return;
    setBusy(false);
    if (!r.ok) return fail(r.message);
    setPassword(""); setNext(""); setConfirm("");
    if (readSession().has) return succeed({ ok: true, rt: readSession().rt, ps: readSession().ps });
    setMode("login"); setInfo(r.message);
  };

  const eye = (
    <button type="button" className="auth-eye" aria-label={show ? "Hide password" : "Show password"} aria-pressed={show} onClick={() => setShow((v) => !v)}>
      <EyeIcon off={show} />
    </button>
  );
  const strength = passwordStrength(next);

  return (
    <form className={`auth-form ${compact ? "compact" : ""}`} onSubmit={submit} noValidate>
      <div className={`auth-lock-badge ${done ? "done" : ""}`}><LockIcon open={done} /></div>
      <h2 className="auth-title">{mode === "login" ? (compact ? "Session ended" : "Welcome back") : "Change password"}</h2>
      <p className="auth-sub">
        {mode === "login"
          ? (compact ? "Log in again to carry on. This page keeps what was on screen." : "One login for Real Trade, Position Stocks and Real Money Calibration.")
          : "Enter your current password, then choose a new one. It applies to both trading services."}
      </p>

      <div key={shake} className={`auth-fields ${shake ? "shake" : ""}`}>
        <Field id="auth-user" label="Username" value={username} onChange={setUsername} autoComplete="username" />
        <Field id="auth-pass" label={mode === "login" ? "Password" : "Current password"} type={show ? "text" : "password"} value={password}
          onChange={setPassword} autoComplete="current-password" onKey={caps_} trailing={eye} autoFocus />
        {mode === "change" && (
          <>
            <Field id="auth-new" label="New password" type={show ? "text" : "password"} value={next} onChange={setNext} autoComplete="new-password" onKey={caps_} />
            <div className="auth-meter" aria-hidden="true">{[1, 2, 3, 4].map((i) => <span key={i} className={i <= strength ? `on s${strength}` : ""} />)}</div>
            <Field id="auth-confirm" label="Repeat new password" type={show ? "text" : "password"} value={confirm} onChange={setConfirm} autoComplete="new-password" onKey={caps_} />
            <p className="auth-hint">At least {MIN_NEW_PASSWORD} characters. Different from the current one.</p>
          </>
        )}
      </div>

      {caps && <p className="auth-caps" role="status">Caps Lock is on.</p>}
      {err && <p className="auth-error" role="alert">{err}</p>}
      {info && <p className="auth-info" role="status">{info}</p>}

      <button className="auth-submit" type="submit" disabled={busy || done || !password || (mode === "change" && (!next || !confirm))}>
        {done ? "Unlocked" : busy ? (mode === "login" ? "Logging in…" : "Changing…") : mode === "login" ? "Log in" : "Change password"}
      </button>
      <button type="button" className="auth-link" onClick={() => { setMode(mode === "login" ? "change" : "login"); setErr(null); setInfo(null); }} disabled={busy || done}>
        {mode === "login" ? "Change password" : "Back to log in"}
      </button>
      <p className="auth-foot">Sessions end automatically. A countdown ring in the top bar shows the time left.</p>
    </form>
  );
}

/* ---------- full login page (split screen on desktop, one card on phones) ---------- */
function LoginPage({ title, onHold, onSuccess }: { title: string; onHold: () => void; onSuccess: (o?: LoginOutcome) => void }) {
  return (
    <div className="auth-page">
      <aside className="auth-art" aria-hidden="true">
        <div className="auth-art-glow" />
        <div className="auth-brand"><span className="brand-mark">S</span><span>Stockky</span></div>
        <h1>One login for every trading page.</h1>
        <ul>
          <li>Real Trade</li><li>Position Stocks</li><li>Real Money Calibration</li>
        </ul>
        <svg className="auth-chart" viewBox="0 0 400 140" preserveAspectRatio="none">
          <path className="auth-chart-area" d="M0 110 C40 100 60 70 100 78 S160 40 200 52 S270 96 310 60 S370 20 400 26 V140 H0 Z" />
          <path className="auth-chart-line" pathLength="1" d="M0 110 C40 100 60 70 100 78 S160 40 200 52 S270 96 310 60 S370 20 400 26" />
        </svg>
      </aside>
      <section className="auth-panel" aria-label={`${title} login`}>
        <p className="auth-for">{title} needs the admin login</p>
        <LoginForm onHold={onHold} onSuccess={onSuccess} />
      </section>
    </div>
  );
}

/* ---------- the gate ---------- */
export function AdminGate({ title, children, onExit }: { title: string; children: ReactNode; onExit?: () => void }) {
  const session = useAdminSession();
  const [phase, setPhase] = useState<GatePhase>(() => initialGatePhase(readSession().has));
  const holding = useRef(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => { setPhase((p) => nextGatePhase(p, session.has, holding.current)); }, [session.has]);
  useEffect(() => {
    const h = () => { holding.current = false; setPhase("login"); }; // explicit log-out: back to the login page
    window.addEventListener(AUTH_LOGOUT_EVENT, h);
    return () => window.removeEventListener(AUTH_LOGOUT_EVENT, h);
  }, []);

  const hold = useCallback(() => { holding.current = true; }, []);
  const finish = useCallback((o?: LoginOutcome) => { holding.current = false; setNotice(o?.partial ?? null); setPhase(readSession().has ? "app" : "login"); }, []);

  if (phase === "login") return <LoginPage title={title} onHold={hold} onSuccess={finish} />;
  const locked = phase === "lock";
  return (
    <>
      {notice && (
        <div className="auth-notice" role="status">
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss">×</button>
        </div>
      )}
      <div className={locked ? "auth-behind" : undefined} aria-hidden={locked || undefined} {...(locked ? ({ inert: "" } as any) : {})}>{children}</div>
      {locked && (
        <div className="auth-overlay" role="dialog" aria-modal="true" aria-label="Session ended">
          <div className="auth-overlay-card">
            <LoginForm compact onHold={hold} onSuccess={finish} />
            {onExit && <button type="button" className="auth-link auth-exit" onClick={onExit}>Leave this page</button>}
          </div>
        </div>
      )}
    </>
  );
}

/* ---------- top-bar countdown ring ---------- */
export function SessionChip() {
  const s = useAdminSession();
  const peak = useRef(0);
  if (!s.has) { peak.current = 0; return null; }
  const left = s.secondsLeft;
  if (left != null) peak.current = Math.max(peak.current, left);
  const frac = left == null || !peak.current ? 1 : Math.min(1, left / peak.current);
  const warn = left != null && left <= 600;
  const C = 2 * Math.PI * 9;
  return (
    <button type="button" className={`session-chip ${warn ? "warn" : ""}`} onClick={logoutEverywhere}
      title={left != null ? `Admin session: ${formatCountdown(left)} left. Click to log out.` : "Admin session active. Click to log out."}
      aria-label={left != null ? `Admin session, ${formatCountdown(left)} left. Log out.` : "Admin session active. Log out."}>
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <circle cx="12" cy="12" r="9" className="ring-bg" />
        <circle cx="12" cy="12" r="9" className="ring-fg" strokeDasharray={C} strokeDashoffset={C * (1 - frac)} transform="rotate(-90 12 12)" />
      </svg>
      <span>{left != null ? formatCountdown(left) : "Admin"}</span>
    </button>
  );
}
