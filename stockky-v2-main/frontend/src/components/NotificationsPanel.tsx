import { useEffect, useState } from "react";
import { api, NotificationConfig } from "../api";

type Channel = "telegram" | "discord" | "slack" | "callmebot";

interface ChannelMeta {
  label: string;
  blurb: string;
  setupUrl: string;
  setupLabel: string;
}

const CHANNEL_META: Record<Channel, ChannelMeta> = {
  telegram: {
    label: "Telegram",
    blurb: "Free bot, no billing account. Best for instant phone alerts.",
    setupUrl: "https://core.telegram.org/bots#how-do-i-create-a-bot",
    setupLabel: "How to create a bot ->",
  },
  discord: {
    label: "Discord",
    blurb: "Free incoming webhook on any server/channel you own.",
    setupUrl: "https://support.discord.com/hc/en-us/articles/228383668",
    setupLabel: "How to get a webhook URL ->",
  },
  slack: {
    label: "Slack",
    blurb: "Free incoming webhook via a Slack app.",
    setupUrl: "https://api.slack.com/messaging/webhooks",
    setupLabel: "How to set up Slack webhooks ->",
  },
  callmebot: {
    label: "CallMeBot (Voice / Call alert)",
    blurb: "Uses your Telegram @username from CallMeBot (e.g. @dpdeep29). Optional API key only if CallMeBot gave you one. Supports up to 5 users.",
    setupUrl: "https://www.callmebot.com/telegram-call-api/",
    setupLabel: "CallMeBot call API guide ->",
  },
};

