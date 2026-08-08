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
import multiprocessing as mp
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

from audio_utils import (
    BYTES_PER_SECOND,
    POST_CONTEXT_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    append_log,
    transcribe_question,
)
from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRecoverableError,
    CodexAppServerTransportError,
)
from session_store import SessionStore


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
TRANSCRIPTION_PADDING_MS = 200
PREVIEW_INTERVAL_MS = 1_000
PREVIEW_WINDOW_SECONDS = 12
SILENCE_END_MS = 1_000
PRE_ROLL_MS = 300
MAX_UTTERANCE_SECONDS = 60
HISTORY_SECONDS = 180
ENTER_DEBOUNCE_MS = 300
WHISPER_WARMUP_SECONDS = 1
VAD_RMS = int(os.environ.get("INTERVIEW_VAD_RMS", "250"))
TEST_LOGGING = os.environ.get("INTERVIEW_TEST_LOG", "0") != "0"
TEST_LABEL = os.environ.get("INTERVIEW_TEST_LABEL")
TEXT_WIDTH_CHARS = shutil.get_terminal_size(fallback=(100, 24)).columns

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "interview-assistant"
WINDOW_STATE_PATH = CONFIG_DIR / "window_state.json"
SESSION_STORE_PATH = CONFIG_DIR / "sessions.json"
SESSION_INITIALIZATION_TEXT = (
    "Interview Assistant persistent session initialized. "
    "This is background metadata, not an interview question."
)
HOTKEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant/"
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
        self.awaiting_question_boundary = False
        self.awaiting_question_end = None
        self.deferred_audio = bytearray()
        self.replay_audio = bytearray()
        self.replay_start = 0
        self.speech_run_ms = 0
        self.silence_ms = 0
        self.last_preview_bytes = 0

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

    def _start_utterance(self, absolute_end):
        self.utterance = bytearray().join(self.pre_roll)
        self.utterance_start = absolute_end - len(self.utterance)
        self.active = True
        self.silence_ms = 0
        self.last_preview_bytes = len(self.utterance)

    def requeue_question_remainder(
        self,
        pcm_remainder,
        remainder_start,
        source_end,
    ):
        """Replay the post-boundary PCM before audio captured after it."""
        with self.condition:
            if (
                not self.awaiting_question_boundary
                or self.awaiting_question_end != source_end
            ):
                return False
            self.replay_audio = bytearray(pcm_remainder)
            self.replay_audio.extend(self.deferred_audio)
            self.replay_start = remainder_start
            self.deferred_audio.clear()
            self.awaiting_question_boundary = False
            self.awaiting_question_end = None
            self.pre_roll.clear()
            self.pre_roll_bytes = 0
            self.speech_run_ms = 0
            self.silence_ms = 0
            self.last_preview_bytes = 0
            self.condition.notify_all()
        return True

    def _process_detection_chunk(self, data, absolute_end):
        chunk_ms = len(data) * 1000 / BYTES_PER_SECOND
        rms = self._rms(data)
        loud = rms >= VAD_RMS
        self._append_pre_roll(data)

        if not self.active:
            self.speech_run_ms = (
                self.speech_run_ms + chunk_ms if loud else 0
            )
            if self.speech_run_ms >= 50:
                self._start_utterance(absolute_end)
            return None, None

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
                and absolute_end >= self.question_finalize_at
            )
            or utterance_bytes >= BYTES_PER_SECOND * MAX_UTTERANCE_SECONDS
        )
        if should_preview:
            preview_size = BYTES_PER_SECOND * PREVIEW_WINDOW_SECONDS
            preview = bytes(self.utterance[-preview_size:])
            self.last_preview_bytes = utterance_bytes
        else:
            preview = None
        if not should_finish:
            return preview, None

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
        if self.question_finalize_at is not None:
            self.awaiting_question_boundary = True
            self.awaiting_question_end = final[2]
            self.deferred_audio.clear()
        self.active = False
        self.utterance.clear()
        self.speech_run_ms = 0
        self.silence_ms = 0
        self.last_preview_bytes = 0
        self.question_finalize_at = None
        return preview, final

    def _process_replay(self):
        if not self.replay_audio:
            return []
        replay = bytes(self.replay_audio)
        cursor = self.replay_start
        self.replay_audio.clear()
        events = []
        for offset in range(0, len(replay), 320):
            chunk = replay[offset:offset + 320]
            cursor += len(chunk)
            preview, final = self._process_detection_chunk(chunk, cursor)
            events.append((preview, final))
        return events

    def _read_loop(self):
        try:
            while not self.stopped.is_set():
                data = self.process.stdout.read(320)
                if not data:
                    break

                with self.condition:
                    self._append_history(data)
                    if self.awaiting_question_boundary:
                        self.deferred_audio.extend(data)
                        self.condition.notify_all()
                        continue
                    events = self._process_replay()
                    events.append(
                        self._process_detection_chunk(data, self.total_bytes)
                    )
                    self.condition.notify_all()

                for preview, final in events:
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
        self.jobs.put((math.inf, next(self.sequence), None, None))
        self.thread.join()

    def _run(self):
        startup_started = time.perf_counter()
        try:
            load_started = time.perf_counter()
            model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            load_seconds = time.perf_counter() - load_started
            warmup_started = time.perf_counter()
            warmup_audio = np.zeros(
                SAMPLE_RATE * WHISPER_WARMUP_SECONDS,
                dtype=np.float32,
            )
            segments, _ = model.transcribe(
                warmup_audio,
                language=LANGUAGE,
                vad_filter=False,
                word_timestamps=True,
                condition_on_previous_text=False,
            )
            list(segments)
            warmup_seconds = time.perf_counter() - warmup_started
        except Exception as error:
            GLib.idle_add(self.on_ready, None, error)
            return
        GLib.idle_add(self.on_ready, {
            "load_seconds": load_seconds,
            "warmup_seconds": warmup_seconds,
            "startup_seconds": time.perf_counter() - startup_started,
        }, None)

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


