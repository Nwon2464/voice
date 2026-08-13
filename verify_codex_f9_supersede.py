#!/usr/bin/env python3
"""Run one real App Server F8→F9 supersede verification without GTK input.

This is intentionally a standalone operator harness, not a unit test.  It
uses the production SessionStore/InterviewThreadBackend/CodexWorker and the
production InterviewApp F8, F9, request, interrupt, and stream callbacks.
Only the Moonshine result is deterministic: fixed committed semantic snapshots
are supplied in place of physical audio and ASR timing.
"""

import json
import tempfile
import threading
import time
from pathlib import Path

import interview_app
from context_manager import ContextManager
from interview_thread_backend import InterviewThreadBackend
from session_store import SessionStore, normalize_codex_settings


TIMEOUT_SECONDS = 120
REPORT_PATH = Path("/tmp/codex_f9_supersede_result.json")
PROGRESS_PATH = Path("/tmp/codex_f9_supersede_progress.json")


class HeadlessTranscriptWindow:
    """Record the production Answer-window operations without creating GTK UI."""

    def __init__(self):
        self.status = ""
        self.boundary_status = ""
        self.response_status = ""
        self.text = ""
        self.operations = []

    def set_status(self, text):
        self.status = text
        self.operations.append(("set_status", text))

    def set_boundary_status(self, text):
        self.boundary_status = text
        self.operations.append(("set_boundary_status", text))

    def set_response_status(self, text):
        self.response_status = text
        self.operations.append(("set_response_status", text))

    def set_text(self, text):
        self.text = text
        self.operations.append(("set_text", text))

    def start_stream(self, text):
        self.text = text
        self.operations.append(("start_stream", text))

    def append_stream(self, text):
        self.text += text
        self.operations.append(("append_stream", text))

    def finish_stream(self, text):
        self.text = text
        self.operations.append(("finish_stream", text))

    def discard_current_answer(self, *, remove_completed=False):
        self.text = ""
        self.operations.append(("discard_current_answer", remove_completed))


class DeterministicSnapshotWorker:
    """Accept production F8/F9 snapshot requests for later fixed completion."""

    def __init__(self):
        self.callbacks = []
        self.last_committed_sample_cursor = 0

    def request_snapshot(self, cursor, callback):
        self.callbacks.append((cursor, callback))
        return True


def semantic_result(text, cursor):
    return {
        "text": text,
        "display_text": text,
        "lines": [{"text": text}],
        "committed": True,
        "captured_sample_cursor": cursor,
        "target_sample_cursor": cursor,
        "queued_sample_cursor": cursor,
        "consumed_sample_cursor": cursor,
        "cursor_complete": True,
        "audio_drop_samples": 0,
        "max_backlog_ms": 0.0,
        "barrier_wait_ms": 0.0,
        "force_update_ms": 0.0,
    }


def capture_at(cursor):
    def capture(enqueue):
        return cursor, enqueue(cursor), {
            "received_cursor": cursor,
            "queued_cursor": cursor,
            "consumed_cursor": cursor,
            "audio_drop_samples": 0,
        }

    return capture


def read_events(log_path):
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def event_for(events, event, generation=None):
    return next(
        (
            item for item in reversed(events)
            if item.get("event") == event
            and (generation is None or item.get("generation") == generation)
        ),
        None,
    )


