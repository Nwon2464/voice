"""Windows bridge audio backend used by the WSLg Interview Assistant."""

from __future__ import annotations

import threading

from windows_port.audio import SAMPLE_WIDTH_BYTES
from windows_port.bridge_client import WindowsBridgeClient


class WindowsBridgeAudioStream:
    """Give bridge PCM the same cursor/stop interface as the Pulse stream.

    The bridge reader invokes both PCM and hotkey callbacks serially.  Holding
    this lock through ``capture_sample_cursor_and`` keeps an F8/F9 snapshot
    ordered with respect to every accepted PCM chunk, rather than deferring the
    snapshot to the GTK event loop.
    """

    def __init__(
        self,
        role,
        worker,
        on_error,
        on_hotkey,
        on_status,
        *,
        client_factory=WindowsBridgeClient,
    ):
        self.role = role
        self.worker = worker
        self.on_error = on_error
        self.on_hotkey = on_hotkey
        self.on_status = on_status
        self.client_factory = client_factory
        self.client = None
        self.condition = threading.Condition()
        self.total_samples = 0
        self.stopped = threading.Event()

    def start(self):
        if self.client is not None:
            return
        self.stopped.clear()
        self.client = self.client_factory(
            self._on_pcm,
            self._on_status,
            self._on_error,
            on_hotkey=self._on_hotkey,
            capture_audio=True,
        )
        self.client.start()

    def abort(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        if self.client is not None:
            self.client.stop()

    def stop(self):
        self.abort()

    def capture_sample_cursor_and(self, enqueue):
        """Atomically bind a worker semantic request to bridge PCM ingress."""
        with self.condition:
            cursor = self.total_samples
            return cursor, enqueue(cursor)

    def capture_sample_cursor_and_state(self, enqueue):
        """Return the exact press-time cursor state with the snapshot result."""
        with self.condition:
            cursor = self.total_samples
            with self.worker.lock:
                state = {
                    "received_cursor": cursor,
                    "queued_cursor": self.worker.queued_sample_cursor,
                    "consumed_cursor": self.worker.consumed_sample_cursor,
                    "audio_drop_samples": self.worker.audio_drop_samples,
                }
            return cursor, enqueue(cursor), state

    def cursor_state(self):
        with self.condition, self.worker.lock:
            return {
                "received_cursor": self.total_samples,
                "queued_cursor": self.worker.queued_sample_cursor,
                "consumed_cursor": self.worker.consumed_sample_cursor,
                "audio_drop_samples": self.worker.audio_drop_samples,
            }

    def _on_pcm(self, pcm):
        if len(pcm) % SAMPLE_WIDTH_BYTES:
            self._on_error(ValueError("Windows bridge PCM has an incomplete s16le sample"))
            return
        try:
            with self.condition:
                if self.stopped.is_set():
                    return
                start_cursor = self.total_samples
                end_cursor = start_cursor + len(pcm) // SAMPLE_WIDTH_BYTES
                if not self.worker.submit_pcm(pcm, start_cursor, end_cursor):
                    raise RuntimeError("Moonshine worker is not accepting PCM")
                self.total_samples = end_cursor
                self.condition.notify_all()
        except Exception as error:
            self._on_error(error)

    def _on_hotkey(self, event):
        if not self.stopped.is_set():
            self.on_hotkey(event, self.capture_sample_cursor_and_state)

    def _on_status(self, status):
        self.on_status(status)

    def _on_error(self, error):
        if not self.stopped.is_set():
            self.on_error(self.role, error)
