"""Raw interviewer audio capture with absolute PCM sample cursors."""

import subprocess
import threading
from collections import deque


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2


def get_interviewer_audio_source():
    sink = subprocess.check_output(
        ["pactl", "get-default-sink"], text=True
    ).strip()
    return f"{sink}.monitor"


def start_audio_capture(source):
    return subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel", "error",
            "-f", "pulse",
            "-fragment_size", "640",
            "-sample_rate", str(SAMPLE_RATE),
            "-channels", "1",
            "-i", source,
            "-f", "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


class AudioStream:
    """Capture raw PCM and forward it with an absolute sample cursor."""

    def __init__(self, role, source, on_pcm, on_error):
        self.role = role
        self.source = source
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.process = None
        self.thread = None
        self.stderr_thread = None
        self.stderr_tail = deque(maxlen=20)
        self.stopped = threading.Event()
        self.condition = threading.Condition()
        self.total_samples = 0

    def start(self):
        self.process = start_audio_capture(self.source)
        self.stderr_thread = threading.Thread(
            target=self._read_stderr,
            daemon=True,
        )
        self.stderr_thread.start()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def abort(self):
        """Stop capture without joining the current PCM reader thread."""
        self.stopped.set()
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
        with self.condition:
            self.condition.notify_all()

    def stop(self):
        self.abort()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.stderr_thread is not None:
            self.stderr_thread.join(timeout=2)

    def capture_sample_cursor_and(self, enqueue):
        """Record the absolute cursor and enqueue F8 before future PCM."""
        with self.condition:
            cursor = self.total_samples
            accepted = enqueue(cursor)
            return cursor, accepted

    def _read_loop(self):
        try:
            while not self.stopped.is_set():
                data = self.process.stdout.read(320)
                if not data:
                    if not self.stopped.is_set():
                        if self.stderr_thread is not None:
                            self.stderr_thread.join(timeout=0.2)
                        return_code = self.process.poll()
                        detail = "\n".join(self.stderr_tail).strip()
                        suffix = f": {detail}" if detail else ""
                        raise RuntimeError(
                            "Audio capture stopped unexpectedly "
                            f"(exit code {return_code}){suffix}"
                        )
                    break
                if len(data) % SAMPLE_WIDTH:
                    raise RuntimeError("Capture returned an incomplete s16le sample")

                with self.condition:
                    chunk_start = self.total_samples
                    self.total_samples += len(data) // SAMPLE_WIDTH
                    chunk_end = self.total_samples
                    self.on_pcm(data, chunk_start, chunk_end)
                    self.condition.notify_all()
        except Exception as error:
            self.on_error(self.role, error)

    def _read_stderr(self):
        stream = None if self.process is None else self.process.stderr
        if stream is None:
            return
        for line in stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            self.stderr_tail.append(line.rstrip())
