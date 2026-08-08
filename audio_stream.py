"""Platform-neutral PCM speech segmentation for Interview Assistant."""

import math
import threading
from collections import deque

import numpy as np

from audio_utils import BYTES_PER_SECOND, POST_CONTEXT_MS, SAMPLE_WIDTH


class PcmSpeechSegmenter:
    """Turn a continuous 16 kHz mono s16le stream into speech utterances."""

    def __init__(
        self,
        on_preview,
        on_utterance,
        vad_rms=250,
        preview_interval_ms=1_000,
        preview_window_seconds=12,
        silence_end_ms=1_000,
        pre_roll_ms=300,
        max_utterance_seconds=60,
        history_seconds=180,
    ):
        self.on_preview = on_preview
        self.on_utterance = on_utterance
        self.vad_rms = vad_rms
        self.preview_interval_ms = preview_interval_ms
        self.preview_window_seconds = preview_window_seconds
        self.silence_end_ms = silence_end_ms
        self.pre_roll_ms = pre_roll_ms
        self.max_utterance_seconds = max_utterance_seconds
        self.history_seconds = history_seconds
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

    def feed(self, data):
        """Consume complete int16 samples and synchronously emit callbacks."""
        if not data:
            return
        if len(data) % SAMPLE_WIDTH:
            data = data[: len(data) - len(data) % SAMPLE_WIDTH]
        if not data:
            return

        chunk_ms = len(data) * 1000 / BYTES_PER_SECOND
        loud = self._rms(data) >= self.vad_rms
        preview = None
        final = None
        with self.condition:
            self._append_history(data)
            self._append_pre_roll(data)
            if not self.active:
                self.speech_run_ms = self.speech_run_ms + chunk_ms if loud else 0
                if self.speech_run_ms >= 50:
                    self._start_utterance()
                self.condition.notify_all()
                return

            self.utterance.extend(data)
            self.silence_ms = 0 if loud else self.silence_ms + chunk_ms
            utterance_bytes = len(self.utterance)
            should_preview = (
                utterance_bytes >= BYTES_PER_SECOND * 0.6
                and utterance_bytes - self.last_preview_bytes
                >= BYTES_PER_SECOND * self.preview_interval_ms / 1000
            )
            should_finish = (
                self.silence_ms >= self.silence_end_ms
                or (
                    self.question_finalize_at is not None
                    and self.total_bytes >= self.question_finalize_at
                )
                or utterance_bytes
                >= BYTES_PER_SECOND * self.max_utterance_seconds
            )
            if should_preview:
                preview_size = BYTES_PER_SECOND * self.preview_window_seconds
                preview = bytes(self.utterance[-preview_size:])
                self.last_preview_bytes = utterance_bytes
            if should_finish:
                final = (
                    bytes(self.utterance),
                    self.utterance_start,
                    self.utterance_start + len(self.utterance),
                    {
                        "vad_method": "rms",
                        "vad_threshold_rms": self.vad_rms,
                    },
                )
                self.last_completed_span = (final[1], final[2])
                self.active = False
                self.utterance.clear()
                self.speech_run_ms = 0
                self.silence_ms = 0
                self.last_preview_bytes = 0
                self.question_finalize_at = None
            self.condition.notify_all()

        if preview is not None:
            self.on_preview(preview)
        if final is not None:
            self.on_utterance(*final)

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
        limit = int(BYTES_PER_SECOND * self.pre_roll_ms / 1000)
        while self.pre_roll and self.pre_roll_bytes > limit:
            removed = self.pre_roll.popleft()
            self.pre_roll_bytes -= len(removed)

    def _append_history(self, data):
        self.history.extend(data)
        self.total_bytes += len(data)
        limit = BYTES_PER_SECOND * self.history_seconds
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
        self.last_preview_bytes = 0
