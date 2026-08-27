#!/usr/bin/env python3
"""Local interview transcription app with F8-triggered Codex answers."""

import fcntl
import os
import socket
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
TRIGGER_SOCKET = RUNTIME_DIR / "interview-assistant-trigger.sock"
TRIGGER_LOCK_PATH = RUNTIME_DIR / "interview-assistant-trigger.lock"


def send_app_command(command):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(command, str(TRIGGER_SOCKET))
    except OSError as error:
        print(f"Interview Assistant is not running: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def send_benchmark_wav_start(wav_name):
    """Record the external playback boundary in the active session log."""
    encoded_name = wav_name.encode("utf-8")
    if not encoded_name or b"\x00" in encoded_name:
        print("Benchmark WAV name must be non-empty text", file=sys.stderr)
        return 2
    return send_app_command(b"BENCHMARK_WAV_START:" + encoded_name)


# GNOME의 전역 단축키 명령은 이 경로만 실행한다. 무거운 모듈은 불러오지 않는다.
if __name__ == "__main__" and "--trigger" in sys.argv:
    raise SystemExit(send_app_command(b"F8"))
if __name__ == "__main__" and "--trigger-f7" in sys.argv:
    raise SystemExit(send_app_command(b"F7"))
if __name__ == "__main__" and "--trigger-f9" in sys.argv:
    raise SystemExit(send_app_command(b"F9"))
if __name__ == "__main__" and "--stop" in sys.argv:
    raise SystemExit(send_app_command(b"STOP"))
if __name__ == "__main__" and "--benchmark-wav-start" in sys.argv:
    if len(sys.argv) != 3:
        print("Usage: interview_app.py --benchmark-wav-start <wav-name>")
        raise SystemExit(2)
    raise SystemExit(send_benchmark_wav_start(sys.argv[2]))


import json
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime

from audio.capture import (
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioStream,
    get_interviewer_audio_source,
    start_audio_capture,
)
from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRecoverableError,
    CodexAppServerTransportError,
)
from codex.worker import (
    CODEX_DEVELOPER_INSTRUCTIONS,
    CODEX_FAST_MODE,
    CODEX_MODEL,
    CODEX_REASONING,
    CODEX_TIMEOUT_SECONDS,
    CodexWorker,
)
from context_manager import CONTEXT_STATUS_SYNCED, ContextManager
from interview.controller import (
    InterviewApp,
    append_log,
    create_app_session,
    moonshine_asr_backend,
)
from interview.headless import HeadlessInterviewApp, _HeadlessTranscriptWindow
from interview_thread_backend import InterviewThreadBackend
from moonshine_streaming_worker import MoonshineStreamingWorker
from session_store import (
    SessionStore,
    normalize_codex_settings,
)
from ui.live import (
    ANSWER_CONTENT_SCROLL_PIXELS,
    ANSWER_POSITION_GUIDE_HEIGHT,
    ANSWER_SCROLL_DEBOUNCE_MS,
    ANSWER_SMOOTH_SCROLL_THRESHOLD,
    TEXT_WIDTH_CHARS,
    InterviewControlWindow,
    TranscriptWindow,
)
from ui.preparation import (
    APP_MODE_TITLES,
    BACKGROUND_JOIN_TIMEOUT_SECONDS,
    CHAT_RESPONSE_BACK,
    CHAT_RESPONSE_START_INTERVIEW,
    FALLBACK_CODEX_MODELS,
    INTERVIEW_QUESTION_MARKER,
    NO_INTERVIEW_CONVERSATION_TEXT,
    NO_INTERVIEW_THREAD_TEXT,
    PREPARATION_CONVERSATION_RATIO,
    PREPARATION_MESSAGE_MARKER,
    STT_PRESENTATION,
    MOONSHINE_VOICE_VERSION,
    PreparationDialog,
    can_start_interview,
    context_display_name,
    context_display_rows,
    context_scope_style,
    context_status_style,
    context_status_summary,
    interview_conversation_messages,
    load_context_display_rows,
    model_reasoning_efforts,
    model_supports_fast,
    preparation_conversation_position,
    preparation_runtime_summary,
    preparation_section,
    runtime_options,
    stt_model_detail,
    stt_presentation,
)
from ui.session_dialogs import (
    SESSION_RESPONSE_ARCHIVE,
    SESSION_RESPONSE_ARCHIVE_ALL,
    SESSION_RESPONSE_BACK,
    SESSION_RESPONSE_NEW,
    SESSION_RESPONSE_RENAME,
    CompactMenuSelector,
    NewContextDialog,
    RenameSessionDialog,
    SessionChooserDialog,
    _archive_session,
    _archive_sessions,
    _confirm_archive,
    _new_codex_client,
    _show_session_error,
    archive_persisted_codex_session,
    choose_interview_session,
    initial_session_settings,
    session_list_row,
    stt_status_summary,
)
from ui.styles import install_application_css


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
APP_MODE = os.environ.get("INTERVIEW_APP_MODE", "normal")
CODEX_ENABLED = os.environ.get("INTERVIEW_DISABLE_CODEX", "0") == "0"
ENTER_DEBOUNCE_MS = 300
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
STT_DIAGNOSTICS_ENABLED = (
    os.environ.get("INTERVIEW_STT_DIAGNOSTICS", "0") != "0"
)
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
LOG_WRITE_LOCK = threading.Lock()
BOUNDARY_STATUS_LISTENING = "● LISTENING"
BOUNDARY_STATUS_F7 = "✓ F7 CHECKPOINT"
BOUNDARY_STATUS_F8 = "✓ F8 NEW"
BOUNDARY_STATUS_F9 = "✓ F9 CONTINUED"
BOUNDARY_STATUS_ERROR = "ERROR"
RESPONSE_STATUS_READY = "●"
RESPONSE_STATUS_THINKING = "◌"
RESPONSE_STATUS_UPDATING = "◌"
RESPONSE_STATUS_ERROR = "×"
INTERVIEW_CHECKPOINT_MARKER = "INTERVIEWER CONTEXT CHECKPOINT:"
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
HOTKEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant/"
)
HOTKEY_F7_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant-checkpoint/"
)
HOTKEY_F9_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant-continuation/"
)


def create_live_codex_worker(on_ready, thread_id, settings):
    """Compatibility factory using the public worker alias in this module."""
    snapshot = normalize_codex_settings(settings)
    return CodexWorker(
        on_ready,
        thread_id=thread_id,
        model=snapshot["codex_model"],
        effort=snapshot["codex_reasoning_effort"],
        fast_mode=snapshot["codex_fast_mode"],
    )


def launch_interview_launcher():
    return subprocess.Popen(
        [sys.executable, str(APP_DIR / "interview_launcher.py")],
        cwd=APP_DIR,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


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
