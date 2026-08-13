#!/usr/bin/env python3
"""Local interview transcription app with F8-triggered Codex answers."""

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


# GNOME의 전역 단축키 명령은 이 경로만 실행한다. 무거운 모듈은 불러오지 않는다.
if __name__ == "__main__" and "--trigger" in sys.argv:
    from linux_port.backend import send_app_command

    raise SystemExit(send_app_command(b"F8"))
if __name__ == "__main__" and "--trigger-f9" in sys.argv:
    from linux_port.backend import send_app_command

    raise SystemExit(send_app_command(b"F9"))
if __name__ == "__main__" and "--stop" in sys.argv:
    from linux_port.backend import send_app_command

    raise SystemExit(send_app_command(b"STOP"))


import json
import queue
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRecoverableError,
    CodexAppServerTransportError,
)
from context_manager import CONTEXT_STATUS_SYNCED, ContextManager
from interview_thread_backend import InterviewThreadBackend
from moonshine_streaming_worker import MoonshineStreamingWorker
from platform_backend import (
    PULSEAUDIO_AUDIO_BACKEND,
    SUPPORTED_AUDIO_BACKENDS,
    WINDOWS_BRIDGE_AUDIO_BACKEND,
    create_platform_backend,
)
from session_store import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
    SessionStore,
    normalize_codex_settings,
)


# Ubuntu의 python3-gi는 시스템 경로에 설치되어 있고 venv에는 노출되지 않는다.
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

os.environ.setdefault("GDK_BACKEND", "x11")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango


APP_VERSION = "moonshine-small-streaming-dev"
CODEX_MODEL = os.environ.get("INTERVIEW_CODEX_MODEL", DEFAULT_CODEX_MODEL)
CODEX_REASONING = os.environ.get(
    "INTERVIEW_CODEX_REASONING",
    DEFAULT_CODEX_REASONING_EFFORT,
)
CODEX_FAST_MODE = False
APP_MODE = os.environ.get("INTERVIEW_APP_MODE", "normal")
CODEX_ENABLED = os.environ.get("INTERVIEW_DISABLE_CODEX", "0") == "0"
CODEX_TIMEOUT_SECONDS = 60
BACKGROUND_JOIN_TIMEOUT_SECONDS = 5
CODEX_DEVELOPER_INSTRUCTIONS = """You assist a job candidate with interview preparation and live answers.
Follow the candidate's preferences, background, speaking style, and answer format established in the conversation.
When a turn contains CURRENT INTERVIEWER QUESTION, return an immediately speakable answer draft in the same language as that question.
Do not invent specific personal facts; ask during preparation or use adaptable wording when details are missing.
During the live interview, assume the candidate spoke your previous live-answer draft unless the later interviewer transcript indicates otherwise.
Each live-interview turn contains only conversation transcribed since the previous request plus the current interviewer question."""
ANSWER_SCROLL_DEBOUNCE_MS = 450
ANSWER_SMOOTH_SCROLL_THRESHOLD = 1.5
ANSWER_CONTENT_SCROLL_PIXELS = 60
ANSWER_POSITION_GUIDE_HEIGHT = 96
ENTER_DEBOUNCE_MS = 300
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
STT_DIAGNOSTICS_ENABLED = (
    os.environ.get("INTERVIEW_STT_DIAGNOSTICS", "0") != "0"
)
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
TEXT_WIDTH_CHARS = shutil.get_terminal_size(fallback=(100, 24)).columns
SAMPLE_RATE = 16_000
LOG_WRITE_LOCK = threading.Lock()
BOUNDARY_STATUS_LISTENING = "● LISTENING"
BOUNDARY_STATUS_AUTO = "✓ AUTO"
BOUNDARY_STATUS_F8 = "✓ F8 NEW"
BOUNDARY_STATUS_F9 = "✓ F9 CONTINUED"
BOUNDARY_STATUS_ERROR = "ERROR"
RESPONSE_STATUS_READY = "● READY"
RESPONSE_STATUS_THINKING = "◌ THINKING..."
RESPONSE_STATUS_UPDATING = "◌ UPDATING..."
RESPONSE_STATUS_ERROR = "ERROR"
NO_INTERVIEW_THREAD_TEXT = "아직 Interview Thread가 없습니다."
NO_INTERVIEW_CONVERSATION_TEXT = "아직 면접 대화가 없습니다."
INTERVIEW_QUESTION_MARKER = "CURRENT INTERVIEWER QUESTION:"
STT_PRESENTATION = {
    "en": {
        "language": "English",
        "title": "Moonshine Small Streaming",
        "model": "small-streaming-en",
        "mode": "Streaming ASR",
    },
    "ja": {
        "language": "Japanese",
        "title": "Moonshine Base",
        "model": "base-ja",
        "mode": "Base ASR",
    },
}
APP_MODE_TITLES = {
    "normal": "Normal Interview",
    "performance": "Performance Test",
    "stt_diagnostic": "STT Diagnostic",
}

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
FALLBACK_CODEX_MODELS = [
    {
        "model": "gpt-5.6-sol",
        "displayName": "GPT-5.6 Sol",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "low",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
    },
    {
        "model": "gpt-5.6-terra",
        "displayName": "GPT-5.6 Terra",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
    },
    {
        "model": "gpt-5.6-luna",
        "displayName": "GPT-5.6 Luna",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max")
        ],
    },
    {
        "model": "gpt-5.5",
        "displayName": "GPT-5.5",
        "additionalSpeedTiers": ["fast"],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh")
        ],
    },
    {
        "model": "gpt-5.2",
        "displayName": "GPT-5.2",
        "additionalSpeedTiers": [],
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh")
        ],
    },
]
def append_log(log_path, event):
    if log_path is None:
        return
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        **event,
    }
    with LOG_WRITE_LOCK:
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_app_session():
    if not TEST_LOGGING:
        return None, None
    session_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    session_dir = APP_DIR / "test_runs" / f"app_session_{session_id}_{os.getpid()}"
    session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    session_dir.chmod(0o700)
    log_path = session_dir / "session.jsonl"
    log_path.touch(mode=0o600)
    log_path.chmod(0o600)
    return session_dir, log_path


class CodexWorker:
    """Run queued turns on one persistent App Server thread."""

    _LATEST_JOB = object()

    def __init__(
        self,
        on_ready,
        thread_id=None,
        *,
        model=CODEX_MODEL,
        effort=CODEX_REASONING,
        fast_mode=CODEX_FAST_MODE,
        load_model_catalog=False,
    ):
        self.jobs = queue.Queue()
        self.accepting = True
        self.on_ready = on_ready
        self.thread_id = thread_id
        self.model = model
        self.effort = effort
        self.fast_mode = bool(fast_mode)
        self.load_model_catalog = load_model_catalog
        self.client = None
        self.client_lock = threading.Lock()
        self.turn_active = threading.Event()
        self.latest_lock = threading.Lock()
        self.latest_job = None
        self.latest_token_queued = False
        self.active_latest_generation = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_model_and_effort(self, model, effort):
        """Apply Preparation selections to subsequent turns on this thread."""
        self.model = model
        self.effort = effort
        with self.client_lock:
            client = self.client
            if client is not None:
                client.model = model
                client.effort = effort

    def submit(
        self,
        prompt,
        callback,
        on_delta=None,
        interactive=False,
        on_approval=None,
        on_start=None,
    ):
        if self.accepting:
            self.jobs.put(
                (
                    prompt,
                    callback,
                    on_delta,
                    interactive,
                    on_approval,
                    on_start,
                    None,
                    None,
                )
            )

    def submit_latest(
        self,
        generation,
        prompt,
        callback,
        on_delta=None,
        on_start=None,
        on_recovery=None,
    ):
        """Keep only the newest live turn and interrupt the active one."""
        if not self.accepting:
            return False
        job = (
            prompt,
            callback,
            on_delta,
            False,
            None,
            on_start,
            on_recovery,
            generation,
        )
        interrupt_active = False
        with self.latest_lock:
            self.latest_job = job
            if not self.latest_token_queued:
                self.latest_token_queued = True
                self.jobs.put(self._LATEST_JOB)
            interrupt_active = (
                self.active_latest_generation is not None
                and self.active_latest_generation != generation
            )
        if interrupt_active:
            with self.client_lock:
                client = self.client
            if client is not None:
                client.request_interrupt()
        return True

    def stop(self):
        self.accepting = False
        with self.latest_lock:
            self.latest_job = None
            self.latest_token_queued = False
        with self.client_lock:
            client = self.client
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self.jobs.put(None)
        if client is not None and self.turn_active.is_set():
            client.request_interrupt()
        self.thread.join(timeout=3)
        if client is not None:
            client.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self):
        try:
            _client, ready = self._start_client(self.thread_id)
            startup_error = None
        except Exception as caught:
            ready = None
            startup_error = caught
        GLib.idle_add(self.on_ready, ready, startup_error)

        while True:
            job = self.jobs.get()
            if job is None:
                return
            if job is self._LATEST_JOB:
                with self.latest_lock:
                    job = self.latest_job
                    self.latest_job = None
                    self.latest_token_queued = False
                    if job is not None:
                        self.active_latest_generation = job[-1]
                if job is None:
                    continue
            (
                prompt,
                callback,
                on_delta,
                interactive,
                on_approval,
                on_start,
                on_recovery,
                generation,
            ) = job
            result, error = self._run_job(
                prompt,
                on_delta,
                interactive,
                on_approval,
                on_start,
                on_recovery,
                generation,
                startup_error,
            )
            if result is not None:
                startup_error = None
            if generation is not None:
                with self.latest_lock:
                    if self.active_latest_generation == generation:
                        self.active_latest_generation = None
                    if self.client is not None:
                        self.client.clear_interrupt_request()
            GLib.idle_add(callback, result, error)

    def _run_job(
        self,
        prompt,
        on_delta,
        interactive,
        on_approval,
        on_start,
        on_recovery,
        generation,
        startup_error,
    ):
        stream_callback = None
        if on_delta is not None:
            stream_callback = lambda delta, elapsed: GLib.idle_add(
                on_delta, delta, elapsed
            )
        approval_callback = None
        if on_approval is not None:
            approval_callback = lambda method, params: self._request_approval(
                on_approval,
                method,
                params,
            )

        recovery_attempts = 0
        started = False
        current_prompt = prompt
        while True:
            with self.client_lock:
                client = self.client
            if client is None:
                caught = startup_error or CodexAppServerTransportError(
                    "Codex App Server is unavailable"
                )
            else:
                try:
                    if not started and on_start is not None:
                        on_start()
                    started = True
                    self.turn_active.set()
                    try:
                        result = client.run_turn(
                            current_prompt,
                            on_delta=stream_callback,
                            interactive=interactive,
                            on_approval=approval_callback,
                        )
                    finally:
                        self.turn_active.clear()
                    result["recovery_attempts"] = recovery_attempts
                    return result, None
                except Exception as error:
                    caught = error

            can_recover = (
                generation is not None
                and recovery_attempts == 0
                and self.accepting
                and isinstance(caught, CodexAppServerRecoverableError)
                and self._generation_is_current(generation)
            )
            if not can_recover:
                if recovery_attempts:
                    stage = (
                        "failed"
                        if self._generation_is_current(generation)
                        else "superseded"
                    )
                    self._notify_recovery(on_recovery, stage, {
                        "attempt": recovery_attempts,
                        "error": str(caught),
                        "thread_id": self.thread_id,
                    })
                    if isinstance(caught, CodexAppServerRecoverableError):
                        self._discard_client(client)
                return None, caught

            recovery_attempts = 1
            self._notify_recovery(on_recovery, "started", {
                "attempt": recovery_attempts,
                "error": str(caught),
                "thread_id": self.thread_id,
            })
            try:
                _client, ready = self._restart_client()
            except Exception as recovery_error:
                self._notify_recovery(on_recovery, "failed", {
                    "attempt": recovery_attempts,
                    "error": str(recovery_error),
                    "thread_id": self.thread_id,
                })
                return None, recovery_error

            if not self._generation_is_current(generation):
                self._notify_recovery(on_recovery, "superseded", {
                    "attempt": recovery_attempts,
                    "thread_id": ready["thread_id"],
                })
                return None, caught

            self._notify_recovery(on_recovery, "resumed", {
                "attempt": recovery_attempts,
                "thread_id": ready["thread_id"],
                "startup_seconds": ready["startup_seconds"],
                "process_id": ready.get("process_id"),
            })
            current_prompt = f"""LIVE RECOVERY RETRY:
The previous attempt ended because the Codex transport failed. Any partial assistant output from that failed attempt was NOT SPOKEN by the candidate. Answer the current interviewer question again.

{prompt}"""
            startup_error = None

    def _start_client(self, thread_id):
        client = CodexAppServerClient(
            model=self.model,
            effort=self.effort,
            fast_mode=self.fast_mode,
            cwd=APP_DIR,
            developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
            timeout_seconds=CODEX_TIMEOUT_SECONDS,
        )
        with self.client_lock:
            self.client = client
        try:
            ready = client.start(
                thread_id=thread_id,
                ephemeral=thread_id is None,
            )
            if self.load_model_catalog:
                try:
                    ready["models"] = client.list_models()
                except CodexAppServerError as error:
                    ready["model_list_error"] = str(error)
        except Exception:
            with self.client_lock:
                if self.client is client:
                    self.client = None
            client.stop()
            raise
        self.thread_id = ready["thread_id"]
        return client, ready

    def _restart_client(self):
        thread_id = self.thread_id
        if not thread_id:
            raise CodexAppServerError(
                "Cannot recover Codex without a persistent thread id"
            )
        with self.client_lock:
            old_client = self.client
            self.client = None
        if old_client is not None:
            old_client.stop()
        client, ready = self._start_client(thread_id)
        if ready["thread_id"] != thread_id:
            client.stop()
            with self.client_lock:
                if self.client is client:
                    self.client = None
            raise CodexAppServerError(
                "Codex resumed a different thread during recovery"
            )
        return client, ready

    def _discard_client(self, client):
        if client is None:
            return
        with self.client_lock:
            if self.client is client:
                self.client = None
        client.stop()

    def _generation_is_current(self, generation):
        with self.latest_lock:
            if self.active_latest_generation != generation:
                return False
            return (
                self.latest_job is None
                or self.latest_job[-1] <= generation
            )

    @staticmethod
    def _notify_recovery(callback, stage, details):
        if callback is not None:
            GLib.idle_add(callback, stage, details)

    @staticmethod
    def _request_approval(callback, method, params):
        completed = threading.Event()
        decision = {"value": "decline"}

        def ask():
            try:
                decision["value"] = callback(method, params)
            finally:
                completed.set()
            return False

        GLib.idle_add(ask)
        completed.wait()
        return decision["value"]


