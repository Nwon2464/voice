"""Minimal persistent Codex App Server client over stdio JSONL."""

import json
import queue
import shutil
import subprocess
import threading
import time
from collections import deque


class CodexAppServerError(RuntimeError):
    """Raised when the App Server protocol or process fails."""


class CodexAppServerClient:
    """Own one App Server process and one conversation thread."""

    def __init__(
        self,
        model,
        effort,
        cwd,
        developer_instructions,
        timeout_seconds=60,
        codex_path=None,
    ):
        self.model = model
        self.effort = effort
        self.cwd = str(cwd)
        self.developer_instructions = developer_instructions
        self.timeout_seconds = timeout_seconds
        self.codex_path = codex_path or shutil.which("codex")
        self.process = None
        self.thread_id = None
        self._request_id = 0
        self._messages = queue.Queue()
        self._notifications = deque()
        self._stderr = deque(maxlen=40)
        self._write_lock = threading.Lock()
        self._stopped = False
        self._interrupt_requested = threading.Event()

    def connect(self):
        """Start and initialize App Server without opening a thread."""
        if self._stopped:
            raise CodexAppServerError("Codex App Server is stopping")
        if not self.codex_path:
            raise CodexAppServerError("Codex CLI was not found in PATH")
        if self.process is not None:
            raise CodexAppServerError("Codex App Server is already running")

        command = [
            self.codex_path,
            "app-server",
            "--stdio",
            "--disable", "fast_mode",
            "--config", f'model_reasoning_effort="{self.effort}"',
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "interview_assistant",
                        "title": "Interview Assistant",
                        "version": "1.0.0-dev",
                    }
                },
                timeout=15,
            )
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.stop()
            raise

    def start(self, thread_id=None, ephemeral=True):
        """Connect and either create a thread or resume a persisted one."""
        started = time.perf_counter()
        self.connect()
        params = {
            "model": self.model,
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "developerInstructions": self.developer_instructions,
        }
        if thread_id:
            method = "thread/resume"
            params["threadId"] = thread_id
        else:
            method = "thread/start"
            params["ephemeral"] = ephemeral
            params["serviceName"] = "interview_assistant"
        try:
            response = self._request(method, params, timeout=15)
        except Exception:
            self.stop()
            raise
        self.thread_id = response.get("thread", {}).get("id")
        if not self.thread_id:
            self.stop()
            raise CodexAppServerError("Codex App Server returned no thread id")
        return {
            "thread_id": self.thread_id,
            "thread": response.get("thread", {}),
            "startup_seconds": time.perf_counter() - started,
        }

    def archive_thread(self, thread_id):
        """Move a persisted thread into Codex's archived session store."""
        self._ensure_running()
        self._request(
            "thread/archive",
            {"threadId": thread_id},
            timeout=15,
        )

    def inject_items(self, items):
        """Persist model-visible history items without starting a turn."""
        self._ensure_running()
        if not self.thread_id:
            raise CodexAppServerError("Codex thread has not started")
        self._request(
            "thread/inject_items",
            {
                "threadId": self.thread_id,
                "items": items,
            },
            timeout=15,
        )

    def run_turn(
        self,
        prompt,
        on_delta=None,
        interactive=False,
        on_approval=None,
    ):
        self._ensure_running()
        started = time.perf_counter()
        response = self._request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": self.model,
                "effort": self.effort,
                "approvalPolicy": "on-request" if interactive else "never",
                "sandboxPolicy": (
                    {
                        "type": "workspaceWrite",
                        "writableRoots": [self.cwd],
                        "networkAccess": False,
                    }
                    if interactive
                    else {"type": "readOnly"}
                ),
            },
            timeout=15,
        )
        turn_id = response.get("turn", {}).get("id")
        if not turn_id:
            raise CodexAppServerError("Codex App Server returned no turn id")

        deadline = started + self.timeout_seconds
        first_token_seconds = None
        first_visible_seconds = None
        stream_delta_count = 0
        delta_text = {}
        completed_messages = []
        agent_message_phases = {}
        interrupt_sent = False

        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self._terminate_after_timeout()
                raise CodexAppServerError(
                    f"Codex did not respond within {self.timeout_seconds} seconds"
                )
            if self._interrupt_requested.is_set() and not interrupt_sent:
                self._request(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": turn_id},
                    timeout=15,
                )
                interrupt_sent = True
                self._interrupt_requested.clear()
            message = self._poll_notification(min(remaining, 0.25))
            if message is None:
                continue
            method = message.get("method")
            params = message.get("params", {})
            if message.get("id") is not None and method:
                self._handle_server_request(message, on_approval)
                continue
            if params.get("threadId") != self.thread_id:
                continue
            message_turn_id = params.get("turnId") or params.get("turn", {}).get("id")
            if message_turn_id != turn_id:
                continue

            if method == "item/started":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    agent_message_phases[item.get("id", "")] = item.get("phase")
            elif method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                if delta:
                    if first_token_seconds is None:
                        first_token_seconds = time.perf_counter() - started
                    item_id = params.get("itemId", "")
                    delta_text[item_id] = delta_text.get(item_id, "") + delta
                    phase = params.get("phase") or agent_message_phases.get(item_id)
                    if on_delta is not None and phase != "commentary":
                        visible_seconds = time.perf_counter() - started
                        if first_visible_seconds is None:
                            first_visible_seconds = visible_seconds
                        stream_delta_count += 1
                        on_delta(delta, visible_seconds)
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("text", "").strip():
                    completed_messages.append(item)
            elif method == "turn/completed":
                turn = params.get("turn", {})
                status = turn.get("status")
                self._interrupt_requested.clear()
                if status != "completed":
                    error = turn.get("error") or {}
                    detail = error.get("message") or f"turn ended with status {status}"
                    raise CodexAppServerError(detail)
                break

        answer = self._choose_answer(completed_messages, delta_text)
        if not answer:
            raise CodexAppServerError("Codex returned no answer")
        return {
            "text": answer,
            "elapsed": time.perf_counter() - started,
            "first_token_seconds": first_token_seconds,
            "first_visible_seconds": first_visible_seconds,
            "stream_delta_count": stream_delta_count,
            "thread_id": self.thread_id,
            "turn_id": turn_id,
        }

    def request_interrupt(self):
        """Ask the active run_turn loop to cancel its current turn."""
        self._interrupt_requested.set()

    def stop(self):
        self._stopped = True
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _choose_answer(completed_messages, delta_text):
        final_messages = [
            item.get("text", "").strip()
            for item in completed_messages
            if item.get("phase") == "final_answer" and item.get("text", "").strip()
        ]
        if final_messages:
            return final_messages[-1]
        if completed_messages:
            return completed_messages[-1].get("text", "").strip()
        return "".join(delta_text.values()).strip()

    @staticmethod
    def conversation_turns(thread):
        """Extract displayable user/final-answer text grouped by turn."""
        displayed = []
        for turn in thread.get("turns", []):
            messages = []
            for item in turn.get("items", []):
                item_type = item.get("type")
                if item_type == "userMessage":
                    text = CodexAppServerClient._content_text(item.get("content", []))
                    if text:
                        messages.append({"role": "user", "text": text})
                elif item_type == "agentMessage":
                    phase = item.get("phase")
                    text = item.get("text", "").strip()
                    if text and phase != "commentary":
                        messages.append({"role": "assistant", "text": text})
            if messages:
                displayed.append(messages)
        return displayed

    @staticmethod
    def _content_text(content):
        return "\n".join(
            part.get("text", "").strip()
            for part in content
            if part.get("type") in ("text", "input_text", "output_text")
            and part.get("text", "").strip()
        )

    def _handle_server_request(self, message, on_approval):
        method = message.get("method", "")
        params = message.get("params", {})
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            decision = "decline"
            if on_approval is not None:
                decision = on_approval(method, params)
            self._send({
                "id": message["id"],
                "result": {"decision": decision},
            })
            return
        self._send({
            "id": message["id"],
            "error": {
                "code": -32601,
                "message": f"Unsupported server request: {method}",
            },
        })

    def _request(self, method, params, timeout):
        self._request_id += 1
        request_id = self._request_id
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.perf_counter() + timeout
        deferred = []
        try:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise CodexAppServerError(f"Timed out waiting for {method}")
                message = self._next_raw_message(remaining)
                if message.get("id") != request_id:
                    deferred.append(message)
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    detail = error.get("message") or str(error)
                    raise CodexAppServerError(f"{method} failed: {detail}")
                return message.get("result") or {}
        finally:
            self._notifications.extend(deferred)

    def _send(self, message):
        self._ensure_running()
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise CodexAppServerError(f"Could not write to Codex App Server: {error}")

    def _next_notification(self, timeout):
        if self._notifications:
            return self._notifications.popleft()
        return self._next_raw_message(timeout)

    def _poll_notification(self, timeout):
        if self._notifications:
            return self._notifications.popleft()
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty:
            return None
        return self._check_transport_message(message)

    def _next_raw_message(self, timeout):
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty:
            raise CodexAppServerError("Timed out waiting for Codex App Server")
        return self._check_transport_message(message)

    def _check_transport_message(self, message):
        if message.get("_transport_eof"):
            detail = "\n".join(self._stderr).strip()
            if not detail:
                code = None if self.process is None else self.process.poll()
                detail = f"process exited with status {code}"
            raise CodexAppServerError(f"Codex App Server stopped: {detail}")
        return message

    def _read_stdout(self):
        stream = None if self.process is None else self.process.stdout
        if stream is None:
            self._messages.put({"_transport_eof": True})
            return
        for line in stream:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                self._stderr.append(f"Invalid App Server JSON: {line.rstrip()}")
        self._messages.put({"_transport_eof": True})

    def _read_stderr(self):
        stream = None if self.process is None else self.process.stderr
        if stream is None:
            return
        for line in stream:
            self._stderr.append(line.rstrip())

    def _ensure_running(self):
        if self._stopped:
            raise CodexAppServerError("Codex App Server is stopping")
        if self.process is None:
            raise CodexAppServerError("Codex App Server has not started")
        code = self.process.poll()
        if code is not None:
            detail = "\n".join(self._stderr).strip()
            suffix = f": {detail}" if detail else ""
            raise CodexAppServerError(
                f"Codex App Server exited with status {code}{suffix}"
            )

    def _terminate_after_timeout(self):
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
