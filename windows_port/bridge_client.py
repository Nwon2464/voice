"""WSL client for the minimal native Windows WASAPI helper."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

from windows_port.bridge_protocol import AUDIO, STATUS, read_frame


class WindowsBridgeClient:
    """Launch the Windows helper through WSL interop and dispatch its frames."""

    def __init__(self, on_pcm, on_status, on_error):
        self.on_pcm = on_pcm
        self.on_status = on_status
        self.on_error = on_error
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.stderr = deque(maxlen=40)
        self.stopped = threading.Event()
        self.closing = threading.Event()

    def start(self) -> None:
        if self.process is not None:
            return
        python_path = self._windows_python_path()
        helper_path = self._windows_path(
            Path(__file__).resolve().parents[1] / "windows_bridge_helper.py"
        )
        command = [python_path, helper_path]
        device_id = os.environ.get("INTERVIEW_AUDIO_DEVICE_ID")
        if device_id:
            command.extend(["--device-id", device_id])
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def stop(self) -> None:
        self.closing.set()
        process = self.process
        if process is None:
            self.stopped.set()
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"Q")
                    process.stdin.flush()
                process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        self.stopped.set()
        if self.reader is not None:
            self.reader.join(timeout=1)
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=1)
        self.process = None

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while not self.stopped.is_set():
                kind, payload = read_frame(process.stdout)
                if kind == AUDIO:
                    self.on_pcm(payload)
                elif kind == STATUS:
                    self.on_status(json.loads(payload.decode("utf-8")))
                else:
                    raise RuntimeError(f"unsupported Windows bridge frame: {kind!r}")
        except EOFError:
            if not self.closing.is_set():
                detail = "\n".join(self.stderr).strip()
                self.on_error(
                    RuntimeError(
                        detail or f"Windows bridge exited with status {process.poll()}"
                    )
                )
        except Exception as error:
            if not self.closing.is_set():
                self.on_error(error)

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self.stderr.append(line.decode("utf-8", errors="replace").rstrip())

    @classmethod
    def _windows_python_path(cls) -> str:
        configured = os.environ.get("INTERVIEW_WINDOWS_PYTHON")
        if configured:
            return configured
        output = subprocess.check_output(
            ["cmd.exe", "/d", "/c", "echo", "%LOCALAPPDATA%"],
            text=True,
            errors="replace",
            cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
        )
        candidates = [line.strip() for line in output.splitlines() if line.strip()]
        if not candidates:
            raise RuntimeError("could not resolve %LOCALAPPDATA% through cmd.exe")
        windows_path = candidates[-1] + "\\InterviewAssistantBridge\\.venv\\Scripts\\python.exe"
        linux_path = subprocess.check_output(
            ["wslpath", "-u", windows_path], text=True
        ).strip()
        if not Path(linux_path).is_file():
            raise RuntimeError(
                "Windows bridge environment is missing; run ./setup_windows_bridge.sh"
            )
        return linux_path

    @staticmethod
    def _windows_path(path: Path) -> str:
        return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
