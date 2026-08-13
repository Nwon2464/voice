import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from windows_port.bridge_client import WindowsBridgeClient
from windows_port.bridge_protocol import (
    AUDIO,
    STATUS,
    encode_frame,
    encode_hotkey,
    encode_status,
    read_frame,
)


ROOT = Path(__file__).resolve().parents[1]


class WindowsBridgeClientTests(unittest.TestCase):
    def test_reader_dispatches_pcm_and_status_frames(self):
        received_pcm = []
        statuses = []
        errors = []
        client = WindowsBridgeClient(received_pcm.append, statuses.append, errors.append)
        client.process = SimpleNamespace(
            stdout=io.BytesIO(
                encode_frame(AUDIO, b"\x01\x00")
                + encode_status({"event": "ready", "device": {"name": "Speakers"}})
            ),
            stderr=io.BytesIO(),
            poll=lambda: 0,
        )
        # EOF is expected from this finite test stream, not a bridge failure.
        client.closing.set()

        client._read()

        self.assertEqual(received_pcm, [b"\x01\x00"])
        self.assertEqual(statuses, [{"event": "ready", "device": {"name": "Speakers"}}])
        self.assertEqual(errors, [])

    def test_reader_preserves_hotkey_order_alongside_audio_and_status_frames(self):
        received_pcm = []
        hotkeys = []
        statuses = []
        errors = []
        client = WindowsBridgeClient(
            received_pcm.append,
            statuses.append,
            errors.append,
            on_hotkey=hotkeys.append,
        )
        client.process = SimpleNamespace(
            stdout=io.BytesIO(
                encode_frame(AUDIO, b"\x01\x00")
                + encode_hotkey("F8", 1, 100)
                + encode_status({"event": "ready"})
                + encode_hotkey("F9", 2, 200)
                + encode_frame(AUDIO, b"\x02\x00")
            ),
            stderr=io.BytesIO(),
            poll=lambda: 0,
        )
        client.closing.set()

        client._read()

        self.assertEqual(received_pcm, [b"\x01\x00", b"\x02\x00"])
        self.assertEqual([event["key"] for event in hotkeys], ["F8", "F9"])
        self.assertEqual([event["sequence"] for event in hotkeys], [1, 2])
        self.assertEqual(statuses, [{"event": "ready"}])
        self.assertEqual(errors, [])

    def test_unknown_frame_is_reported_as_an_error(self):
        errors = []
        client = WindowsBridgeClient(lambda _pcm: None, lambda _status: None, errors.append)
        client.process = SimpleNamespace(
            stdout=io.BytesIO(encode_frame(b"?")),
            stderr=io.BytesIO(),
            poll=lambda: 1,
        )

        client._read()

        self.assertEqual(len(errors), 1)
        self.assertIn("unsupported Windows bridge frame", str(errors[0]))

    def test_invalid_hotkey_frame_is_reported_as_an_error(self):
        errors = []
        client = WindowsBridgeClient(
            lambda _pcm: None,
            lambda _status: None,
            errors.append,
            on_hotkey=lambda _event: None,
        )
        client.process = SimpleNamespace(
            stdout=io.BytesIO(encode_frame(b"H", b"not-json")),
            stderr=io.BytesIO(),
            poll=lambda: 1,
        )

        client._read()

        self.assertEqual(len(errors), 1)
        self.assertIn("invalid hotkey JSON payload", str(errors[0]))

    def test_configured_windows_python_skips_windows_path_lookup(self):
        with patch.dict(os.environ, {"INTERVIEW_WINDOWS_PYTHON": "/custom/python.exe"}):
            self.assertEqual(WindowsBridgeClient._windows_python_path(), "/custom/python.exe")

    def test_windows_path_conversion_uses_wslpath(self):
        with patch("windows_port.bridge_client.subprocess.check_output", return_value="C:\\repo\\helper.py\n") as command:
            result = WindowsBridgeClient._windows_path("/home/won/voice/windows_port/bridge_helper.py")
        self.assertEqual(result, "C:\\repo\\helper.py")
        self.assertEqual(command.call_args.args[0][:2], ["wslpath", "-w"])

    def test_client_launches_repository_root_wrapper(self):
        client = WindowsBridgeClient(lambda _pcm: None, lambda _status: None, lambda _error: None)
        process = MagicMock()
        with (
            patch.object(client, "_windows_python_path", return_value="/windows/python.exe"),
            patch.object(client, "_windows_path", return_value="\\\\wsl.localhost\\Ubuntu\\home\\won\\voice\\windows_bridge_helper.py") as windows_path,
            patch("windows_port.bridge_client.subprocess.Popen", return_value=process) as popen,
            patch("windows_port.bridge_client.threading.Thread") as thread,
        ):
            client.start()

        self.assertEqual(
            windows_path.call_args.args[0], ROOT / "windows_bridge_helper.py"
        )
        self.assertEqual(
            popen.call_args.args[0],
            [
                "/windows/python.exe",
                "\\\\wsl.localhost\\Ubuntu\\home\\won\\voice\\windows_bridge_helper.py",
            ],
        )
        self.assertEqual(thread.call_count, 2)

    def test_hotkey_mode_launches_helper_without_audio_capture(self):
        client = WindowsBridgeClient(
            lambda _pcm: None,
            lambda _status: None,
            lambda _error: None,
            on_hotkey=lambda _event: None,
            capture_audio=False,
        )
        process = MagicMock()
        with (
            patch.object(client, "_windows_python_path", return_value="/windows/python.exe"),
            patch.object(client, "_windows_path", return_value="C:\\repo\\windows_bridge_helper.py"),
            patch("windows_port.bridge_client.subprocess.Popen", return_value=process) as popen,
            patch("windows_port.bridge_client.threading.Thread"),
        ):
            client.start()

        self.assertEqual(
            popen.call_args.args[0],
            [
                "/windows/python.exe",
                "C:\\repo\\windows_bridge_helper.py",
                "--hotkeys",
                "--no-audio",
            ],
        )

    def test_root_wrapper_imports_package_when_run_as_a_script(self):
        # A direct package-internal script would set sys.path[0] to
        # windows_port/.  The root wrapper keeps the repository import root.
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(ROOT / "windows_bridge_helper.py")],
                cwd=directory,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"ModuleNotFoundError", result.stderr)
        kind, payload = read_frame(io.BytesIO(result.stdout))
        self.assertEqual(kind, STATUS)
        self.assertIn(b"must run with Windows Python", payload)
