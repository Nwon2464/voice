#!/usr/bin/env python3
"""Local interview transcription app with F8-triggered Codex answers."""

import os
import socket
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
TRIGGER_SOCKET = RUNTIME_DIR / "interview-assistant-trigger.sock"


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


# GNOME의 전역 단축키 명령은 이 경로만 실행한다. 무거운 모듈은 불러오지 않는다.
if __name__ == "__main__" and "--trigger" in sys.argv:
    raise SystemExit(send_app_command(b"F8"))
if __name__ == "__main__" and "--trigger-f9" in sys.argv:
    raise SystemExit(send_app_command(b"F9"))
if __name__ == "__main__" and "--stop" in sys.argv:
    raise SystemExit(send_app_command(b"STOP"))


import json
import queue
import shlex
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
from moonshine_streaming_worker import MoonshineStreamingWorker
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
LANGUAGE = os.environ.get("INTERVIEW_LANGUAGE", "en")
CODEX_MODEL = os.environ.get("INTERVIEW_CODEX_MODEL", DEFAULT_CODEX_MODEL)
CODEX_REASONING = os.environ.get(
    "INTERVIEW_CODEX_REASONING",
    DEFAULT_CODEX_REASONING_EFFORT,
)
CODEX_FAST_MODE = False
CODEX_ENABLED = os.environ.get("INTERVIEW_DISABLE_CODEX", "0") == "0"
CODEX_TIMEOUT_SECONDS = 60
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
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
TEXT_WIDTH_CHARS = shutil.get_terminal_size(fallback=(100, 24)).columns
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
LOG_WRITE_LOCK = threading.Lock()
BOUNDARY_STATUS_LISTENING = "● LISTENING"
BOUNDARY_STATUS_AUTO = "✓ AUTO"
BOUNDARY_STATUS_F8 = "✓ F8 NEW"
BOUNDARY_STATUS_F9 = "✓ F9 CONTINUED"
RESPONSE_STATUS_READY = "● READY"
RESPONSE_STATUS_THINKING = "◌ THINKING..."
RESPONSE_STATUS_UPDATING = "◌ UPDATING..."
RESPONSE_STATUS_ERROR = "ERROR"

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
SESSION_INITIALIZATION_TEXT = (
    "Interview Assistant persistent session initialized. "
    "This is background metadata, not an interview question."
)
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
HOTKEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant/"
)
HOTKEY_F9_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant-continuation/"
)


def get_interviewer_audio_source():
    sink = subprocess.check_output(
        ["pactl", "get-default-sink"], text=True
    ).strip()
    return f"{sink}.monitor"


def start_audio_capture(source):
    return subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-f", "pulse",
            "-fragment_size", "640",
            "-sample_rate", str(SAMPLE_RATE),
            "-channels", "1",
            "-i", source,
            "-f", "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


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
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir, session_dir / "session.jsonl"


class AudioStream:
    """Capture raw PCM and forward it with an absolute sample cursor."""

    def __init__(self, role, source, on_pcm, on_error):
        self.role = role
        self.source = source
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.process = None
        self.thread = None
        self.stopped = threading.Event()
        self.condition = threading.Condition()
        self.total_samples = 0

    def start(self):
        self.process = start_audio_capture(self.source)
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def capture_sample_cursor_and(self, enqueue):
        """Record the absolute cursor and enqueue F8 before future PCM."""
        with self.condition:
            cursor = self.total_samples
            accepted = enqueue(cursor)
            return cursor, accepted

    def _read_loop(self):
        try:
            while not self.stopped.is_set():
                data = self.process.stdout.read(320)
                if not data:
                    break
                if len(data) % SAMPLE_WIDTH:
                    raise RuntimeError("Capture returned an incomplete s16le sample")

                with self.condition:
                    chunk_start = self.total_samples
                    self.total_samples += len(data) // SAMPLE_WIDTH
                    chunk_end = self.total_samples
                    self.on_pcm(data, chunk_start, chunk_end)
                    self.condition.notify_all()
        except Exception as error:
            self.on_error(self.role, error)


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