def create_live_codex_worker(on_ready, thread_id, settings):
    """Create one live worker from an immutable session-settings snapshot."""
    snapshot = normalize_codex_settings(settings)
    return CodexWorker(
        on_ready,
        thread_id=thread_id,
        model=snapshot["codex_model"],
        effort=snapshot["codex_reasoning_effort"],
        fast_mode=snapshot["codex_fast_mode"],
    )


SESSION_RESPONSE_NEW = 1
SESSION_RESPONSE_ARCHIVE = 2
SESSION_RESPONSE_RENAME = 3
SESSION_RESPONSE_BACK = 4


def moonshine_asr_backend(language):
    return (
        "moonshine-base-ja"
        if language == "ja"
        else "moonshine-small-streaming"
    )


def session_list_row(session):
    settings = normalize_codex_settings(session.get("settings"))
    last_used = (
        session.get("last_used_at") or session.get("created_at") or ""
    )
    if "T" in last_used:
        last_used = last_used.replace("T", " ")[:16]
    return (
        session.get("name") or "Unnamed Session",
        stt_status_summary(settings["stt_language"]),
        last_used,
        session["session_id"],
    )


def model_reasoning_efforts(model):
    efforts = []
    for option in model.get("supportedReasoningEfforts", []):
        effort = (
            option.get("reasoningEffort")
            if isinstance(option, dict)
            else option
        )
        if effort and effort not in efforts:
            efforts.append(effort)
    return efforts


def model_supports_fast(model):
    speed_tiers = {
        str(tier).lower() for tier in model.get("additionalSpeedTiers", [])
    }
    if "fast" in speed_tiers:
        return True
    return any(
        str(tier.get("name", "")).lower() == "fast"
        for tier in model.get("serviceTiers", [])
        if isinstance(tier, dict)
    )


def stt_presentation(language):
    return STT_PRESENTATION.get(language, STT_PRESENTATION["en"])


def stt_status_summary(language):
    return "JA · Base" if language == "ja" else "EN · Streaming"


def runtime_options(environment=None):
    environment = os.environ if environment is None else environment
    audio_backend = environment.get(
        "INTERVIEW_AUDIO_BACKEND", PULSEAUDIO_AUDIO_BACKEND
    ).strip().lower()
    if audio_backend not in SUPPORTED_AUDIO_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_AUDIO_BACKENDS))
        raise ValueError(
            f"unsupported INTERVIEW_AUDIO_BACKEND={audio_backend!r}; "
            f"expected one of: {supported}"
        )
    return {
        "mode": environment.get("INTERVIEW_APP_MODE", "normal"),
        "codex_enabled": environment.get("INTERVIEW_DISABLE_CODEX", "0")
        == "0",
        "logging_enabled": environment.get("INTERVIEW_TEST_LOG", "0")
        != "0",
        "diagnostics_enabled": environment.get(
            "INTERVIEW_STT_DIAGNOSTICS", "0"
        )
        != "0",
        "audio_backend": audio_backend,
    }


def preparation_runtime_summary(options, language):
    title = APP_MODE_TITLES.get(options["mode"], options["mode"])
    presentation = stt_presentation(language)
    return (
        f"Mode: {title}  ·  "
        f"Codex: {'On' if options['codex_enabled'] else 'Off'}  ·  "
        f"Logging: {'On' if options['logging_enabled'] else 'Off'}  ·  "
        f"STT: {presentation['language']} / {presentation['model']}"
    )


def context_scope_style(scope):
    return "scope-session" if scope == "SESSION" else "scope-global"


def context_status_style(status):
    return {
        "SYNCED": "status-synced",
        "CHANGED": "status-changed",
        "NOT SYNCED": "status-not-synced",
    }.get(status, "status-not-synced")


def context_status_summary(context_rows):
    statuses = {row.get("status") for row in context_rows}
    if "CHANGED" in statuses:
        return "● Context Changed", "status-changed"
    if "NOT SYNCED" in statuses:
        return "● Context Not Synced", "status-not-synced"
    return "● Context Synced", "status-synced"


PREPARATION_CONVERSATION_RATIO = 0.72


def preparation_conversation_position(width):
    return max(0, round(width * PREPARATION_CONVERSATION_RATIO))


_APPLICATION_CSS_INSTALLED = False


