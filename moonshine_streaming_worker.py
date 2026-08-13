"""Persistent Moonshine Small streaming worker for the live GNOME app."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable

import numpy as np


SAMPLE_RATE = 16_000
UPDATE_INTERVAL_SECONDS = 0.5
STOP_JOIN_TIMEOUT_SECONDS = 5.0
MOONSHINE_MODEL_BY_LANGUAGE = {
    "en": ("small-streaming-en", "SMALL_STREAMING"),
    "ja": ("base-ja", "BASE"),
}
AUTO_SILENCE_MS = int(os.environ.get("INTERVIEW_AUTO_SILENCE_MS", "1500"))
AUTO_SILENCE_RMS_THRESHOLD = int(
    os.environ.get("INTERVIEW_AUTO_SILENCE_RMS_THRESHOLD", "250")
)


def transcript_lines_snapshot(transcript) -> list[dict]:
    """Copy Moonshine transcript lines before the native snapshot is released."""
    return [
        {
            "line_id": int(line.line_id),
            "text": line.text.strip(),
            "start_seconds": round(float(line.start_time), 4),
            "duration_seconds": round(float(line.duration), 4),
            "is_complete": bool(line.is_complete),
        }
        for line in (transcript.lines if transcript is not None else [])
        if line.text.strip()
    ]


def lines_display_text(lines: list[dict]) -> str:
    return "\n".join(line["text"] for line in lines if line["text"])


def lines_question_text(lines: list[dict]) -> str:
    return " ".join(line["text"] for line in lines if line["text"])


class MoonshineStreamingWorker:
    """Own one model/stream and serialize PCM, Preview and F8 operations."""

    def __init__(
        self,
        on_ready: Callable,
        on_preview: Callable,
        on_error: Callable,
        *,
        on_auto_commit: Callable | None = None,
        dispatch: Callable = lambda callback, *args: callback(*args),
        engine_factory: Callable | None = None,
        force_update_flag: int | None = None,
        language: str = "en",
        auto_silence_ms: int = AUTO_SILENCE_MS,
        auto_silence_rms_threshold: int = AUTO_SILENCE_RMS_THRESHOLD,
    ):
        self.on_ready = on_ready
        self.on_preview = on_preview
        self.on_error = on_error
        self.on_auto_commit = on_auto_commit
        self.dispatch = dispatch
        self.engine_factory = engine_factory
        self.force_update_flag = force_update_flag
        self.language = language
        if language not in MOONSHINE_MODEL_BY_LANGUAGE:
            raise ValueError(f"unsupported Moonshine language: {language}")
        self.auto_silence_samples = int(SAMPLE_RATE * auto_silence_ms / 1000)
        self.auto_silence_rms_threshold = auto_silence_rms_threshold
        if self.auto_silence_samples <= 0:
            raise ValueError("auto_silence_ms must be positive")
        if self.auto_silence_rms_threshold < 0:
            raise ValueError("auto_silence_rms_threshold cannot be negative")
        self.jobs: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.thread = None
        self.accepting = False
        self.queued_sample_cursor = 0
        self.consumed_sample_cursor = 0
        self.audio_drop_samples = 0
        self.max_backlog_samples = 0
        self.last_committed_sample_cursor = None
        self.pending_f8_requests = 0

    def start(self) -> bool:
        with self.lock:
            if self.thread is not None or self.accepting:
                return False
            self.accepting = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return True

    def submit_pcm(
        self,
        pcm_audio: bytes,
        start_sample_cursor: int,
        end_sample_cursor: int,
    ) -> bool:
        if not pcm_audio:
            return True
        if len(pcm_audio) % 2:
            raise ValueError("PCM chunk must contain complete s16le samples")
        sample_count = len(pcm_audio) // 2
        if end_sample_cursor - start_sample_cursor != sample_count:
            raise ValueError("PCM chunk length does not match its cursor span")
        with self.lock:
            if not self.accepting:
                return False
            if start_sample_cursor != self.queued_sample_cursor:
                if start_sample_cursor > self.queued_sample_cursor:
                    self.audio_drop_samples += (
                        start_sample_cursor - self.queued_sample_cursor
                    )
                raise ValueError(
                    "non-contiguous Moonshine PCM cursor: "
                    f"expected {self.queued_sample_cursor}, got {start_sample_cursor}"
                )
            self.queued_sample_cursor = end_sample_cursor
            self.max_backlog_samples = max(
                self.max_backlog_samples,
                self.queued_sample_cursor - self.consumed_sample_cursor,
            )
            self.jobs.put(("pcm", pcm_audio, start_sample_cursor, end_sample_cursor))
        return True

    def request_snapshot(
        self,
        target_sample_cursor: int,
        callback: Callable,
    ) -> bool:
        """Enqueue F8 after PCM through target was atomically queued by capture."""
        with self.lock:
            if not self.accepting:
                return False
            if target_sample_cursor != self.queued_sample_cursor:
                raise ValueError(
                    "F8 target must equal the queued absolute audio cursor: "
                    f"target={target_sample_cursor}, queued={self.queued_sample_cursor}"
                )
            request = {
                "target_sample_cursor": target_sample_cursor,
                "queued_sample_cursor": self.queued_sample_cursor,
                "audio_drop_samples": self.audio_drop_samples,
                "max_backlog_samples": self.max_backlog_samples,
                "requested_at": time.perf_counter(),
                "callback": callback,
            }
            self.audio_drop_samples = 0
            self.max_backlog_samples = 0
            self.pending_f8_requests += 1
            self.jobs.put(("snapshot", request))
        return True

    def stop(self) -> bool:
        """Stop after queued PCM; report whether the worker actually exited."""
        with self.lock:
            if not self.accepting and self.thread is None:
                return True
            self.accepting = False
            thread = self.thread
            self.jobs.put(("stop",))
        if thread is not None:
            thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                self.dispatch(
                    self.on_error,
                    RuntimeError(
                        "Moonshine worker did not stop within "
                        f"{STOP_JOIN_TIMEOUT_SECONDS:g} seconds"
                    ),
                )
                return False
        with self.lock:
            self.thread = None
        return True

    def _default_engine_factory(self):
        from moonshine_voice import ModelArch, Transcriber, get_model_for_language
        from moonshine_voice.transcriber import MOONSHINE_FLAG_FORCE_UPDATE

        _model_name, arch_name = MOONSHINE_MODEL_BY_LANGUAGE[self.language]
        arch = getattr(ModelArch, arch_name)
        model_path, returned_arch = get_model_for_language(self.language, arch)
        if returned_arch != arch:
            raise RuntimeError(
                f"requested {arch}, model downloader returned {returned_arch}"
            )
        options = {"return_audio_data": "false"}
        if self.language == "ja":
            options["max_tokens_per_second"] = "13.0"
        transcriber = Transcriber(
            model_path=model_path,
            model_arch=arch,
            update_interval=UPDATE_INTERVAL_SECONDS,
            options=options,
        )
        return transcriber, MOONSHINE_FLAG_FORCE_UPDATE

    def _new_stream(self, transcriber, listener):
        stream = transcriber.create_stream()
        stream.add_listener(listener)
        stream.start()
        return stream

    def _run(self) -> None:
        transcriber = None
        stream = None
        listener = None
        preview_lines = {}
        ready_dispatched = False
        active_request = None
        speech_seen_since_commit = False
        silence_samples = 0

        worker = self

        try:
            load_started = time.perf_counter()
            if self.engine_factory is None:
                transcriber, default_force_flag = self._default_engine_factory()
            else:
                transcriber = self.engine_factory()
                default_force_flag = 1
            force_flag = (
                self.force_update_flag
                if self.force_update_flag is not None
                else default_force_flag
            )

            from moonshine_voice import TranscriptEventListener

            class Listener(TranscriptEventListener):
                def _update(self, line):
                    copied = {
                        "line_id": int(line.line_id),
                        "text": line.text.strip(),
                        "start_seconds": round(float(line.start_time), 4),
                        "duration_seconds": round(float(line.duration), 4),
                        "is_complete": bool(line.is_complete),
                    }
                    if copied["text"]:
                        preview_lines[copied["line_id"]] = copied
                    else:
                        preview_lines.pop(copied["line_id"], None)
                    lines = sorted(
                        preview_lines.values(),
                        key=lambda item: (
                            item["start_seconds"], item["line_id"]
                        ),
                    )
                    worker.dispatch(worker.on_preview, {
                        "text": lines_display_text(lines),
                        "lines": lines,
                        "consumed_sample_cursor": worker.consumed_sample_cursor,
                    })

                def on_line_started(self, event):
                    self._update(event.line)

                def on_line_updated(self, event):
                    self._update(event.line)

                def on_line_text_changed(self, event):
                    self._update(event.line)

                def on_line_completed(self, event):
                    self._update(event.line)

                def on_error(self, event):
                    worker.dispatch(worker.on_error, event.error)

            listener = Listener()
            stream = self._new_stream(transcriber, listener)

            def snapshot_result(
                request,
                *,
                committed,
                barrier_wait_ms,
                force_update_ms=0.0,
            ):
                target = request["target_sample_cursor"]
                return {
                    "text": "",
                    "display_text": "",
                    "lines": [],
                    "commit_source": request["commit_source"],
                    "committed": committed,
                    "duplicate_suppressed": not committed,
                    "commit_requested_at": request["requested_at"],
                    "captured_sample_cursor": target,
                    "target_sample_cursor": target,
                    "queued_sample_cursor": request["queued_sample_cursor"],
                    "consumed_sample_cursor": self.consumed_sample_cursor,
                    "cursor_complete": self.consumed_sample_cursor == target,
                    "audio_drop_samples": request["audio_drop_samples"],
                    "max_backlog_ms": round(
                        request["max_backlog_samples"] / SAMPLE_RATE * 1000,
                        1,
                    ),
                    "barrier_wait_ms": round(barrier_wait_ms, 1),
                    "force_update_ms": round(force_update_ms, 1),
                }

            accumulated_segments = []

            def commit_snapshot(request):
                """Snapshot/reset a silence segment or commit all segments manually."""
                nonlocal stream, speech_seen_since_commit, silence_samples
                target = request["target_sample_cursor"]
                if self.consumed_sample_cursor != target:
                    raise RuntimeError(
                        "Moonshine cursor barrier failed: "
                        f"consumed={self.consumed_sample_cursor}, target={target}"
                    )

                if (
                    request["commit_source"] != "silence"
                    and not accumulated_segments
                    and self.last_committed_sample_cursor is not None
                    and not speech_seen_since_commit
                ):
                    result = snapshot_result(
                        request,
                        committed=False,
                        barrier_wait_ms=(
                            time.perf_counter() - request["requested_at"]
                        ) * 1000,
                    )
                    self.dispatch(request["callback"], result, None)
                    return

                force_started = time.perf_counter()
                transcript = stream.update_transcription(force_flag)
                force_done = time.perf_counter()
                lines = transcript_lines_snapshot(transcript)
                current_text = lines_question_text(lines)
                current_display_text = lines_display_text(lines)
                if request["commit_source"] == "silence":
                    if current_text:
                        accumulated_segments.append({
                            "text": current_text,
                            "display_text": current_display_text,
                            "lines": lines,
                        })
                    committed_lines = lines
                    committed_text = current_text
                    committed_display_text = current_display_text
                else:
                    committed_lines = [
                        line
                        for segment in accumulated_segments
                        for line in segment["lines"]
                    ] + lines
                    committed_text = " ".join(
                        text
                        for text in (
                            *(
                                segment["text"]
                                for segment in accumulated_segments
                            ),
                            current_text,
                        )
                        if text
                    )
                    committed_display_text = "\n".join(
                        text
                        for text in (
                            *(
                                segment["display_text"]
                                for segment in accumulated_segments
                            ),
                            current_display_text,
                        )
                        if text
                    )
                committed = bool(committed_text)
                result = snapshot_result(
                    request,
                    committed=committed,
                    barrier_wait_ms=(
                        force_started - request["requested_at"]
                    ) * 1000,
                    force_update_ms=(force_done - force_started) * 1000,
                )
                result.update({
                    "text": committed_text,
                    "display_text": committed_display_text,
                    "lines": committed_lines,
                    "accumulated_segment_count": len(accumulated_segments),
                    "segment_preserved": (
                        request["commit_source"] == "silence"
                        and bool(current_text)
                    ),
                })
                if request["commit_source"] != "silence" and committed:
                    self.last_committed_sample_cursor = target
                    accumulated_segments.clear()
                speech_seen_since_commit = False
                silence_samples = 0
                self.dispatch(request["callback"], result, None)

                stream.remove_listener(listener)
                stream.close()
                preview_lines.clear()
                stream = self._new_stream(transcriber, listener)

            model_name, _arch_name = MOONSHINE_MODEL_BY_LANGUAGE[self.language]
            self.dispatch(self.on_ready, {
                "model": model_name,
                "language": self.language,
                "load_seconds": time.perf_counter() - load_started,
                "update_interval_ms": round(UPDATE_INTERVAL_SECONDS * 1000),
            }, None)
            ready_dispatched = True

            while True:
                job = self.jobs.get()
                if job[0] == "stop":
                    return
                if job[0] == "pcm":
                    _, pcm_audio, _start_cursor, end_cursor = job
                    pcm_samples = np.frombuffer(pcm_audio, dtype="<i2")
                    rms = float(np.sqrt(np.mean(
                        pcm_samples.astype(np.float32) ** 2
                    )))
                    audio = pcm_samples.astype(np.float32)
                    audio /= 32768.0
                    stream.add_audio(audio.tolist(), SAMPLE_RATE)
                    with self.lock:
                        self.consumed_sample_cursor = end_cursor

                    if rms >= self.auto_silence_rms_threshold:
                        speech_seen_since_commit = True
                        silence_samples = 0
                    elif speech_seen_since_commit:
                        silence_samples += len(pcm_samples)

                    with self.lock:
                        f8_pending = self.pending_f8_requests > 0
                    if (
                        self.on_auto_commit is not None
                        and speech_seen_since_commit
                        and silence_samples >= self.auto_silence_samples
                        and not f8_pending
                    ):
                        with self.lock:
                            request = {
                                "target_sample_cursor": end_cursor,
                                "queued_sample_cursor": end_cursor,
                                "audio_drop_samples": self.audio_drop_samples,
                                "max_backlog_samples": self.max_backlog_samples,
                                "requested_at": time.perf_counter(),
                                "callback": self.on_auto_commit,
                                "commit_source": "silence",
                            }
                            self.audio_drop_samples = 0
                            self.max_backlog_samples = 0
                        active_request = request
                        commit_snapshot(request)
                        active_request = None
                    continue

                _, request = job
                with self.lock:
                    self.pending_f8_requests -= 1
                active_request = request
                request["commit_source"] = "f8"
                commit_snapshot(request)
                active_request = None
        except Exception as error:
            with self.lock:
                self.accepting = False
            self.dispatch(self.on_error, error)
            if not ready_dispatched:
                self.dispatch(self.on_ready, None, error)
            if active_request is not None:
                self.dispatch(active_request["callback"], None, error)
            while True:
                try:
                    pending = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if pending[0] == "snapshot":
                    self.dispatch(pending[1]["callback"], None, error)
        finally:
            if stream is not None:
                if listener is not None:
                    stream.remove_listener(listener)
                stream.close()
            if transcriber is not None:
                transcriber.close()
