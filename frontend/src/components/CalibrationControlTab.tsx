// frontend/src/components/CalibrationControlTab.tsx  (group 442)
//
// "Real Money Calibration": ONE page to (1) switch the rules of the two real-money services ON / OFF, (2) see the daily trades with entry / exit time and
// profit / loss, (3) run every test, calibration and training job from buttons instead of typed commands, with the txt result kept for download, and
// (4) follow the written Monday / daily / weekly / monthly plan. Two sub-tabs, one per service. Backends: GET/POST /control/* on each trading service and
// calibration-runner-service (nginx /calibration/). Nothing here places a trade.
import { useEffect, useState } from "react";
import { SubTabs } from "./ui/SubTabs";
import { setRealTradeApiUrl } from "../realTradeApi";
import { setPositionStocksApiUrl } from "../positionStocksApi";
import { AUTH_LOST_EVENT, anyToken, deriveServiceUrls, getRunnerUrl, serviceUrl, loginBoth, logoutAll, sessionSecondsLeft, setRunnerUrl, type Svc } from "../calibrationApi";
import SwitchesPanel from "./calibration/SwitchesPanel";
import TradesPanel from "./calibration/TradesPanel";
import RunPlanPanel from "./calibration/RunPlanPanel";
import TestsPanel from "./calibration/TestsPanel";
import RunAllBar from "./calibration/RunAllBar";
import { RunsPanel, RunWindow } from "./calibration/RunsPanel";
import { RunnerBanner, RunnerProvider, useRunner } from "./calibration/RunnerContext";
import { Btn, Card, Msg } from "./calibration/ui";

type Section = "switches" | "trades" | "plan" | "tests" | "runs";

const SVC_ITEMS: { id: Svc; label: string }[] = [{ id: "rt", label: "Real Trade" }, { id: "ps", label: "Position Stocks Service" }];
const SECTIONS: { id: Section; label: string }[] = [
  { id: "switches", label: "Switches" },
  { id: "trades", label: "Daily trades" },
  { id: "plan", label: "Run plan" },
  { id: "tests", label: "Tests & training" },
  { id: "runs", label: "Runs & downloads" },
];

function LoginCard({ onDone }: { onDone: () => void }) {
  const [u, setU] = useState("admin");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    setErr(null);
    const r = await loginBoth(u.trim(), p);
    setBusy(false);
    if (r.error) setErr(r.error);
    else { setP(""); onDone(); }
  };
  return (
    <Card title="Admin login">
      <p className="mb-2 text-mist">Your session has expired or you are not logged in. A session lasts a fixed time after login (30 minutes by default) and is not extended while you use the page. Same admin username and password as Auto Trade. This page changes real-money switches and runs jobs, so it needs the login.</p>
      <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); void go(); }}>
        <label className="text-[11px]"><span className="block text-mist">Username</span><input value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]" /></label>
        <label className="text-[11px]"><span className="block text-mist">Password</span><input type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]" /></label>
        <Btn primary disabled={busy || !p} onClick={() => void go()}>{busy ? "Logging in…" : "Log in"}</Btn>
      </form>
      {err && <div className="mt-2"><Msg kind="err">{err}</Msg></div>}
    </Card>
  );
}

/** group 457: the session is a fixed window, so say so BEFORE it ends (a long run would otherwise log you out in the middle of watching it). */
function SessionNotice() {
  const [left, setLeft] = useState<number | null>(() => sessionSecondsLeft());
  useEffect(() => {
    const id = setInterval(() => setLeft(sessionSecondsLeft()), 15000);
    return () => clearInterval(id);
  }, []);
  if (left == null || left > 600) return null;
  const mins = Math.max(1, Math.ceil(left / 60));
  return (
    <Msg kind="warn">
      Your admin session ends in about {mins} min. It is a fixed time from login and is not extended while you use the page. A run that is already going keeps running on the server;
      after you log in again, open it from "Runs &amp; downloads".
    </Msg>
  );
}