def _preview_whisper_process(input_connection, output_connection):
    """Run cancelable preview transcription outside the final-answer worker."""
    startup_started = time.perf_counter()
    try:
        load_started = time.perf_counter()
        model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
        load_seconds = time.perf_counter() - load_started
        warmup_started = time.perf_counter()
        segments, _ = model.transcribe(
            np.zeros(SAMPLE_RATE * WHISPER_WARMUP_SECONDS, dtype=np.float32),
            language=LANGUAGE,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        list(segments)
        output_connection.send((
            "ready",
            {
                "load_seconds": load_seconds,
                "warmup_seconds": time.perf_counter() - warmup_started,
                "startup_seconds": time.perf_counter() - startup_started,
            },
            None,
        ))
    except Exception as error:
        output_connection.send(("ready", None, str(error)))
        return

    try:
        while True:
            try:
                job = input_connection.recv()
            except EOFError:
                return
            if job is None:
                return
            job_id, pcm_audio = job
            started = time.perf_counter()
            try:
                text = transcribe_pcm(model, pcm_audio)
                error = None
            except Exception as caught:
                text = None
                error = str(caught)
            output_connection.send((
                "result",
                job_id,
                text,
                error,
                time.perf_counter() - started,
            ))
    finally:
        input_connection.close()
        output_connection.close()


class PreviewWhisperWorker:
    """Cancelable small-model process used only for live preview text."""

    def __init__(self, on_ready):
        self.on_ready = on_ready
        self.context = mp.get_context("spawn")
        self.lock = threading.Lock()
        self.generation = 0
        self.job_sequence = itertools.count()
        self.process = None
        self.input_connection = None
        self.output_connection = None
        self.callbacks = {}
        self.ready = False
        self.stopped = False

    def start(self):
        with self.lock:
            if self.stopped:
                return False
            if self.process is not None and self.process.is_alive():
                return False
            self.generation += 1
            generation = self.generation
            child_input, parent_input = self.context.Pipe(duplex=False)
            parent_output, child_output = self.context.Pipe(duplex=False)
            process = self.context.Process(
                target=_preview_whisper_process,
                args=(child_input, child_output),
                daemon=True,
            )
            self.process = process
            self.input_connection = parent_input
            self.output_connection = parent_output
            self.ready = False
            try:
                process.start()
            except Exception:
                self.process = None
                self.input_connection = None
                self.output_connection = None
                for connection in (
                    child_input,
                    parent_input,
                    parent_output,
                    child_output,
                ):
                    connection.close()
                raise
            child_input.close()
            child_output.close()
        threading.Thread(
            target=self._listen,
            args=(generation, process, parent_output),
            daemon=True,
        ).start()
        return True

    def submit(self, pcm_audio, callback):
        with self.lock:
            if (
                self.stopped
                or not self.ready
                or self.process is None
                or not self.process.is_alive()
            ):
                return False
            job_id = next(self.job_sequence)
            self.callbacks[job_id] = callback
            input_connection = self.input_connection
        try:
            input_connection.send((job_id, pcm_audio))
            return True
        except (BrokenPipeError, EOFError, OSError):
            with self.lock:
                self.callbacks.pop(job_id, None)
            return False

    def cancel(self):
        started = time.perf_counter()
        with self.lock:
            self.generation += 1
            process = self.process
            input_connection = self.input_connection
            output_connection = self.output_connection
            self.process = None
            self.input_connection = None
            self.output_connection = None
            self.ready = False
            self.callbacks.clear()
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
        for connection in (input_connection, output_connection):
            if connection is not None:
                connection.close()
        return time.perf_counter() - started

    def stop(self):
        self.stopped = True
        return self.cancel()

    def _listen(self, generation, process, output_connection):
        was_ready = False
        while True:
            with self.lock:
                if generation != self.generation or self.stopped:
                    return
            try:
                if output_connection.poll(0.1):
                    message = output_connection.recv()
                else:
                    message = None
            except (EOFError, OSError):
                return
            if message is None:
                if process.is_alive():
                    continue
                if not was_ready:
                    GLib.idle_add(
                        self._deliver_ready,
                        generation,
                        None,
                        "Preview Whisper process stopped during startup",
                    )
                return

            if message[0] == "ready":
                _, result, error = message
                was_ready = error is None
                with self.lock:
                    if generation != self.generation:
                        return
                    self.ready = was_ready
                GLib.idle_add(
                    self._deliver_ready,
                    generation,
                    result,
                    error,
                )
                if error is not None:
                    return
                continue

            _, job_id, text, error, elapsed = message
            with self.lock:
                if generation != self.generation:
                    return
                callback = self.callbacks.pop(job_id, None)
            if callback is not None:
                GLib.idle_add(
                    self._deliver_result,
                    generation,
                    callback,
                    text,
                    error,
                    elapsed,
                )

    def _deliver_ready(self, generation, result, error):
        with self.lock:
            if generation != self.generation or self.stopped:
                return False
        return self.on_ready(result, error)

    def _deliver_result(
        self,
        generation,
        callback,
        text,
        error,
        elapsed,
    ):
        with self.lock:
            if generation != self.generation or self.stopped:
                return False
        return callback(text, error, elapsed)


class CodexWorker:
    """Run queued turns on one persistent App Server thread."""

    _LATEST_JOB = object()

    def __init__(self, on_ready, thread_id=None):
        self.jobs = queue.Queue()
        self.accepting = True
        self.on_ready = on_ready
        self.thread_id = thread_id
        self.client = None
        self.client_lock = threading.Lock()
        self.turn_active = threading.Event()
        self.latest_lock = threading.Lock()
        self.latest_job = None
        self.latest_token_queued = False
        self.active_latest_generation = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

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
            model=CODEX_MODEL,
            effort=CODEX_REASONING,
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


SESSION_RESPONSE_NEW = 1
SESSION_RESPONSE_ARCHIVE = 2


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

        self.model = Gtk.ListStore(str, str)
        preferred_path = None
        for index, session in enumerate(sessions):
            self.model.append((session.get("name", ""), session["thread_id"]))
            if session["thread_id"] == preferred_thread_id:
                preferred_path = Gtk.TreePath.new_from_indices([index])

        self.tree = Gtk.TreeView(model=self.model)
        self.tree.set_headers_visible(True)
        self.tree.set_activate_on_single_click(False)
        self.tree.append_column(
            Gtk.TreeViewColumn(
                "만든 시각",
                Gtk.CellRendererText(),
                text=0,
            )
        )
        id_renderer = Gtk.CellRendererText()
        id_renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.tree.append_column(
            Gtk.TreeViewColumn("세션 ID", id_renderer, text=1)
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
        return {
            "name": model[tree_iter][0],
            "thread_id": model[tree_iter][1],
        }

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


def _new_codex_client():
    return CodexAppServerClient(
        model=CODEX_MODEL,
        effort=CODEX_REASONING,
        cwd=APP_DIR,
        developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
    )


def create_persisted_codex_session():
    client = _new_codex_client()
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
                result = create_persisted_codex_session()
                created = datetime.now().astimezone()
                thread_id = result["thread_id"]
                store.add(
                    thread_id,
                    created.strftime("%Y-%m-%d %H:%M"),
                    created.isoformat(timespec="seconds"),
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
            return selected["thread_id"]

        return None


CHAT_RESPONSE_BACK = 10
CHAT_RESPONSE_START_INTERVIEW = 11
CHAT_HISTORY_PAGE_TURNS = 50


class PreparationChatDialog(Gtk.Dialog):
    """Chat with the selected Codex thread before starting audio capture."""

    def __init__(self, thread_id):
        super().__init__(title="Interview Preparation")
        self.thread_id = thread_id
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
        self._set_busy(False)
        self.status.set_text("Codex 세션에 연결 중…")
        self.worker = CodexWorker(self._codex_ready, thread_id=self.thread_id)
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

    def __init__(self, position, on_back, on_close):
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
    def __init__(self, codex_thread_id):
        self.session_dir, self.log_path = create_app_session()
        self.codex_thread_id = codex_thread_id
        self.codex_enabled = CODEX_ENABLED
        self.exit_action = None
        self.running = True
        self.preview_pending = False
        self.interviewer_utterance_count = 0
        self.question_count = 0
        self.codex_request_count = 0
        self.active_codex_generation = 0
        self.codex_request_states = {}
        self.remote_utterances = []
        self.pending_questions = []
        self.conversation_context = []
        self.codex_context_cursor = 0
        self.codex_state_lock = threading.Lock()
        self.transcript_lock = threading.Lock()
        self.last_f8_at = None
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
        )
        if self.codex_enabled:
            self.answer_window.set_status("Waiting for F8…")
        else:
            self.answer_window.set_status("Codex disabled · Waiting for F8…")
        for window in (self.remote_window, self.answer_window):
            window.connect("key-press-event", self._key_pressed)

        self._install_css()
        self.remote_window.show_all()
        self.answer_window.show_all()
        self.control_window.show_all()
        self._start_trigger_listener()
        hotkey_status = self._install_global_f8()

        remote_source = get_interviewer_audio_source()
        append_log(self.log_path, {
            "event": "app_session_start",
            "app_version": APP_VERSION,
            "remote_source": remote_source,
            "microphone_capture": False,
            "whisper_model": WHISPER_MODEL,
            "language": LANGUAGE,
            "codex_enabled": self.codex_enabled,
            "codex_model": CODEX_MODEL,
            "codex_reasoning_effort": CODEX_REASONING,
            "codex_fast_mode": CODEX_FAST_MODE,
            "codex_transport": "app_server_stdio",
            "codex_session_scope": "persistent_selected_thread",
            "codex_thread_id": self.codex_thread_id,
            "candidate_response_source": (
                "completed_codex_answer_assumed_spoken_"
                "superseded_answer_not_spoken"
            ),
            "question_transcript_mode": "reuse_interviewer_utterance",
            "preview_transcription": "cancelable_small_process",
            "global_f8": hotkey_status,
            "test_label": TEST_LABEL,
        })
        self.remote_audio = AudioStream(
            "INTERVIEWER", remote_source,
            self._preview_audio, self._final_audio, self._audio_error,
        )
        self.preview_worker = PreviewWhisperWorker(
            self._preview_whisper_ready,
        )
        self.worker = WhisperWorker(self._whisper_ready)
        self.codex_worker = None
        if self.codex_enabled:
            self.codex_worker = CodexWorker(
                self._codex_ready,
                thread_id=self.codex_thread_id,
            )
        self.remote_audio.start()

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
        .close-button { color: #d8dde5; font: bold 18px Sans; padding: 0 4px; }
        .close-button:hover { color: #ffffff; background: rgba(255, 90, 90, 0.55); }
        .control-button { color: #fff5d9; font: bold 18px Sans; padding: 2px 8px; }
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

    def _whisper_ready(self, result, error):
        if error:
            message = f"Whisper error: {error}"
            self.remote_window.set_status(message)
            append_log(self.log_path, {"event": "whisper_error", "error": str(error)})
        else:
            self.remote_window.set_status("Preparing live preview…")
            append_log(self.log_path, {
                "event": "whisper_ready",
                "load_seconds": round(result["load_seconds"], 3),
                "warmup_seconds": round(result["warmup_seconds"], 3),
                "startup_seconds": round(result["startup_seconds"], 3),
            })
            self.preview_worker.start()
        return False

    def _preview_whisper_ready(self, result, error):
        if not self.running:
            return False
        if error:
            self.remote_window.set_status("Listening… (preview unavailable)")
            append_log(self.log_path, {
                "event": "preview_whisper_error",
                "error": str(error),
            })
        else:
            self.remote_window.set_status("Listening…")
            append_log(self.log_path, {
                "event": "preview_whisper_ready",
                "load_seconds": round(result["load_seconds"], 3),
                "warmup_seconds": round(result["warmup_seconds"], 3),
                "startup_seconds": round(result["startup_seconds"], 3),
            })
        return False

    def _preview_audio(self, role, pcm_audio):
        if self.preview_pending:
            return

        def finished(text, error, elapsed):
            self.preview_pending = False
            if error:
                append_log(self.log_path, {
                    "event": "preview_error", "role": role, "error": str(error),
                })
            elif text:
                self._window(role).set_text(text)
            return False

        self.preview_pending = self.preview_worker.submit(
            pcm_audio,
            finished,
        )

    def _final_audio(self, role, pcm_audio, start, end, vad_details):
        self.interviewer_utterance_count += 1
        utterance = self.interviewer_utterance_count
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
            relative_trigger = None
            try:
                if state is not None:
                    with self.transcript_lock:
                        if state["questions"]:
                            marker = state["questions"][0]
                if marker is None:
                    text = transcribe_pcm(model, pcm_audio)
                    details = {}
                else:
                    relative_trigger = max(0, marker["trigger"] - start)
                    (
                        text,
                        boundary_bytes,
                        replay_start_bytes,
                        details,
                    ) = transcribe_question(
                        model,
                        pcm_audio,
                        relative_trigger,
                        silence_padding_ms=TRANSCRIPTION_PADDING_MS,
                        replay_vad_rms=VAD_RMS,
                    )
                    remainder = pcm_audio[replay_start_bytes:]
                    requeued = self.remote_audio.requeue_question_remainder(
                        remainder,
                        start + replay_start_bytes,
                        end,
                    )
                    details["requeued_audio_seconds"] = round(
                        len(remainder) / BYTES_PER_SECOND,
                        3,
                    ) if requeued else 0
            except Exception as error:
                if marker is not None and relative_trigger is not None:
                    fallback_boundary = min(relative_trigger, len(pcm_audio))
                    fallback_boundary -= fallback_boundary % SAMPLE_WIDTH
                    self.remote_audio.requeue_question_remainder(
                        pcm_audio[fallback_boundary:],
                        start + fallback_boundary,
                        end,
                    )
                append_log(self.log_path, {
                    "event": "utterance_error",
                    "role": role,
                    "utterance": utterance,
                    "error": str(error),
                })
                raise
            result = {
                "text": text,
                "elapsed": time.perf_counter() - started,
                "details": details,
            }
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
            return result

        def finished(result, error):
            if error:
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
            self._restart_preview_worker()
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
            if self.codex_enabled:
                self._request_codex_answer(
                    marker["question"],
                    result["text"],
                )
            else:
                self.answer_window.set_status(
                    "Codex disabled · question logged only"
                )
                append_log(self.log_path, {
                    "event": "codex_request_skipped",
                    "question": marker["question"],
                    "reason": "disabled_for_audio_test",
                })
        self._restart_preview_worker()

    def _restart_preview_worker(self):
        if self.running and self.preview_worker.start():
            append_log(self.log_path, {
                "event": "preview_whisper_restart",
            })

    def _request_codex_answer(self, question_number, question_text):
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
                    state["status"] = "superseded"
                    state["spoken"] = False
                    superseded.append({
                        "generation": old_generation,
                        "request": state["request"],
                        "question": state["question"],
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
        self.answer_window.set_status("Thinking…")
        if superseded:
            self.answer_window.set_text("")
        append_log(self.log_path, {
            "event": "codex_request",
            "request": request_number,
            "question": question_number,
            "generation": generation,
            "model": CODEX_MODEL,
            "reasoning_effort": CODEX_REASONING,
            "fast_mode": CODEX_FAST_MODE,
            "context_items": len(context),
            "superseded_requests": len(superseded),
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
            elif stage == "resumed":
                self.answer_window.set_status("Thinking… (retry 1/1)")
            elif stage == "failed":
                self.answer_window.set_status("Codex unavailable")
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
        self.question_count += 1
        question_number = self.question_count
        marker = self.remote_audio.capture_question_marker()
        preview_cancel_seconds = self.preview_worker.cancel()
        self.preview_pending = False
        marker["question"] = question_number
        marker["committed"] = False
        append_log(self.log_path, {
            "event": "f8_trigger",
            "question": question_number,
            "trigger_absolute_seconds": round(
                marker["trigger"] / BYTES_PER_SECOND, 3
            ),
            "utterance_state": marker["source"],
            "preview_cancel_seconds": round(preview_cancel_seconds, 3),
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
        return self.remote_window

    def back_to_chat(self, *_args):
        return self._stop("back")

    def shutdown(self, *_args):
        return self._stop("quit")

    def _stop(self, exit_action):
        if not self.running:
            return False
        self.running = False
        self.exit_action = exit_action
        self._save_window_state()
        self.remote_audio.stop()
        self.preview_worker.stop()
        self.worker.stop()
        if self.codex_worker is not None:
            self.codex_worker.stop()
        if self.trigger_socket is not None:
            self.trigger_socket.close()
        TRIGGER_SOCKET.unlink(missing_ok=True)
        append_log(self.log_path, {
            "event": "app_session_end",
            "exit_action": exit_action,
            "questions": self.question_count,
            "interviewer_utterances": self.interviewer_utterance_count,
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
        thread_id = choose_interview_session(store)
        if thread_id is None:
            return
        chat = PreparationChatDialog(thread_id)
        while True:
            response = chat.run_session()
            if response == CHAT_RESPONSE_BACK:
                chat.destroy()
                break
            if response != CHAT_RESPONSE_START_INTERVIEW:
                chat.destroy()
                return

            app = InterviewApp(thread_id)
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
