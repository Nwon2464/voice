"""Live interview orchestration and F7/F8/F9 lifecycle control."""

import fcntl
import json
import os
import shlex
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from audio.capture import SAMPLE_RATE, AudioStream, get_interviewer_audio_source
from codex.worker import (
    CODEX_FAST_MODE,
    CODEX_MODEL,
    CODEX_REASONING,
    create_live_codex_worker,
)
from moonshine_streaming_worker import MoonshineStreamingWorker
from session_store import normalize_codex_settings
from ui.live import InterviewControlWindow, TranscriptWindow
from ui.preparation import runtime_options
from ui.styles import install_application_css

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk


APP_DIR = Path(__file__).resolve().parents[1]
INTERVIEW_APP_PATH = APP_DIR / "interview_app.py"
APP_VERSION = "moonshine-small-streaming-dev"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
TRIGGER_SOCKET = RUNTIME_DIR / "interview-assistant-trigger.sock"
TRIGGER_LOCK_PATH = RUNTIME_DIR / "interview-assistant-trigger.lock"
CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
ENTER_DEBOUNCE_MS = 300
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
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


def moonshine_asr_backend(language):
    return (
        "moonshine-small-streaming-ja"
        if language == "ja"
        else "moonshine-small-streaming"
    )


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
        self.checkpoint_context = []
        self.checkpoint_barrier_waiters = []
        self.codex_state_lock = threading.Lock()
        self.last_f8_at = None
        self.last_f7_at = None
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
        f7_hotkey_status = self._install_global_f7()
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
            "question_transcript_mode": (
                "f7_inject_f8_current_stream_cursor_barrier"
            ),
            "preview_transcription": "moonshine_transcript_lines",
            "global_f8": hotkey_status,
            "global_f7": f7_hotkey_status,
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
            "Moonshine Small Streaming EN"
            if self.stt_language == "en"
            else "Moonshine Small Streaming JA"
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

    def _install_global_f7(self):
        return self._install_global_hotkey(
            key="F7",
            path=HOTKEY_F7_PATH,
            name="Interview Assistant: Checkpoint Question",
            trigger_argument="--trigger-f7",
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
                f"{shlex.quote(str(INTERVIEW_APP_PATH))} {trigger_argument}"
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
                if data == b"F7":
                    GLib.idle_add(self._on_f7)
                elif data == b"F8":
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
        if event.keyval == Gdk.KEY_F7:
            self._on_f7()
            return True
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
            self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
            append_log(self.log_path, {
                "event": "question_error",
                "question": pending_question_number,
                "commit_source": commit_source,
                "error": str(error),
            })
            return False

        if not result.get("committed", True):
            self.answer_window.set_status("Waiting for question…")
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
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
        latency_field = {"f8_to_question_ms": round(elapsed * 1000, 1)}
        if not question_text:
            self.answer_window.set_status("No question detected")
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
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
        if commit_source == "f8":
            self.remote_window.set_boundary_status(BOUNDARY_STATUS_F8)
        context_index = len(self.conversation_context)
        self.conversation_context.append(("INTERVIEWER", question_text))
        commit_state = {
            "commit_source": commit_source,
            "text": question_text,
            "question_number": question_number,
            "target_sample_cursor": result["target_sample_cursor"],
            "conversation_context_index": context_index,
            "codex_generation": None,
        }
        self.last_commit_state = commit_state
        if self.codex_enabled:
            def request_after_checkpoints(checkpoint_fallbacks):
                self._answer_ui_trigger = commit_source
                try:
                    generation = self._request_codex_answer(
                        question_number,
                        question_text,
                        checkpoint_fallbacks=checkpoint_fallbacks,
                    )
                    commit_state["codex_generation"] = generation
                    return generation
                finally:
                    self._answer_ui_trigger = None

            self._after_checkpoint_barrier(request_after_checkpoints)
        else:
            self.answer_window.set_status(
                "Codex disabled · question logged only"
            )
            append_log(self.log_path, {
                "event": "codex_request_skipped",
                "question": question_number,
                "reason": "disabled_for_audio_test",
            })
        return False

    def _moonshine_checkpoint_ready(self, commit_started, result, error):
        if not self.running:
            return False
        if error:
            self.remote_window.set_status(f"Moonshine error: {error}")
            self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
            append_log(self.log_path, {
                "event": "checkpoint_error",
                "commit_source": "f7",
                "error": str(error),
            })
            return False

        saved = result.get("checkpoint_saved", False)
        checkpoint = None
        if saved:
            if not hasattr(self, "checkpoint_context"):
                self.checkpoint_context = []
            if not hasattr(self, "checkpoint_barrier_waiters"):
                self.checkpoint_barrier_waiters = []
            checkpoint = {
                "sequence": len(self.checkpoint_context) + 1,
                "text": result["text"].strip(),
                "status": "pending" if self.codex_enabled else "skipped",
                "fallback_consumed": False,
                "target_sample_cursor": result["target_sample_cursor"],
            }
            self.checkpoint_context.append(checkpoint)
            self.last_commit_state = None
        append_log(self.log_path, {
            "event": "checkpoint" if saved else "checkpoint_suppressed",
            "commit_source": "f7",
            "text": result["text"].strip(),
            "checkpoint_sequence": (
                checkpoint["sequence"] if checkpoint is not None else None
            ),
            "target_sample_cursor": result["target_sample_cursor"],
            "consumed_sample_cursor": result["consumed_sample_cursor"],
            "cursor_complete": result["cursor_complete"],
            "audio_drop_samples": result["audio_drop_samples"],
            "max_backlog_ms": result["max_backlog_ms"],
            "barrier_wait_ms": result["barrier_wait_ms"],
            "force_update_ms": result["force_update_ms"],
            "f7_to_checkpoint_ms": round(
                (time.perf_counter() - commit_started) * 1000, 1
            ),
        })
        if saved:
            self.remote_window.set_text(result["display_text"] or result["text"])
            self.remote_window.set_boundary_status(BOUNDARY_STATUS_F7)
            if self.codex_enabled:
                self.answer_window.set_response_status(RESPONSE_STATUS_THINKING)
                items = [{
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            f"{INTERVIEW_CHECKPOINT_MARKER}\n"
                            f"{checkpoint['text']}"
                        ),
                    }],
                }]
                accepted = self.codex_worker.submit_inject_items(
                    items,
                    lambda inject_result, inject_error, saved_checkpoint=checkpoint: (
                        self._checkpoint_inject_finished(
                            saved_checkpoint,
                            inject_result,
                            inject_error,
                        )
                    ),
                )
                if not accepted:
                    self._checkpoint_inject_finished(
                        checkpoint,
                        None,
                        RuntimeError("Codex worker rejected checkpoint injection"),
                    )
            else:
                self.answer_window.set_status(
                    "Codex disabled · checkpoint stored locally"
                )
                self.answer_window.set_response_status(RESPONSE_STATUS_READY)
        else:
            self.answer_window.set_status("No new checkpoint detected")
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
        return False

    def _checkpoint_inject_finished(self, checkpoint, result, error):
        if not self.running:
            return False
        checkpoint["status"] = "failed" if error else "injected"
        append_log(self.log_path, {
            "event": "checkpoint_inject_error" if error else "checkpoint_injected",
            "checkpoint_sequence": checkpoint["sequence"],
            "target_sample_cursor": checkpoint["target_sample_cursor"],
            "item_count": result.get("item_count") if result else None,
            "error": str(error) if error else None,
        })
        if error:
            self.answer_window.set_status(
                "Checkpoint inject failed · will include with next question"
            )
            self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
        elif not self._checkpoint_inject_pending():
            self.answer_window.set_status("Checkpoint context synced")
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
        self._flush_checkpoint_barrier()
        return False

    def _checkpoint_inject_pending(self):
        return any(
            checkpoint["status"] == "pending"
            for checkpoint in getattr(self, "checkpoint_context", [])
        )

    def _failed_checkpoint_fallbacks(self):
        checkpoint_context = getattr(self, "checkpoint_context", [])
        return [
            checkpoint
            for checkpoint in checkpoint_context
            if checkpoint["status"] == "failed"
            and not checkpoint["fallback_consumed"]
        ]

    def _after_checkpoint_barrier(self, callback):
        if self._checkpoint_inject_pending():
            if not hasattr(self, "checkpoint_barrier_waiters"):
                self.checkpoint_barrier_waiters = []
            self.checkpoint_barrier_waiters.append(callback)
            self.answer_window.set_status("Waiting for checkpoint context…")
            return None
        return callback(self._failed_checkpoint_fallbacks())

    def _flush_checkpoint_barrier(self):
        if self._checkpoint_inject_pending():
            return
        waiters = getattr(self, "checkpoint_barrier_waiters", [])
        self.checkpoint_barrier_waiters = []
        checkpoint_fallbacks = self._failed_checkpoint_fallbacks()
        for callback in waiters:
            callback(list(checkpoint_fallbacks))

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
            self.answer_window.set_response_status(RESPONSE_STATUS_ERROR)
            append_log(self.log_path, {
                "event": "question_error",
                "question": question_number,
                "commit_source": "f9_continuation",
                "error": str(error),
            })
            return False
        if not result.get("committed", True):
            self.answer_window.set_status("Waiting for question…")
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
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
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
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
        commit_state = {
            "commit_source": "f9_continuation",
            "text": combined_text,
            "question_number": question_number,
            "target_sample_cursor": result["target_sample_cursor"],
            "conversation_context_index": context_index,
            "codex_generation": None,
        }
        self.last_commit_state = commit_state
        if self.codex_enabled:
            def request_after_checkpoints(checkpoint_fallbacks):
                self._answer_ui_trigger = "f9"
                try:
                    generation = self._request_codex_answer(
                        question_number,
                        combined_text,
                        supersedes_generation=base["codex_generation"],
                        correction={"previous_text": base["text"]},
                        checkpoint_fallbacks=checkpoint_fallbacks,
                    )
                    commit_state["codex_generation"] = generation
                    return generation
                finally:
                    self._answer_ui_trigger = None

            self._after_checkpoint_barrier(request_after_checkpoints)
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
        checkpoint_fallbacks=None,
    ):
        with self.codex_state_lock:
            context_end = len(self.conversation_context)
            context = self.conversation_context[
                self.codex_context_cursor:context_end
            ]
        if context and context[-1] == ("INTERVIEWER", question_text):
            context = context[:-1]
        context_lines = [
            f"{role}: {text}"
            for role, text in context
        ]
        context_lines.extend(
            f"{INTERVIEW_CHECKPOINT_MARKER}\n{checkpoint['text']}"
            for checkpoint in (checkpoint_fallbacks or [])
        )
        context_text = "\n".join(context_lines) or "(none)"
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
            "checkpoint_fallback_items": len(checkpoint_fallbacks or []),
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
            if error is None:
                for checkpoint in checkpoint_fallbacks or []:
                    checkpoint["fallback_consumed"] = True
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

    def _on_f7(self):
        now = time.perf_counter()
        if (
            self.last_f7_at is not None
            and now - self.last_f7_at < ENTER_DEBOUNCE_MS / 1000
        ):
            append_log(self.log_path, {
                "event": "f7_ignored",
                "reason": "debounce",
                "interval_ms": round((now - self.last_f7_at) * 1000, 1),
            })
            return False
        self.last_f7_at = now
        if not self.moonshine_ready or not self.audio_started:
            append_log(self.log_path, {
                "event": "f7_ignored",
                "reason": "moonshine_not_ready",
            })
            self.remote_window.set_status("Moonshine is still loading…")
            return False
        callback = lambda result, error: self._moonshine_checkpoint_ready(
            now, result, error
        )
        try:
            target_cursor, accepted = self.remote_audio.capture_sample_cursor_and(
                lambda cursor: self.asr_worker.request_checkpoint(cursor, callback)
            )
        except Exception as error:
            self._moonshine_checkpoint_ready(now, None, error)
            return False
        if not accepted:
            self._moonshine_checkpoint_ready(
                now,
                None,
                RuntimeError("Moonshine worker rejected F7 checkpoint"),
            )
            return False
        append_log(self.log_path, {
            "event": "f7_trigger",
            "target_sample_cursor": target_cursor,
            "trigger_absolute_seconds": round(target_cursor / SAMPLE_RATE, 3),
            "asr_backend": moonshine_asr_backend(
                getattr(self, "stt_language", "en")
            ),
        })
        self.answer_window.set_status("Saving checkpoint…")
        self.answer_window.set_response_status(RESPONSE_STATUS_THINKING)
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
        self.answer_window.set_response_status(RESPONSE_STATUS_THINKING)
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
            self.answer_window.set_response_status(RESPONSE_STATUS_READY)
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
        self.answer_window.set_response_status(RESPONSE_STATUS_UPDATING)
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
