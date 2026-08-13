import threading
import time
import unittest

from windows_port.moonshine_probe import (
    PcmCursorForwarder,
    drain_worker,
    finalize_drain,
    parse_args,
    stop_bridge_and_drain,
)


class _Worker:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.queued_sample_cursor = 0
        self.consumed_sample_cursor = 0
        self.audio_drop_samples = 0
        self.max_backlog_samples = 0
        self.accepting = True

    def submit_pcm(self, pcm, start_cursor, end_cursor):
        self.calls.append((pcm, start_cursor, end_cursor))
        if not self.accepting:
            return False
        self.queued_sample_cursor = end_cursor
        self.consumed_sample_cursor = end_cursor
        self.max_backlog_samples = max(self.max_backlog_samples, len(pcm) // 2)
        return True


class WindowsMoonshineProbeTests(unittest.TestCase):
    def test_forwarder_assigns_contiguous_cursors_from_zero(self):
        worker = _Worker()
        forwarder = PcmCursorForwarder(worker)

        self.assertTrue(forwarder.submit(bytes(320)))
        self.assertTrue(forwarder.submit(bytes(640)))

        self.assertEqual(
            worker.calls,
            [(bytes(320), 0, 160), (bytes(640), 160, 480)],
        )
        self.assertEqual(
            forwarder.diagnostics(),
            {
                "received_sample_cursor": 480,
                "queued_sample_cursor": 480,
                "consumed_sample_cursor": 480,
                "audio_drop_samples": 0,
                "max_backlog_samples": 320,
                "max_backlog_ms": 20.0,
                "pcm_bytes_forwarded": 960,
            },
        )

    def test_rejected_pcm_does_not_advance_the_cursor(self):
        worker = _Worker()
        worker.accepting = False
        forwarder = PcmCursorForwarder(worker)

        self.assertFalse(forwarder.submit(bytes(320)))
        self.assertEqual(forwarder.diagnostics()["received_sample_cursor"], 0)
        self.assertEqual(forwarder.diagnostics()["pcm_bytes_forwarded"], 0)

    def test_incomplete_s16le_pcm_is_rejected_before_worker_submission(self):
        worker = _Worker()
        forwarder = PcmCursorForwarder(worker)

        with self.assertRaisesRegex(ValueError, "complete s16le"):
            forwarder.submit(b"\x00")
        self.assertEqual(worker.calls, [])

    def test_language_selection_is_limited_to_existing_worker_languages(self):
        self.assertEqual(parse_args(["--language", "en"]).language, "en")
        self.assertEqual(parse_args(["--language", "ja"]).language, "ja")

    def test_drain_waits_for_worker_to_consume_the_final_cursor(self):
        worker = _Worker()
        worker.queued_sample_cursor = 320
        worker.consumed_sample_cursor = 0

        def consume_later():
            time.sleep(0.01)
            with worker.lock:
                worker.consumed_sample_cursor = 320

        thread = threading.Thread(target=consume_later)
        thread.start()
        result = drain_worker(worker, 320, timeout=0.2, poll_interval=0.001)
        thread.join()

        self.assertTrue(result["drain_deadline_met"])
        self.assertFalse(result["drain_timeout"])
        self.assertEqual(result["drain_target_sample_cursor"], 320)
        self.assertEqual(
            result["drain_consumed_at_deadline_sample_cursor"], 320
        )

    def test_drain_timeout_reports_unconsumed_tail(self):
        worker = _Worker()
        worker.queued_sample_cursor = 320
        worker.consumed_sample_cursor = 160

        result = drain_worker(worker, 320, timeout=0.01, poll_interval=0.001)

        self.assertFalse(result["drain_deadline_met"])
        self.assertTrue(result["drain_timeout"])
        self.assertEqual(result["drain_target_sample_cursor"], 320)
        self.assertEqual(
            result["drain_consumed_at_deadline_sample_cursor"], 160
        )

    def test_bridge_is_stopped_before_the_drain_target_is_frozen(self):
        worker = _Worker()
        forwarder = PcmCursorForwarder(worker)

        class _Bridge:
            def stop(self):
                # Simulate the final frame dispatched while bridge.stop() joins
                # the reader thread.
                forwarder.submit(bytes(320))

        result = stop_bridge_and_drain(_Bridge(), forwarder, worker, timeout=0.1)

        self.assertTrue(result["drain_deadline_met"])
        self.assertEqual(result["drain_target_sample_cursor"], 160)

    def test_timeout_caught_up_during_shutdown_is_distinct_from_audio_loss(self):
        drain = {
            "drain_deadline_met": False,
            "drain_timeout": True,
            "drain_reason": "timed out after 10 seconds",
            "drain_target_sample_cursor": 320,
            "drain_consumed_at_deadline_sample_cursor": 160,
            "drain_seconds": 10.0,
            "_drain_started_at": 100.0,
        }

        result = finalize_drain(drain, 320, finished_at=102.5)

        self.assertEqual(
            result["drain_status"], "timed_out_but_completed_during_shutdown"
        )
        self.assertTrue(result["completed_after_shutdown"])
        self.assertEqual(result["unprocessed_after_shutdown_samples"], 0)
        self.assertEqual(result["final_catch_up_seconds"], 2.5)
        self.assertEqual(
            result["drain_consumed_at_deadline_sample_cursor"], 160
        )
        self.assertEqual(result["final_consumed_after_shutdown_sample_cursor"], 320)

    def test_incomplete_after_shutdown_reports_the_unprocessed_tail(self):
        drain = {
            "drain_deadline_met": False,
            "drain_timeout": True,
            "drain_reason": "timed out after 10 seconds",
            "drain_target_sample_cursor": 320,
            "drain_consumed_at_deadline_sample_cursor": 160,
            "drain_seconds": 10.0,
            "_drain_started_at": 100.0,
        }

        result = finalize_drain(drain, 240, finished_at=103.0)

        self.assertEqual(result["drain_status"], "incomplete_after_shutdown")
        self.assertFalse(result["completed_after_shutdown"])
        self.assertEqual(result["unprocessed_after_shutdown_samples"], 80)

    def test_completion_within_timeout_remains_distinct_after_shutdown(self):
        drain = {
            "drain_deadline_met": True,
            "drain_timeout": False,
            "drain_reason": None,
            "drain_target_sample_cursor": 320,
            "drain_consumed_at_deadline_sample_cursor": 320,
            "drain_seconds": 0.2,
            "_drain_started_at": 100.0,
        }

        result = finalize_drain(drain, 320, finished_at=100.2)

        self.assertEqual(result["drain_status"], "completed_within_timeout")


if __name__ == "__main__":
    unittest.main()