function ConnectionCard() {
  const [url, setUrl] = useState(getRunnerUrl());
  const [rtUrl, setRtUrl] = useState(serviceUrl("rt"));
  const [psUrl, setPsUrl] = useState(serviceUrl("ps"));
  const { reload, error } = useRunner();
  return (
    <details className="rounded-2xl border border-slate bg-graphite p-3 text-[12px]">
      <summary className="cursor-pointer text-[11px] font-bold uppercase tracking-widest text-mist">Connection {error ? "(problem)" : ""}</summary>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="text-[11px]"><span className="block text-mist">Runner URL (calibration-runner-service)</span><input value={url} onChange={(e) => setUrl(e.target.value)} className="w-full rounded-lg sm:w-72 border border-slate bg-transparent px-2 py-1 text-[12px]" /></label>
        <label className="text-[11px]"><span className="block text-mist">Real Trade URL (real-trade-service)</span><input value={rtUrl} onChange={(e) => setRtUrl(e.target.value)} className="w-full rounded-lg sm:w-72 border border-slate bg-transparent px-2 py-1 text-[12px]" /></label>
        <label className="text-[11px]"><span className="block text-mist">Position Stocks URL (position-stocks-service)</span><input value={psUrl} onChange={(e) => setPsUrl(e.target.value)} className="w-full rounded-lg sm:w-72 border border-slate bg-transparent px-2 py-1 text-[12px]" /></label>
        <Btn small onClick={() => { setRunnerUrl(url); setRealTradeApiUrl(rtUrl); setPositionStocksApiUrl(psUrl); window.location.reload(); }}>Save</Btn>
        <Btn small onClick={() => { logoutAll(); window.location.reload(); }}>Log out</Btn>
      </div>
      <p className="mt-1 text-[11px] text-mist">The two trading-service URLs are shared with the Auto Trade and Position Stocks pages (a missing one is filled in from the gateway URL: /realtrade and /positionstocks). Default runner URL: the same host under /calibration.</p>
    </details>
  );
}

function Body() {
  const [svc, setSvc] = useState<Svc>("rt");
  const [section, setSection] = useState<Section>("switches");
  const { start } = useRunner();
  return (
    <div className="space-y-3 mode-frame" data-mode="REAL" data-frame-label="REAL MONEY · these switches change live trading">
      <SubTabs items={SVC_ITEMS} value={svc} onChange={setSvc} label="Service" />
      <SubTabs items={SECTIONS} value={section} onChange={setSection} label="Section" />
      <RunnerBanner />
      <SessionNotice />
      {section !== "runs" && <RunAllBar svc={svc} />}
      {section === "switches" && <SwitchesPanel svc={svc} />}
      {section === "trades" && <TradesPanel svc={svc} onRunReport={() => void start("job", "daily_trades", { days: 10, origin: "all" })} />}
      {section === "plan" && <RunPlanPanel svc={svc} />}
      {section === "tests" && <TestsPanel svc={svc} />}
      {section === "runs" && <RunsPanel />}
      <ConnectionCard />
      <RunWindow />
    </div>
  );
}

export default function CalibrationControlTab() {
  const [authKey, setAuthKey] = useState(0);
  useState(() => { deriveServiceUrls(); return 0; }); // group 445: fill a missing service URL before anything is fetched
  // group 444: any 401 (expired session) clears the tokens and lands here, so the login card replaces the red errors
  useEffect(() => {
    const h = () => setAuthKey((k) => k + 1);
    window.addEventListener(AUTH_LOST_EVENT, h);
    return () => window.removeEventListener(AUTH_LOST_EVENT, h);
  }, []);
  if (!anyToken()) return <LoginCard onDone={() => setAuthKey((k) => k + 1)} />;
  return (
    <RunnerProvider authKey={String(authKey)}>
      <Body />
    </RunnerProvider>
  );
}
