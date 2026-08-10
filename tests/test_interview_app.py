import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import interview_app


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
        self.boundary_status = None
        self.response_status = None
        self.text = None
        self.stream = ""

    def set_status(self, status):
        self.status = status

    def set_text(self, text):
        self.text = text
        self.stream = text

    def set_boundary_status(self, status):
        self.boundary_status = status

    def set_response_status(self, status):
        self.response_status = status

    def start_stream(self, text):
        self.stream = text

    def append_stream(self, text):
        self.stream += text

    def finish_stream(self, text):
        self.stream = text


class _FakeVisibleWindow:
    def __init__(self, text, position, size, scroll):
        self.visible = True
        self.text = text
        self.position = position
        self.size = size
        self.scroll = scroll

    def hide(self):
        self.visible = False

    def show(self):
        self.visible = True


class _FakeControlWindow:
    def __init__(self):
        self.visible = True
        self.live_windows_hidden = None

    def set_live_windows_hidden(self, hidden):
        self.live_windows_hidden = hidden


class _FakeSensitiveWidget:
    def __init__(self):
        self.sensitive = None

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


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

    def test_live_worker_receives_selected_model_and_effort_snapshot(self):
        captured = {}

        class FakeWorker:
            def __init__(self, on_ready, thread_id=None, **kwargs):
                captured.update({
                    "on_ready": on_ready,
                    "thread_id": thread_id,
                    **kwargs,
                })

        callback = lambda *_args: False
        settings = {
            "codex_model": "gpt-5.6-terra",
            "codex_reasoning_effort": "high",
            "codex_fast_mode": True,
        }
        with patch.object(interview_app, "CodexWorker", FakeWorker):
            worker = interview_app.create_live_codex_worker(
                callback,
                "thread-existing",
                dict(settings),
            )

        settings["codex_model"] = "gpt-5.6-luna"
        self.assertIsInstance(worker, FakeWorker)
        self.assertEqual(captured["thread_id"], "thread-existing")
        self.assertEqual(captured["model"], "gpt-5.6-terra")
        self.assertEqual(captured["effort"], "high")
        self.assertTrue(captured["fast_mode"])

    def test_session_list_row_displays_created_model_and_effort(self):
        row = interview_app.session_list_row({
            "thread_id": "thread-123",
            "created_at": "2026-08-10T17:30:45+09:00",
            "settings": {
                "codex_model": "gpt-5.6-luna",
                "codex_reasoning_effort": "medium",
            },
        })

        self.assertEqual(row, (
            "2026-08-10 17:30",
            "thread-123",
            "gpt-5.6-luna",
            "medium",
        ))

    def test_preparation_settings_wait_for_catalog_before_enabling(self):
        dialog = interview_app.PreparationChatDialog.__new__(
            interview_app.PreparationChatDialog
        )
        dialog.model_combo = _FakeSensitiveWidget()
        dialog.reasoning_combo = _FakeSensitiveWidget()
        dialog.fast_combo = _FakeSensitiveWidget()

        dialog._set_settings_sensitive(False)

        self.assertFalse(dialog.model_combo.sensitive)
        self.assertFalse(dialog.reasoning_combo.sensitive)
        self.assertFalse(dialog.fast_combo.sensitive)

        dialog._set_settings_sensitive(True)

        self.assertTrue(dialog.model_combo.sensitive)
        self.assertTrue(dialog.reasoning_combo.sensitive)
        self.assertTrue(dialog.fast_combo.sensitive)

    def test_unsupported_model_cannot_reach_live_snapshot_with_fast_enabled(self):
        dialog = interview_app.PreparationChatDialog.__new__(
            interview_app.PreparationChatDialog
        )
        dialog.codex_models = [{
            "model": "gpt-5.2",
            "additionalSpeedTiers": [],
            "serviceTiers": [],
        }]
        dialog.codex_settings = {
            "codex_model": "gpt-5.2",
            "codex_reasoning_effort": "low",
            "codex_fast_mode": True,
        }

        snapshot = dialog.settings_snapshot()

        self.assertFalse(snapshot["codex_fast_mode"])
        self.assertFalse(interview_app.model_supports_fast(
            dialog.codex_models[0]
        ))

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

    def test_normal_request_is_thinking_until_current_first_visible(self):
        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.log_path = None
        app.running = True
        app.codex_request_count = 0
        app.active_codex_generation = 0
        app.codex_request_states = {}
        app.codex_state_lock = threading.Lock()
        app.codex_context_cursor = 0
        app.conversation_context = [("INTERVIEWER", "Why this role?")]
        app.answer_window = _FakeAnswerWindow()
        app.codex_worker = _CaptureLatestWorker()

        app._request_codex_answer(1, "Why this role?")

        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_THINKING,
        )
        job = app.codex_worker.jobs[0]
        job["on_start"]()
        job["on_delta"]("Current answer", 0.1)
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_READY,
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


