#!/usr/bin/env python3
"""Local interview transcription app with F8-triggered Codex answers."""

import fcntl
import os
import socket
import sys
from collections import deque
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
from context_manager import CONTEXT_STATUS_SYNCED, ContextManager
from interview_thread_backend import InterviewThreadBackend
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
When a turn starts with PREPARATION MESSAGE:, treat it as a direct preparation question from the candidate. Answer helpfully, and use the exchange to establish preferences and background for later live answers.
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
SAMPLE_WIDTH = 2
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
PREPARATION_MESSAGE_MARKER = "PREPARATION MESSAGE:"
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


def create_app_session(test_logging=None, benchmark_type=None):
    if test_logging is None:
        test_logging = TEST_LOGGING
    if not test_logging:
        return None, None
    session_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    if benchmark_type is None:
        benchmark_type = os.environ.get(
            "INTERVIEW_BENCHMARK_TYPE", ""
        ).strip()
    if benchmark_type and not all(
        character.isascii() and (
            character.isalnum() or character in {"_", "-"}
        )
        for character in benchmark_type
    ):
        raise ValueError(
            "INTERVIEW_BENCHMARK_TYPE must use letters, digits, '_' or '-'"
        )
    name_parts = ["app_session"]
    if benchmark_type:
        name_parts.append(benchmark_type)
    name_parts.extend([session_id, str(os.getpid())])
    session_dir = APP_DIR / "test_runs" / "_".join(name_parts)
    session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    session_dir.chmod(0o700)
    log_path = session_dir / "session.jsonl"
    log_path.touch(mode=0o600)
    log_path.chmod(0o600)
    return session_dir, log_path


class AudioStream:
    """Capture raw PCM and forward it with an absolute sample cursor."""

    def __init__(self, role, source, on_pcm, on_error):
        self.role = role
        self.source = source
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.process = None
        self.thread = None
        self.stderr_thread = None
        self.stderr_tail = deque(maxlen=20)
        self.stopped = threading.Event()
        self.condition = threading.Condition()
        self.total_samples = 0

    def start(self):
        self.process = start_audio_capture(self.source)
        self.stderr_thread = threading.Thread(
            target=self._read_stderr,
            daemon=True,
        )
        self.stderr_thread.start()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def abort(self):
        """Stop capture without joining the current PCM reader thread."""
        self.stopped.set()
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
        with self.condition:
            self.condition.notify_all()

    def stop(self):
        self.abort()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=2)

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
                    if not self.stopped.is_set():
                        if self.stderr_thread is not None:
                            self.stderr_thread.join(timeout=0.2)
                        return_code = self.process.poll()
                        detail = "\n".join(self.stderr_tail).strip()
                        suffix = f": {detail}" if detail else ""
                        raise RuntimeError(
                            "Audio capture stopped unexpectedly "
                            f"(exit code {return_code}){suffix}"
                        )
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

    def _read_stderr(self):
        stream = None if self.process is None else self.process.stderr
        if stream is None:
            return
        for line in stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            self.stderr_tail.append(line.rstrip())


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
SESSION_RESPONSE_ARCHIVE_ALL = 5


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
    }


