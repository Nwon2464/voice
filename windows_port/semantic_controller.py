"""Standalone adapter for the existing Moonshine F8/F9 semantic rules."""

from __future__ import annotations

import threading
import time

from windows_port.audio import SAMPLE_RATE


class SemanticCommitController:
    """Apply main's F8-new-question/F9-continuation semantics without UI or Codex."""

    def __init__(self, worker, pcm_forwarder, *, emit=lambda _record: None):
        self.worker = worker
        self.pcm_forwarder = pcm_forwarder
        self.emit = emit
        self.lock = threading.Lock()
        self.events = []
        self.question_count = 0
        self.last_question = None
        self.pending_snapshots = 0
        self.pending_done = threading.Event()
        self.pending_done.set()

    def on_hotkey(self, event: dict) -> None:
        """Capture the bridge cursor immediately and request the worker barrier."""
        key = event["key"]
        if key == "F8":
            self._request_snapshot(event, "f8", base=None)
            return
        if key == "F9":
            with self.lock:
                base = None if self.last_question is None else dict(self.last_question)
            if not self._valid_base(base):
                record = self._record_hotkey(event, self.pcm_forwarder.cursor_state())
                with self.lock:
                    self.events.append(record)
                self._finish(record, rejection_reason="no_valid_previous_question")
                return
            self._request_snapshot(event, "f9_continuation", base=base)
            return
        raise ValueError(f"unsupported semantic hotkey: {key!r}")

    def on_silence_segment(self, result, error) -> None:
        """Observe native stream reset/accumulation without semantic question commit."""
        record = {
            "event_type": "silence",
            "semantic_commit_accepted": False,
            "semantic_rejection_reason": None,
            "segment_text": "" if result is None else result.get("text", "").strip(),
            "segment_preserved": False if result is None else result.get("segment_preserved", False),
            "accumulated_segment_count": 0 if result is None else result.get("accumulated_segment_count", 0),
            "error": None if error is None else str(error),
        }
        if result is not None:
            record.update(self._worker_diagnostics(result))
        with self.lock:
            self.events.append(record)
        self.emit(record)

    def wait_for_pending(self, timeout: float) -> bool:
        return self.pending_done.wait(timeout)

    def summary(self) -> dict:
        with self.lock:
            hotkeys = [record for record in self.events if record.get("event_type") == "hotkey"]
            accepted = [record for record in hotkeys if record.get("semantic_commit_accepted")]
            rejected = [record for record in hotkeys if record.get("semantic_rejection_reason")]
            return {
                "f8_count": sum(record["hotkey_key"] == "F8" for record in hotkeys),
                "f9_count": sum(record["hotkey_key"] == "F9" for record in hotkeys),
                "valid_semantic_commit_count": len(accepted),
                "rejected_semantic_commit_count": len(rejected),
                "semantic_question_count": self.question_count,
                "pending_semantic_snapshots": self.pending_snapshots,
                "max_barrier_ms": round(max((record.get("barrier_wait_ms", 0.0) for record in hotkeys), default=0.0), 1),
                "semantic_events": [dict(record) for record in self.events],
            }

    def _request_snapshot(self, event, source, base) -> None:
        started = time.perf_counter()
        record = self._record_hotkey(event, self.pcm_forwarder.cursor_state())
        with self.lock:
            self.events.append(record)
            # Count before request_snapshot so a synchronous test double cannot
            # complete its callback before the pending state has been recorded.
            self.pending_snapshots += 1
            self.pending_done.clear()

        def callback(result, error):
            self._snapshot_ready(record, source, base, started, result, error)

        try:
            target_cursor, accepted = self.pcm_forwarder.capture_sample_cursor_and(
                lambda cursor: self.worker.request_snapshot(cursor, callback)
            )
            with self.lock:
                record["target_sample_cursor"] = target_cursor
                record["received_cursor_at_press"] = target_cursor
        except Exception as error:
            self._finish(record, rejection_reason="snapshot_error", error=error)
            return
        if not accepted:
            self._finish(record, rejection_reason="worker_rejected_snapshot")

    def _record_hotkey(self, event, state):
        received_cursor = state["received_cursor"]
        consumed_cursor = state["consumed_cursor"]
        return {
            "event_type": "hotkey",
            "hotkey_key": event["key"],
            "hotkey_sequence": event["sequence"],
            "hotkey_timestamp_ns": event["timestamp_ns"],
            "received_cursor_at_press": received_cursor,
            "target_sample_cursor": None,
            "consumed_cursor_at_event": consumed_cursor,
            "queued_cursor_at_event": state["queued_cursor"],
            "backlog_samples_at_press": max(received_cursor - consumed_cursor, 0),
            "backlog_ms_at_press": round(
                max(received_cursor - consumed_cursor, 0) / SAMPLE_RATE * 1000, 1
            ),
            "audio_drop_samples": state["audio_drop_samples"],
            "semantic_commit_accepted": False,
            "semantic_rejection_reason": None,
        }

    def _snapshot_ready(self, record, source, base, started, result, error) -> None:
        if error is not None:
            self._finish(record, rejection_reason="worker_error", error=error)
            return
        if result is None or not result.get("committed", True):
            self._finish(record, rejection_reason=self._empty_or_duplicate_reason(source))
            return
        text = result["text"].strip()
        if not text:
            self._finish(record, rejection_reason="empty_question")
            return
        if source == "f8":
            with self.lock:
                self.question_count += 1
                self.last_question = {
                    "commit_source": "f8",
                    "text": text,
                    "question_number": self.question_count,
                    "target_sample_cursor": result["target_sample_cursor"],
                }
                question_number = self.question_count
            self._finish(
                record,
                accepted=True,
                question_number=question_number,
                question_text=text,
                result=result,
                started=started,
            )
            return

        with self.lock:
            current = None if self.last_question is None else dict(self.last_question)
        if not self._valid_base(base) or current != base:
            self._finish(record, rejection_reason="previous_question_changed", result=result, started=started)
            return
        if result["target_sample_cursor"] <= base["target_sample_cursor"]:
            self._finish(record, rejection_reason="cursor_not_newer", result=result, started=started)
            return
        combined = base["text"].rstrip() + " " + text.lstrip()
        with self.lock:
            self.last_question = {
                "commit_source": "f9_continuation",
                "text": combined,
                "question_number": base["question_number"],
                "target_sample_cursor": result["target_sample_cursor"],
            }
        self._finish(
            record,
            accepted=True,
            question_number=base["question_number"],
            question_text=combined,
            continuation_segment_text=text,
            result=result,
            started=started,
        )

    def _finish(
        self,
        record,
        *,
        accepted=False,
        rejection_reason=None,
        error=None,
        question_number=None,
        question_text=None,
        continuation_segment_text=None,
        result=None,
        started=None,
    ) -> None:
        with self.lock:
            record["semantic_commit_accepted"] = accepted
            record["semantic_rejection_reason"] = rejection_reason
            record["error"] = None if error is None else str(error)
            if result is not None:
                record.update(self._worker_diagnostics(result))
            if started is not None:
                record["semantic_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if question_number is not None:
                record["question_number"] = question_number
            if question_text is not None:
                record["question_text"] = question_text
            if continuation_segment_text is not None:
                record["continuation_segment_text"] = continuation_segment_text
            if record.get("target_sample_cursor") is None and result is not None:
                record["target_sample_cursor"] = result["target_sample_cursor"]
            if self.pending_snapshots:
                self.pending_snapshots -= 1
                if not self.pending_snapshots:
                    self.pending_done.set()
        self.emit(record)

    @staticmethod
    def _worker_diagnostics(result):
        return {
            "target_sample_cursor": result["target_sample_cursor"],
            "consumed_cursor_after_barrier": result["consumed_sample_cursor"],
            "cursor_complete": result["cursor_complete"],
            "barrier_wait_ms": result["barrier_wait_ms"],
            "force_update_ms": result["force_update_ms"],
            "audio_drop_samples": result["audio_drop_samples"],
            "max_backlog_ms": result["max_backlog_ms"],
        }

    def _empty_or_duplicate_reason(self, source):
        with self.lock:
            if source == "f8" and self.last_question is not None:
                return "duplicate_f8"
        if source == "f9_continuation":
            return "empty_continuation"
        return "empty_question"

    @staticmethod
    def _valid_base(base):
        return bool(
            base
            and base.get("text")
            and base.get("commit_source") in {"f8", "f9_continuation"}
            and isinstance(base.get("question_number"), int)
            and isinstance(base.get("target_sample_cursor"), int)
        )
