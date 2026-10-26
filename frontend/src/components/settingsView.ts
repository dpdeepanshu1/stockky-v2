// E158: summary tiles for the Settings tab (pure, no React: tested by plain node in settingsView.test.mjs).
// Read from the state the page already holds (backend check, API address box, theme, database wake). Only the host of the
// address is shown, never a path or query. An unsaved edit and a bad address are said in words. Nothing animates a number.
export type SettingsKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
export type SettingsInput = {
  backendUp: "checking" | "up" | "down";
  url: string; savedUrl: string;
  theme: "dark" | "light";
  dbWake: "idle" | "waking" | "ok" | "error"; dbDetail?: string | null;
};

/** Host of an http(s) address, "" when the text is empty, null when it is not a usable full address. */
export function apiHost(raw: string | null | undefined): string | null {
  const t = (raw ?? "").trim();
  if (!t) return "";
  try {
    const u = new URL(t);
    return u.protocol === "http:" || u.protocol === "https:" ? u.host : null;
  } catch { return null; }
}

export function settingsKpis(i: SettingsInput | null | undefined): SettingsKpi[] {
  if (!i) return [];
  const saved = apiHost(i.savedUrl);
  const typed = apiHost(i.url);
  const edited = (i.url ?? "").trim() !== (i.savedUrl ?? "").trim();

  const backend: SettingsKpi = i.backendUp === "up"
    ? { label: "Backend", value: "Connected", sub: saved ? saved : "same origin", tone: "info" }
    : i.backendUp === "down"
      ? { label: "Backend", value: "Unreachable", sub: saved ? `${saved} did not answer` : "no answer from this origin", tone: "warn" }
      : { label: "Backend", value: "Checking", sub: "waiting for the first answer", tone: "flat" };

  const address: SettingsKpi = typed === null
    ? { label: "API address", value: "Not valid", sub: "needs a full address starting with https://", tone: "warn" }
    : edited
      ? { label: "API address", value: typed || "Same origin", sub: "Edited, not saved - press Save URL", tone: "warn" }
      : { label: "API address", value: saved || "Same origin", sub: saved ? "saved on this device" : "no API URL set", tone: "info" };

  const theme: SettingsKpi = { label: "Theme", value: i.theme === "light" ? "Light" : "Dark", sub: "this device", tone: "flat" };

  const detail = (i.dbDetail ?? "").trim();
  const db: SettingsKpi = i.dbWake === "waking" ? { label: "Databases", value: "Waking", sub: "waiting for the databases", tone: "info" }
    : i.dbWake === "ok" ? { label: "Databases", value: "Awake", sub: detail || "all targets answered", tone: "info" }
    : i.dbWake === "error" ? { label: "Databases", value: "Wake failed", sub: detail ? detail.slice(0, 80) : "no detail from the backend", tone: "warn" }
    : { label: "Databases", value: "Not checked", sub: "wakes on page load; press Wake DB to force", tone: "flat" };

  return [backend, address, theme, db];
}

// E168: Save URL and Power Off (pure). Save URL stored any text (setApiUrl normalises a missing scheme but nothing refused junk such as
// "hello world" or "ftp://x"), which then broke every request until fixed. The strip now reads the same normalised address Save would
// store, so "my-vm.duckdns.org" is no longer called "Not valid" while Save accepts it. Power Off used to say "Processes stopped. Ready for
// fresh start" and reload even when the request had failed (or went to the wrong host); the report now follows the real answer.
export type UrlCheck = { ok: true } | { ok: false; message: string };
export function checkApiUrl(normalized: string | null | undefined): UrlCheck {
  return apiHost(normalized) === null
    ? { ok: false, message: "That is not a usable address. Use the full gateway address, for example https://my-gateway.example.com." }
    : { ok: true };
}
export function powerOffReport(r: { ok: boolean; message?: unknown; phases?: unknown; error?: unknown }): { ok: boolean; text: string } {
  if (!r.ok) {
    const why = typeof r.error === "string" && r.error.trim() ? r.error.trim() : "no answer from the backend";
    return { ok: false, text: `Power-off did not go through: ${why}.\nNothing was confirmed stopped. Check the Backend status and try again.` };
  }
  const head = typeof r.message === "string" && r.message.trim() ? r.message.trim() : "Processes stopped.";
  const lines = Array.isArray(r.phases)
    ? (r.phases as any[]).filter((p) => p && typeof p.phase === "string").map((p) => `${p.phase}: ${typeof p.detail === "string" ? p.detail : "—"}`)
    : [];
  return { ok: true, text: `${head}${lines.length ? `\n\n${lines.join("\n")}` : ""}\n\nReady for fresh start. Refreshing…` };
}
