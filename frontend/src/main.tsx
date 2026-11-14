import React, { Suspense, lazy } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { installGatewayAuth } from "./gatewayAuth";
import { getApiUrl } from "./api";
import "./styles/base.css"; // round 8i: Tailwind entry + live rules of the former index.css
import "./styles/legacy.css"; // round 8i: live rules of the former design-v3..v8, auth-login, light-contrast and alignment layers, folded in their old order
import "./styles/tokens.css"; // E513: calm palette, one font, flat surfaces (delete this line to roll back)
import "./styles/components.css"; // E534: component kit styles (k- classes; delete this line and components/kit to roll back)
import "./styles/shell.css"; // E535: shell parts (sh- classes: sidebar sections, mode strip, session pill, phone Trading sheet; loads last; delete this line to roll back the styling)
import "./styles/home.css"; // E536: Home + stock decision top (hm- / dt- classes; loads last; delete this line to roll back)
import "./styles/trading.css"; // E537: trading page header and grouped tabs (tr- classes; loads last; delete this line to roll back)
import "./styles/control.css"; // E539: F&O option chain, engine status row, Calibration side list (oc- / cx- classes; loads last; delete this line to roll back)
import "./styles/markets.css"; // E540: summary tiles on the kit, watchlist order, Paper Trades tabs (mk- classes; loads last; delete this line to roll back)

// TV / 10-foot mode: auto-detected from the browser's user agent (Tizen, webOS,
// Android TV, Fire TV, Chromecast, …). Force it on/off with ?tv=1 / ?tv=0 —
// the choice is remembered. It only adds the `tv` class used by styles/legacy.css.
try {
  const q = new URLSearchParams(window.location.search).get("tv");
  if (q === "1" || q === "0") window.localStorage.setItem("stockky_tv", q);
  const pref = window.localStorage.getItem("stockky_tv");
  const uaTv = /SmartTV|SMART-TV|Tizen|Web0S|WebOS|NetCast|HbbTV|BRAVIA|VIDAA|CrKey|GoogleTV|Android ?TV|AppleTV|AFT[A-Z]|Roku|PhilipsTV/i.test(navigator.userAgent || "");
  if (pref === "1" || (pref !== "0" && uaTv)) document.documentElement.classList.add("tv");
} catch { /* storage blocked — stay in normal mode */ }

// group 460: lets styles/legacy.css pause looping animations while the browser tab is hidden
try {
  const markHidden = () => document.documentElement.setAttribute("data-hidden", String(document.hidden));
  document.addEventListener("visibilitychange", markHidden);
  markHidden();
} catch { /* no document */ }

// E148 (F-09): apply the saved "Animations: Off" choice at start-up, so it holds on every tab (the Dashboard has no hero with the switch)
try { if (window.localStorage.getItem("stockky:v6_calm") === "1") document.documentElement.classList.add("v6-calm"); } catch { /* storage blocked: animations stay on */ }

installGatewayAuth(getApiUrl); // group 453: adds the admin session token to the gateway's protected routes

// E534: ?kit=1 opens the component-kit preview page instead of the app (no service calls; the app itself is untouched without the flag)
const KitPreview = lazy(() => import("./components/kit/KitPreview"));
const showKit = (() => { try { return new URLSearchParams(window.location.search).get("kit") === "1"; } catch { return false; } })();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {showKit ? <Suspense fallback={null}><KitPreview /></Suspense> : <App />}
  </React.StrictMode>
);
