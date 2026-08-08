import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
if HAS_FASTER_WHISPER:
    import interview_app


class _Segment:
    text = "final utterance before shutdown"
    words = []


class _FakeInfo:
    language = "en"


class _FakeWhisperModel:
    def __init__(self, *_args, **_kwargs):
        pass

    def transcribe(self, *_args, **_kwargs):
        return iter([_Segment()]), _FakeInfo()


class _FakeRemoteAudio:
    def __init__(self):
        self.requeued = []

    def requeue_question_remainder(
        self,
        pcm_remainder,
        remainder_start,
        source_end,
    ):
        self.requeued.append((pcm_remainder, remainder_start, source_end))
        return True


class _LatestOnlyCodexClient:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.prompts = []
        self.q1_started = threading.Event()
        self.release_q1 = threading.Event()
        self.interrupt_calls = 0
        self.clear_calls = 0
        self.__class__.instances.append(self)

    def start(self, **_kwargs):
        return {"thread_id": "thread-test", "startup_seconds": 0}

    def run_turn(self, prompt, on_delta=None, **_kwargs):
        self.prompts.append(prompt)
        if prompt == "Q1":
            self.q1_started.set()
            if not self.release_q1.wait(timeout=2):
                raise RuntimeError("test timed out waiting to interrupt Q1")
            raise RuntimeError("interrupted")
        if on_delta is not None:
            on_delta(prompt, 0.01)
        return {
            "text": prompt,
            "elapsed": 0.01,
            "first_token_seconds": 0.01,
            "first_visible_seconds": 0.01,
            "stream_delta_count": 1,
            "thread_id": "thread-test",
            "turn_id": f"turn-{prompt}",
        }

    def request_interrupt(self):
        self.interrupt_calls += 1

    def clear_interrupt_request(self):
        self.clear_calls += 1

    def stop(self):
        pass


class _RecoveryCodexClient:
    instances = []
    fail_instances = set()

    def __init__(self, *_args, **_kwargs):
        self.index = len(self.__class__.instances)
        self.start_thread_id = None
        self.prompts = []
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self, thread_id=None, **_kwargs):
        self.start_thread_id = thread_id
        return {
            "thread_id": thread_id or "thread-created",
            "startup_seconds": 0.02,
        }

    def run_turn(self, prompt, on_delta=None, **_kwargs):
        self.prompts.append(prompt)
        if self.index in self.__class__.fail_instances:
            raise interview_app.CodexAppServerTransportError(
                f"transport failed on client {self.index}"
            )
        if on_delta is not None:
            on_delta("Recovered answer", 0.01)
        return {
            "text": "Recovered answer",
            "elapsed": 0.02,
            "first_token_seconds": 0.01,
            "first_visible_seconds": 0.01,
            "stream_delta_count": 1,
            "thread_id": self.start_thread_id,
            "turn_id": f"turn-{self.index}",
        }

    def request_interrupt(self):
        pass

    def clear_interrupt_request(self):
        pass

    def stop(self):
        self.stopped = True


class _CaptureLatestWorker:
    def __init__(self):
        self.jobs = []

    def submit_latest(
        self,
        generation,
        prompt,
        callback,
        on_delta=None,
        on_start=None,
        on_recovery=None,
    ):
        self.jobs.append({
            "generation": generation,
            "prompt": prompt,
            "callback": callback,
            "on_delta": on_delta,
            "on_start": on_start,
            "on_recovery": on_recovery,
        })
        return True


class _FakeAnswerWindow:
    def __init__(self):
        self.status = None
        self.text = None
        self.stream = ""

    def set_status(self, status):
        self.status = status

    def set_text(self, text):
        self.text = text
        self.stream = text

    def start_stream(self, text):
        self.stream = text

    def append_stream(self, text):
        self.stream += text

    def finish_stream(self, text):
        self.stream = text


