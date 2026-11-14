// frontend/src/components/session/SessionRail.tsx  (E277)
// "Trading day": the plan's morning timeline as one rail, with a marker that sits where IST time stands. Presentation only: no request, no control.
// The slot list and wording come from sessionView.ts (sessionView.test.mjs); the services' own switches decide what really runs.
// The clock tick is a plain setInterval (no network). Motion (a one-time sweep and a ring on the running slot) is in design-v8.css and is off under reduced-motion, TV mode and "Animations: Off".
import { useEffect, useState } from "react";
import { sessionView, type SessionService } from "../../sessionView";

export default function SessionRail({ service }: { service: SessionService }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => { if (typeof document === "undefined" || document.visibilityState !== "hidden") setNow(new Date()); }, 30000);
    return () => clearInterval(id);
  }, []);
  const v = sessionView(service, now);
  return (
    <section className={`w-day ${v.weekday ? "" : "off"}`} aria-label={v.title} data-testid={`session-rail-${service}`}>
      <header className="w-day-head">
        <h3>{v.title}</h3>
        <span className="w-day-clock">{v.weekday ? "IST" : "weekend"}</span>
      </header>
      <div className="w-day-scroll">
        <ol className="w-slots" style={{ ["--n" as any]: v.slots.length, ["--p" as any]: v.pct }}>
          <li className="w-track" aria-hidden="true"><span className="w-fill" /></li>
          {v.slots.map((s) => (
            <li key={s.id} className={`w-slot ${s.state}`} aria-current={s.state === "now" ? "step" : undefined} title={s.note}>
              <span className="dot" aria-hidden="true">{s.state === "done" ? "✓" : s.state === "now" ? "●" : "○"}</span>
              <b>{s.label}</b>
              <small>{s.at}</small>
            </li>
          ))}
        </ol>
      </div>
      <p className="w-day-words" data-testid="session-words">{v.words}</p>
    </section>
  );
}
