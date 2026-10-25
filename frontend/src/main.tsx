import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import { installGatewayAuth } from "./gatewayAuth";
import { getApiUrl } from "./api";
import "./design-v3.css"; // fresh UI layer (2026-10-16) — loads last so it wins; delete this line to roll back
import "./design-v4.css"; // group 460: fresh-green tokens + motion toolkit (loads after v3); delete this line to roll back
import "./design-v5.css"; // design v5: calmer finance look, AI orb + AI Insights deck (loads after v4; delete this line to roll back)
import "./design-v6.css"; // design v6 (E122): finance + AI hero, KPI tiles, tabs, Trade History, switches for Real Trade / Position Stocks / Calibration (loads after v5; delete this line to roll back)
import "./design-v7.css"; // design v7: risk rail, exposure strip, capital ring, mode ladder, evidence meter, drift beat, armed glow (loads after v6; delete this line to roll back)
import "./auth-login.css"; // group 460: login page, re-login overlay, session chip
import "./light-contrast.css"; // group 464: light-theme button/colour contrast (delete this line to roll back)
import "./alignment.css"; // group 465: layout alignment fixes (loads last; delete this line to roll back)

// TV / 10-foot mode: auto-detected from the browser's user agent (Tizen, webOS,
// Android TV, Fire TV, Chromecast, …). Force it on/off with ?tv=1 / ?tv=0 —
// the choice is remembered. It only adds the `tv` class used by design-v3.css.
try {
  const q = new URLSearchParams(window.location.search).get("tv");
  if (q === "1" || q === "0") window.localStorage.setItem("stockky_tv", q);
  const pref = window.localStorage.getItem("stockky_tv");
  const uaTv = /SmartTV|SMART-TV|Tizen|Web0S|WebOS|NetCast|HbbTV|BRAVIA|VIDAA|CrKey|GoogleTV|Android ?TV|AppleTV|AFT[A-Z]|Roku|PhilipsTV/i.test(navigator.userAgent || "");
  if (pref === "1" || (pref !== "0" && uaTv)) document.documentElement.classList.add("tv");
} catch { /* storage blocked — stay in normal mode */ }

// group 460: lets design-v4.css pause looping animations while the browser tab is hidden
try {
  const markHidden = () => document.documentElement.setAttribute("data-hidden", String(document.hidden));
  document.addEventListener("visibilitychange", markHidden);
  markHidden();
} catch { /* no document */ }

installGatewayAuth(getApiUrl); // group 453: adds the admin session token to the gateway's protected routes

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
