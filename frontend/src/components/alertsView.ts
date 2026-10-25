// E157: summary tiles for the Alerts (notification channels) tab (pure, no React: tested by plain node in alertsView.test.mjs).
// Read from the channel config the tab already holds. Only yes / no / counts are shown: never a token, webhook or chat id.
// A missing flag is "not reported", never "off". Nothing animates a number.
export type AlertsKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
type Ch = { configured?: boolean; enabled?: boolean } | null | undefined;
export type AlertsConfig = {
  telegram?: Ch; discord?: Ch; slack?: Ch;
  callmebot?: (NonNullable<Ch> & { recipients_count?: number }) | null;
  persisted?: boolean;
};

const NAMES: [keyof Omit<AlertsConfig, "persisted">, string][] = [["telegram", "Telegram"], ["discord", "Discord"], ["slack", "Slack"], ["callmebot", "CallMeBot"]];
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function alertsKpis(c: AlertsConfig | null | undefined): AlertsKpi[] {
  if (!c) return [];
  const connected = NAMES.filter(([k]) => c[k]?.configured === true).map(([, n]) => n);
  const on = NAMES.filter(([k]) => c[k]?.configured === true && c[k]?.enabled === true).map(([, n]) => n);
  const off = connected.filter((n) => !on.includes(n));

  const channels: AlertsKpi = connected.length
    ? { label: "Channels", value: `${connected.length}/${NAMES.length}`, sub: connected.join(" · "), tone: "info" }
    : { label: "Channels", value: `0/${NAMES.length}`, sub: "No channel connected - no alerts will be sent", tone: "warn" };

  const sending: AlertsKpi = on.length
    ? { label: "Switched on", value: String(on.length), sub: off.length ? `${on.join(" · ")} (off: ${off.join(", ")})` : on.join(" · "), tone: "info" }
    : connected.length
      ? { label: "Switched on", value: "0", sub: `Connected but switched off: ${off.join(", ")}`, tone: "warn" }
      : { label: "Switched on", value: "0", sub: "nothing to switch on yet", tone: "flat" };

  const cb = c.callmebot;
  const rec = num(cb?.recipients_count);
  const voice: AlertsKpi = !cb || cb.configured !== true
    ? { label: "Voice call", value: "Not set up", sub: "CallMeBot rings you on urgent alerts", tone: "flat" }
    : cb.enabled === true
      ? { label: "Voice call", value: "On", sub: rec != null ? `${rec} recipient${rec === 1 ? "" : "s"}` : "recipient count not reported", tone: "info" }
      : { label: "Voice call", value: "Off", sub: "set up, but switched off", tone: "flat" };

  const saved: AlertsKpi = c.persisted === true
    ? { label: "Settings stored", value: "Saved", sub: "kept on the backend, survives restarts", tone: "info" }
    : c.persisted === false
      ? { label: "Settings stored", value: "Memory only", sub: "lost on restart - set CACHE_DATABASE_URL on the notification service", tone: "warn" }
      : { label: "Settings stored", value: "—", sub: "not reported", tone: "flat" };

  return [channels, sending, voice, saved];
}