def session_list_row(session):
    settings = normalize_codex_settings(session.get("settings"))
    created = session.get("created_at") or session.get("name", "")
    if "T" in created:
        created = created.split("+", 1)[0].replace("T", " ")[:16]
    return (
        created,
        session["thread_id"],
        settings["codex_model"],
        settings["codex_reasoning_effort"],
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


class SessionChooserDialog(Gtk.Dialog):
    """Keyboard-friendly chooser for Interview Assistant-owned sessions."""

    def __init__(self, sessions, preferred_thread_id=None):
        super().__init__(title="Interview Assistant Sessions")
        self.set_default_size(720, 420)
        self.set_border_width(12)
        self.set_modal(True)

        self.new_button = self.add_button("새 세션", SESSION_RESPONSE_NEW)
        self.archive_button = self.add_button(
            "세션 삭제",
            SESSION_RESPONSE_ARCHIVE,
        )
        self.add_button("취소", Gtk.ResponseType.CANCEL)
        self.open_button = self.add_button("선택", Gtk.ResponseType.OK)
        self.open_button.get_style_context().add_class("suggested-action")

        content = self.get_content_area()
        content.set_spacing(10)
        heading = Gtk.Label()
        heading.set_markup("<b>면접 세션 선택</b>")
        heading.set_xalign(0)
        content.pack_start(heading, False, False, 0)

        help_text = Gtk.Label(
            label="↑/↓로 이동하고 Enter를 누르면 선택한 세션으로 들어갑니다."
        )
        help_text.set_xalign(0)
        content.pack_start(help_text, False, False, 0)

        self.sessions_by_thread_id = {
            session["thread_id"]: session for session in sessions
        }
        self.model = Gtk.ListStore(str, str, str, str)
        preferred_path = None
        for index, session in enumerate(sessions):
            self.model.append(session_list_row(session))
            if session["thread_id"] == preferred_thread_id:
                preferred_path = Gtk.TreePath.new_from_indices([index])

        self.tree = Gtk.TreeView(model=self.model)
        self.tree.set_headers_visible(True)
        self.tree.set_activate_on_single_click(False)
        self.tree.append_column(
            Gtk.TreeViewColumn(
                "Created",
                Gtk.CellRendererText(),
                text=0,
            )
        )
        id_renderer = Gtk.CellRendererText()
        id_renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.tree.append_column(
            Gtk.TreeViewColumn("세션 ID", id_renderer, text=1)
        )
        self.tree.append_column(
            Gtk.TreeViewColumn("Model", Gtk.CellRendererText(), text=2)
        )
        self.tree.append_column(
            Gtk.TreeViewColumn("Effort", Gtk.CellRendererText(), text=3)
        )
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
        return self.sessions_by_thread_id[model[tree_iter][1]]

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
        self.archive_button.set_sensitive(has_selection)


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


def create_persisted_codex_session(settings=None):
    client = _new_codex_client(settings)
    try:
        result = client.start(ephemeral=False)
        client.inject_items([{
            "type": "message",
            "role": "developer",
            "content": [{
                "type": "input_text",
                "text": SESSION_INITIALIZATION_TEXT,
            }],
        }])
        return result
    finally:
        client.stop()


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
        text="선택한 세션을 보관함으로 옮길까요?",
    )
    dialog.format_secondary_text(
        f"{session['name']}\n{session['thread_id']}\n\n나중에 복구할 수 있습니다."
    )
    dialog.add_button("취소", Gtk.ResponseType.CANCEL)
    dialog.add_button("보관함으로 이동", Gtk.ResponseType.OK)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def choose_interview_session(store):
    preferred_thread_id = None
    while True:
        dialog = SessionChooserDialog(
            store.active(),
            preferred_thread_id=preferred_thread_id,
        )
        response = dialog.run()
        selected = dialog.selected_session()
        dialog.destroy()

        if response == SESSION_RESPONSE_NEW:
            try:
                settings = normalize_codex_settings()
                result = create_persisted_codex_session(settings)
                created = datetime.now().astimezone()
                thread_id = result["thread_id"]
                store.add(
                    thread_id,
                    created.strftime("%Y-%m-%d %H:%M"),
                    created.isoformat(timespec="seconds"),
                    settings,
                )
                preferred_thread_id = thread_id
            except Exception as error:
                _show_session_error(error)
            continue

        if response == SESSION_RESPONSE_ARCHIVE and selected is not None:
            if _confirm_archive(selected):
                try:
                    archive_persisted_codex_session(selected["thread_id"])
                    store.mark_archived(selected["thread_id"])
                    preferred_thread_id = None
                except Exception as error:
                    if isinstance(error, CodexAppServerError) and (
                        "no rollout found" in str(error).lower()
                    ):
                        store.mark_archived(selected["thread_id"])
                        preferred_thread_id = None
                    else:
                        _show_session_error(error)
            continue

        if response == Gtk.ResponseType.OK and selected is not None:
            store.mark_used(selected["thread_id"])
            selected["last_used_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            return selected

        return None


CHAT_RESPONSE_BACK = 10
CHAT_RESPONSE_START_INTERVIEW = 11
CHAT_HISTORY_PAGE_TURNS = 50


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


class PreparationChatDialog(Gtk.Dialog):
    """Chat with the selected Codex thread before starting audio capture."""

    def __init__(self, thread_id, session_store=None, session_settings=None):
        super().__init__(title="Interview Preparation")
        self.thread_id = thread_id
        self.session_store = session_store
        self.codex_settings = normalize_codex_settings(session_settings)
        self.codex_models = list(FALLBACK_CODEX_MODELS)
        self._updating_settings_ui = False
        self.worker = None
        self.ready = False
        self.busy = False
        self.active = False
        self.stream_started = False
        self.history_turns = []
        self.history_start = 0
        self.set_default_size(820, 640)
        self.set_border_width(12)
        self.set_modal(False)

        self.back_button = self.add_button("뒤로가기", CHAT_RESPONSE_BACK)
        self.start_button = self.add_button(
            "면접 시작",
            CHAT_RESPONSE_START_INTERVIEW,
        )
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.set_sensitive(False)

        content = self.get_content_area()
        content.set_spacing(10)
        heading = Gtk.Label()
        heading.set_markup("<b>면접 준비 채팅</b>")
        heading.set_xalign(0)
        content.pack_start(heading, False, False, 0)

        session_label = Gtk.Label(label=f"세션 ID: {thread_id}")
        session_label.set_xalign(0)
        session_label.set_selectable(True)
        content.pack_start(session_label, False, False, 0)

        settings_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        settings_row.pack_start(Gtk.Label(label="Model"), False, False, 0)
        self.model_combo = CompactMenuSelector(self._model_changed)
        self.model_combo.set_size_request(180, -1)
        settings_row.pack_start(self.model_combo, False, False, 0)
        settings_row.pack_start(Gtk.Label(label="Reasoning"), False, False, 6)
        self.reasoning_combo = CompactMenuSelector(self._reasoning_changed)
        self.reasoning_combo.set_size_request(110, -1)
        settings_row.pack_start(self.reasoning_combo, False, False, 0)
        settings_row.pack_start(Gtk.Label(label="Fast"), False, False, 6)
        self.fast_combo = CompactMenuSelector(self._fast_changed)
        self.fast_combo.set_size_request(72, -1)
        self.fast_combo.append("off", "Off")
        self.fast_combo.append("on", "On")
        settings_row.pack_start(self.fast_combo, False, False, 0)
        content.pack_start(settings_row, False, False, 0)
        self._set_model_catalog(self.codex_models, persist=False)
        self._set_settings_sensitive(False)

        self.history = Gtk.TextView()
        self.history.set_editable(False)
        self.history.set_cursor_visible(False)
        self.history.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.history.set_left_margin(14)
        self.history.set_right_margin(14)
        self.history.set_top_margin(12)
        self.history.set_bottom_margin(12)
        self.history_buffer = self.history.get_buffer()
        self.user_tag = self.history_buffer.create_tag(
            "chat-user",
            foreground="#8ec8ff",
            weight=Pango.Weight.BOLD,
        )
        self.codex_tag = self.history_buffer.create_tag(
            "chat-codex",
            foreground="#ffc75c",
            weight=Pango.Weight.BOLD,
        )
        self.body_tag = self.history_buffer.create_tag(
            "chat-body",
            foreground="#f2f4f7",
        )
        self.error_tag = self.history_buffer.create_tag(
            "chat-error",
            foreground="#ff8f8f",
        )

        self.history_scroller = Gtk.ScrolledWindow()
        self.history_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.history_scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.history_scroller.add(self.history)
        self.history_scroller.get_vadjustment().connect(
            "value-changed",
            self._history_scrolled,
        )
        content.pack_start(self.history_scroller, True, True, 0)

        self.status = Gtk.Label(label="Codex 세션에 연결 중…")
        self.status.set_xalign(0)
        content.pack_start(self.status, False, False, 0)

        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_scroller = Gtk.ScrolledWindow()
        input_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        input_scroller.set_size_request(-1, 92)
        self.input = Gtk.TextView()
        self.input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input.set_left_margin(10)
        self.input.set_right_margin(10)
        self.input.set_top_margin(8)
        self.input.set_bottom_margin(8)
        self.input.connect("key-press-event", self._input_key_pressed)
        input_scroller.add(self.input)
        input_row.pack_start(input_scroller, True, True, 0)

        self.send_button = Gtk.Button(label="전송")
        self.send_button.set_sensitive(False)
        self.send_button.connect("clicked", self._send)
        input_row.pack_end(self.send_button, False, False, 0)
        content.pack_start(input_row, False, False, 0)

        self.connect("delete-event", self._delete)
        self.show_all()

    def run_session(self):
        self.active = True
        self.ready = False
        self._set_settings_sensitive(False)
        self._set_busy(False)
        self.status.set_text("Codex 세션에 연결 중…")
        snapshot = self.settings_snapshot()
        self.worker = CodexWorker(
            self._codex_ready,
            thread_id=self.thread_id,
            model=snapshot["codex_model"],
            effort=snapshot["codex_reasoning_effort"],
            load_model_catalog=True,
        )
        response = self.run()
        self.hide()
        self.active = False
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        return response

    def _codex_ready(self, result, error):
        if not self.active or self.worker is None:
            return False
        if error:
            self.status.set_text(f"Codex 연결 오류: {error}")
            self._append_status(f"Codex 연결 오류: {error}")
            return False
        self.ready = True
        if result.get("models"):
            self._set_model_catalog(result["models"], persist=True)
        self._set_settings_sensitive(True)
        self.history_turns = CodexAppServerClient.conversation_turns(
            result.get("thread", {})
        )
        self.history_start = max(
            0,
            len(self.history_turns) - CHAT_HISTORY_PAGE_TURNS,
        )
        self._render_history()
        self.status.set_text("준비 내용을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈")
        self._set_busy(False)
        self.input.grab_focus()
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
        self.model_combo.set_sensitive(sensitive)
        self.reasoning_combo.set_sensitive(sensitive)
        self.fast_combo.set_sensitive(sensitive)

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

    def _persist_settings(self):
        if self.session_store is not None:
            self.session_store.update_settings(
                self.thread_id,
                self.codex_settings,
            )
        if self.worker is not None:
            self.worker.set_model_and_effort(
                self.codex_settings["codex_model"],
                self.codex_settings["codex_reasoning_effort"],
            )

    def _render_history(self):
        self.history_buffer.set_text("")
        for turn in self.history_turns[self.history_start:]:
            for message in turn:
                self._append_message(message["role"], message["text"])
        GLib.idle_add(self._scroll_to_bottom)

    def _append_message(self, role, text):
        if not text:
            return
        end = self.history_buffer.get_end_iter()
        if self.history_buffer.get_char_count():
            self.history_buffer.insert(end, "\n")
            end = self.history_buffer.get_end_iter()
        if role == "user":
            self.history_buffer.insert_with_tags(end, "YOU\n", self.user_tag)
        else:
            self.history_buffer.insert_with_tags(end, "CODEX\n", self.codex_tag)
        end = self.history_buffer.get_end_iter()
        self.history_buffer.insert_with_tags(end, text, self.body_tag)

    def _append_status(self, text):
        end = self.history_buffer.get_end_iter()
        if self.history_buffer.get_char_count():
            self.history_buffer.insert(end, "\n\n")
            end = self.history_buffer.get_end_iter()
        self.history_buffer.insert_with_tags(end, text, self.error_tag)
        GLib.idle_add(self._scroll_to_bottom)

    def _start_codex_stream(self, delta):
        self.stream_started = True
        end = self.history_buffer.get_end_iter()
        self.history_buffer.insert(end, "\n\n")
        end = self.history_buffer.get_end_iter()
        self.history_buffer.insert_with_tags(end, "CODEX\n", self.codex_tag)
        end = self.history_buffer.get_end_iter()
        self.history_buffer.insert_with_tags(end, delta, self.body_tag)
        GLib.idle_add(self._scroll_to_bottom)

    def _append_codex_stream(self, delta):
        end = self.history_buffer.get_end_iter()
        self.history_buffer.insert_with_tags(end, delta, self.body_tag)
        GLib.idle_add(self._scroll_to_bottom)

    def _send(self, *_args):
        if not self.ready or self.busy or self.worker is None:
            return
        buffer = self.input.get_buffer()
        text = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            True,
        ).strip()
        if not text:
            return
        buffer.set_text("")
        self._append_message("user", text)
        self.stream_started = False
        self._set_busy(True)
        self.status.set_text("Codex가 답변을 작성 중입니다…")
        GLib.idle_add(self._scroll_to_bottom)

        def streamed(delta, _elapsed):
            if not self.active:
                return False
            if self.stream_started:
                self._append_codex_stream(delta)
            else:
                self._start_codex_stream(delta)
            return False

        def finished(result, error):
            if not self.active:
                return False
            turn_messages = [{"role": "user", "text": text}]
            if error:
                self._append_status(f"Codex 오류: {error}")
            else:
                turn_messages.append({
                    "role": "assistant",
                    "text": result["text"],
                })
                if not self.stream_started:
                    self._append_message("assistant", result["text"])
            self.history_turns.append(turn_messages)
            self.status.set_text(
                "준비 내용을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈"
            )
            self._set_busy(False)
            self.input.grab_focus()
            return False

        self.worker.submit(
            text,
            finished,
            streamed,
            interactive=True,
            on_approval=self._approve_tool,
        )

    def _approve_tool(self, method, params):
        is_command = method == "item/commandExecution/requestApproval"
        title = "명령 실행을 허용할까요?" if is_command else "파일 변경을 허용할까요?"
        detail = params.get("reason") or "Codex가 작업 승인을 요청했습니다."
        if is_command and params.get("command"):
            command = params["command"]
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            detail = f"{detail}\n\n{command}"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.add_button("거부", Gtk.ResponseType.CANCEL)
        dialog.add_button("허용", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return "accept" if response == Gtk.ResponseType.OK else "decline"

    def _set_busy(self, busy):
        self.busy = busy
        enabled = self.ready and not busy
        self.send_button.set_sensitive(enabled)
        self.start_button.set_sensitive(enabled)
        self.input.set_sensitive(enabled)

    def _input_key_pressed(self, _widget, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        self._send()
        return True

    def _history_scrolled(self, adjustment):
        if adjustment.get_value() > 1 or self.history_start == 0:
            return
        self.history_start = max(0, self.history_start - CHAT_HISTORY_PAGE_TURNS)
        self._render_history()

    def _scroll_to_bottom(self):
        adjustment = self.history_scroller.get_vadjustment()
        adjustment.set_value(
            max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
        )
        return False

    def _delete(self, *_args):
        self.response(Gtk.ResponseType.DELETE_EVENT)
        return True


class InterviewControlWindow(Gtk.Window):
    """One draggable control surface for interview navigation and exit."""

    def __init__(self, position, on_back, on_close, on_toggle_visibility):
        super().__init__(title="INTERVIEW CONTROLS")
        self.on_close = on_close
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
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
        back_button.set_tooltip_text("준비 채팅으로 돌아가기")
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
        self.set_default_size(width, height)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
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
        if not self.focus_mode:
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

        if not self.focus_mode:
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
                back_button.set_tooltip_text("준비 채팅으로 돌아가기")
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
                self.text.get_buffer().set_text(text)
                GLib.idle_add(self._reset_focus_scroll)
            else:
                self.text.set_text(text)

    def set_status(self, text):
        if self.focus_mode:
            self.text.get_buffer().set_text(text)
            GLib.idle_add(self._reset_focus_scroll)
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
        self.text.get_buffer().set_text(text)
        GLib.idle_add(self._reset_focus_scroll)

    def append_stream(self, text):
        if not text:
            return
        if not self.focus_mode:
            self.text.set_text(f"{self.text.get_text()}{text}")
            return
        buffer = self.text.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)

    def finish_stream(self, text):
        if not self.focus_mode:
            self.set_text(text)
            return
        buffer = self.text.get_buffer()
        current = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            True,
        )
        if current != text:
            buffer.set_text(text)

    def _reset_focus_scroll(self):
        self.focus_scroller.get_vadjustment().set_value(0)
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
    def __init__(self, codex_thread_id, codex_settings=None):
        self.session_dir, self.log_path = create_app_session()
        self.codex_thread_id = codex_thread_id
        live_codex_settings = normalize_codex_settings(codex_settings)
        self.codex_model = live_codex_settings["codex_model"]
        self.codex_reasoning_effort = live_codex_settings[
            "codex_reasoning_effort"
        ]
        self.codex_fast_mode = live_codex_settings["codex_fast_mode"]
        self.codex_enabled = CODEX_ENABLED
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
        self.socket_thread = None
        self.trigger_socket = None
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
            "ANSWER",
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
        self._start_trigger_listener()
        hotkey_status = self._install_global_f8()
        f9_hotkey_status = self._install_global_f9()

        remote_source = get_interviewer_audio_source()
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": APP_VERSION,
            "remote_source": remote_source,
            "microphone_capture": False,
            "asr_backend": "moonshine-small-streaming",
            "moonshine_update_interval_ms": 500,
            "moonshine_word_timestamps": False,
            "language": LANGUAGE,
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
        self.remote_audio = AudioStream(
            "INTERVIEWER",
            remote_source,
            self._moonshine_pcm,
            self._audio_error,
        )
        self.asr_worker = MoonshineStreamingWorker(
            self._moonshine_ready,
            self._moonshine_preview,
            self._moonshine_error,
            on_auto_commit=self._moonshine_auto_commit,
            dispatch=lambda callback, *args: GLib.idle_add(callback, *args),
            language=LANGUAGE,
        )
        self.codex_worker = None
        if self.codex_enabled:
            self.codex_worker = create_live_codex_worker(
                self._codex_ready,
                self.codex_thread_id,
                live_codex_settings,
            )
        self.remote_window.set_status("Moonshine Small loading…")
        self.asr_worker.start()

    def _install_css(self):
        css = b"""
        window { background-color: rgba(18, 20, 24, 0.94); border-radius: 14px; }
        window.interviewer { border: 2px solid rgba(95, 176, 255, 0.85); }
        window.answer { border: 2px solid rgba(255, 195, 92, 0.82); }
        window.control { border: 2px solid rgba(255, 195, 92, 0.82); }
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

    def _load_window_state(self):
        try:
            return json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_window_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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

    def _install_global_f8(self):
        return self._install_global_hotkey(
            key="F8",
            path=HOTKEY_PATH,
            name="Interview Assistant: Capture Question",
            trigger_argument="--trigger",
        )

    def _install_global_f9(self):
        return self._install_global_hotkey(
            key="F9",
            path=HOTKEY_F9_PATH,
            name="Interview Assistant: Continue Previous Question",
            trigger_argument="--trigger-f9",
        )

    def _install_global_hotkey(self, key, path, name, trigger_argument):
        try:
            media_keys = Gio.Settings.new(
                "org.gnome.settings-daemon.plugins.media-keys"
            )
            paths = list(media_keys.get_strv("custom-keybindings"))
            for existing_path in paths:
                setting = Gio.Settings.new_with_path(
                    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                    existing_path,
                )
                if (
                    setting.get_string("binding") == key
                    and existing_path != path
                ):
                    return "conflict"

            setting = Gio.Settings.new_with_path(
                "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                path,
            )
            command = (
                f"{shlex.quote(sys.executable)} "
                f"{shlex.quote(str(Path(__file__).resolve()))} {trigger_argument}"
            )
            setting.set_string("name", name)
            setting.set_string("command", command)
            setting.set_string("binding", key)
            if path not in paths:
                paths.append(path)
                media_keys.set_strv("custom-keybindings", paths)
            Gio.Settings.sync()
            return "installed"
        except Exception as error:
            append_log(self.log_path, {
                "event": "hotkey_error",
                "key": key,
                "error": str(error),
            })
            return f"error: {error}"

    def _start_trigger_listener(self):
        try:
            TRIGGER_SOCKET.unlink(missing_ok=True)
            self.trigger_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.trigger_socket.bind(str(TRIGGER_SOCKET))
            os.chmod(TRIGGER_SOCKET, 0o600)
            self.trigger_socket.settimeout(0.5)
        except OSError as error:
            raise RuntimeError(f"Cannot create F8 trigger socket: {error}") from error

        def listen():
            while self.running:
                try:
                    data = self.trigger_socket.recv(32)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if data == b"F8":
                    GLib.idle_add(self._on_f8)
                elif data == b"F9":
                    GLib.idle_add(self._on_f9)
                elif data == b"STOP":
                    GLib.idle_add(self.shutdown)

        self.socket_thread = threading.Thread(target=listen, daemon=True)
        self.socket_thread.start()

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
            self.remote_audio.start()
            self.audio_started = True
        return False

    def _moonshine_pcm(self, pcm_audio, start_cursor, end_cursor):
        try:
            accepted = self.asr_worker.submit_pcm(
                pcm_audio,
                start_cursor,
                end_cursor,
            )
            if not accepted:
                raise RuntimeError("Moonshine worker is not accepting PCM")
        except Exception as error:
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
        self.remote_window.set_status(f"Moonshine error: {error}")
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
        if error:
            self.answer_window.set_status(f"Moonshine error: {error}")
            append_log(self.log_path, {
                "event": "question_error",
                "question": question_number,
                "commit_source": commit_source,
                "error": str(error),
            })
            return False

        if not result.get("committed", True):
            self.answer_window.set_status("Waiting for question…")
            append_log(self.log_path, {
                "event": "question_duplicate_suppressed",
                "question": question_number,
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
        append_log(self.log_path, {
            "event": "question",
            "question": question_number,
            "commit_source": commit_source,
            "text": question_text,
            "stt_seconds": round(elapsed, 3),
            "asr_backend": "moonshine-small-streaming",
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
        if not question_text:
            self.answer_window.set_status("No question detected")
            return False

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
        self.question_count += 1
        question_number = self.question_count
        commit_started = (
            result.get("commit_requested_at", time.perf_counter())
            if result is not None
            else time.perf_counter()
        )
        return self._moonshine_question_ready(
            question_number,
            commit_started,
            result,
            error,
            commit_source="silence",
        )

    def _continuation_base_is_valid(self, base):
        if not base or not base.get("text"):
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
            "asr_backend": "moonshine-small-streaming",
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
            self.answer_window.set_text("")
        append_log(self.log_path, {
            "event": "codex_request",
            "request": request_number,
            "question": question_number,
            "generation": generation,
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

    def _on_f8(self):
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
            self.remote_window.set_status("Moonshine is still loading…")
            return False
        self.question_count += 1
        question_number = self.question_count
        callback = lambda result, error: self._moonshine_question_ready(
            question_number,
            now,
            result,
            error,
            commit_source="f8",
        )
        try:
            target_cursor, accepted = self.remote_audio.capture_sample_cursor_and(
                lambda cursor: self.asr_worker.request_snapshot(cursor, callback)
            )
        except Exception as error:
            self._moonshine_question_ready(
                question_number,
                now,
                None,
                error,
            )
            return False
        if not accepted:
            self._moonshine_question_ready(
                question_number,
                now,
                None,
                RuntimeError("Moonshine worker rejected F8 snapshot"),
            )
            return False
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": question_number,
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": "moonshine-small-streaming",
        })
        self.answer_window.set_status("Transcribing question…")
        return False

    def _on_f9(self):
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
            self.remote_window.set_status("Moonshine is still loading…")
            return False
        base = getattr(self, "last_commit_state", None)
        if not self._continuation_base_is_valid(base):
            append_log(self.log_path, {
                "event": "f9_ignored",
                "reason": "no_valid_previous_question",
            })
            self.answer_window.set_status("No previous question to continue")
            return False
        base = dict(base)
        callback = lambda result, error: self._moonshine_continuation_ready(
            base,
            now,
            result,
            error,
        )
        try:
            target_cursor, accepted = self.remote_audio.capture_sample_cursor_and(
                lambda cursor: self.asr_worker.request_snapshot(cursor, callback)
            )
        except Exception as error:
            self._moonshine_continuation_ready(base, now, None, error)
            return False
        if not accepted:
            self._moonshine_continuation_ready(
                base,
                now,
                None,
                RuntimeError("Moonshine worker rejected F9 snapshot"),
            )
            return False
        append_log(self.log_path, {
            "event": "f9_trigger",
            "commit_source": "f9_continuation",
            "previous_question": base["question_number"],
            "previous_target_sample_cursor": base["target_sample_cursor"],
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": "moonshine-small-streaming",
        })
        self.answer_window.set_status("Transcribing continuation…")
        return False

    def _audio_error(self, role, error):
        append_log(self.log_path, {
            "event": "audio_error", "role": role, "error": str(error),
        })
        GLib.idle_add(self._window(role).set_status, f"Audio error: {error}")

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
        self._save_window_state()
        self.remote_audio.stop()
        self.asr_worker.stop()
        if self.codex_worker is not None:
            self.codex_worker.stop()
        if self.trigger_socket is not None:
            self.trigger_socket.close()
        TRIGGER_SOCKET.unlink(missing_ok=True)
        append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": exit_action,
            "questions": self.question_count,
            "codex_requests": self.codex_request_count,
        })
        for window in (
            self.remote_window,
            self.answer_window,
            self.control_window,
        ):
            window.hide()
        Gtk.main_quit()
        return False


def main():
    if not CODEX_ENABLED:
        app = InterviewApp(None)
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT,
            signal.SIGINT,
            app.shutdown,
        )
        Gtk.main()
        return

    store = SessionStore(SESSION_STORE_PATH)
    while True:
        session = choose_interview_session(store)
        if session is None:
            return
        thread_id = session["thread_id"]
        chat = PreparationChatDialog(
            thread_id,
            session_store=store,
            session_settings=session.get("settings"),
        )
        while True:
            response = chat.run_session()
            if response == CHAT_RESPONSE_BACK:
                chat.destroy()
                break
            if response != CHAT_RESPONSE_START_INTERVIEW:
                chat.destroy()
                return

            live_codex_settings = chat.settings_snapshot()
            app = InterviewApp(
                thread_id,
                codex_settings=live_codex_settings,
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
            chat.destroy()
            return


if __name__ == "__main__":
    main()