class MoonshineAppIntegrationTest(unittest.TestCase):
    @staticmethod
    def _result(text, cursor):
        return {
            "text": text,
            "display_text": text,
            "lines": [{"text": text}] if text else [],
            "committed": True,
            "captured_sample_cursor": cursor,
            "target_sample_cursor": cursor,
            "queued_sample_cursor": cursor,
            "consumed_sample_cursor": cursor,
            "cursor_complete": True,
            "audio_drop_samples": 0,
            "max_backlog_ms": 20.0,
            "barrier_wait_ms": 5.0,
            "force_update_ms": 2.0,
        }

    @staticmethod
    def _app(codex_enabled=False, log_path=None):
        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.running = True
        app.log_path = log_path
        app.codex_enabled = codex_enabled
        app.question_count = 0
        app.remote_window = _FakeAnswerWindow()
        app.answer_window = _FakeAnswerWindow()
        app.conversation_context = []
        app.last_commit_state = None
        if codex_enabled:
            app.codex_request_count = 0
            app.active_codex_generation = 0
            app.codex_request_states = {}
            app.codex_state_lock = threading.Lock()
            app.codex_context_cursor = 0
            app.codex_worker = _CaptureLatestWorker()
        return app

    def _commit(self, app, number, text, cursor, source):
        app.question_count = max(app.question_count, number)
        app._moonshine_question_ready(
            number,
            time.perf_counter(),
            self._result(text, cursor),
            None,
            commit_source=source,
        )

    def _continue(self, app, text, cursor):
        base = dict(app.last_commit_state)
        app._moonshine_continuation_ready(
            base,
            time.perf_counter(),
            self._result(text, cursor),
            None,
        )

    def test_f8_snapshot_commits_one_question_and_one_codex_request(self):
        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.running = True
        app.log_path = None
        app.codex_enabled = True
        app.remote_window = _FakeAnswerWindow()
        app.answer_window = _FakeAnswerWindow()
        app.conversation_context = []
        requests = []
        app._request_codex_answer = lambda number, text: requests.append(
            (number, text)
        )
        result = {
            "text": "Why this role?",
            "display_text": "Why this role?",
            "lines": [{"text": "Why this role?"}],
            "captured_sample_cursor": 16_000,
            "target_sample_cursor": 16_000,
            "queued_sample_cursor": 16_000,
            "consumed_sample_cursor": 16_000,
            "cursor_complete": True,
            "audio_drop_samples": 0,
            "max_backlog_ms": 20.0,
            "barrier_wait_ms": 5.0,
            "force_update_ms": 2.0,
        }

        app._moonshine_question_ready(
            1,
            time.perf_counter(),
            result,
            None,
        )

        self.assertEqual(
            app.conversation_context,
            [("INTERVIEWER", "Why this role?")],
        )
        self.assertEqual(requests, [(1, "Why this role?")])
        self.assertEqual(app.remote_window.text, "Why this role?")
        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_F8,
        )

    def test_moonshine_ready_sets_listening_boundary_status(self):
        app = self._app()
        app.moonshine_ready = False
        app.audio_started = True

        app._moonshine_ready({
            "model": "small-streaming",
            "language": "en",
            "load_seconds": 0.1,
            "update_interval_ms": 500,
        }, None)

        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_LISTENING,
        )

    def test_new_transcript_activity_restores_listening_boundary_status(self):
        app = self._app()
        app.remote_window.set_boundary_status(interview_app.BOUNDARY_STATUS_AUTO)

        app._moonshine_preview({"text": "New interviewer speech", "lines": []})

        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_LISTENING,
        )

    def test_auto_commit_logs_silence_source_with_codex_disabled(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
            app.running = True
            app.log_path = Path(directory) / "session.jsonl"
            app.codex_enabled = False
            app.question_count = 0
            app.remote_window = _FakeAnswerWindow()
            app.answer_window = _FakeAnswerWindow()
            app.conversation_context = []
            result = {
                "text": "Why this role?",
                "display_text": "Why this role?",
                "lines": [{"text": "Why this role?"}],
                "commit_requested_at": time.perf_counter(),
                "committed": True,
                "captured_sample_cursor": 24_000,
                "target_sample_cursor": 24_000,
                "queued_sample_cursor": 24_000,
                "consumed_sample_cursor": 24_000,
                "cursor_complete": True,
                "audio_drop_samples": 0,
                "max_backlog_ms": 20.0,
                "barrier_wait_ms": 0.0,
                "force_update_ms": 2.0,
            }

            app._moonshine_auto_commit(result, None)

            events = [
                json.loads(line)
                for line in app.log_path.read_text(encoding="utf-8").splitlines()
            ]
            question = next(event for event in events if event["event"] == "question")
            self.assertEqual(question["commit_source"], "silence")
            self.assertEqual(app.question_count, 1)
            self.assertEqual(app.answer_window.status, "Codex disabled · question logged only")
            self.assertEqual(
                app.remote_window.boundary_status,
                interview_app.BOUNDARY_STATUS_AUTO,
            )

    def test_silence_a_then_f9_b_replaces_a_with_combined_question(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            log_path = Path(directory) / "session.jsonl"
            app = self._app(log_path=log_path)
            self._commit(app, 1, "Tell me about a project where you", 16_000, "silence")

            self._continue(
                app,
                "had to solve a difficult technical problem.",
                32_000,
            )

            combined = (
                "Tell me about a project where you "
                "had to solve a difficult technical problem."
            )
            self.assertEqual(app.conversation_context, [("INTERVIEWER", combined)])
            self.assertEqual(app.remote_window.text, combined)
            self.assertEqual(
                app.last_commit_state["commit_source"],
                "f9_continuation",
            )
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            correction = [
                event for event in events
                if event.get("commit_source") == "f9_continuation"
                and event["event"] == "question"
            ][0]
            self.assertEqual(correction["previous_question"], 1)
            self.assertEqual(correction["previous_target_sample_cursor"], 16_000)
            self.assertEqual(correction["text"], combined)
            self.assertTrue(correction["cursor_complete"])
            self.assertEqual(correction["audio_drop_samples"], 0)
            self.assertEqual(
                app.remote_window.boundary_status,
                interview_app.BOUNDARY_STATUS_F9,
            )

    def test_f8_a_then_f9_b_replaces_a_with_combined_question(self):
        app = self._app()
        self._commit(app, 1, "What experience do you", 10_000, "f8")

        self._continue(app, "have with Python?", 20_000)

        self.assertEqual(app.conversation_context, [
            ("INTERVIEWER", "What experience do you have with Python?"),
        ])
        self.assertEqual(app.last_commit_state["question_number"], 1)
        self.assertEqual(app.question_count, 1)

    def test_silence_a_then_f8_b_stays_as_two_questions(self):
        app = self._app()
        self._commit(app, 1, "Why this role?", 10_000, "silence")
        self._commit(app, 2, "What are your strengths?", 20_000, "f8")

        self.assertEqual(app.conversation_context, [
            ("INTERVIEWER", "Why this role?"),
            ("INTERVIEWER", "What are your strengths?"),
        ])
        self.assertEqual(app.last_commit_state["commit_source"], "f8")

    def test_f8_a_then_f8_b_stays_as_two_questions(self):
        app = self._app()
        self._commit(app, 1, "Why this role?", 10_000, "f8")
        self._commit(app, 2, "What are your strengths?", 20_000, "f8")

        self.assertEqual(len(app.conversation_context), 2)
        self.assertEqual(app.conversation_context[0][1], "Why this role?")
        self.assertEqual(app.conversation_context[1][1], "What are your strengths?")

    def test_empty_b_on_f9_does_not_alter_a_or_send_codex(self):
        app = self._app(codex_enabled=True)
        self._commit(app, 1, "Why this role?", 10_000, "silence")
        original_state = dict(app.last_commit_state)
        original_boundary_status = app.remote_window.boundary_status

        self._continue(app, "", 20_000)

        self.assertEqual(app.conversation_context, [
            ("INTERVIEWER", "Why this role?"),
        ])
        self.assertEqual(app.last_commit_state, original_state)
        self.assertEqual(len(app.codex_worker.jobs), 1)
        self.assertEqual(
            app.remote_window.boundary_status,
            original_boundary_status,
        )

    def test_f9_with_no_valid_previous_question_fails_before_snapshot(self):
        app = self._app()
        app.last_f9_at = None
        app.moonshine_ready = True
        app.audio_started = True
        snapshot_calls = []
        app.remote_audio = SimpleNamespace(
            capture_sample_cursor_and=lambda _enqueue: snapshot_calls.append(True)
        )

        app._on_f9()

        self.assertEqual(snapshot_calls, [])
        self.assertEqual(app.conversation_context, [])
        self.assertEqual(app.answer_window.status, "No previous question to continue")

    def test_completed_codex_a_is_explicitly_superseded_by_a_plus_b(self):
        app = self._app(codex_enabled=True)
        self._commit(app, 1, "Tell me about a project where you", 10_000, "silence")
        first_job = app.codex_worker.jobs[0]
        first_job["on_start"]()
        first_job["callback"]({
            "text": "Old answer",
            "elapsed": 0.1,
            "first_token_seconds": 0.05,
            "first_visible_seconds": 0.05,
            "stream_delta_count": 1,
            "thread_id": "thread-test",
            "turn_id": "turn-a",
        }, None)
        self.assertEqual(app.codex_request_states[1]["status"], "completed")

        self._continue(app, "had to solve a difficult problem.", 20_000)

        self.assertEqual(app.codex_request_states[1]["status"], "superseded")
        self.assertFalse(app.codex_request_states[1]["spoken"])
        self.assertEqual(app.active_codex_generation, 2)
        correction_prompt = app.codex_worker.jobs[1]["prompt"]
        self.assertIn("previous interviewer question was incomplete", correction_prompt)
        self.assertIn("previous answer was NOT SPOKEN", correction_prompt)
        self.assertIn(
            "Tell me about a project where you had to solve a difficult problem.",
            correction_prompt,
        )

    def test_running_codex_a_is_superseded_and_late_output_is_hidden(self):
        app = self._app(codex_enabled=True)
        self._commit(app, 1, "What would you", 10_000, "f8")
        first_job = app.codex_worker.jobs[0]
        first_job["on_start"]()
        first_job["on_delta"]("Old partial", 0.05)
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_READY,
        )

        self._continue(app, "do in this situation?", 20_000)

        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_UPDATING,
        )
        self.assertEqual(app.codex_request_states[1]["status"], "superseded")
        self.assertFalse(app.codex_request_states[1]["spoken"])
        self.assertEqual(app.answer_window.text, "")
        first_job["on_delta"](" stale tail", 0.1)
        first_job["callback"](None, RuntimeError("interrupted"))
        self.assertEqual(
            app.codex_request_states[1]["status"],
            "superseded_finished",
        )
        self.assertEqual(app.answer_window.text, "")
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_UPDATING,
        )
        self.assertEqual(app.active_codex_generation, 2)
        corrected_job = app.codex_worker.jobs[1]
        corrected_job["on_start"]()
        corrected_job["on_delta"]("Corrected answer", 0.1)
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_READY,
        )

    def test_f9_uses_atomic_cursor_capture_and_worker_snapshot(self):
        app = self._app()
        self._commit(app, 1, "What would you", 10_000, "f8")
        app.last_f9_at = None
        app.moonshine_ready = True
        app.audio_started = True
        captured = []
        requested = []
        app.remote_audio = SimpleNamespace(
            capture_sample_cursor_and=lambda enqueue: (
                captured.append(20_000),
                (20_000, enqueue(20_000)),
            )[1]
        )
        app.asr_worker = SimpleNamespace(
            request_snapshot=lambda cursor, callback: (
                requested.append((cursor, callback)),
                True,
            )[1],
        )

        app._on_f9()

        self.assertEqual(captured, [20_000])
        self.assertEqual(requested[0][0], 20_000)


