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

// E167: what the Save buttons accept (pure). The old Telegram / Discord / Slack Save sent whatever was typed, or nothing, then said
// "saved successfully" and switched the channel on even when the backend still reported it as not connected. These checks look at the
// SHAPE only and never echo a token or URL back in a message. An empty field on an already-connected channel just re-enables it.
export type ChannelFields = { telegramToken?: string; telegramChatId?: string; discordUrl?: string; slackUrl?: string };
export type ChannelConnected = { telegram?: boolean; discord?: boolean; slack?: boolean };
export type ChannelCheck = { ok: true } | { ok: false; message: string };
const bad = (message: string): ChannelCheck => ({ ok: false, message });
function hostPath(url: string): { host: string; path: string } | null {
  try { const u = new URL(url); return u.protocol === "https:" && !u.username && !u.password ? { host: u.hostname.toLowerCase(), path: u.pathname } : null; } catch { return null; }
}
export function checkChannelInput(channel: string, f: ChannelFields, connected: ChannelConnected): ChannelCheck {
  if (channel === "telegram") {
    const token = (f.telegramToken ?? "").trim(), chat = (f.telegramChatId ?? "").trim();
    if (!token && !connected.telegram) return bad("Paste the Telegram bot token first (from @BotFather).");
    if (token && !/^\d{5,15}:[A-Za-z0-9_-]{30,}$/.test(token)) return bad("That does not look like a Telegram bot token (it is digits, a colon, then letters and digits).");
    if (!chat && !connected.telegram) return bad("Enter the Telegram chat id first.");
    if (chat && !/^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$/.test(chat)) return bad("The Telegram chat id is a number (it may start with -) or a @channelname.");
    return { ok: true };
  }
  if (channel === "discord" || channel === "slack") {
    const raw = ((channel === "discord" ? f.discordUrl : f.slackUrl) ?? "").trim();
    const name = channel === "discord" ? "Discord" : "Slack";
    if (!raw) return connected[channel] ? { ok: true } : bad(`Paste the ${name} webhook URL first.`);
    const hp = hostPath(raw);
    if (!hp) return bad(`The ${name} webhook must be a full https:// address.`);
    const hostOk = channel === "discord" ? /^((ptb|canary)\.)?(discord|discordapp)\.com$/.test(hp.host) && hp.path.startsWith("/api/webhooks/") : hp.host === "hooks.slack.com" && hp.path.startsWith("/services/");
    return hostOk ? { ok: true } : bad(`That is not a ${name} webhook address (${channel === "discord" ? "discord.com/api/webhooks/..." : "hooks.slack.com/services/..."}).`);
  }
  return { ok: true };
}
export function saveOutcome(channel: string, cfg: { [k: string]: { configured?: boolean } | undefined } | null | undefined): { text: string; connected: boolean } {
  const name = channel === "telegram" ? "Telegram" : channel === "discord" ? "Discord" : channel === "slack" ? "Slack" : channel;
  const connected = cfg?.[channel]?.configured === true;
  return connected
    ? { text: `${name} saved and connected.`, connected: true }
    : { text: `${name} saved, but the backend still reports it as not connected. Check the values and that the notification service is up.`, connected: false };
}
