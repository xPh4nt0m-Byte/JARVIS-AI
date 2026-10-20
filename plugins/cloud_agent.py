"""
cloud_agent.py — this computer's half of the cloud body.

WHAT IT DOES
    Pairs this JARVIS with the one running on your own server (core/cloud.py),
    and keeps the two acting as ONE assistant:

      * settings flow to the server — the name, the Gemini key, the Telegram
        bot and approved phones, your time zone, how to wake this computer;
      * memory flows both ways — what either body learns, the other knows;
      * long tasks and reminders can be handed to the server, so they finish
        and arrive on the phone even if this computer is turned off;
      * when this computer comes back, it says — unprompted — what happened
        while it was away: finished research, reminders, what you told the
        cloud from your phone;
      * Telegram talks to whichever body is awake: this computer while it is on,
        the server the moment it is not.

WHAT IT COSTS WHEN YOU DO NOT USE IT
    Discovery runs this file on every launch, so nothing heavy is at module
    scope: no `core.cloud` import, no socket, no thread. The one thing that
    runs is a config read asking whether a pairing code is set. Disabled, the
    model is never told this tool exists. Deleted, nothing changes.

ALWAYS CONNECTED ONCE PAIRED
    There is no "connect on launch" switch. Pasting a pairing code is the
    decision to use the cloud, and a switch that could be left off produced
    exactly the state nobody wants: JARVIS open, not linked, and the phone
    getting answers from neither body. Unpair by clearing the code, or turn
    the plugin off in 🧩 PLUGINS.

WHAT CAN NEVER BE CHANGED FROM THE SERVER
    Every switch below is read from THIS computer's config, and the server has
    no route that writes it. A compromised server can ask; it cannot give
    itself permission.
"""
from __future__ import annotations

import threading
import time

try:
    from memory.config_manager import (
        get_assistant_name,
        get_plugin_config,
        save_plugin_config,
    )
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

    def get_plugin_config(namespace):                # type: ignore[misc]
        return {}

_NS = "cloud_agent"

_POLL_WAIT = 25
_HTTP_TIMEOUT = 45
_SYNC_DEBOUNCE = 20        # seconds after a local change before it is pushed
_CMD_FIRST = 45            # a desk turn gets this long to say its first line
_CMD_QUIET = 15            # ...then this long after each one
_CMD_MAX = 240             # and never longer than this
_SETTLE = 12               # the log must be quiet this long before speaking unprompted
_AUTOSTART_WAIT = 300
_MISSING = ("core/cloud.py is missing from this install — it ships with the app. "
            "Update the app; there is nothing to pip install.")

# One JARVIS per computer may hold the cloud link. Two copies running at once —
# a double-clicked shortcut, auto-start plus a manual launch — would both claim
# Telegram, and whichever one has no live conversation swallows the phone's
# messages without a word. A loopback port is the lock: the OS frees it the
# moment the process ends, however it ends, so a crash never leaves it stuck.
_INSTANCE_PORT = 47443
_instance_lock: list = []

_LANGUAGE = ("Use the language the user writes to you in (their phone messages below show "
             "it), or the one they normally speak — never English just because these "
             "instructions are.")