class _FakePreviewWorker:
    def start(self):
        return False


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipUnless(
    HAS_FASTER_WHISPER,
    "faster_whisper is available in the application virtualenv",
)
class CodexLatestOnlyTest(unittest.TestCase):
    def setUp(self):
        _LatestOnlyCodexClient.instances.clear()
        _RecoveryCodexClient.instances.clear()
        _RecoveryCodexClient.fail_instances = set()

    def test_worker_interrupts_active_and_runs_only_newest_pending_turn(self):
        callbacks = []
        with patch.object(
            interview_app,
            "CodexAppServerClient",
            _LatestOnlyCodexClient,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            worker = interview_app.CodexWorker(lambda *_args: False)
            self.assertTrue(
                _wait_until(lambda: bool(_LatestOnlyCodexClient.instances))
            )
            client = _LatestOnlyCodexClient.instances[0]
            worker.submit_latest(
                1,
                "Q1",
                lambda result, error: callbacks.append((result, error)),
            )
            self.assertTrue(client.q1_started.wait(timeout=2))

            worker.submit_latest(2, "Q2", lambda *_args: None)
            worker.submit_latest(3, "Q3", lambda *_args: None)
            q4_done = threading.Event()
            worker.submit_latest(
                4,
                "Q4",
                lambda *_args: q4_done.set(),
            )
            client.release_q1.set()

            self.assertTrue(q4_done.wait(timeout=2))
            worker.stop()

        self.assertEqual(client.prompts, ["Q1", "Q4"])
        self.assertGreaterEqual(client.interrupt_calls, 1)
        self.assertEqual(len(callbacks), 1)
        self.assertIsNotNone(callbacks[0][1])

    def test_superseded_stream_is_hidden_and_pending_context_is_preserved(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.log_path = Path(directory) / "session.jsonl"
            app.running = True
            app.codex_request_count = 0
            app.active_codex_generation = 0
            app.codex_request_states = {}
            app.codex_state_lock = threading.Lock()
            app.codex_context_cursor = 0
            app.conversation_context = [("INTERVIEWER", "Q1 transcript")]
            app.answer_window = _FakeAnswerWindow()
            app.codex_worker = _CaptureLatestWorker()

            app._request_codex_answer(1, "Q1 transcript")
            q1 = app.codex_worker.jobs[0]
            q1["on_start"]()
            q1["on_delta"]("old partial", 0.1)
            self.assertEqual(app.answer_window.stream, "old partial")

            app.conversation_context.append(("INTERVIEWER", "Q2 transcript"))
            app._request_codex_answer(2, "Q2 transcript")
            q2 = app.codex_worker.jobs[1]
            app.conversation_context.append(("INTERVIEWER", "Q3 transcript"))
            app._request_codex_answer(3, "Q3 transcript")
            q3 = app.codex_worker.jobs[2]

            q1["on_delta"](" stale tail", 0.2)
            q1["callback"](None, RuntimeError("interrupted"))

            self.assertEqual(app.answer_window.stream, "")
            self.assertEqual(app.answer_window.text, "")
            self.assertIn("INTERVIEWER: Q2 transcript", q3["prompt"])
            self.assertIn("NOT SPOKEN", q3["prompt"])
            self.assertEqual(app.conversation_context, [
                ("INTERVIEWER", "Q1 transcript"),
                ("INTERVIEWER", "Q2 transcript"),
                ("INTERVIEWER", "Q3 transcript"),
            ])
            self.assertEqual(
                app.codex_request_states[1]["status"],
                "superseded_finished",
            )
            self.assertEqual(
                app.codex_request_states[2]["status"],
                "superseded",
            )
            self.assertEqual(q2["generation"], 2)
            self.assertEqual(q3["generation"], 3)

    def test_empty_final_transcript_does_not_replace_codex_turn(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.log_path = Path(directory) / "session.jsonl"
            app.running = True
            app.codex_enabled = True
            app.codex_request_count = 1
            app.active_codex_generation = 1
            app.codex_request_states = {
                1: {
                    "request": 1,
                    "question": 1,
                    "status": "running",
                    "spoken": None,
                }
            }
            app.answer_window = _FakeAnswerWindow()
            app.remote_window = _FakeAnswerWindow()
            app.preview_worker = _FakePreviewWorker()
            app.codex_worker = _CaptureLatestWorker()
            marker = {"question": 2, "committed": False}
            state = {"utterance": 2, "audio_file": "interviewer_002.wav"}
            result = {"text": "", "elapsed": 0.1, "details": {}}

            app._commit_question(marker, state, result, None)

            self.assertEqual(app.codex_worker.jobs, [])
            self.assertEqual(app.active_codex_generation, 1)
            self.assertEqual(
                app.codex_request_states[1]["status"],
                "running",
            )

    def test_recovery_failure_marks_codex_unavailable_but_keeps_app_running(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.log_path = Path(directory) / "session.jsonl"
            app.running = True
            app.codex_request_count = 0
            app.active_codex_generation = 0
            app.codex_request_states = {}
            app.codex_state_lock = threading.Lock()
            app.codex_context_cursor = 0
            app.conversation_context = [("INTERVIEWER", "Q1 transcript")]
            app.answer_window = _FakeAnswerWindow()
            app.codex_worker = _CaptureLatestWorker()

            app._request_codex_answer(1, "Q1 transcript")
            job = app.codex_worker.jobs[0]
            job["on_start"]()
            job["on_recovery"]("started", {
                "attempt": 1,
                "error": "process exited",
                "thread_id": "thread-persistent",
            })
            job["on_recovery"]("failed", {
                "attempt": 1,
                "error": "resume failed",
                "thread_id": "thread-persistent",
            })
            job["callback"](None, RuntimeError("resume failed"))

            self.assertTrue(app.running)
            self.assertEqual(app.answer_window.status, "Codex unavailable")
            self.assertEqual(
                app.codex_request_states[1]["status"],
                "unavailable",
            )
            self.assertFalse(app.codex_request_states[1]["spoken"])

    def test_transport_failure_restarts_resumes_and_retries_once(self):
        _RecoveryCodexClient.fail_instances = {0}
        recovery_events = []
        completed = []
        done = threading.Event()
        with patch.object(
            interview_app,
            "CodexAppServerClient",
            _RecoveryCodexClient,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            worker = interview_app.CodexWorker(
                lambda *_args: False,
                thread_id="thread-persistent",
            )
            self.assertTrue(
                _wait_until(lambda: bool(_RecoveryCodexClient.instances))
            )
            worker.submit_latest(
                25,
                "Q25",
                lambda result, error: (
                    completed.append((result, error)),
                    done.set(),
                ),
                on_recovery=lambda stage, details: recovery_events.append(
                    (stage, details)
                ),
            )
            self.assertTrue(done.wait(timeout=2))
            worker.stop()

        self.assertEqual(len(_RecoveryCodexClient.instances), 2)
        first, resumed = _RecoveryCodexClient.instances
        self.assertTrue(first.stopped)
        self.assertEqual(resumed.start_thread_id, "thread-persistent")
        self.assertEqual(first.prompts, ["Q25"])
        self.assertEqual(len(resumed.prompts), 1)
        self.assertIn("LIVE RECOVERY RETRY", resumed.prompts[0])
        self.assertIn("Q25", resumed.prompts[0])
        self.assertEqual([stage for stage, _details in recovery_events], [
            "started",
            "resumed",
        ])
        self.assertIsNone(completed[0][1])
        self.assertEqual(completed[0][0]["recovery_attempts"], 1)

    def test_second_transport_failure_stops_retry_but_next_question_recovers(self):
        _RecoveryCodexClient.fail_instances = {0, 1}
        q1_done = threading.Event()
        q2_done = threading.Event()
        completed = []
        recovery_events = []
        with patch.object(
            interview_app,
            "CodexAppServerClient",
            _RecoveryCodexClient,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            worker = interview_app.CodexWorker(
                lambda *_args: False,
                thread_id="thread-persistent",
            )
            self.assertTrue(
                _wait_until(lambda: bool(_RecoveryCodexClient.instances))
            )
            worker.submit_latest(
                30,
                "Q30",
                lambda result, error: (
                    completed.append((30, result, error)),
                    q1_done.set(),
                ),
                on_recovery=lambda stage, _details: recovery_events.append(
                    (30, stage)
                ),
            )
            self.assertTrue(q1_done.wait(timeout=2))
            worker.submit_latest(
                31,
                "Q31",
                lambda result, error: (
                    completed.append((31, result, error)),
                    q2_done.set(),
                ),
                on_recovery=lambda stage, _details: recovery_events.append(
                    (31, stage)
                ),
            )
            self.assertTrue(q2_done.wait(timeout=2))
            worker.stop()

        q30 = next(item for item in completed if item[0] == 30)
        q31 = next(item for item in completed if item[0] == 31)
        self.assertIsNotNone(q30[2])
        self.assertIsNone(q31[2])
        self.assertEqual(q31[1]["text"], "Recovered answer")
        self.assertEqual(len(_RecoveryCodexClient.instances), 3)
        self.assertEqual(
            _RecoveryCodexClient.instances[2].start_thread_id,
            "thread-persistent",
        )
        self.assertEqual(
            [stage for generation, stage in recovery_events if generation == 30],
            ["started", "resumed", "failed"],
        )
        self.assertEqual(
            [stage for generation, stage in recovery_events if generation == 31],
            ["started", "resumed"],
        )


@unittest.skipUnless(
    HAS_FASTER_WHISPER,
    "faster_whisper is available in the application virtualenv",
)
class InterviewAppShutdownTest(unittest.TestCase):
    def test_pending_utterance_is_logged_before_worker_stops(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.session_dir = root
            app.log_path = root / "session.jsonl"
            app.interviewer_utterance_count = 0
            app.remote_utterances = []
            app.pending_questions = []
            app.transcript_lock = threading.Lock()

            with patch.object(
                interview_app,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                app.worker = interview_app.WhisperWorker(
                    lambda *_args: False
                )
                pcm_audio = bytes(32_000)
                app._final_audio(
                    "INTERVIEWER",
                    pcm_audio,
                    0,
                    len(pcm_audio),
                    {"vad_method": "test"},
                )
                app.worker.stop()

            events = [
                json.loads(line)
                for line in app.log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(list(root.glob("interviewer_*.wav"))), 1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "utterance")
            self.assertEqual(
                events[0]["text"],
                "final utterance before shutdown",
            )
            self.assertFalse(app.worker.thread.is_alive())

    def test_final_audio_requeues_bytes_after_returned_replay_start(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            app = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            app.session_dir = root
            app.log_path = root / "session.jsonl"
            app.interviewer_utterance_count = 0
            app.remote_utterances = []
            app.pending_questions = [{
                "trigger": 16_000,
                "suggested_start": 0,
                "target_span": None,
                "source": "active",
                "question": 1,
                "committed": False,
            }]
            app.transcript_lock = threading.Lock()
            app.remote_audio = _FakeRemoteAudio()
            pcm_audio = bytes(range(256)) * 125

            with patch.object(
                interview_app,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                app.worker = interview_app.WhisperWorker(
                    lambda *_args: False
                )
                app._final_audio(
                    "INTERVIEWER",
                    pcm_audio,
                    0,
                    len(pcm_audio),
                    {"vad_method": "test"},
                )
                app.worker.stop()

            self.assertEqual(len(app.remote_audio.requeued), 1)
            remainder, remainder_start, source_end = (
                app.remote_audio.requeued[0]
            )
            self.assertEqual(remainder, b"")
            self.assertEqual(remainder_start, len(pcm_audio))
            self.assertEqual(source_end, len(pcm_audio))


@unittest.skipUnless(
    HAS_FASTER_WHISPER,
    "faster_whisper is available in the application virtualenv",
)
class AudioRemainderReplayTest(unittest.TestCase):
    def test_empty_snapshot_remainder_and_deferred_silence_emit_no_utterance(self):
        stream = interview_app.AudioStream(
            "INTERVIEWER",
            "unused",
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: None,
        )
        deferred_silence = np.zeros(16_000, dtype=np.int16).tobytes()
        source_end = 50_000
        stream.awaiting_question_boundary = True
        stream.awaiting_question_end = source_end
        stream.deferred_audio.extend(deferred_silence)

        requeued = stream.requeue_question_remainder(
            b"",
            source_end,
            source_end,
        )
        events = stream._process_replay()

        self.assertTrue(requeued)
        self.assertFalse(stream.active)
        self.assertFalse(any(final for _preview, final in events))

    def test_remainder_precedes_deferred_audio_without_duplication(self):
        stream = interview_app.AudioStream(
            "INTERVIEWER",
            "unused",
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: None,
        )
        speech = np.full(1_600, 1_000, dtype=np.int16).tobytes()
        remainder_silence = np.zeros(6_400, dtype=np.int16).tobytes()
        deferred_silence = np.zeros(9_600, dtype=np.int16).tobytes()
        remainder = speech + remainder_silence
        source_end = 50_000
        remainder_start = source_end - len(remainder)
        stream.awaiting_question_boundary = True
        stream.awaiting_question_end = source_end
        stream.deferred_audio.extend(deferred_silence)

        requeued = stream.requeue_question_remainder(
            remainder,
            remainder_start,
            source_end,
        )

        self.assertTrue(requeued)
        self.assertEqual(
            bytes(stream.replay_audio),
            remainder + deferred_silence,
        )
        events = stream._process_replay()
        finals = [final for _preview, final in events if final is not None]
        self.assertEqual(len(finals), 1)
        pcm_audio, start, end, _details = finals[0]
        self.assertEqual(pcm_audio, remainder + deferred_silence)
        self.assertEqual(start, remainder_start)
        self.assertEqual(end, remainder_start + len(pcm_audio))


if __name__ == "__main__":
    unittest.main()
