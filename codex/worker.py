"""Ordered Codex App Server work queue for preparation and live turns."""

import os
import queue
import sys
import threading
from pathlib import Path

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRecoverableError,
    CodexAppServerTransportError,
)
from session_store import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
    normalize_codex_settings,
)


os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi
except ModuleNotFoundError:
    sys.path.append("/usr/lib/python3/dist-packages")
    import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib


APP_DIR = Path(__file__).resolve().parents[1]
CODEX_MODEL = os.environ.get("INTERVIEW_CODEX_MODEL", DEFAULT_CODEX_MODEL)
CODEX_REASONING = os.environ.get(
    "INTERVIEW_CODEX_REASONING",
    DEFAULT_CODEX_REASONING_EFFORT,
)
CODEX_FAST_MODE = False
CODEX_TIMEOUT_SECONDS = 60
CODEX_DEVELOPER_INSTRUCTIONS = """You assist a job candidate with interview preparation and live answers.
Follow the candidate's preferences, background, speaking style, and answer format established in the conversation.
When a turn starts with PREPARATION MESSAGE:, treat it as a direct preparation question from the candidate. Answer helpfully, and use the exchange to establish preferences and background for later live answers.
When a turn contains CURRENT INTERVIEWER QUESTION, return an immediately speakable answer draft in the same language as that question.
When history or prompt context contains INTERVIEWER CONTEXT CHECKPOINT:, treat its text as prior interviewer context for the next question, not as a current question or candidate statement.
Do not invent specific personal facts; ask during preparation or use adaptable wording when details are missing.
During the live interview, assume the candidate spoke your previous live-answer draft unless the later interviewer transcript indicates otherwise.
Each live-interview turn contains only conversation transcribed since the previous request plus the current interviewer question."""