def install_application_css():
    global _APPLICATION_CSS_INSTALLED
    if _APPLICATION_CSS_INSTALLED:
        return
    css = b"""
    window { background-color: rgba(18, 20, 24, 0.96); }
    window.interviewer, window.answer, window.control { border-radius: 14px; }
    window.interviewer { border: 2px solid rgba(95, 176, 255, 0.85); }
    window.answer { border: 2px solid rgba(255, 195, 92, 0.82); }
    window.control { border: 2px solid rgba(255, 195, 92, 0.82); }
    window.preparation-window, window.session-window {
        background-color: #15181d;
    }
    frame.preparation-card {
        background-color: rgba(37, 41, 48, 0.72);
        border: 1px solid rgba(174, 181, 191, 0.16);
        border-radius: 8px;
        padding: 10px;
    }
    frame.preparation-status-bar {
        background-color: rgba(37, 41, 48, 0.58);
        border: 1px solid rgba(174, 181, 191, 0.14);
        border-radius: 7px;
        padding: 6px 9px;
    }
    .status-session { color: #e8edf3; font: bold 11px Sans; }
    .status-stt { color: #aeb7c3; font: 10px Sans; }
    .context-panel-toggle { padding: 3px 8px; }
    .settings-button { padding: 3px 9px; }
    .settings-heading { color: #dce8f5; font: bold 11px Sans; }
    .section-title {
        color: #e8edf3;
        font: bold 12px Sans;
        padding: 0 0 4px;
    }
    .section-description { color: #9fa8b5; font: 10px Sans; }
    .stt-model-title { color: #dce8f5; font: bold 11px Sans; }
    .stt-model-detail { color: #9fa8b5; font: 10px Sans; }
    .context-header { color: #8f99a7; font: bold 9px Sans; }
    .context-filename { color: #aeb7c3; font: 10px Monospace; }
    .context-badge {
        border-radius: 5px;
        padding: 3px 7px;
        font: bold 9px Sans;
    }
    .scope-global {
        color: #9dccff;
        background-color: rgba(65, 126, 181, 0.28);
        border: 1px solid rgba(115, 177, 231, 0.34);
    }
    .scope-session {
        color: #d6b8ff;
        background-color: rgba(121, 82, 166, 0.30);
        border: 1px solid rgba(172, 130, 219, 0.34);
    }
    .status-synced {
        color: #9ed6ad;
        background-color: rgba(61, 128, 79, 0.25);
        border: 1px solid rgba(102, 170, 120, 0.30);
    }
    .status-changed {
        color: #f0c67c;
        background-color: rgba(159, 111, 37, 0.27);
        border: 1px solid rgba(214, 160, 72, 0.32);
    }
    .status-not-synced {
        color: #c7ccd4;
        background-color: rgba(105, 112, 124, 0.25);
        border: 1px solid rgba(151, 158, 169, 0.28);
    }
    .context-edit { padding: 2px 10px; }
    textview.conversation-view, textview.conversation-view text {
        color: #eef2f6;
        background-color: #101318;
    }
    .heading { color: #8ec8ff; font: bold 12px Sans; letter-spacing: 1px; }
    window.answer .heading { color: #ffc75c; }
    .position-guide { border: 2px solid rgba(255, 195, 92, 0.75); background: transparent; }
    .focus-transcript { color: #fff5d9; background: transparent; font: bold 22px Sans; }
    .focus-transcript text { color: #fff5d9; background: transparent; }
    .transcript { color: #ffffff; font: 20px Sans; }
    .boundary-status { color: rgba(142, 200, 255, 0.78); font: bold 11px Sans; padding: 1px 2px 0; border-top: 1px solid rgba(142, 200, 255, 0.18); }
    .response-status { color: rgba(255, 199, 92, 0.78); font: bold 11px Sans; padding: 1px 2px 0; border-top: 1px solid rgba(255, 199, 92, 0.18); }
    .shortcut-reminder { color: rgba(174, 181, 191, 0.68); font: 10px Sans; padding: 1px 2px 0; }
    .close-button { color: #d8dde5; font: bold 18px Sans; padding: 0 4px; }
    .close-button:hover { color: #ffffff; background: rgba(255, 90, 90, 0.55); }
    .control-button { color: #fff5d9; font: bold 18px Sans; padding: 2px 8px; }
    .visibility-button { padding: 2px 4px; }
    .control-drag { color: #aeb5bf; font: 18px Sans; padding: 0 4px; }
    .resize-handle { background: rgba(139, 146, 157, 0.16); }
    .resize-handle:hover { background: rgba(142, 200, 255, 0.55); }
    .resize-corner { color: #aeb5bf; font: 12px Sans; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _APPLICATION_CSS_INSTALLED = True


def preparation_section(title, description=None):
    frame = Gtk.Frame()
    frame.set_shadow_type(Gtk.ShadowType.NONE)
    frame.get_style_context().add_class("preparation-card")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    heading = Gtk.Label(label=title)
    heading.set_xalign(0)
    heading.get_style_context().add_class("section-title")
    box.pack_start(heading, False, False, 0)
    if description:
        detail = Gtk.Label(label=description)
        detail.set_xalign(0)
        detail.set_line_wrap(True)
        detail.get_style_context().add_class("section-description")
        box.pack_start(detail, False, False, 0)
    frame.add(box)
    return frame, box


def context_display_name(filename):
    stem = Path(filename).stem
    return " ".join(stem.replace("_", " ").replace("-", " ").split()).title()


def context_display_rows(contexts):
    return [
        {
            "scope": context.scope.upper(),
            "display_name": context_display_name(context.name),
            "filename": context.name,
            "path": context.path,
            "status": context.status,
        }
        for context in contexts
    ]


def load_context_display_rows(context_manager, session_id):
    return context_display_rows(
        context_manager.resolve_effective_context_states(session_id)
    )


def interview_conversation_messages(thread):
    """Extract only interviewer questions and final Codex answers."""
    messages = []
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                text = CodexAppServerClient._content_text(
                    item.get("content", [])
                )
                if INTERVIEW_QUESTION_MARKER not in text:
                    continue
                question = text.rsplit(INTERVIEW_QUESTION_MARKER, 1)[1].strip()
                if question:
                    messages.append({"role": "interviewer", "text": question})
            elif (
                item_type == "agentMessage"
                and item.get("phase") == "final_answer"
            ):
                text = item.get("text", "").strip()
                if text:
                    messages.append({"role": "codex", "text": text})
    return messages


def can_start_interview(session, context_rows, codex_enabled=True):
    if not session:
        return False
    if not codex_enabled:
        return True
    return bool(
        session.get("interview_thread_id")
        and all(
            row.get("status") == CONTEXT_STATUS_SYNCED
            for row in context_rows
        )
    )


class RenameSessionDialog(Gtk.Dialog):
    """Rename only the user-facing label of an app session."""

    def __init__(self, session):
        super().__init__(title="세션 이름 변경", modal=True)
        self.set_default_size(420, -1)
        self.set_border_width(12)
        self.add_button("취소", Gtk.ResponseType.CANCEL)
        self.rename_button = self.add_button(
            "이름 변경",
            Gtk.ResponseType.OK,
        )
        self.rename_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(8)
        label = Gtk.Label(label="Session Name")
        label.set_xalign(0)
        content.pack_start(label, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(session.get("name") or "")
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", self._name_changed)
        content.pack_start(self.name_entry, False, False, 0)
        self._name_changed(self.name_entry)
        self.show_all()
        self.name_entry.grab_focus()
        self.name_entry.select_region(0, -1)

    def session_name(self):
        return self.name_entry.get_text().strip()

    def _name_changed(self, entry):
        self.rename_button.set_sensitive(bool(entry.get_text().strip()))


class SessionChooserDialog(Gtk.Dialog):
    """Keyboard-friendly chooser for Interview Assistant-owned sessions."""

    def __init__(self, sessions, preferred_session_id=None):
        super().__init__(title="Interview Assistant Sessions")
        self.set_default_size(820, 520)
        self.set_resizable(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_skip_taskbar_hint(False)
        self.set_border_width(12)
        self.set_modal(True)
        self.get_style_context().add_class("session-window")

        self.new_button = self.add_button("+ 새 세션", SESSION_RESPONSE_NEW)
        self.rename_button = self.add_button(
            "이름 변경",
            SESSION_RESPONSE_RENAME,
        )
        self.rename_button.set_sensitive(False)
        self.archive_button = self.add_button(
            "삭제",
            SESSION_RESPONSE_ARCHIVE,
        )
        self.archive_button.set_sensitive(False)
        self.archive_button.get_style_context().add_class(
            "destructive-action"
        )
        self.get_action_area().set_child_secondary(
            self.archive_button,
            True,
        )
        self.add_button("뒤로가기", SESSION_RESPONSE_BACK)
        self.add_button("취소", Gtk.ResponseType.CANCEL)
        self.open_button = self.add_button("열기", Gtk.ResponseType.OK)
        self.open_button.set_sensitive(False)
        self.open_button.get_style_context().add_class("suggested-action")

        content = self.get_content_area()
        content.set_spacing(10)
        heading = Gtk.Label()
        heading.set_markup("<b>면접 세션 선택</b>")
        heading.set_xalign(0)
        content.pack_start(heading, False, False, 0)

        help_text = Gtk.Label(
            label=(
                "최근 사용한 면접 세션부터 표시됩니다.\n"
                "세션을 선택하고 열기를 누르거나 Enter를 눌러주세요."
            )
        )
        help_text.set_xalign(0)
        content.pack_start(help_text, False, False, 0)

        self.sessions_by_session_id = {
            session["session_id"]: session for session in sessions
        }
        self.model = Gtk.ListStore(str, str, str, str)
        preferred_path = None
        for index, session in enumerate(sessions):
            self.model.append(session_list_row(session))
            if session["session_id"] == preferred_session_id:
                preferred_path = Gtk.TreePath.new_from_indices([index])

        self.tree = Gtk.TreeView(model=self.model)
        self.tree.set_headers_visible(True)
        self.tree.set_activate_on_single_click(False)
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_renderer.set_property("weight", Pango.Weight.BOLD)
        name_column = Gtk.TreeViewColumn("Name", name_renderer, text=0)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        self.tree.append_column(name_column)
        stt_renderer = Gtk.CellRendererText()
        stt_renderer.set_property("foreground", "#9dccff")
        stt_renderer.set_property("weight", Pango.Weight.BOLD)
        stt_column = Gtk.TreeViewColumn("STT", stt_renderer, text=1)
        stt_column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        stt_column.set_cell_data_func(stt_renderer, self._render_stt_cell)
        self.tree.append_column(stt_column)
        last_used_renderer = Gtk.CellRendererText()
        last_used_renderer.set_property("xalign", 1.0)
        last_used_column = Gtk.TreeViewColumn(
            "Last Used",
            last_used_renderer,
            text=2,
        )
        last_used_column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        self.tree.append_column(last_used_column)
        self.tree.connect("row-activated", self._row_activated)
        self.tree.connect("key-press-event", self._key_pressed)
        selection = self.tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        selection.connect("changed", self._selection_changed)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        content.pack_start(scroller, True, True, 0)

        self.empty_label = Gtk.Label(
            label="저장된 면접 세션이 없습니다. ‘새 세션’을 눌러 만드세요."
        )
        self.empty_label.set_xalign(0)
        content.pack_start(self.empty_label, False, False, 0)
        self.empty_label.set_visible(len(self.model) == 0)

        if len(self.model):
            selection.select_path(preferred_path or Gtk.TreePath.new_first())
        else:
            self._selection_changed(selection)

        self.show_all()
        self.empty_label.set_visible(len(self.model) == 0)
        self.tree.grab_focus()

    def selected_session(self):
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is None:
            return None
        return self.sessions_by_session_id[model[tree_iter][3]]

    def _row_activated(self, *_args):
        self.response(Gtk.ResponseType.OK)

    def _key_pressed(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.selected_session() is not None:
                self.response(Gtk.ResponseType.OK)
                return True
        return False

    def _selection_changed(self, selection):
        has_selection = selection.get_selected()[1] is not None
        self.open_button.set_sensitive(has_selection)
        self.rename_button.set_sensitive(has_selection)
        self.archive_button.set_sensitive(has_selection)

    def _render_stt_cell(self, _column, cell, model, tree_iter, _data):
        color = (
            "#d6b8ff"
            if model[tree_iter][1].startswith("JA")
            else "#9dccff"
        )
        cell.set_property("foreground", color)


def _new_codex_client(settings=None):
    settings = normalize_codex_settings(settings or {
        "codex_model": CODEX_MODEL,
        "codex_reasoning_effort": CODEX_REASONING,
    })
    return CodexAppServerClient(
        model=settings["codex_model"],
        effort=settings["codex_reasoning_effort"],
        fast_mode=settings["codex_fast_mode"],
        cwd=APP_DIR,
        developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
    )


def archive_persisted_codex_session(thread_id):
    client = _new_codex_client()
    try:
        client.connect()
        client.archive_thread(thread_id)
    finally:
        client.stop()


def _show_session_error(error):
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text="세션 작업을 완료하지 못했습니다.",
    )
    dialog.format_secondary_text(str(error))
    dialog.run()
    dialog.destroy()


def _confirm_archive(session):
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text="선택한 세션을 삭제할까요?",
    )
    dialog.format_secondary_text(
        f"{session['name']}\n\n이 세션은 활성 목록에서 제거됩니다."
    )
    dialog.add_button("취소", Gtk.ResponseType.CANCEL)
    delete_button = dialog.add_button("삭제", Gtk.ResponseType.OK)
    delete_button.get_style_context().add_class("destructive-action")
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def choose_interview_session(store, context_manager, codex_enabled=True):
    preferred_session_id = None
    while True:
        dialog = SessionChooserDialog(
            store.active(),
            preferred_session_id=preferred_session_id,
        )
        response = dialog.run()
        selected = dialog.selected_session()
        dialog.destroy()

        if response == SESSION_RESPONSE_BACK:
            return SESSION_RESPONSE_BACK

        if response == SESSION_RESPONSE_NEW:
            try:
                created = datetime.now().astimezone()
                session = store.create(
                    created.strftime("%Y-%m-%d %H:%M"),
                    created.isoformat(timespec="seconds"),
                    normalize_codex_settings(),
                )
                session_id = session["session_id"]
                context_manager.ensure_session(session_id)
                preferred_session_id = session_id
            except Exception as error:
                _show_session_error(error)
            continue

        if response == SESSION_RESPONSE_RENAME and selected is not None:
            rename_dialog = RenameSessionDialog(selected)
            rename_response = rename_dialog.run()
            new_name = rename_dialog.session_name()
            rename_dialog.destroy()
            if rename_response == Gtk.ResponseType.OK:
                try:
                    store.update_name(selected["session_id"], new_name)
                    preferred_session_id = selected["session_id"]
                except (OSError, ValueError) as error:
                    _show_session_error(error)
            continue

        if response == SESSION_RESPONSE_ARCHIVE and selected is not None:
            if _confirm_archive(selected):
                try:
                    interview_thread_id = selected.get("interview_thread_id")
                    if codex_enabled and interview_thread_id:
                        archive_persisted_codex_session(interview_thread_id)
                    store.mark_archived(selected["session_id"])
                    preferred_session_id = None
                except Exception as error:
                    if isinstance(error, CodexAppServerError) and (
                        "no rollout found" in str(error).lower()
                    ):
                        store.mark_archived(selected["session_id"])
                        preferred_session_id = None
                    else:
                        _show_session_error(error)
            continue

        if response == Gtk.ResponseType.OK and selected is not None:
            try:
                context_manager.ensure_session(selected["session_id"])
            except (OSError, ValueError) as error:
                _show_session_error(error)
                continue
            store.mark_used(selected["session_id"])
            selected["last_used_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            return selected

        return None


def launch_interview_launcher():
    return subprocess.Popen(
        [sys.executable, str(APP_DIR / "interview_launcher.py")],
        cwd=APP_DIR,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


CHAT_RESPONSE_BACK = 10
CHAT_RESPONSE_START_INTERVIEW = 11


class CompactMenuSelector(Gtk.MenuButton):
    """Small GTK3 menu-backed selector without ComboBox popup focus races."""

    def __init__(self, on_changed):
        super().__init__()
        self._on_changed = on_changed
        self._active_id = None
        self._labels = {}
        self._items = {}
        self._menu = Gtk.Menu()
        self.set_popup(self._menu)
        self.set_direction(Gtk.ArrowType.DOWN)

    def remove_all(self):
        for child in self._menu.get_children():
            child.destroy()
        self._labels.clear()
        self._items.clear()
        self._active_id = None
        self.set_label("")

    def append(self, item_id, label, sensitive=True):
        self._labels[item_id] = label
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        item.connect("activate", self._activate, item_id)
        self._menu.append(item)
        self._items[item_id] = item
        item.show()

    def set_item_sensitive(self, item_id, sensitive):
        item = self._items.get(item_id)
        if item is not None:
            item.set_sensitive(sensitive)

    def set_active_id(self, item_id):
        if item_id not in self._labels:
            return False
        self._active_id = item_id
        self.set_label(self._labels[item_id])
        return True

    def get_active_id(self):
        return self._active_id

    def _activate(self, _item, item_id):
        if item_id == self._active_id:
            return
        self.set_active_id(item_id)
        self._on_changed(self)


class NewContextDialog(Gtk.Dialog):
    """Collect a free-form Context name and destination scope."""

    def __init__(self, parent):
        super().__init__(
            title="New Context",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(420, -1)
        self.set_border_width(12)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.create_button = self.add_button("Create", Gtk.ResponseType.OK)
        self.create_button.get_style_context().add_class("suggested-action")
        self.create_button.set_sensitive(False)
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(8)
        name_label = Gtk.Label(label="Context Name")
        name_label.set_xalign(0)
        content.pack_start(name_label, False, False, 0)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", self._name_changed)
        content.pack_start(self.name_entry, False, False, 0)

        scope_label = Gtk.Label(label="Scope")
        scope_label.set_xalign(0)
        content.pack_start(scope_label, False, False, 4)
        self.session_scope = Gtk.RadioButton.new_with_label_from_widget(
            None,
            "Session",
        )
        self.global_scope = Gtk.RadioButton.new_with_label_from_widget(
            self.session_scope,
            "Global",
        )
        self.session_scope.set_active(True)
        content.pack_start(self.session_scope, False, False, 0)
        content.pack_start(self.global_scope, False, False, 0)

        file_label = Gtk.Label(label="File")
        file_label.set_xalign(0)
        content.pack_start(file_label, False, False, 4)
        self.filename_label = Gtk.Label(label="—")
        self.filename_label.set_xalign(0)
        self.filename_label.set_selectable(True)
        content.pack_start(self.filename_label, False, False, 0)
        self.show_all()
        self.name_entry.grab_focus()

    def context_name(self):
        return self.name_entry.get_text()

    def context_scope(self):
        return "session" if self.session_scope.get_active() else "global"

    def _name_changed(self, entry):
        try:
            filename = ContextManager.context_filename(entry.get_text())
        except ValueError:
            filename = "—"
        self.filename_label.set_text(filename)
        self.create_button.set_sensitive(filename != "—")


class PreparationDialog(Gtk.Dialog):
    """Configure Context and live settings before starting an interview."""

    def __init__(
        self,
        session_id,
        session_store=None,
        session_settings=None,
        context_manager=None,
        runtime=None,
    ):
        super().__init__(title="Interview Preparation")
        self.session_id = session_id
        self.session_store = session_store
        self.context_manager = context_manager
        self.runtime = runtime_options() if runtime is None else dict(runtime)
        self.codex_enabled = self.runtime["codex_enabled"]
        self.codex_settings = normalize_codex_settings(session_settings)
        self.codex_models = list(FALLBACK_CODEX_MODELS)
        self._updating_settings_ui = False
        self.active = False
        self.context_sync_in_progress = False
        self.context_sync_generation = 0
        self.model_catalog_load_generation = 0
        self.conversation_load_generation = 0
        self.background_stop = threading.Event()
        self.background_lock = threading.Lock()
        self.background_threads = set()
        self.background_clients = set()
        self.session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        self.set_default_size(940, 760)
        self.set_resizable(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_skip_taskbar_hint(False)
        self.set_border_width(12)
        self.set_modal(False)
        self.get_style_context().add_class("preparation-window")

        self.back_button = self.add_button("뒤로가기", CHAT_RESPONSE_BACK)
        self.start_button = self.add_button(
            "면접 시작",
            CHAT_RESPONSE_START_INTERVIEW,
        )
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.set_sensitive(False)

        content = self.get_content_area()
        content.set_spacing(8)
        session_name = (
            self.session.get("name")
            if self.session is not None
            else "Interview Session"
        ) or "Interview Session"
        status_frame = Gtk.Frame()
        status_frame.set_shadow_type(Gtk.ShadowType.NONE)
        status_frame.get_style_context().add_class(
            "preparation-status-bar"
        )
        status_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        self.session_summary_label = Gtk.Label(label=session_name)
        self.session_summary_label.set_xalign(0)
        self.session_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.session_summary_label.get_style_context().add_class(
            "status-session"
        )
        status_bar.pack_start(
            self.session_summary_label,
            True,
            True,
            0,
        )
        self.runtime_summary_label = Gtk.Label()
        self.runtime_summary_label.set_xalign(0)
        self.runtime_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        status_bar.pack_start(
            self.runtime_summary_label,
            True,
            True,
            0,
        )
        self.context_panel_button = Gtk.ToggleButton()
        self.context_panel_button.set_active(True)
        self.context_panel_button.set_relief(Gtk.ReliefStyle.NONE)
        self.context_panel_button.set_tooltip_text(
            "Context panel 접기/펼치기"
        )
        self.context_panel_button.get_style_context().add_class(
            "context-panel-toggle"
        )
        status_bar.pack_start(
            self.context_panel_button,
            False,
            False,
            0,
        )
        self.stt_summary_label = Gtk.Label()
        self.stt_summary_label.get_style_context().add_class("status-stt")
        status_bar.pack_start(self.stt_summary_label, False, False, 0)
        self.settings_button = Gtk.Button(label="⚙ Settings")
        self.settings_button.set_relief(Gtk.ReliefStyle.NONE)
        self.settings_button.get_style_context().add_class("settings-button")
        self.settings_button.connect("clicked", self._show_settings)
        status_bar.pack_end(self.settings_button, False, False, 0)
        status_frame.add(status_bar)
        content.pack_start(status_frame, False, False, 0)

        self.settings_dialog = Gtk.Dialog(
            title="Interview Settings",
            transient_for=self,
            modal=True,
        )
        self.settings_dialog.set_default_size(620, 390)
        self.settings_dialog.set_resizable(True)
        self.settings_dialog.add_button("닫기", Gtk.ResponseType.CLOSE)
        self.settings_dialog.connect("delete-event", self._close_settings)
        settings_content = self.settings_dialog.get_content_area()
        settings_content.set_border_width(12)
        settings_content.set_spacing(10)

        session_heading = Gtk.Label(label="Session")
        session_heading.set_xalign(0)
        session_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(session_heading, False, False, 0)
        session_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        session_grid.attach(Gtk.Label(label="Session Name"), 0, 0, 1, 1)
        session_name_label = Gtk.Label(label=session_name)
        session_name_label.set_xalign(0)
        session_name_label.set_selectable(True)
        session_name_label.set_hexpand(True)
        session_grid.attach(session_name_label, 1, 0, 1, 1)
        session_grid.attach(Gtk.Label(label="Session ID"), 0, 1, 1, 1)
        session_id_label = Gtk.Label(label=session_id)
        session_id_label.set_xalign(0)
        session_id_label.set_selectable(True)
        session_id_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        session_grid.attach(session_id_label, 1, 1, 1, 1)
        settings_content.pack_start(session_grid, False, False, 0)
        settings_content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            2,
        )

        codex_heading = Gtk.Label(label="Codex")
        codex_heading.set_xalign(0)
        codex_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(codex_heading, False, False, 0)
        codex_grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        model_label = Gtk.Label(label="Model")
        model_label.set_xalign(0)
        codex_grid.attach(model_label, 0, 0, 1, 1)
        self.model_combo = CompactMenuSelector(self._model_changed)
        self.model_combo.set_size_request(180, -1)
        codex_grid.attach(self.model_combo, 0, 1, 1, 1)
        reasoning_label = Gtk.Label(label="Reasoning")
        reasoning_label.set_xalign(0)
        codex_grid.attach(reasoning_label, 1, 0, 1, 1)
        self.reasoning_combo = CompactMenuSelector(self._reasoning_changed)
        self.reasoning_combo.set_size_request(110, -1)
        codex_grid.attach(self.reasoning_combo, 1, 1, 1, 1)
        fast_label = Gtk.Label(label="Fast")
        fast_label.set_xalign(0)
        codex_grid.attach(fast_label, 2, 0, 1, 1)
        self.fast_combo = CompactMenuSelector(self._fast_changed)
        self.fast_combo.set_size_request(72, -1)
        self.fast_combo.append("off", "Off")
        self.fast_combo.append("on", "On")
        codex_grid.attach(self.fast_combo, 2, 1, 1, 1)
        settings_content.pack_start(codex_grid, False, False, 0)
        settings_content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            2,
        )

        stt_heading = Gtk.Label(label="Speech Recognition")
        stt_heading.set_xalign(0)
        stt_heading.get_style_context().add_class("settings-heading")
        settings_content.pack_start(stt_heading, False, False, 0)
        stt_grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        language_label = Gtk.Label(label="STT Language")
        language_label.set_xalign(0)
        stt_grid.attach(language_label, 0, 0, 1, 1)
        self.stt_language_combo = CompactMenuSelector(
            self._stt_language_changed
        )
        self.stt_language_combo.set_size_request(160, -1)
        self.stt_language_combo.append("en", "English")
        self.stt_language_combo.append("ja", "Japanese")
        self.stt_language_combo.set_active_id(
            self.codex_settings["stt_language"]
        )
        stt_grid.attach(self.stt_language_combo, 0, 1, 1, 1)
        self.stt_model_info = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        self.stt_model_title = Gtk.Label()
        self.stt_model_title.set_xalign(0)
        self.stt_model_title.get_style_context().add_class("stt-model-title")
        self.stt_model_detail = Gtk.Label()
        self.stt_model_detail.set_xalign(0)
        self.stt_model_detail.get_style_context().add_class("stt-model-detail")
        self.stt_model_info.pack_start(
            self.stt_model_title,
            False,
            False,
            0,
        )
        self.stt_model_info.pack_start(
            self.stt_model_detail,
            False,
            False,
            0,
        )
        stt_grid.attach(self.stt_model_info, 1, 1, 1, 1)
        settings_content.pack_start(stt_grid, False, False, 0)
        self._update_stt_model_info(self.codex_settings["stt_language"])
        self._set_model_catalog(self.codex_models, persist=False)
        self._set_settings_sensitive(True)

        workspace = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        workspace.set_wide_handle(True)
        workspace.set_hexpand(True)
        workspace.set_vexpand(True)
        workspace.connect("size-allocate", self._allocate_workspace)
        content.pack_start(workspace, True, True, 0)
        self.workspace_paned = workspace

        conversation_frame = Gtk.Frame()
        conversation_frame.set_shadow_type(Gtk.ShadowType.NONE)
        conversation_frame.get_style_context().add_class("preparation-card")
        conversation_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        conversation_heading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        conversation_heading = Gtk.Label(label="Interview Conversation")
        conversation_heading.set_xalign(0)
        conversation_heading.get_style_context().add_class("section-title")
        conversation_heading_row.pack_start(
            conversation_heading,
            True,
            True,
            0,
        )
        self.conversation_refresh_button = Gtk.Button(
            label="Refresh Conversation"
        )
        self.conversation_refresh_button.connect(
            "clicked",
            self._refresh_conversation,
        )
        conversation_heading_row.pack_end(
            self.conversation_refresh_button,
            False,
            False,
            0,
        )
        conversation_box.pack_start(
            conversation_heading_row,
            False,
            False,
            0,
        )
        self.conversation_view = Gtk.TextView()
        self.conversation_view.set_editable(False)
        self.conversation_view.set_cursor_visible(False)
        self.conversation_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.conversation_view.set_left_margin(14)
        self.conversation_view.set_right_margin(14)
        self.conversation_view.set_top_margin(12)
        self.conversation_view.set_bottom_margin(12)
        self.conversation_view.get_style_context().add_class(
            "conversation-view"
        )
        self.conversation_buffer = self.conversation_view.get_buffer()
        self.conversation_interviewer_tag = self.conversation_buffer.create_tag(
            "conversation-interviewer",
            foreground="#8ec8ff",
            weight=Pango.Weight.BOLD,
        )
        self.conversation_codex_tag = self.conversation_buffer.create_tag(
            "conversation-codex",
            foreground="#ffc75c",
            weight=Pango.Weight.BOLD,
        )
        self.conversation_body_tag = self.conversation_buffer.create_tag(
            "conversation-body",
            foreground="#f2f4f7",
        )
        conversation_scroller = Gtk.ScrolledWindow()
        conversation_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        conversation_scroller.set_shadow_type(Gtk.ShadowType.IN)
        conversation_scroller.add(self.conversation_view)
        conversation_box.pack_start(conversation_scroller, True, True, 0)
        conversation_frame.add(conversation_box)
        workspace.pack1(conversation_frame, resize=True, shrink=False)

        self.context_frame, context_box = preparation_section(
            "Context",
            "Global Context와 이 세션의 override 및 sync 상태입니다.",
        )
        self.context_list_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
        )
        self.context_list_box.set_vexpand(True)
        context_box.pack_start(self.context_list_box, True, True, 0)
        context_primary_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.add_context_button = Gtk.Button(label="+ Context")
        self.add_context_button.set_sensitive(self.context_manager is not None)
        self.add_context_button.connect("clicked", self._new_context)
        context_primary_actions.pack_start(
            self.add_context_button,
            True,
            True,
            0,
        )
        self.refresh_context_button = Gtk.Button(label="Refresh")
        self.refresh_context_button.set_sensitive(
            self.context_manager is not None
        )
        self.refresh_context_button.connect("clicked", self._refresh_contexts)
        context_primary_actions.pack_start(
            self.refresh_context_button,
            True,
            True,
            0,
        )
        context_box.pack_start(context_primary_actions, False, False, 2)
        self.sync_context_button = Gtk.Button(label="Sync Context")
        self.sync_context_button.set_sensitive(
            self.codex_enabled
            and self.context_manager is not None
            and self.session_store is not None
        )
        self.sync_context_button.connect("clicked", self._sync_contexts)
        context_box.pack_start(
            self.sync_context_button,
            False,
            True,
            0,
        )
        self.context_rows = []
        self._refresh_contexts()
        workspace.pack2(self.context_frame, resize=True, shrink=False)
        self.context_panel_button.connect(
            "toggled",
            self._toggle_context_panel,
        )
        self._set_conversation_text(NO_INTERVIEW_THREAD_TEXT)

        self.connect("delete-event", self._delete)
        self.show_all()

    def _allocate_workspace(self, paned, allocation):
        position = (
            preparation_conversation_position(allocation.width)
            if self.context_panel_button.get_active()
            else allocation.width
        )
        if paned.get_position() != position:
            paned.set_position(position)

    def _toggle_context_panel(self, button):
        if button.get_active():
            self.context_frame.show_all()
        else:
            self.context_frame.hide()
        self._update_context_summary()
        self.workspace_paned.queue_resize()

    def _show_settings(self, *_args):
        self.settings_dialog.show_all()
        self.settings_dialog.run()
        self.settings_dialog.hide()

    def _close_settings(self, dialog, _event):
        dialog.response(Gtk.ResponseType.CLOSE)
        return True

    def _refresh_contexts(self, *_args):
        self.context_rows = (
            load_context_display_rows(self.context_manager, self.session_id)
            if self.context_manager is not None
            else []
        )
        for child in self.context_list_box.get_children():
            child.destroy()
        context_grid = Gtk.Grid(column_spacing=7, row_spacing=5)
        context_grid.set_hexpand(True)
        for column, title in enumerate(("SCOPE", "NAME", "STATUS", "")):
            header = Gtk.Label(label=title)
            header.set_xalign(0)
            header.get_style_context().add_class("context-header")
            context_grid.attach(header, column, 0, 1, 1)
        for row_number, row in enumerate(self.context_rows):
            grid_row = row_number + 1
            scope_label = Gtk.Label(label=row["scope"])
            scope_label.set_xalign(0.5)
            scope_label.get_style_context().add_class("context-badge")
            scope_label.get_style_context().add_class(
                context_scope_style(row["scope"])
            )
            display_label = Gtk.Label(label=row["display_name"])
            display_label.set_xalign(0)
            display_label.set_hexpand(True)
            display_label.set_ellipsize(Pango.EllipsizeMode.END)
            display_label.set_tooltip_text(row["filename"])
            status_label = Gtk.Label(label=row["status"])
            status_label.set_xalign(0.5)
            status_label.get_style_context().add_class("context-badge")
            status_label.get_style_context().add_class(
                context_status_style(row["status"])
            )
            edit_button = Gtk.Button(label="Edit")
            edit_button.get_style_context().add_class("context-edit")
            edit_button.connect("clicked", self._edit_context, row)
            context_grid.attach(scope_label, 0, grid_row, 1, 1)
            context_grid.attach(display_label, 1, grid_row, 1, 1)
            context_grid.attach(status_label, 2, grid_row, 1, 1)
            context_grid.attach(edit_button, 3, grid_row, 1, 1)
        if not self.context_rows:
            empty_label = Gtk.Label(label="등록된 Context가 없습니다.")
            empty_label.set_xalign(0)
            context_grid.attach(empty_label, 0, 1, 4, 1)
        context_scroller = Gtk.ScrolledWindow()
        context_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        context_scroller.set_shadow_type(Gtk.ShadowType.NONE)
        context_scroller.add(context_grid)
        self.context_list_box.pack_start(
            context_scroller,
            True,
            True,
            0,
        )
        self.context_list_box.show_all()
        self._update_context_summary()
        self._update_start_button()

    def _update_context_summary(self):
        if not hasattr(self, "context_panel_button"):
            return
        label, style_class = context_status_summary(self.context_rows)
        if self.context_sync_in_progress:
            label = "◌ Context Syncing..."
            style_class = "status-not-synced"
        arrow = "▾" if self.context_panel_button.get_active() else "▸"
        self.context_panel_button.set_label(f"{label}  {arrow}")
        style = self.context_panel_button.get_style_context()
        for candidate in (
            "status-synced",
            "status-changed",
            "status-not-synced",
        ):
            style.remove_class(candidate)
        style.add_class(style_class)

    def _new_context(self, *_args):
        dialog = NewContextDialog(self)
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                break
            try:
                self.context_manager.create_context(
                    dialog.context_scope(),
                    self.session_id,
                    dialog.context_name(),
                )
            except FileExistsError:
                self._show_context_error(
                    "Context가 이미 존재합니다.",
                    "같은 scope에 동일한 filename의 Context가 있습니다.",
                    parent=dialog,
                )
                continue
            except (OSError, ValueError) as error:
                self._show_context_error(
                    "Context를 만들 수 없습니다.",
                    str(error),
                    parent=dialog,
                )
                continue
            self._refresh_contexts()
            break
        dialog.destroy()

    def _sync_contexts(self, *_args):
        if (
            not getattr(self, "codex_enabled", True)
            or self.context_sync_in_progress
        ):
            return
        session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        if session is None:
            self._show_context_error(
                "Context를 sync할 수 없습니다.",
                "현재 세션 정보를 찾을 수 없습니다.",
            )
            return
        self._ensure_background_state()
        self.context_sync_in_progress = True
        self.context_sync_generation += 1
        generation = self.context_sync_generation
        self.sync_context_button.set_sensitive(False)
        self._update_context_summary()
        self._update_start_button()
        self._start_background_task(
            self._run_context_sync,
            session,
            generation,
        )

    def _run_context_sync(self, session, generation):
        client = None

        def client_factory(settings):
            nonlocal client
            client = self._new_background_client(settings)
            return client

        try:
            backend = InterviewThreadBackend(
                self.session_store,
                self.context_manager,
                client_factory,
            )
            result = backend.create(session)
            error = None
        except Exception as caught_error:
            result = None
            error = caught_error
        finally:
            self._unregister_background_client(client)
        GLib.idle_add(
            self._context_sync_finished,
            generation,
            result,
            error,
        )

    def _context_sync_finished(self, generation, _result, error):
        if generation != self.context_sync_generation:
            return False
        self.context_sync_in_progress = False
        if not self.active:
            return False
        self.sync_context_button.set_sensitive(
            getattr(self, "codex_enabled", True)
        )
        if error is not None:
            self._update_context_summary()
            self._update_start_button()
            self._show_context_error(
                "Context sync에 실패했습니다.",
                str(error),
            )
            return False
        self.session = self.session_store.get(self.session_id)
        self._refresh_contexts()
        self._refresh_conversation()
        return False

    def _set_conversation_text(self, text):
        self.conversation_buffer.set_text(text)

    def _refresh_conversation(self, *_args):
        if self.session_store is not None:
            self.session = self.session_store.get(self.session_id)
        self.conversation_load_generation += 1
        generation = self.conversation_load_generation
        if not getattr(self, "codex_enabled", True):
            self.conversation_refresh_button.set_sensitive(False)
            self._set_conversation_text("Codex is disabled in this mode.")
            return
        thread_id = (
            self.session.get("interview_thread_id")
            if self.session is not None
            else None
        )
        if not thread_id:
            self.conversation_refresh_button.set_sensitive(True)
            self._set_conversation_text(NO_INTERVIEW_THREAD_TEXT)
            return
        self.conversation_refresh_button.set_sensitive(False)
        self._set_conversation_text("면접 대화를 불러오는 중…")
        self._start_background_task(
            self._run_conversation_load,
            generation,
            thread_id,
            self.settings_snapshot(),
        )

    def _run_conversation_load(self, generation, thread_id, settings):
        client = None
        try:
            client = self._new_background_client(settings)
            client.connect()
            thread = client.read_thread(thread_id, include_turns=True)
            error = None
        except Exception as caught_error:
            thread = None
            error = caught_error
        finally:
            if client is not None:
                try:
                    client.stop()
                finally:
                    self._unregister_background_client(client)
        GLib.idle_add(
            self._conversation_load_finished,
            generation,
            thread_id,
            thread,
            error,
        )

    def _conversation_load_finished(
        self,
        generation,
        thread_id,
        thread,
        error,
    ):
        current_thread_id = (
            self.session.get("interview_thread_id")
            if self.session is not None
            else None
        )
        if (
            not self.active
            or generation != self.conversation_load_generation
            or thread_id != current_thread_id
        ):
            return False
        self.conversation_refresh_button.set_sensitive(True)
        if error is not None:
            self._set_conversation_text(
                f"면접 대화를 불러올 수 없습니다: {error}"
            )
            return False
        messages = interview_conversation_messages(thread or {})
        self.conversation_buffer.set_text("")
        if not messages:
            self._set_conversation_text(NO_INTERVIEW_CONVERSATION_TEXT)
            return False
        for message in messages:
            self._append_conversation_message(
                message["role"],
                message["text"],
            )
        return False

    def _append_conversation_message(self, role, text):
        end = self.conversation_buffer.get_end_iter()
        if self.conversation_buffer.get_char_count():
            self.conversation_buffer.insert(end, "\n\n")
            end = self.conversation_buffer.get_end_iter()
        if role == "interviewer":
            label = "INTERVIEWER\n"
            tag = self.conversation_interviewer_tag
        else:
            label = "CODEX\n"
            tag = self.conversation_codex_tag
        self.conversation_buffer.insert_with_tags(end, label, tag)
        end = self.conversation_buffer.get_end_iter()
        self.conversation_buffer.insert_with_tags(
            end,
            text,
            self.conversation_body_tag,
        )

    def interview_thread_id(self):
        if not can_start_interview(
            self.session,
            self.context_rows,
            getattr(self, "codex_enabled", True),
        ):
            return None
        return self.session.get("interview_thread_id")

    def _edit_context(self, _button, row):
        try:
            path = Path(row["path"]).resolve(strict=True)
            if not path.is_file():
                raise OSError(f"Context file does not exist: {path}")
            if not Gio.AppInfo.launch_default_for_uri(path.as_uri(), None):
                raise OSError(f"No application can open: {path.name}")
        except (OSError, ValueError, GLib.Error) as error:
            self._show_context_error(
                "Context 파일을 열 수 없습니다.",
                str(error),
            )

    def _show_context_error(self, title, detail, parent=None):
        dialog = Gtk.MessageDialog(
            transient_for=parent or self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def run_session(self):
        self._ensure_background_state()
        self.background_stop.clear()
        self.session = (
            self.session_store.get(self.session_id)
            if self.session_store is not None
            else None
        )
        self._refresh_contexts()
        self.sync_context_button.set_sensitive(
            getattr(self, "codex_enabled", True)
            and not self.context_sync_in_progress
            and self.context_manager is not None
            and self.session_store is not None
        )
        self.active = True
        if getattr(self, "codex_enabled", True):
            self._load_model_catalog()
        self._refresh_conversation()
        response = self.run()
        self.active = False
        self.context_sync_generation += 1
        self.model_catalog_load_generation += 1
        self.conversation_load_generation += 1
        self._stop_background_tasks()
        self.hide()
        return response

    def _load_model_catalog(self):
        self.model_catalog_load_generation += 1
        generation = self.model_catalog_load_generation
        self._start_background_task(
            self._run_model_catalog_load,
            generation,
            self.settings_snapshot(),
        )

    def _run_model_catalog_load(self, generation, settings):
        client = None
        try:
            client = self._new_background_client(settings)
            client.connect()
            models = client.list_models()
        except Exception:
            models = None
        finally:
            if client is not None:
                try:
                    client.stop()
                finally:
                    self._unregister_background_client(client)
        GLib.idle_add(self._model_catalog_finished, generation, models)

    def _ensure_background_state(self):
        if not hasattr(self, "background_stop"):
            self.background_stop = threading.Event()
            self.background_lock = threading.Lock()
            self.background_threads = set()
            self.background_clients = set()
        if not hasattr(self, "context_sync_generation"):
            self.context_sync_generation = 0

    def _start_background_task(self, target, *args):
        self._ensure_background_state()
        if self.background_stop.is_set():
            return None
        thread_holder = {}

        def run():
            try:
                target(*args)
            finally:
                with self.background_lock:
                    self.background_threads.discard(thread_holder["thread"])

        thread = threading.Thread(target=run, daemon=True)
        thread_holder["thread"] = thread
        with self.background_lock:
            self.background_threads.add(thread)
        thread.start()
        return thread

    def _new_background_client(self, settings):
        self._ensure_background_state()
        client = _new_codex_client(settings)
        with self.background_lock:
            if self.background_stop.is_set():
                client.stop()
                raise RuntimeError("Preparation background work is stopping")
            self.background_clients.add(client)
        return client

    def _unregister_background_client(self, client):
        if client is None:
            return
        self._ensure_background_state()
        with self.background_lock:
            self.background_clients.discard(client)

    def _stop_background_tasks(self):
        self._ensure_background_state()
        self.background_stop.set()
        with self.background_lock:
            clients = list(self.background_clients)
            threads = list(self.background_threads)
        for client in clients:
            try:
                client.stop()
            except Exception:
                pass
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(timeout=BACKGROUND_JOIN_TIMEOUT_SECONDS)

    def _model_catalog_finished(self, generation, models):
        if (
            not self.active
            or generation != self.model_catalog_load_generation
        ):
            return False
        if models:
            self._set_model_catalog(models, persist=True)
        return False

    def settings_snapshot(self):
        snapshot = dict(self.codex_settings)
        selected_model = next(
            (
                model for model in self.codex_models
                if model.get("model") == snapshot["codex_model"]
            ),
            None,
        )
        snapshot["codex_fast_mode"] = bool(
            snapshot["codex_fast_mode"]
            and selected_model is not None
            and model_supports_fast(selected_model)
        )
        return snapshot

    def _set_settings_sensitive(self, sensitive):
        codex_sensitive = sensitive and getattr(self, "codex_enabled", True)
        self.model_combo.set_sensitive(codex_sensitive)
        self.reasoning_combo.set_sensitive(codex_sensitive)
        self.fast_combo.set_sensitive(codex_sensitive)
        self.stt_language_combo.set_sensitive(sensitive)

    def _set_model_catalog(self, models, persist):
        visible = [model for model in models if not model.get("hidden", False)]
        if not visible:
            visible = list(FALLBACK_CODEX_MODELS)
        self.codex_models = visible
        selected_model = self.codex_settings["codex_model"]
        available = {model.get("model"): model for model in visible}
        if selected_model not in available:
            default = next(
                (model for model in visible if model.get("isDefault")),
                visible[0],
            )
            selected_model = default["model"]
            self.codex_settings["codex_model"] = selected_model

        self._updating_settings_ui = True
        self.model_combo.remove_all()
        for model in visible:
            model_id = model.get("model")
            if model_id:
                self.model_combo.append(
                    model_id,
                    model.get("displayName") or model_id,
                )
        self.model_combo.set_active_id(selected_model)
        self._populate_reasoning(available[selected_model])
        self._sync_fast(available[selected_model])
        self._updating_settings_ui = False
        if persist:
            self._persist_settings()

    def _populate_reasoning(self, model):
        efforts = model_reasoning_efforts(model)
        selected = self.codex_settings["codex_reasoning_effort"]
        if selected not in efforts:
            selected = model.get("defaultReasoningEffort")
            if selected not in efforts:
                selected = efforts[0]
            self.codex_settings["codex_reasoning_effort"] = selected
        self.reasoning_combo.remove_all()
        for effort in efforts:
            self.reasoning_combo.append(effort, effort.capitalize())
        self.reasoning_combo.set_active_id(selected)

    def _sync_fast(self, model):
        supported = model_supports_fast(model)
        self.fast_combo.set_item_sensitive("on", supported)
        if not supported:
            self.codex_settings["codex_fast_mode"] = False
        self.fast_combo.set_active_id(
            "on" if self.codex_settings["codex_fast_mode"] else "off"
        )

    def _model_changed(self, combo):
        if self._updating_settings_ui:
            return
        model_id = combo.get_active_id()
        model = next(
            (item for item in self.codex_models if item.get("model") == model_id),
            None,
        )
        if model is None:
            return
        self._updating_settings_ui = True
        self.codex_settings["codex_model"] = model_id
        self._populate_reasoning(model)
        self._sync_fast(model)
        self._updating_settings_ui = False
        self._persist_settings()

    def _reasoning_changed(self, combo):
        if self._updating_settings_ui:
            return
        effort = combo.get_active_id()
        if not effort:
            return
        self.codex_settings["codex_reasoning_effort"] = effort
        self._persist_settings()

    def _fast_changed(self, combo):
        if self._updating_settings_ui:
            return
        requested = combo.get_active_id() == "on"
        selected_model = next(
            (
                model for model in self.codex_models
                if model.get("model") == self.codex_settings["codex_model"]
            ),
            None,
        )
        enabled = bool(
            requested
            and selected_model is not None
            and model_supports_fast(selected_model)
        )
        self.codex_settings["codex_fast_mode"] = enabled
        if requested and not enabled:
            self._updating_settings_ui = True
            combo.set_active_id("off")
            self._updating_settings_ui = False
        self._persist_settings()

    def _stt_language_changed(self, combo):
        if self._updating_settings_ui:
            return
        language = combo.get_active_id()
        if language not in {"en", "ja"}:
            return
        self.codex_settings["stt_language"] = language
        self._update_stt_model_info(language)
        self._persist_settings()

    def _update_stt_model_info(self, language):
        presentation = stt_presentation(language)
        self.stt_model_title.set_text(presentation["title"])
        self.stt_model_detail.set_text(
            f"model: {presentation['model']}  ·  {presentation['mode']}"
        )
        self.stt_summary_label.set_text(stt_status_summary(language))
        self.stt_summary_label.set_tooltip_text(
            f"{presentation['title']}\n"
            f"model: {presentation['model']}\n"
            f"{presentation['mode']}"
        )
        if hasattr(self, "runtime_summary_label"):
            summary = preparation_runtime_summary(self.runtime, language)
            self.runtime_summary_label.set_text(summary)
            self.runtime_summary_label.set_tooltip_text(summary)

    def _persist_settings(self):
        if self.session_store is not None:
            self.session_store.update_settings(
                self.session_id,
                self.codex_settings,
            )

    def _update_start_button(self):
        self.start_button.set_sensitive(
            not self.context_sync_in_progress
            and can_start_interview(
                self.session,
                self.context_rows,
                getattr(self, "codex_enabled", True),
            )
        )

    def _delete(self, *_args):
        self.response(Gtk.ResponseType.DELETE_EVENT)
        return True


class InterviewControlWindow(Gtk.Window):
    """One draggable control surface for interview navigation and exit."""

    def __init__(self, position, on_back, on_close, on_toggle_visibility):
        super().__init__(title="INTERVIEW CONTROLS")
        self.on_close = on_close
        # A non-focusable UTILITY window can display in WSLg but not receive
        # reliable pointer input after focus moves to a native Windows app.
        # Keep this compact, but let the normal window manager own movement
        # and input delivery.
        self.set_decorated(True)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_size_request(132, 44)
        self.move(*position)
        self.connect("delete-event", self._delete)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_border_width(4)

        drag_handle = Gtk.EventBox()
        drag_handle.set_visible_window(False)
        drag_handle.set_tooltip_text("드래그해서 제어창 이동")
        drag_handle.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        drag_handle.connect("button-press-event", self._drag)
        drag_label = Gtk.Label(label="⠿")
        drag_label.get_style_context().add_class("control-drag")
        drag_handle.add(drag_label)
        row.pack_start(drag_handle, True, True, 0)

        self.visibility_button = Gtk.Button()
        self.visibility_button.set_can_focus(False)
        self.visibility_button.get_style_context().add_class("control-button")
        self.visibility_button.get_style_context().add_class(
            "visibility-button"
        )
        self.visibility_button.connect(
            "clicked",
            lambda _button: on_toggle_visibility(),
        )
        row.pack_start(self.visibility_button, False, False, 0)
        self.set_live_windows_hidden(False)

        back_button = Gtk.Button(label="←")
        back_button.set_tooltip_text("면접 준비 화면으로 돌아가기")
        back_button.get_style_context().add_class("control-button")
        back_button.connect("clicked", lambda _button: on_back())
        row.pack_start(back_button, False, False, 0)

        close_button = Gtk.Button(label="×")
        close_button.set_tooltip_text("앱 종료")
        close_button.get_style_context().add_class("close-button")
        close_button.connect("clicked", lambda _button: on_close())
        row.pack_start(close_button, False, False, 0)

        self.add(row)
        self.get_style_context().add_class("control")

    def set_live_windows_hidden(self, hidden):
        icon_name = (
            "view-reveal-symbolic" if hidden else "view-conceal-symbolic"
        )
        self.visibility_button.set_image(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        )
        self.visibility_button.set_tooltip_text(
            "Restore interview windows" if hidden else "Hide interview windows"
        )

    def _drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _delete(self, *_args):
        self.on_close()
        return True


class TranscriptWindow(Gtk.Window):
    def __init__(
        self,
        role,
        title,
        width,
        height,
        position,
        on_close,
        focus_mode=False,
        on_back=None,
        show_close=True,
    ):
        super().__init__(title=title)
        self.role = role
        self.on_close = on_close
        self.focus_mode = focus_mode
        self.on_back = on_back
        self.show_close = show_close
        self.last_focus_scroll_at = None
        self.smooth_scroll_delta = 0.0
        self.boundary_status = None
        self.response_status = None
        self.answer_history = []
        self.active_answer = ""
        self.focus_placeholder = ""
        self.latest_answer_mark = None
        self.set_default_size(width, height)
        # WSLg does not reliably retain undecorated UTILITY overlays after a
        # native Windows application takes focus.  These are real persistent
        # top-level windows: give them normal decorations and WM resize grips.
        self.set_decorated(True)
        self.set_resizable(True)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_size_request(320, 100)
        self.move(*position)
        self.connect("delete-event", self._delete)
        self.connect("button-press-event", self._drag)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(2 if self.focus_mode else 12)
        box.set_hexpand(True)
        box.set_vexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.get_style_context().add_class("heading")
        close_button = None
        if self.show_close:
            close_button = Gtk.Button(label="×")
            close_button.set_relief(Gtk.ReliefStyle.NONE)
            close_button.set_can_focus(False)
            close_button.set_tooltip_text("Close")
            close_button.get_style_context().add_class("close-button")
            close_button.connect("clicked", lambda _button: self.on_close())
        header.pack_start(heading, True, True, 0)
        if close_button is not None:
            header.pack_end(close_button, False, False, 0)

        if self.focus_mode:
            self.text = Gtk.TextView()
            self.text.set_editable(False)
            self.text.set_cursor_visible(False)
            self.text.set_can_focus(False)
            self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.text.set_left_margin(0)
            self.text.set_right_margin(28)
            self.text.set_top_margin(0)
            self.text.set_bottom_margin(ANSWER_POSITION_GUIDE_HEIGHT)
            self.text.get_buffer().set_text("")
            self.text.get_style_context().add_class("focus-transcript")
        else:
            self.text = Gtk.Label(label="Moonshine loading…")
            self.text.set_xalign(0)
            self.text.set_yalign(0)
            self.text.set_line_wrap(True)
            self.text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self.text.set_max_width_chars(TEXT_WIDTH_CHARS)
            self.text.set_selectable(False)
            self.text.get_style_context().add_class("transcript")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.add(self.text)

        box.pack_start(header, False, False, 0)
        if self.focus_mode:
            self.focus_scroller = scroller
            answer_overlay = Gtk.Overlay()
            answer_overlay.add(scroller)

            self.position_guide = Gtk.Frame()
            self.position_guide.set_shadow_type(Gtk.ShadowType.NONE)
            self.position_guide.set_size_request(-1, ANSWER_POSITION_GUIDE_HEIGHT)
            self.position_guide.set_halign(Gtk.Align.FILL)
            self.position_guide.set_valign(Gtk.Align.START)
            self.position_guide.get_style_context().add_class("position-guide")
            answer_overlay.add_overlay(self.position_guide)
            answer_overlay.set_overlay_pass_through(self.position_guide, True)

            if close_button is not None:
                close_button.set_halign(Gtk.Align.END)
                close_button.set_valign(Gtk.Align.START)
                close_button.set_margin_top(2)
                close_button.set_margin_end(2)
                answer_overlay.add_overlay(close_button)
            if self.on_back is not None:
                back_button = Gtk.Button(label="←")
                back_button.set_tooltip_text("면접 준비 화면으로 돌아가기")
                back_button.set_halign(Gtk.Align.START)
                back_button.set_valign(Gtk.Align.START)
                back_button.set_margin_top(2)
                back_button.set_margin_start(2)
                back_button.connect(
                    "clicked",
                    lambda _button: self.on_back(),
                )
                answer_overlay.add_overlay(back_button)
            box.pack_start(answer_overlay, True, True, 0)

            if self.role == "ANSWER":
                response_status_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=8,
                )
                self.response_status = Gtk.Label(label="")
                self.response_status.set_xalign(0)
                self.response_status.set_single_line_mode(True)
                self.response_status.get_style_context().add_class(
                    "response-status"
                )
                response_status_row.pack_start(
                    self.response_status,
                    True,
                    True,
                    0,
                )
                shortcut_reminder = Gtk.Label(
                    label="F8 NEW  ·  F9 CONTINUE"
                )
                shortcut_reminder.set_xalign(1)
                shortcut_reminder.set_single_line_mode(True)
                shortcut_reminder.get_style_context().add_class(
                    "shortcut-reminder"
                )
                response_status_row.pack_end(
                    shortcut_reminder,
                    False,
                    False,
                    0,
                )
                box.pack_end(response_status_row, False, False, 0)

            for widget in (self, answer_overlay, scroller, self.text):
                widget.add_events(Gdk.EventMask.SCROLL_MASK)
                widget.connect("scroll-event", self._focus_scroll)
            scroller.connect("size-allocate", self._answer_view_resized)
        else:
            box.pack_start(scroller, True, True, 0)
            if self.role == "INTERVIEWER":
                self.boundary_status = Gtk.Label(label="")
                self.boundary_status.set_xalign(0)
                self.boundary_status.set_single_line_mode(True)
                self.boundary_status.get_style_context().add_class(
                    "boundary-status"
                )
                box.pack_end(self.boundary_status, False, False, 0)

        resize_right = self._resize_handle(
            Gdk.WindowEdge.EAST,
            "Drag horizontally to change width",
            8,
            -1,
            "resize-horizontal",
        )
        resize_bottom = self._resize_handle(
            Gdk.WindowEdge.SOUTH,
            "Drag vertically to change height",
            -1,
            8,
            "resize-vertical",
        )
        resize_corner = self._resize_handle(
            Gdk.WindowEdge.SOUTH_EAST,
            "Drag to change width and height",
            14,
            14,
            "resize-corner",
        )
        resize_corner.add(Gtk.Label(label="↘"))
        resize_right.set_vexpand(True)
        resize_bottom.set_hexpand(True)

        grid = Gtk.Grid()
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        grid.attach(box, 0, 0, 1, 1)
        grid.attach(resize_right, 1, 0, 1, 1)
        grid.attach(resize_bottom, 0, 1, 1, 1)
        grid.attach(resize_corner, 1, 1, 1, 1)
        self.add(grid)
        self.get_style_context().add_class(role.lower())

    def set_text(self, text):
        if text:
            if self.focus_mode:
                self.active_answer = ""
                self.answer_history.append(text)
                self.focus_placeholder = ""
                self._render_focus_answers()
                buffer = self.text.get_buffer()
                answer_start = len("\n\n".join(self.answer_history[:-1]))
                if answer_start:
                    answer_start += 2
                self._set_latest_answer_mark(
                    buffer.get_iter_at_offset(answer_start)
                )
                GLib.idle_add(
                    self._align_latest_answer_once,
                    self.latest_answer_mark,
                )
            else:
                self.text.set_text(text)
        elif self.focus_mode:
            self.active_answer = ""
            self._render_focus_answers()

    def discard_current_answer(self, *, remove_completed=False):
        if not self.focus_mode:
            return
        self.active_answer = ""
        if remove_completed and self.answer_history:
            self.answer_history.pop()
        self._render_focus_answers()

    def set_status(self, text):
        if self.focus_mode:
            self.focus_placeholder = text
            if not self.answer_history and not self.active_answer:
                self._render_focus_answers()
        else:
            self.text.set_text(text)

    def set_boundary_status(self, text):
        if self.boundary_status is not None:
            self.boundary_status.set_text(text)

    def set_response_status(self, text):
        if self.response_status is not None:
            self.response_status.set_text(text)

    def start_stream(self, text):
        if not self.focus_mode:
            self.text.set_text(text)
            return
        self.active_answer = ""
        self.focus_placeholder = ""
        self._render_focus_answers()
        buffer = self.text.get_buffer()
        if self.answer_history:
            buffer.insert(buffer.get_end_iter(), "\n\n")
        self._set_latest_answer_mark(buffer.get_end_iter())
        buffer.insert(buffer.get_end_iter(), text)
        self.active_answer = text
        GLib.idle_add(
            self._align_latest_answer_once,
            self.latest_answer_mark,
        )

    def append_stream(self, text):
        if not text:
            return
        if not self.focus_mode:
            self.text.set_text(f"{self.text.get_text()}{text}")
            return
        self.active_answer += text
        self.text.get_buffer().insert(
            self.text.get_buffer().get_end_iter(),
            text,
        )

    def finish_stream(self, text):
        if not self.focus_mode:
            self.set_text(text)
            return
        buffer = self.text.get_buffer()
        if self.latest_answer_mark is None:
            self.set_text(text)
            return
        answer_start = buffer.get_iter_at_mark(self.latest_answer_mark)
        current = buffer.get_text(
            answer_start,
            buffer.get_end_iter(),
            True,
        )
        if current != text:
            buffer.delete(answer_start, buffer.get_end_iter())
            buffer.insert(buffer.get_end_iter(), text)
        self.active_answer = ""
        self.answer_history.append(text)
        self.focus_placeholder = ""

    def _render_focus_answers(self):
        self._clear_latest_answer_mark()
        parts = [*self.answer_history]
        if self.active_answer:
            parts.append(self.active_answer)
        rendered = "\n\n".join(parts) or self.focus_placeholder
        self.text.get_buffer().set_text(rendered)

    def _clear_latest_answer_mark(self):
        if self.latest_answer_mark is not None:
            self.text.get_buffer().delete_mark(self.latest_answer_mark)
            self.latest_answer_mark = None

    def _set_latest_answer_mark(self, position):
        self._clear_latest_answer_mark()
        self.latest_answer_mark = self.text.get_buffer().create_mark(
            None,
            position,
            True,
        )

    def _align_latest_answer_once(self, mark):
        if mark is not self.latest_answer_mark:
            return False
        buffer = self.text.get_buffer()
        answer_start = buffer.get_iter_at_mark(mark)
        answer_rect = self.text.get_iter_location(answer_start)
        adjustment = self.focus_scroller.get_vadjustment()
        minimum = adjustment.get_lower()
        maximum = max(
            minimum,
            adjustment.get_upper() - adjustment.get_page_size(),
        )
        adjustment.set_value(max(minimum, min(answer_rect.y, maximum)))
        return False

    def _focus_scroll(self, _widget, event):
        if not self.focus_mode:
            return True

        step = 0
        if event.direction == Gdk.ScrollDirection.UP:
            step = -1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            step = 1
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            self.smooth_scroll_delta += event.delta_y
            if abs(self.smooth_scroll_delta) < ANSWER_SMOOTH_SCROLL_THRESHOLD:
                return True
            step = 1 if self.smooth_scroll_delta > 0 else -1
            self.smooth_scroll_delta = 0.0
        if not step:
            return True

        now = time.monotonic()
        if (
            self.last_focus_scroll_at is not None
            and now - self.last_focus_scroll_at
            < ANSWER_SCROLL_DEBOUNCE_MS / 1000
        ):
            return True
        self.last_focus_scroll_at = now
        adjustment = self.focus_scroller.get_vadjustment()
        minimum = adjustment.get_lower()
        maximum = max(minimum, adjustment.get_upper() - adjustment.get_page_size())
        target = adjustment.get_value() + step * ANSWER_CONTENT_SCROLL_PIXELS
        adjustment.set_value(max(minimum, min(target, maximum)))
        return True

    def _answer_view_resized(self, _widget, allocation):
        if self.focus_mode:
            self.text.set_bottom_margin(
                max(ANSWER_POSITION_GUIDE_HEIGHT, allocation.height * 2)
            )

    def _drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        if event.button == 3:
            self.begin_resize_drag(
                Gdk.WindowEdge.SOUTH_EAST,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _resize_handle(self, edge, tooltip, width, height, style_class):
        handle = Gtk.EventBox()
        handle.set_visible_window(True)
        handle.set_size_request(width, height)
        handle.set_tooltip_text(tooltip)
        handle.get_style_context().add_class("resize-handle")
        handle.get_style_context().add_class(style_class)
        handle.connect("button-press-event", self._resize, edge)
        return handle

    def _resize(self, _widget, event, edge):
        if event.button == 1:
            self.begin_resize_drag(
                edge,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _delete(self, *_args):
        self.on_close()
        return True


class InterviewApp:
    def __init__(self, codex_thread_id, codex_settings=None, runtime=None):
        self.session_dir, self.log_path = create_app_session()
        self.runtime = runtime_options() if runtime is None else dict(runtime)
        self.codex_thread_id = codex_thread_id
        live_codex_settings = normalize_codex_settings(codex_settings)
        self.codex_model = live_codex_settings["codex_model"]
        self.codex_reasoning_effort = live_codex_settings[
            "codex_reasoning_effort"
        ]
        self.codex_fast_mode = live_codex_settings["codex_fast_mode"]
        self.stt_language = live_codex_settings["stt_language"]
        self.codex_enabled = self.runtime["codex_enabled"]
        self.exit_action = None
        self.running = True
        self.question_count = 0
        self.codex_request_count = 0
        self.active_codex_generation = 0
        self.codex_request_states = {}
        self.conversation_context = []
        self.codex_context_cursor = 0
        self.codex_state_lock = threading.Lock()
        self.last_f8_at = None
        self.last_f9_at = None
        self.last_commit_state = None
        self.live_windows_hidden = False
        self.moonshine_ready = False
        self.audio_started = False
        self.audio_failure_reported = False
        self.audio_backend = self.runtime["audio_backend"]
        self.bridge_statuses = []
        self.window_state = self._load_window_state()
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        screen_width = geometry.width

        remote_default = (
            geometry.x + (screen_width - 720) // 2,
            geometry.y + 48,
            720,
            170,
        )
        answer_default = (
            geometry.x + (screen_width - 720) // 2,
            geometry.y + 230,
            720,
            300,
        )
        control_default = (
            answer_default[0],
            max(geometry.y + 8, answer_default[1] - 54),
            132,
            44,
        )
        remote_state = self.window_state.get("INTERVIEWER", remote_default)
        answer_state = self.window_state.get("ANSWER", answer_default)
        control_state = self.window_state.get("CONTROL", control_default)
        self.remote_window = TranscriptWindow(
            "INTERVIEWER",
            "INTERVIEWER",
            remote_state[2],
            remote_state[3],
            remote_state[:2],
            self.shutdown,
            show_close=False,
        )
        self.answer_window = TranscriptWindow(
            "ANSWER",
            "CODEX",
            answer_state[2],
            answer_state[3],
            answer_state[:2],
            self.shutdown,
            focus_mode=True,
            show_close=False,
        )
        self.control_window = InterviewControlWindow(
            control_state[:2],
            self.back_to_chat,
            self.shutdown,
            self.toggle_live_windows_visibility,
        )
        for window in (self.remote_window, self.answer_window):
            window.connect("key-press-event", self._key_pressed)

        self._install_css()
        self.remote_window.show_all()
        self.answer_window.show_all()
        self.control_window.show_all()
        self.answer_window.set_status(
            "Codex loading…" if self.codex_enabled
            else "Codex disabled · STT Diagnostic mode"
        )
        self.asr_worker = MoonshineStreamingWorker(
            self._moonshine_ready,
            self._moonshine_preview,
            self._moonshine_error,
            on_auto_commit=self._moonshine_auto_commit,
            dispatch=lambda callback, *args: GLib.idle_add(callback, *args),
            language=self.stt_language,
        )
        self.platform_backend = create_platform_backend(
            self.audio_backend,
            worker=self.asr_worker,
            on_pcm=self._moonshine_pcm,
            on_error=self._audio_error,
            on_f8=self._on_f8,
            on_f9=self._on_f9,
            on_stop=self.shutdown,
            on_status=self._platform_status,
            gio=Gio,
            idle_add=GLib.idle_add,
            app_command_path=Path(__file__).resolve(),
            is_running=lambda: self.running,
        )
        self.remote_audio = self.platform_backend
        platform_start = self.platform_backend.prepare()
        remote_source = platform_start["remote_source"]
        hotkey_status = platform_start["global_f8"]
        f9_hotkey_status = platform_start["global_f9"]
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": APP_VERSION,
            "remote_source": remote_source,
            "audio_backend": self.audio_backend,
            "microphone_capture": False,
            "asr_backend": moonshine_asr_backend(self.stt_language),
            "moonshine_update_interval_ms": 500,
            "moonshine_word_timestamps": False,
            "language": self.stt_language,
            "app_mode": self.runtime["mode"],
            "logging_enabled": self.runtime["logging_enabled"],
            "stt_diagnostics_enabled": self.runtime["diagnostics_enabled"],
            "codex_enabled": self.codex_enabled,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "codex_fast_mode": self.codex_fast_mode,
            "codex_transport": "app_server_stdio",
            "codex_session_scope": "persistent_selected_thread",
            "codex_thread_id": self.codex_thread_id,
            "candidate_response_source": (
                "completed_codex_answer_assumed_spoken_"
                "superseded_answer_not_spoken"
            ),
            "question_transcript_mode": "f8_cursor_barrier_force_snapshot",
            "preview_transcription": "moonshine_transcript_lines",
            "global_f8": hotkey_status,
            "global_f9": f9_hotkey_status,
            "test_label": TEST_LABEL,
        })
        self.codex_worker = None
        if self.codex_enabled:
            self.codex_worker = create_live_codex_worker(
                self._codex_ready,
                self.codex_thread_id,
                live_codex_settings,
            )
        model_label = (
            "Moonshine Small" if self.stt_language == "en" else "Moonshine Base JA"
        )
        self.remote_window.set_status(f"{model_label} loading…")
        self.asr_worker.start()

    def _install_css(self):
        install_application_css()

    def _load_window_state(self):
        try:
            return json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_window_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONFIG_DIR.chmod(0o700)
        state = {}
        for role, window in (
            ("INTERVIEWER", self.remote_window),
            ("ANSWER", self.answer_window),
            ("CONTROL", self.control_window),
        ):
            x, y = window.get_position()
            width, height = window.get_size()
            state[role] = [x, y, width, height]
        WINDOW_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        WINDOW_STATE_PATH.chmod(0o600)

    def _key_pressed(self, _window, event):
        if event.keyval == Gdk.KEY_F8:
            self._on_f8()
            return True
        if event.keyval == Gdk.KEY_F9:
            self._on_f9()
            return True
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval == Gdk.KEY_q:
            self.shutdown()
            return True
        return False

    def _moonshine_ready(self, result, error):
        if not self.running:
            return False
        if error:
            self.remote_window.set_status(f"Moonshine startup error: {error}")
            append_log(self.log_path, {
                "event": "moonshine_startup_error",
                "error": str(error),
            })
            return False
        self.moonshine_ready = True
        self.remote_window.set_status("Listening…")
        self.remote_window.set_boundary_status(BOUNDARY_STATUS_LISTENING)
        append_log(self.log_path, {
            "event": "moonshine_ready",
            "model": result["model"],
            "language": result["language"],
            "load_seconds": round(result["load_seconds"], 3),
            "update_interval_ms": result["update_interval_ms"],
        })
        if not self.audio_started:
            self.platform_backend.start()
            self.audio_started = True
        return False

    def _moonshine_pcm(self, pcm_audio, start_cursor, end_cursor):
        if getattr(self, "audio_failure_reported", False):
            return
        try:
            accepted = self.asr_worker.submit_pcm(
                pcm_audio,
                start_cursor,
                end_cursor,
            )
            if not accepted:
                raise RuntimeError("Moonshine worker is not accepting PCM")
        except Exception as error:
            self.audio_failure_reported = True
            self.remote_audio.abort()
            GLib.idle_add(self._moonshine_error, error)

    def _moonshine_preview(self, snapshot):
        if not self.running:
            return False
        if snapshot["text"]:
            self.remote_window.set_boundary_status(BOUNDARY_STATUS_LISTENING)
            self.remote_window.set_text(snapshot["text"])
        return False

    def _moonshine_error(self, error):
        if not self.running:
            return False
        if not getattr(self, "audio_failure_reported", False):
            self.audio_failure_reported = True
            self.remote_audio.abort()
        self.audio_started = False
        self.remote_window.set_status(f"Moonshine error: {error}")
        self.remote_window.set_boundary_status(BOUNDARY_STATUS_ERROR)
        append_log(self.log_path, {
            "event": "moonshine_error",
            "error": str(error),
        })
        return False

    def _moonshine_question_ready(
        self,
        question_number,
        commit_started,
        result,
        error,
        commit_source="f8",
    ):
        if not self.running:
            return False
        pending_question_number = (
            question_number
            if question_number is not None
            else self.question_count + 1
        )
        if error:
            self.answer_window.set_status(f"Moonshine error: {error}")
            append_log(self.log_path, {
                "event": "question_error",
                "question": pending_question_number,
                "commit_source": commit_source,
                "error": str(error),
            })
            return False

        if not result.get("committed", True):
            self.answer_window.set_status("Waiting for question…")
            append_log(self.log_path, {
                "event": "question_duplicate_suppressed",
                "question": pending_question_number,
                "commit_source": commit_source,
                "target_sample_cursor": result["target_sample_cursor"],
                "last_committed_sample_cursor": (
                    self.asr_worker.last_committed_sample_cursor
                ),
            })
            return False

        question_text = result["text"].strip()
        elapsed = time.perf_counter() - commit_started
        latency_field = (
            {"f8_to_question_ms": round(elapsed * 1000, 1)}
            if commit_source == "f8"
            else {"silence_commit_to_question_ms": round(elapsed * 1000, 1)}
        )
        if not question_text:
            self.answer_window.set_status("No question detected")
            return False

        if question_number is None:
            self.question_count += 1
            question_number = self.question_count

        append_log(self.log_path, {
            "event": "question",
            "question": question_number,
            "commit_source": commit_source,
            "text": question_text,
            "stt_seconds": round(elapsed, 3),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
            "transcript_lines": result["lines"],
            "captured_sample_cursor": result["captured_sample_cursor"],
            "target_sample_cursor": result["target_sample_cursor"],
            "queued_sample_cursor": result["queued_sample_cursor"],
            "consumed_sample_cursor": result["consumed_sample_cursor"],
            "cursor_complete": result["cursor_complete"],
            "audio_drop_samples": result["audio_drop_samples"],
            "max_backlog_ms": result["max_backlog_ms"],
            "barrier_wait_ms": result["barrier_wait_ms"],
            "force_update_ms": result["force_update_ms"],
            **latency_field,
        })

        self.remote_window.set_text(result["display_text"] or question_text)
        if commit_source == "silence":
            self.remote_window.set_boundary_status(BOUNDARY_STATUS_AUTO)
        elif commit_source == "f8":
            self.remote_window.set_boundary_status(BOUNDARY_STATUS_F8)
        context_index = len(self.conversation_context)
        self.conversation_context.append(("INTERVIEWER", question_text))
        codex_generation = None
        if self.codex_enabled:
            codex_generation = self._request_codex_answer(
                question_number,
                question_text,
            )
        else:
            self.answer_window.set_status(
                "Codex disabled · question logged only"
            )
            append_log(self.log_path, {
                "event": "codex_request_skipped",
                "question": question_number,
                "reason": "disabled_for_audio_test",
            })
        self.last_commit_state = {
            "commit_source": commit_source,
            "text": question_text,
            "question_number": question_number,
            "target_sample_cursor": result["target_sample_cursor"],
            "conversation_context_index": context_index,
            "codex_generation": codex_generation,
        }
        return False

    def _moonshine_auto_commit(self, result, error):
        if not self.running:
            return False
        if error:
            return self._moonshine_error(error)
        append_log(self.log_path, {
            "event": "silence_segment",
            "text": result["text"].strip(),
            "segment_preserved": result.get("segment_preserved", False),
            "accumulated_segment_count": result.get(
                "accumulated_segment_count", 0
            ),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
            "target_sample_cursor": result["target_sample_cursor"],
            "consumed_sample_cursor": result["consumed_sample_cursor"],
            "cursor_complete": result["cursor_complete"],
            "audio_drop_samples": result["audio_drop_samples"],
            "max_backlog_ms": result["max_backlog_ms"],
            "force_update_ms": result["force_update_ms"],
        })
        self.remote_window.set_boundary_status(BOUNDARY_STATUS_AUTO)
        return False

    def _continuation_base_is_valid(self, base):
        if not base or not base.get("text"):
            return False
        if base.get("commit_source") not in {"f8", "f9_continuation"}:
            return False
        if getattr(self, "last_commit_state", None) != base:
            return False
        context_index = base.get("conversation_context_index")
        if not isinstance(context_index, int):
            return False
        if not 0 <= context_index < len(self.conversation_context):
            return False
        return self.conversation_context[context_index] == (
            "INTERVIEWER",
            base["text"],
        )

    def _moonshine_continuation_ready(
        self,
        base,
        commit_started,
        result,
        error,
    ):
        if not self.running:
            return False
        question_number = base["question_number"]
        if error:
            self.answer_window.set_status(f"Moonshine error: {error}")
            append_log(self.log_path, {
                "event": "question_error",
                "question": question_number,
                "commit_source": "f9_continuation",
                "error": str(error),
            })
            return False
        if not result.get("committed", True):
            self.answer_window.set_status("Waiting for question…")
            append_log(self.log_path, {
                "event": "question_duplicate_suppressed",
                "question": question_number,
                "commit_source": "f9_continuation",
                "target_sample_cursor": result["target_sample_cursor"],
                "last_committed_sample_cursor": (
                    self.asr_worker.last_committed_sample_cursor
                ),
            })
            return False

        segment_text = result["text"].strip()
        rejection_reason = None
        if not segment_text:
            rejection_reason = "empty_current_segment"
        elif result["target_sample_cursor"] <= base["target_sample_cursor"]:
            rejection_reason = "cursor_not_newer"
        elif not self._continuation_base_is_valid(base):
            rejection_reason = "previous_question_changed"
        if rejection_reason is not None:
            self.answer_window.set_status("No continuation applied")
            append_log(self.log_path, {
                "event": "question_continuation_rejected",
                "commit_source": "f9_continuation",
                "reason": rejection_reason,
                "previous_question": question_number,
                "previous_target_sample_cursor": base["target_sample_cursor"],
                "target_sample_cursor": result["target_sample_cursor"],
                "segment_text": segment_text,
            })
            return False

        combined_text = base["text"].rstrip() + " " + segment_text.lstrip()
        context_index = base["conversation_context_index"]
        self.conversation_context[context_index] = (
            "INTERVIEWER",
            combined_text,
        )
        elapsed = time.perf_counter() - commit_started
        append_log(self.log_path, {
            "event": "question",
            "question": question_number,
            "commit_source": "f9_continuation",
            "text": combined_text,
            "continuation_segment_text": segment_text,
            "previous_question": base["question_number"],
            "previous_commit_source": base["commit_source"],
            "previous_target_sample_cursor": base["target_sample_cursor"],
            "stt_seconds": round(elapsed, 3),
            "f9_to_question_ms": round(elapsed * 1000, 1),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
            "transcript_lines": result["lines"],
            "captured_sample_cursor": result["captured_sample_cursor"],
            "target_sample_cursor": result["target_sample_cursor"],
            "queued_sample_cursor": result["queued_sample_cursor"],
            "consumed_sample_cursor": result["consumed_sample_cursor"],
            "cursor_complete": result["cursor_complete"],
            "audio_drop_samples": result["audio_drop_samples"],
            "max_backlog_ms": result["max_backlog_ms"],
            "barrier_wait_ms": result["barrier_wait_ms"],
            "force_update_ms": result["force_update_ms"],
        })
        self.remote_window.set_text(combined_text)
        self.remote_window.set_boundary_status(BOUNDARY_STATUS_F9)
        codex_generation = None
        if self.codex_enabled:
            codex_generation = self._request_codex_answer(
                question_number,
                combined_text,
                supersedes_generation=base["codex_generation"],
                correction={"previous_text": base["text"]},
            )
        else:
            self.answer_window.set_status(
                "Codex disabled · corrected question logged only"
            )
            append_log(self.log_path, {
                "event": "codex_request_skipped",
                "question": question_number,
                "commit_source": "f9_continuation",
                "reason": "disabled_for_audio_test",
            })
        self.last_commit_state = {
            "commit_source": "f9_continuation",
            "text": combined_text,
            "question_number": question_number,
            "target_sample_cursor": result["target_sample_cursor"],
            "conversation_context_index": context_index,
            "codex_generation": codex_generation,
        }
        return False

    def _request_codex_answer(
        self,
        question_number,
        question_text,
        supersedes_generation=None,
        correction=None,
    ):
        with self.codex_state_lock:
            context_end = len(self.conversation_context)
            context = self.conversation_context[
                self.codex_context_cursor:context_end
            ]
        if context and context[-1] == ("INTERVIEWER", question_text):
            context = context[:-1]
        context_text = "\n".join(
            f"{role}: {text}"
            for role, text in context
        ) or "(none)"
        prompt = f"""NEW CONVERSATION SINCE THE PREVIOUS REQUEST:
{context_text}

