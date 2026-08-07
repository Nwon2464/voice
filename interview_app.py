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


# GNOME의 전역 F8 명령은 이 경로만 실행한다. 무거운 모듈은 불러오지 않는다.
if __name__ == "__main__" and "--trigger" in sys.argv:
    raise SystemExit(send_app_command(b"F8"))
if __name__ == "__main__" and "--stop" in sys.argv:
    raise SystemExit(send_app_command(b"STOP"))


import itertools
import json
import math
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps

from audio_utils import (
    BYTES_PER_SECOND,
    POST_CONTEXT_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    append_log,
    transcribe_question,
)
from codex_app_server import CodexAppServerClient


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


APP_VERSION = "v1-app-server-dev"
WHISPER_MODEL = os.environ.get("INTERVIEW_WHISPER_MODEL", "small")
LANGUAGE = os.environ.get("INTERVIEW_LANGUAGE", "en")
CODEX_MODEL = os.environ.get("INTERVIEW_CODEX_MODEL", "gpt-5.6-sol")
CODEX_REASONING = os.environ.get("INTERVIEW_CODEX_REASONING", "low")
CODEX_FAST_MODE = False
CODEX_TIMEOUT_SECONDS = 60
CODEX_DEVELOPER_INSTRUCTIONS = """You assist a job candidate during a live interview.
Use only the supplied interview transcript and do not use tools or browse the web.
Write only a concise, immediately speakable answer draft in the same language as the current question, using 3-5 short sentences.
Do not add headings, follow-up questions, key-points sections, or commentary.
Do not invent specific personal facts; use broadly adaptable wording when details are missing.
Your earlier messages are answer drafts, not proof of what the candidate actually said.
Only transcript lines labelled ME are the candidate's actual spoken words.
Each new turn contains only conversation transcribed since the previous request plus the current interviewer question."""
ANSWER_SCROLL_DEBOUNCE_MS = 450
ANSWER_SMOOTH_SCROLL_THRESHOLD = 1.5
ANSWER_CONTENT_SCROLL_PIXELS = 60
ANSWER_POSITION_GUIDE_HEIGHT = 96
TRANSCRIPTION_PADDING_MS = 200
PREVIEW_INTERVAL_MS = 1_000
PREVIEW_WINDOW_SECONDS = 12
SILENCE_END_MS = 1_000
PRE_ROLL_MS = 300
MAX_UTTERANCE_SECONDS = 60
HISTORY_SECONDS = 180
ENTER_DEBOUNCE_MS = 300
VAD_RMS = int(os.environ.get("INTERVIEW_VAD_RMS", "250"))
MIC_VAD_CHECK_MS = 500
MIC_VAD_OPTIONS = VadOptions(
    threshold=0.5,
    min_speech_duration_ms=250,
    min_silence_duration_ms=SILENCE_END_MS,
    speech_pad_ms=300,
)
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
TEXT_WIDTH_CHARS = shutil.get_terminal_size(fallback=(100, 24)).columns

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
HOTKEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant/"
)


def get_audio_sources():
    sink = subprocess.check_output(
        ["pactl", "get-default-sink"], text=True
    ).strip()
    microphone = subprocess.check_output(
        ["pactl", "get-default-source"], text=True
    ).strip()
    return f"{sink}.monitor", microphone


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


