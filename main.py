import platform as _platform
import subprocess as _subprocess

if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


import asyncio
import re
import threading
import time
import json
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

from core import outcome
from core.action_loader import strip_meta
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
)
from core                     import gemini as _gemini
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.viseme               import VisemeStream
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

WAKE_SLEEP_TIMEOUT = 120.0

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


_TAIL_MARGIN = 0.25

_STALL_LIMIT = 30.0

_VIS_WIN = 1024
_VIS_HOP = 480

_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE
_CURSOR_SLACK = 0.15



def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)
        b_f1_hi = (freqs >= 450) & (freqs < 1100)
        b_f2_bk = (freqs >= 600) & (freqs < 1300)
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)
        b_hiss = (freqs >= 3800) & (freqs < 8000)

        out = []
        for start in range(0, x.size, _VIS_HOP):
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            width *= (1.0 - openness) ** 0.8
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


def _describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    try:
        from core import platform_support
        out.extend(platform_support.describe_for_model(platform_support.detect()))
    except Exception as _e:
        print(f"[Limits] platform capabilities unavailable: {_e}")

    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

_REPEAT_MIN = 12


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. xPh4nt0m-Byte, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self._platform_notes_shown = False
        self.ui             = ui
        self._asst_name     = "JARVI    S"
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False
        self._pending_vision       = None
        self._vision_cam_active    = False
        self._vision_close_pending = False
        self._vision_last_time     = 0.0
        self._vision_busy          = False
        self._interrupted          = False
        self._engine_caps          = None
        self._engine               = None
        self._rx_deadline          = None
        self._visemes              = VisemeStream()
        self._last_out_logged      = ""
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None
        self._out_level            = 0.0
        self._echo                 = EchoGuard()
        self._out_latency          = 0.20
        self._tail_until           = 0.0
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True

        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False
        self._sys_monitor      = SystemMonitor()
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()
        self._session_log: list[str] = []

        self._enhanced_live = True
        self._tuned_live    = True

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas
        self.ui.request_say = self.plugin_say

        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        self.ui.wake_is_ready    = wake_is_ready
        self.ui.wake_get_state   = self._wake_state
        self.ui.on_wake_toggle   = self._ui_wake_toggle
        self.ui.on_wake_manual   = self._ui_wake_manual
        self.ui.on_wake_install  = self._ui_wake_install


    def _wake_state(self) -> dict:
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")


    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")


    def _arm_stall(self) -> None:
        """A user turn has gone out; start the clock if one is not already running.

        Deliberately does NOT push an existing deadline forward. The question is
        "how long since the server last said anything while we were waiting",
        not "how long since the most recent send" — otherwise a burst of sends
        into a dead socket would keep resetting the very timer meant to catch it.
        """
        if self._rx_deadline is None:
            self._rx_deadline = time.monotonic() + _STALL_LIMIT

    def _clear_stall(self) -> None:
        """The server spoke, so the return path is alive."""
        self._rx_deadline = None

    async def _watch_stall(self) -> None:
        """Rebuild the session when a sent turn goes unanswered past the limit.

        The reconnect keeps the resumption handle, so the conversation survives:
        this costs a couple of seconds and a line in the log, where before it
        cost the rest of the session with no indication anything was wrong.
        """
        while True:
            await asyncio.sleep(2)
            deadline = self._rx_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self._clear_stall()
            print(f"[JARVIS] ⚠️  No response for {_STALL_LIMIT:.0f}s — "
                  f"the live session is not answering. Rebuilding it.")
            self.ui.write_log("SYS: The model stopped answering — reconnecting.")
            self.request_reconnect(keep_context=True, reason="no response")
            return

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return (url, key, f"{url}/auto-login?key={key}", manual,
                getattr(self._dashboard, "_bind_error", "") or "")

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        if not held and self._engine is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._engine.flush_microphone(SEND_SAMPLE_RATE), self._loop)
            except Exception as e:
                print(f"[JARVIS] PTT flush failed: {e}")
        try:
            if held:
                state = "LISTENING"
            else:
                state = ("SLEEPING" if (self._wake_enabled and not self._awake)
                         else "LISTENING")
            self.ui.set_state(state)
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        self._visemes.reset()
        self._play_cursor = 0.0
        if self._turn_done_event:
            self._turn_done_event.clear()
        if self._engine is not None:
            try:
                self._engine.interrupt()
            except Exception:
                pass
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())

        if self._engine_caps is not None:
            _caps = self._engine_caps
            _blocked = set()
            if not _caps.get("vision"):
                _blocked |= {"screen_process", "close_camera"}
            if not _caps.get("tools"):
                _blocked = {(d.get("name") if isinstance(d, dict)
                             else getattr(d, "name", "")) for d in _all_decls}
            if _blocked:
                _all_decls = [d for d in _all_decls
                              if (d.get("name") if isinstance(d, dict)
                                  else getattr(d, "name", "")) not in _blocked]
                print(f"[JARVIS] {len(_blocked)} tool(s) withheld — "
                      f"this engine cannot serve them")

        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": strip_meta(_all_decls)}],
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif self._action_registry.has(name):
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, outcome.unwrap(r))

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            if jarvis_speaking:
                return

            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            if self._ptt_enabled and not self._ptt_held:
                return
            
            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    self._clear_stall()

                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                self._visemes.feed_text(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                self._visemes.reset()
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            if self._vision_close_pending:
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
                        await self._flush_pending_vision()
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                batch = bytearray(chunk)
                while len(batch) < 9600:
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()


    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        print("[JARVIS] Briefing phase 1 (greeting) sent.")

        async def _deliver_news():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=8.0)
                except Exception as e:
                    self.ui.write_log(f"SYS: News fetch timed out/failed: {e!r}")
                    news_text = ""

                if not self.session:
                    return

                failed = (not news_text) or news_text.startswith(
                    ("No news found", "Search failed", "Please provide")
                )
                if not failed:
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    self.ui.write_log(
                        f"SYS: News unavailable — backend returned: {news_text[:120]!r}"
                    )
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                print("[JARVIS] Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                print(f"[JARVIS] Briefing phase 2 failed: {e}")
                self.ui.write_log("SYS: Could not fetch the news for the briefing.")

        asyncio.create_task(_deliver_news())


    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:
            return
        self._session_log = []

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")


    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")


    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)
        while True:
            if self.session and self._awake:
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print("[JARVIS] Monitor alert sent.")
                            await asyncio.sleep(6)
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)


    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")


    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                self._phone_active = False
                continue
            self._phone_active = True
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()


    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)


    def _engine_mode(self) -> str:
        try:
            from memory.config_manager import get_provider_mode
            return get_provider_mode()
        except Exception:
            return "gemini"

    def _resolve_engine_caps(self):
        """What the chosen engine can do — or None for Gemini Live.

        Called BEFORE `_build_config()`, because the tool declarations depend on
        the answer. Building the providers here rather than later is deliberate:
        the registry caches them by configuration, so the objects made now are
        the same ones the engine gets a moment later, and a misconfiguration is
        reported while there is still a Gemini session to fall back to.
        """
        if self._engine_mode() != "pipeline":
            return None
        try:
            from memory.config_manager import get_pipeline, get_provider_config
            from providers import registry

            pipe = get_pipeline()
            llm_id = str(pipe.get("llm") or "").strip()
            if not llm_id:
                raise RuntimeError("no language model is chosen")
            missing = registry.missing_fields(llm_id, get_provider_config(llm_id))
            if missing:
                raise RuntimeError(f"{llm_id} needs: {', '.join(missing)}")
            llm = registry.build(llm_id, get_provider_config(llm_id))

            from memory.config_manager import save_provider_config
            ensure = getattr(llm, "ensure_vision_known", None)
            if ensure is not None:
                try:
                    seen = ensure(lambda v, _id=llm_id: save_provider_config(_id, v))
                    self.ui.write_log(
                        f"SYS: {llm_id} "
                        + ("can see images." if seen else "is text only."))
                except Exception as e:
                    print(f"[JARVIS] vision check skipped: {e}")

            caps = dict(getattr(llm, "caps", {}) or {})
            caps["audio_out"] = bool(str(pipe.get("tts") or "").strip())
            return caps
        except Exception as e:
            self.ui.write_log(f"SYS: local engine unavailable ({e}) — "
                              f"using Gemini Live.")
            print(f"[JARVIS] Pipeline engine unavailable: {e}")
            return None

    @asynccontextmanager
    async def _open_session(self, live_model: str, config):
        """The conversation, from whichever engine is configured.

        Both branches yield an object with the same four methods and the same
        event shapes, which is the entire contract — see providers/base.py. The
        Live branch is unchanged from what it has always been, down to the
        client being rebuilt on every reconnect.
        """
        if self._engine_caps is None:
            client = genai.Client(
                api_key=_get_api_key(),
                http_options={"api_version":
                              "v1alpha" if self._enhanced_live else "v1beta"},
            )
            async with client.aio.live.connect(model=live_model,
                                               config=config) as session:
                yield session
            return

        from memory.config_manager import get_pipeline, get_provider_config
        from providers import registry
        from providers.engine_pipeline import PipelineEngine

        pipe = get_pipeline()

        def part(slot):
            pid = str(pipe.get(slot) or "").strip()
            if not pid:
                return None
            cfg = dict(get_provider_config(pid))
            cfg["log"] = self.ui.write_log
            return registry.build(pid, cfg)

        engine = PipelineEngine(part("llm"), part("stt"), part("tts"),
                                config=config, log=self.ui.write_log)
        engine.auto_turn = not self._ptt_enabled
        self._engine = engine
        try:
            yield engine
        finally:
            self._engine = None
            await engine.close()

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        audio_devices.prefetch()

        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                global LIVE_MODEL
                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                print(f"[JARVIS] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None

                self._engine_caps = self._resolve_engine_caps()
                config = self._build_config()

                async with (
                    self._open_session(live_model, config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    _orig_send = session.send_client_content

                    async def _send_and_arm(*a, **kw):
                        result = await _orig_send(*a, **kw)
                        self._arm_stall()
                        return result

                    session.send_client_content = _send_and_arm

                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._rx_deadline     = None

                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: JARVIS online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")

                    if not self._platform_notes_shown:
                        self._platform_notes_shown = True
                        try:
                            from core import platform_support
                            for _line in platform_support.describe_for_user(
                                    platform_support.detect()):
                                self.ui.write_log(_line)
                        except Exception as _e:
                            print(f"[Platform] capability notes unavailable: {_e}")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_stall())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    if self._engine is not None:
                        tg.create_task(self._engine.warmup())

                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                if _gemini.note_live_failure(live_model, err_str):
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 15
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()