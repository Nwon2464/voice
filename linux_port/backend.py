"""PulseAudio capture and GNOME hotkey transport for the GTK interview app."""

from __future__ import annotations

import fcntl
import os
import shlex
import socket
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path


SAMPLE_WIDTH = 2
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
TRIGGER_SOCKET = RUNTIME_DIR / "interview-assistant-trigger.sock"
TRIGGER_LOCK_PATH = RUNTIME_DIR / "interview-assistant-trigger.lock"
HOTKEY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant/"
)
HOTKEY_F9_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/"
    "custom-keybindings/interview-assistant-continuation/"
)


def send_app_command(command, *, trigger_socket=TRIGGER_SOCKET):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(command, str(trigger_socket))
    except OSError as error:
        print(f"Interview Assistant is not running: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


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
            "-sample_rate", "16000",
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


class LinuxPlatformBackend:
    """Own Linux-only capture, GNOME hotkeys, and Unix trigger cleanup."""

    name = "pulseaudio"

    def __init__(
        self,
        on_pcm,
        on_error,
        on_f8,
        on_f9,
        on_stop,
        on_status,
        *,
        gio,
        idle_add,
        app_command_path,
        is_running,
        audio_stream_factory=AudioStream,
    ):
        self.on_error = on_error
        self.on_f8 = on_f8
        self.on_f9 = on_f9
        self.on_stop = on_stop
        self.on_status = on_status
        self.gio = gio
        self.idle_add = idle_add
        self.app_command_path = Path(app_command_path)
        self.is_running = is_running
        source = get_interviewer_audio_source()
        self.remote_source = source
        self.audio_stream = audio_stream_factory(
            "INTERVIEWER",
            source,
            on_pcm,
            on_error,
        )
        self.trigger_socket = None
        self.trigger_lock_file = None
        self.socket_thread = None

    def prepare(self):
        self._start_trigger_listener()
        return {
            "remote_source": self.remote_source,
            "global_f8": self._install_global_f8(),
            "global_f9": self._install_global_f9(),
        }

    def start(self):
        self.audio_stream.start()

    def abort(self):
        self.audio_stream.abort()

    def stop(self):
        audio_error = None
        try:
            self.audio_stream.stop()
        except Exception as error:
            audio_error = error
        try:
            self._close_trigger_listener()
        except Exception:
            if audio_error is None:
                raise
        if audio_error is not None:
            raise audio_error

    def capture_sample_cursor_and(self, enqueue):
        return self.audio_stream.capture_sample_cursor_and(enqueue)

    def _install_global_f8(self):
        return self._install_global_hotkey(
            key="F8",
            path=HOTKEY_PATH,
            name="Interview Assistant: Capture Question",
            trigger_argument="--trigger",
        )

    def _install_global_f9(self):
        return self._install_global_hotkey(
            key="F9",
            path=HOTKEY_F9_PATH,
            name="Interview Assistant: Continue Previous Question",
            trigger_argument="--trigger-f9",
        )

    def _install_global_hotkey(self, key, path, name, trigger_argument):
        try:
            media_keys = self.gio.Settings.new(
                "org.gnome.settings-daemon.plugins.media-keys"
            )
            paths = list(media_keys.get_strv("custom-keybindings"))
            for existing_path in paths:
                setting = self.gio.Settings.new_with_path(
                    "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                    existing_path,
                )
                if setting.get_string("binding") == key and existing_path != path:
                    return "conflict"
            setting = self.gio.Settings.new_with_path(
                "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding",
                path,
            )
            command = (
                f"{shlex.quote(sys.executable)} "
                f"{shlex.quote(str(self.app_command_path))} {trigger_argument}"
            )
            setting.set_string("name", name)
            setting.set_string("command", command)
            setting.set_string("binding", key)
            if path not in paths:
                paths.append(path)
                media_keys.set_strv("custom-keybindings", paths)
            self.gio.Settings.sync()
            return "installed"
        except Exception as error:
            self.on_status({
                "event": "hotkey_error",
                "key": key,
                "error": str(error),
            })
            return f"error: {error}"

    def _start_trigger_listener(self):
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = TRIGGER_LOCK_PATH.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError("Interview Assistant is already running") from error
        self.trigger_lock_file = lock_file
        try:
            TRIGGER_SOCKET.unlink(missing_ok=True)
            self.trigger_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.trigger_socket.bind(str(TRIGGER_SOCKET))
            os.chmod(TRIGGER_SOCKET, 0o600)
            self.trigger_socket.settimeout(0.5)
        except OSError as error:
            self._close_trigger_listener()
            raise RuntimeError(f"Cannot create F8 trigger socket: {error}") from error

        def listen():
            while self.is_running():
                try:
                    data = self.trigger_socket.recv(32)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if data == b"F8":
                    self.idle_add(self.on_f8)
                elif data == b"F9":
                    self.idle_add(self.on_f9)
                elif data == b"STOP":
                    self.idle_add(self.on_stop)

        self.socket_thread = threading.Thread(target=listen, daemon=True)
        self.socket_thread.start()

    def _close_trigger_listener(self):
        socket_error = None
        try:
            if self.trigger_socket is not None:
                self.trigger_socket.close()
        except OSError as error:
            socket_error = error
        finally:
            self.trigger_socket = None
            if self.trigger_lock_file is not None:
                try:
                    TRIGGER_SOCKET.unlink(missing_ok=True)
                finally:
                    fcntl.flock(self.trigger_lock_file.fileno(), fcntl.LOCK_UN)
                    self.trigger_lock_file.close()
                    self.trigger_lock_file = None
        if socket_error is not None:
            raise socket_error
