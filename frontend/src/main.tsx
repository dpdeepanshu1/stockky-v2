import React, { Suspense, lazy } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { installGatewayAuth } from "./gatewayAuth";
import { getApiUrl } from "./api";
import { reloadOnceForNewVersion } from "./chunkReload";
import "./styles/base.css"; // round 532: Tailwind + former index.css + folded design layers (renamed, same cascade order; motion is opt-in)
import "./styles/tokens.css"; // E513: calm palette, one font, flat surfaces
import "./styles/components.css"; // E534: component kit (k- classes)
import "./styles/shell.css"; // E558 (F4): the five page sheets are imported here, in the order pages.css used to give them
import "./styles/home.css";
import "./styles/trading.css";
import "./styles/control.css";
import "./styles/markets.css";

// TV / 10-foot mode: auto-detected from the browser's user agent (Tizen, webOS,
// Android TV, Fire TV, Chromecast, …). Force it on/off with ?tv=1 / ?tv=0 —
// the choice is remembered. It only adds the `tv` class used by styles/base.css.
try {
  const q = new URLSearchParams(window.location.search).get("tv");
  if (q === "1" || q === "0") window.localStorage.setItem("stockky_tv", q);
  const pref = window.localStorage.getItem("stockky_tv");
  const uaTv = /SmartTV|SMART-TV|Tizen|Web0S|WebOS|NetCast|HbbTV|BRAVIA|VIDAA|CrKey|GoogleTV|Android ?TV|AppleTV|AFT[A-Z]|Roku|PhilipsTV/i.test(navigator.userAgent || "");
  if (pref === "1" || (pref !== "0" && uaTv)) document.documentElement.classList.add("tv");
} catch { /* storage blocked — stay in normal mode */ }

// group 460: lets styles/base.css pause looping animations while the browser tab is hidden
try {
  const markHidden = () => document.documentElement.setAttribute("data-hidden", String(document.hidden));
  document.addEventListener("visibilitychange", markHidden);
  markHidden();
} catch { /* no document */ }

// E148 (F-09): apply the saved "Animations: Off" choice at start-up, so it holds on every tab (the Dashboard has no hero with the switch)
try { if (window.localStorage.getItem("stockky:v6_calm") === "1") document.documentElement.classList.add("anim-off"); } catch { /* storage blocked: animations stay on */ }

// E564: Vite fires this when a lazy page's file (or its CSS) cannot be preloaded, which is what an old tab sees after a redeploy: load the new build once
try { window.addEventListener("vite:preloadError", (e) => { e.preventDefault(); reloadOnceForNewVersion(); }); } catch { /* no window */ }

installGatewayAuth(getApiUrl); // group 453: adds the admin session token to the gateway's protected routes

// E534: ?kit=1 opens the component-kit preview page instead of the app (no service calls; the app itself is untouched without the flag)
const KitPreview = lazy(() => import("./components/kit/KitPreview"));
const showKit = (() => { try { return new URLSearchParams(window.location.search).get("kit") === "1"; } catch { return false; } })();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {showKit ? <Suspense fallback={null}><KitPreview /></Suspense> : <App />}
  </React.StrictMode>
);