def _test(values: dict) -> tuple[bool, str]:
    """TEST & SYNC: prove the code, the certificate and the secret, then push
    this computer's settings and memory so the server is ready at once."""
    try:
        from core import cloud
    except ImportError:
        return False, _MISSING
    code = str(values.get("pair_code") or "").strip()
    if not code:
        return False, "Paste the pairing code the server printed."
    try:
        client = cloud.Client.from_code(code, pem=str(get_plugin_config(_NS).get("pem") or ""))
        client.request("POST", "hello", {}, timeout=15)
        if client.pem:
            save_plugin_config(_NS, {"pem": client.pem})
        h = _sync(client, dict(get_plugin_config(_NS), **values)).get("hello") or {}
    except (ValueError, cloud.LinkError) as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    bits = [f"Connected to {h.get('name', 'JARVIS')} on {h.get('host', 'the server')}."]
    bits.append("Gemini key ✓" if h.get("gemini_key") else
                "No Gemini key on the server — turn on 'give the server this Gemini key'.")
    if h.get("telegram_configured"):
        bits.append("Telegram ✓")
    elif values.get("share_telegram", True):
        bits.append("No Telegram bot yet — set one up under TELEGRAM REMOTE first.")
    if h.get("telegram_phones") is not None:
        bits.append(f"{h['telegram_phones']} phone(s) approved on the server.")
    bits.append(f"Server clock reads your time as {h.get('user_time', '?')}.")
    brief = _brief_from_cfg(_cfg())
    bits.append(f"Briefing: {', '.join(brief['times'])}." if brief["enabled"] else "Briefing: off.")
    # Pressing TEST & SYNC is the moment someone decides to use the cloud, so
    # the link comes up now rather than at the next launch.
    started = next((_start(obj) for obj in _player_candidates()
                    if callable(getattr(obj, "write_log", None))
                    and callable(getattr(obj, "on_text_command", None))), "")
    bits.append("Linked — JARVIS connects automatically from now on." if started
                else "JARVIS will connect automatically on its next launch.")
    return True, " ".join(bits)


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "☁️  CLOUD AGENT (VPS)",
    "fields": [
        {"key": "pair_code", "label": "Pairing code from the server", "type": "password",
         "placeholder": "Printed at the end of: python -m core.cloud deploy root@SERVER_IP"},
        {"key": "share_gemini_key", "label": "Give the server this Gemini API key (it needs one to think)",
         "type": "toggle", "default": True},
        {"key": "share_telegram",
         "label": "Telegram on the server while this computer is off (uses TELEGRAM REMOTE's bot)",
         "type": "toggle", "default": True},
        {"key": "share_memory", "label": "Share memory with the cloud (both ways)",
         "type": "toggle", "default": True},
        # ── the briefing. Also changeable by voice here and from the phone;
        # whichever changed last wins, and this form shows the result.
        {"key": "brief_enabled", "label": "☀️ Briefing on my phone", "type": "toggle", "default": False},
        {"key": "brief_times", "label": "Briefing times (HH:MM, comma-separated)", "type": "text",
         "default": "08:00", "placeholder": "08:00   or   07:30, 21:00"},
        {"key": "brief_days", "label": "Briefing days", "type": "choice",
         "options": ["Every day", "Weekdays (Mon–Fri)", "Weekends (Sat–Sun)", "Custom days"],
         "default": "Every day"},
        {"key": "brief_custom_days", "label": "Custom days — numbers, 1 = Monday … 7 = Sunday",
         "type": "text", "placeholder": "e.g. 1,3,5   (only used with Custom days)"},
        {"key": "brief_weather", "label": "Briefing: weather", "type": "toggle", "default": True},
        {"key": "brief_city", "label": "City for the weather", "type": "text",
         "placeholder": "Empty = the city JARVIS remembers for you"},
        {"key": "brief_topics", "label": "Briefing: news topics (comma-separated)", "type": "text",
         "placeholder": "e.g. artificial intelligence, Formula 1   (empty = no news)"},
        {"key": "brief_reminders", "label": "Briefing: today's reminders", "type": "toggle", "default": True},
        {"key": "brief_tasks", "label": "Briefing: finished cloud tasks", "type": "toggle", "default": True},
        {"key": "brief_extra", "label": "Briefing: anything else to include", "type": "text",
         "placeholder": "In your own words — e.g. a motivating quote, the dollar rate"},
        {"key": "brief_length", "label": "Briefing length", "type": "choice",
         "options": ["Short", "Detailed"], "default": "Short"},
        {"key": "brief_language", "label": "Briefing language", "type": "text",
         "placeholder": "Empty = the language you use with JARVIS"},
        # Off by default, and only ever switchable here. This is the switch
        # that lets a message to the server end up as an action on this PC.
        {"key": "accept_commands",
         "label": "Let the cloud hand my phone's requests to this computer (open apps, Steam…)",
         "type": "toggle", "default": False},
        {"key": "allow_screen", "label": "Let the cloud send screenshots of this computer to my phone",
         "type": "toggle", "default": False},
        {"key": "wake_host", "label": "Wake-on-LAN: your home's public IP or DDNS name",
         "type": "text",
         "placeholder": "Router forwards a UDP port to this PC. Empty = no wake."},
        {"key": "wake_port", "label": "Wake-on-LAN port (the one forwarded on the router)",
         "type": "text", "default": "9", "placeholder": "9"},
        {"key": "wake_mac", "label": "This computer's MAC address", "type": "text",
         "placeholder": "Empty = detected automatically"},
        {"key": "wake_url", "label": "OR a URL that wakes this computer (ESP32, Home Assistant…)",
         "type": "text", "placeholder": "Used instead of the address above when filled in"},
    ],
    "action": {"label": "TEST & SYNC", "run": lambda values: _test(values or {})},
}

PLUGIN = {
    "name": "cloud_agent",
    "description": (
        "Your cloud body: the same assistant on the user's own server, awake around the clock. "
        "delegate = hand it long work that should carry on even if this computer is turned off "
        "(research, comparisons, reports, reading many sources); notify = 'desk' to announce the "
        "result here, 'phone' when the request came from the phone (a [Telegram …] message), "
        "'both' when unsure. REMINDERS: a one-off is set with the `reminder` tool (it reaches this "
        "computer AND the phone). Use remind here for REPEATING ones, which only the cloud can "
        "repeat and which arrive on the phone with Done/snooze buttons: text = exact words in the "
        "user's language, plus time 'HH:MM' with optional days (numbers 1=Mon…7=Sun; weekdays = "
        "[1,2,3,4,5]; none = every day), or monthly_day + time, or every_minutes with optional "
        "between 'HH:MM-HH:MM'. reminders = list; cancel_reminder = by id. BRIEFING: briefing = "
        "show or change the phone briefing (enabled, times, days, city, weather/reminders/tasks, "
        "topics, extra, length, language — pass only what the user wants changed); briefing_now "
        "= send one right away. jobs / result = the cloud's work; status; connect / disconnect; "
        "sync = push settings and memory now."),
    "brief": "Long tasks, repeating reminders and the phone briefing on the always-on cloud server.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "enum": ["delegate", "remind", "reminders", "cancel_reminder", "briefing",
                                "briefing_now", "jobs", "result", "status", "connect",
                                "disconnect", "sync"]},
            "task": {"type": "STRING", "description": "for delegate: the whole task, self-contained"},
            "notify": {"type": "STRING", "enum": ["desk", "phone", "both"]},
            "text": {"type": "STRING", "description": "for remind: the reminder's exact words"},
            "at": {"type": "STRING", "description": "for remind: YYYY-MM-DD HH:MM, local time (one-off)"},
            "time": {"type": "STRING", "description": "for remind: HH:MM of a repeating reminder"},
            "days": {"type": "ARRAY", "items": {"type": "INTEGER"},
                     "description": "remind / briefing: 1=Monday … 7=Sunday"},
            "monthly_day": {"type": "INTEGER", "description": "for remind: day of the month, 1-31"},
            "every_minutes": {"type": "INTEGER", "description": "for remind: repeat every N minutes"},
            "between": {"type": "STRING", "description": "for remind with every_minutes: 'HH:MM-HH:MM'"},
            "id": {"type": "STRING", "description": "reminder id or job id"},
            "enabled": {"type": "BOOLEAN", "description": "for briefing"},
            "times": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "for briefing: HH:MM list"},
            "city": {"type": "STRING", "description": "for briefing: city for the weather"},
            "weather": {"type": "BOOLEAN"}, "reminders": {"type": "BOOLEAN"}, "tasks": {"type": "BOOLEAN"},
            "topics": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "for briefing: news topics"},
            "extra": {"type": "STRING", "description": "for briefing: anything else, in the user's words"},
            "length": {"type": "STRING", "enum": ["short", "detailed"]},
            "language": {"type": "STRING", "description": "for briefing: its language, empty = usual"},
        },
        "required": ["action"],
    },
}