def transcribe_pcm(model, pcm_audio):
    if not pcm_audio:
        return ""
    samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32)
    samples /= 32768.0
    samples = np.pad(
        samples,
        (0, int(SAMPLE_RATE * TRANSCRIPTION_PADDING_MS / 1000)),
    )
    segments, _ = model.transcribe(
        samples,
        language=LANGUAGE,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def save_wav(path, pcm_audio):
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(SAMPLE_WIDTH)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(pcm_audio)


def create_app_session():
    if not TEST_LOGGING:
        return None, None
    session_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    session_dir = APP_DIR / "test_runs" / f"app_session_{session_id}_{os.getpid()}"
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir, session_dir / "session.jsonl"


class AudioStream:
    """Capture one Pulse source and emit speech previews/final utterances."""

    def __init__(self, role, source, on_preview, on_utterance, on_error):
        self.role = role
        self.source = source
        self.on_preview = on_preview
        self.on_utterance = on_utterance
        self.on_error = on_error
        self.process = None
        self.thread = None
        self.stopped = threading.Event()
        self.condition = threading.Condition()
        self.history = bytearray()
        self.history_start = 0
        self.total_bytes = 0
        self.pre_roll = deque()
        self.pre_roll_bytes = 0
        self.active = False
        self.utterance = bytearray()
        self.utterance_start = 0
        self.last_completed_span = None
        self.question_finalize_at = None
        self.speech_run_ms = 0
        self.silence_ms = 0
        self.last_preview_bytes = 0
        self.mic_pending = bytearray()
        self.mic_pending_start = 0
        self.mic_last_check_bytes = 0

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

    def capture_question_marker(self):
        with self.condition:
            trigger = self.total_bytes
            if self.active:
                start = self.utterance_start
                self.question_finalize_at = trigger + int(
                    BYTES_PER_SECOND * POST_CONTEXT_MS / 1000
                )
                target_span = None
                source = "active"
            elif self.last_completed_span is not None:
                start = self.last_completed_span[0]
                target_span = self.last_completed_span
                source = "completed"
            else:
                start = max(self.history_start, trigger - BYTES_PER_SECOND * 30)
                target_span = None
                source = "unmatched"
            return {
                "trigger": trigger,
                "suggested_start": start,
                "target_span": target_span,
                "source": source,
            }

    def _rms(self, data):
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if not samples.size:
            return 0
        return int(math.sqrt(float(np.mean(samples * samples))))

    def _append_pre_roll(self, data):
        self.pre_roll.append(data)
        self.pre_roll_bytes += len(data)
        limit = int(BYTES_PER_SECOND * PRE_ROLL_MS / 1000)
        while self.pre_roll and self.pre_roll_bytes > limit:
            removed = self.pre_roll.popleft()
            self.pre_roll_bytes -= len(removed)

    def _append_history(self, data):
        self.history.extend(data)
        self.total_bytes += len(data)
        limit = BYTES_PER_SECOND * HISTORY_SECONDS
        if len(self.history) > limit:
            remove = len(self.history) - limit
            remove -= remove % SAMPLE_WIDTH
            del self.history[:remove]
            self.history_start += remove

    def _start_utterance(self):
        self.utterance = bytearray().join(self.pre_roll)
        self.utterance_start = self.total_bytes - len(self.utterance)
        self.active = True
        self.silence_ms = 0
        self.last_preview_bytes = len(self.utterance)

    def _process_mic_pending(self, final=False):
        if not self.mic_pending:
            return

        samples = np.frombuffer(self.mic_pending, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        spans = get_speech_timestamps(
            samples,
            MIC_VAD_OPTIONS,
            sampling_rate=SAMPLE_RATE,
        )
        complete_spans = [
            span
            for span in spans
            if final or span["end"] < len(samples)
        ]
        active_span = (
            spans[-1]
            if spans and spans[-1]["end"] >= len(samples) and not final
            else None
        )

        pending = bytes(self.mic_pending)
        pending_start = self.mic_pending_start
        for span in complete_spans:
            start_byte = int(span["start"]) * SAMPLE_WIDTH
            end_byte = int(span["end"]) * SAMPLE_WIDTH
            pcm_audio = pending[start_byte:end_byte]
            absolute_start = pending_start + start_byte
            absolute_end = pending_start + end_byte
            self.last_completed_span = (absolute_start, absolute_end)
            self.on_utterance(
                self.role,
                pcm_audio,
                absolute_start,
                absolute_end,
                {
                    "vad_method": "silero",
                    "vad_probability_threshold": MIC_VAD_OPTIONS.threshold,
                },
            )

        if active_span is not None:
            start_byte = int(active_span["start"]) * SAMPLE_WIDTH
            preview_size = BYTES_PER_SECOND * PREVIEW_WINDOW_SECONDS
            preview = pending[max(start_byte, len(pending) - preview_size):]
            if (
                self.total_bytes - self.last_preview_bytes
                >= BYTES_PER_SECOND * PREVIEW_INTERVAL_MS / 1000
            ):
                self.last_preview_bytes = self.total_bytes
                self.on_preview(self.role, preview)

        if complete_spans:
            remove_bytes = int(complete_spans[-1]["end"]) * SAMPLE_WIDTH
            del self.mic_pending[:remove_bytes]
            self.mic_pending_start += remove_bytes
            self.mic_last_check_bytes = len(self.mic_pending)
        elif not spans and len(self.mic_pending) > BYTES_PER_SECOND * 5:
            keep_bytes = BYTES_PER_SECOND * 3
            remove_bytes = len(self.mic_pending) - keep_bytes
            remove_bytes -= remove_bytes % SAMPLE_WIDTH
            del self.mic_pending[:remove_bytes]
            self.mic_pending_start += remove_bytes
            self.mic_last_check_bytes = len(self.mic_pending)

    def _read_mic_loop(self):
        while not self.stopped.is_set():
            data = self.process.stdout.read(320)
            if not data:
                break
            with self.condition:
                if not self.mic_pending:
                    self.mic_pending_start = self.total_bytes
                self._append_history(data)
                self.mic_pending.extend(data)
                should_check = (
                    len(self.mic_pending) - self.mic_last_check_bytes
                    >= BYTES_PER_SECOND * MIC_VAD_CHECK_MS / 1000
                )
                self.condition.notify_all()
            if should_check:
                self.mic_last_check_bytes = len(self.mic_pending)
                self._process_mic_pending()

        self._process_mic_pending(final=True)

    def _read_loop(self):
        if self.role == "ME":
            try:
                self._read_mic_loop()
            except Exception as error:
                self.on_error(self.role, error)
            return

        try:
            while not self.stopped.is_set():
                data = self.process.stdout.read(320)
                if not data:
                    break
                chunk_ms = len(data) * 1000 / BYTES_PER_SECOND
                rms = self._rms(data)
                loud = rms >= VAD_RMS

                with self.condition:
                    self._append_history(data)
                    self._append_pre_roll(data)

                    if not self.active:
                        self.speech_run_ms = (
                            self.speech_run_ms + chunk_ms if loud else 0
                        )
                        if self.speech_run_ms >= 50:
                            self._start_utterance()
                        self.condition.notify_all()
                        continue

                    self.utterance.extend(data)
                    self.silence_ms = 0 if loud else self.silence_ms + chunk_ms
                    utterance_bytes = len(self.utterance)
                    should_preview = (
                        utterance_bytes >= BYTES_PER_SECOND * 0.6
                        and utterance_bytes - self.last_preview_bytes
                        >= BYTES_PER_SECOND * PREVIEW_INTERVAL_MS / 1000
                    )
                    should_finish = (
                        self.silence_ms >= SILENCE_END_MS
                        or (
                            self.question_finalize_at is not None
                            and self.total_bytes >= self.question_finalize_at
                        )
                        or utterance_bytes
                        >= BYTES_PER_SECOND * MAX_UTTERANCE_SECONDS
                    )
                    if should_preview:
                        preview_size = BYTES_PER_SECOND * PREVIEW_WINDOW_SECONDS
                        preview = bytes(self.utterance[-preview_size:])
                        self.last_preview_bytes = utterance_bytes
                    else:
                        preview = None
                    if should_finish:
                        final = (
                            bytes(self.utterance),
                            self.utterance_start,
                            self.utterance_start + len(self.utterance),
                            {
                                "vad_method": "rms",
                                "vad_threshold_rms": VAD_RMS,
                            },
                        )
                        self.last_completed_span = (final[1], final[2])
                        self.active = False
                        self.utterance.clear()
                        self.speech_run_ms = 0
                        self.silence_ms = 0
                        self.last_preview_bytes = 0
                        self.question_finalize_at = None
                    else:
                        final = None
                    self.condition.notify_all()

                if preview is not None:
                    self.on_preview(self.role, preview)
                if final is not None:
                    self.on_utterance(self.role, *final)
        except Exception as error:
            self.on_error(self.role, error)


class WhisperWorker:
    def __init__(self, on_ready):
        self.jobs = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.on_ready = on_ready
        self.accepting = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, priority, processor, callback):
        if self.accepting:
            self.jobs.put((priority, next(self.sequence), processor, callback))

    def stop(self):
        self.accepting = False
        self.jobs.put((-1, next(self.sequence), None, None))
        self.thread.join()

    def _run(self):
        try:
            model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        except Exception as error:
            GLib.idle_add(self.on_ready, error)
            return
        GLib.idle_add(self.on_ready, None)

        while True:
            _, _, processor, callback = self.jobs.get()
            if processor is None:
                return
            try:
                result = processor(model)
                error = None
            except Exception as caught:
                result = None
                error = caught
            GLib.idle_add(callback, result, error)


class CodexWorker:
    """Run queued turns on one persistent App Server thread."""

    def __init__(self, on_ready):
        self.jobs = queue.Queue()
        self.accepting = True
        self.on_ready = on_ready
        self.client = None
        self.client_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, prompt, callback):
        if self.accepting:
            self.jobs.put((prompt, callback))

    def stop(self):
        self.accepting = False
        with self.client_lock:
            client = self.client
        if client is not None:
            client.stop()
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self.jobs.put(None)
        self.thread.join(timeout=3)

    def _run(self):
        try:
            client = CodexAppServerClient(
                model=CODEX_MODEL,
                effort=CODEX_REASONING,
                cwd=APP_DIR,
                developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
                timeout_seconds=CODEX_TIMEOUT_SECONDS,
            )
            with self.client_lock:
                self.client = client
            ready = client.start()
            startup_error = None
        except Exception as caught:
            ready = None
            startup_error = caught
        GLib.idle_add(self.on_ready, ready, startup_error)

        while True:
            job = self.jobs.get()
            if job is None:
                return
            prompt, callback = job
            if startup_error is not None:
                result = None
                error = startup_error
            else:
                try:
                    result = client.run_turn(prompt)
                    error = None
                except Exception as caught:
                    result = None
                    error = caught
            GLib.idle_add(callback, result, error)


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
    ):
        super().__init__(title=title)
        self.role = role
        self.on_close = on_close
        self.focus_mode = focus_mode
        self.last_focus_scroll_at = None
        self.smooth_scroll_delta = 0.0
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
        close_button = Gtk.Button(label="×")
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_can_focus(False)
        close_button.set_tooltip_text("Close")
        close_button.get_style_context().add_class("close-button")
        close_button.connect("clicked", lambda _button: self.on_close())
        if not self.focus_mode:
            header.pack_start(heading, True, True, 0)
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
            self.text.get_buffer().set_text("Waiting for F8…")
            self.text.get_style_context().add_class("focus-transcript")
        else:
            self.text = Gtk.Label(label="Whisper loading…")
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

            close_button.set_halign(Gtk.Align.END)
            close_button.set_valign(Gtk.Align.START)
            close_button.set_margin_top(2)
            close_button.set_margin_end(2)
            answer_overlay.add_overlay(close_button)
            box.pack_start(answer_overlay, True, True, 0)

            for widget in (self, answer_overlay, scroller, self.text):
                widget.add_events(Gdk.EventMask.SCROLL_MASK)
                widget.connect("scroll-event", self._focus_scroll)
            scroller.connect("size-allocate", self._answer_view_resized)
        else:
            box.pack_start(scroller, True, True, 0)

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
    def __init__(self):
        self.session_dir, self.log_path = create_app_session()
        self.running = True
        self.preview_pending = {"INTERVIEWER": False, "ME": False}
        self.utterance_counts = {"INTERVIEWER": 0, "ME": 0}
        self.question_count = 0
        self.codex_request_count = 0
        self.remote_utterances = []
        self.pending_questions = []
        self.conversation_context = []
        self.codex_context_cursor = 0
        self.transcript_lock = threading.Lock()
        self.last_f8_at = None
        self.socket_thread = None
        self.trigger_socket = None
        self.window_state = self._load_window_state()
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        screen_width = geometry.width
        screen_height = geometry.height

        remote_default = (
            geometry.x + (screen_width - 720) // 2,
            geometry.y + 48,
            720,
            170,
        )
        me_default = (
            geometry.x + screen_width - 550,
            geometry.y + screen_height - 210,
            520,
            160,
        )
        answer_default = (
            geometry.x + (screen_width - 720) // 2,
            geometry.y + 230,
            720,
            300,
        )
        remote_state = self.window_state.get("INTERVIEWER", remote_default)
        me_state = self.window_state.get("ME", me_default)
        answer_state = self.window_state.get("ANSWER", answer_default)
        self.remote_window = TranscriptWindow(
            "INTERVIEWER",
            "INTERVIEWER",
            remote_state[2],
            remote_state[3],
            remote_state[:2],
            self.shutdown,
        )
        self.me_window = TranscriptWindow(
            "ME", "ME", me_state[2], me_state[3], me_state[:2], self.shutdown
        )
        self.answer_window = TranscriptWindow(
            "ANSWER",
            "ANSWER",
            answer_state[2],
            answer_state[3],
            answer_state[:2],
            self.shutdown,
            focus_mode=True,
        )
        self.answer_window.set_status("Waiting for F8…")
        for window in (self.remote_window, self.me_window, self.answer_window):
            window.connect("key-press-event", self._key_pressed)

        self._install_css()
        self.remote_window.show_all()
        self.me_window.show_all()
        self.answer_window.show_all()
        self._start_trigger_listener()
        hotkey_status = self._install_global_f8()

        remote_source, mic_source = get_audio_sources()
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": APP_VERSION,
            "remote_source": remote_source,
            "microphone_source": mic_source,
            "whisper_model": WHISPER_MODEL,
            "language": LANGUAGE,
            "codex_enabled": True,
            "codex_model": CODEX_MODEL,
            "codex_reasoning_effort": CODEX_REASONING,
            "codex_fast_mode": CODEX_FAST_MODE,
            "codex_transport": "app_server_stdio",
            "codex_session_scope": "app_lifetime_ephemeral",
            "question_transcript_mode": "reuse_interviewer_utterance",
            "global_f8": hotkey_status,
            "test_label": TEST_LABEL,
        })
        self.remote_audio = AudioStream(
            "INTERVIEWER", remote_source,
            self._preview_audio, self._final_audio, self._audio_error,
        )
        self.me_audio = AudioStream(
            "ME", mic_source,
            self._preview_audio, self._final_audio, self._audio_error,
        )
        self.worker = WhisperWorker(self._whisper_ready)
        self.codex_worker = CodexWorker(self._codex_ready)
        self.remote_audio.start()
        self.me_audio.start()

    def _install_css(self):
        css = b"""
        window { background-color: rgba(18, 20, 24, 0.94); border-radius: 14px; }
        window.interviewer { border: 2px solid rgba(95, 176, 255, 0.85); }
        window.me { border: 2px solid rgba(116, 220, 158, 0.75); }
        window.answer { border: 2px solid rgba(255, 195, 92, 0.82); }
        .heading { color: #8ec8ff; font: bold 12px Sans; letter-spacing: 1px; }
        window.me .heading { color: #80dfa6; }
        window.answer .heading { color: #ffc75c; }
        .position-guide { border: 2px solid rgba(255, 195, 92, 0.75); background: transparent; }
        .focus-transcript { color: #fff5d9; background: transparent; font: bold 22px Sans; }
        .focus-transcript text { color: #fff5d9; background: transparent; }
        .transcript { color: #ffffff; font: 20px Sans; }
        .close-button { color: #d8dde5; font: bold 18px Sans; padding: 0 4px; }
        .close-button:hover { color: #ffffff; background: rgba(255, 90, 90, 0.55); }
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
            ("ME", self.me_window),
            ("ANSWER", self.answer_window),
        ):
            x, y = window.get_position()
            width, height = window.get_size()
            state[role] = [x, y, width, height]
        WINDOW_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _install_global_f8(self):
        try:
            media_keys = Gio.Settings.new(
                "org.gnome.settings-daemon.plugins.media-keys"
            )
            paths = list(media_keys.get_strv("custom-keybindings"))
            for path in paths:
                setting = Gio.Settings.new_with_path(
                    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                    path,
                )
                if setting.get_string("binding") == "F8" and path != HOTKEY_PATH:
                    return "conflict"

            setting = Gio.Settings.new_with_path(
                "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                HOTKEY_PATH,
            )
            command = (
                f"{shlex.quote(sys.executable)} "
                f"{shlex.quote(str(Path(__file__).resolve()))} --trigger"
            )
            setting.set_string("name", "Interview Assistant: Capture Question")
            setting.set_string("command", command)
            setting.set_string("binding", "F8")
            if HOTKEY_PATH not in paths:
                paths.append(HOTKEY_PATH)
                media_keys.set_strv("custom-keybindings", paths)
            Gio.Settings.sync()
            return "installed"
        except Exception as error:
            append_log(self.log_path, {
                "event": "hotkey_error",
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
                elif data == b"STOP":
                    GLib.idle_add(self.shutdown)

        self.socket_thread = threading.Thread(target=listen, daemon=True)
        self.socket_thread.start()

    def _key_pressed(self, _window, event):
        if event.keyval == Gdk.KEY_F8:
            self._on_f8()
            return True
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval == Gdk.KEY_q:
            self.shutdown()
            return True
        return False

    def _whisper_ready(self, error):
        if error:
            message = f"Whisper error: {error}"
            self.remote_window.set_status(message)
            self.me_window.set_status(message)
            append_log(self.log_path, {"event": "whisper_error", "error": str(error)})
        else:
            self.remote_window.set_status("Listening…")
            self.me_window.set_status("Listening…")
        return False

    def _preview_audio(self, role, pcm_audio):
        if self.preview_pending[role]:
            return
        self.preview_pending[role] = True

        def processor(model):
            return transcribe_pcm(model, pcm_audio)

        def finished(text, error):
            self.preview_pending[role] = False
            if error:
                append_log(self.log_path, {
                    "event": "preview_error", "role": role, "error": str(error),
                })
            elif text:
                self._window(role).set_text(text)
            return False

        self.worker.submit(2, processor, finished)

    def _final_audio(self, role, pcm_audio, start, end, vad_details):
        self.utterance_counts[role] += 1
        utterance = self.utterance_counts[role]
        audio_file = None
        if self.session_dir:
            audio_file = f"{role.lower()}_{utterance:03d}.wav"
            save_wav(self.session_dir / audio_file, pcm_audio)

        state = None
        if role == "INTERVIEWER":
            state = {
                "utterance": utterance,
                "start": start,
                "end": end,
                "audio_file": audio_file,
                "questions": [],
                "result": None,
                "error": None,
            }
            with self.transcript_lock:
                self.remote_utterances.append(state)
                for marker in list(self.pending_questions):
                    if self._question_matches_utterance(marker, state):
                        state["questions"].append(marker)
                        self.pending_questions.remove(marker)
                self.remote_utterances[:] = self.remote_utterances[-20:]

        def processor(model):
            started = time.perf_counter()
            marker = None
            if state is not None:
                with self.transcript_lock:
                    if state["questions"]:
                        marker = state["questions"][0]
            if marker is None:
                text = transcribe_pcm(model, pcm_audio)
                details = {}
            else:
                relative_trigger = max(0, marker["trigger"] - start)
                text, _boundary, details = transcribe_question(
                    model,
                    pcm_audio,
                    relative_trigger,
                    silence_padding_ms=TRANSCRIPTION_PADDING_MS,
                )
            return {
                "text": text,
                "elapsed": time.perf_counter() - started,
                "details": details,
            }

        def finished(result, error):
            if error:
                append_log(self.log_path, {
                    "event": "utterance_error",
                    "role": role,
                    "utterance": utterance,
                    "error": str(error),
                })
                if state is not None:
                    with self.transcript_lock:
                        state["error"] = error
                        questions = list(state["questions"])
                    for marker in questions:
                        self._commit_question(marker, state, None, error)
            else:
                if state is not None:
                    with self.transcript_lock:
                        state["result"] = result
                append_log(self.log_path, {
                    "event": "utterance",
                    "role": role,
                    "utterance": utterance,
                    "audio_file": audio_file,
                    "start_absolute_seconds": round(start / BYTES_PER_SECOND, 3),
                    "end_absolute_seconds": round(end / BYTES_PER_SECOND, 3),
                    "text": result["text"],
                    "stt_seconds": round(result["elapsed"], 3),
                    **vad_details,
                    **result["details"],
                })
                if result["text"]:
                    self._window(role).set_text(result["text"])
                    self.conversation_context.append((role, result["text"]))
                if state is not None:
                    with self.transcript_lock:
                        questions = list(state["questions"])
                    for marker in questions:
                        self._commit_question(marker, state, result, None)
            return False

        priority = 0 if state is not None and state["questions"] else 1
        self.worker.submit(priority, processor, finished)

    @staticmethod
    def _question_matches_utterance(marker, state):
        target_span = marker["target_span"]
        if target_span is not None:
            return target_span == (state["start"], state["end"])
        return (
            marker["source"] == "active"
            and marker["suggested_start"] == state["start"]
            and marker["trigger"] <= state["end"]
        )

    def _commit_question(self, marker, state, result, error):
        if marker.get("committed"):
            return
        marker["committed"] = True
        if error:
            append_log(self.log_path, {
                "event": "question_error",
                "question": marker["question"],
                "source_utterance": state["utterance"],
                "error": str(error),
            })
            return
        append_log(self.log_path, {
            "event": "question",
            "question": marker["question"],
            "source_utterance": state["utterance"],
            "audio_file": state["audio_file"],
            "text": result["text"],
            "stt_seconds": round(result["elapsed"], 3),
            "additional_stt_seconds": 0,
            "transcript_reused": True,
            **result["details"],
        })
        if result["text"]:
            self.remote_window.set_text(result["text"])
            self._request_codex_answer(marker["question"], result["text"])

    def _request_codex_answer(self, question_number, question_text):
        context_end = len(self.conversation_context)
        context = self.conversation_context[self.codex_context_cursor:context_end]
        self.codex_context_cursor = context_end
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
        self.answer_window.set_status("Thinking…")
        append_log(self.log_path, {
            "event": "codex_request",
            "request": request_number,
            "question": question_number,
            "model": CODEX_MODEL,
            "reasoning_effort": CODEX_REASONING,
            "fast_mode": CODEX_FAST_MODE,
            "context_items": len(context),
        })

        def finished(result, error):
            if not self.running:
                return False
            if error:
                self.answer_window.set_status(f"Codex error: {error}")
                append_log(self.log_path, {
                    "event": "codex_error",
                    "request": request_number,
                    "question": question_number,
                    "error": str(error),
                })
            else:
                self.answer_window.set_text(result["text"])
                append_log(self.log_path, {
                    "event": "codex_response",
                    "request": request_number,
                    "question": question_number,
                    "text": result["text"],
                    "elapsed_seconds": round(result["elapsed"], 3),
                    "first_token_seconds": (
                        round(result["first_token_seconds"], 3)
                        if result["first_token_seconds"] is not None
                        else None
                    ),
                    "thread_id": result["thread_id"],
                    "turn_id": result["turn_id"],
                })
            return False

        self.codex_worker.submit(prompt, finished)

    def _codex_ready(self, result, error):
        if not self.running:
            return False
        if error:
            self.answer_window.set_status(f"Codex startup error: {error}")
            append_log(self.log_path, {
                "event": "codex_app_server_error",
                "error": str(error),
            })
        else:
            append_log(self.log_path, {
                "event": "codex_app_server_ready",
                "thread_id": result["thread_id"],
                "startup_seconds": round(result["startup_seconds"], 3),
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
        self.question_count += 1
        question_number = self.question_count
        marker = self.remote_audio.capture_question_marker()
        marker["question"] = question_number
        marker["committed"] = False
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": question_number,
            "trigger_absolute_seconds": round(
                marker["trigger"] / BYTES_PER_SECOND, 3
            ),
            "utterance_state": marker["source"],
        })
        matched_state = None
        matched_result = None
        matched_error = None
        with self.transcript_lock:
            for state in reversed(self.remote_utterances):
                if self._question_matches_utterance(marker, state):
                    state["questions"].append(marker)
                    matched_state = state
                    matched_result = state["result"]
                    matched_error = state["error"]
                    break
            if matched_state is None:
                self.pending_questions.append(marker)
        if matched_result is not None:
            self._commit_question(marker, matched_state, matched_result, None)
        elif matched_error is not None:
            self._commit_question(marker, matched_state, None, matched_error)
        return False

    def _audio_error(self, role, error):
        append_log(self.log_path, {
            "event": "audio_error", "role": role, "error": str(error),
        })
        GLib.idle_add(self._window(role).set_status, f"Audio error: {error}")

    def _window(self, role):
        return self.remote_window if role == "INTERVIEWER" else self.me_window

    def shutdown(self, *_args):
        if not self.running:
            return False
        self.running = False
        self._save_window_state()
        self.remote_audio.stop()
        self.me_audio.stop()
        self.worker.stop()
        self.codex_worker.stop()
        if self.trigger_socket is not None:
            self.trigger_socket.close()
        TRIGGER_SOCKET.unlink(missing_ok=True)
        append_log(self.log_path, {
            "event": "app_session_end",
            "questions": self.question_count,
            "interviewer_utterances": self.utterance_counts["INTERVIEWER"],
            "me_utterances": self.utterance_counts["ME"],
            "codex_requests": self.codex_request_count,
        })
        Gtk.main_quit()
        return False


def main():
    app = InterviewApp()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, app.shutdown)
    Gtk.main()


if __name__ == "__main__":
    main()
