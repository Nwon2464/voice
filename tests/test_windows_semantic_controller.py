import threading
import unittest

from windows_port.moonshine_probe import PcmCursorForwarder
from windows_port.semantic_controller import SemanticCommitController


class _Worker:
    def __init__(self):
        self.lock = threading.Lock()
        self.accepting = True
        self.queued_sample_cursor = 0
        self.consumed_sample_cursor = 0
        self.audio_drop_samples = 0
        self.max_backlog_samples = 0
        self.submitted = []
        self.snapshots = []

    def submit_pcm(self, pcm, start_cursor, end_cursor):
        with self.lock:
            if not self.accepting:
                return False
            self.submitted.append((start_cursor, end_cursor))
            self.queued_sample_cursor = end_cursor
            self.max_backlog_samples = max(
                self.max_backlog_samples,
                end_cursor - self.consumed_sample_cursor,
            )
        return True

    def request_snapshot(self, target_cursor, callback):
        with self.lock:
            if not self.accepting:
                return False
            if target_cursor != self.queued_sample_cursor:
                raise ValueError("target must equal queued cursor")
            self.snapshots.append((target_cursor, callback))
        return True

    def complete_snapshot(self, index, result=None, error=None):
        target, callback = self.snapshots[index]
        with self.lock:
            self.consumed_sample_cursor = target
        callback(result, error)


def _result(text, target, *, committed=True, barrier_ms=400.0):
    return {
        "committed": committed,
        "text": text,
        "target_sample_cursor": target,
        "consumed_sample_cursor": target,
        "cursor_complete": True,
        "barrier_wait_ms": barrier_ms,
        "force_update_ms": 12.0,
        "audio_drop_samples": 0,
        "max_backlog_ms": 900.0,
    }


def _event(key, sequence):
    return {"event": "hotkey", "key": key, "sequence": sequence, "timestamp_ns": sequence}


class WindowsSemanticControllerTests(unittest.TestCase):
    def setUp(self):
        self.worker = _Worker()
        self.forwarder = PcmCursorForwarder(self.worker)
        self.emitted = []
        self.controller = SemanticCommitController(
            self.worker, self.forwarder, emit=self.emitted.append
        )

    def _pcm(self, samples):
        self.assertTrue(self.forwarder.submit(bytes(samples * 2)))

    def _commit_f8(self, text="first question"):
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 1))
        self.worker.complete_snapshot(0, _result(text, 160))

    def test_f8_snapshots_received_cursor_and_requests_matching_barrier_target(self):
        self._pcm(160)
        with self.worker.lock:
            self.worker.consumed_sample_cursor = 80

        self.controller.on_hotkey(_event("F8", 1))

        self.assertEqual(self.worker.snapshots[0][0], 160)
        record = self.controller.summary()["semantic_events"][0]
        self.assertEqual(record["received_cursor_at_press"], 160)
        self.assertEqual(record["target_sample_cursor"], 160)
        self.assertEqual(record["consumed_cursor_at_event"], 80)
        self.assertEqual(record["backlog_samples_at_press"], 80)

    def test_f8_result_is_bounded_by_target_even_when_later_pcm_arrives(self):
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 1))
        self._pcm(160)  # Tail after F8 must belong to a later snapshot.

        self.worker.complete_snapshot(0, _result("question before F8", 160))

        record = self.controller.summary()["semantic_events"][0]
        self.assertEqual(record["question_text"], "question before F8")
        self.assertEqual(record["target_sample_cursor"], 160)
        self.assertEqual(self.forwarder.target_cursor(), 320)
        self.assertEqual(self.controller.question_count, 1)

    def test_valid_f8_commits_one_new_question_after_worker_barrier(self):
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 1))
        self.worker.complete_snapshot(0, _result("Why this role?", 160, barrier_ms=812.5))

        record = self.controller.summary()["semantic_events"][0]
        self.assertTrue(record["semantic_commit_accepted"])
        self.assertEqual(record["question_number"], 1)
        self.assertEqual(record["consumed_cursor_after_barrier"], 160)
        self.assertEqual(record["barrier_wait_ms"], 812.5)

    def test_empty_f8_is_rejected_without_consuming_question_number(self):
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 1))
        self.worker.complete_snapshot(0, _result("", 160, committed=False))

        record = self.controller.summary()["semantic_events"][0]
        self.assertEqual(record["semantic_rejection_reason"], "empty_question")
        self.assertEqual(self.controller.question_count, 0)

    def test_duplicate_f8_is_rejected_without_consuming_question_number(self):
        self._commit_f8()
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 2))
        self.worker.complete_snapshot(1, _result("", 320, committed=False))

        record = self.controller.summary()["semantic_events"][-1]
        self.assertEqual(record["semantic_rejection_reason"], "duplicate_f8")
        self.assertEqual(self.controller.question_count, 1)

    def test_f9_without_valid_f8_is_rejected_before_requesting_snapshot(self):
        self._pcm(160)
        self.controller.on_hotkey(_event("F9", 1))

        self.assertEqual(self.worker.snapshots, [])
        record = self.controller.summary()["semantic_events"][0]
        self.assertEqual(record["semantic_rejection_reason"], "no_valid_previous_question")

    def test_f9_continues_previous_question_without_creating_a_new_number(self):
        self._commit_f8("What experience do you")
        self._pcm(160)
        self.controller.on_hotkey(_event("F9", 2))
        self.worker.complete_snapshot(1, _result("have with Python?", 320))

        record = self.controller.summary()["semantic_events"][-1]
        self.assertTrue(record["semantic_commit_accepted"])
        self.assertEqual(record["question_number"], 1)
        self.assertEqual(record["question_text"], "What experience do you have with Python?")
        self.assertEqual(self.controller.question_count, 1)
        self.assertEqual(self.controller.last_question["commit_source"], "f9_continuation")

    def test_silence_segment_is_not_a_semantic_question_and_f8_uses_worker_accumulation(self):
        self.controller.on_silence_segment(
            {
                **_result("segment one", 160),
                "segment_preserved": True,
                "accumulated_segment_count": 1,
            },
            None,
        )
        self._pcm(160)
        self.controller.on_hotkey(_event("F8", 1))
        self.worker.complete_snapshot(0, _result("segment one active two", 160))

        summary = self.controller.summary()
        self.assertEqual(summary["semantic_question_count"], 1)
        self.assertEqual(summary["semantic_events"][0]["event_type"], "silence")
        self.assertEqual(summary["semantic_events"][1]["question_text"], "segment one active two")

    def test_hotkey_event_order_is_preserved_and_audio_cursor_is_contiguous(self):
        self._pcm(160)
        self.controller.on_hotkey(_event("F9", 1))
        self.controller.on_hotkey(_event("F8", 2))
        self.worker.complete_snapshot(0, _result("question", 160))

        summary = self.controller.summary()
        hotkeys = [event for event in summary["semantic_events"] if event["event_type"] == "hotkey"]
        self.assertEqual([event["hotkey_sequence"] for event in hotkeys], [1, 2])
        self.assertEqual(self.worker.submitted, [(0, 160)])
        self.assertTrue(self.controller.wait_for_pending(0))
