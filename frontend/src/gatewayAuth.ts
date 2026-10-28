// group 453: the gateway's dangerous routes (alert settings, power-off, circuit reset, overnight config/run, resume, training, feed hard-reset)
// now need the admin session token the real-trade login already stored. Several screens call them with a bare fetch(), so instead of editing
// every call site the token is added here, in one place, and ONLY for those paths (every other gateway call goes out unchanged: no extra CORS
// preflight, and the token is never sent anywhere else).
// No imports on purpose: this file is tested by plain node (src/gatewayAuth.test.mjs).
const TOKEN_KEY = "stockky:real_trade_session_token";
// E241: the gateway accepts the token of either login service (admin_guard._ACCEPT). Someone logged in only on Position Stocks has no real-trade token,
// so the gateway calls (training, alerts...) went out bare and got a 401. Fall back to the Position Stocks token.
const TOKEN_KEY_FALLBACK = "stockky:position_stocks_session_token";
const ADMIN_PATH =
  /^\/(?:notifications\/(?:config|call\/me|test|send-picks)|training\/train|ops\/(?:power-off|circuit-reset|overnight\/(?:config|run)|resume-activity|qstash\/publish)|(?:data-)?feed\/(?:hard-reset|purge-over-cap)|(?:surprise\/)?ipo\/(?:notify-top-picks|purge-non-equity)|(?:surprise|stockky-hot)\/notify-top-picks|scan\/universe\/cache)(?:[/?#]|$)/;
// E148 (F-12): the training service's own system-level actions, reached through the gateway's /training/* proxy. Mirrors admin_guard._TRAINING_ADMIN
// (exact paths only, so the progress poll /training/api/train/progress goes out unchanged).
const ADMIN_TRAINING_PATH =
  /^\/training\/(?:api\/(?:train|lock\/clear|models\/promote\/[^/?#]+|admin(?:\/[^?#]*)?)|train\/(?:run|clear-lock)|lock|debug\/abort)\/?(?:[?#]|$)/;

/** True when `url` is a gateway call to one of the protected paths. `gatewayBase` is "/api", "https://host" or "" (page origin). */
export function isGatewayAdminUrl(url: string, gatewayBase: string): boolean {
  const base = (gatewayBase || "").replace(/\/$/, "");
  if (!url.startsWith(base + "/")) return false;
  let path = url.slice(base.length);
  if (path.startsWith("/api/")) path = path.slice(4); // the gateway also answers under /api/... (aliases)
  return ADMIN_PATH.test(path) || ADMIN_TRAINING_PATH.test(path);
}

let installed = false;

/** Wraps window.fetch once. `getBase` returns the live gateway base URL (it can change in Settings). */
export function installGatewayAuth(getBase: () => string): void {
  if (installed || typeof window === "undefined" || typeof window.fetch !== "function") return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    try {
      const isReq = typeof Request !== "undefined" && input instanceof Request;
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : isReq ? (input as Request).url : String(input);
      if (isGatewayAdminUrl(url, getBase())) {
        const token = window.localStorage.getItem(TOKEN_KEY) || window.localStorage.getItem(TOKEN_KEY_FALLBACK);
        if (token) {
          const headers = new Headers(init?.headers ?? (isReq ? (input as Request).headers : undefined));
          if (!headers.has("Authorization")) {
            headers.set("Authorization", `Bearer ${token}`);
            return original(input, { ...init, headers });
          }
        }
      }
    } catch {
      /* never let the auth helper break a request */
    }
    return original(input, init);
  };
}