# ── state ────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state: dict = {
    "running": False, "stop": None, "player": None, "client": None,
    "last_ok": 0.0, "last_error": "", "commands": 0,
    "synced_mem_mtime": 0.0, "synced_cfg_mtime": 0.0, "dirty_at": 0.0,
    "holding_tg": False, "tg_restart_at": 0.0,
}
# The conversation is ready to be spoken into. Unprompted speech sent before
# the Live session exists is dropped without a word, so it waits for this.
_live = threading.Event()
_last_line = [0.0]
_listener: list = []
_speak_lock = threading.Lock()


def _cfg() -> dict:
    return get_plugin_config(_NS)


def _log(player, text: str) -> None:
    try:
        if player:
            player.write_log(text)
    except Exception:
        pass


def _install_listener(player) -> None:
    """Watch the activity log for the app saying it is online, and for quiet.

    A Qt signal, not a wrapper around write_log: telegram_remote wraps that
    method, and two wrappers put on and taken off in different orders are how
    an answer ends up relayed twice. A signal takes any number of listeners."""
    if _listener:
        return
    sig = getattr(getattr(player, "_win", None), "_log_sig", None)
    if sig is None:
        _live.set()
        _listener.append(None)
        return

    def on_line(text):
        _last_line[0] = time.monotonic()
        if str(text).startswith("SYS: JARVIS online"):
            _live.set()
    from PyQt6.QtCore import Qt
    sig.connect(on_line, Qt.ConnectionType.DirectConnection)
    _listener.append(on_line)


def _speak(player, instruction: str, title: str = "", panel: str = "") -> None:
    """Say something unprompted, once the app is live and has finished talking
    (the morning briefing lands at the same moment a returning computer does)."""
    def go():
        if not _live.wait(300):
            return
        with _speak_lock:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and time.monotonic() - _last_line[0] < _SETTLE:
                time.sleep(1)
            if title and panel:
                try:
                    player.show_content(title[:48], panel[:4000])
                except Exception:
                    pass
            say = getattr(player, "request_say", None)
            if callable(say):
                say(instruction)
            time.sleep(6)
    threading.Thread(target=go, daemon=True, name="cloud-agent-speak").start()


def _client():
    from core import cloud
    cfg = _cfg()
    code = str(cfg.get("pair_code") or "").strip()
    if not code:
        raise cloud.LinkError("No pairing code yet — settings → CLOUD AGENT.")
    client = _state.get("client")
    if client is None or client.secret not in code:
        client = cloud.Client.from_code(code, pem=str(cfg.get("pem") or ""))
        _state["client"] = client
    return client


def _save_pem(client) -> None:
    if client.pem and client.pem != _cfg().get("pem"):
        save_plugin_config(_NS, {"pem": client.pem})


# ── what the server is told ──────────────────────────────────────────────────

def _local_mac() -> str:
    """The MAC of the adapter that carries traffic to the internet — the one
    Wake-on-LAN has to reach. Connecting a UDP socket sends nothing; it only
    asks the OS which local address it would route through."""
    import socket
    try:
        import psutil
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        for addrs in psutil.net_if_addrs().values():
            if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
                for a in addrs:
                    if a.family == psutil.AF_LINK and a.address:
                        return a.address.replace("-", ":").upper()
    except Exception:
        pass
    return ""


