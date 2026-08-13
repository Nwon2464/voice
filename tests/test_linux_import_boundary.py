import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = APP_ROOT / "interview_app.py"


class LinuxImportBoundaryTests(unittest.TestCase):
    def test_general_interview_app_import_does_not_load_linux_backend(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import interview_app; "
                    "assert 'linux_port.backend' not in sys.modules"
                ),
            ],
            cwd=APP_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_backend_selection_does_not_load_linux_backend(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from platform_backend import ("
                    "WINDOWS_BRIDGE_AUDIO_BACKEND, create_platform_backend); "
                    "create_platform_backend("
                    "WINDOWS_BRIDGE_AUDIO_BACKEND, worker=object(), "
                    "on_pcm=lambda *_args: None, on_error=lambda *_args: None, "
                    "on_f8=lambda **_kwargs: None, "
                    "on_f9=lambda **_kwargs: None, on_stop=lambda: None, "
                    "on_status=lambda _status: None); "
                    "assert 'linux_port.backend' not in sys.modules"
                ),
            ],
            cwd=APP_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_linux_cli_triggers_route_to_linux_command_sender(self):
        commands = {
            "--trigger": b"F8",
            "--trigger-f9": b"F9",
            "--stop": b"STOP",
        }
        for argument, command in commands.items():
            with self.subTest(argument=argument):
                with patch.object(sys, "argv", [str(APP_PATH), argument]):
                    with patch(
                        "linux_port.backend.send_app_command",
                        return_value=0,
                    ) as sender:
                        with self.assertRaises(SystemExit) as exit_info:
                            runpy.run_path(str(APP_PATH), run_name="__main__")

                self.assertEqual(exit_info.exception.code, 0)
                sender.assert_called_once_with(command)