class CodexWorker:
    """Run ordered context injections and turns on one App Server thread."""

    _LATEST_JOB = object()
    _INJECT_JOB = object()

    def __init__(
        self,
        on_ready,
        thread_id=None,
        *,
        model=CODEX_MODEL,
        effort=CODEX_REASONING,
        fast_mode=CODEX_FAST_MODE,
        load_model_catalog=False,
    ):
        self.jobs = queue.Queue()
        self.accepting = True
        self.on_ready = on_ready
        self.thread_id = thread_id
        self.model = model
        self.effort = effort
        self.fast_mode = bool(fast_mode)
        self.load_model_catalog = load_model_catalog
        self.client = None
        self.client_lock = threading.Lock()
        self.turn_active = threading.Event()
        self.latest_lock = threading.Lock()
        self.latest_job = None
        self.latest_token_queued = False
        self.active_latest_generation = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_model_and_effort(self, model, effort):
        """Apply Preparation selections to subsequent turns on this thread."""
        self.model = model
        self.effort = effort
        with self.client_lock:
            client = self.client
            if client is not None:
                client.model = model
                client.effort = effort

    def submit(
        self,
        prompt,
        callback,
        on_delta=None,
        interactive=False,
        on_approval=None,
        on_start=None,
    ):
        if self.accepting:
            self.jobs.put(
                (
                    prompt,
                    callback,
                    on_delta,
                    interactive,
                    on_approval,
                    on_start,
                    None,
                    None,
                )
            )

    def submit_latest(
        self,
        generation,
        prompt,
        callback,
        on_delta=None,
        on_start=None,
        on_recovery=None,
    ):
        """Keep only the newest live turn and interrupt the active one."""
        if not self.accepting:
            return False
        job = (
            prompt,
            callback,
            on_delta,
            False,
            None,
            on_start,
            on_recovery,
            generation,
        )
        interrupt_active = False
        with self.latest_lock:
            self.latest_job = job
            if not self.latest_token_queued:
                self.latest_token_queued = True
                self.jobs.put(self._LATEST_JOB)
            interrupt_active = (
                self.active_latest_generation is not None
                and self.active_latest_generation != generation
            )
        if interrupt_active:
            with self.client_lock:
                client = self.client
            if client is not None:
                client.request_interrupt()
        return True

    def submit_inject_items(self, items, callback):
        """Queue context injection in order with live turns."""
        if not self.accepting:
            return False
        self.jobs.put((self._INJECT_JOB, items, callback))
        return True

    def stop(self):
        self.accepting = False
        with self.latest_lock:
            self.latest_job = None
            self.latest_token_queued = False
        with self.client_lock:
            client = self.client
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self.jobs.put(None)
        if client is not None and self.turn_active.is_set():
            client.request_interrupt()
        self.thread.join(timeout=3)
        if client is not None:
            client.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self):
        try:
            _client, ready = self._start_client(self.thread_id)
            startup_error = None
        except Exception as caught:
            ready = None
            startup_error = caught
        GLib.idle_add(self.on_ready, ready, startup_error)

        while True:
            job = self.jobs.get()
            if job is None:
                return
            if isinstance(job, tuple) and job[0] is self._INJECT_JOB:
                _, items, callback = job
                with self.client_lock:
                    client = self.client
                if client is None:
                    result = None
                    error = startup_error or CodexAppServerTransportError(
                        "Codex App Server is unavailable"
                    )
                else:
                    try:
                        client.inject_items(items)
                        result = {"item_count": len(items)}
                        error = None
                    except Exception as caught:
                        result = None
                        error = caught
                GLib.idle_add(callback, result, error)
                continue
            if job is self._LATEST_JOB:
                with self.latest_lock:
                    job = self.latest_job
                    self.latest_job = None
                    self.latest_token_queued = False
                    if job is not None:
                        self.active_latest_generation = job[-1]
                if job is None:
                    continue
            (
                prompt,
                callback,
                on_delta,
                interactive,
                on_approval,
                on_start,
                on_recovery,
                generation,
            ) = job
            result, error = self._run_job(
                prompt,
                on_delta,
                interactive,
                on_approval,
                on_start,
                on_recovery,
                generation,
                startup_error,
            )
            if result is not None:
                startup_error = None
            if generation is not None:
                with self.latest_lock:
                    if self.active_latest_generation == generation:
                        self.active_latest_generation = None
                    if self.client is not None:
                        self.client.clear_interrupt_request()
            GLib.idle_add(callback, result, error)

    def _run_job(
        self,
        prompt,
        on_delta,
        interactive,
        on_approval,
        on_start,
        on_recovery,
        generation,
        startup_error,
    ):
        stream_callback = None
        if on_delta is not None:
            stream_callback = lambda delta, elapsed: GLib.idle_add(
                on_delta, delta, elapsed
            )
        approval_callback = None
        if on_approval is not None:
            approval_callback = lambda method, params: self._request_approval(
                on_approval,
                method,
                params,
            )

        recovery_attempts = 0
        started = False
        current_prompt = prompt
        while True:
            with self.client_lock:
                client = self.client
            if client is None:
                caught = startup_error or CodexAppServerTransportError(
                    "Codex App Server is unavailable"
                )
            else:
                try:
                    if not started and on_start is not None:
                        on_start()
                    started = True
                    self.turn_active.set()
                    try:
                        result = client.run_turn(
                            current_prompt,
                            on_delta=stream_callback,
                            interactive=interactive,
                            on_approval=approval_callback,
                        )
                    finally:
                        self.turn_active.clear()
                    result["recovery_attempts"] = recovery_attempts
                    return result, None
                except Exception as error:
                    caught = error

            can_recover = (
                generation is not None
                and recovery_attempts == 0
                and self.accepting
                and isinstance(caught, CodexAppServerRecoverableError)
                and self._generation_is_current(generation)
            )
            if not can_recover:
                if recovery_attempts:
                    stage = (
                        "failed"
                        if self._generation_is_current(generation)
                        else "superseded"
                    )
                    self._notify_recovery(on_recovery, stage, {
                        "attempt": recovery_attempts,
                        "error": str(caught),
                        "thread_id": self.thread_id,
                    })
                    if isinstance(caught, CodexAppServerRecoverableError):
                        self._discard_client(client)
                return None, caught

            recovery_attempts = 1
            self._notify_recovery(on_recovery, "started", {
                "attempt": recovery_attempts,
                "error": str(caught),
                "thread_id": self.thread_id,
            })
            try:
                _client, ready = self._restart_client()
            except Exception as recovery_error:
                self._notify_recovery(on_recovery, "failed", {
                    "attempt": recovery_attempts,
                    "error": str(recovery_error),
                    "thread_id": self.thread_id,
                })
                return None, recovery_error

            if not self._generation_is_current(generation):
                self._notify_recovery(on_recovery, "superseded", {
                    "attempt": recovery_attempts,
                    "thread_id": ready["thread_id"],
                })
                return None, caught

            self._notify_recovery(on_recovery, "resumed", {
                "attempt": recovery_attempts,
                "thread_id": ready["thread_id"],
                "startup_seconds": ready["startup_seconds"],
                "process_id": ready.get("process_id"),
            })
            current_prompt = f"""LIVE RECOVERY RETRY:
The previous attempt ended because the Codex transport failed. Any partial assistant output from that failed attempt was NOT SPOKEN by the candidate. Answer the current interviewer question again.

{prompt}"""
            startup_error = None

    def _start_client(self, thread_id):
        client = CodexAppServerClient(
            model=self.model,
            effort=self.effort,
            fast_mode=self.fast_mode,
            cwd=APP_DIR,
            developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
            timeout_seconds=CODEX_TIMEOUT_SECONDS,
        )
        with self.client_lock:
            self.client = client
        try:
            ready = client.start(
                thread_id=thread_id,
                ephemeral=thread_id is None,
            )
            if self.load_model_catalog:
                try:
                    ready["models"] = client.list_models()
                except CodexAppServerError as error:
                    ready["model_list_error"] = str(error)
        except Exception:
            with self.client_lock:
                if self.client is client:
                    self.client = None
            client.stop()
            raise
        self.thread_id = ready["thread_id"]
        return client, ready

    def _restart_client(self):
        thread_id = self.thread_id
        if not thread_id:
            raise CodexAppServerError(
                "Cannot recover Codex without a persistent thread id"
            )
        with self.client_lock:
            old_client = self.client
            self.client = None
        if old_client is not None:
            old_client.stop()
        client, ready = self._start_client(thread_id)
        if ready["thread_id"] != thread_id:
            client.stop()
            with self.client_lock:
                if self.client is client:
                    self.client = None
            raise CodexAppServerError(
                "Codex resumed a different thread during recovery"
            )
        return client, ready

    def _discard_client(self, client):
        if client is None:
            return
        with self.client_lock:
            if self.client is client:
                self.client = None
        client.stop()

    def _generation_is_current(self, generation):
        with self.latest_lock:
            if self.active_latest_generation != generation:
                return False
            return (
                self.latest_job is None
                or self.latest_job[-1] <= generation
            )

    @staticmethod
    def _notify_recovery(callback, stage, details):
        if callback is not None:
            GLib.idle_add(callback, stage, details)

    @staticmethod
    def _request_approval(callback, method, params):
        completed = threading.Event()
        decision = {"value": "decline"}

        def ask():
            try:
                decision["value"] = callback(method, params)
            finally:
                completed.set()
            return False

        GLib.idle_add(ask)
        completed.wait()
        return decision["value"]


def create_live_codex_worker(on_ready, thread_id, settings):
    """Create one live worker from an immutable session-settings snapshot."""
    snapshot = normalize_codex_settings(settings)
    return CodexWorker(
        on_ready,
        thread_id=thread_id,
        model=snapshot["codex_model"],
        effort=snapshot["codex_reasoning_effort"],
        fast_mode=snapshot["codex_fast_mode"],
    )