def initial_session_settings(environment=None):
    """Read optional benchmark-only settings for a newly created session.

    Normal sessions keep the existing defaults.  Benchmark automation supplies
    this value so each new session is configured before Context Sync creates
    its fresh Codex thread.
    """
    environment = os.environ if environment is None else environment
    raw = environment.get("INTERVIEW_BENCHMARK_INITIAL_SETTINGS")
    if not raw:
        return normalize_codex_settings()
    try:
        settings = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "INTERVIEW_BENCHMARK_INITIAL_SETTINGS must be JSON object"
        ) from error
    if not isinstance(settings, dict):
        raise ValueError(
            "INTERVIEW_BENCHMARK_INITIAL_SETTINGS must be JSON object"
        )
    return normalize_codex_settings(settings)


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
                if INTERVIEW_QUESTION_MARKER in text:
                    question = text.rsplit(
                        INTERVIEW_QUESTION_MARKER,
                        1,
                    )[1].strip()
                    if question:
                        messages.append({
                            "role": "interviewer",
                            "text": question,
                        })
                elif text.startswith(PREPARATION_MESSAGE_MARKER):
                    question = text[len(PREPARATION_MESSAGE_MARKER):].strip()
                    if question:
                        messages.append({
                            "role": "candidate",
                            "text": question,
                        })
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
            "선택 삭제",
            SESSION_RESPONSE_ARCHIVE,
        )
        self.archive_button.set_sensitive(False)
        self.archive_button.get_style_context().add_class(
            "destructive-action"
        )
        self.archive_all_button = self.add_button(
            "전체 삭제",
            SESSION_RESPONSE_ARCHIVE_ALL,
        )
        self.archive_all_button.set_sensitive(bool(sessions))
        self.archive_all_button.get_style_context().add_class(
            "destructive-action"
        )
        self.get_action_area().set_child_secondary(
            self.archive_button,
            True,
        )
        self.get_action_area().set_child_secondary(
            self.archive_all_button,
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
                "세션 하나를 선택해 열거나 이름을 변경할 수 있습니다.\n"
                "Ctrl/Shift로 여러 세션을 선택해 삭제할 수 있습니다."
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
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
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
        sessions = self.selected_sessions()
        return sessions[0] if len(sessions) == 1 else None

    def selected_sessions(self):
        model, paths = self.tree.get_selection().get_selected_rows()
        return [
            self.sessions_by_session_id[model[path][3]]
            for path in paths
        ]

    def all_sessions(self):
        return list(self.sessions_by_session_id.values())

    def _row_activated(self, _tree, path, *_args):
        selection = self.tree.get_selection()
        selection.unselect_all()
        selection.select_path(path)
        if self.selected_session() is None:
            return None
        self.response(Gtk.ResponseType.OK)

    def _key_pressed(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.selected_session() is not None:
                self.response(Gtk.ResponseType.OK)
                return True
        return False

    def _selection_changed(self, selection):
        _model, paths = selection.get_selected_rows()
        selection_count = len(paths)
        self.open_button.set_sensitive(selection_count == 1)
        self.rename_button.set_sensitive(selection_count == 1)
        self.archive_button.set_sensitive(selection_count > 0)
        self.archive_all_button.set_sensitive(len(self.model) > 0)

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


def _confirm_archive(sessions):
    sessions = list(sessions)
    if len(sessions) == 1:
        text = "선택한 세션을 삭제할까요?"
        detail = (
            f"{sessions[0]['name']}\n\n"
            "이 세션은 활성 목록에서 제거됩니다."
        )
    else:
        text = f"{len(sessions)}개의 세션을 삭제할까요?"
        names = "\n".join(
            session.get("name") or "Unnamed Session"
            for session in sessions[:5]
        )
        remaining = len(sessions) - 5
        if remaining > 0:
            names += f"\n외 {remaining}개"
        detail = f"{names}\n\n선택한 세션은 활성 목록에서 제거됩니다."
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=text,
    )
    dialog.format_secondary_text(detail)
    dialog.add_button("취소", Gtk.ResponseType.CANCEL)
    delete_button = dialog.add_button("삭제", Gtk.ResponseType.OK)
    delete_button.get_style_context().add_class("destructive-action")
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def _archive_session(store, session, codex_enabled):
    interview_thread_id = session.get("interview_thread_id")
    try:
        if codex_enabled and interview_thread_id:
            archive_persisted_codex_session(interview_thread_id)
    except CodexAppServerError as error:
        if "no rollout found" not in str(error).lower():
            raise
    store.mark_archived(session["session_id"])


def _archive_sessions(store, sessions, codex_enabled):
    failures = []
    for session in sessions:
        try:
            _archive_session(store, session, codex_enabled)
        except Exception as error:
            failures.append((session, error))
    return failures


def choose_interview_session(store, context_manager, codex_enabled=True):
    preferred_session_id = None
    while True:
        dialog = SessionChooserDialog(
            store.active(),
            preferred_session_id=preferred_session_id,
        )
        response = dialog.run()
        selected = dialog.selected_session()
        selected_sessions = dialog.selected_sessions()
        all_sessions = dialog.all_sessions()
        dialog.destroy()

        if response == SESSION_RESPONSE_BACK:
            return SESSION_RESPONSE_BACK

        if response == SESSION_RESPONSE_NEW:
            try:
                created = datetime.now().astimezone()
                session = store.create(
                    created.strftime("%Y-%m-%d %H:%M"),
                    created.isoformat(timespec="seconds"),
                    initial_session_settings(),
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

        sessions_to_archive = (
            selected_sessions
            if response == SESSION_RESPONSE_ARCHIVE else all_sessions
        )
        if (
            response in {
                SESSION_RESPONSE_ARCHIVE,
                SESSION_RESPONSE_ARCHIVE_ALL,
            }
            and sessions_to_archive
        ):
            if _confirm_archive(sessions_to_archive):
                failures = _archive_sessions(
                    store,
                    sessions_to_archive,
                    codex_enabled,
                )
                preferred_session_id = None
                if failures:
                    failure_lines = "\n".join(
                        f"{session.get('name')}: {error}"
                        for session, error in failures
                    )
                    _show_session_error(
                        RuntimeError(
                            "일부 세션을 삭제하지 못했습니다:\n"
                            f"{failure_lines}"
                        )
                    )
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
        self.preparation_worker = None
        self.preparation_ready = False
        self.preparation_busy = False
        self.preparation_stream_started = False
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
        self.conversation_candidate_tag = self.conversation_buffer.create_tag(
            "conversation-candidate",
            foreground="#9dccff",
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
        self.preparation_chat_status = Gtk.Label()
        self.preparation_chat_status.set_xalign(0)
        self.preparation_chat_status.set_line_wrap(True)
        self.preparation_chat_status.get_style_context().add_class(
            "section-description"
        )
        conversation_box.pack_start(
            self.preparation_chat_status,
            False,
            False,
            0,
        )
        preparation_input_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        preparation_input_scroller = Gtk.ScrolledWindow()
        preparation_input_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        preparation_input_scroller.set_size_request(-1, 76)
        self.preparation_input = Gtk.TextView()
        self.preparation_input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preparation_input.set_left_margin(10)
        self.preparation_input.set_right_margin(10)
        self.preparation_input.set_top_margin(8)
        self.preparation_input.set_bottom_margin(8)
        self.preparation_input.set_sensitive(False)
        self.preparation_input.set_tooltip_text(
            "Context를 Sync한 뒤 준비 질문을 입력할 수 있습니다."
        )
        self.preparation_input.connect(
            "key-press-event",
            self._preparation_input_key_pressed,
        )
        preparation_input_scroller.add(self.preparation_input)
        preparation_input_row.pack_start(
            preparation_input_scroller,
            True,
            True,
            0,
        )
        self.preparation_send_button = Gtk.Button(label="질문 보내기")
        self.preparation_send_button.set_sensitive(False)
        self.preparation_send_button.connect(
            "clicked",
            self._send_preparation_message,
        )
        preparation_input_row.pack_end(
            self.preparation_send_button,
            False,
            False,
            0,
        )
        conversation_box.pack_start(preparation_input_row, False, False, 0)
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
        self._update_preparation_chat()

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
        self._stop_preparation_worker()
        self.context_sync_in_progress = True
        self.context_sync_generation += 1
        generation = self.context_sync_generation
        self.sync_context_button.set_sensitive(False)
        self._update_context_summary()
        self._update_start_button()
        self._update_preparation_chat()
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
            self._update_preparation_chat()
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
        elif role == "candidate":
            label = "YOU\n"
            tag = self.conversation_candidate_tag
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

    def _preparation_chat_thread_id(self):
        if not (
            self.active
            and getattr(self, "codex_enabled", True)
            and not self.context_sync_in_progress
            and can_start_interview(self.session, self.context_rows)
        ):
            return None
        return self.session.get("interview_thread_id")

    def _stop_preparation_worker(self):
        worker = getattr(self, "preparation_worker", None)
        self.preparation_worker = None
        self.preparation_ready = False
        self.preparation_busy = False
        self.preparation_stream_started = False
        if worker is not None:
            worker.stop()

    def _update_preparation_chat(self):
        if not hasattr(self, "preparation_input"):
            return
        thread_id = self._preparation_chat_thread_id()
        worker = self.preparation_worker
        if not thread_id:
            if worker is not None:
                self._stop_preparation_worker()
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            if not getattr(self, "codex_enabled", True):
                status = "Codex is disabled in this mode."
            elif self.context_sync_in_progress:
                status = "Context를 Sync하는 중입니다…"
            else:
                status = "Context를 Sync한 뒤 준비 질문을 입력할 수 있습니다."
            self.preparation_chat_status.set_text(status)
            return
        if worker is not None and worker.thread_id != thread_id:
            self._stop_preparation_worker()
            worker = None
        if worker is None:
            self.preparation_ready = False
            self.preparation_busy = False
            self.preparation_stream_started = False
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            self.preparation_chat_status.set_text(
                "준비 대화에 연결하는 중입니다…"
            )
            settings = self.settings_snapshot()
            self.preparation_worker = CodexWorker(
                self._preparation_chat_ready,
                thread_id=thread_id,
                model=settings["codex_model"],
                effort=settings["codex_reasoning_effort"],
                fast_mode=settings["codex_fast_mode"],
            )
            return
        self._set_preparation_chat_busy(self.preparation_busy)

    def _preparation_chat_ready(self, result, error):
        if not self.active or self.preparation_worker is None:
            return False
        if error is not None:
            self.preparation_ready = False
            self.preparation_input.set_sensitive(False)
            self.preparation_send_button.set_sensitive(False)
            self.preparation_chat_status.set_text(
                f"준비 대화에 연결할 수 없습니다: {error}"
            )
            return False
        expected_thread_id = self._preparation_chat_thread_id()
        if not expected_thread_id or result.get("thread_id") != expected_thread_id:
            self._stop_preparation_worker()
            self._update_preparation_chat()
            return False
        self.preparation_ready = True
        self.preparation_chat_status.set_text(
            "준비 질문을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈"
        )
        self._set_preparation_chat_busy(False)
        self.preparation_input.grab_focus()
        return False

    def _set_preparation_chat_busy(self, busy):
        self.preparation_busy = busy
        enabled = self.preparation_ready and not busy
        self.preparation_input.set_sensitive(enabled)
        self.preparation_send_button.set_sensitive(enabled)

    def _send_preparation_message(self, *_args):
        if (
            not self.preparation_ready
            or self.preparation_busy
            or self.preparation_worker is None
        ):
            return
        buffer = self.preparation_input.get_buffer()
        text = buffer.get_text(
            buffer.get_start_iter(),
            buffer.get_end_iter(),
            True,
        ).strip()
        if not text:
            return
        buffer.set_text("")
        self._append_conversation_message("candidate", text)
        self.preparation_stream_started = False
        self._set_preparation_chat_busy(True)
        self.preparation_chat_status.set_text("Codex가 답변을 작성 중입니다…")

        def streamed(delta, _elapsed):
            if not self.active or not self.preparation_busy:
                return False
            if self.preparation_stream_started:
                end = self.conversation_buffer.get_end_iter()
                self.conversation_buffer.insert_with_tags(
                    end,
                    delta,
                    self.conversation_body_tag,
                )
            else:
                self.preparation_stream_started = True
                self._append_conversation_message("codex", delta)
            return False

        def finished(result, error):
            if not self.active:
                return False
            if error is not None:
                self.preparation_chat_status.set_text(
                    f"준비 질문을 전송할 수 없습니다: {error}"
                )
            elif not self.preparation_stream_started:
                self._append_conversation_message("codex", result["text"])
            self._set_preparation_chat_busy(False)
            if error is None:
                self.preparation_chat_status.set_text(
                    "준비 질문을 입력하세요. Enter 전송 · Shift+Enter 줄바꿈"
                )
                self._refresh_conversation()
            self.preparation_input.grab_focus()
            return False

        self.preparation_worker.submit(
            f"{PREPARATION_MESSAGE_MARKER}\n{text}",
            finished,
            streamed,
            interactive=True,
            on_approval=self._approve_preparation_tool,
        )

    def _preparation_input_key_pressed(self, _widget, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            return False
        self._send_preparation_message()
        return True

    def _approve_preparation_tool(self, method, params):
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
        self._update_preparation_chat()
        if getattr(self, "codex_enabled", True):
            self._load_model_catalog()
        self._refresh_conversation()
        response = self.run()
        self.active = False
        self.context_sync_generation += 1
        self.model_catalog_load_generation += 1
        self.conversation_load_generation += 1
        self._stop_preparation_worker()
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
        worker = getattr(self, "preparation_worker", None)
        if worker is not None:
            worker.set_model_and_effort(
                self.codex_settings["codex_model"],
                self.codex_settings["codex_reasoning_effort"],
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
        self._pending_answer_ui_diagnostics = None
        self._answer_ui_diagnostics_by_mark = {}
        self.set_default_size(width, height)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()
        # Do not impose an application-level minimum size.  The window
        # manager and GTK's content requirements remain the only limits, so
        # the resize handles can adjust the live windows freely.
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

    def prepare_corrected_answer_alignment(self):
        """Keep F9's pending corrected answer at its future stream position."""
        if not self.focus_mode:
            return
        buffer = self.text.get_buffer()
        if self.answer_history:
            buffer.insert(buffer.get_end_iter(), "\n\n")
        self._set_latest_answer_mark(buffer.get_end_iter())
        # Align now, then once more after GTK has processed the buffer resize.
        self._align_latest_answer_once(self.latest_answer_mark)
        GLib.idle_add(
            self._align_latest_answer_once,
            self.latest_answer_mark,
        )

    def configure_answer_ui_diagnostics(self, context, logger):
        """Attach one diagnostic record to the next streamed answer only."""
        self._pending_answer_ui_diagnostics = (dict(context), logger)

    def answer_ui_diagnostic_snapshot(self, mark=None):
        """Return UI state for logs without mutating the displayed answer."""
        snapshot = {
            "history_count": len(self.answer_history),
            "latest_answer_mark_offset": None,
            "latest_answer_y": None,
            "scroll_value": None,
            "vadjustment_lower": None,
            "vadjustment_upper": None,
            "vadjustment_page_size": None,
            "maximum_scroll": None,
            "mark_is_current": None,
        }
        if not self.focus_mode:
            return snapshot
        selected_mark = self.latest_answer_mark if mark is None else mark
        snapshot["mark_is_current"] = (
            selected_mark is not None
            and selected_mark is self.latest_answer_mark
        )
        if selected_mark is not None:
            try:
                answer_start = self.text.get_buffer().get_iter_at_mark(
                    selected_mark
                )
                snapshot["latest_answer_mark_offset"] = answer_start.get_offset()
                snapshot["latest_answer_y"] = self.text.get_iter_location(
                    answer_start
                ).y
            except (TypeError, ValueError):
                pass
        adjustment = self.focus_scroller.get_vadjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        page_size = adjustment.get_page_size()
        snapshot.update({
            "scroll_value": adjustment.get_value(),
            "vadjustment_lower": lower,
            "vadjustment_upper": upper,
            "vadjustment_page_size": page_size,
            "maximum_scroll": max(lower, upper - page_size),
        })
        return snapshot

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
        diagnostic = getattr(self, "_pending_answer_ui_diagnostics", None)
        self._pending_answer_ui_diagnostics = None
        if diagnostic is not None:
            diagnostics_by_mark = getattr(
                self,
                "_answer_ui_diagnostics_by_mark",
                None,
            )
            if diagnostics_by_mark is None:
                diagnostics_by_mark = {}
                self._answer_ui_diagnostics_by_mark = diagnostics_by_mark
            diagnostics_by_mark[id(self.latest_answer_mark)] = (
                diagnostic
            )
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
        diagnostic = getattr(
            self,
            "_answer_ui_diagnostics_by_mark",
            {},
        ).pop(id(mark), None)
        snapshot = getattr(self, "answer_ui_diagnostic_snapshot", None)
        before = snapshot(mark) if snapshot is not None else None
        if mark is self.latest_answer_mark:
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
        after = snapshot(mark) if snapshot is not None else None
        if diagnostic is not None:
            context, logger = diagnostic
            logger("answer_scroll_align", context, before, after)
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
        self.socket_thread = None
        self.trigger_socket = None
        self.trigger_lock_file = None
        self.audio_failure_reported = False
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
            language=self.stt_language,
        )
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
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = TRIGGER_LOCK_PATH.open("a+", encoding="utf-8")
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError(
                "Interview Assistant is already running"
            ) from error

        self.trigger_lock_file = lock_file
        try:
            TRIGGER_SOCKET.unlink(missing_ok=True)
            self.trigger_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.trigger_socket.bind(str(TRIGGER_SOCKET))
            os.chmod(TRIGGER_SOCKET, 0o600)
            self.trigger_socket.settimeout(0.5)
        except OSError as error:
            self._close_trigger_listener()
            raise RuntimeError(f"Cannot create F8 trigger socket: {error}") from error

        def listen():
            while self.running:
                try:
                    data = self.trigger_socket.recv(512)
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
                elif data.startswith(b"BENCHMARK_WAV_START:"):
                    raw_payload = data.partition(b":")[2].decode(
                        "utf-8", errors="replace"
                    ).strip()
                    try:
                        payload = json.loads(raw_payload)
                    except ValueError:
                        payload = {"wav": raw_payload}
                    if not isinstance(payload, dict):
                        payload = {}
                    wav_name = str(payload.get("wav") or "").strip()
                    if wav_name:
                        GLib.idle_add(
                            self._benchmark_wav_start,
                            wav_name,
                            payload.get("playback_command_started_at_unix_ns"),
                        )

        self.socket_thread = threading.Thread(target=listen, daemon=True)
        self.socket_thread.start()

    def _benchmark_wav_start(self, wav_name, started_at_unix_ns=None):
        """Keep benchmark playback timing in the existing JSONL session log."""
        if self.running:
            event = {
                "event": "benchmark_wav_start",
                "wav": wav_name,
                "next_question": self.question_count + 1,
            }
            if isinstance(started_at_unix_ns, int):
                event["playback_command_started_at_unix_ns"] = (
                    started_at_unix_ns
                )
            append_log(self.log_path, event)
        return False

    def _close_trigger_listener(self):
        socket_error = None
        try:
            if self.trigger_socket is not None:
                self.trigger_socket.close()
        except OSError as error:
            socket_error = error
        finally:
            self.trigger_socket = None
            if self.trigger_lock_file is not None:
                try:
                    TRIGGER_SOCKET.unlink(missing_ok=True)
                finally:
                    fcntl.flock(
                        self.trigger_lock_file.fileno(),
                        fcntl.LOCK_UN,
                    )
                    self.trigger_lock_file.close()
                    self.trigger_lock_file = None
        if socket_error is not None:
            raise socket_error

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
            self._answer_ui_trigger = commit_source
            try:
                codex_generation = self._request_codex_answer(
                    question_number,
                    question_text,
                )
            finally:
                self._answer_ui_trigger = None
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
            self._answer_ui_trigger = "f9"
            try:
                codex_generation = self._request_codex_answer(
                    question_number,
                    combined_text,
                    supersedes_generation=base["codex_generation"],
                    correction={"previous_text": base["text"]},
                )
            finally:
                self._answer_ui_trigger = None
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

    def _answer_ui_snapshot(self):
        fields = {
            "history_count": None,
            "latest_answer_mark_offset": None,
            "latest_answer_y": None,
            "scroll_value": None,
            "vadjustment_lower": None,
            "vadjustment_upper": None,
            "vadjustment_page_size": None,
            "maximum_scroll": None,
            "mark_is_current": None,
        }
        snapshot = getattr(
            self.answer_window,
            "answer_ui_diagnostic_snapshot",
            None,
        )
        if snapshot is not None:
            fields.update(snapshot())
        return fields

    def _log_answer_ui(self, event, context, before=None, after=None):
        """Write diagnostics only; never change the Answer window state."""
        before = self._answer_ui_snapshot() if before is None else before
        after = self._answer_ui_snapshot() if after is None else after
        payload = {
            "event": event,
            **context,
            **after,
            "scroll_value_before": before["scroll_value"],
            "scroll_value_after": after["scroll_value"],
            "vadjustment_lower_before": before["vadjustment_lower"],
            "vadjustment_upper_before": before["vadjustment_upper"],
            "vadjustment_page_size_before": (
                before["vadjustment_page_size"]
            ),
            "maximum_scroll_before": before["maximum_scroll"],
        }
        append_log(self.log_path, payload)

    def _request_codex_answer(
        self,
        question_number,
        question_text,
        supersedes_generation=None,
        correction=None,
        trigger=None,
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
        ui_trigger = trigger or getattr(self, "_answer_ui_trigger", None) or "unknown"
        ui_context = {
            "generation": generation,
            "question": question_number,
            "trigger": ui_trigger,
            "f8_or_f9": (
                ui_trigger if ui_trigger in {"f8", "f9"} else None
            ),
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
                discard_before = self._answer_ui_snapshot()
                discard(remove_completed=remove_completed)
                if correction is not None:
                    prepare_alignment = getattr(
                        self.answer_window,
                        "prepare_corrected_answer_alignment",
                        None,
                    )
                    if prepare_alignment is not None:
                        prepare_alignment()
                    self._log_answer_ui(
                        "answer_discard",
                        ui_context,
                        discard_before,
                        self._answer_ui_snapshot(),
                    )
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
                stream_before = self._answer_ui_snapshot()
                configure_diagnostics = getattr(
                    self.answer_window,
                    "configure_answer_ui_diagnostics",
                    None,
                )
                if configure_diagnostics is not None:
                    configure_diagnostics(ui_context, self._log_answer_ui)
                self.answer_window.start_stream(delta)
                self._log_answer_ui(
                    "answer_stream_start",
                    ui_context,
                    stream_before,
                    self._answer_ui_snapshot(),
                )
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
        callback = lambda result, error: self._moonshine_question_ready(
            None,
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
                None,
                now,
                None,
                error,
            )
            return False
        if not accepted:
            self._moonshine_question_ready(
                None,
                now,
                None,
                RuntimeError("Moonshine worker rejected F8 snapshot"),
            )
            return False
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": self.question_count + 1,
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
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
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
        })
        self.answer_window.set_status("Transcribing continuation…")
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

        run_cleanup("window_state", self._save_window_state)
        run_cleanup("audio", self.remote_audio.stop)
        run_cleanup("moonshine", self.asr_worker.stop)
        if self.codex_worker is not None:
            run_cleanup("codex", self.codex_worker.stop)
        run_cleanup("trigger_socket", self._close_trigger_listener)
        run_cleanup("session_log", lambda: append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": exit_action,
            "questions": self.question_count,
            "codex_requests": self.codex_request_count,
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


class _HeadlessTranscriptWindow:
    """No-op live window preserving InterviewApp's streaming callbacks."""

    def set_text(self, _text):
        pass

    def set_status(self, _text):
        pass

    def set_boundary_status(self, _text):
        pass

    def set_response_status(self, _text):
        pass

    def discard_current_answer(self, **_kwargs):
        pass

    def start_stream(self, _text):
        pass

    def append_stream(self, _text):
        pass

    def finish_stream(self, _text):
        pass

    def hide(self):
        pass


class HeadlessInterviewApp(InterviewApp):
    """Run the existing live audio/F8/Codex flow without GTK windows.

    Benchmark helpers call ``trigger_f8`` directly after WAV playback.  The
    remaining question commit and Codex streaming path is inherited from
    ``InterviewApp`` unchanged.
    """

    def __init__(self, codex_thread_id, codex_settings, test_label):
        self.session_dir, self.log_path = create_app_session(
            test_logging=True,
            benchmark_type="benchmark_a",
        )
        self.runtime = {
            "mode": "performance",
            "codex_enabled": True,
            "logging_enabled": True,
            "diagnostics_enabled": False,
        }
        self.test_label = test_label
        self.codex_thread_id = codex_thread_id
        live_codex_settings = normalize_codex_settings(codex_settings)
        self.codex_model = live_codex_settings["codex_model"]
        self.codex_reasoning_effort = live_codex_settings[
            "codex_reasoning_effort"
        ]
        self.codex_fast_mode = live_codex_settings["codex_fast_mode"]
        self.stt_language = live_codex_settings["stt_language"]
        self.codex_enabled = True
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
        self.live_windows_hidden = True
        self.moonshine_ready = False
        self.audio_started = False
        self.audio_failure_reported = False
        self.remote_window = _HeadlessTranscriptWindow()
        self.answer_window = _HeadlessTranscriptWindow()
        remote_source = get_interviewer_audio_source()
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": APP_VERSION,
            "remote_source": remote_source,
            "microphone_capture": False,
            "asr_backend": moonshine_asr_backend(self.stt_language),
            "moonshine_update_interval_ms": 500,
            "moonshine_word_timestamps": False,
            "language": self.stt_language,
            "app_mode": self.runtime["mode"],
            "logging_enabled": True,
            "stt_diagnostics_enabled": False,
            "codex_enabled": True,
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
            "global_f8": "headless_direct",
            "global_f9": "disabled_headless",
            "test_label": self.test_label,
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
            language=self.stt_language,
        )
        self.codex_worker = create_live_codex_worker(
            self._codex_ready,
            self.codex_thread_id,
            live_codex_settings,
        )
        self.asr_worker.start()

    def trigger_f8(self):
        """Use the same F8 handler as the live GUI without a human keypress."""
        return self._on_f8()

    def record_benchmark_wav_start(self, wav_name, started_at_unix_ns):
        return self._benchmark_wav_start(wav_name, started_at_unix_ns)

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

        run_cleanup("audio", self.remote_audio.stop)
        run_cleanup("moonshine", self.asr_worker.stop)
        run_cleanup("codex", self.codex_worker.stop)
        append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": exit_action,
            "questions": self.question_count,
            "codex_requests": self.codex_request_count,
            "cleanup_errors": [
                {"resource": name, "error": str(error)}
                for name, error in cleanup_errors
            ],
        })
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