def write_json(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def progress(stage):
    write_json(PROGRESS_PATH, {
        "stage": stage,
        "timestamp_monotonic": round(time.monotonic(), 3),
    })


def pump_until(predicate, timeout=TIMEOUT_SECONDS):
    """Drive GLib callbacks while waiting for the real worker thread."""
    context = interview_app.GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if context.pending():
            context.iteration(False)
            if predicate():
                return
        else:
            time.sleep(0.01)
    raise TimeoutError("timed out waiting for production Codex event")


def build_headless_app(thread_id, settings, log_path):
    app = interview_app.InterviewApp.__new__(interview_app.InterviewApp)
    app.running = True
    app.log_path = log_path
    app.codex_enabled = True
    app.codex_thread_id = thread_id
    app.codex_model = settings["codex_model"]
    app.codex_reasoning_effort = settings["codex_reasoning_effort"]
    app.codex_fast_mode = settings["codex_fast_mode"]
    app.stt_language = "en"
    app.question_count = 0
    app.codex_request_count = 0
    app.active_codex_generation = 0
    app.codex_request_states = {}
    app.codex_state_lock = threading.Lock()
    app.conversation_context = []
    app.codex_context_cursor = 0
    app.last_commit_state = None
    app.last_f8_at = None
    app.last_f9_at = None
    app.moonshine_ready = True
    app.audio_started = True
    app.remote_window = HeadlessTranscriptWindow()
    app.answer_window = HeadlessTranscriptWindow()
    app.asr_worker = DeterministicSnapshotWorker()
    app.codex_worker = interview_app.create_live_codex_worker(
        app._codex_ready,
        thread_id,
        settings,
    )
    return app


def run():
    settings = normalize_codex_settings({
        "codex_model": interview_app.CODEX_MODEL,
        "codex_reasoning_effort": interview_app.CODEX_REASONING,
        "codex_fast_mode": interview_app.CODEX_FAST_MODE,
        "stt_language": "en",
    })
    created_thread_id = None
    app = None
    try:
        progress("provisioning_persistent_selected_thread")
        with tempfile.TemporaryDirectory(prefix="codex-f9-supersede-") as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions.json")
            context_manager = ContextManager(root / "context")
            session = store.create("F9 actual supersede verification", settings=settings)
            context_manager.ensure_session(session["session_id"])
            provisioned = InterviewThreadBackend(
                store,
                context_manager,
                interview_app._new_codex_client,
            ).create(session)
            created_thread_id = provisioned["interview_thread_id"]
            log_path = root / "events.jsonl"
            log_path.touch(mode=0o600)
            app = build_headless_app(created_thread_id, settings, log_path)

            progress("waiting_for_resumed_app_server")
            pump_until(lambda: event_for(read_events(log_path), "codex_app_server_ready"))

            first_question = (
                "Please give a detailed immediately speakable answer in twenty "
                "concise numbered points, each with a concrete example: how "
                "would you lead a complex cross-functional product launch?"
            )
            app._on_f8(capture_sample_cursor_and=capture_at(16_000))
            cursor, callback = app.asr_worker.callbacks.pop(0)
            callback(semantic_result(first_question, cursor), None)
            progress("waiting_for_generation_1_stream")
            pump_until(lambda: event_for(
                read_events(log_path), "codex_stream_start", generation=1
            ))
            first_stream = event_for(
                read_events(log_path), "codex_stream_start", generation=1
            )
            if app.codex_request_states[1]["status"] != "running":
                raise RuntimeError("generation 1 was no longer active at F9")

            f9_started = time.perf_counter()
            app._on_f9(capture_sample_cursor_and=capture_at(32_000))
            cursor, callback = app.asr_worker.callbacks.pop(0)
            callback(
                semantic_result(
                    "and how would you measure success after launch?",
                    cursor,
                ),
                None,
            )
            progress("waiting_for_generation_1_supersede")
            pump_until(lambda: event_for(
                read_events(log_path), "codex_request_superseded", generation=1
            ))
            supersede_latency_ms = round(
                (time.perf_counter() - f9_started) * 1000,
                1,
            )
            progress("waiting_for_generation_2_stream")
            pump_until(lambda: event_for(
                read_events(log_path), "codex_stream_start", generation=2
            ))
            progress("waiting_for_generation_2_response")
            pump_until(lambda: event_for(
                read_events(log_path), "codex_response", generation=2
            ))
            progress("waiting_for_generation_1_finish")
            pump_until(lambda: event_for(
                read_events(log_path), "codex_superseded_finished", generation=1
            ))

            events = read_events(log_path)
            f8_question = next(item for item in events if (
                item.get("event") == "question" and item.get("commit_source") == "f8"
            ))
            f9_question = next(item for item in events if (
                item.get("event") == "question"
                and item.get("commit_source") == "f9_continuation"
            ))
            request_one = event_for(events, "codex_request", generation=1)
            request_two = event_for(events, "codex_request", generation=2)
            response_two = event_for(events, "codex_response", generation=2)
            if f8_question["question"] != 1 or f9_question["question"] != 1:
                raise RuntimeError("F9 did not preserve question number 1")
            if request_one["thread_id"] != created_thread_id:
                raise RuntimeError("generation 1 did not use the selected thread")
            if request_two["thread_id"] != created_thread_id:
                raise RuntimeError("generation 2 did not reuse the selected thread")
            if response_two["thread_id"] != created_thread_id:
                raise RuntimeError("generation 2 response returned a different thread")
            if event_for(events, "codex_response", generation=1) is not None:
                raise RuntimeError("superseded generation 1 completed as a response")
            if app.answer_window.text != response_two["text"]:
                raise RuntimeError("stale generation output overwrote the current answer")

            result = {
                "ok": True,
                "thread_id": created_thread_id,
                "question_number": f9_question["question"],
                "generation_1_first_stream_seconds": first_stream["elapsed_seconds"],
                "supersede_latency_ms": supersede_latency_ms,
                "generation_1_status": app.codex_request_states[1]["status"],
                "generation_2_status": app.codex_request_states[2]["status"],
                "generation_2_response": response_two["text"],
                "stale_response_suppressed": True,
                "event_sequence": [item["event"] for item in events],
            }
            write_json(REPORT_PATH, result)
            progress("completed")
            return result
    finally:
        progress("cleanup")
        if app is not None:
            app.running = False
            app.codex_worker.stop()
        if created_thread_id is not None:
            interview_app.archive_persisted_codex_session(created_thread_id)
        progress("archived_test_thread")


if __name__ == "__main__":
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2))
    except Exception as error:
        result = {"ok": False, "error": str(error)}
        write_json(REPORT_PATH, result)
        print(json.dumps(result, ensure_ascii=False))
        raise