CURRENT INTERVIEWER QUESTION:
{question_text}
"""
        self.codex_request_count += 1
        request_number = self.codex_request_count
        generation = request_number
        stream_started = False
        stale_stream_logged = False
        recovery_failed = False
        superseded = []
        with self.codex_state_lock:
            for old_generation, state in self.codex_request_states.items():
                if state["status"] in {"pending", "running", "recovering"}:
                    previous_status = state["status"]
                    state["status"] = "superseded"
                    state["spoken"] = False
                    superseded.append({
                        "generation": old_generation,
                        "request": state["request"],
                        "question": state["question"],
                        "previous_status": previous_status,
                    })
            if (
                supersedes_generation is not None
                and supersedes_generation in self.codex_request_states
                and not any(
                    item["generation"] == supersedes_generation
                    for item in superseded
                )
            ):
                state = self.codex_request_states[supersedes_generation]
                previous_status = state["status"]
                state["status"] = "superseded"
                state["spoken"] = False
                superseded.append({
                    "generation": supersedes_generation,
                    "request": state["request"],
                    "question": state["question"],
                    "previous_status": previous_status,
                })
            self.active_codex_generation = generation
            self.codex_request_states[generation] = {
                "request": request_number,
                "question": question_number,
                "status": "pending",
                "spoken": None,
            }
        for old in superseded:
            append_log(self.log_path, {
                "event": "codex_request_superseded",
                **old,
                "superseded_by_generation": generation,
                "superseded_by_question": question_number,
                "spoken": False,
            })
        if superseded:
            superseded_questions = ", ".join(
                str(item["question"]) for item in superseded
            )
            prompt = f"""LIVE TURN CONTROL:
Codex answer generation for earlier live question(s) {superseded_questions} was superseded. Any partial assistant output from those interrupted turns was NOT SPOKEN by the candidate. Do not treat it as a candidate statement.

{prompt}"""
        if correction is not None:
            prompt = f"""CONTINUATION CORRECTION:
The previous interviewer question was incomplete. The combined question below replaces it in full. The previous answer was NOT SPOKEN by the candidate. Answer only the corrected combined question and do not treat the earlier answer as a candidate statement.

PREVIOUS INCOMPLETE QUESTION:
{correction["previous_text"]}

{prompt}"""
        pending_response_status = (
            RESPONSE_STATUS_UPDATING
            if correction is not None
            else RESPONSE_STATUS_THINKING
        )
        self.answer_window.set_response_status(pending_response_status)
        self.answer_window.set_status("Thinking…")
        if superseded or correction is not None:
            remove_completed = correction is not None and any(
                item["generation"] == supersedes_generation
                and item["previous_status"] == "completed"
                for item in superseded
            )
            discard = getattr(
                self.answer_window,
                "discard_current_answer",
                None,
            )
            if discard is None:
                self.answer_window.set_text("")
            else:
                discard(remove_completed=remove_completed)
        append_log(self.log_path, {
            "event": "codex_request",
            "request": request_number,
            "question": question_number,
            "generation": generation,
            "thread_id": getattr(self, "codex_thread_id", None),
            "model": getattr(self, "codex_model", CODEX_MODEL),
            "reasoning_effort": getattr(
                self,
                "codex_reasoning_effort",
                CODEX_REASONING,
            ),
            "fast_mode": getattr(
                self,
                "codex_fast_mode",
                CODEX_FAST_MODE,
            ),
            "context_items": len(context),
            "superseded_requests": len(superseded),
            "correction": correction is not None,
            "supersedes_generation": supersedes_generation,
        })

        def started():
            with self.codex_state_lock:
                state = self.codex_request_states[generation]
                if state["status"] == "pending":
                    state["status"] = "running"
                self.codex_context_cursor = max(
                    self.codex_context_cursor,
                    context_end,
                )
            append_log(self.log_path, {
                "event": "codex_turn_start",
                "request": request_number,
                "question": question_number,
                "generation": generation,
                "thread_id": getattr(self, "codex_thread_id", None),
            })

        def streamed(delta, elapsed):
            nonlocal stream_started, stale_stream_logged
            if not self.running:
                return False
            with self.codex_state_lock:
                is_current = (
                    generation == self.active_codex_generation
                    and self.codex_request_states[generation]["status"]
                    != "superseded"
                )
            if not is_current:
                if not stale_stream_logged:
                    stale_stream_logged = True
                    append_log(self.log_path, {
                        "event": "codex_stale_stream_ignored",
                        "request": request_number,
                        "question": question_number,
                        "generation": generation,
                    })
                return False
            if stream_started:
                self.answer_window.append_stream(delta)
            else:
                stream_started = True
                self.answer_window.set_response_status(RESPONSE_STATUS_READY)
                self.answer_window.start_stream(delta)
                append_log(self.log_path, {
                    "event": "codex_stream_start",
                    "request": request_number,
                    "question": question_number,
                    "generation": generation,
                    "elapsed_seconds": round(elapsed, 3),
                })
            return False

        def recovery(stage, details):
            nonlocal stream_started, recovery_failed
            if not self.running:
                return False
            with self.codex_state_lock:
                state = self.codex_request_states[generation]
                is_current = (
                    generation == self.active_codex_generation
                    and state["status"] != "superseded"
                )
                if stage == "started" and is_current:
                    state["status"] = "recovering"
                elif stage == "resumed" and is_current:
                    state["status"] = "running"
                elif stage == "failed" and is_current:
                    state["status"] = "unavailable"
                    state["spoken"] = False
                    recovery_failed = True
            append_log(self.log_path, {
                "event": f"codex_recovery_{stage}",
                "request": request_number,
                "question": question_number,
                "generation": generation,
                **details,
            })
            if not is_current:
                return False
            if stage == "started":
                stream_started = False
                self.answer_window.set_text("")
                self.answer_window.set_status("Reconnecting Codex…")
                self.answer_window.set_response_status(
                    pending_response_status
                )
            elif stage == "resumed":
                self.answer_window.set_status("Thinking… (retry 1/1)")
            elif stage == "failed":
                self.answer_window.set_status("Codex unavailable")
                self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
            return False

        def finished(result, error):
            if not self.running:
                return False
            with self.codex_state_lock:
                state = self.codex_request_states[generation]
                is_current = (
                    generation == self.active_codex_generation
                    and state["status"] != "superseded"
                )
                if not is_current:
                    state["status"] = "superseded_finished"
                    state["spoken"] = False
                elif error:
                    state["status"] = (
                        "unavailable" if recovery_failed else "failed"
                    )
                    state["spoken"] = False
                else:
                    state["status"] = "completed"
                    state["spoken"] = True
            if not is_current:
                append_log(self.log_path, {
                    "event": "codex_superseded_finished",
                    "request": request_number,
                    "question": question_number,
                    "generation": generation,
                    "spoken": False,
                    "error": str(error) if error else None,
                })
                return False
            if error:
                self.answer_window.set_status(
                    "Codex unavailable"
                    if recovery_failed
                    else f"Codex error: {error}"
                )
                self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
                append_log(self.log_path, {
                    "event": "codex_error",
                    "request": request_number,
                    "question": question_number,
                    "generation": generation,
                    "error": str(error),
                    "recovery_failed": recovery_failed,
                })
            else:
                if stream_started:
                    self.answer_window.finish_stream(result["text"])
                else:
                    self.answer_window.set_text(result["text"])
                    self.answer_window.set_response_status(
                        RESPONSE_STATUS_READY
                    )
                append_log(self.log_path, {
                    "event": "codex_response",
                    "request": request_number,
                    "question": question_number,
                    "generation": generation,
                    "text": result["text"],
                    "elapsed_seconds": round(result["elapsed"], 3),
                    "first_token_seconds": (
                        round(result["first_token_seconds"], 3)
                        if result["first_token_seconds"] is not None
                        else None
                    ),
                    "first_visible_seconds": (
                        round(result["first_visible_seconds"], 3)
                        if result["first_visible_seconds"] is not None
                        else None
                    ),
                    "stream_delta_count": result["stream_delta_count"],
                    "thread_id": result["thread_id"],
                    "turn_id": result["turn_id"],
                    "spoken": True,
                    "recovery_attempts": result.get("recovery_attempts", 0),
                })
            return False

        self.codex_worker.submit_latest(
            generation,
            prompt,
            finished,
            streamed,
            started,
            recovery,
        )
        return generation

    def _codex_ready(self, result, error):
        if not self.running:
            return False
        if error:
            self.answer_window.set_status(f"Codex startup error: {error}")
            self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
            append_log(self.log_path, {
                "event": "codex_app_server_error",
                "error": str(error),
            })
        else:
            append_log(self.log_path, {
                "event": "codex_app_server_ready",
                "thread_id": result["thread_id"],
                "startup_seconds": round(result["startup_seconds"], 3),
                "process_id": result.get("process_id"),
            })
        return False

    def _platform_status(self, status):
        """Record status already normalized by the selected platform backend."""
        self.bridge_statuses.append(dict(status))
        append_log(self.log_path, status)

    def _hotkey_status(self, text, hotkey_event):
        if hotkey_event is None:
            self.answer_window.set_status(text)
        else:
            GLib.idle_add(self.answer_window.set_status, text)

    @staticmethod
    def _hotkey_log_fields(hotkey_event):
        if hotkey_event is None:
            return {}
        return {
            "hotkey_transport": WINDOWS_BRIDGE_AUDIO_BACKEND,
            "hotkey_sequence": hotkey_event.get("sequence"),
            "hotkey_timestamp_ns": hotkey_event.get("timestamp_ns"),
        }

    @staticmethod
    def _capture_result(capture, enqueue):
        """Accept both the legacy cursor pair and bridge press-time state."""
        result = capture(enqueue)
        target_cursor, accepted = result[:2]
        state = result[2] if len(result) > 2 else {
            "received_cursor": target_cursor,
            "queued_cursor": None,
            "consumed_cursor": None,
            "audio_drop_samples": None,
        }
        return target_cursor, accepted, state

    @staticmethod
    def _press_cursor_log_fields(target_cursor, state):
        received_cursor = state.get("received_cursor", target_cursor)
        consumed_cursor = state.get("consumed_cursor")
        backlog_samples = (
            None if consumed_cursor is None
            else max(received_cursor - consumed_cursor, 0)
        )
        return {
            "received_sample_cursor_at_press": received_cursor,
            "queued_sample_cursor_at_press": state.get("queued_cursor"),
            "consumed_sample_cursor_at_press": consumed_cursor,
            "backlog_samples_at_press": backlog_samples,
            "backlog_ms_at_press": (
                None if backlog_samples is None
                else round(backlog_samples / SAMPLE_RATE * 1000, 1)
            ),
            "audio_drop_samples_at_press": state.get("audio_drop_samples"),
        }

    def _on_f8(self, *, capture_sample_cursor_and=None, hotkey_event=None):
        now = time.perf_counter()
        if self.last_f8_at is not None and now - self.last_f8_at < ENTER_DEBOUNCE_MS / 1000:
            append_log(self.log_path, {
                "event": "f8_ignored",
                "reason": "debounce",
                "interval_ms": round((now - self.last_f8_at) * 1000, 1),
            })
            return False
        self.last_f8_at = now
        if not self.moonshine_ready or not self.audio_started:
            append_log(self.log_path, {
                "event": "f8_ignored",
                "reason": "moonshine_not_ready",
            })
            if hotkey_event is None:
                self.remote_window.set_status("Moonshine is still loading…")
            else:
                GLib.idle_add(
                    self.remote_window.set_status, "Moonshine is still loading…"
                )
            return False
        callback = lambda result, error: self._moonshine_question_ready(
            None,
            now,
            result,
            error,
            commit_source="f8",
        )
        try:
            capture = capture_sample_cursor_and or self.remote_audio.capture_sample_cursor_and
            target_cursor, accepted, press_state = self._capture_result(capture,
                lambda cursor: self.asr_worker.request_snapshot(cursor, callback)
            )
        except Exception as error:
            if hotkey_event is None:
                self._moonshine_question_ready(None, now, None, error)
            else:
                GLib.idle_add(self._moonshine_question_ready, None, now, None, error)
            return False
        if not accepted:
            error = RuntimeError("Moonshine worker rejected F8 snapshot")
            if hotkey_event is None:
                self._moonshine_question_ready(None, now, None, error)
            else:
                GLib.idle_add(self._moonshine_question_ready, None, now, None, error)
            return False
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": self.question_count + 1,
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
            **self._press_cursor_log_fields(target_cursor, press_state),
            **self._hotkey_log_fields(hotkey_event),
        })
        self._hotkey_status("Transcribing question…", hotkey_event)
        return False

    def _on_f9(self, *, capture_sample_cursor_and=None, hotkey_event=None):
        now = time.perf_counter()
        if self.last_f9_at is not None and now - self.last_f9_at < ENTER_DEBOUNCE_MS / 1000:
            append_log(self.log_path, {
                "event": "f9_ignored",
                "reason": "debounce",
                "interval_ms": round((now - self.last_f9_at) * 1000, 1),
            })
            return False
        self.last_f9_at = now
        if not self.moonshine_ready or not self.audio_started:
            append_log(self.log_path, {
                "event": "f9_ignored",
                "reason": "moonshine_not_ready",
            })
            if hotkey_event is None:
                self.remote_window.set_status("Moonshine is still loading…")
            else:
                GLib.idle_add(
                    self.remote_window.set_status, "Moonshine is still loading…"
                )
            return False
        base = getattr(self, "last_commit_state", None)
        if not self._continuation_base_is_valid(base):
            append_log(self.log_path, {
                "event": "f9_ignored",
                "reason": "no_valid_previous_question",
            })
            self._hotkey_status("No previous question to continue", hotkey_event)
            return False
        base = dict(base)
        callback = lambda result, error: self._moonshine_continuation_ready(
            base,
            now,
            result,
            error,
        )
        try:
            capture = capture_sample_cursor_and or self.remote_audio.capture_sample_cursor_and
            target_cursor, accepted, press_state = self._capture_result(capture,
                lambda cursor: self.asr_worker.request_snapshot(cursor, callback)
            )
        except Exception as error:
            if hotkey_event is None:
                self._moonshine_continuation_ready(base, now, None, error)
            else:
                GLib.idle_add(
                    self._moonshine_continuation_ready, base, now, None, error
                )
            return False
        if not accepted:
            error = RuntimeError("Moonshine worker rejected F9 snapshot")
            if hotkey_event is None:
                self._moonshine_continuation_ready(base, now, None, error)
            else:
                GLib.idle_add(
                    self._moonshine_continuation_ready, base, now, None, error
                )
            return False
        append_log(self.log_path, {
            "event": "f9_trigger",
            "commit_source": "f9_continuation",
            "previous_question": base["question_number"],
            "previous_target_sample_cursor": base["target_sample_cursor"],
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
            **self._press_cursor_log_fields(target_cursor, press_state),
            **self._hotkey_log_fields(hotkey_event),
        })
        self._hotkey_status("Transcribing continuation…", hotkey_event)
        return False

    def _audio_error(self, role, error):
        append_log(self.log_path, {
            "event": "audio_error", "role": role, "error": str(error),
        })
        self.audio_started = False

        def show_error():
            window = self._window(role)
            window.set_status(f"Audio error: {error}")
            window.set_boundary_status(BOUNDARY_STATUS_ERROR)
            return False

        GLib.idle_add(show_error)

    def _window(self, role):
        return self.remote_window

    def back_to_chat(self, *_args):
        return self._stop("back")

    def toggle_live_windows_visibility(self, *_args):
        self.live_windows_hidden = not self.live_windows_hidden
        if self.live_windows_hidden:
            self.remote_window.hide()
            self.answer_window.hide()
        else:
            self.remote_window.show()
            self.answer_window.show()
        self.control_window.set_live_windows_hidden(
            self.live_windows_hidden
        )
        return False

    def shutdown(self, *_args):
        return self._stop("quit")

    def _stop(self, exit_action):
        if not self.running:
            return False
        self.running = False
        self.exit_action = exit_action
        cleanup_errors = []

        def run_cleanup(name, callback):
            try:
                callback()
            except Exception as error:
                cleanup_errors.append((name, error))

        cursor_state = None
        cursor_snapshot = getattr(self.remote_audio, "cursor_state", None)
        if cursor_snapshot is not None:
            try:
                cursor_state = cursor_snapshot()
            except Exception as error:
                cleanup_errors.append(("audio_cursor_state", error))

        run_cleanup("window_state", self._save_window_state)
        run_cleanup("audio", self.platform_backend.stop)
        run_cleanup("moonshine", self.asr_worker.stop)
        if self.codex_worker is not None:
            run_cleanup("codex", self.codex_worker.stop)
        run_cleanup("session_log", lambda: append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": exit_action,
            "questions": self.question_count,
            "codex_requests": self.codex_request_count,
            "audio_backend": getattr(self, "audio_backend", PULSEAUDIO_AUDIO_BACKEND),
            "final_audio_cursor_state": cursor_state,
            "windows_bridge_stopped": (
                getattr(self, "audio_backend", None)
                == WINDOWS_BRIDGE_AUDIO_BACKEND
            ),
            "cleanup_errors": [
                {"resource": name, "error": str(error)}
                for name, error in cleanup_errors
            ],
        }))
        for window in (
            self.remote_window,
            self.answer_window,
            self.control_window,
        ):
            run_cleanup("window_hide", window.hide)
        run_cleanup("gtk_quit", Gtk.main_quit)
        return False


def main():
    install_application_css()
    active_runtime = runtime_options()
    store = SessionStore(SESSION_STORE_PATH)
    context_manager = ContextManager(CONFIG_DIR)
    while True:
        session = choose_interview_session(
            store,
            context_manager,
            codex_enabled=active_runtime["codex_enabled"],
        )
        if session == SESSION_RESPONSE_BACK:
            launch_interview_launcher()
            return
        if session is None:
            return
        session_id = session["session_id"]
        preparation = PreparationDialog(
            session_id,
            session_store=store,
            session_settings=session.get("settings"),
            context_manager=context_manager,
            runtime=active_runtime,
        )
        while True:
            response = preparation.run_session()
            if response == CHAT_RESPONSE_BACK:
                preparation.destroy()
                break
            if response != CHAT_RESPONSE_START_INTERVIEW:
                preparation.destroy()
                return

            live_codex_settings = preparation.settings_snapshot()
            interview_thread_id = preparation.interview_thread_id()
            if active_runtime["codex_enabled"] and interview_thread_id is None:
                continue
            app = InterviewApp(
                interview_thread_id,
                codex_settings=live_codex_settings,
                runtime=active_runtime,
            )
            signal_source = GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT,
                signal.SIGINT,
                app.shutdown,
            )
            Gtk.main()
            GLib.source_remove(signal_source)
            if app.exit_action == "back":
                continue
            preparation.destroy()
            return


if __name__ == "__main__":
    main()