def _tz_offset_min() -> int:
    from datetime import datetime
    return int(datetime.now().astimezone().utcoffset().total_seconds() // 60)


_DAY_CHOICES = {"Every day": [1, 2, 3, 4, 5, 6, 7], "Weekdays (Mon–Fri)": [1, 2, 3, 4, 5],
                "Weekends (Sat–Sun)": [6, 7]}


def _brief_from_cfg(cfg: dict) -> dict:
    """The settings form, read into the briefing's shape."""
    import re
    choice = str(cfg.get("brief_days") or "Every day")
    days = _DAY_CHOICES.get(choice) or sorted(
        {int(x) for x in re.split(r"[^\d]+", str(cfg.get("brief_custom_days") or ""))
         if x.isdigit() and 1 <= int(x) <= 7}) or list(_DAY_CHOICES["Every day"])
    return {"enabled": bool(cfg.get("brief_enabled", False)),
            "times": [t for t in re.split(r"[,\s;]+", str(cfg.get("brief_times") or "08:00")) if t],
            "days": list(days),
            "city": str(cfg.get("brief_city") or "").strip(),
            "weather": bool(cfg.get("brief_weather", True)),
            "topics": [t.strip() for t in str(cfg.get("brief_topics") or "").split(",") if t.strip()],
            "reminders": bool(cfg.get("brief_reminders", True)),
            "tasks": bool(cfg.get("brief_tasks", True)),
            "extra": str(cfg.get("brief_extra") or "").strip(),
            "length": "detailed" if str(cfg.get("brief_length") or "").lower().startswith("detail") else "short",
            "language": str(cfg.get("brief_language") or "").strip()}


def _brief_to_cfg(b: dict) -> dict:
    """The briefing, written back into the settings form's fields."""
    days = list(b.get("days") or _DAY_CHOICES["Every day"])
    choice = next((label for label, value in _DAY_CHOICES.items() if value == days), "Custom days")
    return {"brief_enabled": bool(b.get("enabled")), "brief_times": ", ".join(b.get("times") or ["08:00"]),
            "brief_days": choice,
            "brief_custom_days": ",".join(map(str, days)) if choice == "Custom days" else "",
            "brief_weather": bool(b.get("weather", True)), "brief_city": b.get("city") or "",
            "brief_topics": ", ".join(b.get("topics") or []),
            "brief_reminders": bool(b.get("reminders", True)), "brief_tasks": bool(b.get("tasks", True)),
            "brief_extra": b.get("extra") or "",
            "brief_length": "Detailed" if b.get("length") == "detailed" else "Short",
            "brief_language": b.get("language") or ""}


def _brief_hash(b: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps({k: v for k, v in b.items() if k != "updated"},
                                     sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _settings_payload(cfg: dict) -> dict:
    from memory.config_manager import load_api_keys
    keys = load_api_keys()
    out = {"assistant_name": keys.get("assistant_name") or "",
           "user_name": keys.get("user_name") or "", "tz_offset_min": _tz_offset_min()}
    if cfg.get("share_gemini_key", True) and keys.get("gemini_api_key"):
        out["gemini_api_key"] = keys["gemini_api_key"]
    tg = get_plugin_config("telegram_remote")
    if not cfg.get("share_telegram", True):
        out["telegram"] = {"disabled": True}
    elif str(tg.get("bot_token") or "").strip():
        out["telegram"] = {k: tg.get(k) for k in
                           ("bot_token", "allowed_chat_ids", "pairing_code",
                            "relay_replies", "voice_replies") if k in tg}
    # A change made in the form is noticed by its content changing, since the
    # form itself saves no timestamp. It then counts as the newest version.
    brief = _brief_from_cfg(cfg)
    digest = _brief_hash(brief)
    if digest != cfg.get("brief_hash"):
        updated = time.time()
        save_plugin_config(_NS, {"brief_hash": digest, "brief_updated": updated})
    else:
        updated = float(cfg.get("brief_updated") or 0)
    out["briefing"] = dict(brief, updated=updated)
    out["wake"] = {"wake_host": str(cfg.get("wake_host") or "").strip(),
                   "wake_port": str(cfg.get("wake_port") or "9").strip(),
                   "wake_mac": str(cfg.get("wake_mac") or "").strip() or _local_mac(),
                   "wake_url": str(cfg.get("wake_url") or "").strip()}
    return out


def _mtimes() -> tuple[float, float]:
    from memory.config_manager import CONFIG_FILE
    from memory.memory_manager import MEMORY_PATH

    def mt(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return mt(MEMORY_PATH), mt(CONFIG_FILE)


def _sync(client, cfg: dict) -> dict:
    body = {"settings": _settings_payload(cfg), "memory_seq": int(cfg.get("memory_seq") or 0)}
    if cfg.get("share_memory", True):
        from memory.memory_manager import load_memory
        body["memory"] = load_memory()
    resp = client.request("POST", "sync", body, timeout=30)
    _save_pem(client)
    # The server may have paired a phone this desk has never seen. Adopting its
    # list keeps the two in agreement; a used pairing code is cleared.
    ids = str((resp.get("telegram") or {}).get("allowed_chat_ids") or "")
    if ids and ids != str(get_plugin_config("telegram_remote").get("allowed_chat_ids") or ""):
        save_plugin_config("telegram_remote", {"allowed_chat_ids": ids, "pairing_code": ""})
    # Changed from the phone more recently than here? Then the form follows.
    server_brief = resp.get("briefing")
    if isinstance(server_brief, dict):
        now_cfg = _cfg()
        if float(server_brief.get("updated") or 0) > float(now_cfg.get("brief_updated") or 0) + 0.001:
            fields = _brief_to_cfg(server_brief)
            fields["brief_updated"] = float(server_brief["updated"])
            fields["brief_hash"] = _brief_hash(_brief_from_cfg(dict(now_cfg, **fields)))
            save_plugin_config(_NS, fields)
    _state["synced_mem_mtime"], _state["synced_cfg_mtime"] = _mtimes()
    return resp


# ── Telegram: whichever body is awake ────────────────────────────────────────

def _desk_tr():
    import sys
    tr = sys.modules.get("plugins.telegram_remote")
    if tr is None or not str(get_plugin_config("telegram_remote").get("bot_token") or "").strip():
        return None
    return tr


def _take_telegram(player, client) -> None:
    """Ask the server to let go of the bot, then listen here."""
    tr = _desk_tr()
    if tr is None or not _cfg().get("share_telegram", True):
        return
    _state["take_tried_at"] = time.monotonic()
    resp = client.request("POST", "telegram", {"hold": True}, timeout=60)
    if not resp.get("released"):
        # Said once, then retried quietly every minute from the loop: the
        # server keeps answering the phone in the meantime, so nothing is lost.
        if not _state.get("take_warned"):
            _state["take_warned"] = True
            _log(player, "SYS: Cloud agent — the server has not released Telegram yet; it keeps "
                         "answering and this computer will try again every minute.")
        return
    _state["take_warned"] = False
    _state["holding_tg"] = True
    # Telegram needs a moment to let go of the server's open long poll; starting
    # at once is answered with 409 and stops this computer's bridge again.
    time.sleep(3)
    _state["tg_restart_at"] = 0.0
    _ensure_desk_tg(player)
    _log(player, "SYS: Cloud agent — Telegram talks to this computer now; the server takes "
                 "it back when this computer goes off.")


def _ensure_desk_tg(player) -> None:
    tr = _desk_tr()
    if tr is None or tr._state.get("running") or tr._state.get("stopping"):
        return
    if _state["tg_restart_at"] and time.monotonic() - _state["tg_restart_at"] < 20:
        return
    _state["tg_restart_at"] = time.monotonic()
    try:
        said = tr.run({"action": "start"}, player=player)
        _log(player, f"SYS: Cloud agent — desk Telegram: {said}")
    except Exception as e:
        _log(player, f"SYS: Cloud agent — desk Telegram could not start: {e}")


def _give_back_telegram(player, client) -> None:
    """Stop listening here, then tell the server it is its turn."""
    tr = _desk_tr()
    if _state["holding_tg"] and tr is not None and tr._state.get("running"):
        tr.run({"action": "stop"}, player=player)
        deadline = time.monotonic() + 25            # one long poll
        while time.monotonic() < deadline and (tr._state.get("running") or tr._state.get("stopping")):
            time.sleep(0.5)
    _state["holding_tg"] = False
    try:
        client.request("POST", "bye", {}, timeout=10)
    except Exception:
        pass


def mirror_reminder(text: str, at: str) -> tuple[bool, str]:
    """Called by actions/reminder.py for every one-off reminder: put a copy on
    the cloud, which sends it to the phone at that time. Works whether or not
    the link loop is running — it needs only the pairing code."""
    try:
        client = _client()
        result = str(client.request("POST", "schedule", {
            "action": "add", "reminder": str(text or "").strip() or "Reminder", "at": at,
            "tz_offset_min": _tz_offset_min()}, timeout=15).get("result") or "")
        _save_pem(client)
    except Exception as e:
        return False, str(e)
    return result.startswith("status=done"), result


def _on_exit() -> None:
    """Closing the app hands Telegram straight back, instead of leaving the
    phone unanswered until the server notices the silence (~80 s).

    Hooked to Qt's aboutToQuit as well as atexit: aboutToQuit fires while the
    window is closing and everything still works, whereas atexit runs at the
    very end of interpreter shutdown and is the first thing to be skipped. Sent
    once, whichever gets there first."""
    client = _state.get("client")
    if _state.get("said_bye") or not _state["running"] or client is None:
        return
    _state["said_bye"] = True
    try:
        client.request("POST", "bye", {}, timeout=3)
    except Exception:
        pass


# ── the loop ─────────────────────────────────────────────────────────────────

def _loop(player, stop: threading.Event) -> None:
    from core import cloud

    fails, synced = 0, False
    event_seq = int(_cfg().get("event_seq") or 0)
    client = None
    while not stop.is_set():
        cfg = _cfg()
        try:
            client = _client()
            if not synced:
                resp = _sync(client, cfg)
                synced = True
                _log(player, f"SYS: Cloud agent connected — {(resp.get('hello') or {}).get('host', 'server')}.")
                _take_telegram(player, client)

            mem, conf = _mtimes()
            if mem != _state["synced_mem_mtime"] or conf != _state["synced_cfg_mtime"]:
                if not _state["dirty_at"]:
                    _state["dirty_at"] = time.monotonic()
                elif time.monotonic() - _state["dirty_at"] > _SYNC_DEBOUNCE:
                    _sync(client, cfg)
                    _state["dirty_at"] = 0.0

            resp = client.request("POST", "poll", {
                "info": {"host": _hostname(), "accept_commands": bool(cfg.get("accept_commands")),
                         "allow_screen": bool(cfg.get("allow_screen")),
                         "holds_telegram": _state["holding_tg"]},
                "event_seq": event_seq, "memory_seq": int(cfg.get("memory_seq") or 0),
                "wait": _POLL_WAIT,
            }, timeout=_HTTP_TIMEOUT)
            if fails:
                _log(player, "SYS: Cloud agent — link restored.")
            fails, _state["last_ok"], _state["last_error"] = 0, time.time(), ""

            # A 409 at start-up can stop either bridge; the server is not
            # polling, so this one comes back.
            if _state["holding_tg"] and not resp.get("telegram_running"):
                _ensure_desk_tg(player)
            # A takeover the server refused is tried again, not given up on.
            if (not _state["holding_tg"] and cfg.get("share_telegram", True)
                    and time.monotonic() - _state.get("take_tried_at", 0.0) > 60):
                _take_telegram(player, client)

            if resp.get("deltas"):
                _apply_deltas(resp["deltas"])
            new_seq = {"memory_seq": int(resp.get("memory_seq") or 0)}
            for ev in resp.get("events") or []:
                _on_event(player, ev)
            if int(resp.get("event_seq") or 0) != event_seq:
                event_seq = int(resp.get("event_seq") or 0)
                new_seq["event_seq"] = event_seq
            if any(cfg.get(k) != v for k, v in new_seq.items()):
                save_plugin_config(_NS, new_seq)
                _state["synced_cfg_mtime"] = _mtimes()[1]
            if float(resp.get("briefing_updated") or 0) > float(_cfg().get("brief_updated") or 0) + 0.001:
                _sync(client, _cfg())
            for cmd in resp.get("commands") or []:
                threading.Thread(target=_run_command, args=(player, client, cmd),
                                 daemon=True, name="cloud-agent-cmd").start()
        except cloud.LinkError as e:
            fails += 1
            _state["last_error"] = str(e)
            if fails == 1 or fails % 20 == 0:
                _log(player, f"SYS: Cloud agent — {e} Retrying.")
            if e.status == 401:
                _log(player, "SYS: Cloud agent stopped — the pairing code was rejected.")
                break
            stop.wait(min(60, 2 ** min(fails, 6)))
        except Exception as e:
            fails += 1
            _state["last_error"] = f"{type(e).__name__}: {e}"
            print(f"[CloudAgent] loop error: {e}")
            stop.wait(min(60, 2 ** min(fails, 6)))
    if client is not None:
        _give_back_telegram(player, client)
    with _lock:
        _state["running"] = False
    _log(player, "SYS: Cloud agent disconnected.")


def _hostname() -> str:
    import platform
    return platform.node()


def _apply_deltas(deltas: list) -> None:
    """What the cloud learnt, written into this computer's memory."""
    from memory.memory_manager import forget, update_memory
    for d in deltas:
        try:
            op = d.get("op")
            if op == "set":
                update_memory({d["category"]: {d["key"]: {"value": d["value"]}}})
            elif op == "forget":
                forget(d["key"], d["category"])
            elif op in ("monitor_add", "monitor_remove", "monitor_state"):
                _apply_monitor_delta(d)
        except Exception as e:
            print(f"[CloudAgent] memory delta skipped: {e}")
    _state["synced_mem_mtime"] = _mtimes()[0]


def _apply_monitor_delta(d: dict) -> None:
    """A watch-list change made on the cloud: a topic added or removed from the
    phone, or how far the cloud got while checking — which is what stops this
    computer announcing a headline the phone already received."""
    from actions.background_monitor import _load, _save, add_monitor
    op, slug = d.get("op"), d.get("key")
    if op == "monitor_add":
        if slug not in _load():
            add_monitor(d.get("value") or slug)
        return
    monitors = _load()
    if slug not in monitors:
        return
    if op == "monitor_remove":
        monitors.pop(slug, None)
    else:
        state = d.get("data") or {}
        if str(state.get("last_check") or "") < str(monitors[slug].get("last_check") or ""):
            return
        monitors[slug].update(last_check=state.get("last_check", ""), last_hash=state.get("last_hash", ""))
    _save(monitors)


def _digest(entries: list) -> str:
    name = get_assistant_name()
    rows = []
    for e in entries:
        when = time.strftime("%d.%m %H:%M", time.localtime(e.get("at", 0)))
        kind, text = e.get("kind"), str(e.get("text") or "")
        if kind == "phone":
            rows.append(f"{when}  📱 You: {text}")
        elif kind == "reply":
            rows.append(f"{when}  ☁ {name}: {text}")
        elif kind == "job":
            rows.append(f"{when}  ✅ Task {e.get('status', 'done')}: {e.get('task', '')}\n{text}")
        elif kind == "reminder":
            rows.append(f"{when}  ⏰ Reminder sent to your phone: {text}")
        elif kind == "alert":
            rows.append(f"{when}  📰 {text}")
    return "\n\n".join(rows)


def _on_event(player, ev: dict) -> None:
    from core import outcome

    kind = ev.get("kind")
    if kind == "away":
        text = _digest(ev.get("entries") or [])
        if not text:
            return
        since = time.strftime("%H:%M", time.localtime(ev.get("since") or time.time()))
        _log(player, f"SYS: ☁ While this computer was away (since {since}), the cloud kept working.")
        _speak(player, (
            f"[WHILE YOU WERE AWAY] This computer was off or disconnected since {since}, and your "
            f"cloud body kept working. What happened is below, and on the screen now. Speak first, "
            f"unprompted: welcome the user back in one short sentence, then give the single most "
            f"useful thing — the headline finding of a finished task, a reminder they may have "
            f"missed — in one or two sentences, and offer to go into detail. Do not read the list "
            f"out. {_LANGUAGE}\n" + outcome.untrusted("cloud-digest", text[:3500])),
            "WHILE YOU WERE AWAY", text)
    elif kind == "job_done":
        task, result = str(ev.get("task") or ""), str(ev.get("result") or "")
        _log(player, f"SYS: ☁ Cloud task {ev.get('status', 'done')} — {task[:70]}")
        _speak(player, (
            f"[CLOUD RESULT] Your cloud body finished a task the user gave it: '{task[:200]}'. "
            f"Tell the user in one or two sentences what came of it; the full text is on the "
            f"screen. {_LANGUAGE}\n" + outcome.untrusted("cloud-job", result[:3000])),
            f"CLOUD — {task[:36]}", result)
    elif kind == "reminder":
        text = str(ev.get("text") or "")
        _log(player, f"SYS: ⏰ {text}")
        _speak(player, f"[REMINDER] A reminder the user set is due now: «{text}». Say it to them "
                       f"now, in one short sentence, in the language it is written in.")


# ── requests from the cloud, run on this computer ────────────────────────────

class _Tap:
    """Collects one turn's answer lines from the activity log (see
    _install_listener for why a signal and not a wrapper)."""

    def __init__(self, player):
        self.player = player
        self.lines: list[str] = []
        self._cv = threading.Condition()
        self._sig = getattr(getattr(player, "_win", None), "_log_sig", None)
        self._wrapped = None
        prefix = f"{get_assistant_name()}:"

        def on_line(text):
            text = str(text)
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
            elif not text.startswith(("SYS:", "ERR:")):
                return
            with self._cv:
                self.lines.append(text)
                self._cv.notify_all()
        self._on_line = on_line

    def __enter__(self):
        if self._sig is not None:
            from PyQt6.QtCore import Qt
            self._sig.connect(self._on_line, Qt.ConnectionType.DirectConnection)
        else:
            orig = self.player.write_log

            def wrapped(text):
                orig(text)
                self._on_line(text)
            self._wrapped, self._orig = wrapped, orig
            self.player.write_log = wrapped
        return self

    def __exit__(self, *exc):
        if self._sig is not None:
            try:
                self._sig.disconnect(self._on_line)
            except Exception:
                pass
        elif self._wrapped is not None and self.player.write_log is self._wrapped:
            self.player.write_log = self._orig

    def next_lines(self, after: int, timeout: float) -> list[str]:
        with self._cv:
            if len(self.lines) <= after:
                self._cv.wait(timeout=timeout)
            return self.lines[after:]


def _post(client, cmd_id: str, **payload) -> None:
    try:
        client.request("POST", "output", {"cmd_id": cmd_id, **payload}, timeout=60)
    except Exception as e:
        print(f"[CloudAgent] could not return output: {e}")


def _run_command(player, client, cmd: dict) -> None:
    cfg = _cfg()
    kind, cid = cmd.get("kind"), cmd.get("id", "")
    _state["commands"] += 1

    if kind == "screenshot":
        if not cfg.get("allow_screen"):
            return _post(client, cid, done=True, error=(
                "This computer does not share its screen with the cloud. The switch is at "
                "the computer: settings → CLOUD AGENT."))
        try:
            import base64
            from plugins import _telegram_ops as ops
            job = ops.produce("screenshot", "", {})
            _log(player, "SYS: Cloud agent sent a screenshot to the phone.")
            return _post(client, cid, done=True, caption=job.get("caption", ""),
                         photo_b64=base64.b64encode(job["data"]).decode("ascii"))
        except Exception as e:
            return _post(client, cid, done=True, error=f"Screenshot failed: {e}")

    if kind == "system":
        try:
            from plugins import _telegram_ops as ops
            return _post(client, cid, done=True, text=ops.produce("system_status", "", {})["text"])
        except Exception as e:
            return _post(client, cid, done=True, error=f"No readout: {e}")

    if not cfg.get("accept_commands"):
        return _post(client, cid, done=True, error=(
            "This computer does not accept requests from the cloud. The switch is at the "
            "computer: settings → CLOUD AGENT."))

    text = str(cmd.get("text") or "").strip()
    fn = getattr(player, "on_text_command", None)
    if not text or not callable(fn):
        return _post(client, cid, done=True, error="No live session on the computer yet.")
    if not _live.wait(180):
        return _post(client, cid, done=True, error="The computer's assistant did not come online in time.")

    try:
        get_state, wake_now = getattr(player, "wake_get_state", None), getattr(player, "on_wake_manual", None)
        if callable(get_state) and callable(wake_now):
            st = get_state() or {}
            if st.get("enabled") and not st.get("awake"):
                wake_now()
                time.sleep(0.3)
    except Exception:
        pass

    with _Tap(player) as tap:
        _log(player, f"[☁ Cloud]: {text}")
        fn(text)
        sent, started = 0, time.monotonic()
        quiet_until = started + _CMD_FIRST
        while time.monotonic() < min(quiet_until, started + _CMD_MAX):
            new = tap.next_lines(sent, timeout=1.0)
            for line in new:
                _post(client, cid, text=line)
            if new:
                sent += len(new)
                quiet_until = time.monotonic() + _CMD_QUIET
    _post(client, cid, done=True)


# ── entry point ──────────────────────────────────────────────────────────────

def _start(player) -> str:
    try:
        from core import cloud  # noqa: F401
    except ImportError:
        return _MISSING
    if not str(_cfg().get("pair_code") or "").strip():
        return ("There is no pairing code yet. Install the cloud agent on your server, then "
                "paste the code it prints into settings → CLOUD AGENT.")
    if player is None:
        return "Sir, I have no interface to connect the cloud to."
    with _lock:
        if _state["running"]:
            return "The cloud link is already up."
        if not _claim_instance():
            return ("Another JARVIS window on this computer already holds the cloud link, so this "
                    "one stays off it. Close the extra copy (Task Manager → pythonw.exe) if you "
                    "did not mean to open two.")
        stop = threading.Event()
        _state.update(running=True, stop=stop, player=player, client=None)
    _install_listener(player)
    _install_button_tap(player)
    _state["said_bye"] = False
    import atexit
    atexit.register(_on_exit)
    try:
        from PyQt6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None and not _state.get("quit_hooked"):
            app.aboutToQuit.connect(_on_exit)
            _state["quit_hooked"] = True
    except Exception:
        pass
    threading.Thread(target=_loop, args=(player, stop), daemon=True, name="cloud-agent").start()
    return "Connecting to the cloud. I'll say so here once the link is up."


def _install_button_tap(player) -> None:
    """Reminder buttons pressed while THIS computer answers Telegram.

    The press arrives here as an ordinary message and would go to the
    conversation, where a model would have to guess what "⏰ +10 min" refers to.
    The exact button tokens are caught first and handed to the server, which
    knows the reminder and snoozes it — every other message passes straight on.
    """
    if _state.get("button_tap") or player is None:
        return
    try:
        orig = player.on_text_command
    except Exception:
        return
    if not callable(orig) or getattr(orig, "_cloud_tap", False):
        return

    def tapped(text, *args, **kwargs):
        try:
            from core.cloud import reminder_button
            minutes = reminder_button(text)
        except Exception:
            minutes = None
        if minutes is None:
            return orig(text, *args, **kwargs)
        tr = _desk_tr()
        chat = tr._state.get("turn_chat") if tr is not None else None
        if tr is not None:
            tr._state["turn_open"] = False

        def send():
            try:
                _client().request("POST", "reminder_button", {"text": text, "chat": chat}, timeout=20)
            except Exception as e:
                _log(player, f"SYS: Cloud agent — the reminder button could not reach the server: {e}")
        threading.Thread(target=send, daemon=True, name="cloud-agent-button").start()

    tapped._cloud_tap = True          # type: ignore[attr-defined]
    try:
        player.on_text_command = tapped
        _state["button_tap"] = True
    except Exception:
        pass


def _claim_instance() -> bool:
    if _instance_lock:
        return True
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", _INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _instance_lock.append(s)
    return True


def _status() -> str:
    cfg = _cfg()
    if not _state["running"]:
        return "The cloud link is not running." + ("" if cfg.get("pair_code") else " No pairing code is set.")
    ok = _state["last_ok"]
    bits = [f"Cloud link up; last contact {int(time.time() - ok)}s ago." if ok else "Cloud link starting."]
    if _state["last_error"]:
        bits.append(f"Last problem: {_state['last_error']}")
    tr = _desk_tr()
    if not _state["holding_tg"]:
        bits.append("Telegram is answered by the server.")
    elif tr is not None and tr._state.get("running"):
        bits.append("Telegram is answered by this computer, and its bridge is listening.")
    else:
        bits.append("Telegram should be answered by this computer, but its bridge is NOT "
                    "listening right now — it is being restarted.")
    bits.append("Accepts requests from the cloud." if cfg.get("accept_commands")
                else "Does not accept requests from the cloud.")
    return " ".join(bits)


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not _HAS_CONFIG:
        return "This Mark is too old for the cloud agent — it has no plugin settings store."
    action = str(parameters.get("action") or "status").strip().lower()
    player = player or _state.get("player")

    if action == "connect":
        # Asked for by voice or text, so the conversation is already live.
        _live.set()
        return _start(player)
    if action == "disconnect":
        ev = _state.get("stop")
        if not _state["running"] or ev is None:
            return "The cloud link is not running."
        ev.set()
        return "Disconnecting from the cloud. Telegram goes back to the server; its tasks carry on."
    if action == "status":
        return _status()

    try:
        from core import cloud, outcome
        client = _client()
        if action == "sync":
            _sync(client, _cfg())
            return outcome.done("cloud_agent", synced="settings and memory")
        if action == "delegate":
            task = str(parameters.get("task") or "").strip()
            if not task:
                return outcome.failed("cloud_agent", "delegate needs the task")
            notify = str(parameters.get("notify") or "desk")
            job = client.request("POST", "delegate", {"task": task, "notify": notify},
                                 timeout=20).get("job") or {}
            return outcome.started("cloud_agent", job_id=job.get("id"),
                                   note="the cloud works on it even if this computer is turned "
                                        "off; the result is announced when it is done")
        recurring = any(parameters.get(k) for k in ("daily", "time", "days", "monthly_day", "every_minutes"))
        if action == "remind" and not recurring:
            # A one-off goes through the built-in reminder, which sets the
            # desktop notification and calls mirror_reminder() for the phone —
            # one path, so the phone copy is never added twice.
            at = str(parameters.get("at") or "").strip()
            if len(at) != 16:
                return outcome.failed("cloud_agent", "remind needs at 'YYYY-MM-DD HH:MM' or daily 'HH:MM'")
            args = {"date": at[:10], "time": at[11:], "message": parameters.get("text") or ""}
            run_tool = getattr(player, "run_tool", None)
            if callable(run_tool):
                return run_tool("reminder", args)
            from actions.reminder import reminder as _local_reminder
            return _local_reminder(args, player=player)
        if action in ("remind", "reminders", "cancel_reminder"):
            body = {"action": {"remind": "add", "reminders": "list", "cancel_reminder": "remove"}[action],
                    "reminder": parameters.get("text") or "", "id": parameters.get("id") or "",
                    "tz_offset_min": _tz_offset_min()}
            for key in ("at", "daily", "time", "days", "monthly_day", "every_minutes", "between"):
                if parameters.get(key) not in (None, "", []):
                    body[key] = parameters[key]
            return str(client.request("POST", "schedule", body, timeout=20).get("result") or "")
        if action == "briefing_now":
            client.request("POST", "briefing", {"action": "now"}, timeout=20)
            return outcome.started("cloud_agent", note="the briefing is being put together on the "
                                                       "server and sent to the phone")
        if action == "briefing":
            from core.cloud import normalise_briefing
            changes = {k: parameters[k] for k in ("enabled", "times", "days", "city", "weather",
                                                  "reminders", "tasks", "topics", "extra", "length",
                                                  "language") if k in parameters}
            current = _brief_from_cfg(_cfg())
            if changes:
                merged = normalise_briefing(dict(current, **changes))
                save_plugin_config(_NS, _brief_to_cfg(merged))
                _sync(client, _cfg())
                current = _brief_from_cfg(_cfg())
            facts = {"enabled": current["enabled"], "times": ", ".join(current["times"]),
                     "days": ",".join(map(str, current["days"])) + " (1=Mon)",
                     "sections": ",".join(k for k in ("weather", "reminders", "tasks") if current[k]) or "-",
                     "city": current["city"] or "(remembered)", "topics": ", ".join(current["topics"]) or "-",
                     "extra": current["extra"] or "-", "length": current["length"],
                     "language": current["language"] or "(usual)"}
            if changes:
                return outcome.done("cloud_agent", changed=",".join(changes), **facts)
            return outcome.done("cloud_agent", **facts)
        if action == "jobs":
            jobs = client.request("POST", "jobs", {}, timeout=20).get("jobs") or []
            return "\n".join(f"{j['id']} [{j['status']}] {j['task'][:80]}" for j in jobs[:10]) \
                or "The cloud has no tasks."
        if action == "result":
            job = client.request("POST", "jobs", {"job_id": parameters.get("id") or ""},
                                 timeout=20).get("job")
            if not job:
                return outcome.failed("cloud_agent", "no task with that id")
            return outcome.untrusted("cloud-job", f"[{job['status']}] {job['task']}\n\n{job['result']}")
    except ImportError:
        return _MISSING
    except Exception as e:
        return f"Sir, the cloud link failed: {e}"
    return f"I don't have a '{action}' action for the cloud agent."


# ── connect on launch ────────────────────────────────────────────────────────

def _player_candidates():
    """Read-only discovery of the running interface — the same approach, and
    the same reasoning, as telegram_remote.py's."""
    try:
        from core import confirm
        for cb in (confirm._show_cb, confirm._log_cb, confirm._hide_cb):
            owner = getattr(cb, "__self__", None)
            if owner is not None:
                yield owner
    except Exception:
        pass


def _autostart() -> None:
    deadline = time.monotonic() + _AUTOSTART_WAIT
    while time.monotonic() < deadline:
        for obj in _player_candidates():
            if callable(getattr(obj, "write_log", None)) and callable(getattr(obj, "on_text_command", None)):
                _install_listener(obj)
                time.sleep(4)
                _log(obj, f"SYS: Cloud agent (auto-start) — {_start(obj)}")
                return
        time.sleep(2)


if _HAS_CONFIG:
    try:
        from memory.config_manager import get_plugin_enabled
        if str(_cfg().get("pair_code") or "").strip() and get_plugin_enabled(_NS):
            threading.Thread(target=_autostart, daemon=True, name="cloud-agent-autostart").start()
    except Exception:
        pass