export default function NotificationsPanel() {
  const [config, setConfig] = useState<NotificationConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Channel | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  const [telegramToken, setTelegramToken] = useState("");
  const [telegramChatId, setTelegramChatId] = useState("");
  const [discordUrl, setDiscordUrl] = useState("");
  const [slackUrl, setSlackUrl] = useState("");
  const [callUser, setCallUser] = useState("");
  const [callKey, setCallKey] = useState("");
  const [callUsers, setCallUsers] = useState("");
  const [testingCall, setTestingCall] = useState(false);

  function load() {
    setLoading(true);
    setLoadError(null);
    api
      .getNotificationConfig()
      .then((cfg) => {
        setConfig(cfg);
        setTelegramChatId(cfg.telegram.chat_id || "");
        const cm = cfg.callmebot as { users?: string; users_preview?: string; user?: string };
        const extras = (cm?.users || cm?.users_preview || "").trim();
        if (extras) setCallUsers(extras);
      })
      .catch((e) => setLoadError((e as Error).message))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function saveChannel(channel: Channel) {
    setSaving(channel);
    setTestResult(null);
    try {
      let update: Parameters<typeof api.saveNotificationConfig>[0] = {};
      let extras: string[] = [];
      if (channel === "telegram") {
        update = {
          telegram_bot_token: telegramToken || undefined,
          telegram_chat_id: telegramChatId || undefined,
          enabled: { telegram: true },
        };
      } else if (channel === "discord") {
        update = { discord_webhook_url: discordUrl || undefined, enabled: { discord: true } };
      } else if (channel === "callmebot") {
        let user = (callUser || "").trim();
        if (!user) {
          user = ((config?.callmebot as any)?.user || "").trim();
        }
        if (!user) {
          setTestResult("Enter your CallMeBot Telegram user first (e.g. @dpdeep29).");
          window.alert("Enter CallMeBot user like @dpdeep29 before Connect & start.");
          return;
        }
        if (!user.startsWith("@") && !/^[0-9+]+$/.test(user)) {
          user = "@" + user;
        }
        // Normalize extras: ensure @ prefix, max 5 including primary
        extras = (callUsers || "")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => {
            if (s.startsWith("@") || /^[0-9+]+$/.test(s) || s.includes(":")) return s;
            return "@" + s;
          })
          .slice(0, 4); // primary + 4 extras = 5
        update = {
          callmebot_user: user,
          callmebot_apikey: callKey || undefined,
          callmebot_users: extras.length ? extras.join(",") : "",
          enabled: { callmebot: true },
        };
      } else {
        update = { slack_webhook_url: slackUrl || undefined, enabled: { slack: true } };
      }
      const cfg = await api.saveNotificationConfig(update);
      setConfig(cfg);
      if (channel === "telegram") setTelegramToken("");
      if (channel === "discord") setDiscordUrl("");
      if (channel === "slack") setSlackUrl("");
      if (channel === "callmebot") {
        setCallUser("");
        setCallKey("");
        const savedExtras = (cfg.callmebot as any)?.users || (cfg.callmebot as any)?.users_preview || extras.join(",");
        setCallUsers(savedExtras || extras.join(","));
        setTestResult(
          `CallMeBot saved & enabled for ${(cfg.callmebot as any)?.user || callUser || "user"}. Status: ${
            cfg.callmebot?.configured ? "CONNECTED" : "not connected — check username"
          }.`
        );
        window.alert(
          cfg.callmebot?.configured
            ? `CallMeBot connected successfully.\nUser: ${(cfg.callmebot as any)?.user || callUser}\nYou can press Test CallMeBot now.`
            : "Saved, but backend still reports not connected. Check the username format (@dpdeep29) and that notification service is up."
        );
      } else {
        setTestResult(`${channel} saved successfully.`);
      }
    } catch (e) {
      const msg = (e as Error).message || "Save failed";
      setLoadError(msg);
      setTestResult(`Save failed: ${msg}`);
      window.alert(`Could not save ${channel}: ${msg}`);
    } finally {
      setSaving(null);
    }
  }

  async function toggleChannel(channel: Channel, enabled: boolean) {
    try {
      const cfg = await api.saveNotificationConfig({ enabled: { [channel]: enabled } });
      setConfig(cfg);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }

  async function disconnectChannel(channel: Channel) {
    try {
      const cfg = await api.clearNotificationChannel(channel);
      setConfig(cfg);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }

  async function runTest() {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.testNotifications();
      if (!r.delivered) {
        setTestResult(r.note || "No channel is configured and turned on yet.");
      } else {
        const summary = Object.entries(r.results || {})
          .map(([ch, status]) => `${ch}: ${status}`)
          .join(" - ");
        setTestResult(`Sent -- ${summary}`);
      }
    } catch (e) {
      setTestResult((e as Error).message);
    } finally {
      setTesting(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-2xl border border-slate bg-graphite p-8">
        <p className="font-display tabular-nums text-xs text-mist">Loading notification settings...</p>
      </div>
    );
  }

  if (loadError && !config) {
    return (
      <div className="rounded-2xl border border-signal-sell/40 bg-signal-sell/5 p-6">
        <p className="font-display tabular-nums text-xs text-signal-sell/70 uppercase tracking-widest mb-1">
          Couldn't load notification settings
        </p>
        <p className="text-sm text-signal-sell mb-3">{loadError}</p>
        <button onClick={load} className="font-display tabular-nums text-xs text-mist hover:text-paper underline">
          Retry
        </button>
      </div>
    );
  }

  if (!config) return null;

  return (
    <div className="space-y-5 alerts-terminal">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="font-display text-xl mb-1 text-signal-prepare/90">Notification channels</h2>
          <p className="text-xs text-mist/60 mb-2">Voice-first CallMeBot on urgent alerts · outbox retry · Telegram / Discord / Slack</p>
          <p className="text-mist text-sm max-w-lg">
            Connect a free channel to get pinged on new BUY NOW calls and SELL flips. Everything
            here is configured from the page -- no code or redeploy needed.
            {config.persisted ? (
              <span className="block mt-1 text-signal-buy text-xs opacity-80">
                Config is saved on the backend (Neon) — survives restarts.
              </span>
            ) : (
              <span className="block mt-1 text-signal-hold">
                Config is only in memory right now — set CACHE_DATABASE_URL on the
                notification service so Telegram / CallMeBot settings survive restarts.
              </span>
            )}
          </p>
        </div>
        <button
          onClick={runTest}
          disabled={testing}
          className="border border-slate rounded-xl px-4 py-2.5 font-display tabular-nums text-xs text-mist hover:text-paper hover:border-mist transition whitespace-nowrap disabled:opacity-50"
        >
          {testing ? "Sending..." : "Send test notification"}
        </button>
      </div>

      {testResult && (
        <div className="rounded-xl border border-slate bg-graphite px-4 py-3 font-display tabular-nums text-xs text-mist">
          {testResult}
        </div>
      )}

      <ChannelCard
        channel="telegram"
        connected={config.telegram.configured}
        enabled={config.telegram.enabled}
        masked={config.telegram.masked}
        onToggle={(v) => toggleChannel("telegram", v)}
        onDisconnect={() => disconnectChannel("telegram")}
        saving={saving === "telegram"}
        onSave={() => saveChannel("telegram")}
      >
        <Field
          label="Bot token"
          placeholder={config.telegram.configured ? `Saved: ${config.telegram.masked}` : "123456:ABC-DEF..."}
          value={telegramToken}
          onChange={setTelegramToken}
          type="password"
        />
        <Field
          label="Chat ID"
          placeholder="e.g. 123456789"
          value={telegramChatId}
          onChange={setTelegramChatId}
        />
      </ChannelCard>

      <ChannelCard
        channel="discord"
        connected={config.discord.configured}
        enabled={config.discord.enabled}
        masked={config.discord.masked}
        onToggle={(v) => toggleChannel("discord", v)}
        onDisconnect={() => disconnectChannel("discord")}
        saving={saving === "discord"}
        onSave={() => saveChannel("discord")}
      >
        <Field
          label="Webhook URL"
          placeholder={config.discord.configured ? `Saved: ${config.discord.masked}` : "https://discord.com/api/webhooks/..."}
          value={discordUrl}
          onChange={setDiscordUrl}
          type="password"
        />
      </ChannelCard>

      <ChannelCard
        channel="slack"
        connected={config.slack.configured}
        enabled={config.slack.enabled}
        masked={config.slack.masked}
        onToggle={(v) => toggleChannel("slack", v)}
        onDisconnect={() => disconnectChannel("slack")}
        saving={saving === "slack"}
        onSave={() => saveChannel("slack")}
      >
        <Field
          label="Webhook URL"
          placeholder={config.slack.configured ? `Saved: ${config.slack.masked}` : "https://hooks.slack.com/services/..."}
          value={slackUrl}
          onChange={setSlackUrl}
          type="password"
        />
      </ChannelCard>

      <ChannelCard
        channel="callmebot"
        connected={Boolean(config.callmebot?.configured)}
        enabled={Boolean(config.callmebot?.enabled)}
        masked={config.callmebot?.masked || ""}
        onToggle={(v) => toggleChannel("callmebot", v)}
        onDisconnect={() => disconnectChannel("callmebot")}
        saving={saving === "callmebot"}
        onSave={() => saveChannel("callmebot")}
      >
        <Field
          label="Primary Telegram user"
          placeholder={
            (config.callmebot as any)?.user
              ? `Saved: ${(config.callmebot as any).user}`
              : "e.g. @dpdeep29"
          }
          value={callUser}
          onChange={setCallUser}
        />
        <Field
          label="API key (optional — leave blank if CallMeBot only gave you @username)"
          placeholder={config.callmebot?.configured && config.callmebot.masked !== "none" ? `Saved: ${config.callmebot.masked}` : "Usually not needed"}
          value={callKey}
          onChange={setCallKey}
          type="password"
        />
        <Field
          label="Extra users (max 5 total, comma-separated)"
          placeholder="@friend2,@friend3 or @user:optionalkey"
          value={callUsers}
          onChange={setCallUsers}
        />
        <button
          type="button"
          disabled={testingCall}
          onClick={async () => {
            setTestingCall(true);
            setTestResult(null);
            try {
              // Save first if user typed credentials but not connected yet
              if (!config.callmebot?.configured && (callUser || "").trim()) {
                let user = callUser.trim();
                if (!user.startsWith("@") && !/^[0-9+]+$/.test(user)) user = "@" + user;
                let extrasSave = (callUsers || "").split(",").map((s) => s.trim()).filter(Boolean).slice(0, 4);
                const cfg = await api.saveNotificationConfig({
                  callmebot_user: user,
                  callmebot_apikey: callKey || undefined,
                  callmebot_users: extrasSave.length
                    ? extrasSave.join(",")
                    : callUsers || undefined,
                  enabled: { callmebot: true },
                });
                setConfig(cfg);
              }
              const r = await api.testCallMeBot(
                "Stockky test call. If you hear or see this, CallMeBot is connected."
              );
              if (r.ok) {
                const detail = r.result || "sent";
                const msg =
                  "CallMeBot test finished.\n\n" +
                  detail +
                  "\n\nEach extra user must activate CallMeBot once in Telegram (same bot you used). Only then will they receive alerts.";
                setTestResult(detail);
                window.alert(msg);
              } else {
                const msg =
                  `CallMeBot failed: ${r.result || r.error || "unknown"}\n\n` +
                  "Primary and extras each need CallMeBot activated on their Telegram account.";
                setTestResult(msg);
                window.alert(msg);
              }
            } catch (e) {
              const msg = (e as Error).message || "Test failed";
              setTestResult(msg);
              window.alert(`CallMeBot test error: ${msg}`);
            } finally {
              setTestingCall(false);
            }
          }}
          className="mt-2 font-display tabular-nums text-xs px-3 py-2 rounded-xl border border-signal-buy/40 text-signal-buy hover:bg-signal-buy/15 disabled:opacity-40"
        >
          {testingCall ? "Calling…" : "📞 Test CallMeBot"}
        </button>
        <p className="text-[10px] text-mist/60 mt-2 font-display tabular-nums leading-relaxed">
          Primary example: <span className="text-signal-buy">@dpdeep29</span>. Extra users must each open CallMeBot in Telegram and start the bot once — otherwise only you get the call.
          Auto-alerts on <span className="text-signal-buy">BUY NOW</span> go to all configured users.
        </p>
      </ChannelCard>
    </div>
  );
}

function ChannelCard({
  channel,
  connected,
  enabled,
  masked,
  onToggle,
  onDisconnect,
  onSave,
  saving,
  children,
}: {
  channel: Channel;
  connected: boolean;
  enabled: boolean;
  masked: string;
  onToggle: (v: boolean) => void;
  onDisconnect: () => void;
  onSave: () => void;
  saving: boolean;
  children: React.ReactNode;
}) {
  const meta = CHANNEL_META[channel];
  return (
    <div className="rounded-2xl border border-slate bg-graphite p-5">
      <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className="font-display tabular-nums text-sm text-paper">{meta.label}</span>
            <StatusPill connected={connected} enabled={enabled} />
          </div>
          <p className="text-mist/70 text-xs max-w-sm">{meta.blurb}</p>
          <a
            href={meta.setupUrl}
            target="_blank"
            rel="noreferrer"
            className="font-display tabular-nums text-[11px] text-signal-prepare hover:text-paper transition inline-block mt-1"
          >
            {meta.setupLabel}
          </a>
        </div>

        {connected && (
          <div className="flex items-center gap-3 shrink-0">
            <span className="font-display tabular-nums text-[10px] text-mist/50">{masked}</span>
            <ToggleSwitch checked={enabled} onChange={onToggle} label={enabled ? "Started" : "Stopped"} />
            <button
              onClick={onDisconnect}
              className="font-display tabular-nums text-[10px] uppercase tracking-widest text-mist/50 hover:text-signal-sell transition"
            >
              Disconnect
            </button>
          </div>
        )}
      </div>

      <div className="grid sm:grid-cols-2 gap-3 items-end">
        {children}
        <button
          onClick={onSave}
          disabled={saving}
          className="border border-slate rounded-xl px-4 py-2.5 font-display tabular-nums text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition disabled:opacity-50 h-fit"
        >
          {saving ? "Saving..." : connected ? "Update & start" : "Connect & start"}
        </button>
      </div>
    </div>
  );
}

function StatusPill({ connected, enabled }: { connected: boolean; enabled: boolean }) {
  if (!connected) {
    return (
      <span className="font-display tabular-nums text-[10px] uppercase tracking-widest text-mist/40 border border-slate rounded-full px-2 py-0.5">
        Not connected
      </span>
    );
  }
  return enabled ? (
    <span className="font-display tabular-nums text-[10px] uppercase tracking-widest text-signal-buy border border-signal-buy/40 bg-signal-buy/10 rounded-full px-2 py-0.5">
      Running
    </span>
  ) : (
    <span className="font-display tabular-nums text-[10px] uppercase tracking-widest text-signal-hold border border-signal-hold/40 bg-signal-hold/10 rounded-full px-2 py-0.5">
      Stopped
    </span>
  );
}

function ToggleSwitch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 group"
      title={checked ? "Stop this channel" : "Start this channel"}
    >
      <span
        className={`relative w-9 h-5 rounded-full transition-colors ${
          checked ? "bg-signal-buy/70" : "bg-slate"
        }`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-paper transition-transform ${
            checked ? "translate-x-4" : ""
          }`}
        />
      </span>
      <span className="font-display tabular-nums text-[10px] text-mist group-hover:text-paper transition">{label}</span>
    </button>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: "text" | "password";
}) {
  return (
    <label className="block">
      <span className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-1 block">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        className="w-full bg-ink/60 border border-slate rounded-xl px-3 py-2.5 font-display tabular-nums text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
      />
    </label>
  );
}
