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
from codex import worker as codex_worker_module
from interview import controller as controller_module
from ui import preparation as preparation_module
from ui import session_dialogs as session_dialogs_module
from context_manager import (
    CONTEXT_STATUS_CHANGED,
    CONTEXT_STATUS_SYNCED,
    ContextManager,
    ContextState,
)


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
        self.inject_jobs = []

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

    def submit_inject_items(self, items, callback):
        self.inject_jobs.append({
            "items": items,
            "callback": callback,
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


class _FocusTextIter:
    def __init__(self, offset):
        self.offset = offset


class _FocusTextMark:
    def __init__(self, offset, left_gravity):
        self.offset = offset
        self.left_gravity = left_gravity


class _FocusTextBuffer:
    def __init__(self):
        self.text = ""
        self.marks = []
        self.set_text_calls = 0

    def set_text(self, text):
        self.text = text
        self.set_text_calls += 1

    def get_end_iter(self):
        return _FocusTextIter(len(self.text))

    def get_iter_at_offset(self, offset):
        return _FocusTextIter(offset)

    def get_iter_at_mark(self, mark):
        return _FocusTextIter(mark.offset)

    def get_text(self, start, end, _include_hidden):
        return self.text[start.offset:end.offset]

    def insert(self, position, text):
        offset = position.offset
        self.text = self.text[:offset] + text + self.text[offset:]
        for mark in self.marks:
            if mark.offset > offset or (
                mark.offset == offset and not mark.left_gravity
            ):
                mark.offset += len(text)

    def delete(self, start, end):
        removed = end.offset - start.offset
        self.text = self.text[:start.offset] + self.text[end.offset:]
        for mark in self.marks:
            if mark.offset > end.offset:
                mark.offset -= removed
            elif mark.offset >= start.offset:
                mark.offset = start.offset

    def create_mark(self, _name, position, left_gravity):
        mark = _FocusTextMark(position.offset, left_gravity)
        self.marks.append(mark)
        return mark

    def delete_mark(self, mark):
        self.marks.remove(mark)


class _FocusTextView:
    def __init__(self):
        self.buffer = _FocusTextBuffer()
        self.scroll_position = 0
        self.scroll_calls = []

    def get_buffer(self):
        return self.buffer

    def scroll_to_mark(
        self,
        mark,
        within_margin,
        use_align,
        xalign,
        yalign,
    ):
        self.scroll_position = mark.offset
        self.scroll_calls.append(
            (mark, within_margin, use_align, xalign, yalign)
        )

    def get_iter_location(self, position):
        return SimpleNamespace(y=position.offset)


class _FocusAdjustment:
    def __init__(self):
        self.value = 0

    def get_lower(self):
        return 0

    def get_upper(self):
        return 10_000

    def get_page_size(self):
        return 100

    def set_value(self, value):
        self.value = value

    def get_value(self):
        return self.value


class _FocusScroller:
    def __init__(self):
        self.adjustment = _FocusAdjustment()

    def get_vadjustment(self):
        return self.adjustment


class _FocusHistoryHarness:
    discard_current_answer = interview_app.TranscriptWindow.discard_current_answer
    prepare_corrected_answer_alignment = (
        interview_app.TranscriptWindow.prepare_corrected_answer_alignment
    )
    start_stream = interview_app.TranscriptWindow.start_stream
    append_stream = interview_app.TranscriptWindow.append_stream
    finish_stream = interview_app.TranscriptWindow.finish_stream
    _render_focus_answers = interview_app.TranscriptWindow._render_focus_answers
    _clear_latest_answer_mark = (
        interview_app.TranscriptWindow._clear_latest_answer_mark
    )
    _set_latest_answer_mark = (
        interview_app.TranscriptWindow._set_latest_answer_mark
    )
    _align_latest_answer_once = (
        interview_app.TranscriptWindow._align_latest_answer_once
    )

    def __init__(self, history):
        self.focus_mode = True
        self.text = _FocusTextView()
        self.focus_scroller = _FocusScroller()
        self.answer_history = list(history)
        self.active_answer = ""
        self.focus_placeholder = ""
        self.latest_answer_mark = None
        self._render_focus_answers()


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


class _FakeTextBuffer:
    def __init__(self):
        self.text = None

    def set_text(self, text):
        self.text = text


class _ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class CodexLatestOnlyTest(unittest.TestCase):
    def test_response_status_uses_compact_indicators(self):
        self.assertEqual(interview_app.RESPONSE_STATUS_READY, "●")
        self.assertEqual(interview_app.RESPONSE_STATUS_THINKING, "◌")
        self.assertEqual(interview_app.RESPONSE_STATUS_UPDATING, "◌")
        self.assertEqual(interview_app.RESPONSE_STATUS_ERROR, "×")

    def test_benchmark_initial_session_settings_override_new_session_defaults(self):
        settings = interview_app.initial_session_settings({
            "INTERVIEW_BENCHMARK_INITIAL_SETTINGS": json.dumps({
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
                "codex_fast_mode": False,
                "stt_language": "ja",
            }),
        })

        self.assertEqual(settings, {
            "codex_model": "gpt-5.4",
            "codex_reasoning_effort": "medium",
            "codex_fast_mode": False,
            "stt_language": "ja",
        })

    def setUp(self):
        _LatestOnlyCodexClient.instances.clear()
        _RecoveryCodexClient.instances.clear()
        _RecoveryCodexClient.fail_instances = set()

    def test_worker_interrupts_active_and_runs_only_newest_pending_turn(self):
        callbacks = []
        with patch.object(
            codex_worker_module,
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

    def test_worker_orders_injections_before_following_live_turn(self):
        events = []
        done = threading.Event()

        class OrderedClient(_RecoveryCodexClient):
            def inject_items(self, items):
                events.append(("inject", items[0]["content"][0]["text"]))

            def run_turn(self, prompt, **kwargs):
                events.append(("turn", prompt))
                return super().run_turn(prompt, **kwargs)

        with patch.object(
            codex_worker_module,
            "CodexAppServerClient",
            OrderedClient,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            worker = interview_app.CodexWorker(lambda *_args: False)
            self.assertTrue(_wait_until(lambda: worker.client is not None))
            worker.submit_inject_items(
                [{
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "checkpoint 1"}],
                }],
                lambda *_args: None,
            )
            worker.submit_inject_items(
                [{
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "checkpoint 2"}],
                }],
                lambda *_args: None,
            )
            worker.submit_latest(
                1,
                "question",
                lambda *_args: done.set(),
            )
            self.assertTrue(done.wait(timeout=2))
            worker.stop()

        self.assertEqual(events, [
            ("inject", "checkpoint 1"),
            ("inject", "checkpoint 2"),
            ("turn", "question"),
        ])

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

    def test_session_list_row_displays_name_stt_and_last_used(self):
        row = interview_app.session_list_row({
            "session_id": "session-123",
            "created_at": "2026-08-10T17:30:45+09:00",
            "last_used_at": "2026-08-11T09:15:20+09:00",
            "name": "Backend Interview",
            "settings": {
                "codex_model": "gpt-5.6-luna",
                "codex_reasoning_effort": "medium",
                "stt_language": "ja",
            },
        })

        self.assertEqual(row, (
            "Backend Interview",
            "JA · Base",
            "2026-08-11 09:15",
            "session-123",
        ))

    def test_session_back_returns_to_mode_selection_without_opening_session(self):
        dialog = SimpleNamespace(
            run=lambda: interview_app.SESSION_RESPONSE_BACK,
            selected_session=lambda: None,
            selected_sessions=lambda: [],
            all_sessions=lambda: [],
            destroy=lambda: None,
        )
        store = SimpleNamespace(active=lambda: [])

        with patch.object(
            session_dialogs_module,
            "SessionChooserDialog",
            return_value=dialog,
        ):
            result = interview_app.choose_interview_session(
                store,
                SimpleNamespace(),
            )

        self.assertEqual(result, interview_app.SESSION_RESPONSE_BACK)

    def test_bulk_archive_marks_each_selected_session(self):
        archived = []
        store = SimpleNamespace(
            mark_archived=lambda session_id: archived.append(session_id),
        )
        sessions = [
            {"session_id": "session-a", "name": "A"},
            {"session_id": "session-b", "name": "B"},
        ]

        failures = interview_app._archive_sessions(
            store,
            sessions,
            codex_enabled=False,
        )

        self.assertEqual(failures, [])
        self.assertEqual(archived, ["session-a", "session-b"])

    def test_bulk_archive_continues_after_individual_failure(self):
        archived = []
        store = SimpleNamespace(
            mark_archived=lambda session_id: archived.append(session_id),
        )
        sessions = [
            {"session_id": "session-a", "name": "A"},
            {"session_id": "session-b", "name": "B"},
        ]

        with patch.object(
            session_dialogs_module,
            "_archive_session",
            side_effect=[RuntimeError("failed"), None],
        ) as archive_session:
            failures = interview_app._archive_sessions(
                store,
                sessions,
                codex_enabled=True,
            )

        self.assertEqual(archive_session.call_count, 2)
        self.assertEqual([session["session_id"] for session, _ in failures], [
            "session-a",
        ])
        self.assertEqual(archived, [])

    def test_session_back_relaunches_mode_selector_detached(self):
        with patch.object(interview_app.subprocess, "Popen") as popen:
            interview_app.launch_interview_launcher()

        popen.assert_called_once_with(
            [
                interview_app.sys.executable,
                str(interview_app.APP_DIR / "interview_launcher.py"),
            ],
            cwd=interview_app.APP_DIR,
            stdin=interview_app.subprocess.DEVNULL,
            start_new_session=True,
        )

    def test_context_filename_becomes_human_readable_display_name(self):
        self.assertEqual(interview_app.context_display_name("company.md"), "Company")
        self.assertEqual(
            interview_app.context_display_name("answer_style.md"),
            "Answer Style",
        )
        self.assertEqual(
            interview_app.context_display_name("interview-focus.md"),
            "Interview Focus",
        )

    def test_stt_presentation_names_actual_model_and_asr_mode(self):
        self.assertEqual(interview_app.stt_presentation("en"), {
            "language": "English",
            "title": "Moonshine Small Streaming",
            "model": "small-streaming-en",
            "mode": "Streaming ASR",
        })
        self.assertEqual(interview_app.stt_presentation("ja"), {
            "language": "Japanese",
            "title": "Moonshine Base",
            "model": "base-ja",
            "mode": "Base ASR",
        })

    def test_context_scope_and_status_have_distinct_style_classes(self):
        self.assertEqual(
            interview_app.context_scope_style("GLOBAL"),
            "scope-global",
        )
        self.assertEqual(
            interview_app.context_scope_style("SESSION"),
            "scope-session",
        )
        self.assertEqual(
            interview_app.context_status_style("SYNCED"),
            "status-synced",
        )
        self.assertEqual(
            interview_app.context_status_style("CHANGED"),
            "status-changed",
        )
        self.assertEqual(
            interview_app.context_status_style("NOT SYNCED"),
            "status-not-synced",
        )

    def test_preparation_workspace_starts_at_72_28_ratio(self):
        self.assertEqual(
            interview_app.preparation_conversation_position(1000),
            720,
        )
        self.assertEqual(
            interview_app.preparation_conversation_position(800),
            576,
        )

    def test_preparation_status_summaries_reflect_context_and_stt(self):
        self.assertEqual(interview_app.stt_status_summary("en"), "EN · Streaming")
        self.assertEqual(interview_app.stt_status_summary("ja"), "JA · Base")
        self.assertEqual(
            interview_app.context_status_summary([{"status": "SYNCED"}]),
            ("● Context Synced", "status-synced"),
        )
        self.assertEqual(
            interview_app.context_status_summary([
                {"status": "SYNCED"},
                {"status": "CHANGED"},
            ]),
            ("● Context Changed", "status-changed"),
        )
        self.assertEqual(
            interview_app.context_status_summary([
                {"status": "NOT SYNCED"},
            ]),
            ("● Context Not Synced", "status-not-synced"),
        )

    def test_stt_diagnostic_runtime_options_keep_codex_off(self):
        diagnostic = interview_app.runtime_options({
            "INTERVIEW_APP_MODE": "stt_diagnostic",
            "INTERVIEW_DISABLE_CODEX": "1",
            "INTERVIEW_TEST_LOG": "1",
            "INTERVIEW_STT_DIAGNOSTICS": "1",
        })
        self.assertFalse(diagnostic["codex_enabled"])
        self.assertEqual(
            interview_app.preparation_runtime_summary(diagnostic, "ja"),
            "Mode: STT Diagnostic  ·  Codex: Off  ·  Logging: On  ·  "
            "STT: Japanese / base-ja",
        )

    def test_effective_contexts_keep_scope_name_file_and_path_for_ui(self):
        path = Path("/tmp/session/contexts/company.md")
        rows = interview_app.context_display_rows([
            ContextState(
                scope="session",
                name="company.md",
                path=path,
                status="NOT SYNCED",
                content_hash="a" * 64,
                synced_hash=None,
            ),
        ])

        self.assertEqual(rows, [{
            "scope": "SESSION",
            "display_name": "Company",
            "filename": "company.md",
            "path": path,
            "status": "NOT SYNCED",
        }])

    def test_interview_conversation_keeps_question_and_final_answer_only(self):
        thread = {
            "turns": [{
                "items": [
                    {
                        "type": "developerMessage",
                        "content": [{
                            "type": "input_text",
                            "text": "INTERVIEW CONTEXT SNAPSHOT\nsecret",
                        }],
                    },
                    {
                        "type": "systemMessage",
                        "content": [{
                            "type": "text",
                            "text": "internal system message",
                        }],
                    },
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "text",
                            "text": "internal user item without marker",
                        }],
                    },
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "text",
                            "text": (
                                "NEW CONVERSATION SINCE THE PREVIOUS REQUEST:\n"
                                "INTERVIEWER: hidden context\n\n"
                                "CURRENT INTERVIEWER QUESTION:\n"
                                "Tell me about a difficult project."
                            ),
                        }],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "internal commentary",
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "I solved a difficult migration problem.",
                    },
                ],
            }],
        }

        messages = interview_app.interview_conversation_messages(thread)

        self.assertEqual(messages, [
            {
                "role": "interviewer",
                "text": "Tell me about a difficult project.",
            },
            {
                "role": "codex",
                "text": "I solved a difficult migration problem.",
            },
        ])

    def test_interview_conversation_keeps_preparation_question_and_answer(self):
        thread = {
            "turns": [{
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{
                            "type": "text",
                            "text": (
                                f"{interview_app.PREPARATION_MESSAGE_MARKER}\n"
                                "What should I emphasize for this role?"
                            ),
                        }],
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "Emphasize a relevant, measurable result.",
                    },
                ],
            }],
        }

        messages = interview_app.interview_conversation_messages(thread)

        self.assertEqual(messages, [
            {
                "role": "candidate",
                "text": "What should I emphasize for this role?",
            },
            {
                "role": "codex",
                "text": "Emphasize a relevant, measurable result.",
            },
        ])

    def test_preparation_chat_requires_synced_context_and_thread(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.active = True
        dialog.codex_enabled = True
        dialog.context_sync_in_progress = False
        dialog.session = {"interview_thread_id": "thread-interview"}
        dialog.context_rows = [{"status": "SYNCED"}]

        self.assertEqual(
            dialog._preparation_chat_thread_id(),
            "thread-interview",
        )

        dialog.context_rows = [{"status": "CHANGED"}]

        self.assertIsNone(dialog._preparation_chat_thread_id())

    def test_conversation_without_interview_thread_shows_guidance(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.session_store = None
        dialog.session = {"interview_thread_id": None}
        dialog.conversation_load_generation = 0
        dialog.conversation_refresh_button = _FakeSensitiveWidget()
        shown = []
        dialog._set_conversation_text = shown.append

        with patch.object(
            interview_app.threading,
            "Thread",
            side_effect=AssertionError("must not read a missing thread"),
        ):
            dialog._refresh_conversation()

        self.assertEqual(shown, [interview_app.NO_INTERVIEW_THREAD_TEXT])
        self.assertTrue(dialog.conversation_refresh_button.sensitive)

    def test_thread_without_visible_conversation_shows_empty_message(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.active = True
        dialog.session = {"interview_thread_id": "thread-current"}
        dialog.conversation_load_generation = 3
        dialog.conversation_refresh_button = _FakeSensitiveWidget()
        dialog.conversation_buffer = _FakeTextBuffer()
        shown = []
        dialog._set_conversation_text = shown.append

        result = dialog._conversation_load_finished(
            3,
            "thread-current",
            {"turns": []},
            None,
        )

        self.assertFalse(result)
        self.assertEqual(
            shown,
            [interview_app.NO_INTERVIEW_CONVERSATION_TEXT],
        )
        self.assertTrue(dialog.conversation_refresh_button.sensitive)

    def test_archived_thread_result_cannot_replace_current_conversation(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.active = True
        dialog.session = {"interview_thread_id": "thread-current"}
        dialog.conversation_load_generation = 4
        dialog.conversation_refresh_button = _FakeSensitiveWidget()
        dialog._set_conversation_text = lambda _text: self.fail(
            "an archived thread result must be ignored"
        )

        result = dialog._conversation_load_finished(
            3,
            "thread-archived",
            {"turns": []},
            None,
        )

        self.assertFalse(result)

    def test_conversation_loader_reads_current_thread_without_resume(self):
        events = []

        class ReadClient:
            def connect(self):
                events.append("connect")

            def read_thread(self, thread_id, include_turns=True):
                events.append(("read", thread_id, include_turns))
                return {"id": thread_id, "turns": []}

            def start(self, **_kwargs):
                raise AssertionError("Conversation refresh must not resume")

            def stop(self):
                events.append("stop")

        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        captured = {}
        dialog._conversation_load_finished = lambda *args: captured.update(
            args=args
        )
        with patch.object(
            preparation_module,
            "_new_codex_client",
            return_value=ReadClient(),
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            dialog._run_conversation_load(
                5,
                "thread-current",
                {"codex_model": "gpt-test"},
            )

        self.assertEqual(events, [
            "connect",
            ("read", "thread-current", True),
            "stop",
        ])
        self.assertEqual(captured["args"][:3], (
            5,
            "thread-current",
            {"id": "thread-current", "turns": []},
        ))
        self.assertIsNone(captured["args"][3])

    def test_context_refresh_reloads_changed_and_restored_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ContextManager(temporary_directory)
            context = manager.create_context(
                "session",
                "thread-123",
                "Company",
            )
            context.path.write_text("original", encoding="utf-8")
            manager.record_successful_sync("thread-123", context)
            metadata_path = manager.sync_metadata_path("thread-123")
            metadata_before = metadata_path.read_bytes()

            context.path.write_text("modified", encoding="utf-8")
            changed = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(changed[0]["status"], CONTEXT_STATUS_CHANGED)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)

            context.path.write_text("original", encoding="utf-8")
            restored = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(restored[0]["status"], CONTEXT_STATUS_SYNCED)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)

    def test_context_refresh_reloads_external_addition_and_deletion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ContextManager(temporary_directory)
            company = manager.create_context(
                "session",
                "thread-123",
                "Company",
            )
            position = manager.ensure_session("thread-123") / "position.md"
            position.write_text("position", encoding="utf-8")

            added = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(
                {row["filename"] for row in added},
                {"company.md", "position.md"},
            )

            company.path.unlink()
            deleted = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(
                [row["filename"] for row in deleted],
                ["position.md"],
            )

    def test_context_refresh_reloads_override_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = ContextManager(temporary_directory)
            global_path = manager.global_context_dir / "answer_style.md"
            global_path.write_text("global", encoding="utf-8")

            before = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(before[0]["scope"], "GLOBAL")
            self.assertEqual(before[0]["filename"], "answer_style.md")

            session = manager.create_context(
                "session",
                "thread-123",
                "Answer Style",
            )
            overridden = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(len(overridden), 1)
            self.assertEqual(overridden[0]["scope"], "SESSION")
            self.assertEqual(overridden[0]["filename"], "answer-style.md")

            session.path.unlink()
            restored = interview_app.load_context_display_rows(
                manager,
                "thread-123",
            )
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["scope"], "GLOBAL")
            self.assertEqual(restored[0]["filename"], "answer_style.md")

    def test_sync_context_runs_backend_off_ui_path_and_refreshes_success(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.session_id = "session-local"
        dialog.context_manager = object()
        dialog.context_sync_in_progress = False
        dialog.active = True
        dialog.sync_context_button = _FakeSensitiveWidget()
        dialog.start_button = _FakeSensitiveWidget()
        dialog.session = None
        dialog.context_rows = []
        sessions = [
            {"session_id": "session-local", "version": 1},
            {"session_id": "session-local", "version": 2},
        ]

        class FakeStore:
            def get(self, _session_id):
                return sessions.pop(0)

        captured = {}

        class FakeBackend:
            def __init__(self, store, manager, factory):
                captured.update({
                    "store": store,
                    "manager": manager,
                    "factory": factory,
                })

            def create(self, session):
                captured["session"] = session
                return {"interview_thread_id": "thread-interview"}

        dialog.session_store = FakeStore()
        dialog._refresh_contexts = lambda *_args: captured.update(refresh=True)
        dialog._refresh_conversation = lambda: captured.update(
            conversation_refresh=True
        )
        dialog._show_context_error = lambda *_args, **_kwargs: captured.update(
            error=True
        )
        with patch.object(preparation_module, "InterviewThreadBackend", FakeBackend), \
             patch.object(interview_app.threading, "Thread", _ImmediateThread), \
             patch.object(
                 interview_app.GLib,
                 "idle_add",
                 side_effect=lambda callback, *args: callback(*args),
             ):
            dialog._sync_contexts()

        self.assertEqual(captured["session"]["version"], 1)
        self.assertIs(captured["store"], dialog.session_store)
        self.assertIs(captured["manager"], dialog.context_manager)
        self.assertTrue(callable(captured["factory"]))
        self.assertEqual(dialog.session["version"], 2)
        self.assertTrue(captured["refresh"])
        self.assertTrue(captured["conversation_refresh"])
        self.assertNotIn("error", captured)
        self.assertFalse(dialog.context_sync_in_progress)
        self.assertTrue(dialog.sync_context_button.sensitive)

    def test_sync_context_failure_shows_error_without_refresh(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.session_id = "session-local"
        dialog.context_manager = object()
        dialog.context_sync_in_progress = False
        dialog.active = True
        dialog.sync_context_button = _FakeSensitiveWidget()
        dialog.start_button = _FakeSensitiveWidget()
        dialog.session = None
        dialog.context_rows = []
        dialog.session_store = SimpleNamespace(
            get=lambda _session_id: {
                "session_id": "session-local",
            }
        )
        captured = {}

        class FailingBackend:
            def __init__(self, *_args):
                pass

            def create(self, _session):
                raise RuntimeError("sync failed")

        dialog._refresh_contexts = lambda *_args: captured.update(refresh=True)
        dialog._show_context_error = lambda title, detail, **_kwargs: (
            captured.update(title=title, detail=detail)
        )
        with patch.object(
            preparation_module,
            "InterviewThreadBackend",
            FailingBackend,
        ), patch.object(
            interview_app.threading,
            "Thread",
            _ImmediateThread,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            dialog._sync_contexts()

        self.assertNotIn("refresh", captured)
        self.assertIn("실패", captured["title"])
        self.assertEqual(captured["detail"], "sync failed")
        self.assertFalse(dialog.context_sync_in_progress)
        self.assertTrue(dialog.sync_context_button.sensitive)

    def test_sync_context_ignores_duplicate_click_while_running(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.context_sync_in_progress = True
        dialog.session_store = SimpleNamespace(
            get=lambda _session_id: self.fail("session must not be reloaded")
        )

        dialog._sync_contexts()

    def test_interview_start_requires_thread_and_all_contexts_synced(self):
        synced = [{"status": "SYNCED"}, {"status": "SYNCED"}]
        not_synced = [{"status": "SYNCED"}, {"status": "NOT SYNCED"}]
        changed = [{"status": "CHANGED"}]

        self.assertFalse(interview_app.can_start_interview(None, synced))
        self.assertFalse(interview_app.can_start_interview(
            {"interview_thread_id": None},
            synced,
        ))
        self.assertFalse(interview_app.can_start_interview(
            {"interview_thread_id": "thread-interview"},
            not_synced,
        ))
        self.assertFalse(interview_app.can_start_interview(
            {"interview_thread_id": "thread-interview"},
            changed,
        ))
        self.assertTrue(interview_app.can_start_interview(
            {"interview_thread_id": "thread-interview"},
            synced,
        ))

    def test_codex_off_session_can_start_without_thread_or_context_sync(self):
        self.assertFalse(interview_app.can_start_interview(None, [], False))
        self.assertTrue(interview_app.can_start_interview(
            {"interview_thread_id": None},
            [{"status": "NOT SYNCED"}],
            False,
        ))

    def test_start_button_and_live_thread_use_cached_interview_state(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.context_sync_in_progress = False
        dialog.start_button = _FakeSensitiveWidget()
        dialog.session = {"interview_thread_id": "thread-interview"}
        dialog.context_rows = [{"status": "SYNCED"}]

        dialog._update_start_button()

        self.assertTrue(dialog.start_button.sensitive)
        self.assertEqual(dialog.interview_thread_id(), "thread-interview")

        dialog.context_rows = [{"status": "CHANGED"}]
        dialog._update_start_button()

        self.assertFalse(dialog.start_button.sensitive)
        self.assertIsNone(dialog.interview_thread_id())

    def test_start_button_is_disabled_while_context_sync_runs(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.context_sync_in_progress = True
        dialog.start_button = _FakeSensitiveWidget()
        dialog.session = {"interview_thread_id": "thread-interview"}
        dialog.context_rows = [{"status": "SYNCED"}]

        dialog._update_start_button()

        self.assertFalse(dialog.start_button.sensitive)

    def test_preparation_settings_can_be_enabled_without_a_thread(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.model_combo = _FakeSensitiveWidget()
        dialog.reasoning_combo = _FakeSensitiveWidget()
        dialog.fast_combo = _FakeSensitiveWidget()
        dialog.stt_language_combo = _FakeSensitiveWidget()

        dialog._set_settings_sensitive(False)

        self.assertFalse(dialog.model_combo.sensitive)
        self.assertFalse(dialog.reasoning_combo.sensitive)
        self.assertFalse(dialog.fast_combo.sensitive)
        self.assertFalse(dialog.stt_language_combo.sensitive)

        dialog._set_settings_sensitive(True)

        self.assertTrue(dialog.model_combo.sensitive)
        self.assertTrue(dialog.reasoning_combo.sensitive)
        self.assertTrue(dialog.fast_combo.sensitive)
        self.assertTrue(dialog.stt_language_combo.sensitive)

    def test_codex_off_disables_only_codex_settings(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog.codex_enabled = False
        dialog.model_combo = _FakeSensitiveWidget()
        dialog.reasoning_combo = _FakeSensitiveWidget()
        dialog.fast_combo = _FakeSensitiveWidget()
        dialog.stt_language_combo = _FakeSensitiveWidget()

        dialog._set_settings_sensitive(True)

        self.assertFalse(dialog.model_combo.sensitive)
        self.assertFalse(dialog.reasoning_combo.sensitive)
        self.assertFalse(dialog.fast_combo.sensitive)
        self.assertTrue(dialog.stt_language_combo.sensitive)

    def test_model_catalog_load_does_not_start_or_resume_a_thread(self):
        events = []

        class CatalogClient:
            def connect(self):
                events.append("connect")

            def list_models(self):
                events.append("list_models")
                return [{"model": "gpt-test"}]

            def start(self, **_kwargs):
                raise AssertionError("Preparation must not start a Codex thread")

            def stop(self):
                events.append("stop")

        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        captured = {}
        dialog._model_catalog_finished = (
            lambda generation, models: captured.update(
                generation=generation,
                models=models,
            )
        )
        with patch.object(
            preparation_module,
            "_new_codex_client",
            return_value=CatalogClient(),
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            dialog._run_model_catalog_load(7, {"codex_model": "gpt-test"})

        self.assertEqual(events, ["connect", "list_models", "stop"])
        self.assertEqual(captured, {
            "generation": 7,
            "models": [{"model": "gpt-test"}],
        })

    def test_unsupported_model_cannot_reach_live_snapshot_with_fast_enabled(self):
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
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
            "stt_language": "en",
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
            codex_worker_module,
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
            codex_worker_module,
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


class AnswerHistoryScrollTest(unittest.TestCase):
    def test_f9_prepares_next_answer_position_before_first_stream_delta(self):
        idle_callbacks = []
        window = _FocusHistoryHarness(["Older answer", "Discard me"])
        with patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: idle_callbacks.append(
                (callback, args)
            ),
        ):
            window.discard_current_answer(remove_completed=True)
            window.prepare_corrected_answer_alignment()

            expected_start = len("Older answer\n\n")
            self.assertEqual(window.latest_answer_mark.offset, expected_start)
            self.assertEqual(
                window.focus_scroller.adjustment.get_value(),
                expected_start,
            )
            self.assertEqual(window.text.buffer.text, "Older answer\n\n")

            window.start_stream("Corrected")
            stale_callback, stale_args = idle_callbacks.pop(0)
            stale_callback(*stale_args)
            callback, args = idle_callbacks.pop(0)
            callback(*args)
            self.assertEqual(
                window.focus_scroller.adjustment.get_value(),
                expected_start,
            )
            self.assertEqual(
                window.text.buffer.text,
                "Older answer\n\nCorrected",
            )

    def test_new_answer_aligns_once_and_streaming_preserves_manual_scroll(self):
        idle_callbacks = []
        window = _FocusHistoryHarness(["First answer", "Second answer"])
        initial_set_text_calls = window.text.buffer.set_text_calls

        with patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: idle_callbacks.append(
                (callback, args)
            ),
        ):
            window.start_stream("Short answer")

            self.assertEqual(len(idle_callbacks), 1)
            callback, args = idle_callbacks.pop()
            callback(*args)
            latest_start = len("First answer\n\nSecond answer\n\n")
            self.assertEqual(window.latest_answer_mark.offset, latest_start)
            self.assertEqual(
                window.focus_scroller.adjustment.get_value(),
                latest_start,
            )
            self.assertEqual(
                window.text.buffer.text,
                "First answer\n\nSecond answer\n\nShort answer",
            )

            aligned_position = window.focus_scroller.adjustment.get_value()
            window.append_stream(" plus tokens")
            self.assertEqual(
                window.focus_scroller.adjustment.get_value(),
                aligned_position,
            )
            self.assertEqual(idle_callbacks, [])
            self.assertEqual(
                window.text.buffer.set_text_calls,
                initial_set_text_calls + 1,
            )

            window.focus_scroller.adjustment.set_value(3)
            window.append_stream(" after manual scroll")
            self.assertEqual(window.focus_scroller.adjustment.get_value(), 3)
            self.assertEqual(idle_callbacks, [])

            final_text = "Short answer plus tokens after manual scroll"
            window.finish_stream(final_text)
            self.assertEqual(window.focus_scroller.adjustment.get_value(), 3)
            self.assertEqual(
                window.text.buffer.set_text_calls,
                initial_set_text_calls + 1,
            )
            self.assertEqual(window.answer_history[-1], final_text)
            self.assertTrue(window.text.buffer.text.startswith("First answer\n\n"))
            self.assertGreater(window.latest_answer_mark.offset, 0)

    def test_finish_corrects_only_latest_answer_without_scrolling(self):
        idle_callbacks = []
        window = _FocusHistoryHarness(["Previous answer"])
        with patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: idle_callbacks.append(
                (callback, args)
            ),
        ):
            window.start_stream("Draft")
            callback, args = idle_callbacks.pop()
            callback(*args)
            window.focus_scroller.adjustment.set_value(2)
            set_text_calls = window.text.buffer.set_text_calls

            window.finish_stream("Corrected final answer")

            self.assertEqual(window.focus_scroller.adjustment.get_value(), 2)
            self.assertEqual(window.text.buffer.set_text_calls, set_text_calls)
            self.assertEqual(
                window.text.buffer.text,
                "Previous answer\n\nCorrected final answer",
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
        app.checkpoint_context = []
        app.checkpoint_barrier_waiters = []
        app.last_commit_state = None
        app.last_f7_at = None
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
        app._request_codex_answer = (
            lambda number, text, **_kwargs: requests.append((number, text))
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

    def test_f7_checkpoint_does_not_commit_question_or_call_codex(self):
        app = self._app(codex_enabled=True)
        app._moonshine_checkpoint_ready(
            time.perf_counter(),
            {
                **self._result("Long interviewer question so far", 16_000),
                "commit_source": "f7",
                "checkpoint_saved": True,
            },
            None,
        )

        self.assertEqual(app.question_count, 0)
        self.assertEqual(app.conversation_context, [])
        self.assertEqual(app.codex_worker.jobs, [])
        self.assertEqual(len(app.codex_worker.inject_jobs), 1)
        self.assertIsNone(app.last_commit_state)
        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_F7,
        )

    def test_f7_inject_order_and_f8_barrier_with_failure_fallback(self):
        app = self._app(codex_enabled=True)
        checkpoints = ("first context", "second context")
        for index, text in enumerate(checkpoints, start=1):
            app._moonshine_checkpoint_ready(
                time.perf_counter(),
                {
                    **self._result(text, index * 10_000),
                    "commit_source": "f7",
                    "checkpoint_saved": True,
                },
                None,
            )

        injected_texts = [
            job["items"][0]["content"][0]["text"]
            for job in app.codex_worker.inject_jobs
        ]
        self.assertEqual(injected_texts, [
            f"{interview_app.INTERVIEW_CHECKPOINT_MARKER}\nfirst context",
            f"{interview_app.INTERVIEW_CHECKPOINT_MARKER}\nsecond context",
        ])
        self.assertTrue(all(
            job["items"][0]["role"] == "user"
            for job in app.codex_worker.inject_jobs
        ))

        current_question = "post checkpoint question"
        app._moonshine_question_ready(
            None,
            time.perf_counter(),
            self._result(current_question, 30_000),
            None,
            commit_source="f8",
        )
        self.assertEqual(app.codex_worker.jobs, [])

        first, second = app.codex_worker.inject_jobs
        first["callback"]({"item_count": 1}, None)
        self.assertEqual(app.codex_worker.jobs, [])
        second["callback"](None, RuntimeError("inject failed"))

        self.assertEqual(len(app.codex_worker.jobs), 1)
        prompt = app.codex_worker.jobs[0]["prompt"]
        self.assertNotIn("first context", prompt)
        self.assertIn(
            f"{interview_app.INTERVIEW_CHECKPOINT_MARKER}\nsecond context",
            prompt,
        )
        self.assertIn(
            f"CURRENT INTERVIEWER QUESTION:\n{current_question}",
            prompt,
        )
        self.assertEqual(
            [checkpoint["status"] for checkpoint in app.checkpoint_context],
            ["injected", "failed"],
        )
        self.assertFalse(app.checkpoint_context[1]["fallback_consumed"])
        app.codex_worker.jobs[0]["callback"]({
            "text": "answer",
            "elapsed": 0.1,
            "first_token_seconds": 0.05,
            "first_visible_seconds": 0.05,
            "stream_delta_count": 1,
            "thread_id": "thread-test",
            "turn_id": "turn-test",
        }, None)
        self.assertTrue(app.checkpoint_context[1]["fallback_consumed"])

        app._moonshine_question_ready(
            None,
            time.perf_counter(),
            self._result("next question", 40_000),
            None,
            commit_source="f8",
        )
        self.assertNotIn("second context", app.codex_worker.jobs[1]["prompt"])

    def test_f8_transcription_uses_busy_indicator(self):
        app = self._app()
        app.last_f8_at = None
        app.moonshine_ready = True
        app.audio_started = True
        app.remote_audio = SimpleNamespace(
            capture_sample_cursor_and=lambda enqueue: (
                24_000,
                enqueue(24_000),
            )
        )
        app.asr_worker = SimpleNamespace(
            request_snapshot=lambda _cursor, _callback: True,
        )

        app._on_f8()

        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_THINKING,
        )

    def test_f7_uses_atomic_cursor_capture_and_checkpoint_request(self):
        app = self._app()
        app.moonshine_ready = True
        app.audio_started = True
        requested = []
        app.remote_audio = SimpleNamespace(
            capture_sample_cursor_and=lambda enqueue: (
                24_000,
                enqueue(24_000),
            )
        )
        app.asr_worker = SimpleNamespace(
            request_checkpoint=lambda cursor, callback: (
                requested.append((cursor, callback)),
                True,
            )[1],
        )

        app._on_f7()

        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0][0], 24_000)
        self.assertEqual(app.question_count, 0)
        self.assertEqual(app.conversation_context, [])
        self.assertEqual(app.answer_window.status, "Saving checkpoint…")
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_THINKING,
        )

    def test_successful_f7_ends_previous_f8_f9_continuation_chain(self):
        app = self._app()
        self._commit(app, 1, "previous question", 10_000, "f8")
        app._moonshine_checkpoint_ready(
            time.perf_counter(),
            {
                **self._result("next question context", 20_000),
                "commit_source": "f7",
                "checkpoint_saved": True,
            },
            None,
        )
        app.last_f9_at = None
        app.moonshine_ready = True
        app.audio_started = True
        snapshot_calls = []
        app.remote_audio = SimpleNamespace(
            capture_sample_cursor_and=lambda _enqueue: snapshot_calls.append(True)
        )

        app._on_f9()

        self.assertIsNone(app.last_commit_state)
        self.assertEqual(snapshot_calls, [])
        self.assertEqual(app.conversation_context, [
            ("INTERVIEWER", "previous question"),
        ])
        self.assertEqual(
            app.answer_window.status,
            "No previous question to continue",
        )

    def test_final_f8_uses_only_post_checkpoint_transcript_as_question(self):
        app = self._app(codex_enabled=True)
        current = "current segment only"

        app._moonshine_question_ready(
            None,
            time.perf_counter(),
            self._result(current, 32_000),
            None,
            commit_source="f8",
        )

        self.assertEqual(app.question_count, 1)
        self.assertEqual(app.conversation_context, [("INTERVIEWER", current)])
        self.assertEqual(len(app.codex_worker.jobs), 1)
        prompt = app.codex_worker.jobs[0]["prompt"]
        self.assertIn(f"CURRENT INTERVIEWER QUESTION:\n{current}", prompt)

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
        app.remote_window.set_boundary_status(interview_app.BOUNDARY_STATUS_F7)

        app._moonshine_preview({"text": "New interviewer speech", "lines": []})

        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_LISTENING,
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

    def test_japanese_question_f8_and_f9_logs_use_base_ja_backend(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            log_path = Path(directory) / "session.jsonl"
            app = self._app(log_path=log_path)
            app.stt_language = "ja"
            self._commit(app, 1, "質問です", 10_000, "f8")
            self._continue(app, "続きです", 20_000)
            app.last_f8_at = None
            app.last_f9_at = None
            app.moonshine_ready = True
            app.audio_started = True
            app.asr_worker = SimpleNamespace(
                request_snapshot=lambda _cursor, _callback: True,
            )
            cursors = iter((30_000, 40_000))
            app.remote_audio = SimpleNamespace(
                capture_sample_cursor_and=lambda enqueue: (
                    lambda cursor: (cursor, enqueue(cursor))
                )(next(cursors))
            )

            app._on_f8()
            app._on_f9()

            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            asr_events = [
                event for event in events
                if event["event"] in {"question", "f8_trigger", "f9_trigger"}
            ]
            self.assertEqual(len(asr_events), 4)
            self.assertEqual(
                {event["asr_backend"] for event in asr_events},
                {"moonshine-base-ja"},
            )

    def test_empty_and_duplicate_f8_do_not_consume_question_number(self):
        app = self._app(codex_enabled=True)
        app.asr_worker = SimpleNamespace(last_committed_sample_cursor=10_000)
        empty = self._result("", 10_000)
        empty["committed"] = False

        app._moonshine_question_ready(
            None, time.perf_counter(), empty, None, commit_source="f8"
        )
        app._moonshine_question_ready(
            None,
            time.perf_counter(),
            self._result("Why this role?", 20_000),
            None,
            commit_source="f8",
        )

        self.assertEqual(app.question_count, 1)
        self.assertEqual(len(app.codex_worker.jobs), 1)
        self.assertEqual(app.last_commit_state["question_number"], 1)

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
        self._commit(app, 1, "Tell me about a project where you", 10_000, "f8")
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
        self.assertEqual(
            app.answer_window.response_status,
            interview_app.RESPONSE_STATUS_UPDATING,
        )


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

    def test_unexpected_ffmpeg_eof_reports_audio_error_once(self):
        errors = []
        process = SimpleNamespace(
            stdout=io.BytesIO(b""),
            stderr=io.BytesIO(b"pulse input failed\n"),
            poll=lambda: 1,
        )
        stream = interview_app.AudioStream(
            "INTERVIEWER",
            "unused",
            lambda *_args: None,
            lambda role, error: errors.append((role, str(error))),
        )
        stream.process = process
        stream._read_stderr()

        stream._read_loop()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "INTERVIEWER")
        self.assertIn("Audio capture stopped unexpectedly", errors[0][1])
        self.assertIn("pulse input failed", errors[0][1])


class RuntimeLifecycleRegressionTest(unittest.TestCase):
    def test_shutdown_continues_cleanup_after_individual_failures(self):
        calls = []

        class Resource:
            def __init__(self, name, fail=False):
                self.name = name
                self.fail = fail

            def stop(self):
                calls.append(self.name)
                if self.fail:
                    raise RuntimeError(f"{self.name} failed")

        class Window:
            def hide(self):
                calls.append("hide")

        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.running = True
        app.exit_action = None
        app._save_window_state = lambda: (_ for _ in ()).throw(
            PermissionError("window state is read-only")
        )
        app.remote_audio = Resource("audio")
        app.asr_worker = Resource("moonshine", fail=True)
        app.codex_worker = Resource("codex")
        app.trigger_socket = None
        app.trigger_lock_file = None
        app.log_path = None
        app.question_count = 0
        app.codex_request_count = 0
        app.remote_window = Window()
        app.answer_window = Window()
        app.control_window = Window()

        with patch.object(interview_app.Gtk, "main_quit") as main_quit:
            result = app._stop("quit")

        self.assertFalse(result)
        self.assertEqual(calls[:3], ["audio", "moonshine", "codex"])
        self.assertEqual(calls.count("hide"), 3)
        main_quit.assert_called_once_with()

    def test_second_instance_is_rejected_without_touching_owner_socket(self):
        class FakeSocket:
            def bind(self, _path):
                pass

            def settimeout(self, _timeout):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            socket_path = runtime_dir / "trigger.sock"
            lock_path = runtime_dir / "trigger.lock"
            first = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            first.running = False
            first.trigger_socket = None
            first.trigger_lock_file = None
            first.socket_thread = None
            second = interview_app.InterviewApp.__new__(
                interview_app.InterviewApp
            )
            second.running = False
            second.trigger_socket = None
            second.trigger_lock_file = None
            second.socket_thread = None

            with patch.object(controller_module, "RUNTIME_DIR", runtime_dir), \
                    patch.object(controller_module, "TRIGGER_SOCKET", socket_path), \
                    patch.object(
                        controller_module,
                        "TRIGGER_LOCK_PATH",
                        lock_path,
                    ), patch.object(
                        interview_app.socket,
                        "socket",
                        return_value=FakeSocket(),
                    ), patch.object(interview_app.os, "chmod"):
                first._start_trigger_listener()
                socket_path.write_text("owner", encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, "already running"):
                    second._start_trigger_listener()

                self.assertEqual(
                    socket_path.read_text(encoding="utf-8"),
                    "owner",
                )
                first._close_trigger_listener()

    def test_repeated_pcm_rejection_aborts_audio_and_reports_once(self):
        callbacks = []

        class Audio:
            abort_calls = 0

            def abort(self):
                self.abort_calls += 1

        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.audio_failure_reported = False
        app.asr_worker = SimpleNamespace(
            submit_pcm=lambda *_args: False,
        )
        app.remote_audio = Audio()

        with patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callbacks.append(
                (callback, args)
            ),
        ):
            for cursor in range(5):
                app._moonshine_pcm(b"\0\0", cursor, cursor + 1)

        self.assertEqual(app.remote_audio.abort_calls, 1)
        self.assertEqual(len(callbacks), 1)

    def test_audio_error_clears_listening_state(self):
        app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
        app.log_path = None
        app.audio_started = True
        app.remote_window = _FakeAnswerWindow()

        with patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ):
            app._audio_error("INTERVIEWER", RuntimeError("ffmpeg exited"))

        self.assertFalse(app.audio_started)
        self.assertEqual(app.remote_window.status, "Audio error: ffmpeg exited")
        self.assertEqual(
            app.remote_window.boundary_status,
            interview_app.BOUNDARY_STATUS_ERROR,
        )

    def test_preparation_close_stops_and_joins_background_client(self):
        started = threading.Event()
        released = threading.Event()

        class BlockingClient:
            def __init__(self):
                self.stop_calls = 0

            def connect(self):
                started.set()
                self.assert_released = released.wait(timeout=2)
                raise RuntimeError("stopped")

            def list_models(self):
                return []

            def stop(self):
                self.stop_calls += 1
                released.set()

        client = BlockingClient()
        dialog = interview_app.PreparationDialog.__new__(
            interview_app.PreparationDialog
        )
        dialog._ensure_background_state()

        with patch.object(
            preparation_module,
            "_new_codex_client",
            return_value=client,
        ), patch.object(
            interview_app.GLib,
            "idle_add",
            side_effect=lambda *_args: False,
        ):
            thread = dialog._start_background_task(
                dialog._run_model_catalog_load,
                1,
                {"codex_model": "gpt-test"},
            )
            self.assertTrue(started.wait(timeout=2))
            dialog._stop_background_tasks()

        self.assertFalse(thread.is_alive())
        self.assertTrue(client.assert_released)
        self.assertGreaterEqual(client.stop_calls, 1)
        self.assertEqual(dialog.background_clients, set())
        self.assertEqual(dialog.background_threads, set())

    def test_jsonl_session_log_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            controller_module,
            "APP_DIR",
            Path(directory),
        ), patch.object(controller_module, "TEST_LOGGING", True):
            session_dir, log_path = interview_app.create_app_session()

            self.assertEqual(session_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)

    def test_benchmark_session_log_directory_includes_benchmark_type(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            controller_module,
            "APP_DIR",
            Path(directory),
        ), patch.object(controller_module, "TEST_LOGGING", True), patch.dict(
            interview_app.os.environ,
            {"INTERVIEW_BENCHMARK_TYPE": "benchmark_a"},
            clear=False,
        ):
            session_dir, _log_path = interview_app.create_app_session()

        self.assertTrue(session_dir.name.startswith("app_session_benchmark_a_"))


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
