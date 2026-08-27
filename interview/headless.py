"""Headless live-interview runtime used by benchmark tooling."""

import os
import sys
import threading

from audio.capture import AudioStream, get_interviewer_audio_source
from codex.worker import create_live_codex_worker
from moonshine_streaming_worker import MoonshineStreamingWorker
from session_store import normalize_codex_settings
from interview.controller import (
    APP_VERSION,
    InterviewApp,
    append_log,
    create_app_session,
    moonshine_asr_backend,
)

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib



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
        self.checkpoint_context = []
        self.checkpoint_barrier_waiters = []
        self.codex_state_lock = threading.Lock()
        self.last_f8_at = None
        self.last_f7_at = None
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
            "question_transcript_mode": (
                "f7_inject_f8_current_stream_cursor_barrier"
            ),
            "preview_transcription": "moonshine_transcript_lines",
            "global_f8": "headless_direct",
            "global_f7": "disabled_headless",
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
