import { useEffect, useState } from "react";

// The header status strip previously rendered `new Date()` inline in the
// App component's own JSX, so the clock only advanced whenever something
// else in App re-rendered (a poll tick, a state change, etc.) — it would
// visibly freeze for stretches and then jump, which read as "wrong". This
// self-ticks on its own 1s interval, isolated from App's render cycle.
export function StatusClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return (
    <span className="topbar-clock" title="Indian Standard Time">
      {now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST
    </span>
  );
}

export function MarketClock() {
  const [now, setNow] = useState(() => new Date());
  const [phase, setPhase] = useState<string | null>(null);
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    let c = false;
    (async () => {
      try {
        const { getApiUrl } = await import("../../api");
        const base = getApiUrl();
        if (!base) return;
        const res = await fetch(`${base.replace(/\/$/, "")}/market/session`);
        if (!c && res.ok) {
          const j = await res.json();
          setPhase(j.phase || null);
        }
      } catch { /* ignore */ }
    })();
    return () => { c = true; };
  }, []);
  const ist = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    weekday: "short",
  }).format(now);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    weekday: "short",
  }).formatToParts(now);
  const map: Record<string, string> = {};
  for (const p of parts) if (p.type !== "literal") map[p.type] = p.value;
  const hm = parseInt((map.hour || "0") + (map.minute || "0"), 10);
  const day = map.weekday || "";
  const weekday = !["Sat", "Sun"].includes(day);
  const open = phase ? phase === "open" : weekday && hm >= 915 && hm <= 1530;
  const pre = phase ? phase === "preopen" : weekday && hm >= 830 && hm < 915;
  const post = phase ? phase === "post" : weekday && hm > 1530 && hm <= 1600;
  const holiday = phase === "holiday";
  const label = holiday ? "NSE HOLIDAY" : open ? "NSE OPEN" : pre ? "PRE-OPEN" : post ? "POST" : "NSE CLOSED";
  const color = holiday ? "text-signal-sell" : open ? "text-signal-buy" : pre || post ? "text-amber-300" : "text-mist/60";
  return (
    <div className="market-clock mono text-[11px] flex flex-wrap gap-3 mb-2">
      <span className={color}>{label}</span>
      <span className="text-mist/70">{ist} IST</span>
      <span className="text-mist/50">Session 09:15–15:30 · Full warm 08:30–16:00</span>
    </div>
  );
}
