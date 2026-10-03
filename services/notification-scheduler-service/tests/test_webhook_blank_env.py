"""
tests/test_webhook_blank_env.py

Regression for blank / padded Discord and Slack webhook values in
notification/main.py.

Bug: the "configured" and "enabled" checks truth-tested the raw string, so a
whitespace-only value counted as set.
  * DISCORD_WEBHOOK_URL=" " in the environment made ENV_DEFAULTS["enabled"]
    report discord as enabled, and /config reported it as configured, with a
    masked value of spaces.
  * A padded env value (" https://... ") was kept padded, so the stored default
    and the URL handed to httpx.post carried the spaces.
  * The same applied to a stored config row (older save, hand-edited row): the
    senders used the raw value, so " " passed the `if not url` guard and was
    POSTed to.
POST /config already stripped; only the env defaults and the read paths did not.

Fix: env reads are stripped once at import (_DISCORD_WEBHOOK_ENV /
_SLACK_WEBHOOK_ENV, which also drive the default "enabled" flags) and every
reader of a stored value goes through _webhook_url(cfg, key).

Run from services/notification-scheduler-service:
    python -m pytest tests/test_webhook_blank_env.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys

import pytest

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_SERVICE, "notification", "main.py")
_counter = itertools.count()

DISCORD = "https://discord.com/api/webhooks/1122334455667788/abcDEFghiJKL"
SLACK = "https://hooks.slack.com/services/T00000000/B00000000/abcDEFghiJKL"

_CHANNELS = (
    ("discord", "DISCORD_WEBHOOK_URL", "discord_webhook_url", "_send_discord", DISCORD),
    ("slack", "SLACK_WEBHOOK_URL", "slack_webhook_url", "_send_slack", SLACK),
)


def _load(monkeypatch, env):
    """Import notification/main.py fresh under the given env (None = unset)."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("USE_REDIS", "0")
    before = set(sys.modules)
    sys.path.insert(0, _SERVICE)
    name = "_notif_webhook_%d" % next(_counter)
    try:
        spec = importlib.util.spec_from_file_location(name, _PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    except ModuleNotFoundError as e:  # a third-party dependency, not our code
        pytest.skip("optional dependency missing: %s" % e.name)
    finally:
        sys.path.pop(0)
        for k in set(sys.modules) - before:
            sys.modules.pop(k, None)


class _Resp:
    status_code = 200
    text = "ok"

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


@pytest.fixture()
def posts(monkeypatch):
    """Capture httpx.post calls (the module under test reuses the real httpx)."""
    import httpx

    seen = []

    def fake_post(url, **kw):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


_ALL_UNSET = {"DISCORD_WEBHOOK_URL": None, "SLACK_WEBHOOK_URL": None}


# --- env defaults ----------------------------------------------------------

@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_blank_env_is_not_configured_or_enabled(monkeypatch, channel, env, key, sender, url, blank):
    mod = _load(monkeypatch, {**_ALL_UNSET, env: blank})
    assert mod.ENV_DEFAULTS[key] == ""
    assert mod.ENV_DEFAULTS["enabled"][channel] is False
    pub = mod._public_config(dict(mod.ENV_DEFAULTS))
    assert pub[channel]["configured"] is False
    assert pub[channel]["masked"] == ""


@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
def test_unset_env_is_not_configured_or_enabled(monkeypatch, channel, env, key, sender, url):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    assert mod.ENV_DEFAULTS[key] == ""
    assert mod.ENV_DEFAULTS["enabled"][channel] is False


@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
def test_padded_env_is_trimmed_and_enabled(monkeypatch, channel, env, key, sender, url):
    mod = _load(monkeypatch, {**_ALL_UNSET, env: "  " + url + "\n"})
    assert mod.ENV_DEFAULTS[key] == url
    assert mod.ENV_DEFAULTS["enabled"][channel] is True
    pub = mod._public_config(dict(mod.ENV_DEFAULTS))
    assert pub[channel]["configured"] is True
    assert pub[channel]["masked"] == url[:4] + "…" + url[-4:]


def test_one_blank_channel_does_not_disable_the_other(monkeypatch):
    mod = _load(monkeypatch, {"DISCORD_WEBHOOK_URL": "   ", "SLACK_WEBHOOK_URL": SLACK})
    assert mod.ENV_DEFAULTS["enabled"]["discord"] is False
    assert mod.ENV_DEFAULTS["enabled"]["slack"] is True


# --- stored config (padded / blank rows already in the store) ---------------

@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
@pytest.mark.parametrize("stored", [None, "", " ", "\t\n"])
def test_blank_stored_value_is_not_configured(monkeypatch, channel, env, key, sender, url, stored):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    cfg = {key: stored, "enabled": {channel: True}}
    pub = mod._public_config(cfg)
    assert pub[channel]["configured"] is False
    assert pub[channel]["masked"] == ""


@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
def test_padded_stored_value_is_trimmed_in_public_config(monkeypatch, channel, env, key, sender, url):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    cfg = {key: "  " + url + "  ", "enabled": {channel: True}}
    pub = mod._public_config(cfg)
    assert pub[channel]["configured"] is True
    assert pub[channel]["masked"] == url[:4] + "…" + url[-4:]


def test_webhook_url_helper_handles_missing_none_and_non_strings(monkeypatch):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    assert mod._webhook_url({}, "discord_webhook_url") == ""
    assert mod._webhook_url({"discord_webhook_url": None}, "discord_webhook_url") == ""
    assert mod._webhook_url({"discord_webhook_url": "  x  "}, "discord_webhook_url") == "x"


# --- senders ---------------------------------------------------------------

@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
@pytest.mark.parametrize("stored", [None, "", "   ", "\t"])
def test_sender_does_not_post_to_a_blank_webhook(monkeypatch, posts, channel, env, key, sender, url, stored):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    cfg = {key: stored, "enabled": {channel: True}}
    assert getattr(mod, sender)(cfg, "t", "m") is None
    assert posts == []


@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
def test_sender_posts_to_the_trimmed_url(monkeypatch, posts, channel, env, key, sender, url):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    cfg = {key: "  " + url + "\n", "enabled": {channel: True}}
    assert getattr(mod, sender)(cfg, "t", "m") == "sent"
    assert posts == [url]


@pytest.mark.parametrize("channel,env,key,sender,url", _CHANNELS)
def test_sender_still_skips_a_valid_url_when_channel_disabled(monkeypatch, posts, channel, env, key, sender, url):
    mod = _load(monkeypatch, dict(_ALL_UNSET))
    cfg = {key: url, "enabled": {channel: False}}
    assert getattr(mod, sender)(cfg, "t", "m") is None
    assert posts == []


# ===========================================================================
# Telegram + CallMeBot (same bug class, same ENV_DEFAULTS block)
# ===========================================================================

_TG_ENV = {"TELEGRAM_BOT_TOKEN": None, "TELEGRAM_CHAT_ID": None}
_CMB_ENV = {"CALLMEBOT_USER": None, "CALLMEBOT_PHONE": None,
            "CALLMEBOT_APIKEY": None, "CALLMEBOT_USERS": None}
_NO_CHANNEL_ENV = {**_ALL_UNSET, **_TG_ENV, **_CMB_ENV}
TOKEN = "123456789:AAH-abcDEFghiJKL"


@pytest.mark.parametrize("blank", ["", " ", "\t", " \n "])
def test_blank_telegram_env_is_not_configured_or_enabled(monkeypatch, blank):
    mod = _load(monkeypatch, {**_NO_CHANNEL_ENV, "TELEGRAM_BOT_TOKEN": blank, "TELEGRAM_CHAT_ID": "42"})
    assert mod.ENV_DEFAULTS["telegram_bot_token"] == ""
    assert mod.ENV_DEFAULTS["enabled"]["telegram"] is False
    pub = mod._public_config(dict(mod.ENV_DEFAULTS))
    assert pub["telegram"]["configured"] is False
    assert pub["telegram"]["masked"] == ""


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_blank_telegram_chat_id_is_not_enabled(monkeypatch, blank):
    mod = _load(monkeypatch, {**_NO_CHANNEL_ENV, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": blank})
    assert mod.ENV_DEFAULTS["telegram_chat_id"] == ""
    assert mod.ENV_DEFAULTS["enabled"]["telegram"] is False


def test_padded_telegram_env_is_trimmed_and_enabled(monkeypatch):
    mod = _load(monkeypatch, {**_NO_CHANNEL_ENV,
                              "TELEGRAM_BOT_TOKEN": "  " + TOKEN + "\n", "TELEGRAM_CHAT_ID": " 42 "})
    assert mod.ENV_DEFAULTS["telegram_bot_token"] == TOKEN
    assert mod.ENV_DEFAULTS["telegram_chat_id"] == "42"
    assert mod.ENV_DEFAULTS["enabled"]["telegram"] is True
    pub = mod._public_config(dict(mod.ENV_DEFAULTS))
    assert pub["telegram"]["configured"] is True
    assert pub["telegram"]["chat_id"] == "42"


@pytest.mark.parametrize("stored", [None, "", "  "])
def test_blank_stored_telegram_value_is_not_configured(monkeypatch, stored):
    mod = _load(monkeypatch, dict(_NO_CHANNEL_ENV))
    for cfg in ({"telegram_bot_token": stored, "telegram_chat_id": "42"},
                {"telegram_bot_token": TOKEN, "telegram_chat_id": stored}):
        pub = mod._public_config({**cfg, "enabled": {"telegram": True}})
        assert pub["telegram"]["configured"] is False


def test_telegram_sender_refuses_blank_and_uses_trimmed_url(monkeypatch, posts):
    mod = _load(monkeypatch, dict(_NO_CHANNEL_ENV))
    blank = {"telegram_bot_token": "   ", "telegram_chat_id": "42", "enabled": {"telegram": True}}
    assert mod._send_telegram(blank, "t", "m").startswith("not configured")
    assert posts == []
    padded = {"telegram_bot_token": " " + TOKEN + " ", "telegram_chat_id": " 42 ",
              "enabled": {"telegram": True}}
    mod._send_telegram(padded, "t", "m")
    assert posts and posts[0] == "https://api.telegram.org/bot" + TOKEN + "/sendMessage"


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_blank_callmebot_env_is_not_enabled(monkeypatch, blank):
    for var in ("CALLMEBOT_USER", "CALLMEBOT_PHONE", "CALLMEBOT_USERS"):
        mod = _load(monkeypatch, {**_NO_CHANNEL_ENV, var: blank})
        assert mod.ENV_DEFAULTS["enabled"]["callmebot"] is False, var
        assert mod._public_config(dict(mod.ENV_DEFAULTS))["callmebot"]["configured"] is False, var


def test_blank_callmebot_user_falls_back_to_phone(monkeypatch):
    mod = _load(monkeypatch, {**_NO_CHANNEL_ENV, "CALLMEBOT_USER": "  ", "CALLMEBOT_PHONE": " 919800000000 "})
    assert mod.ENV_DEFAULTS["callmebot_user"] == "919800000000"
    assert mod.ENV_DEFAULTS["callmebot_phone"] == "919800000000"
    assert mod.ENV_DEFAULTS["enabled"]["callmebot"] is True


def test_padded_callmebot_env_is_trimmed(monkeypatch):
    mod = _load(monkeypatch, {**_NO_CHANNEL_ENV, "CALLMEBOT_USER": " @alice ",
                              "CALLMEBOT_APIKEY": " k1 ", "CALLMEBOT_USERS": " @bob:k2 , @carol "})
    d = mod.ENV_DEFAULTS
    assert (d["callmebot_user"], d["callmebot_apikey"], d["callmebot_users"]) == ("@alice", "k1", "@bob:k2 , @carol")
    assert mod._callmebot_recipients(d) == [("@alice", "k1"), ("@bob", "k2"), ("@carol", "")]


def test_blank_stored_callmebot_user_does_not_shadow_phone(monkeypatch):
    mod = _load(monkeypatch, dict(_NO_CHANNEL_ENV))
    cfg = {"callmebot_user": "   ", "callmebot_phone": "919800000000", "enabled": {"callmebot": True}}
    assert mod._callmebot_recipients(cfg) == [("919800000000", "")]
    pub = mod._public_config(cfg)["callmebot"]
    assert pub["configured"] is True and pub["user"] == "919800000000"


def test_governance_check_trims_telegram_env(monkeypatch):
    def load(token, chat):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
        before = set(sys.modules)
        sys.path.insert(0, _SERVICE)
        try:
            spec = importlib.util.spec_from_file_location(
                "_gov_%d" % next(_counter), os.path.join(_SERVICE, "scheduler", "governance_check.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
        except ModuleNotFoundError as e:
            pytest.skip("optional dependency missing: %s" % e.name)
        finally:
            sys.path.pop(0)
            for k in set(sys.modules) - before:
                sys.modules.pop(k, None)

    gov = load("   ", "  ")
    assert gov.TELEGRAM_BOT_TOKEN == "" and gov.TELEGRAM_CHAT_ID == ""
    gov = load(" " + TOKEN + " ", " 42\n")
    assert gov.TELEGRAM_BOT_TOKEN == TOKEN and gov.TELEGRAM_CHAT_ID == "42"