class AudioStreamTest(unittest.TestCase):
    def test_streaming_only_capture_forwards_each_raw_pcm_sample_once(self):
        forwarded = []
        stream = interview_app.AudioStream(
            "INTERVIEWER",
            "unused",
            lambda pcm, start, end: forwarded.append(
                (pcm, start, end)
            ),
            lambda *_args: None,
        )
        stream.process = SimpleNamespace(stdout=io.BytesIO(bytes(640)))

        stream._read_loop()

        self.assertEqual(len(forwarded), 2)
        self.assertEqual(forwarded[0], (bytes(320), 0, 160))
        self.assertEqual(forwarded[1], (bytes(320), 160, 320))
        self.assertEqual(stream.total_samples, 320)

    def test_f8_cursor_and_enqueue_are_atomic_under_audio_lock(self):
        stream = interview_app.AudioStream(
            "INTERVIEWER",
            "unused",
            lambda *_args: None,
            lambda *_args: None,
        )
        stream.total_samples = 320
        seen = []

        cursor, accepted = stream.capture_sample_cursor_and(
            lambda target: seen.append(target) or True
        )

        self.assertTrue(accepted)
        self.assertEqual(cursor, 320)
        self.assertEqual(seen, [320])


class LiveWindowVisibilityTest(unittest.TestCase):
    def test_hide_then_restore_preserves_live_window_state(self):
        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.live_windows_hidden = False
        app.remote_window = _FakeVisibleWindow(
            "Question transcript",
            (100, 200),
            (720, 170),
            12.5,
        )
        app.answer_window = _FakeVisibleWindow(
            "Current answer",
            (100, 380),
            (720, 300),
            48.0,
        )
        app.control_window = _FakeControlWindow()
        remote_state = vars(app.remote_window).copy()
        answer_state = vars(app.answer_window).copy()

        app.toggle_live_windows_visibility()

        self.assertFalse(app.remote_window.visible)
        self.assertFalse(app.answer_window.visible)
        self.assertTrue(app.control_window.visible)
        self.assertTrue(app.control_window.live_windows_hidden)
        self.assertEqual(
            {key: value for key, value in vars(app.remote_window).items()
             if key != "visible"},
            {key: value for key, value in remote_state.items()
             if key != "visible"},
        )
        self.assertEqual(
            {key: value for key, value in vars(app.answer_window).items()
             if key != "visible"},
            {key: value for key, value in answer_state.items()
             if key != "visible"},
        )

        app.toggle_live_windows_visibility()

        self.assertTrue(app.remote_window.visible)
        self.assertTrue(app.answer_window.visible)
        self.assertTrue(app.control_window.visible)
        self.assertFalse(app.control_window.live_windows_hidden)
        self.assertEqual(app.remote_window.text, "Question transcript")
        self.assertEqual(app.remote_window.position, (100, 200))
        self.assertEqual(app.remote_window.size, (720, 170))
        self.assertEqual(app.remote_window.scroll, 12.5)
        self.assertEqual(app.answer_window.text, "Current answer")
        self.assertEqual(app.answer_window.position, (100, 380))
        self.assertEqual(app.answer_window.size, (720, 300))
        self.assertEqual(app.answer_window.scroll, 48.0)

if __name__ == "__main__":
    unittest.main()
