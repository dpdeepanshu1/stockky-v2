// E536 (redesign round 4): "Your positions today" on Home. Read-only: it only runs while an admin session exists (hidden and silent while logged out),
// calls the same two GET endpoints the trading pages already use, and has no button that places, closes or changes anything.
import { useEffect, useState } from "react";
import { AUTH_CHANGED_EVENT, AUTH_LOGOUT_EVENT, readSession } from "../../adminAuth";
import { realTradeApi } from "../../realTradeApi";
import { positionStocksApi } from "../../positionStocksApi";
import { setVisibleInterval } from "../../visibleInterval";
import { Card, CardHeader } from "../kit/Card";
import { Stat, Stats } from "../kit/Stat";
import { fromRealTrade, fromScalp, pctText, signedRupees, sortPositions, summarizePositions, toneOf, type HomePosition } from "./homeView";

const POLL_MS = 30_000;
const SHOW_ROWS = 6;

export default function HomePositions() {
  const [session, setSession] = useState(() => readSession());
  const [rows, setRows] = useState<HomePosition[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const on = () => setSession(readSession());
    const evs = [AUTH_CHANGED_EVENT, AUTH_LOGOUT_EVENT, "storage"];
    evs.forEach((e) => window.addEventListener(e, on));
    return () => evs.forEach((e) => window.removeEventListener(e, on));
  }, []);

  const rt = session.rt, ps = session.ps;
  useEffect(() => {
    if (!rt && !ps) { setRows(null); setFailed(false); return; }
    let cancelled = false;
    const load = async () => {
      const [a, b] = await Promise.allSettled([
        rt ? realTradeApi.positions("REAL") : Promise.resolve(null),
        ps ? positionStocksApi.positions() : Promise.resolve(null),
      ]);
      if (cancelled) return;
      const err = (rt && a.status === "rejected") || (ps && b.status === "rejected");
      setFailed(!!err);
      // A failed side keeps nothing rather than showing stale numbers as live.
      setRows([
        ...(a.status === "fulfilled" ? fromRealTrade(a.value as any) : []),
        ...(b.status === "fulfilled" ? fromScalp(b.value as any) : []),
      ]);
    };
    void load();
    const stop = setVisibleInterval(load, POLL_MS);
    return () => { cancelled = true; stop(); };
  }, [rt, ps]);

  if ((!rt && !ps) || rows === null) return null;
  if (rows.length === 0 && !failed) return null;

  const sum = summarizePositions(rows);
  const list = sortPositions(rows).slice(0, SHOW_ROWS);
  return (
    <Card aria-label="Your positions today" data-testid="home-positions">
      <CardHeader title="Your positions today" sub="Open positions in REAL mode, from Auto Trade and Position Stocks. Updates every 30 seconds." />
      {failed && <p className="hm-note" role="alert">One of the two services did not answer, so the list may be incomplete.</p>}
      <Stats>
        <Stat label="Open positions" size="sm" value={String(sum.count)} />
        <Stat label="Open P&L" size="sm" value={sum.pnl === null ? "—" : signedRupees(sum.pnl)} tone={sum.tone} delta={sum.pnl === null ? undefined : sum.tone === "up" ? "in profit" : sum.tone === "down" ? "in loss" : "flat"} sub={sum.missing > 0 ? `${sum.missing} without a live price yet` : undefined} />
      </Stats>
      {list.length > 0 && (
        <ul className="hm-pos-list">
          {list.map((p) => {
            const t = toneOf(p.pnl);
            return (
              <li key={`${p.source}-${p.symbol}`} className="hm-pos-row">
                <span className="hm-pos-sym">{p.symbol}</span>
                <span className="hm-pos-meta">{p.side === "SHORT" ? "Short" : "Long"} · {p.qty} · {p.source}</span>
                <span className={`hm-pos-pnl num ${t}`}>{p.pnl === null ? "—" : `${t === "up" ? "▲ " : t === "down" ? "▼ " : ""}${signedRupees(p.pnl)}`} <small>{pctText(p.pnlPct)}</small></span>
              </li>
            );
          })}
        </ul>
      )}
      {rows.length > SHOW_ROWS && <p className="hm-note">+{rows.length - SHOW_ROWS} more on the trading pages.</p>}
    </Card>
  );
}
